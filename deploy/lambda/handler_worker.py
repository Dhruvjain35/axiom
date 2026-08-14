"""AXIOM :: the worker agent as an invocable Lambda.

    {"mode": "drain",  "seconds": 45, "shards": [0, 1, 2]}   normal work
    {"mode": "chaos",  "seconds": 45, "chaos_post": 1.0}     crash mid-refund, on purpose
    {"mode": "seed",   "tasks": 30,   "reset": true}         reseed the demo

Returns a JSON summary of the invocation. See THE CRASH below for the one case where it
deliberately returns nothing at all.

Why a Lambda
------------
The hosted demo has to stay up from the submission deadline through judging on a $0
budget. EC2, Fargate and an ALB all bill by the hour whether anyone is looking or not.
Lambda's 1M requests + 400,000 GB-seconds per month is ALWAYS free, not a 12-month
introductory offer, and the arithmetic below keeps the whole demo inside a couple of
percent of it. This is the same worker as the ECS path — same claim loop, same fence,
same receipts — invoked instead of daemonized.

Packaging contract
------------------
This file ships at the ZIP ROOT, next to the `axiom/` package and `root.crt`, so the
function's handler is `handler_worker.handler`. It cannot live in a package here:
`deploy.lambda.handler_worker` is a syntax error because `lambda` is a Python keyword.
That is why the build copies this file up rather than shipping the directory, and why
tests/test_lambda_worker.py loads it by path.

THE CRASH
---------
`{"mode":"chaos"}` arms the POST window in provider.py, and when it fires, worker.run()
answers it with os._exit(9). That is not a simulation of a kill; it IS one. os._exit()
leaves the interpreter's exit path entirely unexecuted: no finally blocks, no atexit
hooks, no __del__, no pool.close(), no lease released, no agent row marked DEAD, not
even a flush of buffered stdout. The refund is already durable in the provider's ledger
and the settle transaction never runs. That is crash window W4 exactly, and it is the
same state `docker kill` produces in the EC2 demo.

Being precise about what is and is not equivalent to SIGKILL, because it matters:

  * A SIGKILL arrives from OUTSIDE, at an instant the victim did not choose. This crash
    is raised by the victim's own call stack, at an instant it did choose. The DATABASE
    cannot tell the two apart — both leave a committed receipt in DISPATCHED with no
    settle and a lease that will simply lapse — but only the first proves the process
    had no opportunity to interfere on the way down. We do not claim it does.
    scripts/chaos_demo.py sends real SIGKILLs for that claim; this is how you obtain the
    same window on a platform where nobody can send your process a signal.
  * It is in one way MORE violent than the EC2 kill: os._exit(9) ends the runtime
    process, so Lambda tears down the whole execution environment. The pool, the warm
    container and every cached module go with it, and the next invocation is a cold
    start. `docker kill` on one worker leaves the box running.
  * The one thing a real SIGKILL can do that this cannot is land INSIDE the provider's
    own transaction — window W3, "the effect may or may not have happened". This crash
    is raised between statements in our process, never inside theirs. W3 is covered by
    tests/test_crash_windows.py::test_w3_dispatched_marker_never_decides_correctness.

The invocation that takes the crash returns NO response: Lambda reports "Runtime exited
with error: exit status 9" and marks it failed. That absence is the demo's signal, and
the point is what the next invocation finds — one PREPARED receipt, one refund in the
provider ledger, and a re-send under the same derived key that the provider absorbs.

Local, exactly as Lambda would call it:

    ./.venv/bin/python deploy/lambda/handler_worker.py '{"mode":"drain","seconds":20}'
    ./.venv/bin/python deploy/lambda/handler_worker.py '{"mode":"chaos","chaos_post":1.0}'
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
import uuid
from pathlib import Path

# =========================================================================================
# Everything in this block runs BEFORE the first `import axiom.*`, and has to.
# axiom.config builds a FROZEN Settings at import time, so a knob that is not in the
# environment by now is not a knob at all. tests/conftest.py does the same thing for the
# same reason.
# =========================================================================================

_ROOT = Path(__file__).resolve().parents[2]
if (_ROOT / 'axiom' / '__init__.py').is_file() and str(_ROOT) not in sys.path:
    # Running from the repo, where sys.path[0] is deploy/lambda/ and `axiom` is two
    # directories up. In the deployed ZIP this file sits at the root beside axiom/, the
    # test above is false, and nothing happens.
    sys.path.insert(0, str(_ROOT))

# Bedrock's models are enabled in this account and both answer, but its on-demand quota
# for Titan V2 is 0.0 requests/minute on a quota AWS marks non-adjustable, so the hosted
# demo runs on the deterministic local stand-ins. setdefault, not assignment: the
# function's own configuration stays the place this is declared, and AXIOM_OFFLINE=0
# still works the day that quota is nonzero.
os.environ.setdefault('AXIOM_OFFLINE', '1')

# CockroachDB Cloud BASIC is signed by its own CA, which is in neither the Lambda image's
# trust store nor `sslrootcert=system`. The build ships the cluster cert inside the ZIP;
# /var/task is where a ZIP-deployed function's files land and the only place we can point
# libpq at.
if os.environ.get('AWS_LAMBDA_FUNCTION_NAME') and os.path.exists('/var/task/root.crt'):
    os.environ.setdefault('PGSSLROOTCERT', '/var/task/root.crt')

# --- connection arithmetic ----------------------------------------------------------
# One execution environment = one Python process = one axiom pool + one provider pool,
# and Lambda gives each concurrent invocation its own environment. So the number that
# matters is connections-per-container multiplied by peak concurrency, and small is
# correct here in a way it never is for a long-lived server.
#
# Inside an invocation exactly two threads touch the axiom pool: the claim loop and the
# heartbeat thread. Nothing else in this process is concurrent.
#
#     axiom pool      min 1, max 2      claim loop + heartbeat
#     provider pool   max 6 (hard-coded in provider.py) but SINGLE-THREADED use, so 1
#     ---------------------------------------------------------------------------------
#     <= 3 connections per warm container
#
# CockroachDB Cloud BASIC does not expose its ceiling — `SHOW max_connections` on the
# live cluster returns -1, because the real limit is enforced by the serverless proxy in
# front of the gateway and is not a SQL setting. The only safe assumption is that
# connections are scarce and that a burst of COLD STARTS, not steady state, is what would
# exhaust them. With reserved concurrency capped at 4 the entire demo cannot hold more
# than ~12 connections, however hard anyone mashes the button.
#
# min 1 also matters: it keeps one connection warm across the freeze, so the common
# invocation pays no TLS handshake. lambda_worker.warm() is what makes that safe.
os.environ.setdefault('AXIOM_POOL_MIN', '1')
os.environ.setdefault('AXIOM_POOL_MAX', '2')

# --- free-tier arithmetic, at 512 MB ------------------------------------------------
# 400,000 GB-s/month free / 0.5 GB = 800,000 seconds of execution per month.
#   a full 45s drain                    22.5 GB-s   (0.006% of the grant)
#   an empty-queue drain (idle_exit)     ~0.4 GB-s   (~1.5s, the common case)
#   a 5-minute sweep, queue empty       8,640 invocations = ~6,500 GB-s/month = 1.6%
# Requests are not the binding constraint either: 8,640 of 1,000,000.
# The memory size itself is set on the function, not here — see the report.

from axiom import lambda_worker, seed as seed_mod                    # noqa: E402
from axiom.config import settings                                    # noqa: E402


def _log(msg: str) -> None:
    print(f'[{time.strftime("%H:%M:%S")}] {msg}', flush=True)


def _worker_ref(context) -> str:
    """One agent row per execution ENVIRONMENT, not per invocation.

    axiom_agent is keyed on (tenant, worker_ref) and re-registering under the same ref
    reuses the row — the property that lets an ECS task restart in place and keep its
    identity. A Lambda execution environment is the closest analogue: it survives across
    invocations and dies when the container does. Keying on aws_request_id instead would
    mint a fresh agent row per invocation and quietly turn the worker pool into a log.
    """
    stream = getattr(context, 'log_stream_name', None) or ''
    ident = stream.rsplit(']', 1)[-1][:16] or str(getattr(context, 'aws_request_id', ''))[:16]
    return f'lambda-{ident or uuid.uuid4().hex[:12]}'


# ------------------------------------------------------------------------------- modes

def _drain(event: dict, mode: str, context) -> dict:
    if mode == 'chaos':
        # Chaos is a drain with the crash windows armed. POST defaults to 1.0 because a
        # demo crash you have to wait for is not a demo: the first refund this invocation
        # lands is the one it dies on. PRE (die before the provider is ever called, W2)
        # stays off unless asked for — it proves a different, weaker property.
        pre = float(event.get('chaos_pre', 0.0))
        post = float(event.get('chaos_post', 1.0))
    else:
        pre = post = None

    with lambda_worker.chaos(pre, post):
        summary = lambda_worker.drain(
            seconds=float(event.get('seconds', 45)),
            shards=event.get('shards') or [],
            worker_ref=str(event.get('worker_ref') or _worker_ref(context)),
            remaining_ms=(context.get_remaining_time_in_millis
                          if context is not None else None),
            margin_ms=int(event.get('margin_ms', lambda_worker.DEFAULT_MARGIN_MS)),
            idle_exit=bool(event.get('idle_exit', True)),
            max_tasks=(int(event['max_tasks'])
                       if event.get('max_tasks') is not None else None))

    summary['mode'] = mode
    summary['pool'] = lambda_worker.pool_stats()
    return summary


def _seed(event: dict) -> dict:
    """Reseed the demo tenant. Not part of the worker's job; part of the demo's.

    Deliberately ignores the deadline, because seed() is ONE transaction — tenant,
    policy, mission, prior memories and every task commit together or not at all — and
    there is no half of it to hand to the next invocation. It takes a few seconds against
    the Cloud cluster, so the function's timeout has to comfortably exceed it; if that
    ever stops being true the answer is a smaller --tasks, not a split transaction.
    """
    t0 = time.monotonic()
    did_reset = bool(event.get('reset', False))
    if did_reset:
        seed_mod.reset()
    out = seed_mod.seed(n_tasks=int(event.get('tasks', 30)),
                        budget_cents=int(event.get('budget_cents', 2500_00)))
    return {'mode': 'seed', 'reset': did_reset,
            'elapsed_ms': int((time.monotonic() - t0) * 1000), **out}


# ----------------------------------------------------------------------------- handler

def handler(event, context=None):
    event = event or {}
    mode = str(event.get('mode', 'drain')).lower()
    _log(f'invoke mode={mode} offline={settings.offline} '
         f'remaining={context.get_remaining_time_in_millis() if context else None}ms')

    try:
        if mode == 'seed':
            return _seed(event)
        if mode in ('drain', 'chaos'):
            return _drain(event, mode, context)
        raise ValueError(f'unknown mode {mode!r}: expected "drain", "chaos" or "seed"')
    except Exception:
        # Log and RE-RAISE. Swallowing here would return a 200 with a plausible-looking
        # summary and let a broken invocation count as a success, which is the failure
        # mode this whole project argues against.
        #
        # Note what this does NOT catch: provider.ProviderCrash inherits BaseException
        # precisely so that no `except Exception` anywhere in the system can turn a
        # simulated death into a handled error. It passes straight through this frame to
        # worker.run(), which os._exit(9)s. tests/test_lambda_worker.py asserts that.
        _log('invocation failed:\n' + traceback.format_exc())
        raise


# --------------------------------------------------------------------- local invocation

class _LocalContext:
    """Enough of the Lambda context object to exercise the real deadline arithmetic.

    Deliberately not a stub returning a constant: a local run should be able to overrun
    its own budget exactly the way the deployed one can, so the margin logic is tested by
    every local invocation rather than only by the suite.
    """

    def __init__(self, timeout_s: float):
        self._deadline = time.monotonic() + timeout_s
        self.aws_request_id = f'local-{uuid.uuid4().hex[:12]}'
        self.function_name = 'axiom-worker-local'
        self.log_stream_name = f'local/[$LATEST]{uuid.uuid4().hex}'
        self.memory_limit_in_mb = 512

    def get_remaining_time_in_millis(self) -> int:
        return max(0, int((self._deadline - time.monotonic()) * 1000))


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    event = json.loads(argv[0]) if argv else {'mode': 'drain', 'seconds': 20}
    # Mirror how the function is configured: the platform timeout is the drain budget
    # plus the margin the drain holds back, plus a little slack for the cold start.
    timeout_s = (float(event.get('seconds', 45))
                 + float(event.get('margin_ms', lambda_worker.DEFAULT_MARGIN_MS)) / 1000.0
                 + 5.0)
    out = handler(event, _LocalContext(timeout_s))
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
