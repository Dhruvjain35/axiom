"""AXIOM :: the demo cannot be broken by absence, by a crawler, or by a month.

Every other file in this suite asserts that the ENGINE is correct under crashes. This
one asserts that the DEMO is still standing after four weeks of unattended judging —
which is a different property, with different failure modes, and it is the one the
project loses on if it is wrong. A judge who opens the URL in week three and sees an
empty grid does not read the schema comments.

Each test here corresponds to a defect that was observed, not imagined:

  * the deployed dashboard showing a one-task "Counterexample" mission instead of the
    30-task demo, because mission selection meant "newest"
  * /api/mission answering 404 on an empty tenant, which latches the poll lamp into the
    alarm colour that the design reserves for genuine failure
  * RESET emptying the world with no way back
  * the worker rail growing one row per click, forever
  * a pooled connection that CockroachDB had already closed raising an error class the
    first version of the retry did not catch
  * the headline DUPLICATE REFUNDS being computed over a global ledger that another
    script deliberately puts duplicates into
"""

from __future__ import annotations

import json
import threading
import time
import uuid

import psycopg
import pytest
from psycopg.rows import dict_row
from starlette.responses import Response

from axiom import api, db, demo_state, provider, seed, tasks
from axiom.config import SYSTEM_TENANT
from axiom.demo_state import Unavailable
from axiom.seed import DEMO_TENANT

from conftest import query

DEMO = DEMO_TENANT


def route(fn, **kw):
    """Call a route function directly and normalise (status, body).

    There is no TestClient here on purpose: httpx is not a dependency of this project
    (requirements.txt ships psycopg, boto3, fastapi and uvicorn and nothing else), and
    adding a test-only HTTP client to prove that a plain function returns a dict would
    be a strange trade. The handlers are ordinary `def`s — rule 2 of axiom/api.py — so
    calling them is calling exactly the code the server calls.
    """
    out = fn(**kw)
    if isinstance(out, Response):
        return out.status_code, json.loads(out.body)
    return 200, out


@pytest.fixture(scope='module', autouse=True)
def _leave_the_demo_seeded():
    """Whatever these tests do to the demo tenant, put it back afterwards.

    Several tests here wipe the demo world on purpose. Leaving it wiped would be
    exactly the failure the module is about, and the local cluster is shared with
    whoever is demoing next.
    """
    demo_state.tune_pools()
    yield
    demo_state.invalidate()
    demo_state.ensure_demo()


@pytest.fixture(autouse=True)
def _fresh_gates():
    """Rate gates are process-global. No test may inherit another's countdown."""
    demo_state.reset_gates()
    demo_state.invalidate()
    yield


# ================================================================ mission selection

def test_the_demo_mission_survives_a_one_task_scratch_mission():
    """The counterexample must not be able to take over the screen by being newest.

    scripts/counterexample.py creates a mission on DEMO_TENANT with exactly one task,
    every time it runs. Selection by `created_at DESC` handed it the dashboard, and the
    production cluster was serving "Counterexample / one refund, one crash" with a
    1-tile grid in a 30-tile frame.
    """
    demo_state.ensure_demo()
    before = demo_state.tx(lambda cur: demo_state.select_mission_id(cur, DEMO),
                           readonly=True)
    assert before is not None

    def _scratch(cur):
        mid = tasks.create_mission(cur, tenant_id=DEMO, title='Counterexample',
                                   goal='one refund, one crash', budget_cents=100000,
                                   created_by='test')
        tasks.enqueue(cur, tenant_id=DEMO, mission_id=mid, task_type='refund',
                      dedupe_key=f'ce-scratch-{uuid.uuid4().hex[:8]}',
                      payload={'order_ref': f'CE-{uuid.uuid4().hex[:6]}',
                               'amount_cents': 30000},
                      actor='test')
        return mid

    scratch = demo_state.tx(_scratch)
    try:
        after = demo_state.tx(lambda cur: demo_state.select_mission_id(cur, DEMO),
                              readonly=True)
        assert after == before, 'a 1-task mission took over the dashboard'

        status, body = route(api.mission, tenant_id=DEMO)
        assert status == 200
        assert body['id'] == str(before)
        assert body['title'] != 'Counterexample'
    finally:
        demo_state.tx(lambda cur: cur.execute(
            'DELETE FROM axiom_task WHERE mission_id = %s', (str(scratch),)))
        demo_state.tx(lambda cur: cur.execute(
            'DELETE FROM axiom_event WHERE mission_id = %s', (str(scratch),)))
        demo_state.tx(lambda cur: cur.execute(
            'DELETE FROM axiom_mission WHERE id = %s', (str(scratch),)))


