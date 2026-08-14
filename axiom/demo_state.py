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

4. BOUNDED GROWTH (`reap_agents`, `live_workers`, `forget_orders`, `forget_campaigns`)
   Every click of RUN MISSION registers a worker row, and nothing ever removed them. The
   left rail was already twelve rows deep on the production cluster with three of them
   real. Forty judges times two clicks is eighty. Growth is capped here rather than in
   the renderer, because a bounded API cannot be un-bounded by a UI change.

5. THE HEADLINE (`forget_orders`, `forget_campaigns`)
   DUPLICATE REFUNDS 0 is the number this entire project is judged on. It is computed
   over the external ledger, which is append-only and shared with a script whose entire
   purpose is to double-refund an order on purpose. Any order this module freshly
   enqueues has its ledger history cleared first — a task created ten seconds ago cannot
   legitimately own a refund from last week, and leaving one there would inflate the
   headline with somebody else's evidence.

   The second domain has the SAME hazard and it is strictly worse, because the relay's
   recipient addresses are derived from the campaign ref rather than being random. So
   re-running campaign CMP-2002 under a new task id — which is what a second press of a
   proof endpoint produces, since the idempotency key is derived from `task_id` — inserts
   a SECOND delivery row for every address the first send reached. MEASURED, not feared:

       relay.send(key='key-run-1', campaign_ref='CMP-DRIFT-TEST', recipient_count=50)
       relay.send(key='key-run-2', campaign_ref='CMP-DRIFT-TEST', recipient_count=50)
       -> duplicate_recipients(['CMP-DRIFT-TEST']) == 50

   Fifty people messaged twice, by the system whose headline is that nobody is ever
   messaged twice. `forget_campaigns` is what keeps that number honest across re-runs,
   and it is the same shape as `forget_orders` for the same reason.

6. FIXTURE INTEGRITY (`_restore_fixture_memories`, `_reap_experiment_memories`)
   The demo's prior memories are a FIXTURE — ten rows the recall panel and the recovery
   demo both read from. Two things a judge can do at the URL can destroy that fixture
   permanently, and neither of them is a bug in the engine:

     * `POST /api/memories/{id}/quarantine` is ungated and nothing ever releases one.
       Quarantine is a demo BEAT — the whole point is that it takes effect at commit —
       but a beat that cannot be undone is a one-shot. Quarantine the recall corpus in
       week one and every judge after it sees an empty recall panel.
     * a proof endpoint that writes adverse memories to make a decision flip, and dies
       before deleting them, leaves DUPLICATE_EFFECT memories admissible in the demo
       tenant. The flagship refund demo then ESCALATES where it used to RESEND, for
       everybody, forever, and nothing anywhere reports an error.

   So admissibility of the fixture is an INVARIANT of "there is a coherent demo here"
   rather than a thing anyone remembers to restore, and experiment rows carry a tag with
   a TTL so the failure mode of a half-finished experiment is that it expires.
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

# WHAT COUNTS AS THE FIXTURE, and why it is identified by CONTENT.
#
# axiom_memory has no `created_by` column — `memory.write(actor=...)` names the actor in
# the event journal, not in the row — so "was this written by the seed?" cannot be asked
# of the table directly. The obvious workaround is a marker column or a convention on
# `source`, and both are wrong here: `source` is real provenance ('system:execution' is
# what the SETTLE path writes too), and inventing a column means a migration to a schema
# that is already applied to the live cluster.
#
# So the fixture is identified by what it IS. `content_sha256` is already stored and
# already computed from the content, and the fixture's content is a literal in seed.py —
# so the set of fixture rows is derivable, exact, and cannot drift from what seed.py
# actually writes. tests/test_resilience.py asserts the derivation still matches.
def fixture_content_hashes() -> list[str]:
    return [embeddings.content_sha256(c) for c, _ in seed.PRIOR_RECOVERIES] + \
           [embeddings.content_sha256(c) for _, c in seed.PRIOR_SEMANTIC]

