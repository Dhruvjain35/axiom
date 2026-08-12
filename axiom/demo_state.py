"""AXIOM :: keeping the demo true while nobody is watching it.

Judging runs from 19 Aug to 15 Sep. Nobody will be at the keyboard for most of that
month, and the failure that actually loses this competition is not a wrong answer — it
is a judge opening the URL in week three and finding an empty grid, a red lamp, or a
stack trace. This module exists to make that outcome unreachable.

It holds five separate concerns, and they are in one file because they are one idea:
**the demo must degrade into something honest, never into something broken.**

1. RESILIENT TRANSACTIONS (`tx`, `call`)
   A pooled connection that has been idle across a Lambda freeze — or across a
   CockroachDB Cloud maintenance window — is a live TCP handle to a socket the server
   has already forgotten. psycopg hands it out, the first statement raises
   OperationalError, and the judge's first request of the day fails while everything is
   in fact healthy. `tx()` sweeps the pool with the pool's own `check()` and tries again.

   READS retry freely. WRITES DO NOT — and that distinction is the whole project in
   miniature. If a connection dies after COMMIT is sent and before the acknowledgement
   comes back, the transaction may have committed; a blind retry is then a second
   effect, which is precisely the failure AXIOM exists to argue against. So a write
   path warms the pool first (a read, which is safe to retry, proving the connections
   are live) and then gets exactly one attempt, unless the caller states in writing
   that the body is idempotent-on-replay.

2. MISSION SELECTION (`select_mission_id`)
   `/api/mission` used to show the NEWEST mission for the demo tenant. Every run of
   scripts/counterexample.py creates a one-task mission on that same tenant, so the
   flagship script permanently replaced the 30-task demo with a 1-tile grid in a
   30-tile frame. Selection is now "the newest mission that is actually worth showing",
   which is a property of the data rather than a promise to run scripts in the right
   order.

3. SELF-HEALING STATE (`ensure_demo`)
   If the world is empty, the API seeds it — idempotently, and race-safe across
   processes, so two judges arriving in the same second cannot produce two missions.
   The cross-process mutex is a `SELECT ... FOR UPDATE` on the demo tenant row: under
   SERIALIZABLE the loser either waits or aborts with 40001, and `db.tx` retries it into
   the world the winner just committed.

4. BOUNDED GROWTH (`reap_agents`, `live_workers`, `forget_orders`)
   Every click of RUN MISSION registers a worker row, and nothing ever removed them. The
   left rail was already twelve rows deep on the production cluster with three of them
   real. Forty judges times two clicks is eighty. Growth is capped here rather than in
   the renderer, because a bounded API cannot be un-bounded by a UI change.

5. THE HEADLINE (`forget_orders`)
   DUPLICATE REFUNDS 0 is the number this entire project is judged on. It is computed
   over the external ledger, which is append-only and shared with a script whose entire
   purpose is to double-refund an order on purpose. Any order this module freshly
   enqueues has its ledger history cleared first — a task created ten seconds ago cannot
   legitimately own a refund from last week, and leaving one there would inflate the
   headline with somebody else's evidence.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import typing as t
import uuid

import psycopg
from psycopg_pool import PoolTimeout

from . import db, embeddings, events, memory, policy, provider, seed, tasks
from .config import SYSTEM_TENANT, settings
from .models import MemoryClass, Outcome, TaskState, Trust, ctx_exception, ctx_state
from .seed import DEMO_TENANT

log = logging.getLogger('axiom.demo')

T = t.TypeVar('T')


# ============================================================================ knobs

def _b(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ('1', 'true', 'yes', 'on')


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


# The demo mission's identity. These four values are duplicated from axiom/seed.py on
# purpose: seed.py builds its world inside a nested closure this module cannot call, and
# a wrong copy here would produce a SECOND mission next to the seeded one rather than
# adopting it. tests/test_resilience.py asserts the copies agree, so the duplication is
# checked by the suite rather than by a comment.
DEMO_MISSION_TITLE = "Resolve today's order exceptions"
DEMO_TASKS = 30
DEMO_BUDGET_CENTS = 2500_00
DEMO_POLICY_ID = 'refund_authority'

# A mission with fewer tasks than this is not the demo — it is a script's scratch
# mission (the counterexample creates one per run) and must never take over the screen.
MIN_DEMO_TASKS = _i('AXIOM_MIN_DEMO_TASKS', 5)

AUTOSEED = _b('AXIOM_DEMO_AUTOSEED', True)
AUTOHEAL = _b('AXIOM_DEMO_AUTOHEAL', True)

# How long a "the demo is showable" answer is trusted before we ask the database again.
# Every /api/mission poll would otherwise carry a count query it almost never needs.
HEALTHY_TTL_S = float(_i('AXIOM_DEMO_HEALTH_TTL_S', 30))
# After a failed heal, wait before trying again: a database that is down does not get
# better because a polling dashboard asked it eight times a second.
FAILED_BACKOFF_S = float(_i('AXIOM_DEMO_HEAL_BACKOFF_S', 15))
# A request that finds another thread already seeding waits this long for it, then
# returns the honest empty payload rather than holding the connection open.
SEED_WAIT_S = float(_i('AXIOM_DEMO_SEED_WAIT_S', 10))

# Bounded growth.
AGENT_ROWS_KEPT = _i('AXIOM_AGENT_ROWS_KEPT', 40)      # newest N survive a reap
AGENT_ROW_TTL_S = _i('AXIOM_AGENT_ROW_TTL_S', 3600)    # older than this AND beyond N
MAX_LIVE_WORKERS = _i('AXIOM_MAX_LIVE_WORKERS', 3)     # concurrent demo workers

# Auto-heal: how quiet the board must be before the API starts a worker by itself.
AUTOHEAL_IDLE_S = _i('AXIOM_AUTOHEAL_IDLE_S', 120)
AUTOHEAL_MIN_INTERVAL_S = _i('AXIOM_AUTOHEAL_MIN_INTERVAL_S', 90)

# A dead-on-arrival pooled connection costs one sweep and one retry. Reads only.
READ_ATTEMPTS = 3
# The pool's default wait for a connection is 30s. On a dead database that turns every
# request into a 30-second hang and an uptime monitor into a timeout instead of a 503.
POOL_WAIT_S = float(_i('AXIOM_POOL_WAIT_S', 6))


class Unavailable(RuntimeError):
    """A dependency is down. Carries the honest reason; the API turns it into a 503.

    Deliberately not an HTTPException: this module is importable by scripts that have no
    FastAPI in the picture, and the failure is a fact about the world rather than about
    a request.
    """

    def __init__(self, component: str, detail: str):
        super().__init__(f'{component} unavailable: {detail}')
        self.component = component
        self.detail = detail


# ============================================================== pools and resilience

# psycopg raises these when the connection underneath is gone. They are not errors about
# the query; they are the socket telling us it was closed while nobody was looking.
_DEAD_CONNECTION = (psycopg.OperationalError, psycopg.InterfaceError)

# ...and this is the one that does not. MEASURED, not assumed: cancelling a live
# session with CockroachDB's own `CANCEL SESSION` and then using the pooled connection
# raises psycopg.errors.InternalError_ — a DatabaseError, NOT an OperationalError —
# carrying SQLSTATE XXUUU and a Go network error in the message:
#
#   2026-08-11 19:55:18 ERROR database error XXUUU:
#       read tcp 127.0.0.1:26400->127.0.0.1:51301: i/o timeout
#
# The first version of this retry caught only the psycopg exception classes and let
# that straight through as a 503, which is precisely the "first request after an idle
# gap fails" symptom it exists to prevent. Classifying on the exception type alone
# would have shipped a resilience feature that does not work on this database.
_XXUUU_NETWORK = ('i/o timeout', 'connection reset', 'broken pipe', 'connection refused',
                  'unexpected eof', 'closed', 'no inbound stream connection')


def _is_connection_failure(e: BaseException) -> bool:
    if isinstance(e, _DEAD_CONNECTION):
        return True
    if getattr(getattr(e, 'diag', None), 'sqlstate', None) == 'XXUUU':
        low = str(e).lower()
        return any(s in low for s in _XXUUU_NETWORK)
    return False

_tuned = False


def _tune(p) -> None:
    """Cap one pool's wait-for-a-connection. Idempotent, cheap, never raises."""
    try:
        if getattr(p, 'timeout', 0) > POOL_WAIT_S:
            p.timeout = POOL_WAIT_S
    except Exception as e:                          # noqa: BLE001 — tuning is optional
        log.warning('could not tune pool timeout: %s: %s', type(e).__name__, e)


