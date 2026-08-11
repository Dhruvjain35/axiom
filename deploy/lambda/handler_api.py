"""AXIOM :: the HTTP API as a Lambda function behind a Function URL.

This is not a second implementation of the API. It is `axiom.api:app` — the same
FastAPI object the container and the EC2 box serve — wrapped in the standard ASGI ->
Lambda adapter, plus the four adjustments that a frozen-and-thawed execution context
demands. Everything AXIOM-specific still lives in `axiom/`; if this file ever starts
making product decisions, that is a bug.

Why Lambda at all
-----------------
The demo URL has to answer from Aug 19 to Sep 15 on an account with $0.00 of credits.
Lambda's 1M requests + 400,000 GB-seconds per month is an ALWAYS-free allowance, not a
12-month introductory one, and a Function URL is free (an ALB is ~$16.40/month, ECS
Fargate ~$9/month, API Gateway $1.00 per million). So the entire hosted footprint here
is: two Lambda functions, one Function URL, two CloudWatch log groups on a 7-day
retention. Nothing bills at rest. `deploy/free-tier/` (one EC2 instance, ~$10.40/month) and `deploy/terraform/`
(ECS + ALB, production-shaped) both remain in the repo — this is the $0 path, not a
replacement for the story they tell.

The four things Lambda changes
------------------------------
1. **The lifespan must be OFF.** Mangum runs the ASGI lifespan protocol *per invocation*
   when `lifespan` is "auto" or "on" (see `Mangum.__call__`: the LifespanCycle is entered
   inside the request path). `axiom.api.lifespan` closes both connection pools on
   shutdown, so leaving the default would tear down and rebuild the CockroachDB pool on
   every single HTTP request — a TLS handshake to us-east-1 per request, and the exact
   opposite of what the reused execution context is for. Startup therefore runs once
   here, at import, and shutdown never runs at all: a freeze is not a shutdown, and a
   container that is about to be reclaimed does not need its sockets closed politely.

2. **A thawed container can hold a dead socket.** Between invocations the microVM is
   frozen: no keepalives, no timers, nothing notices the far end going away. psycopg's
   pool hands out whatever it holds, and `db.tx()` retries 40001 — a *serialization*
   error — not a broken connection, so a stale socket surfaces as a 500 on the first
   request after an idle period. `_revalidate_pools()` below turns that into one extra
   round trip on the first request after a gap, and zero overhead on every warm request.

3. **Cold start runs the boot checks.** `axiom.api._startup_checks()` makes four database
   round trips. Useful (it is how "SCHEMA UNREACHABLE" reaches a log instead of a
   mystery), but it must never be able to consume the invocation budget, so it runs on a
   daemon thread that is joined with a deadline and abandoned if the database is slow.

4. **Sizing is a cost decision, not a performance one.** See the arithmetic below.

Sizing: 512 MB, 30 s timeout
----------------------------
Lambda bills GB-seconds, so 1024 MB for 0.5 s costs exactly what 512 MB for 1 s costs.
The two are only equivalent when the work is CPU-bound and scales with the extra CPU
share (Lambda gives a proportional slice of a vCPU; 1769 MB is one full vCPU). AXIOM's
warm path is not CPU-bound — it is CockroachDB round trips to us-east-1 from us-east-2,
which is pure waiting and does NOT get shorter with more memory. Doubling memory
therefore doubles the cost of every warm request and buys nothing. Cold start is the only
CPU-bound part (importing FastAPI + pydantic + psycopg), and it is amortised over the
whole life of a container.

So it was measured rather than argued. Same ZIP, same account, us-east-2, arm64, three
memory sizes, numbers straight off the CloudWatch REPORT lines:

    MB    init      cold billed   warm /api/health   warm /api/crash-windows (no DB)
    256   1952 ms   2175 ms       173 ms             3.0 ms
    512   1447 ms   1635 ms       169 ms             2.7 ms
    1024  1514 ms   1710 ms       188 ms             2.8 ms

(one sweep, back to back, so the comparison is fair. Cold start is the noisiest number
here: across the whole day 512 MB initialized in 1447-2258 ms depending on the host, and
Lambda reclaims an idle container after roughly 5-15 minutes, so a judge who leaves the
tab open for lunch pays one of those again.)

Read the two right-hand columns together and the shape of this workload is undeniable:
a route that touches CockroachDB costs ~170 ms at every memory size, and the same route
without the database costs under 3 ms. 167 of those 170 ms are a cross-region round trip.
Quadrupling the memory does not move it, and 1024 MB is not measurably faster than 512
anywhere — it is only 2x the bill.

    warm request at 512 MB = 0.5 GB x 0.169 s = 0.0845 GB-s
    400,000 GB-s / 0.0845  = ~4,730,000 requests/month

so the 1,000,000-request half of the free tier binds first, by ~4.7x. Dropping to 256 MB
would double the GB-second headroom to ~9.4M requests — against a request cap of 1M,
which buys exactly nothing — while making every cold start 500 ms slower. And it would
not be safe: a steady GET peaks at 109 MB, but POST /api/demo/run-worker imports boto3
to invoke the worker and peaked at **149 MB** in production, which is 58% of a 256 MB
function. A demo that OOMs on the one button that starts the worker is not a saving.
512 MB is the point where the free tier's other half stops being the binding constraint,
the headroom is real, and the demo still feels instant.

The 30 s timeout is not about the warm path (which is ~0.1 s); it is the ceiling on
`POST /api/demo/seed`, which writes ~30 tasks and their embeddings across a
cross-region link. It also caps the damage a hung request can do: worst case 0.5 GB x
30 s = 15 GB-s, and this account's Lambda concurrency limit is 10, so the theoretical
maximum burn rate is 5 GB-s per wall-clock second no matter who finds the URL.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import typing as t

# --------------------------------------------------------------------------- logging
# The Lambda runtime installs its own root handler before this module is imported, which
# makes `logging.basicConfig()` inside `_startup_checks()` a documented no-op — and the
# runtime's default root level drops INFO on the floor. Setting the level explicitly
# here is what puts "schema OK: 12 axiom_* tables, 33 tasks" into CloudWatch, which is
# the one line that tells an operator whether the deployment is actually wired to data.
logging.getLogger().setLevel(os.environ.get('AXIOM_LOG_LEVEL', 'INFO'))
log = logging.getLogger('axiom.lambda')

_T0 = time.time()

from mangum import Mangum                       # noqa: E402 — after logging is configured

from axiom import db, provider                  # noqa: E402
from axiom.api import _startup_checks, app      # noqa: E402

# How long a container may sit idle before its pooled connections are no longer trusted.
# Lambda holds a warm container for minutes at a time and freezes it in between, so no
# keepalive runs and nothing notices CockroachDB Cloud's proxy closing an idle
# connection. 15 s is far below any plausible server-side idle timeout, which makes the
# check deliberately over-eager: being wrong in this direction costs one round trip
# (measured: 311 ms instead of 169 ms) on a request that was already going to talk to
# the database. Being wrong in the other direction costs a 500 on camera.
_STALE_AFTER_S = float(os.environ.get('AXIOM_LAMBDA_STALE_AFTER_S', '15'))

# Ceiling on the boot checks. Lambda gives an on-demand function ~10 s of init before it
# gives up and re-runs initialization inside the invocation, where it is billed and
# counts against the function timeout. Staying well under that is the whole point.
_STARTUP_DEADLINE_S = float(os.environ.get('AXIOM_LAMBDA_STARTUP_DEADLINE_S', '4'))

# Wall clock, not monotonic: the gap we care about is real elapsed time across a freeze,
# and CLOCK_MONOTONIC is not guaranteed to advance while the microVM is suspended.
_last_invocation_at = 0.0


# ==================================================================== cold start only

def _warm_pools() -> None:
    """Open the CockroachDB connections during init, and say so in the log.

    Not a latency trick, and the comment says so because it would be easy to believe
    otherwise: Lambda runs init inside the first request's wall clock and bills it
    (measured: Billed Duration 1635 ms = 1447 ms init + 187 ms handler), so the caller
    who triggers a cold start pays for these handshakes either way.

    What it buys is determinism and a log line. `pool.wait()` turns "the pool is filling
    in a background thread and the first request will block on it for an unbounded time"
    into "the pool is ready, or we know it is not, before any request is routed" — and a
    database that is unreachable says so once at INIT, in CloudWatch, instead of once per
    request in a stack trace. It logs rather than raises for the same reason
    `_startup_checks()` does: a function that refuses to initialize can never serve the
    /api/health that would explain why.
    """
    for name, get in (('axiom', db.pool), ('provider', provider.pool)):
        try:
            get().wait(timeout=_STARTUP_DEADLINE_S)
            log.info('pool warm: %s', name)
        except Exception as e:                   # noqa: BLE001 — init never raises
            log.error('pool NOT warm: %s (%s: %s)', name, type(e).__name__, e)


def _bounded_startup_checks() -> None:
    """Run `axiom.api._startup_checks()` without letting it own the cold start.

    It is the same function the uvicorn lifespan calls, called directly because Mangum's
    lifespan support is switched off (see the module docstring). On a daemon thread with
    a join deadline: if the database is slow the checks finish late, in the background,
    and their logging still lands — while the first request goes ahead regardless.
    """
    th = threading.Thread(target=_startup_checks, name='axiom-startup', daemon=True)
    th.start()
    th.join(_STARTUP_DEADLINE_S)
    if th.is_alive():
        log.warning('startup checks still running after %.1fs; continuing without them',
                    _STARTUP_DEADLINE_S)


_warm_pools()
_bounded_startup_checks()

# lifespan='off' is load-bearing, not tidiness — see §1 of the module docstring. With it
# off, Starlette never receives a scope["state"], which is fine: `Request.state` does a
# setdefault, and nothing in axiom.api reads lifespan state.
_asgi = Mangum(app, lifespan='off')

log.info('cold start complete in %.0f ms (offline=%s)',
         (time.time() - _T0) * 1000, os.environ.get('AXIOM_OFFLINE'))


# ======================================================================= thaw handling

def _revalidate_pools() -> None:
    """Test each pooled connection and replace the dead ones.

    Costs one round trip per connection currently in a pool, which on Lambda is one per
    pool: a container serves one request at a time, so AXIOM_POOL_MIN=1 is the whole
    pool. Measured end to end at 311 ms against 169 ms for the same route warm.

    `ConnectionPool.check()` runs the pool's check callback over the connections it is
    currently holding, discards the ones that raise, and schedules replacements — so a
    connection killed while the container was frozen is retired here rather than handed
    to a request that will 500 on it.

    The pools are read out of their modules' private globals ON PURPOSE. Calling the
    public `db.pool()` would CREATE a pool as a side effect of checking for one, which
    would mean opening a provider connection on a request that never touches the
    provider. Revalidation must never be the thing that connects.
    """
    for name, mod in (('axiom', db), ('provider', provider)):
        p = getattr(mod, '_pool', None)
        if p is None:
            continue
        try:
            p.check()
        except Exception as e:                   # noqa: BLE001 — never fail a request here
            log.warning('pool check failed for %s (%s: %s)', name, type(e).__name__, e)


def _is_replayable(event: dict) -> bool:
    """True only for methods where running the request twice cannot change the world.

    The retry below exists for the one failure mode a proactive check can still lose a
    race with (the connection dies between `check()` and the query). Replaying a POST to
    cover that would mean this file — the deployment wrapper — could double-submit a
    refund approval, which is the precise class of bug the entire project argues against.
    So: GET, HEAD and OPTIONS only.
    """
    method = (event.get('requestContext', {}).get('http', {}).get('method')
              or event.get('httpMethod') or '')
    return method.upper() in ('GET', 'HEAD', 'OPTIONS')


def lambda_handler(event: dict, context: t.Any) -> dict:
    """Function URL (payload format 2.0) -> ASGI -> Function URL response.

    Mangum infers the payload shape from the event; a Function URL delivers the same
    2.0 envelope as an HTTP API, which its `HTTPGateway` handler matches on. Nothing
    here is specific to the URL being public.
    """
    global _last_invocation_at

    now = time.time()
    idle_s = now - _last_invocation_at
    # First invocation in this container needs no check: `_warm_pools()` just opened
    # those connections milliseconds ago.
    thawed = _last_invocation_at > 0.0 and idle_s > _STALE_AFTER_S
    if thawed:
        log.info('thawed after %.0fs idle; revalidating pooled connections', idle_s)
        _revalidate_pools()

    try:
        response = _asgi(event, context)
    finally:
        _last_invocation_at = time.time()

    # Mangum converts an unhandled application exception into a 500 rather than letting
    # it escape (mangum/protocols/http.py), so a dead-socket failure arrives here as a
    # status code, not as a raise. 503 is deliberately NOT retried: that is
    # `RetriesExhausted` — genuine SERIALIZABLE contention, where the correct answer is
    # for the caller to retry, not for the wrapper to hide it.
    if thawed and response.get('statusCode') == 500 and _is_replayable(event):
        log.warning('500 on the first request after a %.0fs freeze; '
                    'revalidating and replaying once', idle_s)
        _revalidate_pools()
        response = _asgi(event, context)
        _last_invocation_at = time.time()

    return response