# How long a fixture memory may stay quarantined before it is released. Long enough that
# a judge can quarantine one, re-run recall, and watch it be gone — which IS the beat —
# and short enough that the next judge, days later, finds the corpus intact.
FIXTURE_QUARANTINE_TTL_S = _i('AXIOM_FIXTURE_QUARANTINE_TTL_S', 900)

# ...and the floor at which we stop waiting for the TTL. Below this the recall panel has
# nothing left to show, which is not a beat, it is an empty screen.
MIN_ADMISSIBLE_MEMORIES = _i('AXIOM_MIN_ADMISSIBLE_MEMORIES', 3)

# THE CONTRACT FOR PROOF ENDPOINTS THAT WRITE MEMORIES TO PROVE A POINT.
#
# scripts/memory_decides.py inserts two adverse memories, flips a recovery decision with
# them, quarantines them, and deletes them on the way out. An HTTP endpoint doing the same
# thing cannot rely on reaching its own cleanup — the request can be cancelled, the
# instance can be frozen, the process can be recycled between the write and the delete.
#
# So any memory a proof endpoint writes MUST carry `source_ref=EXPERIMENT_SOURCE_REF`.
# `source_ref` is the existing free-text provenance column on axiom_memory and it is
# already threaded through `memory.write(source_ref=...)`, so this costs no migration and
# no new convention — it is the column that already means "where exactly did this come
# from". That one string is what makes an abandoned experiment expire instead of
# permanently changing what the flagship demo decides.
EXPERIMENT_SOURCE_REF = 'demo:proof-experiment'
EXPERIMENT_TTL_S = _i('AXIOM_EXPERIMENT_TTL_S', 900)

# How often the periodic hygiene pass may run, per process. Ten minutes is chosen against
# the poll ladder in web/app.js: an abandoned tab settles to one cycle a minute, so this
# is roughly one bounded DELETE per ten polls, and a burst of Vercel instances multiplies
# the COUNT of maintenance passes but not the amount of work any one of them finds to do.
MAINTAIN_INTERVAL_S = _i('AXIOM_MAINTAIN_INTERVAL_S', 600)

# Auto-heal: how quiet the board must be before the API starts a worker by itself.
AUTOHEAL_IDLE_S = _i('AXIOM_AUTOHEAL_IDLE_S', 120)
AUTOHEAL_MIN_INTERVAL_S = _i('AXIOM_AUTOHEAL_MIN_INTERVAL_S', 90)

