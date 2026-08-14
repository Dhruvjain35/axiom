"""AXIOM :: the HTTP surface.

Mission Control's backend, and the only process a judge has to reach. Everything here
is a thin, honest projection of the engine — there is deliberately no business logic in
this file. If an endpoint had to decide anything about an irreversible act, that decision
would live outside `db.tx()` and outside the fence, which is the exact class of mistake
the project exists to argue against.

Four rules this module keeps
----------------------------
1. **Every database access goes through `db.tx()`.** Reads pass `readonly=True` so the
   server can reject a write that leaked into a read path, and so CockroachDB can serve
   them without acquiring write intents. No route opens a raw connection.

2. **Route handlers are plain `def`, never `async def`.** psycopg is blocking; an
   `async def` handler would run it on the event loop and stall every other request
   under exactly the concurrency the demo creates. Plain `def` puts them on Starlette's
   threadpool, which is correct and requires no async driver.

3. **There is no kill-worker endpoint, and `POST /api/demo/run-worker` is not one.**
   Killing a process from a web route is a footgun that outlives the demo it was built
   for: the process it kills is on the same host as the API, and nothing about an HTTP
   request proves the caller meant it. `run-worker` instead STARTS a worker, and in
   `chaos` mode starts one configured to crash itself at the worst possible instant.
   Starting something that will die is a different power from reaching out and killing
   an arbitrary process — the blast radius is a task this system already recovers from
   by design. `scripts/chaos_demo.py` still owns real SIGKILL, where the operator ran
   the script on purpose.

4. **No endpoint may 500, and no endpoint may make "empty" look like "broken".**
   Judging runs unattended for four weeks. Every read goes through `demo_state.tx`,
   which survives a pooled connection the server closed while we were idle; every
   dependency failure lands on a handler that returns JSON with a reason rather than a
   stack trace; and the demo state heals itself before it is read. The rule the whole
   file obeys is that a 5xx must mean "a dependency is genuinely down", a 200 must mean
   "this is what is true", and there must be no third case.

The one endpoint that is a product feature rather than a projection is
`POST /api/memories/recall`: it returns `plan_uses_vector_index`, read out of a live
`EXPLAIN` of the statement it just ran. Every project in this competition will *claim*
vector search. This one shows the query plan, in the UI, at request time — and it would
go false the moment somebody reintroduced the subquery search vector that preflight gate
4 proved silently defeats the index.
"""

from __future__ import annotations

import concurrent.futures as cf
import contextlib
import datetime as dt
import decimal
import functools
import json
import logging
import os
import pathlib
import subprocess
import sys
import threading
import time
import typing as t
import uuid

import psycopg
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from psycopg_pool import PoolTimeout
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import (__version__, db, demo_state, embeddings, events, memory, proofs, provider,
               seed, tasks)
from .config import EMBED_DIMS, SYSTEM_TENANT, settings
from .db import RetriesExhausted
from .demo_state import Unavailable
from .models import MemoryClass, RetrievalClass
from .seed import DEMO_TENANT

log = logging.getLogger('axiom.api')

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB_DIR = REPO_ROOT / 'web'


# ============================================================== JSON serialization

def _encode(o: t.Any) -> t.Any:
    """The one place a non-JSON type becomes JSON.

    psycopg hands back UUIDs, tz-aware datetimes, timedeltas and Decimals — none of
    which json.dumps knows. Doing this once, centrally, is why no route has to remember
    to str() an id. Decimal degrades to int when it is integral (counts, cents) and to
    float otherwise, because a JSON consumer that receives "300" for a row count has to
    guess, and guessing is how a dashboard shows the wrong number.
    """
    if isinstance(o, uuid.UUID):
        return str(o)
    if isinstance(o, (dt.datetime, dt.date, dt.time)):
        return o.isoformat()
    if isinstance(o, dt.timedelta):
        return o.total_seconds()
    if isinstance(o, decimal.Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    if isinstance(o, memoryview):
        return o.tobytes().hex()
    if isinstance(o, set):
        return sorted(o)
    raise TypeError(f'{type(o).__name__} is not JSON serializable')


class AxiomJSON(JSONResponse):
    def render(self, content: t.Any) -> bytes:
        return json.dumps(content, default=_encode, allow_nan=False,
                          separators=(',', ':')).encode()


def json_route(fn: t.Callable) -> t.Callable:
    """Serialize this handler's return value with `_encode`, not with pydantic.

    Not decoration. FastAPI's fast path hands the return value to pydantic's JSON
    dumper, which serializes Decimal as a **string** — so `total_cents` arrived at the
    browser as "164803" and any UI doing arithmetic on it got string concatenation.
    CockroachDB returns DECIMAL from every `sum()` over an INT8, so that is not an edge
    case, it is the money column.

    Returning a Response instance makes FastAPI hand it back untouched (routing.py's
    `isinstance(raw_response, Response)` branch), which puts `_encode` in charge. The
    wrapper preserves __wrapped__, so dependency injection and the OpenAPI schema still
    read the real signature.
    """
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        out = fn(*a, **kw)
        return out if isinstance(out, Response) else AxiomJSON(out)
    return wrapper


# ==================================================================== startup checks

def _startup_checks() -> None:
    """Say out loud what is and is not reachable, once, at boot.

    A demo that starts cleanly against an empty database and only fails on the third
    click wastes the operator's most expensive minutes. This turns that into one line in
    the log before the first request is served. It logs rather than raises: a judge who
    reaches a running API and reads "SCHEMA UNREACHABLE" learns more than one who gets a
    connection refused from a process that killed itself at boot.
    """
    # uvicorn configures its own loggers and leaves the root logger bare, so an
    # unconfigured `axiom.*` logger drops everything below WARNING on the floor. Done
    # here rather than at import so importing axiom.api never reconfigures a host app's
    # logging as a side effect; basicConfig is a no-op if a handler already exists.
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)-5s %(name)s :: %(message)s')

    try:
        def _probe(cur):
            cur.execute("""
                SELECT count(*) AS n FROM [SHOW TABLES FROM axiom]
                WHERE table_name LIKE 'axiom\\_%' ESCAPE '\\'
            """)
            n_tables = cur.fetchone()['n']
            cur.execute('SELECT count(*) AS n FROM axiom_task')
            n_tasks = cur.fetchone()['n']
            cur.execute('SELECT count(*) AS n FROM axiom_memory')
            return n_tables, n_tasks, cur.fetchone()['n']

        n_tables, n_tasks, n_mem = demo_state.tx(_probe, readonly=True)
        log.info('schema OK: %d axiom_* tables, %d tasks, %d memories',
                 n_tables, n_tasks, n_mem)
    except Exception as e:                       # noqa: BLE001 — boot check never raises
        log.error('SCHEMA UNREACHABLE (%s: %s) — apply db/001_schema.sql and check '
                  'DATABASE_URL', type(e).__name__, e)

    # Heal at boot rather than on the first request, so a cold Lambda or a rebooted
    # instance is already showing the demo by the time anyone loads the page. It is the
    # same idempotent call the read path makes, so doing it twice costs one query.
    try:
        out = demo_state.ensure_demo()
        log.info('demo state: %s', 'seeded at boot' if out.get('seeded') else 'coherent')
    except Exception as e:                       # noqa: BLE001
        log.error('could not establish demo state (%s: %s) — the API will retry on the '
                  'first request', type(e).__name__, e)

    try:
        log.info('reaped %d stale agent rows', demo_state.reap_agents())
    except Exception as e:                       # noqa: BLE001
        log.warning('agent reap failed: %s: %s', type(e).__name__, e)

    try:
        s = provider.stats()
        log.info('provider OK: %d refunds, %d replays, %d duplicate orders',
                 s['refunds'], s['replays'], s['duplicate_orders'])
    except Exception as e:                       # noqa: BLE001
        log.error('PROVIDER UNREACHABLE (%s: %s) — apply db/003_provider.sql',
                  type(e).__name__, e)

    log.info('embed=%s llm=%s offline=%s', settings.embed_model, settings.llm_model,
             settings.offline)
    log.info('static UI: %s', WEB_DIR if WEB_DIR.is_dir() else f'{WEB_DIR} (absent)')


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    """Boot checks, then teardown of both pools.

    The checks run synchronously on the loop on purpose: nothing should be served until
    we know what we are serving against, and this is the one moment in the process's
    life where blocking is the correct behaviour.
    """
    # Before anything touches the database: cap how long a request will wait for a
    # connection. The pool's default is 30 seconds, which on a dead database turns every
    # request into a hang and an uptime monitor into a timeout rather than a 503.
    demo_state.tune_pools()
    _startup_checks()
    yield
    db.close_pool()
    provider.close_pool()