def test_a_mission_with_no_tasks_is_still_better_than_nothing():
    """Tier 0 of the selection: an empty mission beats a blank screen."""
    empty = uuid.uuid4()
    tenant = uuid.uuid4()

    def _make(cur):
        cur.execute("INSERT INTO axiom_tenant (id, slug, display_name) "
                    "VALUES (%s, %s, 'selection test')",
                    (str(tenant), f'sel-{tenant.hex[:8]}'))
        return tasks.create_mission(cur, tenant_id=tenant, title='shell', goal='none',
                                    budget_cents=0, created_by='test')

    mid = demo_state.tx(_make)
    try:
        got = demo_state.tx(lambda cur: demo_state.select_mission_id(cur, tenant),
                            readonly=True)
        assert got == mid
    finally:
        demo_state.tx(lambda cur: cur.execute(
            'DELETE FROM axiom_event WHERE tenant_id = %s', (str(tenant),)))
        demo_state.tx(lambda cur: cur.execute(
            'DELETE FROM axiom_mission WHERE id = %s', (str(mid),)))
        demo_state.tx(lambda cur: cur.execute(
            'DELETE FROM axiom_tenant WHERE id = %s', (str(tenant),)))


# ===================================================================== self-healing

def test_mission_endpoint_heals_an_empty_world_instead_of_404ing():
    """Wipe everything, then ask the way a browser asks. It must come back whole."""
    seed.reset()
    demo_state.invalidate()

    status, body = route(api.mission, tenant_id=DEMO)
    assert status == 200
    assert body.get('id'), f'no mission after a heal: {body}'
    assert body['by_state'].get('READY') == demo_state.DEMO_TASKS

    _, tasks_body = route(api.list_tasks, limit=200, tenant_id=DEMO)
    assert len(tasks_body) == demo_state.DEMO_TASKS

    counts = query(
        "SELECT (SELECT count(*) FROM axiom_memory WHERE tenant_id = %s) AS memories, "
        "(SELECT count(*) FROM axiom_policy WHERE tenant_id = %s AND status = 'ACTIVE') "
        "AS policies", (str(DEMO), str(DEMO)))
    assert counts[0]['memories'] > 0, 'healed without the prior memories the demo reads'
    assert counts[0]['policies'] == 1, 'healed without the policy that authorizes refunds'


def test_an_unknown_tenant_gets_an_empty_payload_not_a_404_and_is_never_seeded():
    """Empty must not look broken — and must not become somebody else's data.

    The dashboard keys its empty state off `m.id`, so a body with a null id renders
    "no mission — seed the demo to create one". A 404 instead increments the failure
    counter and lights the alarm colour.
    """
    stranger = uuid.uuid4()
    status, body = route(api.mission, tenant_id=stranger)
    assert status == 200
    assert body['id'] is None and body['empty'] is True
    assert body['by_state'] == {}
    assert 'no mission' in body['note']

    rows = query('SELECT count(*) AS n FROM axiom_mission WHERE tenant_id = %s',
                 (str(stranger),))
    assert rows[0]['n'] == 0, 'the API seeded a tenant that merely looked at it'