# A dead-on-arrival pooled connection costs one sweep and one retry. Reads only.
READ_ATTEMPTS = 3
# How many times a PROVIDER read may wait out a full pool timeout. See `call`. Two, so a
# cold-start connection storm resolves and a genuinely dead dependency is still named
# inside 2 x POOL_WAIT_S.
POOL_TIMEOUT_ATTEMPTS = _i('AXIOM_POOL_TIMEOUT_ATTEMPTS', 2)
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

    WHY A POOL TIMEOUT IS RETRIED HERE AND NOT IN `tx`
    --------------------------------------------------
    MEASURED against the live Vercel deployment on 2026-08-13. Twenty simultaneous GETs
    to /api/health returned twenty DIFFERENT `booted_at` values — Vercel answers a burst
    by cold-starting one instance per concurrent request — and six of the twenty came
    back 503:

        {"db":true,"provider":false,
         "errors":{"provider":"provider unavailable: no connection after 6s"},
         "checks":{"db":{"latency_ms":11.9},"provider":{"latency_ms":6013.8}}}

    The database pool was fine at 12ms and the PROVIDER pool timed out at exactly the 6s
    budget, on the same instance, in the same request. The difference between them is
    that db.pool() is opened at import with min_size connections already in flight, while
    provider.pool() is created lazily inside the request — so twenty cold instances all
    opened their first provider connection at the same instant and CockroachDB Basic
    served some of them slower than 6s. Twelve sequential polls a minute later reused ONE
    instance and every one of them answered in under 300ms.

    So this is not "the database is down", it is "this connection was queued behind
    nineteen other TLS handshakes", and the honest answer to it is to wait once more
    rather than to tell an uptime monitor the provider is gone. One extra attempt, not
    READ_ATTEMPTS of them: the budget is bounded at 2 x POOL_WAIT_S so a genuinely dead
    provider is still reported in twelve seconds rather than eventually.

    `tx` deliberately does NOT do this. Its pool is warm by construction, so a timeout
    there really does mean the cluster stopped answering, and doubling the wait would only
    double how long the page hangs before it can say so.
    """
    # Only for the provider, and only because every provider call was about to open that
    # pool one line later anyway. A relay call must not be the thing that connects to the
    # payment provider — `_sweep`'s docstring makes the same point about revalidation.
    if component == 'provider':
        _tune(provider.pool())
    last: Exception | None = None
    for i in range(READ_ATTEMPTS):
        try:
            return fn()
        except PoolTimeout as e:
            last = e
            if i >= POOL_TIMEOUT_ATTEMPTS - 1:
                raise Unavailable(
                    component,
                    f'no connection after {POOL_WAIT_S * POOL_TIMEOUT_ATTEMPTS:.0f}s') from e
            log.warning('%s pool produced no connection in %.0fs; one more attempt',
                        component, POOL_WAIT_S)
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
              (SELECT count(*) FROM axiom_memory
                 WHERE tenant_id = %(t)s
                   AND retrieval_class = 'ACTIONABLE')                    AS admissible,
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

    ADMISSIBLE memories, not merely present ones. A memory whose retrieval_class is
    QUARANTINED sits in a different partition of the vector index and cannot enter an ANN
    candidate set at all — which is the good property the quarantine demo exists to show,
    and exactly why counting rows here was wrong. A tenant holding forty quarantined
    memories and no admissible ones passes `memories > 0` while every recall on the page
    returns nothing, and `POST /api/memories/{id}/quarantine` is ungated, so getting there
    takes a judge with a mouse rather than an attacker.
    """
    return bool(p.get('mission_id')) and p.get('tasks', 0) > 0 \
        and p.get('admissible', p.get('memories', 0)) > 0 and p.get('policies', 0) > 0


# ================================================================== self-healing seed

_seed_lock = threading.Lock()
_gate_lock = threading.Lock()
_healthy_until = 0.0        # monotonic deadline; before it, skip the probe entirely
_failed_until = 0.0         # monotonic deadline; before it, do not try to heal again
_heals = 0                  # how many times this process has healed the world


