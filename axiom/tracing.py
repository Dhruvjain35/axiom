"""AXIOM :: the crash window as an X-Ray trace. A no-op everywhere except Lambda.

The submission argues about ONE instant — crash window W4, where the provider has
committed a refund and AXIOM does not yet know it. That argument currently lives in
prose, a table and a video. This module makes it telemetry: a request timeline a judge
can open in the AWS console and click through, where the receipt commit, the provider
dispatch, the kill and the later recovery under the SAME idempotency key are subsegments
with timings rather than sentences.

What it emits, and what it deliberately does not
------------------------------------------------
Four subsegments, matching the four boundaries in tasks.py that carry the argument:

    axiom.PREPARE     the receipt-minting statements  (tasks.prepare)
    axiom.dispatch    the only call that touches the world (provider.create_refund)
    axiom.SETTLE      the fused outcome + memory write (tasks.settle)
    axiom.RECOVER     the fused read-receipt-and-recall transaction (tasks.recover)

Not `claim`, not `heartbeat`, not every db.tx. A trace where everything is a subsegment
is a flame graph; a trace with exactly the four boundaries that decide whether customer
#18 is refunded twice is an argument.

`crash_window` annotates the window a crash IMMEDIATELY AFTER that boundary leaves the
system in — W2 after PREPARE (receipt durable, nothing sent), W4 after dispatch (effect
real, unrecorded). That is the filterable field: `annotation.crash_window = "W4"` in the
console returns exactly the traces this project is about.

Why this speaks the daemon protocol instead of importing aws-xray-sdk
---------------------------------------------------------------------
Because the SDK costs 30 MB and would break the build. Measured, not assumed:

    pip install --target ... aws-xray-sdk==2.15.0     (linux/aarch64, cp313)
      aws_xray_sdk   1,048 KB      wrapt      984 KB
      botocore      26,632 KB      urllib3  1,100 KB   dateutil 816 KB   jmespath 172 KB
      -> 17.7 MB zipped, on top of an 11.5 MB package

`aws-xray-sdk` declares `Requires-Dist: botocore >=1.11.3`, and build.sh fails the build
outright if botocore lands in the stage — correctly, since the Lambda runtime already
provides it and 26 MB of duplicate SDK is the fastest route to the 50 MB direct-upload
limit, which is what keeps this deployment off S3 and therefore at $0. A pip requirements
file cannot say `--no-deps` for one entry, so there is no way to list the SDK and get
only the SDK.

What the SDK would actually do for us in a Lambda is small and fully specified: build a
subsegment JSON document and send it as one UDP datagram to AWS_XRAY_DAEMON_ADDRESS,
prefixed with `{"format":"json","version":1}\\n`. That is the whole wire protocol, and
it is implemented below in about eighty lines with no dependencies at all. The trade is
stated plainly: we give up the SDK's automatic patching of boto3/psycopg and its central
sampling rules, neither of which this demo uses — Lambda makes the sampling decision and
hands it to us in `_X_AMZN_TRACE_ID`, and the four boundaries we care about are wrapped
by hand on purpose.

How it vanishes off Lambda
--------------------------
Two gates, and they catch different things.

`enabled()` is the coarse one: no AWS_LAMBDA_FUNCTION_NAME and no AWS_XRAY_DAEMON_ADDRESS
means there is nowhere to send a datagram, so pytest, scripts/ and every local run take a
branch that allocates nothing, opens no socket and needs no AWS credentials.

`_context()` is the one that decides per invocation, and it is the one that matters on a
function whose tracing config is PassThrough. Measured, because the obvious guess is
wrong: axiom-api runs with Mode=PassThrough and still logs `traced=True` at cold start —
the daemon address is exported to EVERY Lambda regardless of mode. What is absent is the
sampling decision. Lambda writes `Sampled=0` into `_X_AMZN_TRACE_ID`, `_context()` returns
None, and nothing is emitted (verified: 5 requests through the API, `aws xray
get-trace-summaries --filter-expression 'service("axiom-api")'` -> 0). The
engine itself never imports this module: `install()` wraps the four boundaries from the
OUTSIDE and is called only by the two Lambda handlers, so axiom/tasks.py and
axiom/provider.py stay unaware that AWS exists. Nothing here can fail a request either —
every emit is best-effort, and a tracing bug must never be able to take down a demo that
is being judged.
"""