def test_concurrent_arrivals_produce_exactly_one_mission():
    """Two judges in the same second must not produce two missions.

    `use_process_lock=False` is what makes this a real test: it removes the Python lock
    so every thread reaches the DATABASE mutex — the `SELECT ... FOR UPDATE` on the
    tenant row inside the seeding transaction — which is the only thing that also works
    across processes (two Lambda containers, or a uvicorn worker pool).
    """
    seed.reset()
    demo_state.invalidate()

    out: list[dict] = []
    errs: list[BaseException] = []

    def go():
        try:
            out.append(demo_state.ensure_demo(use_process_lock=False))
        except BaseException as e:                # noqa: BLE001 — recorded, then asserted
            errs.append(e)

    threads = [threading.Thread(target=go) for _ in range(6)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=90)

    assert not errs, f'concurrent heal raised: {errs}'
    assert len(out) == 6
    assert sum(1 for o in out if o.get('seeded')) == 1, \
        f'more than one thread believed it seeded: {out}'
    assert len({str(o['mission_id']) for o in out}) == 1

    rows = query("""
        SELECT (SELECT count(*) FROM axiom_mission WHERE tenant_id = %s) AS missions,
               (SELECT count(*) FROM axiom_task    WHERE tenant_id = %s) AS tasks
    """, (str(DEMO), str(DEMO)))
    assert rows[0]['missions'] == 1
    assert rows[0]['tasks'] == demo_state.DEMO_TASKS


def test_ensure_demo_is_idempotent_and_cheap_the_second_time():
    demo_state.ensure_demo(force=True)
    demo_state.invalidate()
    again = demo_state.ensure_demo()
    assert again['seeded'] is False
    # third call inside the TTL does not even probe
    third = demo_state.ensure_demo()
    assert third == {'checked': False, 'seeded': False}


def test_reset_leaves_a_working_demo_behind():
    """RESET used to empty the world with no way back. Now it means "clean board"."""
    status, body = route(api.demo_reset, body=api.ResetBody())
    assert status == 200 and body['reseeded'] is True
    assert body['tasks'] == demo_state.DEMO_TASKS

    status, mission = route(api.mission, tenant_id=DEMO)
    assert status == 200 and mission.get('id')

    # ...and the escape hatch still empties it for scripts that ask in writing.
    demo_state.reset_gates()
    status, body = route(api.demo_reset, body=api.ResetBody(reseed=False))
    assert status == 200 and body['reseeded'] is False
    assert query('SELECT count(*) AS n FROM axiom_task WHERE tenant_id = %s',
                 (str(DEMO),))[0]['n'] == 0
    demo_state.invalidate()
    demo_state.ensure_demo()


def test_demo_constants_have_not_drifted_from_the_seed_module():
    """demo_state copies four values from seed.py. A wrong copy makes a SECOND mission.

    seed.seed() builds its world inside a nested closure that cannot be called with an
    external cursor, so the self-heal path re-implements it in one transaction. The
    price of that is this assertion.
    """
    assert demo_state.DEMO_MISSION_TITLE == "Resolve today's order exceptions"
    assert demo_state.DEMO_TASKS == 30
    assert demo_state.DEMO_BUDGET_CENTS == 2500_00
    assert demo_state.DEMO_POLICY_ID == 'refund_authority'
    # The prior-memory corpus is imported rather than copied, so it cannot drift; assert
    # the import is still wired to the same objects.
    assert len(seed.PRIOR_RECOVERIES) + len(seed.PRIOR_SEMANTIC) == 10


# ============================================================ connection resilience

def _cancel(session_ids: list[str]) -> int:
    """Cancel exactly these sessions, from a connection outside the pool.

    Scoped to session ids collected from THIS process's pool rather than to
    `application_name = 'axiom'`, because the local cluster is shared and cancelling
    every axiom session on it would reach into another process's worker.
    """
    n = 0
    with psycopg.connect(db.settings.database_url, autocommit=True) as c:
        for sid in session_ids:
            try:
                c.execute(f"CANCEL SESSION '{sid}'")
                n += 1
            except psycopg.Error:
                pass          # already gone, which is the outcome we wanted anyway
    return n


def test_a_cancelled_session_does_not_look_like_an_operational_error():
    """The measurement the retry is built on. If this changes, the retry stops working.

    CockroachDB does not report a session killed underneath a client as
    psycopg.OperationalError. It reports SQLSTATE XXUUU with a Go network error in the
    message, which psycopg maps to InternalError_ — a DatabaseError. Catching only the
    psycopg connection classes therefore does NOT catch this, and the first version of
    demo_state.tx let it through as a 503 on the first request after an idle gap.
    """
    # A standalone connection, not a pooled one: the point is to observe the error
    # class CockroachDB produces, and borrowing a pooled connection to kill it would
    # leave the pool holding a corpse for whichever test ran next.
    conn = psycopg.connect(db.settings.database_url, row_factory=dict_row)
    try:
        sid = conn.execute('SHOW session_id').fetchone()['session_id']
        conn.commit()
        _cancel([sid])
        with pytest.raises(psycopg.Error) as ei:
            for _ in range(4):
                conn.execute('SELECT 1')
                time.sleep(0.25)
    finally:
        try:
            conn.close()
        except psycopg.Error:
            pass

    e = ei.value
    assert demo_state._is_connection_failure(e), \
        f'the classifier does not recognise {type(e).__name__} / ' \
        f'{getattr(getattr(e, "diag", None), "sqlstate", None)}: {e}'