def _restore_fixture_memories(cur: psycopg.Cursor, *, floor_breached: bool) -> int:
    """Release fixture memories somebody quarantined. Returns how many came back.

    Bounded by construction: it can only ever touch the ten rows `seed.py` writes, it can
    only move them in one direction, and running it twice is a no-op — so there is no
    amount of judging that makes this do more work than it did the first time.

    Two triggers, and the difference between them is the whole design:

      TTL          a quarantined fixture memory older than FIXTURE_QUARANTINE_TTL_S is
                   released. This is what makes the quarantine BEAT survivable: a judge
                   quarantines a memory, re-runs recall, sees it gone from the candidate
                   set — which is the demonstration — and fifteen minutes later the demo
                   is whole again for the next person.
      floor        if fewer than MIN_ADMISSIBLE_MEMORIES remain admissible, the TTL is
                   ignored and everything comes back NOW. Waiting politely for fifteen
                   minutes while the recall panel renders an empty list is not a demo, and
                   `is_coherent` has already declined to call that state healthy — so
                   without this branch a fully-quarantined tenant would pin /api/health at
                   503 for a quarter of an hour with nothing able to fix it.

    `quarantined_at` has to be nulled with the flag: axiom_memory_quar_ck asserts
    `(quarantined = false) = (quarantined_at IS NULL)`, so setting one without the other
    is a check-constraint violation, not a partially-applied update. The reason and the
    releasing actor stay in the journal below rather than in the row, because the row's
    quarantine columns describe a quarantine that is no longer in force.
    """
    cur.execute(f"""
        UPDATE axiom_memory
        SET quarantined = false, quarantined_at = NULL, quarantined_by = NULL,
            quarantine_reason = NULL
        WHERE tenant_id = %(t)s
          AND quarantined = true
          AND content_sha256 = ANY(%(hashes)s)
          {'' if floor_breached else
           "AND quarantined_at < now() - %(ttl)s::INTERVAL"}
        RETURNING id
    """, {'t': str(DEMO_TENANT), 'hashes': fixture_content_hashes(),
          'ttl': f'{FIXTURE_QUARANTINE_TTL_S} seconds'})
    released = [r['id'] for r in cur.fetchall()]
    for mid in released:
        events.append(cur, tenant_id=DEMO_TENANT, subject_type='memory', subject_id=mid,
                      event_type='memory.quarantine_released', actor='system:selfheal',
                      detail={'reason': 'demo fixture restored',
                              'trigger': 'admissible floor' if floor_breached else 'ttl'})
    return len(released)


def _reap_experiment_memories(cur: psycopg.Cursor) -> int:
    """Delete expired proof-experiment memories. Returns how many were removed.

    The rows a memory-experiment endpoint writes to make a decision flip are scaffolding,
    not history: they exist to be recalled once, inside one request, and then to stop
    existing. `scripts/memory_decides.py` deletes its own on the way out, which is correct
    for a script an operator ran and watched. An HTTP endpoint cannot make that promise —
    the request can be cancelled mid-flight and a serverless instance can be frozen
    between the write and the delete — so the cleanup cannot be the only thing standing
    between an abandoned experiment and a permanently changed demo.

    This is a DELETE rather than a quarantine on purpose. A quarantined DUPLICATE_EFFECT
    memory is still visible in the memory browser and still reads, to someone looking at
    the page, as something the system actually lived through. It did not; a request made
    it up to prove a point.
    """
    cur.execute("""
        DELETE FROM axiom_memory
        WHERE tenant_id = %s AND source_ref = %s
          AND occurred_at < now() - %s::INTERVAL
    """, (str(DEMO_TENANT), EXPERIMENT_SOURCE_REF, f'{EXPERIMENT_TTL_S} seconds'))
    return cur.rowcount or 0


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

    # Repair the FIXTURE before counting it, and in this order for a reason: an expired
    # experiment memory is admissible until it is deleted, so reaping first stops it from
    # propping up the admissible count and hiding a quarantined corpus underneath.
    #
    # Both are bounded, both are idempotent, and neither counts as "seeding" — a heal that
    # only had to un-quarantine a row did not build a world, and reporting that it did
    # would make `heals()` and the concurrency test lie.
    reaped = _reap_experiment_memories(cur)
    cur.execute("""
        SELECT count(*) AS n FROM axiom_memory
        WHERE tenant_id = %s AND retrieval_class = 'ACTIONABLE'
    """, (str(DEMO_TENANT),))
    released = _restore_fixture_memories(
        cur, floor_breached=int(cur.fetchone()['n']) < MIN_ADMISSIBLE_MEMORIES)

    # Re-check under the lock. This is the branch a losing racer takes.
    cur.execute("""
        SELECT
          (SELECT count(*) FROM axiom_task   WHERE tenant_id = %(t)s) AS tasks,
          (SELECT count(*) FROM axiom_memory WHERE tenant_id = %(t)s) AS memories,
          (SELECT count(*) FROM axiom_memory
             WHERE tenant_id = %(t)s AND retrieval_class = 'ACTIONABLE') AS admissible,
          (SELECT count(*) FROM axiom_policy
             WHERE tenant_id = %(t)s AND status = 'ACTIVE')           AS policies
    """, {'t': str(DEMO_TENANT)})
    have = dict(cur.fetchone())
    mid = select_mission_id(cur, DEMO_TENANT)
    if mid is not None and have['tasks'] > 0 and have['admissible'] > 0 \
            and have['policies'] > 0:
        return {'seeded': False, 'mission_id': mid, 'created_tasks': 0,
                'created_memories': 0, 'created_orders': [],
                'released_memories': released, 'reaped_memories': reaped}

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
                          'released_memories': released, 'reaped_memories': reaped,
                          'had': have})
    return {'seeded': True, 'mission_id': mid, 'created_tasks': len(created_orders),
            'created_memories': created_memories, 'created_orders': created_orders,
            'released_memories': released, 'reaped_memories': reaped}


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