def tune_pools() -> None:
    """Shorten the pools' wait-for-a-connection timeout. Called once, at API startup.

    `timeout` is a public attribute of psycopg_pool.ConnectionPool and is read per
    `getconn`, so setting it here configures both pools without either module having to
    know this one exists.

    Why not the constructor's `check=` callback, which is the textbook answer to stale
    pooled connections? Three reasons, in order of weight:

      1. The pools are constructed in axiom/db.py and axiom/provider.py, which this
         change does not own. Reaching into `pool._check` — a private attribute — to get
         the behaviour would be exactly the kind of undocumented action-at-a-distance
         this project spends its comments arguing against.
      2. `check=` costs a round trip on EVERY checkout, including the worker's claim
         loop, which is the hottest path in the system and the one whose latency the
         chaos demo is measured on.
      3. It would not remove the retry below anyway. `check=` narrows the window between
         validation and use; it cannot close it. A connection can die in that window, and
         then the caller still has to cope.

    So the deliberate choice is: fail FAST when the database is genuinely gone (this
    function), and recover QUIETLY when only the connection was stale (`tx`). The
    recommended companion change in db.py — `check=ConnectionPool.check_connection,
    max_idle=300` — is reported rather than made.

    `tx()` and `call()` also tune lazily, and that is not belt-and-braces: on Lambda the
    API runs under Mangum with `lifespan='off'` (deploy/lambda/handler_api.py §1), so no
    startup hook fires at all and this function would otherwise never be called in the
    deployed configuration.
    """
    global _tuned
    _tune(db.pool())
    # NOT provider.pool(): calling it CREATES the pool, and a request that never touches
    # the provider should not open a connection to it. handler_api.py makes the same
    # point about revalidation — checking for a pool must not be the thing that connects.
    p = getattr(provider, '_pool', None)
    if p is not None:
        _tune(p)
    _tuned = True