def test_the_first_request_after_every_connection_dies_still_succeeds():
    """Kill every pooled connection this process holds, twice, and keep reading."""
    demo_state.tune_pools()
    demo_state.tx(lambda cur: cur.execute('SELECT 1'), readonly=True)

    for _ in range(2):
        ids = []
        cms = []
        try:
            for _ in range(3):
                cm = db.pool().connection()
                conn = cm.__enter__()
                cms.append(cm)
                ids.append(conn.execute('SHOW session_id').fetchone()['session_id'])
        finally:
            for cm in reversed(cms):
                cm.__exit__(None, None, None)

        assert _cancel(ids) > 0
        got = demo_state.tx(lambda cur: (cur.execute('SELECT 42 AS n'),
                                         cur.fetchone())[1], readonly=True)
        assert got['n'] == 42

        status, body = route(api.mission, tenant_id=DEMO)
        assert status == 200


def test_a_write_is_never_retried_unless_it_says_it_is_idempotent(monkeypatch):
    """The safety property that makes the retry affordable.

    A connection that dies after COMMIT is sent and before the acknowledgement arrives
    may have committed. Retrying that write is a second effect — the exact failure this
    project exists to argue against — so only a caller that states its body is
    idempotent-on-replay gets more than one attempt.
    """
    calls = {'n': 0}

    class _Boom(psycopg.OperationalError):
        pass

    def _always_dead(fn, **kw):
        calls['n'] += 1
        raise _Boom('server closed the connection unexpectedly')

    monkeypatch.setattr(db, 'tx', _always_dead)

    calls['n'] = 0
    with pytest.raises(Unavailable):
        demo_state.tx(lambda cur: None)                       # a write
    assert calls['n'] == 1, 'a non-idempotent write was retried'

    calls['n'] = 0
    with pytest.raises(Unavailable):
        demo_state.tx(lambda cur: None, readonly=True)        # a read
    assert calls['n'] == demo_state.READ_ATTEMPTS

    calls['n'] = 0
    with pytest.raises(Unavailable):
        demo_state.tx(lambda cur: None, idempotent=True)      # a convergent write
    assert calls['n'] == demo_state.READ_ATTEMPTS


def test_a_real_sql_error_is_not_mistaken_for_a_dead_connection():
    """Retrying a broken statement three times would just be three of the same error."""
    calls = {'n': 0}

    def _bad(cur):
        calls['n'] += 1
        cur.execute('SELECT * FROM axiom_table_that_does_not_exist')

    with pytest.raises(psycopg.errors.UndefinedTable):
        demo_state.tx(_bad, readonly=True)
    assert calls['n'] == 1


# ==================================================================== bounded growth

def test_the_worker_rail_is_capped_and_old_rows_are_reaped(monkeypatch):
    """One row per click, forever, is how the left rail reached twelve in production."""
    made = []

    def _make(cur):
        for i in range(20):
            ref = f'resilience-{uuid.uuid4().hex[:8]}'
            cur.execute("""
                INSERT INTO axiom_agent (tenant_id, worker_ref, kind, status,
                                         heartbeat_at, started_at)
                VALUES (%s, %s, 'worker', 'DEAD', now() - INTERVAL '3 hours',
                        now() - INTERVAL '3 hours')
                RETURNING id
            """, (str(SYSTEM_TENANT), ref))
            made.append(cur.fetchone()['id'])

    demo_state.tx(_make)
    try:
        _, rows = route(api.agents, limit=50)
        assert len(rows) <= 50, f'the endpoint returned {len(rows)} rows'
        _, rows = route(api.agents, limit=5)
        assert len(rows) <= 5

        monkeypatch.setattr(demo_state, 'AGENT_ROWS_KEPT', 2)
        monkeypatch.setattr(demo_state, 'AGENT_ROW_TTL_S', 60)
        removed = demo_state.reap_agents()
        assert removed >= 18, f'reaped only {removed} of 20 three-hour-old rows'

        left = query('SELECT count(*) AS n FROM axiom_agent WHERE id = ANY(%s)',
                     ([str(i) for i in made],))
        assert left[0]['n'] <= 2
    finally:
        demo_state.tx(lambda cur: cur.execute(
            'DELETE FROM axiom_agent WHERE id = ANY(%s)',
            ([str(i) for i in made],)))