def forget_campaigns(campaign_refs: t.Sequence[str]) -> int:
    """Delete the relay's delivery history for these campaigns. Returns rows removed.

    THE CONTRACT: anything that is about to (re-)enqueue broadcast tasks for a campaign
    must call this with that campaign's ref FIRST. Not as tidiness — as the thing that
    keeps the second domain's headline true.

    Why it is needed at all, given that the refund side gets away without it in the steady
    state: `relay_delivery` holds one row per RECIPIENT per send, and the relay derives
    recipient addresses deterministically from the campaign ref
    (`CMP-2002+17@example.invalid`). The idempotency key, meanwhile, is a GENERATED column
    over `task_id` — so a re-run of the same campaign under a NEW task is a new key, the
    relay correctly treats it as a new send, and every address from the first send gets a
    second row. `duplicate_recipients()` then reports them, correctly, as people messaged
    twice.

    Measured on the local cluster before this existed:

        send(key='key-run-1', campaign_ref='CMP-DRIFT-TEST', recipient_count=50) -> 201
        send(key='key-run-2', campaign_ref='CMP-DRIFT-TEST', recipient_count=50) -> 201
        duplicate_recipients(['CMP-DRIFT-TEST']) -> 50 rows, 2 deliveries each

    Fifty is only the test size. The real campaigns are 310 to 15,000 recipients, so the
    second press of a broadcast proof would have put a five-figure number under the
    heading RECIPIENTS MESSAGED TWICE — on the demo whose entire claim is that the number
    is zero, in the panel a judge is looking at, permanently, until someone reset it.

    Clearing first makes the steady state bounded BY CONSTRUCTION rather than by a cleanup
    schedule: after any number of presses the relay holds exactly one copy of each
    campaign's recipients, because each run deletes precisely what it is about to recreate.

    The relay import is deliberately local. `relay.pool()` provisions its own database on
    first touch (`CREATE DATABASE IF NOT EXISTS relay`), and a module-level import here
    would put that provisioning on the import path of every process that touches
    demo_state — including the health check of a deployment that has no second domain.
    """
    refs = [r for r in dict.fromkeys(campaign_refs) if r]
    if not refs:
        return 0

    from .domains import relay                      # local: see the docstring

    def _do() -> int:
        with relay.pool().connection() as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute('DELETE FROM relay_delivery WHERE campaign_ref = ANY(%s)',
                            (refs,))
                n = cur.rowcount or 0
                cur.execute('DELETE FROM relay_send WHERE campaign_ref = ANY(%s)', (refs,))
                cur.execute('DELETE FROM relay_request_log WHERE campaign_ref = ANY(%s)',
                            (refs,))
        return n

    return call(_do, component='relay')


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

    In-process, which is the correct scope for what it was built for: stopping ONE judge's
    double-click and ONE crawler's retry loop from turning into thirty subprocesses.

    IT IS NOT A QUOTA, AND ON VERCEL IT IS NOT EVEN CLOSE TO ONE.
    -------------------------------------------------------------
    This used to say "per-container, and that is still every protection this needs". That
    was wrong, and the measurement is unambiguous. Twenty simultaneous GETs to
    /api/health on the live deployment came back with twenty DIFFERENT `booted_at`
    values — Vercel answers a burst by cold-starting one instance per concurrent request,
    each with its own interpreter, its own `_gates` dict, and therefore its own fresh
    permission to act. Twelve SEQUENTIAL requests a minute later were served by exactly
    one instance, so the gate does work against a human clicking twice.

    The consequence for anything expensive or externally visible: a rate gate cannot be
    the thing that makes it safe. Twenty concurrent presses of a proof endpoint are twenty
    gates that all open. What must be bounded is the EFFECT, not the rate —
    `forget_campaigns` before a re-send, an idempotency key derived from immutable inputs
    before an external call — which is the argument this whole project is making anyway.
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


