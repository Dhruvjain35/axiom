"""AXIOM :: the adapter's invariant suite.

The engine's suite proves the protocols. This one proves the SEAM: that wrapping someone
else's function does not weaken any of them, and that the one thing an integration can
silently get wrong — where the idempotency key comes from — cannot be got wrong quietly.

The tests that matter here are the ones that try to cause a second refund through the
decorator: crash the tool after the money moves, call it four times at once, change the
amount under an existing key, hand it an identity that will not survive a restart. Each
of them ends at the same assertion, which is the only one a customer would recognise:
`provider.duplicate_check()` is empty.
"""

from __future__ import annotations

import uuid
import warnings

import pytest

from axiom import adapter, db, provider, tasks
from axiom.models import AttemptState, TaskState
from conftest import POLICY_ID, race                              # noqa: F401


# ============================================================================ harness

@pytest.fixture
def bound(world):
    """A World, with the guards pointed at its tenant, mission and policy."""
    with adapter.bind(tenant_id=world.tenant_id, mission_id=world.mission_id,
                      policy_id=POLICY_ID, actor='system:test'):
        yield world


@pytest.fixture
def bound_strict(strict_world):
    """Same, against the tenant whose policy only authorizes $50 unattended."""
    with adapter.bind(tenant_id=strict_world.tenant_id, mission_id=strict_world.mission_id,
                      policy_id=POLICY_ID, actor='system:test'):
        yield strict_world


@pytest.fixture(scope='module', autouse=True)
def _clean_adapter_agent():
    """The adapter registers ONE agent row per process (worker_ref is pid-derived), so
    this fixture only has to take it back down — otherwise a later pytest session would
    find an ALIVE agent with a fresh heartbeat and refuse to run, which is exactly the
    guard conftest._exclusive_queue is supposed to provide."""
    yield
    ref = adapter._agent_ref
    adapter.shutdown()
    if ref:
        def _wipe(cur):
            cur.execute('SELECT id FROM axiom_agent WHERE worker_ref = %s', (ref,))
            ids = [str(r['id']) for r in cur.fetchall()]
            if ids:
                cur.execute('DELETE FROM axiom_event WHERE subject_id = ANY(%s::UUID[])',
                            (ids,))
                cur.execute('DELETE FROM axiom_agent WHERE id = ANY(%s::UUID[])', (ids,))
        db.tx(_wipe)


def order_ref() -> str:
    return f'ORD-AD-{uuid.uuid4().hex[:10].upper()}'


def make_refund_tool(*, calls: list, fail_after_effect: bool = False, **guard_kwargs):
    """A tool of exactly the shape a team already has: takes business arguments, calls a
    payment provider, returns its response. The decorator is the only AXIOM in it."""
    kwargs = {'action': 'refund', 'key': 'order_id', 'amount': 'amount_cents',
              'provider': 'payments', 'operation': 'refunds.create', **guard_kwargs}

    @adapter.guard(**kwargs)
    def issue_refund(order_id: str, amount_cents: int, idempotency_key: str) -> dict:
        calls.append(idempotency_key)
        r = provider.create_refund(idempotency_key=idempotency_key, order_ref=order_id,
                                   amount_cents=amount_cents, latency_ms=0)
        if fail_after_effect:
            raise ConnectionError('socket died reading the response')
        return {'id': r.provider_ref, 'replayed': r.replayed}

    return issue_refund


# ================================================================== KEY DERIVATION

def test_the_key_comes_from_the_arguments_not_from_the_code():
    """Two separately-written tools, same action and same key, same identity.

    This is the property that survives a restart: the second process does not have the
    first one's function object, its uuid, or its clock — it has the arguments, and that
    is enough to arrive at the same receipt.
    """
    a = make_refund_tool(calls=[])
    b = make_refund_tool(calls=[])
    assert a.axiom_key(order_id='ORD-1', amount_cents=500) == 'refund|order_id=ORD-1'
    assert a.axiom_key(order_id='ORD-1', amount_cents=500) == \
           b.axiom_key(order_id='ORD-1', amount_cents=999)     # amount is not identity
    assert a.axiom_key(order_id='ORD-2', amount_cents=500) != \
           a.axiom_key(order_id='ORD-1', amount_cents=500)


@pytest.mark.parametrize('bad', [None, '', '   ', {'order': 1}, ['ORD-1'], 3.5,
                                 object()])