def _sweep() -> None:
    """Discard broken connections from whichever pools exist. `check()` is public.

    Reads the modules' `_pool` globals rather than calling `db.pool()` / `provider.pool()`
    for the reason handler_api.py gives about revalidation: checking for a pool must
    never be the thing that creates one, or a request that only touched the database
    would open a provider connection on its way to failing.
    """
    for mod in (db, provider):
        p = getattr(mod, '_pool', None)
        if p is None:
            continue
        try:
            p.check()
        except Exception as e:                      # noqa: BLE001 — best effort by design
            log.warning('pool check failed: %s: %s', type(e).__name__, e)


def tx(fn: t.Callable[[psycopg.Cursor], T], *, readonly: bool = False,
       as_of: str | None = None, idempotent: bool = False) -> T:
    """`db.tx` that survives a connection the server closed while we were idle.

    `readonly=True` retries freely: a read that never committed anything can be replayed
    without consequence.

    A WRITE gets one attempt unless `idempotent=True`, which the caller uses to state
    that re-running the body after an unknown commit outcome is provably harmless. The
    only two callers that make that claim are the seed path (whose every write is an
    upsert or a dedupe-keyed insert) and the agent reaper (a DELETE of rows selected by
    age). If you are adding a third, prove it in a test first.
    """
    _tune(db.pool())
    attempts = READ_ATTEMPTS if (readonly or as_of or idempotent) else 1
    last: Exception | None = None

    for i in range(attempts):
        try:
            return db.tx(fn, readonly=readonly, as_of=as_of)
        except PoolTimeout as e:
            # Not a stale connection: the pool could not produce one at all. Retrying
            # only makes the judge wait twice as long for the same answer.
            raise Unavailable('db', f'no connection after {POOL_WAIT_S:.0f}s') from e
        except psycopg.Error as e:
            if not _is_connection_failure(e):
                raise
            last = e
            if i == attempts - 1:
                break
            log.warning('stale pooled connection (%s / %s); sweeping and retrying',
                        type(e).__name__,
                        getattr(getattr(e, 'diag', None), 'sqlstate', '-'))
            _sweep()
            time.sleep(0.05 * (i + 1))

    raise Unavailable('db', f'{type(last).__name__}: {last}')