# ==================================================================== the application

app = FastAPI(
    title='AXIOM',
    version=__version__,
    description='Crash-safe agent execution and memory on CockroachDB.',
    default_response_class=AxiomJSON,
    docs_url='/api/docs',
    openapi_url='/api/openapi.json',
    lifespan=lifespan,
)

# Wide open, deliberately: this serves a single-page demo from the same origin and a
# judge may well open the static files off disk or from a different port. There is no
# authenticated state to protect — the API is read-mostly and its two mutating demo
# routes are scoped to the demo tenant.
app.add_middleware(
    CORSMiddleware, allow_origins=['*'], allow_credentials=False,
    allow_methods=['*'], allow_headers=['*'],
)


def _tenant(x_axiom_tenant: str | None = Header(default=None)) -> uuid.UUID:
    """Tenant resolution. Defaults to the demo tenant; overridable per request.

    Multi-tenancy is a product claim, and a claim you cannot exercise is a claim a judge
    has to take on faith. `-H 'X-Axiom-Tenant: <uuid>'` against any read endpoint
    returns that tenant's rows and only that tenant's rows, because every query below
    passes tenant_id into a WHERE clause that leads a secondary index.
    """
    if not x_axiom_tenant:
        return DEMO_TENANT
    try:
        return uuid.UUID(x_axiom_tenant)
    except ValueError:
        raise HTTPException(400, 'X-Axiom-Tenant must be a UUID')


# ----------------------------------------------------------------- error translation

@app.exception_handler(RetriesExhausted)
def _retries_exhausted(request, exc: RetriesExhausted):
    # 503 rather than 500: the transaction provably did not commit and the caller may
    # retry. Under SERIALIZABLE this is contention, not corruption.
    return AxiomJSON({'error': 'contention', 'detail': str(exc)}, status_code=503)


@app.exception_handler(tasks.LeaseLost)
def _lease_lost(request, exc: tasks.LeaseLost):
    return AxiomJSON({'error': 'lease_lost', 'detail': str(exc)}, status_code=409)


@app.exception_handler(psycopg.errors.InsufficientPrivilege)
def _denied(request, exc):
    return AxiomJSON({'error': 'permission_denied', 'detail': str(exc)}, status_code=403)


# ---------------------------------------------------------------------------------
# The four handlers below are the "no judge ever sees a stack trace" contract. Each
# one answers a different question, and the status codes are chosen so a monitor can
# act on them without parsing prose:
#
#   503  a dependency is down and it is not our fault and not our state — retry later
#   500  a genuine defect in this process — still JSON, still no traceback on the wire
#
# There is no handler that turns a failure into an empty 200. A judge looking at an
# empty grid must be able to tell "nothing has happened yet" from "the database is
# gone", and the only honest way to do that is the status code.

@app.exception_handler(Unavailable)
def _unavailable(request, exc: Unavailable):
    return AxiomJSON(
        {'error': f'{exc.component}_unavailable', 'detail': exc.detail,
         'component': exc.component},
        status_code=503, headers={'Retry-After': '5'})


@app.exception_handler(PoolTimeout)
def _pool_timeout(request, exc: PoolTimeout):
    return AxiomJSON({'error': 'db_unavailable', 'detail': f'connection pool: {exc}',
                      'component': 'db'},
                     status_code=503, headers={'Retry-After': '5'})


@app.exception_handler(psycopg.Error)
def _pg_error(request, exc: psycopg.Error):
    # OperationalError/InterfaceError mean the connection died; everything else that
    # reaches here is a statement this process got wrong. Both are reported as 503 with
    # the SQLSTATE, because the caller's correct move is identical (retry, then look at
    # the logs) and because a demo that says "23505" is more useful than one that says
    # "Internal Server Error".
    code = getattr(getattr(exc, 'diag', None), 'sqlstate', None)
    log.error('database error %s: %s', code, exc)
    return AxiomJSON({'error': 'database_error', 'sqlstate': code,
                      'detail': f'{type(exc).__name__}: {exc}'[:400],
                      'component': 'db'},
                     status_code=503, headers={'Retry-After': '5'})


@app.exception_handler(Exception)
def _unhandled(request, exc: Exception):
    # Starlette re-raises after this so the process still logs the traceback where an
    # operator can read it. What it does NOT do is put the traceback on the wire.
    log.exception('unhandled error on %s', request.url.path)
    return AxiomJSON({'error': 'internal', 'detail': f'{type(exc).__name__}: {exc}'[:400]},
                     status_code=500)


# =========================================================================== HEALTH

_BOOTED_AT = dt.datetime.now(dt.timezone.utc)

# The vector-index probe is an EXPLAIN, so it plans without executing — but it still
# costs a round trip, and this endpoint is what an uptime monitor hits every minute for
# four weeks. Cached, with the age of the answer reported so nobody mistakes a cached
# green light for a live one.
_VEC_TTL_S = 60.0
_vec_cache: dict[str, t.Any] = {'at': 0.0, 'value': None}
_HEALTH_UNIT_VECTOR = [1.0] + [0.0] * (EMBED_DIMS - 1)


def _vector_index_check() -> dict:
    """Is the ANN path still an ANN path? Answered from a live query plan, not a belief.

    This is the one health check in the file that is about correctness rather than
    liveness. The recall query returns identical rows when the plan degrades to a full
    primary-key scan, so nothing a human could observe in the results would catch the
    regression — only the plan does, which is why it is worth a round trip a minute.

    The probe vector is a fixed unit vector rather than a real embedding: EXPLAIN does
    not execute, so its contents cannot matter, and using it here means the health check
    never calls Bedrock.
    """
    now = time.monotonic()
    if _vec_cache['value'] is not None and now - _vec_cache['at'] < _VEC_TTL_S:
        return {**_vec_cache['value'], 'age_seconds': round(now - _vec_cache['at'], 1)}

    out: dict[str, t.Any]
    try:
        def _read(cur):
            return db.explain(cur, memory.recall_sql_for_explain(context_key=False), {
                'vec': db.vector_literal(_HEALTH_UNIT_VECTOR),
                'tenant': str(DEMO_TENANT),
                'cls': str(MemoryClass.EPISODIC),
                'rc': str(RetrievalClass.ACTIONABLE),
                'fetch': settings.recall_k * settings.recall_overfetch,
            })
        plan = demo_state.tx(_read, readonly=True)
        out = {'ok': True, 'in_use': db.uses_vector_index(plan)}
    except Exception as e:                       # noqa: BLE001 — health never raises
        out = {'ok': False, 'in_use': None, 'error': f'{type(e).__name__}: {e}'[:200]}

    _vec_cache.update(at=now, value=out)
    return {**out, 'age_seconds': 0.0}


def _storage_used() -> dict:
    """Bytes on disk for the axiom database, for free-tier headroom.

    `SHOW RANGES ... WITH DETAILS` is the only size query that survives CockroachDB
    Cloud's restriction on crdb_internal (measured: `crdb_internal.ranges_no_leases`
    returns 42501 "Access to crdb_internal and system is restricted"). It costs ~1.4s
    against the Cloud cluster, which is why it is behind ?deep=1 and never on the path
    an uptime monitor polls.

    The request-unit side of the free tier is NOT knowable from SQL — there is no
    supported view for it on a Basic cluster — and this says so rather than inventing a
    number.
    """
    def _read(cur):
        cur.execute('SELECT sum(range_size) AS bytes, count(*) AS ranges '
                    'FROM [SHOW RANGES FROM DATABASE axiom WITH DETAILS]')
        return cur.fetchone()
    try:
        row = demo_state.tx(_read, readonly=True)
        return {'bytes': int(row['bytes'] or 0), 'ranges': int(row['ranges'] or 0),
                'request_units': 'not queryable from SQL on a Basic cluster; see the '
                                 'CockroachDB Cloud console'}
    except Exception as e:                       # noqa: BLE001
        return {'error': f'{type(e).__name__}: {e}'[:200]}