from __future__ import annotations

import contextlib
import functools
import json
import logging
import os
import re
import socket
import threading
import time
import typing as t

log = logging.getLogger('axiom.tracing')

# The daemon reads a length-prefixed pair of JSON documents from one datagram: a header
# announcing the wire format, then the segment itself.
_PREAMBLE = b'{"format":"json","version":1}\n'

# X-Ray rejects a segment whose name contains anything outside this set, and a rejected
# segment is invisible rather than loud. Cheaper to sanitize than to debug an empty trace.
_NAME_OK = re.compile(r'[^\w\s_.:/%&#=+\\\-@]')

# Annotation values are indexed and filterable, which is the whole point of using them
# over metadata — but only str/int/float/bool are accepted, and long strings are wasted
# index space. 250 is far more than any key, ref or rationale we put through here needs.
_MAX_ANNOTATION_CHARS = 250

_sock: socket.socket | None = None
_addr: tuple[str, int] | None = None
_stack = threading.local()          # per-thread span stack; the heartbeat thread has its own
_installed = False
_lock = threading.Lock()


# ============================================================================== gating

def enabled() -> bool:
    """True inside a Lambda that has an X-Ray daemon to talk to. Not "will trace".

    Deliberately coarse, and named for what it checks. AWS_XRAY_DAEMON_ADDRESS is
    exported to every Lambda whatever its tracing mode, so this answers "is there a
    socket to write to", NOT "is this invocation being recorded" — `_context()` answers
    that, per invocation, from the sampling decision. Do not fold the two together: this
    one is a process-lifetime fact and the other changes on every call.

    AXIOM_XRAY=0 is the kill switch for the instrumentation, applied with a config
    update rather than a redeploy. It is NOT the cost lever — Lambda records and bills
    the trace whether or not we add subsegments to it. The function's tracing mode is.
    """
    if os.environ.get('AXIOM_XRAY') == '0':
        return False
    return bool(os.environ.get('AWS_LAMBDA_FUNCTION_NAME')
                and os.environ.get('AWS_XRAY_DAEMON_ADDRESS'))


def _context() -> tuple[str, str] | None:
    """(trace_id, parent_id) for THIS invocation, or None if it is not being traced.

    Read per span rather than cached: the runtime rewrites _X_AMZN_TRACE_ID before every
    invocation, and a warm container that cached the first one would file every later
    invocation's subsegments under the first invocation's trace.
    """
    root = parent = sampled = ''
    for part in os.environ.get('_X_AMZN_TRACE_ID', '').split(';'):
        key, _, value = part.partition('=')
        key = key.strip()
        if key == 'Root':
            root = value
        elif key == 'Parent':
            parent = value
        elif key == 'Sampled':
            sampled = value
    # Sampled=0 is a real answer, not a missing one: X-Ray decided this invocation is not
    # being recorded, and emitting anyway would bill traces nobody asked for.
    if not root or not parent or sampled != '1':
        return None
    return root, parent


# ============================================================================ the wire

def _emit(doc: dict) -> None:
    global _sock, _addr
    try:
        if _sock is None:
            host, _, port = os.environ['AWS_XRAY_DAEMON_ADDRESS'].rpartition(':')
            _addr = (host or '127.0.0.1', int(port))
            # SOCK_DGRAM to a loopback-ish address: send() cannot block and cannot fail
            # on a slow collector, which is what makes it safe to call from the dispatch
            # path of a request that is being judged.
            _sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _sock.sendto(_PREAMBLE + json.dumps(doc, default=str).encode(), _addr)
    except Exception as e:                      # noqa: BLE001 — telemetry never raises
        log.debug('x-ray emit failed (%s: %s)', type(e).__name__, e)


def _coerce(value: t.Any) -> str | int | float | bool:
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    return str(value)[:_MAX_ANNOTATION_CHARS]


