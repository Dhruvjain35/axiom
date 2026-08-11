"""AXIOM :: the worker, shaped for one Lambda invocation.

axiom/worker.py is a PROCESS. It runs until something kills it, and every decision in it
— the daemon heartbeat, the deliberately absent cleanup, the os._exit on a crash — is
written for a thing that expects to die badly and leave the database to sort it out.

A Lambda invocation is a BUDGET. It runs until a deadline the platform hands you, and if
you overrun it the platform kills you mid-statement whether you were ready or not.

This module is only the adapter between those two shapes. It does not re-implement the
claim loop, the fence, the PREPARE/DISPATCH/SETTLE split, or the crash handling; it
imports the Worker and drives it. If anything about correctness ever appears in this
file, it is in the wrong file.

Three things it does own:

1. THE DEADLINE. Worker.run() checks `worker._stop` at the TOP of its loop and nowhere
   else, so setting that event means precisely "finish the task in flight, then claim
   nothing more". A threading.Timer armed for (remaining time - margin) is therefore all
   it takes to turn the Lambda deadline into a clean stop. Overrunning it is survivable
   — a timeout kill mid-refund is crash window W4, which recovery already handles — but
   it should be an event we chose, not the way the thing normally ends.

2. WARM-CONTAINER HYGIENE. The module globals in db.py, provider.py and worker.py all
   outlive an invocation. The pool surviving is the whole point (a cold TLS handshake to
   CockroachDB Cloud costs more than the work does), but a threading.Event left set, a
   heartbeat thread left alive, or a Timer left armed is state that leaks into somebody
   else's invocation, in a process that gets FROZEN in between and cannot be reasoned
   about with normal thread intuitions.

3. CHAOS AS A PER-INVOCATION SETTING. config.Settings is frozen and built at import, on
   the assumption that a process's configuration cannot change under it. A warm Lambda
   container breaks that assumption — the same interpreter serves a plain drain and then
   a chaos run. See chaos().
"""

from __future__ import annotations

import threading
import time
import uuid
from contextlib import contextmanager
from typing import Callable, Iterator, Sequence

from . import db, worker as worker_mod
from .config import settings
from .worker import Worker

# How much of the invocation to hold back for the task already in flight.
#
# One task costs a triage, a recall, and three write transactions (prepare, dispatch,
# settle) plus the provider's own latency. Against the Cloud cluster that is well under
# a second in the mean — but the mean is not what kills you, the retry tail is: db.tx()
# will re-run a contended transaction up to AXIOM_MAX_RETRIES times with backoff, and a
# budget row under contention is exactly the row this system contends on by design.
# 6s is roughly 5x the observed cost of a task and still under 15% of a 45s invocation.
DEFAULT_MARGIN_MS = 6_000


def _log(msg: str) -> None:
    # Same line shape as worker.py's _log. CloudWatch stamps its own timestamp on every
    # line, so this one is redundant there — and kept anyway, because the demo shows the
    # Lambda logs and the EC2 logs side by side and they should read as the same worker.
    print(f'[{time.strftime("%H:%M:%S")}] {msg}', flush=True)


# ------------------------------------------------------------------------ the deadline

def budget_seconds(*, requested: float, remaining_ms: Callable[[], int] | None,
                   margin_ms: int = DEFAULT_MARGIN_MS) -> float:
    """How long this invocation may keep CLAIMING (not: how long it may run).

    Takes the callable rather than the Lambda context object so that nothing in the
    engine has to know what a context object is, and so a test can hand it a lambda.

    Returns 0.0 when there is not enough left to finish a task we would start — the
    caller must then claim nothing at all. Starting one anyway would not corrupt
    anything, but it would GUARANTEE the timeout kill lands mid-refund, which converts a
    routine invocation into a recovery the next one has to pay for.
    """
    if remaining_ms is None:
        return max(0.0, requested)
    return max(0.0, min(requested, (remaining_ms() - margin_ms) / 1000.0))


# --------------------------------------------------------------------------- the chaos

@contextmanager
def chaos(pre: float | None, post: float | None) -> Iterator[None]:
    """Arm the crash windows on the LIVE settings object for one invocation.

    config.settings is a frozen dataclass read once at import — correct for a process
    whose configuration genuinely cannot change, wrong for a warm Lambda container that
    serves {"mode":"drain"} and then {"mode":"chaos"} in the same interpreter.
    AXIOM_CHAOS_POST is never re-read after import, so the rate is written onto the
    object every reader already holds a reference to, and restored on the way out.

    The restore is best-effort by nature, and that is not a defect: the point of chaos is
    that this process may never reach the exit. A container that took the crash is torn
    down by Lambda and serves no further invocation, so there is nothing left to restore
    the setting for.
    """
    fields = (('chaos_crash_before_dispatch', pre), ('chaos_crash_after_dispatch', post))
    changing = [(name, value) for name, value in fields if value is not None]
    if not changing:
        yield
        return

    saved = {name: getattr(settings, name) for name, _ in changing}
    try:
        for name, value in changing:
            object.__setattr__(settings, name, float(value))
        yield
    finally:
        for name, value in saved.items():
            object.__setattr__(settings, name, value)


# ------------------------------------------------------------------------ warm restart