@app.get('/api/health')
@json_route
def health(deep: bool = Query(False, description='add storage size (~1.4s on Cloud)'),
           heal: bool = Query(True, description='self-heal the demo if it is empty'),
          ) -> Response:
    """What is true right now, in the order that matters, with nothing asserted.

    This endpoint is deliberately the most paranoid code in the repo: it is what an
    uptime monitor polls for four weeks and what a judge hits when something looks off,
    so every clause is wrapped and every failure is a value rather than an exception.

    Status code is part of the answer — 200 when the demo is servable, 503 when it is
    not — because a monitor should not have to parse prose to page someone, and because
    the alarm colour in Mission Control is reserved for genuine failure.

    "Servable" means the database answers AND there is a coherent demo to show. A
    perfectly healthy process in front of a wiped database is not healthy, it is a green
    light in front of an empty screen, which is the specific outcome this whole change
    exists to prevent.
    """
    checks: dict[str, t.Any] = {}
    errors: dict[str, str] = {}

    def _probe_db() -> dict:
        t0 = time.perf_counter()
        try:
            demo_state.tx(lambda cur: cur.execute('SELECT 1'), readonly=True)
            return {'ok': True,
                    'latency_ms': round((time.perf_counter() - t0) * 1000, 1)}
        except Exception as e:                   # noqa: BLE001
            errors['db'] = f'{type(e).__name__}: {e}'[:300]
            return {'ok': False,
                    'latency_ms': round((time.perf_counter() - t0) * 1000, 1)}

    def _probe_provider() -> dict:
        t0 = time.perf_counter()
        try:
            s = demo_state.call(provider.stats)
            return {'ok': True,
                    'latency_ms': round((time.perf_counter() - t0) * 1000, 1),
                    'refunds_global': s['refunds'], 'replays_global': s['replays'],
                    'duplicate_orders_global': s['duplicate_orders']}
        except Exception as e:                   # noqa: BLE001
            errors['provider'] = f'{type(e).__name__}: {e}'[:300]
            return {'ok': False,
                    'latency_ms': round((time.perf_counter() - t0) * 1000, 1)}

    # Concurrently, because they are independent databases and a monitor should not wait
    # for one timeout plus the other. Measured with both down: 12.0s serially, 6.0s here
    # — and 6s is the pool wait this module sets, so the endpoint's worst case is now
    # exactly one connection timeout no matter how many dependencies are checked.
    with cf.ThreadPoolExecutor(max_workers=2) as pool:
        f_db, f_prov = pool.submit(_probe_db), pool.submit(_probe_provider)
        checks['db'], checks['provider'] = f_db.result(), f_prov.result()

    demo_ok = False
    if checks['db']['ok']:
        try:
            if heal:
                demo_state.ensure_demo()
            p = demo_state.probe()
            demo_ok = demo_state.is_coherent(p)
            if heal and not demo_ok:
                # The same escalation /api/mission makes, and for the same reason: this
                # probe is PROOF that the cached "healthy" answer is stale, so a plain
                # ensure_demo() above may have short-circuited on it and done nothing.
                #
                # Measured, and the reason this branch exists: four threads polling
                # /api/health across four POST /api/demo/reset calls produced 13 replies
                # of 503 `status: degraded` with `db: true, provider: true, errors: {}`
                # — a completely healthy system paging an uptime monitor because it
                # sampled the half-second between seed.reset() and the re-seed
                # committing. recheck=True re-probes, and the process lock inside
                # ensure_demo makes this thread WAIT for the resetting thread rather
                # than race it, so the re-read below sees the finished world.
                # scripts/soak_test.py asserts no 5xx in any wave and failed on exactly
                # these two responses before this branch existed.
                demo_state.ensure_demo(recheck=True)
                p = demo_state.probe()
                demo_ok = demo_state.is_coherent(p)
            checks['demo'] = {
                'ok': demo_ok, 'mission_id': p.get('mission_id'),
                'title': p.get('title'), 'state': p.get('state'),
                'tasks': p.get('tasks'), 'by_state': p.get('by_state', {}),
                'memories': p.get('memories'), 'active_policies': p.get('policies'),
                'missions': p.get('missions'),
                'budget_cents': p.get('budget_cents'), 'spent_cents': p.get('spent_cents'),
                'self_heals_this_process': demo_state.heals(),
            }
        except Exception as e:                   # noqa: BLE001
            errors['demo'] = f'{type(e).__name__}: {e}'[:300]
            checks['demo'] = {'ok': False}

        checks['vector_index'] = _vector_index_check()

        try:
            checks['workers'] = {'live': demo_state.live_workers(),
                                 'max_concurrent': demo_state.MAX_LIVE_WORKERS}
        except Exception as e:                   # noqa: BLE001
            checks['workers'] = {'error': f'{type(e).__name__}: {e}'[:200]}

        if deep:
            checks['storage'] = _storage_used()

    ok = checks['db']['ok'] and checks.get('provider', {}).get('ok', False) and demo_ok
    body = {
        # --- the shape Mission Control has always read -------------------------
        'ok': ok,
        'db': checks['db']['ok'],
        'provider': checks.get('provider', {}).get('ok', False),
        'version': __version__,
        'offline': settings.offline,
        'errors': errors,
        # --- and the detail a monitor or a judge wants --------------------------
        'status': 'ok' if ok else ('down' if not checks['db']['ok'] else 'degraded'),
        'checks': checks,
        'booted_at': _BOOTED_AT,
        'uptime_seconds': round((dt.datetime.now(dt.timezone.utc) - _BOOTED_AT)
                                .total_seconds(), 1),
        'checked_at': dt.datetime.now(dt.timezone.utc),
    }
    return AxiomJSON(body, status_code=200 if ok else 503)


# ========================================================================== MISSION

# Mission selection lives in demo_state.select_mission_id now, and it is no longer
# "newest". The old rule handed the screen to whichever mission was created last, which
# meant every run of scripts/counterexample.py — a one-task mission on the same tenant —
# replaced the 30-task demo with a single tile in a thirty-tile frame. Verified on the
# production cluster: the deployed demo was showing "Counterexample / one refund, one
# crash" with a $1,000 budget and a 1-tile grid.

# What the API returns when the tenant genuinely has nothing. It is a 200 with a shape
# the dashboard can render, not a 404: `renderMission` keys off `m.id`, so a body with a
# null id paints its own empty state ("no mission — seed the demo to create one") while
# a 404 increments the failure counter and latches the POLL lamp into the alarm colour.
# Empty is not broken, and the API must not tell the UI that it is.
def _empty_mission(tenant_id: uuid.UUID, note: str) -> dict:
    return {'id': None, 'title': None, 'goal': None, 'state': 'EMPTY',
            'budget_cents': 0, 'spent_cents': 0, 'created_at': None,
            'by_state': {}, 'empty': True, 'tenant_id': tenant_id, 'note': note}


def _heal_if_demo(tenant_id: uuid.UUID) -> str:
    """Self-heal, but only ever for the demo tenant. Returns a note for the payload.

    A request carrying `X-Axiom-Tenant` for somebody else's tenant must never cause this
    server to write rows into it. Multi-tenancy is a claim this project makes; silently
    seeding a stranger's tenant because their queue looked empty would make it a lie.
    """
    if tenant_id != DEMO_TENANT:
        return 'no mission for this tenant'
    # recheck=True, not the default: the caller reached this function BECAUSE a read
    # just came back empty, which is proof that any cached "the demo is healthy" answer
    # is stale. The failure backoff still applies, so a dead database is not hammered.
    out = demo_state.ensure_demo(recheck=True)
    if out.get('waiting'):
        return 'seeding in progress'
    if out.get('disabled'):
        return 'no mission; auto-seed is off (POST /api/demo/seed)'
    return 'no mission; POST /api/demo/seed to create one'


@app.get('/api/mission')
@json_route
def mission(tenant_id: uuid.UUID = Depends(_tenant)) -> dict:
    """The mission the dashboard shows — healing the demo first if there is none.

    The heal is attempted BEFORE the read and the read is what decides the answer, so a
    heal that raced another process still returns that process's mission rather than an
    error about the race.
    """
    def _read(cur):
        mid = demo_state.select_mission_id(cur, tenant_id)
        if mid is None:
            return {}
        # Retire the mission if every task is terminal. Deliberately on the read path and
        # therefore NOT readonly: no worker is in a position to know it settled the last
        # task, so without this the header reads STATE RUNNING forever — claiming work is
        # in flight to a viewer looking at an idle queue. Conditional on state='RUNNING'
        # inside the UPDATE, so concurrent readers cannot double-transition it.
        tasks.settle_mission_if_complete(cur, tenant_id=tenant_id, mission_id=mid)
        return tasks.mission_summary(cur, tenant_id=tenant_id, mission_id=mid)

    out = demo_state.tx(_read)
    if out:
        # The one place the API acts on its own. Rate-limited to a probe every 15s and a
        # worker every 90s, and refuses outright unless the board has been still for two
        # minutes with claimable work and no live worker. See _maybe_autoheal.
        _maybe_autoheal()
        return out

    note = _heal_if_demo(tenant_id)
    out = demo_state.tx(_read, readonly=True)
    return out or _empty_mission(tenant_id, note)