class Span:
    """One subsegment. Sent twice: in_progress at open, complete at close.

    The double send is not redundancy for its own sake — it is what makes a KILL visible.
    `os._exit(9)` runs no finally block, so a span that only emitted on close would leave
    the crash as a gap in the trace with nothing to point at. Emitting at open means the
    console shows `axiom.dispatch` still in progress on the invocation that died, which
    is precisely the W4 picture: the call went out, and the process never came back to
    say what happened.
    """

    __slots__ = ('id', 'name', 'trace_id', 'parent_id', 'start', 'annotations', 'closed')

    def __init__(self, name: str, trace_id: str, parent_id: str, annotations: dict):
        self.id = os.urandom(8).hex()
        self.name = _NAME_OK.sub('_', name)[:200]
        self.trace_id = trace_id
        self.parent_id = parent_id
        self.start = time.time()
        self.annotations = annotations
        self.closed = False

    def _doc(self) -> dict:
        return {'id': self.id, 'name': self.name, 'trace_id': self.trace_id,
                'parent_id': self.parent_id, 'type': 'subsegment',
                'start_time': self.start,
                'annotations': self.annotations}

    def annotate(self, **kv: t.Any) -> None:
        for key, value in kv.items():
            if value is not None:
                self.annotations[re.sub(r'\W', '_', key)] = _coerce(value)

    def open(self) -> None:
        doc = self._doc()
        doc['in_progress'] = True
        _emit(doc)

    def close(self, exc: BaseException | None = None) -> None:
        if self.closed:
            return
        self.closed = True
        doc = self._doc()
        doc['end_time'] = time.time()
        if exc is not None:
            # fault, not error: `error` is a 4xx-shaped "the caller was wrong". Everything
            # that escapes these four boundaries is the system failing or being killed.
            doc['fault'] = True
            doc['cause'] = {'working_directory': os.environ.get('LAMBDA_TASK_ROOT', ''),
                            'exceptions': [{'id': os.urandom(8).hex(),
                                            'type': type(exc).__name__,
                                            'message': str(exc)[:_MAX_ANNOTATION_CHARS]}]}
        _emit(doc)


def _stack_list() -> list[Span]:
    got = getattr(_stack, 'spans', None)
    if got is None:
        got = _stack.spans = []
    return got


@contextlib.contextmanager
def span(name: str, **annotations: t.Any) -> t.Iterator[Span | None]:
    """Open a subsegment, or do nothing at all.

    Yields None off Lambda, which callers must tolerate — that None IS the no-op path,
    and making it an object with dummy methods would mean allocating a span per PREPARE
    in a test suite that never traces anything.
    """
    ctx = _context() if enabled() else None
    if ctx is None:
        yield None
        return

    stack = _stack_list()
    trace_id, facade_id = ctx
    sp = Span(name, trace_id, stack[-1].id if stack else facade_id, {})
    sp.annotate(**annotations)
    sp.open()
    stack.append(sp)
    try:
        yield sp
    except BaseException as e:
        # BaseException on purpose: ProviderCrash inherits from it precisely so no
        # `except Exception` can turn a simulated death into a handled error, and the
        # crash is the single most important thing this module has to record.
        sp.close(e)
        raise
    finally:
        try:
            stack.pop()
        except IndexError:                      # pragma: no cover — a span stack cannot underflow
            pass
        sp.close()


def annotate(**kv: t.Any) -> None:
    """Add annotations to the innermost open span. Silent when nothing is open."""
    stack = _stack_list()
    if stack:
        stack[-1].annotate(**kv)


# ================================================================ the four boundaries

def _crash_window_of(exc: BaseException) -> str | None:
    """provider.ProviderCrash names its own window: 'CHAOS: died ... (W4)'."""
    m = re.search(r'\((W\d)\)', str(exc))
    return m.group(1) if m else None


def _wrap_prepare(fn: t.Callable) -> t.Callable:
    @functools.wraps(fn)
    def traced(cur, **kw):
        task = kw.get('task')
        with span('axiom.PREPARE',
                  task_id=getattr(task, 'id', None),
                  dedupe_key=getattr(task, 'dedupe_key', None),
                  step=kw.get('step_name'),
                  amount_cents=kw.get('amount_cents'),
                  operation=kw.get('operation')) as sp:
            result = fn(cur, **kw)
            if sp is not None:
                if result.parked:
                    # No receipt was minted, so no external call is authorized and no
                    # crash window opens. That is a different outcome, not a failure.
                    annotate(prepare_outcome='parked_on_approval',
                             approval_id=result.approval_id)
                else:
                    annotate(prepare_outcome='receipt',
                             idempotency_key=result.receipt.idempotency_key,
                             crash_window='W2')
            return result
    return traced