def call(fn: t.Callable[[], T], *, component: str = 'provider') -> T:
    """The same treatment for a call that owns its own connection (provider.*).

    **This retries. Pass it READS and idempotent writes only.** Its four callers today
    are provider.stats, provider.ledger and a DELETE of ledger rows by order ref. Never
    wrap `provider.create_refund` in it: that call is safe to repeat only under the same
    derived idempotency key, and this function cannot see the key, so the safety would be
    an accident rather than a guarantee. Re-sending a refund is the engine's decision to
    make inside the PREPARE/DISPATCH split, not a connection helper's.

    Tuning provider.pool() here does create it — which is correct at THIS call site and
    nowhere else, because every use of `call` is a provider call that was about to open
    that pool one line later anyway.
    """
    _tune(provider.pool())
    last: Exception | None = None
    for i in range(READ_ATTEMPTS):
        try:
            return fn()
        except PoolTimeout as e:
            raise Unavailable(component, f'no connection after {POOL_WAIT_S:.0f}s') from e
        except psycopg.Error as e:
            if not _is_connection_failure(e):
                raise
            last = e
            if i == READ_ATTEMPTS - 1:
                break
            _sweep()
            time.sleep(0.05 * (i + 1))
    raise Unavailable(component, f'{type(last).__name__}: {last}')


def warm() -> None:
    """Prove the pool's connections are live, cheaply, before a non-idempotent write.

    This is what makes "writes are not retried" affordable. The dead-on-arrival case —
    which is the only common one — is absorbed here by a read that is safe to replay, so
    the write itself almost never meets it.
    """
    tx(lambda cur: cur.execute('SELECT 1'), readonly=True)


# ================================================================ mission selection

_SELECT_MISSION = """
    SELECT m.id, count(t.id) AS n_tasks
    FROM axiom_mission m
    LEFT JOIN axiom_task t ON t.mission_id = m.id AND t.tenant_id = m.tenant_id
    WHERE m.tenant_id = %(tenant)s
    GROUP BY m.id, m.created_at
    ORDER BY CASE WHEN count(t.id) >= %(min_tasks)s THEN 2
                  WHEN count(t.id) > 0              THEN 1
                  ELSE 0 END DESC,
             m.created_at DESC
    LIMIT 1
"""


def select_mission_id(cur: psycopg.Cursor, tenant_id: uuid.UUID) -> uuid.UUID | None:
    """The mission the dashboard should show. Newest, among those worth showing.

    Three tiers, highest first:
      2. a mission with at least MIN_DEMO_TASKS tasks — the demo, or something like it
      1. a mission with at least one task — better than a blank screen
      0. a mission with no tasks at all — the shell a script left behind

    Ordering by tier and only then by recency is what stops `scripts/counterexample.py`
    from hijacking the screen: its mission is real, it is newest, and it has one task.
    """
    cur.execute(_SELECT_MISSION, {'tenant': str(tenant_id), 'min_tasks': MIN_DEMO_TASKS})
    row = cur.fetchone()
    return row['id'] if row else None


def mission_order_refs(cur: psycopg.Cursor, tenant_id: uuid.UUID) -> set[str]:
    """The order refs belonging to the shown mission. The provider ledger's scope.

    Returns an EMPTY set when there is no mission, and the caller must treat that as
    "scope to nothing" rather than "scope to everything". Falling back to the global
    ledger is how the headline number ends up counting a deliberate double-refund that
    another script created to prove a point.
    """
    mid = select_mission_id(cur, tenant_id)
    if mid is None:
        return set()
    cur.execute("""
        SELECT payload->>'order_ref' AS order_ref
        FROM axiom_task WHERE tenant_id = %s AND mission_id = %s
    """, (str(tenant_id), str(mid)))
    return {r['order_ref'] for r in cur.fetchall() if r['order_ref']}


# ======================================================================= the probe

def probe(tenant_id: uuid.UUID = DEMO_TENANT) -> dict:
    """One round trip that answers "is there a coherent demo here?".

    Cheap on purpose: four counts and one selection over tables that hold tens of rows
    for the demo tenant. This runs on the health check and behind the self-heal gate.
    """
    def _read(cur):
        mid = select_mission_id(cur, tenant_id)
        cur.execute("""
            SELECT
              (SELECT count(*) FROM axiom_task   WHERE tenant_id = %(t)s) AS tasks,
              (SELECT count(*) FROM axiom_memory WHERE tenant_id = %(t)s) AS memories,
              (SELECT count(*) FROM axiom_policy
                 WHERE tenant_id = %(t)s AND status = 'ACTIVE')           AS policies,
              (SELECT count(*) FROM axiom_mission WHERE tenant_id = %(t)s) AS missions
        """, {'t': str(tenant_id)})
        out = dict(cur.fetchone())
        out['mission_id'] = mid
        if mid is not None:
            out.update(tasks.mission_summary(cur, tenant_id=tenant_id, mission_id=mid))
        return out

    return tx(_read, readonly=True)