# ============================================================================ TASKS

@app.get('/api/tasks')
@json_route
def list_tasks(limit: int = Query(200, ge=1, le=1000),
               tenant_id: uuid.UUID = Depends(_tenant)) -> list[dict]:
    """The shown mission's tasks. Heals once if the tenant has no mission at all.

    An empty list is a legitimate answer here (a mission with no tasks is a state the
    system can be in), so this route never invents a payload — it heals, re-reads, and
    reports whatever is actually there.
    """
    def _read(cur):
        mid = demo_state.select_mission_id(cur, tenant_id)
        return tasks.list_tasks(cur, tenant_id=tenant_id, mission_id=mid, limit=limit)

    out = demo_state.tx(_read, readonly=True)
    if not out:
        _heal_if_demo(tenant_id)
        out = demo_state.tx(_read, readonly=True)
    return out


@app.get('/api/tasks/{task_id}')
@json_route
def task_detail(task_id: uuid.UUID,
                tenant_id: uuid.UUID = Depends(_tenant)) -> dict:
    """One task, its complete journal, and every receipt it ever minted.

    The three together are the answer to "what did this agent do, and how do you know?".
    The events are read back by `seq`, which is gap-free per subject by construction —
    so a missing number would be visible rather than merely absent.
    """
    def _read(cur):
        row = tasks.get_task(cur, tenant_id=tenant_id, task_id=task_id)
        if not row:
            return None
        journal = events.replay(cur, tenant_id=tenant_id, subject_type='task',
                                subject_id=task_id)
        cur.execute("""
            SELECT id, step_name, step_seq, attempt_state, provider, operation,
                   amount_cents, currency, idempotency_key, request_fingerprint,
                   provider_ref, http_status, lease_epoch, prepared_by,
                   licensed_by_memory_id, policy_id, policy_version,
                   prepared_at, dispatched_at, settled_at, response_body
            FROM axiom_action_attempt
            WHERE tenant_id = %s AND task_id = %s
            ORDER BY step_name, step_seq
        """, (str(tenant_id), str(task_id)))
        return {'task': row, 'events': journal, 'attempts': cur.fetchall()}

    out = demo_state.tx(_read, readonly=True)
    if out is None:
        raise HTTPException(404, f'task {task_id} not found')
    return out


# =========================================================================== EVENTS

@app.get('/api/events')
@json_route
def event_timeline(limit: int = Query(200, ge=1, le=2000),
                   tenant_id: uuid.UUID = Depends(_tenant)) -> list[dict]:
    return demo_state.tx(lambda cur: events.timeline(cur, tenant_id=tenant_id,
                                                     limit=limit), readonly=True)


# ======================================================================== APPROVALS

class DecideBody(BaseModel):
    approved: bool
    decided_by: str = Field(default='human:operator@acme.example', max_length=200)
    note: str = Field(default='', max_length=1000)


@app.get('/api/approvals')
@json_route
def approvals(tenant_id: uuid.UUID = Depends(_tenant)) -> list[dict]:
    return demo_state.tx(lambda cur: tasks.pending_approvals(cur, tenant_id=tenant_id),
                         readonly=True)


@app.post('/api/approvals/{approval_id}/decide')
@json_route
def decide(approval_id: uuid.UUID, body: DecideBody,
           tenant_id: uuid.UUID = Depends(_tenant)) -> dict:
    """A human rules on a parked action.

    The write is one transaction: the approval row, the task's availability, and the
    journal entry all commit together, so there is no instant in which a task is
    claimable on the strength of a decision that has not been recorded.
    """
    def _write(cur):
        tasks.decide_approval(cur, tenant_id=tenant_id, approval_id=approval_id,
                              approved=body.approved, decided_by=body.decided_by,
                              note=body.note)
    try:
        demo_state.warm()          # a live pool, so the write below is not retried
        db.tx(_write)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {'ok': True}


# ========================================================================= MEMORIES

class QuarantineBody(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)
    by: str = Field(default='human:operator@acme.example', max_length=200)