def warm() -> None:
    """Make pooled connections safe to use after the container was frozen.

    Between invocations Lambda freezes the execution environment: no timer fires, no
    keepalive goes out, and CockroachDB Cloud's proxy is free to hang up on a connection
    that has gone quiet. The pool would then hand the claim loop a dead socket — and
    db.tx() retries 40001, not connection errors, so the invocation would fail on
    something that has nothing to do with the workload.

    pool.check() runs psycopg_pool's own liveness probe over the idle connections and
    replaces the ones that did not survive the freeze. Failure here is logged rather than
    raised: if the cluster is genuinely unreachable the first db.tx() says so with a far
    better message, and a check() that times out must not be allowed to eat the budget
    the actual work needs.
    """
    try:
        db.pool().check()
    except Exception as e:                       # noqa: BLE001 — see docstring
        _log(f'pool check after freeze: {type(e).__name__}: {e}')


def _join_heartbeat(w: Worker, timeout: float = 2.0) -> None:
    """Wait for the worker's heartbeat thread to actually be gone.

    On a long-lived box a daemon thread outliving its worker is harmless — the process is
    about to exit. In Lambda the process does NOT exit: it is frozen mid-flight and
    thawed inside the NEXT invocation, where that thread would wake up, take a pooled
    connection, and heartbeat for an agent that stopped existing. Joining costs
    microseconds because Event.wait() returns the instant _stop is set, regardless of
    AXIOM_HEARTBEAT_SECONDS.

    Reaches into Worker._hb because Worker exposes no join and this task may not edit
    worker.py. The clean fix belongs there — see the report.
    """
    hb = w._hb
    if hb is not None and hb.is_alive():
        hb.join(timeout)
        if hb.is_alive():
            _log('WARNING: heartbeat thread survived the join; it holds a pooled '
                 'connection into the next invocation')


# ------------------------------------------------------------------------------ drain

def drain(*, seconds: float, shards: Sequence[int] | None = None,
          worker_ref: str | None = None,
          remaining_ms: Callable[[], int] | None = None,
          margin_ms: int = DEFAULT_MARGIN_MS,
          idle_exit: bool = True, max_tasks: int | None = None) -> dict:
    """Run the real Worker against the real queue for a bounded slice of this invocation.

    `idle_exit` defaults True and that is a cost decision, not a behavioural one: Lambda
    bills GB-milliseconds, and polling an empty queue for the remaining 43 seconds spends
    the always-free budget on nothing. An empty-queue invocation costs ~1.5s instead.
    """
    t0 = time.monotonic()
    budget = budget_seconds(requested=seconds, remaining_ms=remaining_ms,
                            margin_ms=margin_ms)
    ref = worker_ref or f'lambda-{uuid.uuid4().hex[:10]}'

    summary: dict = {
        'worker_ref': ref,
        'shards': [int(s) for s in (shards or [])],
        'budget_seconds': round(budget, 3),
        'tasks': 0,
        # Always 0 in a summary that got RETURNED, by construction. The only crash this
        # worker takes is provider.ProviderCrash, and worker.run() answers that with
        # os._exit(9): no return value, no response, no summary. The observable signal
        # for a crash is therefore the ABSENCE of this JSON — Lambda reports "Runtime
        # exited with error: exit status 9" and discards the execution environment. The
        # field is here so a caller diffing invocation summaries never special-cases it.
        'crashes': 0,
        'chaos_pre': settings.chaos_crash_before_dispatch,
        'chaos_post': settings.chaos_crash_after_dispatch,
        'stopped_by': 'no_budget',
    }

    if budget <= 0:
        # Deliberately before start(): do not even register an agent. Everything after
        # this line would be work we know we cannot finish.
        summary['elapsed_ms'] = int((time.monotonic() - t0) * 1000)
        summary['remaining_ms'] = int(remaining_ms()) if remaining_ms else None
        _log(f'{ref}: no budget left after a {margin_ms}ms margin; claiming nothing')
        return summary

    warm()
    # A warm container arrives here with _stop still SET from the previous invocation's
    # exit path, and Worker.run() would return before claiming anything.
    worker_mod._stop.clear()

    w = Worker(shards=list(summary['shards']), worker_ref=ref)
    stopper = threading.Timer(budget, worker_mod._stop.set)
    stopper.daemon = True          # an armed timer must never hold the interpreter open
    stopper.start()

    try:
        w.start()
        summary['tasks'] = w.run(max_tasks=max_tasks, idle_exit=idle_exit)
        # Read _stop BEFORE the finally sets it: right now it is set only if the timer
        # fired, which is the difference between "ran out of time" and "ran out of work".
        summary['stopped_by'] = (
            'deadline' if worker_mod._stop.is_set()
            else 'max_tasks' if max_tasks is not None and summary['tasks'] >= max_tasks
            else 'idle')
    finally:
        # Order matters. Cancel first so a timer cannot fire into the next invocation;
        # then set _stop so the heartbeat thread's wait() returns immediately; then join
        # it, so no thread is frozen with the container while holding a connection.
        stopper.cancel()
        w.stop()
        _join_heartbeat(w)

    summary['elapsed_ms'] = int((time.monotonic() - t0) * 1000)
    summary['remaining_ms'] = int(remaining_ms()) if remaining_ms else None
    _log(f'{ref}: {summary["tasks"]} tasks in {summary["elapsed_ms"]}ms '
         f'(stopped_by={summary["stopped_by"]}, remaining={summary["remaining_ms"]}ms)')
    return summary


def pool_stats() -> dict:
    """Pool counters, for eyeballing a warm container's reuse and for asserting on it.

    `pool_size` is what a burst of concurrent invocations multiplies against the cluster's
    connection ceiling, and `max_size` is the promise that it cannot grow past — reported
    together because the cap is set from the environment before axiom.config is imported,
    which makes it invisible to anything that inspects the process afterwards.
    """
    s = db.pool().get_stats()
    out = {k: s[k] for k in ('pool_size', 'pool_available', 'requests_waiting',
                             'connections_num') if k in s}
    out['max_size'] = settings.pool_max
    return out