def is_coherent(p: dict) -> bool:
    """What "the judge sees something real" means, as one predicate.

    A mission worth showing, tasks under it, an active policy that authorizes the
    refunds, and the prior memories the recall demo reads from. Missing any one of them
    makes some panel of Mission Control lie about the system rather than describe it.
    """
    return bool(p.get('mission_id')) and p.get('tasks', 0) > 0 \
        and p.get('memories', 0) > 0 and p.get('policies', 0) > 0


# ================================================================== self-healing seed

_seed_lock = threading.Lock()
_gate_lock = threading.Lock()
_healthy_until = 0.0        # monotonic deadline; before it, skip the probe entirely
_failed_until = 0.0         # monotonic deadline; before it, do not try to heal again
_heals = 0                  # how many times this process has healed the world


def _seed_body(cur: psycopg.Cursor, *, prior_vecs, sem_vecs, n_tasks: int,
               budget_cents: int) -> dict:
    """Build (or complete) the demo world inside ONE serializable transaction.

    Every step is convergent, which is what makes the whole function safe to run twice:

      tenant    INSERT ... ON CONFLICT DO NOTHING
      LOCK      SELECT ... FOR UPDATE on the tenant row — the cross-process mutex
      policy    published only when the tenant has no ACTIVE version
      mission   adopted by title when one already exists, created otherwise
      memories  written only when the tenant has none
      tasks     enqueue(), which is dedupe-keyed and returns None on the second call

    The lock is taken AFTER the upsert so the row provably exists, and BEFORE the
    re-check so that a second process either waits for our commit or is aborted with
    40001 and re-runs against the world we just made.
    """
    cur.execute("""
        INSERT INTO axiom_tenant (id, slug, display_name)
        VALUES (%s, 'acme', 'ACME Commerce') ON CONFLICT (id) DO NOTHING
    """, (str(DEMO_TENANT),))
    cur.execute('SELECT id FROM axiom_tenant WHERE id = %s FOR UPDATE',
                (str(DEMO_TENANT),))

    # Re-check under the lock. This is the branch a losing racer takes.
    cur.execute("""
        SELECT
          (SELECT count(*) FROM axiom_task   WHERE tenant_id = %(t)s) AS tasks,
          (SELECT count(*) FROM axiom_memory WHERE tenant_id = %(t)s) AS memories,
          (SELECT count(*) FROM axiom_policy
             WHERE tenant_id = %(t)s AND status = 'ACTIVE')           AS policies
    """, {'t': str(DEMO_TENANT)})
    have = dict(cur.fetchone())
    mid = select_mission_id(cur, DEMO_TENANT)
    if mid is not None and have['tasks'] > 0 and have['memories'] > 0 \
            and have['policies'] > 0:
        return {'seeded': False, 'mission_id': mid, 'created_tasks': 0,
                'created_memories': 0, 'created_orders': []}

    if have['policies'] == 0:
        # Never republish version 1 on top of a retired one: (tenant, policy_id,
        # version) is unique, so the second seed of a half-wiped tenant would abort on
        # 23505 and take the whole heal with it.
        cur.execute("""
            SELECT coalesce(max(version), 0) AS v FROM axiom_policy
            WHERE tenant_id = %s AND policy_id = %s
        """, (str(DEMO_TENANT), DEMO_POLICY_ID))
        version = int(cur.fetchone()['v']) + 1
        policy.publish(
            cur, tenant_id=DEMO_TENANT, policy_id=DEMO_POLICY_ID, version=version,
            body={'description': 'Autonomous refund authority for order exceptions',
                  'max_auto_action_cents': 20000,
                  'escalate_kinds': ['fraud_suspected'],
                  'rationale': 'A refund above $200 is a business decision, not an '
                               'operational one, and gets a human.'},
            max_auto_action_cents=20000, requires_approval=False,
            created_by='system:selfheal', activate=True,
            signature='demo-signature', signed_by='human:cfo@acme.example')

    if mid is None:
        cur.execute("""
            SELECT id FROM axiom_mission
            WHERE tenant_id = %s AND title = %s ORDER BY created_at DESC LIMIT 1
        """, (str(DEMO_TENANT), DEMO_MISSION_TITLE))
        row = cur.fetchone()
        mid = row['id'] if row else tasks.create_mission(
            cur, tenant_id=DEMO_TENANT, title=DEMO_MISSION_TITLE,
            goal=f'Resolve {n_tasks} open order exceptions without double-refunding '
                 f'anyone', budget_cents=budget_cents, created_by='system:selfheal')

    created_memories = 0
    if have['memories'] == 0:
        for content, outcome, vec in prior_vecs:
            memory.write(cur, tenant_id=DEMO_TENANT, memory_class=MemoryClass.EPISODIC,
                         context_key=ctx_state(TaskState.ACTION_PREPARED),
                         content=content, embedding=vec, outcome=outcome,
                         source='system:execution', trust_level=Trust.FIRST_PARTY,
                         actor='system:selfheal')
            created_memories += 1
        for kind, content, vec in sem_vecs:
            memory.write(cur, tenant_id=DEMO_TENANT, memory_class=MemoryClass.SEMANTIC,
                         context_key=ctx_exception(kind), content=content, embedding=vec,
                         outcome=Outcome.RESOLVED, source='human:operator',
                         trust_level=Trust.VERIFIED, actor='system:selfheal')
            created_memories += 1

    created_orders: list[str] = []
    for i in range(n_tasks):
        desc, kind, amount = seed._pick(i)
        order = f'ORD-{1000 + i}'
        tid = tasks.enqueue(
            cur, tenant_id=DEMO_TENANT, mission_id=mid, task_type='refund',
            dedupe_key=f'order:{order}:refund',
            payload={'order_ref': order, 'description': desc,
                     'exception_kind': kind, 'amount_cents': amount},
            actor='system:selfheal')
        if tid:
            created_orders.append(order)

    events.append(cur, tenant_id=DEMO_TENANT, subject_type='mission', subject_id=mid,
                  event_type='demo.selfhealed', actor='system:selfheal',
                  mission_id=mid,
                  detail={'created_tasks': len(created_orders),
                          'created_memories': created_memories,
                          'had': have})
    return {'seeded': True, 'mission_id': mid, 'created_tasks': len(created_orders),
            'created_memories': created_memories, 'created_orders': created_orders}


