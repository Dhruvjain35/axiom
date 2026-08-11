"""AXIOM :: the Lambda invocation contract.

These tests do not re-prove that the worker is correct — tests/test_crash_windows.py and
tests/test_invariants.py do that, against the same engine this handler drives. What is
under test here is narrower and specific to the port: that wrapping a process-shaped
worker in an invocation-shaped budget did not change what it is.

Four things could have gone wrong in the wrapping, and there is a test for each:

  * the deadline is ignored, and the platform kills the worker mid-refund every time
    instead of only when we asked it to (test_the_lambda_deadline_bounds_the_run,
    test_no_budget_left_claims_nothing);
  * a warm container leaks the state that makes the NEXT invocation wrong — a stop event
    left set, a heartbeat thread frozen with a connection in its hand
    (test_a_warm_container_reuses_the_pool_without_leaking_threads);
  * the handler catches the simulated death and turns a crash into a handled error, so
    the demo silently stops demonstrating anything
    (test_a_provider_crash_is_never_swallowed);
  * the crash is not actually a crash (test_the_crash_is_real_and_the_ledger_holds).

That last one is the whole point of the port and it runs the handler in a REAL
subprocess: it asserts the process exits 9 — os._exit, no finally blocks, no flush, no
lease released — with a refund already durable in the provider's ledger, and then that
the next invocation recovers it into exactly one refund.

No AWS is involved anywhere here. The context object is a local fake, the model calls are
offline stand-ins, and the cluster is the local single node.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from axiom import db, lambda_worker, provider
from axiom.config import SHARD_COUNT, settings
from axiom.models import CLAIMABLE_STATES, AttemptState, TaskState
from axiom.provider import ProviderCrash

from conftest import query

ROOT = Path(__file__).resolve().parents[1]
HANDLER_PATH = ROOT / 'deploy' / 'lambda' / 'handler_worker.py'


def _load_handler():
    """Import the handler the way only a test has to.

    In the deployed ZIP this file sits at the root and is imported as `handler_worker`.
    From the repo it lives in deploy/lambda/, which cannot be a package on any Python:
    `import deploy.lambda.handler_worker` is a SyntaxError because `lambda` is a keyword.
    Loading by path is exactly why the build copies the file up to the ZIP root instead
    of shipping the directory, so this awkwardness is the test paying for a deployment
    property rather than a smell in the module.
    """
    spec = importlib.util.spec_from_file_location('handler_worker', HANDLER_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules['handler_worker'] = mod
    spec.loader.exec_module(mod)
    return mod


handler_worker = _load_handler()


class FakeContext:
    """The three members of a Lambda context object this code touches.

    The clock really runs: get_remaining_time_in_millis() decays exactly as the platform's
    does, so a test that asserts on the margin left at exit is asserting on arithmetic
    that had a chance to be wrong.
    """

    def __init__(self, remaining_ms: int):
        self._deadline = time.monotonic() + remaining_ms / 1000.0
        self.aws_request_id = f'test-{uuid.uuid4().hex[:12]}'
        self.log_stream_name = f'2026/08/11/[$LATEST]{uuid.uuid4().hex}'

    def get_remaining_time_in_millis(self) -> int:
        return max(0, int((self._deadline - time.monotonic()) * 1000))


@pytest.fixture(autouse=True)
def _clean_lambda_agents():
    """Delete the agent rows these invocations registered.

    conftest's _destroy_world only knows about agents a test created through World, and
    the handler registers its own. Leaving them is harmless to correctness — they are
    DEAD with a stale heartbeat — but the suite's _exclusive_queue guard reads that table
    to decide whether a real worker is running, and a suite that litters it is a suite
    that eventually confuses its own guard.
    """
    yield
    def _wipe(cur):
        cur.execute("SELECT id FROM axiom_agent WHERE worker_ref LIKE 'lambda-%'")
        ids = [str(r['id']) for r in cur.fetchall()]
        if not ids:
            return
        cur.execute('DELETE FROM axiom_event WHERE subject_type = %s '
                    'AND subject_id = ANY(%s::UUID[])', ('agent', ids))
        cur.execute('DELETE FROM axiom_agent WHERE id = ANY(%s::UUID[])', (ids,))
    db.tx(_wipe)


@pytest.fixture
def idle_shard() -> int:
    """A shard with nothing claimable in it, so a drain there provably does no work.

    claim() is not tenant-scoped by design (workers are shared infrastructure), so a
    timing test that ran against a shard holding somebody's queue would be measuring the
    queue, not the deadline.
    """
    states = ', '.join(f"'{s}'" for s in CLAIMABLE_STATES)
    busy = {r['shard'] for r in query(f"""
        SELECT DISTINCT shard FROM axiom_task
        WHERE available_at <= now() AND attempt < max_attempts AND state IN ({states})
    """)}
    free = sorted(set(range(SHARD_COUNT)) - busy)
    if not free:
        pytest.skip('every shard holds claimable work; this test needs an empty one')
    return free[0]


# ============================================================== the budget arithmetic

@pytest.mark.parametrize('requested, remaining_ms, expected', [
    (45.0, 60_000, 45.0),      # the request is the binding constraint
    (45.0, 20_000, 14.0),      # the deadline is: 20s left, 6s margin
    (45.0, 6_500, 0.5),        # only just enough to be worth starting
    (45.0, 6_000, 0.0),        # exactly the margin: nothing left to spend
    (45.0, 1_000, 0.0),        # already past it; never negative
    (45.0, None, 45.0),        # no context at all (a local run): trust the request
])
def test_budget_never_spends_the_margin(requested, remaining_ms, expected):
    """The margin is what pays for the task already in flight.

    Overrunning is not a correctness failure — a timeout kill mid-refund is window W4 and
    recovery handles it — but it converts a routine invocation into work the next one has
    to clean up, so it must only ever happen because chaos asked for it.
    """
    got = lambda_worker.budget_seconds(
        requested=requested,
        remaining_ms=None if remaining_ms is None else (lambda: remaining_ms),
        margin_ms=6_000)
    assert got == pytest.approx(expected, abs=0.01)


# ======================================================================== normal work

def test_drain_settles_the_queue_and_reports_what_it_did(world):
    jobs = [world.enqueue(amount_cents=5000 + i) for i in range(3)]

    out = handler_worker.handler(
        {'mode': 'drain', 'seconds': 30, 'shards': sorted(world.shards)},
        FakeContext(60_000))

    assert out['mode'] == 'drain'
    assert out['stopped_by'] == 'idle', 'ran out of time instead of out of work'
    assert out['crashes'] == 0
    # `>=`, not `==`: claim() is deliberately not tenant-scoped, so a drain restricted to
    # this world's shards may also pick up another tenant's claimable work. The assertion
    # that matters is the one below, about THIS world's tasks and their ledger rows.
    assert out['tasks'] >= len(jobs)
    assert out['elapsed_ms'] > 0
    assert out['remaining_ms'] < 60_000

    for j in jobs:
        assert world.task_row(j.id)['state'] == str(TaskState.SUCCEEDED)
        assert len(provider.ledger(j.order_ref)) == 1


# =========================================================================== deadline

def test_the_lambda_deadline_bounds_the_run(idle_shard):
    """45 seconds were requested; 8 were left. The 8 win, minus the margin.

    idle_exit is off on purpose: without it the empty queue would end the invocation on
    its own after ~1.2s and the test would pass without the deadline doing anything.
    """
    ctx = FakeContext(8_000)
    t0 = time.monotonic()
    out = handler_worker.handler(
        {'mode': 'drain', 'seconds': 45, 'shards': [idle_shard], 'idle_exit': False}, ctx)
    elapsed = time.monotonic() - t0

    assert out['budget_seconds'] == pytest.approx(2.0, abs=0.2)
    assert out['stopped_by'] == 'deadline'
    assert 1.5 < elapsed < 6.0, f'ran for {elapsed:.1f}s on a 2s budget'
    # The margin is still on the clock at exit — which is the whole property: the
    # platform never had to kill this invocation.
    assert out['remaining_ms'] > 5_000


def test_no_budget_left_claims_nothing(world):
    """Less time left than the margin: do not claim, do not register, just leave.

    Claiming here would not corrupt anything — a task claimed and abandoned is window W1,
    the one window in which no effect can exist — but it would guarantee the timeout kill
    lands mid-task, and every crash in this system is supposed to be one we chose.
    """
    job = world.enqueue()

    out = handler_worker.handler(
        {'mode': 'drain', 'seconds': 45, 'shards': sorted(world.shards)},
        FakeContext(3_000))

    assert out['budget_seconds'] == 0.0
    assert out['stopped_by'] == 'no_budget'
    assert out['tasks'] == 0
    assert out['elapsed_ms'] < 500
    assert world.task_row(job.id)['state'] == str(TaskState.READY)
    assert query('SELECT id FROM axiom_agent WHERE worker_ref = %s',
                 (out['worker_ref'],)) == [], 'it registered an agent it never used'


# ==================================================================== warm containers

def test_a_warm_container_reuses_the_pool_without_leaking_threads(idle_shard):
    """The second invocation is the one under test.

    Between them Lambda would freeze this process. Whatever the first invocation left
    behind — worker._stop set, a heartbeat thread parked in wait(), a Timer still armed —
    is state the second one inherits. If _stop were left set, the second drain would
    return instantly having claimed nothing; if the heartbeat thread survived, it would
    wake up inside this invocation holding a pooled connection.
    """
    threads_before = threading.active_count()
    event = {'mode': 'drain', 'seconds': 3, 'shards': [idle_shard], 'idle_exit': False}

    first = handler_worker.handler(event, FakeContext(30_000))
    pool_between = db.pool()
    second = handler_worker.handler(event, FakeContext(30_000))

    assert db.pool() is pool_between, 'the pool did not survive the invocation'
    assert first['stopped_by'] == 'deadline' and second['stopped_by'] == 'deadline'
    assert second['elapsed_ms'] > 2_000, '_stop was left set by the first invocation'
    assert threading.active_count() <= threads_before, (
        'an invocation left a thread running into the next one')


def test_the_deployed_pool_is_capped_for_a_burst_of_cold_starts(idle_shard):
    """Ask a clean process, because this suite physically cannot observe the cap.

    axiom.config freezes Settings at import, so handler_worker.py sets AXIOM_POOL_MAX
    before the first `import axiom.*` — and by the time pytest loads this file, conftest
    has already raised it to 24 for the contention tests and axiom.config is long
    imported. The pool that in-process tests see is the SUITE's pool, not the handler's.

    The number is load-bearing: every Lambda execution environment gets its own pool, so
    the cluster sees (connections per container) x (peak concurrency), and CockroachDB
    Cloud BASIC does not publish the ceiling it enforces.
    """
    env = {k: v for k, v in os.environ.items()
           if k not in ('AXIOM_POOL_MAX', 'AXIOM_POOL_MIN')}
    proc = subprocess.run(
        [sys.executable, str(HANDLER_PATH),
         json.dumps({'mode': 'drain', 'seconds': 3, 'shards': [idle_shard]})],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=90)

    assert proc.returncode == 0, f'stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}'
    out = json.loads(proc.stdout[proc.stdout.index('{'):])
    # Two threads touch this pool and only two: the claim loop and the heartbeat.
    assert out['pool']['max_size'] == 2
    assert out['pool']['pool_size'] <= out['pool']['max_size']


# ======================================================================== the crash

def test_a_provider_crash_is_never_swallowed(monkeypatch):
    """A simulated death must pass through every frame this port added.

    provider.ProviderCrash inherits BaseException so that no `except Exception` can
    absorb it and turn a correctness demo into a false pass. The handler has exactly one
    such clause (it logs and re-raises real failures), and this asserts that clause did
    not become the thing that quietly ends the chaos demo.

    Also asserts the chaos rate is put back: a 1.0 left on the frozen settings object
    would arm every subsequent test in this process.
    """
    class DyingWorker:
        def __init__(self, shards=None, worker_ref=None):
            self._hb = None

        def start(self):
            pass

        def run(self, max_tasks=None, idle_exit=False):
            raise ProviderCrash('CHAOS: died after the refund landed, before settle (W4)')

        def stop(self):
            pass

    monkeypatch.setattr(lambda_worker, 'Worker', DyingWorker)

    with pytest.raises(ProviderCrash):
        handler_worker.handler({'mode': 'chaos', 'seconds': 5, 'chaos_post': 1.0},
                               FakeContext(30_000))

    assert settings.chaos_crash_after_dispatch == 0.0
    assert settings.chaos_crash_before_dispatch == 0.0


def test_the_crash_is_real_and_the_ledger_holds(world):
    """Kill the worker at the worst instant there is, then count the refunds.

    The handler runs in a real subprocess and takes a real os._exit(9): no finally block,
    no atexit hook, no pool close, no lease release, no settle. Exit code 9 is the
    assertion that it happened — a caught exception would exit 1, a clean drain 0.

    On Lambda this is the invocation that returns no response at all and shows up as
    "Runtime exited with error: exit status 9". What is left behind is window W4: a
    committed receipt in DISPATCHED and a refund that is already real. The second half of
    this test is the next invocation finding it and NOT refunding again.
    """
    job = world.enqueue(amount_cents=7700)

    proc = subprocess.run(
        [sys.executable, str(HANDLER_PATH), json.dumps(
            {'mode': 'chaos', 'seconds': 20, 'chaos_post': 1.0,
             'shards': [job.shard], 'max_tasks': 1})],
        cwd=str(ROOT), env=dict(os.environ),
        capture_output=True, text=True, timeout=120)

    assert proc.returncode == 9, (
        'expected os._exit(9) from worker.run()\n'
        f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}')
    assert 'CHAOS' in proc.stdout
    assert '"budget_seconds"' not in proc.stdout, (
        'a crashed invocation must return no summary at all')

    # --- the state a crash at W4 leaves behind ---------------------------------------
    assert world.task_row(job.id)['state'] == str(TaskState.ACTION_PREPARED)
    receipts = world.receipts(job.id)
    assert len(receipts) == 1
    assert receipts[0]['attempt_state'] == str(AttemptState.DISPATCHED)
    assert len(provider.ledger(job.order_ref)) == 1, 'the refund really did land'

    # --- what the next invocation does with it ---------------------------------------
    world.lease_expires()
    out = handler_worker.handler(
        {'mode': 'drain', 'seconds': 30, 'shards': [job.shard]}, FakeContext(60_000))

    assert out['tasks'] >= 1
    assert world.task_row(job.id)['state'] == str(TaskState.SUCCEEDED)
    ledger = provider.ledger(job.order_ref)
    assert len(ledger) == 1, f'{len(ledger)} refunds for one order: {ledger}'
    assert ledger[0]['replay_count'] == 1, (
        'the recovery re-sent under the same derived key and the provider absorbed it; '
        'a replay_count of 0 would mean recovery never actually re-sent')


# ============================================================================== modes

def test_seed_mode_routes_to_the_seeder(monkeypatch):
    """Routing only — this deliberately does not seed for real.

    A real seed inside the invariant suite would DELETE the demo tenant, wipe the shared
    provider ledger every other test is asserting against, and refill the queue with 30
    claimable tasks that every later test's claim() would pick up. The seeder itself is
    exercised for real by scripts/chaos_demo.py and by the demo.
    """
    seen: dict = {}
    monkeypatch.setattr(handler_worker.seed_mod, 'reset',
                        lambda: seen.__setitem__('reset', True))
    monkeypatch.setattr(handler_worker.seed_mod, 'seed',
                        lambda **kw: seen.update(kw) or {
                            'tenant_id': 'tenant', 'mission_id': 'mission',
                            'tasks': kw['n_tasks'], 'memories': 10})

    out = handler_worker.handler({'mode': 'seed', 'tasks': 7, 'reset': True},
                                 FakeContext(30_000))

    assert seen['reset'] is True
    assert seen['n_tasks'] == 7
    assert out == {'mode': 'seed', 'reset': True, 'elapsed_ms': out['elapsed_ms'],
                   'tenant_id': 'tenant', 'mission_id': 'mission', 'tasks': 7,
                   'memories': 10}


def test_an_unknown_mode_fails_the_invocation():
    """Better a failed invocation than a silent no-op that reads as a drained queue."""
    with pytest.raises(ValueError, match='unknown mode'):
        handler_worker.handler({'mode': 'drian'}, FakeContext(30_000))
