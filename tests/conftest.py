"""AXIOM :: the invariant suite's harness.

This suite does not assert that AXIOM works. It assembles the exact conditions under
which the design would corrupt state — a lease that expired mid-refund, two workers
holding the same fence, a recovered agent that re-synthesized a different request body,
six threads racing a budget that funds three — and asserts that the system refuses.

A test that only proves the happy path is worth nothing here: the happy path is what
every agent framework already does correctly. The value is entirely in the tests that
try to cause a double refund and fail to.

Three harness decisions worth explaining
----------------------------------------
1. **Environment is set before `axiom.config` is imported.** `settings` is a frozen
   dataclass built at import time, so `AXIOM_LEASE_SECONDS=1` has to be in os.environ
   before the first `import axiom.*` anywhere in the process. That is why the env block
   below sits above the imports and why `_harness_matches_env` asserts it took.

2. **Leases really expire.** Tests sleep past a one-second lease rather than reaching
   into SQL to fake an expiry, because the thing under test is the interaction between
   `available_at <= now()` and `lease_epoch`, and a hand-edited row would test neither.

3. **Every test gets a random tenant.** `claim()` is deliberately NOT tenant-scoped —
   workers are shared infrastructure that pull from every tenant's queue — so tests run
   against a cluster that also holds demo data would otherwise claim the demo's tasks.
   Each test's tasks are backdated so they sort first, and `World.claim` drops anything
   belonging to another tenant. Dropping a foreign claim is safe by construction: it is
   crash window W1, which is the one window in which no effect can exist.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------- env, before imports
os.environ.setdefault('DATABASE_URL',
                      'postgresql://root@localhost:26257/axiom?sslmode=disable')
os.environ['AXIOM_OFFLINE'] = '1'            # deterministic embeddings, rule-based triage
os.environ['AXIOM_LEASE_SECONDS'] = '1'      # so "wait for the lease to expire" costs 1.3s
os.environ['AXIOM_HEARTBEAT_SECONDS'] = '3600'   # no fixture may silently renew a lease
os.environ['AXIOM_PROVIDER_LATENCY_MS'] = '0'
os.environ['AXIOM_CHAOS_PRE'] = '0'          # crashes in this suite are explicit, never random
os.environ['AXIOM_CHAOS_POST'] = '0'
os.environ.setdefault('AXIOM_POOL_MAX', '24')    # the contention tests run 6 threads

import copy                                                            # noqa: E402
import time                                                            # noqa: E402
import uuid                                                            # noqa: E402
from dataclasses import dataclass                                      # noqa: E402
from typing import Any, Callable, Sequence                             # noqa: E402

import pytest                                                          # noqa: E402

from axiom import db, embeddings, memory, policy as policy_mod, provider, tasks  # noqa: E402
from axiom.config import SYSTEM_TENANT, settings                       # noqa: E402
from axiom.models import (                                              # noqa: E402
    AttemptState, MemoryClass, Outcome, RetrievalClass, TaskState, Trust,
)

STEP = 'refund'
POLICY_ID = 'refund_authority'


def query(sql: str, params: Sequence[Any] = ()) -> list[dict]:
    """One read-only fetch, outside any World. For assertions about the cluster itself."""
    def _q(cur):
        cur.execute(sql, tuple(params))
        return cur.fetchall()
    return db.tx(_q, readonly=True)


# ============================================================================ guards

@pytest.fixture(scope='session', autouse=True)
def _harness_matches_env():
    """Fail the whole run rather than let a mis-timed import silently lengthen the lease.

    If some module imported axiom.config before the block at the top of this file ran,
    lease_seconds is 20 and every crash-window test would sleep 20s or, worse, race.
    """
    assert settings.offline, 'AXIOM_OFFLINE did not take: tests must not call Bedrock'
    assert settings.lease_seconds == 1, (
        f'AXIOM_LEASE_SECONDS did not take (lease_seconds={settings.lease_seconds}); '
        'axiom.config was imported before conftest set the environment')
    assert settings.provider_latency_ms == 0
    yield
    db.close_pool()
    provider.close_pool()


@pytest.fixture(scope='session', autouse=True)
def _exclusive_queue(_harness_matches_env):
    """Refuse to run while a real worker is alive against the same cluster.

    `claim()` is not tenant-scoped by design, so a live `python -m axiom.worker` will
    happily claim this suite's tasks, refund them, and leave a crash-window test asserting
    against a row some other process already settled. That failure looks like a broken
    invariant and is not one, so detect it up front and say so.
    """
    intruders = query("""
        SELECT worker_ref, status, heartbeat_at FROM axiom_agent
        WHERE status IN ('STARTING', 'ALIVE')
          AND heartbeat_at > now() - INTERVAL '30 seconds'
        ORDER BY heartbeat_at DESC
    """)
    if intruders:
        refs = ', '.join(r['worker_ref'] for r in intruders)
        pytest.exit(
            f'another AXIOM worker is alive on this cluster ({refs}). The invariant '
            'suite needs exclusive use of the queue — stop the workers and re-run.',
            returncode=2)
    yield


@pytest.fixture(scope='session', autouse=True)
def _clean_provider(_exclusive_queue):
    """Wipe the external ledger once per session.

    The provider is a SEPARATE database with no tenant column — a real payments API does
    not know about our tenancy model, which is the entire reason idempotency keys have to
    carry the identity. `duplicate_check()` is therefore global, and it is only a
    meaningful assertion if the session starts from an empty ledger.
    """
    provider.reset()
    yield


# ============================================================================== world

@dataclass
class Job:
    """One enqueued refund task, plus the request body a worker would synthesize for it."""
    id: uuid.UUID
    order_ref: str
    amount_cents: int
    dedupe_key: str
    shard: int

    @property
    def body(self) -> dict:
        return {'order_ref': self.order_ref, 'amount_cents': self.amount_cents,
                'currency': 'USD', 'reason': 'duplicate_charge'}


class World:
    """One tenant, one mission, one policy — and the verbs a worker uses against them.

    Deliberately thin. Tests call the engine functions directly through `db.tx` so that
    each test reads as a specification of the protocol rather than of this helper.
    """

    def __init__(self, tenant_id: uuid.UUID, mission_id: uuid.UUID,
                 budget_cents: int, policy_max_cents: int):
        self.tenant_id = tenant_id
        self.mission_id = mission_id
        self.budget_cents = budget_cents
        self.policy_max_cents = policy_max_cents
        self.shards: set[int] = set()
        self.agent_ids: list[uuid.UUID] = []

    # ------------------------------------------------------------------- construction

    def agent(self, ref: str | None = None) -> uuid.UUID:
        aid = db.tx(lambda cur: tasks.register_agent(
            cur, worker_ref=ref or f'test-{uuid.uuid4().hex[:10]}', shards=[]))
        self.agent_ids.append(aid)
        return aid

    def enqueue_id(self, *, dedupe_key: str, order_ref: str, amount_cents: int,
                   max_attempts: int = 5) -> uuid.UUID | None:
        """Raw enqueue. Returns None when the dedupe index rejected a duplicate."""
        def _apply(cur):
            tid = tasks.enqueue(
                cur, tenant_id=self.tenant_id, mission_id=self.mission_id,
                task_type='refund', dedupe_key=dedupe_key,
                payload={'order_ref': order_ref, 'amount_cents': amount_cents,
                         'description': 'customer charged twice for order',
                         'exception_kind': 'duplicate_charge'},
                max_attempts=max_attempts, actor='system:test')
            if tid is None:
                return None
            # Backdate so this row is unambiguously the oldest claimable one in its shard.
            # claim() orders by available_at and is not tenant-scoped; without this a test
            # run against a cluster that also holds seeded demo data would spend its time
            # claiming the demo's queue.
            cur.execute("UPDATE axiom_task SET available_at = now() - INTERVAL '30 days' "
                        "WHERE id = %s", (str(tid),))
            return tid
        return db.tx(_apply)

    def enqueue(self, *, amount_cents: int = 5000, order_ref: str | None = None,
                max_attempts: int = 5) -> Job:
        order_ref = order_ref or f'ORD-{uuid.uuid4().hex[:12].upper()}'
        dedupe_key = f'order:{order_ref}:refund'
        tid = self.enqueue_id(dedupe_key=dedupe_key, order_ref=order_ref,
                              amount_cents=amount_cents, max_attempts=max_attempts)
        assert tid is not None, 'fixture enqueue was deduped; order_ref collision'
        shard = self.scalar('SELECT shard FROM axiom_task WHERE id = %s', (str(tid),))
        self.shards.add(int(shard))
        return Job(id=tid, order_ref=order_ref, amount_cents=amount_cents,
                   dedupe_key=dedupe_key, shard=int(shard))

    # -------------------------------------------------------------------- the protocol

    def claim(self, agent_id: uuid.UUID, *, want: uuid.UUID | None = None,
              tries: int = 200) -> tasks.Claimed:
        """Claim until this tenant's task comes back. Foreign claims are abandoned.

        Abandoning a claim is not a hack around isolation, it is window W1: the fence was
        bumped and nothing else happened, so the row is claimable again the moment its
        lease lapses and no effect can exist. The suite relies on that being true, which
        is itself asserted by test_w1_*.
        """
        deadline = time.time() + 30.0
        for _ in range(tries):
            c = db.tx(lambda cur: tasks.claim(
                cur, agent_id=agent_id, shards=sorted(self.shards) or None))
            if c is None:
                if time.time() > deadline:
                    break
                time.sleep(0.02)
                continue
            if c.tenant_id != self.tenant_id:
                continue
            if want is None or c.id == want:
                return c
        owner = self.rows('SELECT state, lease_owner, lease_epoch, available_at '
                          'FROM axiom_task WHERE id = %s', (str(want),)) if want else []
        raise AssertionError(
            f'could not claim the expected task within 30s; row={owner}. If lease_owner '
            'is an agent this suite did not create, a real worker is running against '
            'this cluster and stealing the queue.')

    def prepare(self, claimed: tasks.Claimed, agent_id: uuid.UUID, job: Job,
                *, step: str = STEP,
                licensed_by: uuid.UUID | None = None) -> tasks.PrepareResult:
        return db.tx(lambda cur: tasks.prepare(
            cur, task=claimed, agent_id=agent_id, step_name=step,
            provider_name='payments', operation='refunds.create',
            request_body=job.body, amount_cents=job.amount_cents, policy_id=POLICY_ID,
            licensed_by_memory_id=licensed_by))

    def recover(self, claimed: tasks.Claimed, agent_id: uuid.UUID,
                *, step: str = STEP, situation: str = 'duplicate_charge: customer charged twice'):
        return db.tx(lambda cur: tasks.recover(
            cur, task=claimed, agent_id=agent_id, step_name=step,
            situation_embedding=embeddings.embed_list(situation)))

    def settle(self, claimed: tasks.Claimed, agent_id: uuid.UUID,
               receipt: tasks.Receipt, result: provider.ProviderResult,
               *, content: str | None = None) -> uuid.UUID:
        content = content or (
            f'refund {result.provider_ref} settled under key {receipt.idempotency_key}; '
            f'replayed={result.replayed}')
        return db.tx(lambda cur: tasks.settle(
            cur, task=claimed, agent_id=agent_id, receipt=receipt,
            outcome_state=AttemptState.SUCCEEDED, task_state=TaskState.SUCCEEDED,
            response_body=result.body, provider_ref=result.provider_ref,
            http_status=result.status, memory_content=content,
            memory_embedding=embeddings.embed_list(content),
            memory_outcome=Outcome.RESOLVED,
            result={'provider_ref': result.provider_ref, 'replayed': result.replayed}))

    @staticmethod
    def lease_expires() -> None:
        """Wait out the lease the way a dead worker's peers do: by doing nothing."""
        time.sleep(settings.lease_seconds + 0.35)

    # ------------------------------------------------------------------------ reading

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        def _q(cur):
            cur.execute(sql, tuple(params))
            row = cur.fetchone()
            return None if row is None else next(iter(row.values()))
        return db.tx(_q, readonly=True)

    def rows(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        return query(sql, params)

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Run one raw statement and return its rowcount.

        Used by the tests that go around the engine on purpose — asserting that the fence
        lives in the WHERE clause and the uniqueness lives in the index, not merely in a
        Python guard that a future refactor could delete.
        """
        def _q(cur):
            cur.execute(sql, tuple(params))
            return cur.rowcount
        return db.tx(_q)

    def task_row(self, task_id: uuid.UUID) -> dict:
        return self.rows('SELECT * FROM axiom_task WHERE id = %s', (str(task_id),))[0]

    def receipts(self, task_id: uuid.UUID) -> list[dict]:
        return self.rows(
            'SELECT * FROM axiom_action_attempt WHERE task_id = %s ORDER BY step_seq',
            (str(task_id),))

    def live_receipt(self, task_id: uuid.UUID, step: str = STEP) -> tasks.Receipt | None:
        return db.tx(lambda cur: tasks.live_receipt(
            cur, tenant_id=self.tenant_id, task_id=task_id, step_name=step))

    def spent(self) -> tuple[int, int]:
        r = self.rows('SELECT spent_cents, budget_cents FROM axiom_mission WHERE id = %s',
                      (str(self.mission_id),))[0]
        return int(r['spent_cents']), int(r['budget_cents'])

    def events(self, task_id: uuid.UUID) -> list[str]:
        """This task's journal, in gap-free sequence order.

        Scoped to subject_type='task' because the settle transaction also journals the
        memory it wrote, under its own subject and its own sequence.
        """
        return [r['event_type'] for r in self.rows(
            "SELECT event_type FROM axiom_event WHERE tenant_id = %s AND subject_type = 'task' "
            "AND subject_id = %s ORDER BY seq", (str(self.tenant_id), str(task_id)))]

    def remember(self, content: str, *, memory_class: MemoryClass = MemoryClass.EPISODIC,
                 context_key: str = 'state:ACTION_PREPARED',
                 outcome: Outcome = Outcome.RESOLVED,
                 trust_level: int = Trust.FIRST_PARTY,
                 supersedes: uuid.UUID | None = None) -> uuid.UUID:
        vec = embeddings.embed_list(content)
        return db.tx(lambda cur: memory.write(
            cur, tenant_id=self.tenant_id, memory_class=memory_class,
            context_key=context_key, content=content, embedding=vec, outcome=outcome,
            source='system:execution', trust_level=trust_level, supersedes=supersedes,
            actor='system:test'))

    def recall(self, query: str, *, memory_class: MemoryClass = MemoryClass.EPISODIC,
               context_key: str | None = 'state:ACTION_PREPARED',
               retrieval_class: RetrievalClass = RetrievalClass.ACTIONABLE,
               k: int = 10) -> list[memory.Recalled]:
        vec = embeddings.embed_list(query)
        return db.tx(lambda cur: memory.recall(
            cur, tenant_id=self.tenant_id, embedding=vec, memory_class=memory_class,
            context_key=context_key, retrieval_class=retrieval_class, k=k), readonly=True)


# =========================================================================== fixtures

def _create_world(budget_cents: int, policy_max_cents: int,
                  requires_approval: bool) -> World:
    tenant_id = uuid.uuid4()

    def _apply(cur):
        cur.execute("INSERT INTO axiom_tenant (id, slug, display_name) VALUES (%s, %s, %s)",
                    (str(tenant_id), f'test-{tenant_id.hex[:12]}', 'invariant suite'))
        policy_mod.publish(
            cur, tenant_id=tenant_id, policy_id=POLICY_ID, version=1,
            body={'description': 'test refund authority',
                  'max_auto_action_cents': policy_max_cents},
            max_auto_action_cents=policy_max_cents, requires_approval=requires_approval,
            created_by='human:test@axiom.invalid', activate=True)
        return tasks.create_mission(
            cur, tenant_id=tenant_id, title='invariant suite',
            goal='try to break every invariant in the crash-window table',
            budget_cents=budget_cents, created_by='human:test@axiom.invalid')

    mission_id = db.tx(_apply)
    return World(tenant_id, mission_id, budget_cents, policy_max_cents)


def _destroy_world(w: World) -> None:
    """Delete everything this test created, in dependency order.

    Memory's self-references (supersedes / superseded_by) are unlinked first: a single
    DELETE would have to satisfy both sides of a self-FK in one statement, and the
    supersession tests deliberately leave those chains populated.
    """
    def _wipe(cur):
        t = (str(w.tenant_id),)
        cur.execute('DELETE FROM axiom_event WHERE tenant_id = %s', t)
        cur.execute('UPDATE axiom_memory SET supersedes = NULL, superseded_by = NULL, '
                    'superseded_at = NULL WHERE tenant_id = %s', t)
        cur.execute('UPDATE axiom_action_attempt SET licensed_by_memory_id = NULL '
                    'WHERE tenant_id = %s', t)
        cur.execute('DELETE FROM axiom_approval WHERE tenant_id = %s', t)
        cur.execute('DELETE FROM axiom_action_attempt WHERE tenant_id = %s', t)
        cur.execute('DELETE FROM axiom_memory WHERE tenant_id = %s', t)
        cur.execute('DELETE FROM axiom_task WHERE tenant_id = %s', t)
        cur.execute('DELETE FROM axiom_mission WHERE tenant_id = %s', t)
        cur.execute('DELETE FROM axiom_policy WHERE tenant_id = %s', t)
        cur.execute('DELETE FROM axiom_tenant WHERE id = %s', t)
        if w.agent_ids:
            ids = [str(a) for a in w.agent_ids]
            cur.execute('DELETE FROM axiom_event WHERE tenant_id = %s AND subject_id = ANY(%s::UUID[])',
                        (str(SYSTEM_TENANT), ids))
            cur.execute('DELETE FROM axiom_agent WHERE id = ANY(%s::UUID[])', (ids,))
    db.tx(_wipe)


@pytest.fixture
def world() -> World:
    """A tenant whose policy authorizes anything: the acting path, uninterrupted."""
    w = _create_world(budget_cents=10_000_00, policy_max_cents=10_000_00,
                      requires_approval=False)
    try:
        yield w
    finally:
        _destroy_world(w)


@pytest.fixture
def strict_world() -> World:
    """A tenant whose policy ceiling is $50, so anything larger parks on a human."""
    w = _create_world(budget_cents=10_000_00, policy_max_cents=5_000,
                      requires_approval=False)
    try:
        yield w
    finally:
        _destroy_world(w)


@pytest.fixture
def world_factory():
    """For the cross-tenant tests, which need two tenants alive at the same time."""
    made: list[World] = []

    def _make(budget_cents: int = 10_000_00, policy_max_cents: int = 10_000_00) -> World:
        w = _create_world(budget_cents, policy_max_cents, requires_approval=False)
        made.append(w)
        return w

    try:
        yield _make
    finally:
        for w in reversed(made):
            _destroy_world(w)


# ============================================================================ helpers

def dispatch(receipt: tasks.Receipt, *, body: dict | None = None) -> provider.ProviderResult:
    """Exactly what the worker does to the outside world, and nothing else.

    `body` defaults to the receipt's stored request body. Overriding it is how the W7
    tests forge a different intent under an existing key.
    """
    return provider.create_refund(
        idempotency_key=receipt.idempotency_key,
        order_ref=receipt.request_body['order_ref'],
        amount_cents=receipt.amount_cents or 0,
        currency=receipt.currency or 'USD',
        request_body=body if body is not None else receipt.request_body,
        latency_ms=0, chaos_pre=0.0, chaos_post=0.0)


def race(fns: Sequence[Callable[[], Any]]) -> list[tuple[str, Any]]:
    """Run callables on real threads released from one barrier; collect outcome-or-exception.

    Threads, not asyncio: psycopg is synchronous and db.tx() does its own 40001 retry with
    jitter, so real OS threads reproduce the contention the production worker pool
    generates. A cooperative loop would serialize the very statements under test.
    """
    import threading

    barrier = threading.Barrier(len(fns))
    out: list[tuple[str, Any]] = []
    lock = threading.Lock()

    def _run(fn: Callable[[], Any]) -> None:
        barrier.wait()
        try:
            r = ('ok', fn())
        except BaseException as e:                       # ProviderCrash is a BaseException
            r = ('raised', e)
        with lock:
            out.append(r)

    threads = [threading.Thread(target=_run, args=(f,)) for f in fns]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert len(out) == len(fns), 'a racing thread never finished'
    return out


def clone(claimed: tasks.Claimed) -> tasks.Claimed:
    """A second executor's view of the SAME claim (same fence, same epoch).

    prepare() mutates the Claimed it is handed (it pins the policy version and advances
    the state), so a race needs two objects or the threads corrupt each other's input
    rather than racing the database.
    """
    return copy.copy(claimed)