def ensure_demo(*, force: bool = False, recheck: bool = False,
                use_process_lock: bool = True) -> dict:
    """Make sure a judge who arrives right now sees a coherent world. Idempotent.

    Returns a small record of what it did. Never raises for "the world was fine" and
    never raises for "somebody else is already fixing it"; it raises `Unavailable` only
    when the database itself cannot be reached, because at that point there is nothing
    honest left to render and the caller should say so.

    Three levels of insistence, and the middle one exists because of a measured bug:

      default   trust the cached "healthy" answer for HEALTHY_TTL_S seconds
      recheck   the caller has just READ an empty world, so the cache is provably
                stale — probe again, but still respect the failure backoff
      force     a human pressed RESET; ignore both caches and rebuild

    Without `recheck`, a database wiped from outside this process left the API serving
    its honest empty payload for up to thirty seconds after the read that proved the
    cache wrong. Measured: two polls twelve seconds apart both returned `state: EMPTY`
    with a healthy database sitting underneath.

    `use_process_lock=False` exists for the concurrency test, which needs several
    threads to reach the DATABASE mutex rather than queue politely behind a Python one.
    """
    global _healthy_until, _failed_until, _heals

    now = time.monotonic()
    if not force and not recheck and now < _healthy_until:
        return {'checked': False, 'seeded': False}
    if not force and now < _failed_until:
        return {'checked': False, 'seeded': False, 'backing_off': True}
    if not AUTOSEED and not force:
        return {'checked': False, 'seeded': False, 'disabled': True}

    p = probe()
    if is_coherent(p) and not force:
        _healthy_until = time.monotonic() + HEALTHY_TTL_S
        return {'checked': True, 'seeded': False, 'mission_id': p['mission_id']}

    if use_process_lock:
        if not _seed_lock.acquire(timeout=SEED_WAIT_S):
            # Another thread is seeding and is taking longer than a judge's patience.
            # Say so rather than piling on: the caller renders the honest empty state.
            return {'checked': True, 'seeded': False, 'waiting': True}
        try:
            return _heal(p, force=force)
        finally:
            _seed_lock.release()
    return _heal(p, force=force)