def test_run_worker_refuses_to_start_an_unbounded_number(monkeypatch):
    """A judge leaning on the button must not become thirty processes."""
    monkeypatch.setattr(demo_state, 'live_workers', lambda: demo_state.MAX_LIVE_WORKERS)
    started = {'n': 0}
    monkeypatch.setattr(api, '_start_worker',
                        lambda **kw: started.__setitem__('n', started['n'] + 1))

    status, body = route(api.demo_run_worker, body=api.RunWorkerBody())
    assert status == 200
    assert body['started'] is False and body['live_workers'] == demo_state.MAX_LIVE_WORKERS
    assert started['n'] == 0

    # ...and the refusal is a 200, not an error: nothing failed, so nothing turns red.
    assert body['ok'] is True


def test_the_demo_controls_are_rate_limited():
    assert demo_state.gate('unit-test-gate', 30) == 0.0
    again = demo_state.gate('unit-test-gate', 30)
    assert 0 < again <= 30

    demo_state.reset_gates()
    from fastapi import HTTPException
    api._gate('reset', 15)
    with pytest.raises(HTTPException) as ei:
        api._gate('reset', 15)
    assert ei.value.status_code == 429
    assert 'Retry-After' in (ei.value.headers or {})


# ======================================================================= the headline

def test_a_deliberate_duplicate_outside_the_mission_cannot_reach_the_headline():
    """scripts/counterexample.py double-refunds an order ON PURPOSE. It must not count.

    Two refunds under two different idempotency keys for one order ref is exactly what
    the baseline agent in the counterexample does, and the ledger is global and
    append-only. If the dashboard's DUPLICATE REFUNDS were computed over that ledger,
    the project's headline number would be reporting somebody else's evidence.
    """
    demo_state.ensure_demo()
    order = f'CE-BASELINE-{uuid.uuid4().hex[:6]}'
    for i in (1, 2):
        provider.create_refund(idempotency_key=f'test-{uuid.uuid4().hex[:12]}-{i}',
                               order_ref=order, amount_cents=30_000,
                               chaos_pre=0.0, chaos_post=0.0, latency_ms=0)
    try:
        _, glob = route(api.provider_stats, scope='global')
        assert glob['duplicate_orders'] >= 1, 'the fixture did not create a duplicate'

        _, scoped = route(api.provider_stats, scope='mission')
        assert scoped['duplicate_orders'] == 0
        assert scoped['orders_in_scope'] == demo_state.DEMO_TASKS

        _, ledger = route(api.provider_ledger, limit=200, scope='mission')
        assert all(r['order_ref'] != order for r in ledger)
    finally:
        demo_state.forget_orders([order])


def test_an_empty_scope_reports_zero_rather_than_the_global_ledger(monkeypatch):
    """The branch that used to fall back to `global` when there was no mission."""
    order = f'CE-BASELINE-{uuid.uuid4().hex[:6]}'
    for i in (1, 2):
        provider.create_refund(idempotency_key=f'test-{uuid.uuid4().hex[:12]}-{i}',
                               order_ref=order, amount_cents=30_000,
                               chaos_pre=0.0, chaos_post=0.0, latency_ms=0)
    monkeypatch.setattr(api, '_mission_order_refs', set)
    try:
        _, scoped = route(api.provider_stats, scope='mission')
        assert scoped == {'refunds': 0, 'total_cents': 0, 'replays': 0, 'verdicts': {},
                          'duplicate_orders': 0, 'scope': 'mission', 'orders_in_scope': 0}
    finally:
        demo_state.forget_orders([order])