def maintain() -> dict:
    """The periodic hygiene pass. Bounded, idempotent, and it never raises.

    Three deletes and an update, all of them O(the demo fixture) rather than O(judging):
    stale worker rows, expired experiment memories, and fixture memories that have been
    quarantined past their TTL. Nothing here scales with how many people visited.

    WHY IT IS CALLED FROM `should_autoheal` AND NOT FROM A CRON
    -----------------------------------------------------------
    There is no cron. The deployment is a serverless function that exists only while a
    request is in flight, so the only clock this system has is somebody looking at it —
    and the one periodic, already-rate-limited hook demo_state owns on that path is the
    auto-heal probe behind /api/mission. Hanging maintenance off it means hygiene runs
    when the demo is being used, which is exactly when it matters, and never when it is
    not, which is exactly when it costs nothing.

    It sits ABOVE the AUTOHEAL check on purpose: an operator who switches auto-heal off to
    protect a recording is asking not to have a worker started, not asking for quarantined
    memories to stay quarantined for a month.

    Its own gate, not the caller's, so this cannot be starved by an auto-heal that is
    rate-limited on a different schedule.
    """
    if gate('maintain', MAINTAIN_INTERVAL_S):
        return {'ran': False}

    out: dict[str, t.Any] = {'ran': True}
    try:
        out['agents'] = reap_agents()
    except Exception as e:                          # noqa: BLE001 — hygiene is optional
        log.warning('agent reap failed: %s: %s', type(e).__name__, e)

    def _memories(cur) -> dict:
        reaped = _reap_experiment_memories(cur)
        cur.execute("""
            SELECT count(*) AS n FROM axiom_memory
            WHERE tenant_id = %s AND retrieval_class = 'ACTIONABLE'
        """, (str(DEMO_TENANT),))
        low = int(cur.fetchone()['n']) < MIN_ADMISSIBLE_MEMORIES
        return {'reaped_memories': reaped,
                'released_memories': _restore_fixture_memories(cur, floor_breached=low)}

    try:
        # idempotent=True is earned, not assumed: the DELETE selects by TTL and the UPDATE
        # only ever moves rows out of quarantine, so replaying the body after an unknown
        # commit outcome converges on the same world.
        out.update(tx(_memories, idempotent=True))
    except Exception as e:                          # noqa: BLE001
        log.warning('memory hygiene failed: %s: %s', type(e).__name__, e)

    if any(out.get(k) for k in ('agents', 'reaped_memories', 'released_memories')):
        log.info('maintenance: %s', out)
    return out


def should_autoheal() -> tuple[bool, str]:
    """Decide whether the API should start a worker on its own. Explains itself.

    Every condition is a reason NOT to act, and they are all conservative, because the
    worst outcome of this feature is a worker draining the queue in the middle of a
    take that the operator is recording.
    """
    global _last_autoheal
    # Before any early return. See maintain() for why this is the call site.
    maintain()
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