def _heal(p: dict, *, force: bool) -> dict:
    global _healthy_until, _failed_until, _heals

    # Embeddings BEFORE the transaction: db.tx re-runs its callable on 40001, and in
    # online mode each of these fifteen strings is a Bedrock call.
    try:
        prior_vecs = [(c, o, embeddings.embed_list(c)) for c, o in seed.PRIOR_RECOVERIES]
        sem_vecs = [(k, c, embeddings.embed_list(c)) for k, c in seed.PRIOR_SEMANTIC]
    except Exception as e:                          # noqa: BLE001 — Bedrock, or offline
        _failed_until = time.monotonic() + FAILED_BACKOFF_S
        raise Unavailable('embeddings', f'{type(e).__name__}: {e}') from e

    try:
        out = tx(lambda cur: _seed_body(cur, prior_vecs=prior_vecs, sem_vecs=sem_vecs,
                                        n_tasks=DEMO_TASKS,
                                        budget_cents=DEMO_BUDGET_CENTS),
                 idempotent=True)
    except Unavailable:
        _failed_until = time.monotonic() + FAILED_BACKOFF_S
        raise
    except Exception as e:                          # noqa: BLE001
        _failed_until = time.monotonic() + FAILED_BACKOFF_S
        log.error('self-heal failed: %s: %s', type(e).__name__, e)
        raise Unavailable('db', f'{type(e).__name__}: {e}') from e

    if out['created_orders']:
        # A task created one second ago cannot own a refund from last week. Clearing the
        # external history of exactly the orders we just (re)created is what keeps
        # DUPLICATE REFUNDS honest across a reset — and it touches nothing else, so a
        # counterexample run happening at the same instant keeps its evidence.
        try:
            forget_orders(out['created_orders'])
        except Exception as e:                      # noqa: BLE001 — provider is optional
            log.warning('could not clear ledger history for reseeded orders: %s', e)

    if out['seeded']:
        _heals += 1
        log.warning('self-healed the demo: %d tasks, %d memories (mission %s)',
                    out['created_tasks'], out['created_memories'], out['mission_id'])
    _healthy_until = time.monotonic() + HEALTHY_TTL_S
    _failed_until = 0.0
    return {'checked': True, **out}


def invalidate() -> None:
    """Forget the cached "healthy" answer. Called by anything that changes the world."""
    global _healthy_until, _failed_until
    _healthy_until = 0.0
    _failed_until = 0.0


def heals() -> int:
    return _heals


# ==================================================================== bounded growth

def forget_orders(order_refs: t.Sequence[str]) -> int:
    """Delete the external ledger's history for these orders. Returns rows removed.

    Raw SQL against provider.pool() rather than a function in axiom/provider.py, because
    this change does not own that file. The natural home for it is
    `provider.forget(order_refs)`; that is reported, not done.
    """
    refs = [r for r in dict.fromkeys(order_refs) if r]
    if not refs:
        return 0

    def _do() -> int:
        with provider.pool().connection() as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute('DELETE FROM provider_refund WHERE order_ref = ANY(%s)',
                            (refs,))
                n = cur.rowcount or 0
                cur.execute('DELETE FROM provider_request_log WHERE order_ref = ANY(%s)',
                            (refs,))
        return n

    return call(_do)


def reap_agents() -> int:
    """Delete worker rows that are neither recent nor among the newest kept. Returns n.

    Safe by construction, and the schema says why in prose: axiom_task.lease_owner is
    deliberately NOT a foreign key to axiom_agent, so removing an agent row cannot
    cascade into the task table and cannot un-own a live lease. Ownership is proven by
    lease_epoch, which lives in the task row.
    """
    def _do(cur):
        cur.execute("""
            DELETE FROM axiom_agent
            WHERE tenant_id = %(t)s
              AND heartbeat_at < now() - %(ttl)s::INTERVAL
              AND id NOT IN (
                  SELECT id FROM axiom_agent WHERE tenant_id = %(t)s
                  ORDER BY heartbeat_at DESC LIMIT %(keep)s)
        """, {'t': str(SYSTEM_TENANT), 'ttl': f'{AGENT_ROW_TTL_S} seconds',
              'keep': AGENT_ROWS_KEPT})
        return cur.rowcount or 0

    return tx(_do, idempotent=True)