def _wrap_dispatch(fn: t.Callable) -> t.Callable:
    @functools.wraps(fn)
    def traced(**kw):
        with span('axiom.dispatch',
                  idempotency_key=kw.get('idempotency_key'),
                  order_ref=kw.get('order_ref'),
                  amount_cents=kw.get('amount_cents'),
                  crash_window='W4') as sp:
            try:
                result = fn(**kw)
            except BaseException as e:
                # The window a crash HERE actually landed in, off the exception itself
                # rather than off this module's assumptions: chaos_pre dies before the
                # send (W2), chaos_post after the refund is durable (W4).
                if sp is not None:
                    window = _crash_window_of(e)
                    annotate(crashed=True, crash_detail=str(e))
                    if window:
                        annotate(crash_window=window)
                raise
            if sp is not None:
                annotate(idempotent_replay=result.replayed,
                         provider_status=result.status,
                         provider_ref=result.provider_ref)
            return result
    return traced


def _wrap_settle(fn: t.Callable) -> t.Callable:
    @functools.wraps(fn)
    def traced(cur, **kw):
        task = kw.get('task')
        receipt = kw.get('receipt')
        with span('axiom.SETTLE',
                  task_id=getattr(task, 'id', None),
                  dedupe_key=getattr(task, 'dedupe_key', None),
                  idempotency_key=getattr(receipt, 'idempotency_key', None),
                  attempt_state=kw.get('outcome_state'),
                  task_state=kw.get('task_state'),
                  provider_ref=kw.get('provider_ref'),
                  idempotent_replay=bool((kw.get('response_body') or {})
                                         .get('idempotent_replay')),
                  # Nothing is outstanding once this commits. The absence of a window is
                  # the assertion worth being able to filter on.
                  crash_window='none'):
            return fn(cur, **kw)
    return traced


def _wrap_recover(fn: t.Callable) -> t.Callable:
    @functools.wraps(fn)
    def traced(cur, **kw):
        task = kw.get('task')
        with span('axiom.RECOVER',
                  task_id=getattr(task, 'id', None),
                  dedupe_key=getattr(task, 'dedupe_key', None),
                  step=kw.get('step_name'),
                  crash_window='W4') as sp:
            plan = fn(cur, **kw)
            if sp is not None:
                annotate(recovery_action=plan.action,
                         memories_recalled=len(plan.recalled),
                         rationale=plan.rationale,
                         live_receipt=plan.receipt is not None,
                         idempotency_key=getattr(plan.receipt, 'idempotency_key', None))
            return plan
    return traced


def install() -> bool:
    """Wrap the four boundaries. Called ONLY from a Lambda handler; no-op otherwise.

    Deliberately monkeypatching from the outside instead of decorating tasks.py. The
    engine has no AWS in it and should not acquire any to be observable: axiom.tasks does
    not import this module, the test suite exercises the unwrapped functions, and the
    entire instrumentation disappears by not calling this. worker.py resolves
    `tasks.prepare` and `provider.create_refund` through the module object at call time,
    so replacing the attribute is enough — and it reaches the inline worker in the API
    process as well as the worker Lambda, from one call site each.

    Returns whether anything was actually wrapped, so a handler can log the truth.
    """
    global _installed
    with _lock:
        if _installed or not enabled():
            return False
        try:
            from . import provider, tasks
            tasks.prepare = _wrap_prepare(tasks.prepare)
            tasks.settle = _wrap_settle(tasks.settle)
            tasks.recover = _wrap_recover(tasks.recover)
            provider.create_refund = _wrap_dispatch(provider.create_refund)
        except Exception as e:                  # noqa: BLE001 — never fail a cold start
            log.warning('x-ray instrumentation not installed (%s: %s)',
                        type(e).__name__, e)
            return False
        _installed = True
        log.info('x-ray active: PREPARE / dispatch / SETTLE / RECOVER are traced')
        return True