def test_an_identity_that_would_not_survive_a_restart_is_refused(bound, bad):
    """And refused BEFORE anything is written. An unstable key does not fail loudly at
    the moment it is used — it double-charges later, under a crash, in production."""
    calls: list = []
    tool = make_refund_tool(calls=calls)
    before = bound.scalar('SELECT count(*) FROM axiom_task WHERE tenant_id = %s',
                          (str(bound.tenant_id),))
    with pytest.raises(adapter.UnstableKey):
        tool(order_id=bad, amount_cents=500)
    assert calls == [], 'the tool must not run when the identity is unusable'
    assert bound.scalar('SELECT count(*) FROM axiom_task WHERE tenant_id = %s',
                        (str(bound.tenant_id),)) == before


def test_a_key_naming_a_parameter_that_does_not_exist_fails_at_decoration():
    """A typo in key= is a double charge waiting for a crash. Make it an import error."""
    with pytest.raises(adapter.UnstableKey, match='no such parameter'):
        @adapter.guard(action='refund', key='oder_id')
        def issue_refund(order_id: str) -> dict:
            return {}


def test_an_async_tool_is_refused_rather_than_silently_blocking_the_loop():
    with pytest.raises(adapter.AdapterError, match='async'):
        @adapter.guard(action='refund', key='order_id')
        async def issue_refund(order_id: str) -> dict:
            return {}


# ========================================================================= HAPPY PATH

def test_the_outcome_and_its_memory_are_co_committed(bound):
    calls: list = []
    tool = make_refund_tool(calls=calls)
    order = order_ref()

    call = tool.axiom(order_id=order, amount_cents=4200)

    assert call.value['replayed'] is False and call.already_settled is False
    assert len(calls) == 1 and calls[0] == call.idempotency_key
    assert calls[0].startswith('axm_'), 'the key must be the database-generated one'

    row = bound.task_row(call.task_id)
    assert row['state'] == TaskState.SUCCEEDED
    receipts = bound.receipts(call.task_id)
    assert len(receipts) == 1
    assert receipts[0]['attempt_state'] == AttemptState.SUCCEEDED
    assert receipts[0]['provider_ref'] == call.value['id']

    # The memory of the act was written by the SAME transaction that settled it.
    mem = bound.rows('SELECT content, outcome, attempt_id FROM axiom_memory '
                     'WHERE task_id = %s', (str(call.task_id),))
    assert len(mem) == 1 and mem[0]['attempt_id'] == receipts[0]['id']
    assert call.idempotency_key in mem[0]['content']


def test_a_completed_act_is_answered_from_the_record_not_the_provider(bound):
    """Every retry, every duplicate webhook, every impatient double-click."""
    calls: list = []
    tool = make_refund_tool(calls=calls)
    order = order_ref()

    first = tool.axiom(order_id=order, amount_cents=4200)
    again = tool.axiom(order_id=order, amount_cents=4200)

    assert len(calls) == 1, 'the tool must not run a second time'
    assert again.already_settled is True
    assert again.value['id'] == first.value['id']
    assert len(provider.ledger(order)) == 1


def test_the_mission_budget_is_debited_by_the_guarded_call(bound):
    spent_before, _ = bound.spent()
    make_refund_tool(calls=[])(order_id=order_ref(), amount_cents=4200)
    assert bound.spent()[0] == spent_before + 4200


# ============================================================== THE CRASH, AND AFTER

def test_a_tool_that_dies_after_the_effect_does_not_cause_a_second_one(bound):
    """The whole argument, through the decorator.

    The tool reaches the provider, the money moves, and then the call dies on the way
    back. AXIOM cannot know whether the effect landed — so it keeps the receipt, and the
    next call re-sends under the same key rather than minting a new one.
    """
    order = order_ref()
    first_calls: list = []
    dying = make_refund_tool(calls=first_calls, fail_after_effect=True)

    with pytest.raises(ConnectionError):
        dying(order_id=order, amount_cents=7700)

    # The receipt is still LIVE: the evidence that an effect may exist is not discarded
    # just because the process that caused it failed.
    task_id = bound.scalar('SELECT id FROM axiom_task WHERE tenant_id = %s AND '
                           'dedupe_key = %s', (str(bound.tenant_id), f'refund|order_id={order}'))
    live = bound.live_receipt(task_id, step='refund')
    assert live is not None and live.attempt_state in (AttemptState.PREPARED,
                                                       AttemptState.DISPATCHED)

    second_calls: list = []
    healthy = make_refund_tool(calls=second_calls)
    call = healthy.axiom(order_id=order, amount_cents=7700)

    assert call.recovered is True, 'the second call went through RECOVER'
    assert second_calls[0] == first_calls[0], 'the SAME key, derived not generated'
    assert call.value['replayed'] is True, 'the provider recognized the key'
    assert len(provider.ledger(order)) == 1
    assert provider.duplicate_check([order]) == []
    assert bound.task_row(task_id)['state'] == TaskState.SUCCEEDED