def test_freshly_seeded_orders_have_no_inherited_refunds():
    """A task created one second ago cannot own a refund from last week.

    Without this, a demo whose axiom rows were wiped while the external ledger survived
    would re-seed the same thirty order refs, inherit thirty stale refunds, and then
    produce a SECOND refund per order on the next run — thirty duplicates, on camera,
    under a headline that says zero.
    """
    order = 'ORD-1000'                       # the first order the seed always creates
    provider.create_refund(idempotency_key=f'stale-{uuid.uuid4().hex[:12]}',
                           order_ref=order, amount_cents=30_000,
                           chaos_pre=0.0, chaos_post=0.0, latency_ms=0)
    assert provider.ledger(order_ref=order), 'fixture did not land'

    seed.reset()                              # wipes axiom rows AND the whole ledger
    provider.create_refund(idempotency_key=f'stale-{uuid.uuid4().hex[:12]}',
                           order_ref=order, amount_cents=30_000,
                           chaos_pre=0.0, chaos_post=0.0, latency_ms=0)
    demo_state.invalidate()
    demo_state.ensure_demo()

    assert provider.ledger(order_ref=order) == [], \
        'a re-created order kept its old external refunds'


# ============================================================================ health

def test_health_is_honest_about_a_healthy_system():
    demo_state.ensure_demo()
    status, body = route(api.health, deep=False, heal=True)
    assert status == 200
    assert body['ok'] is True and body['status'] == 'ok'
    assert body['checks']['db']['ok'] is True
    assert body['checks']['provider']['ok'] is True
    assert body['checks']['demo']['tasks'] == demo_state.DEMO_TASKS
    assert body['checks']['demo']['active_policies'] == 1
    assert body['checks']['vector_index']['in_use'] is True, \
        'the ANN path degraded to a scan and only the plan can tell you'
    assert body['uptime_seconds'] >= 0


def test_health_answers_503_when_the_database_is_gone(monkeypatch):
    """An uptime monitor must not have to parse prose to page somebody."""
    def _dead(*a, **kw):
        raise Unavailable('db', 'no connection after 6s')

    monkeypatch.setattr(demo_state, 'tx', _dead)
    monkeypatch.setattr(demo_state, 'call', _dead)
    monkeypatch.setattr(demo_state, 'ensure_demo', _dead)

    status, body = route(api.health, deep=False, heal=True)
    assert status == 503
    assert body['ok'] is False and body['status'] == 'down'
    assert 'db' in body['errors']
    assert isinstance(body['errors']['db'], str)


def test_health_does_not_lie_when_only_the_demo_is_empty(monkeypatch):
    """A green light in front of an empty screen is not health."""
    seed.reset()
    demo_state.invalidate()
    status, body = route(api.health, deep=False, heal=False)   # report, do not fix
    assert status == 503
    assert body['db'] is True and body['status'] == 'degraded'
    assert body['checks']['demo']['ok'] is False
    demo_state.invalidate()
    demo_state.ensure_demo()


# ===================================================== degrade, do not disappear

def test_recall_still_returns_hits_when_the_plan_check_fails(monkeypatch):
    """"we could not check" and "it degraded to a scan" are different claims."""
    demo_state.ensure_demo()

    def _explode(cur, sql, params=None):
        raise psycopg.errors.QueryCanceled('EXPLAIN went away')

    monkeypatch.setattr(db, 'explain', _explode)
    status, body = route(api.recall,
                         body=api.RecallBody(query='agent died mid-refund', k=3),
                         tenant_id=DEMO)
    assert status == 200
    assert body['hits'], 'the hits are real and must still be returned'
    assert body['plan_uses_vector_index'] is None
    assert body['plan_checked'] is False


def test_recall_says_which_dependency_failed_when_the_embedder_is_down(monkeypatch):
    def _no_bedrock(_text):
        raise RuntimeError('bedrock: could not connect')

    monkeypatch.setattr(api.embeddings, 'embed_list', _no_bedrock)
    with pytest.raises(Unavailable) as ei:
        route(api.recall, body=api.RecallBody(query='x'), tenant_id=DEMO)
    assert ei.value.component == 'embeddings'