def live_workers() -> int:
    """Workers that have heartbeated recently enough to still be running.

    Three lease periods, not one: a worker mid-refund against a slow provider can be
    perfectly healthy and one beat late, and counting it as dead is how a demo ends up
    with eight python processes on a one-core instance.
    """
    window = max(15, settings.lease_seconds * 3)

    def _do(cur):
        cur.execute("""
            SELECT count(*) AS n FROM axiom_agent
            WHERE tenant_id = %s AND status IN ('STARTING', 'ALIVE', 'DRAINING')
              AND heartbeat_at > now() - %s::INTERVAL
        """, (str(SYSTEM_TENANT), f'{window} seconds'))
        return int(cur.fetchone()['n'])

    return tx(_do, readonly=True)


# ======================================================================= rate gates

_gates: dict[str, float] = {}


def gate(name: str, min_interval_s: float) -> float:
    """Token-bucket-of-one. Returns 0.0 if the caller may proceed, else seconds to wait.

    In-process, which is the correct scope: it exists to stop ONE judge's double-click
    and ONE crawler's retry loop from turning into thirty subprocesses, not to be a
    distributed quota. On Lambda that means per-container, and that is still every
    protection this needs — the concurrency it guards is a single container's.
    """
    now = time.monotonic()
    with _gate_lock:
        nxt = _gates.get(name, 0.0)
        if now < nxt:
            return round(nxt - now, 1)
        _gates[name] = now + min_interval_s
        return 0.0


def reset_gates() -> None:
    with _gate_lock:
        _gates.clear()


# ========================================================================= auto-heal

_last_autoheal = 0.0


def claimable_work(tenant_id: uuid.UUID = DEMO_TENANT) -> dict:
    """Work a worker could pick up right now, and how long the board has been still.

    AWAITING_APPROVAL is excluded even though `tasks.claim` accepts it: a task parked on
    a human decision is not stuck, it is waiting correctly, and starting a worker to
    "fix" it would loop it through the approval path forever.
    """
    def _do(cur):
        cur.execute("""
            SELECT count(*) AS n FROM axiom_task
            WHERE tenant_id = %s AND available_at <= now()
              AND state IN ('READY', 'LEASED', 'ACTION_PREPARED')
              AND attempt < max_attempts
        """, (str(tenant_id),))
        n = int(cur.fetchone()['n'])
        cur.execute("""
            SELECT coalesce(extract(epoch FROM (now() - max(updated_at))), 1e9) AS idle
            FROM axiom_task WHERE tenant_id = %s
        """, (str(tenant_id),))
        return {'claimable': n, 'idle_seconds': float(cur.fetchone()['idle'])}

    return tx(_do, readonly=True)


def should_autoheal() -> tuple[bool, str]:
    """Decide whether the API should start a worker on its own. Explains itself.

    Every condition is a reason NOT to act, and they are all conservative, because the
    worst outcome of this feature is a worker draining the queue in the middle of a
    take that the operator is recording.
    """
    global _last_autoheal
    if not AUTOHEAL:
        return False, 'disabled'
    now = time.monotonic()
    if now - _last_autoheal < AUTOHEAL_MIN_INTERVAL_S:
        return False, 'rate limited'
    # The two queries below are cheap but they would otherwise run on every poll of a
    # dashboard that polls once a second. Probing on a gate keeps this feature's cost at
    # two queries a quarter-minute no matter how many tabs are open.
    if gate('autoheal_probe', 15):
        return False, 'probed recently'
    try:
        if live_workers() > 0:
            return False, 'a worker is already alive'
        w = claimable_work()
    except Unavailable as e:
        return False, e.detail
    if w['claimable'] == 0:
        return False, 'nothing claimable'
    if w['idle_seconds'] < AUTOHEAL_IDLE_S:
        return False, f'board changed {w["idle_seconds"]:.0f}s ago'
    _last_autoheal = now
    return True, f'{w["claimable"]} claimable tasks, idle {w["idle_seconds"]:.0f}s'