def test_the_recovery_is_journalled_as_a_recovery(bound):
    order = order_ref()
    with pytest.raises(ConnectionError):
        make_refund_tool(calls=[], fail_after_effect=True)(order_id=order, amount_cents=100)
    make_refund_tool(calls=[])(order_id=order, amount_cents=100)

    task_id = bound.scalar('SELECT id FROM axiom_task WHERE tenant_id = %s AND '
                           'dedupe_key = %s', (str(bound.tenant_id), f'refund|order_id={order}'))
    events = bound.events(task_id)
    assert 'task.recovered' in events
    assert events.index('task.recovered') < events.index('attempt.settled')


def test_four_callers_at_once_produce_one_effect(bound):
    """A queue of retries, a fanned-out worker pool, a user hammering a button."""
    order = order_ref()
    calls: list = []
    tool = make_refund_tool(calls=calls)

    results = race([lambda: tool.axiom(order_id=order, amount_cents=1500)] * 4)

    ok = [r for kind, r in results if kind == 'ok']
    raised = [r for kind, r in results if kind == 'raised']
    assert len(calls) == 1, f'the tool ran {len(calls)} times'
    assert all(isinstance(e, adapter.ActionInFlight) for e in raised), raised
    assert len({r.value['id'] for r in ok}) == 1, 'everyone who got an answer got the same one'
    assert len(provider.ledger(order)) == 1
    assert provider.duplicate_check([order]) == []


# ================================================================== INTENT, CHANGED

def test_the_same_key_with_a_different_amount_is_a_hard_stop(bound):
    """Same identity + different intent is not a retry — the adapter-level W7."""
    order = order_ref()
    tool = make_refund_tool(calls=[], fail_after_effect=True)
    with pytest.raises(ConnectionError):
        tool(order_id=order, amount_cents=7700)

    with pytest.raises(adapter.IntentChanged, match='not a retry'):
        make_refund_tool(calls=[])(order_id=order, amount_cents=9900)
    assert len(provider.ledger(order)) == 1, 'no second refund at the new amount'


def test_a_tool_that_ignores_the_key_is_warned_about(bound):
    """Not an error — a read-only tool is legitimately non-idempotent. But if this one
    moves money, this warning IS the bug: the provider cannot dedupe what it never saw."""
    @adapter.guard(action='notify', key='order_id')
    def send_note(order_id: str) -> dict:
        return {'sent': order_id}

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        send_note(order_id=order_ref())
    assert any(w.category is adapter.KeyUnusedWarning for w in caught)


# ========================================================================= AUTHORITY

def test_over_the_policy_ceiling_parks_on_a_human_and_sends_nothing(bound_strict):
    """$50 ceiling, $77 refund: the receipt is never minted, so nothing can be in flight."""
    order = order_ref()
    calls: list = []
    tool = make_refund_tool(calls=calls)

    with pytest.raises(adapter.ApprovalRequired) as exc:
        tool(order_id=order, amount_cents=7700)

    assert calls == [] and provider.ledger(order) == []
    task_id = exc.value.task_id
    assert bound_strict.task_row(task_id)['state'] == TaskState.AWAITING_APPROVAL
    assert bound_strict.receipts(task_id) == []

    # A human rules, and the same call now goes through — once.
    db.tx(lambda cur: tasks.decide_approval(
        cur, tenant_id=bound_strict.tenant_id, approval_id=exc.value.approval_id,
        approved=True, decided_by='ops@axiom.invalid'))
    call = tool.axiom(order_id=order, amount_cents=7700)
    assert call.value['replayed'] is False
    assert len(provider.ledger(order)) == 1


def test_a_rejected_approval_refuses_the_act_for_good(bound_strict):
    order = order_ref()
    tool = make_refund_tool(calls=[])
    with pytest.raises(adapter.ApprovalRequired) as exc:
        tool(order_id=order, amount_cents=7700)
    db.tx(lambda cur: tasks.decide_approval(
        cur, tenant_id=bound_strict.tenant_id, approval_id=exc.value.approval_id,
        approved=False, decided_by='ops@axiom.invalid'))

    with pytest.raises(adapter.ActionRefused):
        tool(order_id=order, amount_cents=7700)
    assert provider.ledger(order) == []