def test_every_read_endpoint_survives_a_tenant_with_nothing_in_it():
    """No route may raise on absence. This is the 'nothing 500s' sweep."""
    nobody = uuid.uuid4()
    # Every Query/Depends default is passed explicitly: calling a FastAPI handler as a
    # plain function does not run dependency injection, so an omitted parameter arrives
    # as the Query() sentinel object itself and would be silently interpolated into SQL.
    checks = [
        (api.mission, {'tenant_id': nobody}),
        (api.list_tasks, {'limit': 200, 'tenant_id': nobody}),
        (api.event_timeline, {'limit': 60, 'tenant_id': nobody}),
        (api.approvals, {'tenant_id': nobody}),
        (api.memories, {'limit': 40, 'include_inadmissible': True, 'tenant_id': nobody}),
        (api.unsettled, {'tenant_id': nobody}),
        (api.agents, {'limit': 50}),
        (api.crash_windows, {}),
        (api.provider_ledger, {'limit': 50, 'scope': 'mission'}),
        (api.provider_stats, {'scope': 'mission'}),
        (api.rewind, {'seconds_ago': 5, 'tenant_id': nobody}),
        (api.memory_effects, {'memory_id': uuid.uuid4(), 'tenant_id': nobody}),
    ]
    for fn, kw in checks:
        status, body = route(fn, **kw)
        assert status == 200, f'{fn.__name__} answered {status}: {body}'
        json.dumps(body, default=str)     # and it is serializable, not a live cursor


# ========================================================================= auto-heal

def test_autoheal_refuses_while_a_worker_is_alive(monkeypatch):
    monkeypatch.setattr(demo_state, 'live_workers', lambda: 1)
    demo_state.reset_gates()
    demo_state._last_autoheal = 0.0
    go, why = demo_state.should_autoheal()
    assert go is False and 'alive' in why


def test_autoheal_refuses_while_the_board_is_still_moving(monkeypatch):
    monkeypatch.setattr(demo_state, 'live_workers', lambda: 0)
    monkeypatch.setattr(demo_state, 'claimable_work',
                        lambda *a, **kw: {'claimable': 7, 'idle_seconds': 3.0})
    demo_state.reset_gates()
    demo_state._last_autoheal = 0.0
    go, why = demo_state.should_autoheal()
    assert go is False and 'changed' in why


def test_autoheal_fires_only_on_an_abandoned_board(monkeypatch):
    monkeypatch.setattr(demo_state, 'live_workers', lambda: 0)
    monkeypatch.setattr(demo_state, 'claimable_work',
                        lambda *a, **kw: {'claimable': 3, 'idle_seconds': 9999.0})
    demo_state.reset_gates()
    demo_state._last_autoheal = 0.0
    go, why = demo_state.should_autoheal()
    assert go is True and 'claimable' in why

    # ...and immediately refuses again, so a polling dashboard cannot start a fleet.
    demo_state.reset_gates()
    go, _ = demo_state.should_autoheal()
    assert go is False


def test_autoheal_ignores_tasks_that_are_waiting_on_a_human():
    """AWAITING_APPROVAL is claimable, but it is not stuck — it is waiting correctly.

    Starting a worker to "fix" a parked approval loops it through the approval path
    forever, so the auto-heal predicate deliberately does not count those tasks.
    """
    demo_state.ensure_demo()
    work_before = demo_state.claimable_work()

    def _park(cur):
        cur.execute("""
            UPDATE axiom_task SET state = 'AWAITING_APPROVAL', updated_at = now()
            WHERE tenant_id = %s AND state = 'READY'
              AND id IN (SELECT id FROM axiom_task WHERE tenant_id = %s
                         AND state = 'READY' LIMIT 2)
            RETURNING id
        """, (str(DEMO), str(DEMO)))
        return [r['id'] for r in cur.fetchall()]

    parked = demo_state.tx(_park)
    try:
        after = demo_state.claimable_work()
        assert after['claimable'] == work_before['claimable'] - len(parked)
    finally:
        demo_state.tx(lambda cur: cur.execute(
            "UPDATE axiom_task SET state = 'READY' WHERE id = ANY(%s)",
            ([str(p) for p in parked],)))