class RecallBody(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    memory_class: MemoryClass = MemoryClass.EPISODIC
    context_key: str | None = None
    k: int = Field(default=5, ge=1, le=50)


@app.get('/api/memories')
@json_route
def memories(limit: int = Query(100, ge=1, le=1000),
             include_inadmissible: bool = True,
             tenant_id: uuid.UUID = Depends(_tenant)) -> list[dict]:
    """The memory browser. Inadmissible rows are shown by default, on purpose.

    Hiding quarantined and superseded memories would make the admissibility gate
    invisible, and the gate is the interesting part: those rows sit in a different
    partition of the vector index and cannot enter an ANN candidate set at all.
    """
    return demo_state.tx(lambda cur: memory.browse(
        cur, tenant_id=tenant_id, limit=limit,
        include_inadmissible=include_inadmissible), readonly=True)


@app.post('/api/memories/{memory_id}/quarantine')
@json_route
def quarantine(memory_id: uuid.UUID, body: QuarantineBody,
               tenant_id: uuid.UUID = Depends(_tenant)) -> dict:
    """Quarantine takes effect AT COMMIT, atomically, with no reindex.

    `quarantined` feeds the computed `retrieval_class`, which is a vector-index PREFIX
    column — so this single UPDATE physically moves the row out of the ACTIONABLE
    partition. Re-run POST /api/memories/recall immediately after and the memory is
    gone from the candidate set. That is the demo.
    """
    def _write(cur):
        memory.quarantine(cur, tenant_id=tenant_id, memory_id=memory_id,
                          reason=body.reason, by=body.by)
    try:
        demo_state.warm()          # a live pool, so the write below is not retried
        db.tx(_write)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {'ok': True}


@app.get('/api/memories/{memory_id}/effects')
@json_route
def memory_effects(memory_id: uuid.UUID,
                   tenant_id: uuid.UUID = Depends(_tenant)) -> list[dict]:
    """Every irreversible act this memory licensed. The blast-radius query.

    Backed by the partial index axiom_attempt_by_license, which exists for exactly the
    moment you discover a memory was poisoned and need to know what it already bought.
    """
    return demo_state.tx(lambda cur: memory.effects_licensed_by(
        cur, tenant_id=tenant_id, memory_id=memory_id), readonly=True)


@app.post('/api/memories/recall')
@json_route
def recall(body: RecallBody = Body(...),
           tenant_id: uuid.UUID = Depends(_tenant)) -> dict:
    """ANN recall, plus the query plan that proves it was an ANN recall.

    Embedding happens BEFORE the transaction opens: db.tx() re-executes its callable on
    40001, and embedding inside it would re-hit Bedrock on every retry.

    `plan_uses_vector_index` is not decoration. The identical rows come back when the
    plan degrades to a full primary-key scan, so nothing a human could see in the result
    set would catch the regression — only the plan does. Surfacing it in the UI turns an
    architectural assertion into an observation.

    Two degradations, and neither of them lies:

    * The embedder is a network dependency (Bedrock, unless AXIOM_OFFLINE). If it is
      down there is no query vector, so there is no recall to report and this returns
      503 naming the embedder rather than a 500 naming nothing.
    * The EXPLAIN is a second statement that can fail on its own. If it does, the hits
      are still real and are still returned — but `plan_uses_vector_index` becomes NULL
      rather than false, because "we could not check" and "it degraded to a scan" are
      different claims and only one of them is an alarm.
    """
    try:
        vec = embeddings.embed_list(body.query)
    except Exception as e:                       # noqa: BLE001 — Bedrock is a dependency
        raise Unavailable('embeddings', f'{type(e).__name__}: {e}') from e
    literal = db.vector_literal(vec)
    k = body.k
    fetch = k * settings.recall_overfetch

    def _read(cur):
        hits = memory.recall(cur, tenant_id=tenant_id, embedding=vec,
                             memory_class=body.memory_class,
                             context_key=body.context_key,
                             retrieval_class=RetrievalClass.ACTIONABLE, k=k)
        params: dict[str, t.Any] = {
            'vec': literal, 'tenant': str(tenant_id), 'cls': str(body.memory_class),
            'rc': str(RetrievalClass.ACTIONABLE), 'fetch': fetch,
        }
        if body.context_key is not None:
            params['ck'] = body.context_key
        try:
            plan = db.explain(
                cur, memory.recall_sql_for_explain(context_key=body.context_key is not None),
                params)
        except psycopg.Error as e:
            # The EXPLAIN aborts the surrounding transaction on some errors, so the hits
            # were already materialised above and nothing after this point touches the
            # cursor.
            plan = f'EXPLAIN unavailable: {type(e).__name__}: {e}'
        return hits, plan

    hits, plan = demo_state.tx(_read, readonly=True)
    plan_ok = not plan.startswith('EXPLAIN unavailable')
    return {
        'hits': [{
            'id': h.id, 'content': h.content, 'outcome': h.outcome,
            'distance': h.distance, 'similarity': h.similarity,
            'trust_level': h.trust_level, 'source': h.source,
            'confidence': h.confidence, 'context_key': h.context_key,
            'task_id': h.task_id, 'attempt_id': h.attempt_id,
        } for h in hits],
        'plan_uses_vector_index': db.uses_vector_index(plan) if plan_ok else None,
        'plan_checked': plan_ok,
        'plan': plan,
    }


# =========================================================================== AGENTS

@app.get('/api/agents')
@json_route
def agents(limit: int = Query(50, ge=1, le=200)) -> list[dict]:
    """The worker pool, newest heartbeat first, CAPPED.

    Rows live under the SYSTEM tenant, not the demo tenant.
    `seconds_since_heartbeat` is computed server-side against the cluster's clock rather
    than the browser's: a worker the demo just SIGKILLed must read as stale even if the
    viewer's laptop clock is minutes off.

    The cap is the fix for a real defect, not a precaution. This route had no LIMIT, the
    rail renders every row it is given, and one row is registered per worker start —
    which is once per RUN MISSION and once per KILL A WORKER. The production cluster was
    already returning twelve, ten of them struck-through DEAD, and the panel below them
    was being squeezed into an overlap. Forty judges clicking twice each is eighty rows
    of a list whose useful length is about three.

    Capping in the API rather than the renderer is deliberate: a bounded endpoint cannot
    be un-bounded by a change to the UI, and the UI is not the only consumer. The default
    is 50 rather than the six the rail actually paints, because web/app.js prints
    "+N earlier" from the length of what it was given — a tighter cap here would silently
    make that count wrong, and a number that is quietly wrong is worse than a long list.
    Rows older than an hour are deleted outright by demo_state.reap_agents(), so 50 is a
    ceiling the table does not normally approach.
    """
    def _read(cur):
        cur.execute("""
            SELECT id, worker_ref, kind, status, shards, heartbeat_at, build_sha,
                   region, started_at, stopped_at,
                   extract(epoch FROM (now() - heartbeat_at)) AS seconds_since_heartbeat
            FROM axiom_agent WHERE tenant_id = %s
            ORDER BY heartbeat_at DESC
            LIMIT %s
        """, (str(SYSTEM_TENANT), limit))
        return cur.fetchall()
    return demo_state.tx(_read, readonly=True)


# ========================================================================= RECEIPTS

@app.get('/api/receipts/unsettled')
@json_route
def unsettled(tenant_id: uuid.UUID = Depends(_tenant)) -> list[dict]:
    """The reconciliation worklist: every external call that might be in flight.

    Also the operational answer to "what is this system currently unsure about?", which
    is the question you actually want during an incident. An empty list means every
    authorized effect has a recorded outcome.
    """
    return demo_state.tx(lambda cur: tasks.unsettled_receipts(cur, tenant_id=tenant_id),
                         readonly=True)


# ========================================================================= PROVIDER

def _mission_order_refs() -> set[str]:
    """The order refs belonging to the SHOWN mission. Possibly empty; never None.

    The provider has no notion of our tenants — correctly, since it is a different
    company's system in the story and a different database in fact. So scoping has to
    happen HERE, by asking AXIOM which orders this mission touched and filtering the
    external ledger to those.

    This used to return None when there was no mission, and both callers read None as
    "fall back to the global ledger". That fallback is how the headline number gets
    poisoned: scripts/counterexample.py double-refunds an order ON PURPOSE, and a
    dashboard with no mission would have shown that deliberate duplicate as DUPLICATE
    REFUNDS 1 under a mission grid that had nothing to do with it. An empty scope now
    means an empty scope — zero refunds, zero duplicates, nothing claimed.
    """
    return demo_state.tx(
        lambda cur: demo_state.mission_order_refs(cur, seed.DEMO_TENANT), readonly=True)


@app.get('/api/provider/ledger')
@json_route
def provider_ledger(limit: int = Query(200, ge=1, le=1000),
                    scope: str = Query('mission', pattern='^(mission|global)$')) -> list[dict]:
    """The EXTERNAL world's record. Different database, different connection, no shared
    transaction — which is exactly the relationship a real payments API has with your
    application, minus the network. This is the ledger the demo audits against.

    `scope=mission` (the default) filters to the orders the current mission touched.
    The provider database is genuinely global — it accumulates every refund any run ever
    issued — so an unscoped ledger next to a single mission's task grid can visibly
    disagree with it on camera, which is the last thing you want in the one panel whose
    job is to be trusted. `scope=global` returns the raw external record.
    """
    rows = demo_state.call(lambda: provider.ledger(limit=limit))
    if scope == 'global':
        return rows
    refs = _mission_order_refs()
    return [r for r in rows if r['order_ref'] in refs]


@app.get('/api/provider/stats')
@json_route
def provider_stats(scope: str = Query('mission', pattern='^(mission|global)$')) -> dict:
    """`duplicate_orders` is the headline number and it must be zero.

    `replays` above zero is the other half of the claim: it proves the crashes landed
    inside the dangerous window and that recovery genuinely re-sent under the same
    derived key, rather than the run simply having been lucky.

    Scoped to the current mission by default, for the reason given on the ledger route.
    Note that `duplicate_orders` is recomputed over the scoped rows rather than filtered
    from the global figure: a duplicate outside this mission is still a real duplicate,
    but it is not THIS mission's claim, and conflating the two is how a headline number
    stops meaning anything.

    The empty-scope branch is not a formality. `provider.stats()` treats an empty
    `order_refs` as "no filter" and returns the GLOBAL ledger, which includes the
    deliberate double refund that scripts/counterexample.py creates to prove the thesis.
    A dashboard between missions would then have printed DUPLICATE REFUNDS 1 in
    48-point type, over a grid that had nothing to do with it, and the one number this
    entire project is judged on would have been somebody else's evidence.
    """
    if scope == 'global':
        return demo_state.call(provider.stats)

    refs = _mission_order_refs()
    if not refs:
        return {'refunds': 0, 'total_cents': 0, 'replays': 0, 'verdicts': {},
                'duplicate_orders': 0, 'scope': 'mission', 'orders_in_scope': 0}

    # Scoped in SQL rather than in Python: provider.stats(order_refs) applies the same
    # filter to the refund table AND the request log, so `verdicts` is scoped too.
    out = demo_state.call(lambda: provider.stats(sorted(refs)))
    out['scope'] = 'mission'
    out['orders_in_scope'] = len(refs)
    return out


# ==================================================================== CRASH WINDOWS

# The correctness spec, served as data so the UI, the README and the video are all
# reading the same table rather than three drifting copies of it.
#
# `covered_by` names what enforces or demonstrates each row TODAY. `status` is
# deliberately not a green tick for every row: ENFORCED_BY_SCHEMA means the database
# makes the violation unrepresentable, DEMONSTRATED means a script has actually produced
# the crash and the recovery, and PLANNED means the argument is sound but no artifact in
# this repo currently exercises it. A table that claimed seven greens it had not earned
# would fail the project's own thesis.
CRASH_WINDOWS: list[dict] = [
    {
        'id': 'W1',
        'when': 'Crash after CLAIM, before PREPARE',
        'effect_possible': False,
        'recovery': 'Re-claim with a new lease_epoch; re-plan freely.',
        'guarantee': 'No external effect can exist — nothing was authorized. The receipt '
                     'commits BEFORE any HTTP call, so a crash here is invisible to the world.',
        'covered_by_test': 'scripts/chaos_demo.py (SIGKILL sweep)',
        'status': 'DEMONSTRATED',
    },
    {
        'id': 'W2',
        'when': 'Crash after the receipt COMMIT, before the provider call is sent',
        'effect_possible': True,
        'recovery': 'Re-dispatch under the SAME derived idempotency key.',
        'guarantee': 'Effect state is unknowable from our side, so we do not guess. The key '
                     'is a GENERATED STORED column over immutable inputs, so the recovering '
                     'worker cannot mint a different one. Provider dedupes; effectively-once.',
        'covered_by_test': 'scripts/chaos_demo.py --kill-every (AXIOM_CHAOS_PRE)',
        'status': 'DEMONSTRATED',
    },
    {
        'id': 'W3',
        'when': 'Crash mid-flight, provider outcome unknown',
        'effect_possible': True,
        'recovery': 'Re-dispatch under the same key.',
        'guarantee': 'Identical to W2. DISPATCHED is an observability marker only and is '
                     'safety-equivalent to PREPARED — never branch on the difference.',
        'covered_by_test': 'scripts/chaos_demo.py',
        'status': 'DEMONSTRATED',
    },
    {
        'id': 'W4',
        'when': 'Crash after the provider responded, before SETTLE',
        'effect_possible': True,
        'recovery': 'Re-dispatch under the same key; the provider returns the ORIGINAL '
                    'refund; settle records it.',
        'guarantee': 'Exactly one real-world effect. The replay counter on the provider row '
                     'is the evidence that this window was actually entered.',
        'covered_by_test': 'scripts/chaos_demo.py (AXIOM_CHAOS_POST) -> provider replays > 0',
        'status': 'DEMONSTRATED',
    },
    {
        'id': 'W5',
        'when': 'A zombie worker settles after its lease expired',
        'effect_possible': True,
        'recovery': 'Its settle is rejected on a stale lease_epoch and it exits.',
        'guarantee': 'The fence, not the lease, is the invariant. A lease expiring does not '
                     'stop a GC-paused worker already inside a refund call; the monotonic '
                     'per-row lease_epoch does.',
        'covered_by_test': 'axiom/tasks.py::_assert_fence (raises LeaseLost)',
        'status': 'ENFORCED_IN_CODE',
    },
    {
        'id': 'W6',
        'when': 'Two workers PREPARE the same step concurrently',
        'effect_possible': False,
        'recovery': 'The loser gets 23505 and never calls the provider.',
        'guarantee': 'UNIQUE INDEX axiom_attempt_one_live makes two live receipts for one '
                     '(tenant, task, step) unrepresentable. Database-enforced, not '
                     'convention-enforced.',
        'covered_by_test': 'db/001_schema.sql::axiom_attempt_one_live',
        'status': 'ENFORCED_BY_SCHEMA',
    },
    {
        'id': 'W7',
        'when': 'A recovered LLM re-synthesizes a DIFFERENT request body under the old key',
        'effect_possible': True,
        'recovery': 'request_fingerprint mismatch -> hard stop, escalate to a human.',
        'guarantee': 'Same key plus different intent is not a retry. Defence against the '
                     'semantic-rollback attack class (ACRFence, arXiv:2603.20625); the '
                     'provider independently answers 409 on the same condition.',
        'covered_by_test': 'axiom/tasks.py::verify_fingerprint + axiom/provider.py 409 path',
        'status': 'ENFORCED_IN_CODE',
    },
]


@app.get('/api/crash-windows')
@json_route
def crash_windows() -> list[dict]:
    return CRASH_WINDOWS


# =========================================================================== REWIND

@app.get('/api/rewind')
@json_route
def rewind(seconds_ago: int = Query(30, ge=1, le=3600),
           tenant_id: uuid.UUID = Depends(_tenant)) -> dict:
    """What did this system believe N seconds ago?

    A historical read at AS OF SYSTEM TIME. Two things make it work rather than merely
    compile:

    * The AOST clause is set once for the WHOLE transaction (db.tx(as_of=...)), because
      CockroachDB requires it on a top-level statement — putting it on a nested SELECT
      fails outright, which preflight gate 7 established the hard way.
    * `seconds_ago` is coerced to an int before it reaches the interval string. It is the
      one value in this file that is interpolated rather than bound (SET TRANSACTION
      takes a literal), so int() is the thing standing between a query parameter and the
      transaction control statement.

    Honest limitation, stated in the response: AOST cannot reach further back than the
    range's gc.ttlseconds. Durable audit history is axiom_event and the valid_from /
    valid_until columns; this is a live-inspection convenience, not the audit trail.
    """
    def _read(cur):
        cur.execute('SELECT now() AS at')
        at = cur.fetchone()['at']
        cur.execute("""
            SELECT state, count(*) AS n FROM axiom_task WHERE tenant_id = %s GROUP BY state
        """, (str(tenant_id),))
        by_state = {r['state']: r['n'] for r in cur.fetchall()}
        cur.execute('SELECT count(*) AS n FROM axiom_memory WHERE tenant_id = %s',
                    (str(tenant_id),))
        return {'at': at, 'tasks_by_state': by_state,
                'memory_count': cur.fetchone()['n'],
                'seconds_ago': seconds_ago}

    try:
        return demo_state.tx(_read, as_of=f'-{int(seconds_ago)}s')
    # A dead connection is not a bad request. Without this clause the OperationalError
    # below would be caught by the generic `psycopg.Error` branch and reported as "AS OF
    # SYSTEM TIME is out of range", which is a plausible-sounding lie about a database
    # that is simply unreachable.
    except (psycopg.OperationalError, psycopg.InterfaceError) as e:
        raise Unavailable('db', f'{type(e).__name__}: {e}') from e
    # 42P01 UndefinedTable / 3D000 InvalidCatalogName. psycopg has no `UndefinedDatabase`
    # — the SQLSTATE for "database does not exist" is 3D000, which it names
    # InvalidCatalogName. Naming it wrong turns this handler into a 500 at request time,
    # which is how it was found.
    except (psycopg.errors.UndefinedTable, psycopg.errors.InvalidCatalogName,
            psycopg.errors.InvalidSchemaName) as e:
        # Rewinding past CREATE DATABASE is the mistake that produced a false negative in
        # the very first preflight log: the historical read is fine, the schema simply did
        # not exist yet. Name that, rather than reporting it as a GC bound.
        raise HTTPException(400, f'rewind reaches back before the schema existed: {e}')
    except psycopg.Error as e:
        # The common one is "batch timestamp must be after replica GC threshold" — a real
        # bound of the feature, so it is reported as a bad request rather than a fault.
        raise HTTPException(400, f'AS OF SYSTEM TIME -{seconds_ago}s is out of range: {e}')


# =========================================================================== PROOFS
#
# The five routes below are the only ones in this file that RUN the argument instead of
# projecting it. Everything else here reports what the engine has already done; these
# drive the protocol live, inside the request, so that the strongest evidence this
# project has stops living exclusively in scripts/ where a judge will never look.
#
# They still hold rule 1 of this module: none of them decides anything. The authority
# decision is `tasks.prepare()`, the recovery decision is `tasks.recover()`, and the
# quarantine takes effect at COMMIT inside the transaction that asked — axiom/proofs.py
# drives those functions and never reimplements them, or the proofs would be proving the
# proof harness.
#
# Four protections apply to every POST here, and the last one is the reason the others
# are not enough:
#
#   1. `_gate` — the same in-process minimum interval the demo controls use. Stripe's is
#      much longer than the rest because it creates a real (test-mode) charge and refund
#      on every press and Stripe rate-limits accounts, not endpoints.
#   2. `_PROOF_LOCK` — one proof at a time per instance. Two proofs interleaving would
#      not corrupt anything (each runs in its own tenant), but they would compete for a
#      three-connection pool and make each other look slow.
#   3. Each proof cleans up after itself in a `finally`, and reaps orphans left by a
#      previous instance that was frozen mid-run. Four weeks of judging must not leave
#      four weeks of proof tenants.
#   4. **No proof may 500.** A proof that fails has still learned something, and the
#      honest report of that is a 200 carrying verdict INCONCLUSIVE and the reason — not
#      a stack trace. The two exceptions are deliberate and are not 500s either: 429 when
#      the caller is early, and 503 when a dependency is genuinely down, which is the
#      same contract every other route in this file keeps.

# One proof at a time per process. Not a correctness mechanism — each run is isolated in
# its own tenant — but a fairness one: the pool is three connections wide in production.
_PROOF_LOCK = threading.Lock()


def _run_proof(name: str, min_interval_s: float, fn: t.Callable[..., dict], **kw) -> dict:
    """Gate it, serialize it, run it, and never let it 500."""
    _gate(name, min_interval_s)
    if not _PROOF_LOCK.acquire(timeout=2.0):
        raise HTTPException(
            429, 'another proof is already running on this instance; try again in a moment',
            headers={'Retry-After': '10'})
    try:
        demo_state.warm()          # a proven-live connection, so the writes are not retried
        proofs.reap_stale_tenants()
        return fn(**kw)
    except (Unavailable, HTTPException):
        # A dead database is not an inconclusive proof, it is a dependency being down, and
        # the existing handlers say so with a 503 and a component name.
        raise
    except Exception as e:                       # noqa: BLE001 — see protection 4 above
        log.exception('proof %s failed outside its own guard', name)
        return {'verdict': 'INCONCLUSIVE', 'steps': [],
                'error': f'{type(e).__name__}: {e}'[:300]}
    finally:
        _PROOF_LOCK.release()


@app.post('/api/proof/memory')
@json_route
def proof_memory() -> dict:
    """Run the recovery three times against one crashed task, changing only MEMORY.

    RESEND -> ESCALATE -> RESEND, with the quarantine that flips it back taking effect
    inside the same transaction that asks. The response carries every recalled memory
    with its similarity, so a viewer can check the arithmetic of the decision rather than
    trusting the rationale sentence — and the live EXPLAIN, because "we used the vector
    index" is exactly the kind of claim that is easy to make and easy to have quietly
    stopped being true.

    Safe to press repeatedly: every run builds and then deletes its own tenant, so forty
    presses leave forty nothings behind and the demo everyone else is looking at is never
    touched.
    """
    return _run_proof('proof-memory', 5, proofs.memory_decides, budget_seconds=25.0)


@app.post('/api/proof/stripe')
@json_route
def proof_stripe() -> dict:
    """The same crash — window W4 — against Stripe's real API, in test mode.

    Creates a real test charge, mints the receipt, sends the refund, crashes before
    recording it, recovers under the SAME key, and then asks STRIPE what happened. The
    answer that matters is not "one refund", it is "one refund and Stripe reported the
    second request as a replay".

    Returns `{available: false, reason}` with a 200 when no sandbox key is configured on
    this deployment. That is a fact about the deployment, not a failed proof, and the UI
    can show the recorded result instead.
    """
    if not proofs.stripe_available():
        # Answered BEFORE the gate: a deployment with no key would otherwise spend a
        # 45-second rate limit to say "not configured", forty times a day.
        return proofs.stripe_proof()
    return _run_proof('proof-stripe', 45, proofs.stripe_proof, budget_seconds=90.0)


@app.post('/api/proof/broadcast')
@json_route
def proof_broadcast() -> dict:
    """The same crash in a second workload, where the risk axis is RECIPIENTS.

    Three campaigns, one crash at W4, one recovery, and then the relay's own books are
    audited with the query that matters: one row per human being who received the same
    campaign twice. Three, not the twelve `scripts/demo_domain2.py` runs, because this one
    runs inside an HTTP request.
    """
    return _run_proof('proof-broadcast', 15, proofs.broadcast_proof, budget_seconds=60.0)


@app.get('/api/domains')
@json_route
def domains() -> list[dict]:
    """Every workload this engine protects, and the unit its authority is measured in.

    One column carries the argument: `risk_unit`. Dollars for a refund, PEOPLE for a
    broadcast, one engine, one policy model. That is the difference between a demo and a
    platform, and it is cheaper to read here than to take on faith from a README.
    """
    return proofs.domains()


@app.get('/api/proofs')
@json_route
def proofs_index() -> dict:
    """The receipts index: what was measured, with the command that measured it.

    Two of these numbers are computed live because they are cheap and would otherwise be
    exactly the kind of figure that quietly stops being true — the crash-window count is
    read from the table this file serves, and the vector index is read from a query plan
    (cached for a minute, with the age of the answer reported). Everything else is a
    RECORDED measurement: it was produced by running the command stored beside it, and it
    is labelled `recorded: true` so nothing on the page can pass a laboratory number off
    as a live one.
    """
    out = proofs.measurements()
    out['crash_windows'] = len(CRASH_WINDOWS)

    vec = _vector_index_check()
    out['live'] = {
        'vector_index_in_use': vec.get('in_use'),
        'vector_index_checked_seconds_ago': vec.get('age_seconds'),
        'stripe_proof_available': proofs.stripe_available(),
        'version': __version__,
    }
    # The one tool claim that can be verified from where this process is standing gets
    # verified from where this process is standing.
    for tool in out.get('cockroach_tools', []):
        if 'Vector' in tool.get('name', ''):
            tool['verified_live'] = vec.get('in_use')
    return out


# ============================================================================= DEMO
#
# Everything below this line MUTATES, and every one of these routes is reachable by
# anyone who can reach the demo URL, from any origin (CORS is deliberately open; there
# is no authenticated state to protect). Three protections apply to all of them:
#
#   1. An optional shared token. Set AXIOM_DEMO_TOKEN in the deployed environment and
#      these routes require `X-Axiom-Demo-Token`; leave it unset and they stay open,
#      which is what a laptop demo and the test suite want. It is a demo control panel,
#      not a login: the point is to keep a crawler from resetting the board under a
#      judge, not to protect a secret.
#   2. A minimum interval per route (demo_state.gate). Two judges on the same afternoon
#      are welcome. One judge's double-click, or one crawler's retry loop, is not.
#   3. Nothing here can create unbounded work: seed is idempotent and capped, reset
#      re-seeds rather than emptying, and run-worker refuses to start a fourth worker.

DEMO_TOKEN = os.environ.get('AXIOM_DEMO_TOKEN', '')


def _demo_auth(x_axiom_demo_token: str | None = Header(default=None)) -> None:
    if DEMO_TOKEN and x_axiom_demo_token != DEMO_TOKEN:
        raise HTTPException(403, 'demo controls are token-gated on this deployment; '
                                 'send X-Axiom-Demo-Token')


def _gate(name: str, seconds: float) -> None:
    wait = demo_state.gate(name, seconds)
    if wait:
        raise HTTPException(
            429, f'{name} is rate limited on the public demo — retry in {wait:.0f}s',
            headers={'Retry-After': str(int(wait) + 1)})


class SeedBody(BaseModel):
    tasks: int = Field(default=30, ge=1, le=500)
    reset: bool = True


@app.post('/api/demo/seed', dependencies=[Depends(_demo_auth)])
@json_route
def demo_seed(body: SeedBody = Body(default=SeedBody())) -> dict:
    """Rebuild the demo world: tenant, policy, mission, order exceptions, prior memories.

    Scoped to the demo tenant by construction — axiom.seed only ever touches DEMO_TENANT
    and the provider ledger, so this cannot be pointed at anything else by a header.
    """
    _gate('seed', 8)
    demo_state.warm()               # so the writes below meet a proven-live connection
    if body.reset:
        seed.reset()
    out = seed.seed(n_tasks=body.tasks)
    demo_state.invalidate()         # the cached "healthy" answer is now about old rows
    return {'mission_id': out['mission_id'], 'tasks': out['tasks'],
            'memories': out['memories'], 'tenant_id': out['tenant_id']}


class ResetBody(BaseModel):
    # Default TRUE, and this is the whole point of the flag existing. RESET used to wipe
    # the demo tenant and stop: /api/mission then 404'd, the grid and the journal went
    # empty, the POLL lamp latched into the alarm colour, and the only way back was a
    # button one over that a judge had no reason to press. A demo control that can
    # permanently break the demo is not a control, it is a trap — so reset now means
    # "back to a clean board", and emptiness is available to scripts that ask for it.
    reseed: bool = True


@app.post('/api/demo/reset', dependencies=[Depends(_demo_auth)])
@json_route
def demo_reset(body: ResetBody = Body(default=ResetBody())) -> dict:
    _gate('reset', 15)
    demo_state.warm()
    seed.reset()
    demo_state.invalidate()
    if not body.reseed:
        return {'ok': True, 'reseeded': False,
                'note': 'demo tenant and external ledger cleared; POST /api/demo/seed '
                        'or GET /api/mission to rebuild'}
    out = demo_state.ensure_demo(force=True)
    return {'ok': True, 'reseeded': True, 'mission_id': out.get('mission_id'),
            'tasks': out.get('created_tasks'), 'memories': out.get('created_memories')}


class RunWorkerBody(BaseModel):
    mode: t.Literal['drain', 'chaos'] = 'drain'
    seconds: int = Field(45, ge=5, le=300)


@app.post('/api/demo/run-worker', dependencies=[Depends(_demo_auth)])
@json_route
def demo_run_worker(body: RunWorkerBody = Body(default=RunWorkerBody())) -> dict:
    """Start a worker to drain the queue — optionally one that will die mid-refund.

    This is the demo's control surface, and it is the one place the API starts something
    that acts on the outside world, so the reasoning is worth stating.

    §3 of this module's docstring says there is no kill-worker endpoint, and that still
    holds: this does not kill anything. It STARTS a worker, and in `chaos` mode it starts
    one configured to die at the worst possible instant — after the provider has committed
    a refund and before AXIOM records it (crash window W4). Starting a process that will
    crash itself is a different power from reaching out and killing an arbitrary one; the
    blast radius is a task this system already knows how to recover.

    Two backends, chosen by environment rather than by request, so a caller cannot pick:

      AXIOM_WORKER_LAMBDA set -> asynchronous Lambda invoke (InvocationType='Event').
        The deployed demo. Async because the worker outlives the HTTP request, and
        because a synchronous invoke would bill the API's wall-clock time waiting for it.
      otherwise               -> a detached local subprocess, for laptop demos and the
        chaos script. Same worker module, same code path.

    The refusal at the top is the month-of-judging fix. Every start registers a row in
    axiom_agent and, locally, a python process; nothing used to stop the fortieth. A
    demo URL that quietly accumulates processes on a one-core free-tier instance is a
    demo URL that is fast in week one and dead in week three.
    """
    _gate('run-worker', 3)
    live = demo_state.live_workers()
    if live >= demo_state.MAX_LIVE_WORKERS:
        # Deliberately a 200, not a 429: the caller asked for the queue to be worked and
        # the queue IS being worked. Nothing failed, so nothing should turn red.
        return {'ok': True, 'started': False, 'live_workers': live,
                'note': f'{live} workers are already draining this queue; '
                        f'not starting another'}
    out = _start_worker(mode=body.mode, seconds=body.seconds)
    demo_state.reap_agents()      # one bounded DELETE per start, so rows cannot pile up
    return out


def _start_worker(*, mode: str, seconds: int) -> dict:
    """Start one worker. Shared by the route above and the self-heal path below."""
    fn = os.environ.get('AXIOM_WORKER_LAMBDA')
    chaos = mode == 'chaos'

    # INLINE: a serverless function cannot spawn a process that outlives its request, so
    # on Vercel (and anywhere else that sets this) the worker runs inside the request and
    # returns when its deadline expires. It is the same Worker.run() loop the ECS and
    # Lambda deployments use, handed a deadline instead of a lifetime — the engine does
    # not know the difference, which is the point of keeping the loop in one place.
    #
    # Bounded hard at 55s: Vercel allows far longer, but the caller is a browser waiting
    # on fetch(), and the guided demo polls /api/mission independently while this runs.
    # A request that outlives the viewer's patience is worse than one that returns early
    # and lets the next call continue the drain.
    if os.environ.get('AXIOM_WORKER_INLINE') == '1':
        from .worker import Worker
        budget = max(5, min(int(seconds), 55))
        # chaos_post=1.0 is passed to the Worker, not exported to the environment:
        # settings is frozen at import, so an env var set here would be read by nobody.
        w = Worker(worker_ref=f'inline-{"chaos" if chaos else "drain"}-{uuid.uuid4().hex[:6]}',
                   chaos_post=1.0 if chaos else None)
        try:
            w.start()
            done = w.run(deadline_seconds=budget, idle_exit=True)
            return {'ok': True, 'backend': 'inline', 'mode': mode,
                    'tasks': done, 'budget_seconds': budget}
        except BaseException as e:            # ProviderCrash is a BaseException by design
            # A chaos worker that dies mid-refund is the DEMO SUCCEEDING, not a 500. The
            # crash is the event the viewer came to see; report it as an outcome.
            return {'ok': True, 'backend': 'inline', 'mode': mode, 'crashed': True,
                    'note': f'{type(e).__name__}: {e}'[:200]}
        finally:
            try:
                w.stop()
            except Exception:
                pass

    if fn:
        payload = json.dumps({'mode': mode, 'seconds': seconds,
                              'chaos_post': 1.0 if chaos else 0.0})
        try:
            import boto3
            resp = boto3.client('lambda').invoke(
                FunctionName=fn, InvocationType='Event', Payload=payload.encode())
        except Exception as e:                       # noqa: BLE001
            raise HTTPException(502, f'could not invoke worker lambda {fn}: {e}') from e
        return {'ok': True, 'started': True, 'backend': 'lambda', 'function': fn,
                'mode': mode, 'status': resp.get('StatusCode')}

    env = dict(os.environ)
    if chaos:
        # 1.0 = certain death, and specifically AFTER the refund has landed. A
        # probabilistic kill makes a demo that sometimes proves nothing.
        env['AXIOM_CHAOS_POST'] = '1.0'
    proc = subprocess.Popen(
        [sys.executable, '-m', 'axiom.worker', '--idle-exit',
         '--ref', f'demo-{"chaos" if chaos else "drain"}-{uuid.uuid4().hex[:6]}'],
        cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)      # survives this request; not our child to reap
    return {'ok': True, 'started': True, 'backend': 'subprocess', 'pid': proc.pid,
            'mode': mode}


def _maybe_autoheal() -> None:
    """Finish an abandoned run, if the board has been abandoned. Best effort, never raises.

    The scenario this exists for: a judge starts a chaos worker, the worker dies inside
    crash window W4 exactly as designed, and the judge closes the tab. Three tasks are
    left holding an expired lease. AXIOM recovers those the moment ANY worker runs — that
    is the entire product — but if nobody ever runs one, the next judge, eleven days
    later, opens the URL and sees a board frozen mid-recovery. The system would be
    correct and would look broken.

    Every condition in `should_autoheal` is a reason NOT to act (a live worker, a board
    that changed in the last two minutes, nothing claimable, a heal ninety seconds ago),
    because the worst outcome here is a worker draining the queue during a take the
    operator is recording. Set AXIOM_DEMO_AUTOHEAL=0 to switch it off entirely.
    """
    try:
        go, why = demo_state.should_autoheal()
        if not go:
            return
        log.warning('auto-heal: starting a drain worker (%s)', why)
        _start_worker(mode='drain', seconds=60)
    except Exception as e:                           # noqa: BLE001 — never break a poll
        log.warning('auto-heal skipped: %s: %s', type(e).__name__, e)


# ======================================================================= STATIC UI

_PLACEHOLDER = """<!doctype html><meta charset=utf-8>
<title>AXIOM</title>
<style>
  :root { color-scheme: dark }
  body { margin:0; padding:14vh 8vw; background:#0b0c0d; color:#d6d3cd;
         font:14px/1.65 ui-monospace,'SF Mono',Menlo,Consolas,monospace; }
  h1 { font-size:13px; letter-spacing:.28em; text-transform:uppercase; color:#8f8a82;
       font-weight:500; margin:0 0 2.5rem; }
  p { max-width:62ch; color:#8f8a82; }
  b { color:#e8e5df; font-weight:500 }
  a { color:#c9a227; text-decoration:none; border-bottom:1px solid #3a3733 }
  ul { padding-left:1.1rem; max-width:62ch }
  li { margin:.3rem 0 }
</style>
<h1>Axiom</h1>
<p><b>The API is running. The static UI has not been built into <code>web/</code> yet.</b></p>
<p>Memory is not saved chat history. Memory is what makes autonomous action safe.</p>
<ul>
  <li><a href="/api/health">/api/health</a></li>
  <li><a href="/api/mission">/api/mission</a></li>
  <li><a href="/api/tasks">/api/tasks</a></li>
  <li><a href="/api/crash-windows">/api/crash-windows</a></li>
  <li><a href="/api/provider/stats">/api/provider/stats</a></li>
  <li><a href="/api/docs">/api/docs</a></li>
</ul>
"""


class _UI(StaticFiles):
    """Serves web/ if it exists, and explains itself if it does not.

    `check_dir=False` alone is NOT enough, which cost a debugging round: in Starlette
    1.6 that flag only suppresses the check in __init__. `check_config()` still runs on
    the first request and raises RuntimeError — so a missing web/ turned every request to
    '/' into a 500 rather than a 404, while the app itself started perfectly happily.
    Verified by pointing an instance at a directory that does not exist.

    Neutering check_config has a second, useful consequence: the directory is stat'd per
    request instead of once, so the UI appears the moment somebody creates web/, with no
    API restart. That matters while the API and the UI are built in parallel.
    """

    async def check_config(self) -> None:
        # Deliberately a no-op. Absence of the UI is a state this server handles, not an
        # error it should die on — the API is useful without it.
        return

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as e:
            # Only the document root falls back. A missing /app.js must stay a 404, or a
            # broken asset reference would silently render as this page and look like a
            # UI bug rather than a missing file.
            if e.status_code == 404 and path in ('.', '', 'index.html'):
                return HTMLResponse(_PLACEHOLDER)
            raise


# Mounted LAST, and only last. Starlette matches routes in registration order, so a
# mount at '/' registered earlier would swallow every /api route beneath it.
app.mount('/', _UI(directory=str(WEB_DIR), html=True, check_dir=False), name='ui')