def test_a_risk_descriptor_the_policy_does_not_govern_parks_on_a_human(bound):
    """The act that is not denominated in money, decided by the ENGINE.

    `risk=` may be a `risk.Risk`, in which case the guard does not decide anything: it
    hands the descriptor to Policy.decide(), the same general authority model
    tasks.prepare() uses. This world's policy holds no grant over `data.subjects`, and an
    ungoverned unit is a refusal rather than a default — so 40,000 customer records go to
    a human even though not one cent moves.
    """
    from axiom.risk import Reversibility, Risk

    purged: list = []

    @adapter.guard(action='purge_records', key='workspace_id',
                   risk=lambda a: Risk.of('data.subjects', a['record_count'],
                                          reversibility=Reversibility.IRREVERSIBLE))
    def purge(workspace_id: str, record_count: int, idempotency_key: str) -> dict:
        purged.append(record_count)
        return {'purged': workspace_id}

    with pytest.raises(adapter.ApprovalRequired) as exc:
        purge(workspace_id='WS-9', record_count=40_000)
    assert purged == [], 'nothing irreversible happened'
    assert bound.receipts(exc.value.task_id) == [], 'no receipt was minted either'
    reason = bound.rows('SELECT reason FROM axiom_approval WHERE id = %s',
                        (str(exc.value.approval_id),))[0]['reason']
    assert 'data.subjects' in reason and 'ungoverned' in reason

    db.tx(lambda cur: tasks.decide_approval(
        cur, tenant_id=bound.tenant_id, approval_id=exc.value.approval_id,
        approved=True, decided_by='ops@axiom.invalid'))
    assert purge(workspace_id='WS-9', record_count=40_000) == {'purged': 'WS-9'}
    assert purged == [40_000]


def test_a_granted_risk_proceeds_unattended_and_a_larger_one_does_not(bound):
    """The same act, twice, either side of a written-down ceiling.

    Nothing about this is money-shaped, and nothing about it is special-cased in the
    adapter: the policy grants 100 `data.subjects` at IRREVERSIBLE, so 50 goes and 40,000
    stops.
    """
    from axiom import policy as policy_mod
    from axiom.risk import Grant, Reversibility, Risk

    db.tx(lambda cur: policy_mod.publish(
        cur, tenant_id=bound.tenant_id, policy_id=POLICY_ID, version=2,
        body={'description': 'may delete a handful of records unattended'},
        max_auto_action_cents=10_000_00, requires_approval=False,
        risk_grants=[Grant('data.subjects', 100, Reversibility.IRREVERSIBLE)],
        created_by='human:test@axiom.invalid', activate=True))

    @adapter.guard(action='purge_records', key='workspace_id',
                   risk=lambda a: Risk.of('data.subjects', a['record_count'],
                                          reversibility=Reversibility.IRREVERSIBLE))
    def purge(workspace_id: str, record_count: int, idempotency_key: str) -> dict:
        return {'purged': record_count}

    assert purge(workspace_id='WS-SMALL', record_count=50) == {'purged': 50}
    with pytest.raises(adapter.ApprovalRequired):
        purge(workspace_id='WS-BIG', record_count=40_000)


def test_a_risk_label_is_the_shortcut_and_still_parks(bound):
    """The weaker form, for an act nobody has written a measurement for yet.

    A plain string is matched against a vocabulary in the policy BODY — versioned, hashed
    and signable, so still procedural memory, but Python-enforced and default-open where
    a Risk descriptor is deny-by-default. Both are tested because both ship.
    """
    from axiom import policy as policy_mod
    db.tx(lambda cur: policy_mod.publish(
        cur, tenant_id=bound.tenant_id, policy_id=POLICY_ID, version=2,
        body={'description': 'risk-aware', 'escalate_risks': ['data_deletion']},
        max_auto_action_cents=10_000_00, requires_approval=False,
        created_by='human:test@axiom.invalid', activate=True))

    deleted: list = []

    @adapter.guard(action='purge_workspace', key='workspace_id', risk='data_deletion')
    def purge(workspace_id: str, idempotency_key: str) -> dict:
        deleted.append(idempotency_key)
        return {'purged': workspace_id}

    with pytest.raises(adapter.ApprovalRequired) as exc:
        purge(workspace_id='WS-77')
    assert deleted == [], 'nothing irreversible happened'
    assert bound.receipts(exc.value.task_id) == [], 'no receipt was minted either'

    db.tx(lambda cur: tasks.decide_approval(
        cur, tenant_id=bound.tenant_id, approval_id=exc.value.approval_id,
        approved=True, decided_by='ops@axiom.invalid'))
    assert purge(workspace_id='WS-77') == {'purged': 'WS-77'}
    assert len(deleted) == 1 and deleted[0].startswith('axm_')


def test_an_unbound_call_refuses_rather_than_guessing_a_tenant():
    tool = make_refund_tool(calls=[])
    with adapter.bind(tenant_id=uuid.uuid4(), mission_id=uuid.uuid4(),
                      policy_id='x'):
        pass                                   # ... and restored on exit
    adapter._default_binding = None            # simulate a process that never bound
    with pytest.raises(adapter.NotBound):
        tool(order_id='ORD-NOBIND', amount_cents=1)
