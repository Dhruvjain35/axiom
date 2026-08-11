"""AXIOM :: the crash-window table, as executable specification.

One test per row of the table in docs/HANDOFF.md §6.6. Each one puts the system into the
exact state a crash at that instant would leave behind, then tries to produce the failure
that window makes possible — a second refund, a zombie's write, a forged intent — and
asserts the system refuses.

| #  | crash point                                | effect possible? | the assertion here          |
|----|--------------------------------------------|------------------|-----------------------------|
| W1 | after CLAIM, before PREPARE                | no               | no receipt, no ledger row    |
| W2 | after receipt COMMIT, before send          | unknowably       | RESEND, exactly one refund   |
| W3 | mid-flight (DISPATCHED marker written)     | yes              | DISPATCHED decides nothing   |
| W4 | provider responded, before SETTLE          | yes, it landed   | replayed, no duplicate       |
| W5 | zombie settles after its lease expired     | yes              | LeaseLost; successor wins    |
| W6 | two executors PREPARE the same step        | no               | one receipt; 23505 for the   |
|    |                                            |                  | loser                        |
| W7 | recovered agent re-synthesizes a new body  | yes              | fingerprint mismatch, stop   |

The vocabulary is "effectively-once via idempotency receipts", never "exactly-once": the
provider is a separate database AXIOM cannot enlist in its transactions, so what is
proven below is that every crash window has a defined outcome and that the outcome is
never a second real-world effect.
"""

from __future__ import annotations

import psycopg
import pytest

from axiom import db, provider, tasks
from axiom.models import AttemptState, Outcome, TaskState
from axiom.provider import ProviderError
from axiom.tasks import AlreadyLive, FingerprintMismatch, LeaseLost

from conftest import STEP, clone, dispatch, race


# ================================================================== W1  no effect yet

def test_w1_crash_after_claim_before_prepare_leaves_nothing_behind(world):
    """The claim itself authorizes nothing, so the crash is free.

    This is the window the whole commit ordering exists to create: because the receipt
    commits BEFORE any call goes out, a worker that dies between CLAIM and PREPARE
    cannot have caused an effect. That is not a statement about timing luck, it is a
    consequence of which transaction commits first — and it is what makes it safe for
    the rest of this suite (and for World.claim) to abandon leases casually.
    """
    job = world.enqueue(amount_cents=30_000)
    a, b = world.agent('w1-dies'), world.agent('w1-recovers')

    claimed_a = world.claim(a, want=job.id)
    assert claimed_a.state is TaskState.LEASED
    assert claimed_a.lease_epoch == 1
    # ... and here the process dies. No prepare, no dispatch, no settle, no cleanup.

    world.lease_expires()
    claimed_b = world.claim(b, want=job.id)

    assert claimed_b.lease_epoch == claimed_a.lease_epoch + 1, 'the fence must advance'
    assert claimed_b.state is TaskState.LEASED, 'nothing was prepared, so nothing to recover'
    assert claimed_b.is_recovery is False

    assert world.receipts(job.id) == [], 'a receipt exists that nothing authorized'
    assert world.live_receipt(job.id) is None
    assert provider.ledger(order_ref=job.order_ref) == [], 'money moved before a receipt existed'

    # Try to break it: the dead worker comes back and tries to act on its stale lease.
    with pytest.raises(LeaseLost):
        world.prepare(claimed_a, a, job)
    assert world.receipts(job.id) == []


# ============================================== W2  receipt is durable, nothing was sent

def test_w2_crash_between_receipt_and_send_resends_under_the_same_key(world):
    """The effect is unknown, so the only safe move is to re-send the SAME key.

    "Unknown" is the hard case, not the easy one. A framework that replans here issues a
    second refund; a framework that gives up strands the customer. AXIOM re-dispatches
    the stored request body under the derived key and lets the provider collapse the
    duplicate — which is why this test dispatches TWICE and still asserts one refund.
    """
    job = world.enqueue(amount_cents=12_500)
    a, b = world.agent('w2-dies'), world.agent('w2-recovers')

    claimed_a = world.claim(a, want=job.id)
    receipt = world.prepare(claimed_a, a, job).receipt
    assert receipt is not None
    assert receipt.attempt_state is AttemptState.PREPARED
    assert world.task_row(job.id)['state'] == str(TaskState.ACTION_PREPARED)
    # The receipt is durable and the call has NOT gone out. Die here.
    assert provider.ledger(order_ref=job.order_ref) == []

    world.lease_expires()
    claimed_b = world.claim(b, want=job.id)
    assert claimed_b.is_recovery, 'ACTION_PREPARED must be claimed as a recovery'

    plan = world.recover(claimed_b, b)
    assert plan.action == 'RESEND'
    assert plan.receipt is not None
    assert plan.receipt.idempotency_key == receipt.idempotency_key, (
        'a recovered worker must inherit the key, never mint one')

    first = dispatch(plan.receipt)
    assert (first.status, first.replayed) == (201, False)

    # A second crash in the same window: re-send again under the same key.
    again = dispatch(plan.receipt)
    assert again.replayed is True
    assert again.provider_ref == first.provider_ref

    assert len(provider.ledger(order_ref=job.order_ref)) == 1, 'the customer was refunded twice'
    assert provider.duplicate_check() == []

    world.settle(claimed_b, b, plan.receipt, again)
    assert world.task_row(job.id)['state'] == str(TaskState.SUCCEEDED)
    assert world.live_receipt(job.id) is None
    assert 'task.recovered' in world.events(job.id)


# ================================================ W3  mid-flight, DISPATCHED is a marker

def test_w3_dispatched_marker_never_decides_correctness(world):
    """DISPATCHED is safety-equivalent to PREPARED, and recovery must treat it that way.

    The tempting bug is to branch on it — "PREPARED means we never sent, DISPATCHED means
    we did" — which is false, because the process can die between the send and the marker
    write in either direction. The marker exists for a human watching a dashboard. This
    test asserts the recovery path reaches the same decision under both states.
    """
    job = world.enqueue(amount_cents=8_900)
    a, b = world.agent('w3-dies'), world.agent('w3-recovers')

    claimed_a = world.claim(a, want=job.id)
    receipt = world.prepare(claimed_a, a, job).receipt
    db.tx(lambda cur: tasks.mark_dispatched(cur, receipt=receipt))
    assert world.receipts(job.id)[0]['attempt_state'] == str(AttemptState.DISPATCHED)
    # ... and the HTTP call is in flight, outcome unknown, when the process dies.

    world.lease_expires()
    claimed_b = world.claim(b, want=job.id)

    live = world.live_receipt(job.id)
    assert live is not None, 'DISPATCHED must still count as a live receipt'
    assert live.attempt_state is AttemptState.DISPATCHED

    plan = world.recover(claimed_b, b)
    assert plan.action == 'RESEND', 'DISPATCHED must recover identically to PREPARED'
    assert plan.receipt.idempotency_key == receipt.idempotency_key

    result = dispatch(plan.receipt)
    world.settle(claimed_b, b, plan.receipt, result)
    assert len(provider.ledger(order_ref=job.order_ref)) == 1


# ============================================ W4  the effect landed, we never recorded it

def test_w4_crash_after_the_refund_landed_replays_instead_of_refunding_twice(world):
    """The money is already gone and AXIOM does not know it. The dangerous window.

    Everything before the provider call is reversible; this is the first instant that is
    not. The system's only honest move is to re-send the same key and let the provider
    tell it what already happened — converting "unknown" into "known" at zero cost.
    """
    job = world.enqueue(amount_cents=30_000)
    a, b = world.agent('w4-dies'), world.agent('w4-recovers')

    claimed_a = world.claim(a, want=job.id)
    receipt = world.prepare(claimed_a, a, job).receipt

    landed = dispatch(receipt)                    # THE irreversible act
    assert landed.replayed is False
    # ... and the worker is SIGKILLed here, before settle. AXIOM's own state still says
    # "an effect may exist", which is exactly right: it does, and nothing recorded it.
    assert world.task_row(job.id)['state'] == str(TaskState.ACTION_PREPARED)
    assert world.receipts(job.id)[0]['settled_at'] is None

    world.lease_expires()
    claimed_b = world.claim(b, want=job.id)
    plan = world.recover(claimed_b, b)
    assert plan.action == 'RESEND'

    replay = dispatch(plan.receipt)
    assert replay.replayed is True, 'the provider created a SECOND refund'
    assert replay.provider_ref == landed.provider_ref
    assert replay.status == 200

    assert provider.duplicate_check() == []
    assert len(provider.ledger(order_ref=job.order_ref)) == 1

    # The receipt was minted under epoch 1; B holds epoch 2. Settling must still work:
    # the fence guards the TASK, and the receipt carries the epoch it was minted under.
    assert plan.receipt.lease_epoch == claimed_a.lease_epoch
    assert claimed_b.lease_epoch > plan.receipt.lease_epoch
    mem_id = world.settle(claimed_b, b, plan.receipt, replay)

    assert world.task_row(job.id)['state'] == str(TaskState.SUCCEEDED)
    lesson = world.rows('SELECT resolution, outcome FROM axiom_memory WHERE id = %s',
                        (str(mem_id),))[0]
    assert lesson['resolution']['replayed'] is True, (
        'the memory must record that this was a replay, not a fresh refund')


# ==================================================================== W5  the zombie

def test_w5_zombie_settle_is_rejected_by_the_fence(world):
    """A lease expiring does not stop a worker that is already inside an HTTP call.

    That is why the fence, not the lease, is the correctness mechanism. Agent A comes
    back from a GC pause holding a lease that has been reassigned; its write must be
    refused, and B's must land.
    """
    job = world.enqueue(amount_cents=15_400)
    a, b = world.agent('w5-zombie'), world.agent('w5-successor')

    claimed_a = world.claim(a, want=job.id)
    receipt_a = world.prepare(claimed_a, a, job).receipt

    world.lease_expires()
    claimed_b = world.claim(b, want=job.id)
    assert claimed_b.lease_epoch == claimed_a.lease_epoch + 1

    landed = dispatch(receipt_a)                  # the zombie's call actually lands

    with pytest.raises(LeaseLost):
        world.settle(claimed_a, a, receipt_a, landed)

    # Try to break it below the engine: the same UPDATE the zombie's settle would run,
    # written by hand, must match zero rows. The fence has to live in the WHERE clause.
    stale = world.execute(
        "UPDATE axiom_task SET state = 'SUCCEEDED', lease_owner = NULL WHERE id = %s "
        "AND lease_epoch = %s", (str(job.id), claimed_a.lease_epoch))
    assert stale == 0, 'a stale-epoch write reached the task row'

    assert world.task_row(job.id)['state'] == str(TaskState.ACTION_PREPARED)

    plan = world.recover(claimed_b, b)
    replay = dispatch(plan.receipt)
    assert replay.replayed is True
    world.settle(claimed_b, b, plan.receipt, replay)

    assert world.task_row(job.id)['state'] == str(TaskState.SUCCEEDED)
    assert len(world.receipts(job.id)) == 1, 'the zombie minted a second receipt'
    assert len(provider.ledger(order_ref=job.order_ref)) == 1


# ============================================================ W6  two live receipts

def test_w6_racing_prepares_produce_exactly_one_receipt(world):
    """Two executors holding the SAME fence — the one case the fence cannot decide.

    A duplicated container, a forked process, a retry loop that re-entered: all of them
    present the identical (agent, epoch) pair, so `_assert_fence` passes for both and the
    only thing standing between the customer and two refunds is the partial unique index
    axiom_attempt_one_live. That is the point of putting it in the schema.
    """
    job = world.enqueue(amount_cents=4_500)
    a = world.agent('w6-executor')
    claimed = world.claim(a, want=job.id)

    left, right = clone(claimed), clone(claimed)
    outcomes = race([lambda: world.prepare(left, a, job),
                     lambda: world.prepare(right, a, job)])

    ok = [r for k, r in outcomes if k == 'ok']
    err = [e for k, e in outcomes if k == 'raised']
    assert len(ok) == 1, f'both prepares committed a receipt: {outcomes}'
    assert len(err) == 1
    assert isinstance(err[0], (AlreadyLive, psycopg.errors.UniqueViolation)), (
        f'loser failed for the wrong reason: {err[0]!r}')

    assert len(world.receipts(job.id)) == 1
    spent, _ = world.spent()
    assert spent == job.amount_cents, 'the losing transaction leaked a budget debit'


def test_w6_second_live_receipt_is_refused_by_the_index_itself(world):
    """The same invariant, forced deterministically at the SQL layer.

    prepare() checks for a live receipt first and raises AlreadyLive, which is friendlier
    but is application code. This asserts the database would refuse regardless — insert a
    second receipt for the same (tenant, task, step) with a fresh step_seq and the partial
    unique index rejects it. A future refactor that drops the Python guard still cannot
    produce two in-flight calls.
    """
    job = world.enqueue(amount_cents=4_500)
    a = world.agent('w6-direct')
    claimed = world.claim(a, want=job.id)
    receipt = world.prepare(claimed, a, job).receipt

    with pytest.raises(psycopg.errors.UniqueViolation) as ei:
        world.execute("""
            INSERT INTO axiom_action_attempt (
                tenant_id, task_id, step_name, step_seq, provider, operation,
                amount_cents, currency, request_fingerprint, request_body,
                lease_epoch, prepared_by)
            VALUES (%s, %s, %s, 2, 'payments', 'refunds.create', %s, 'USD',
                    %s, %s, %s, %s)
        """, (str(world.tenant_id), str(job.id), STEP, job.amount_cents,
              receipt.request_fingerprint, '{}', claimed.lease_epoch, str(a)))

    assert 'axiom_attempt_one_live' in str(ei.value)
    assert len(world.receipts(job.id)) == 1


# ================================================= W7  same key, different intent

def test_w7_resynthesized_body_is_a_hard_stop(world):
    """Same idempotency key + different request body is not a retry. It is a new intent.

    The defence against the semantic-rollback class (ACRFence, arXiv:2603.20625): after a
    restart an LLM re-plans and produces a subtly different refund — a different amount,
    a different order — under the key the previous attempt already minted. Both layers
    must refuse: AXIOM before dispatching, and the provider if AXIOM ever did.
    """
    job = world.enqueue(amount_cents=6_700)
    a = world.agent('w7')
    claimed = world.claim(a, want=job.id)
    receipt = world.prepare(claimed, a, job).receipt

    # The stored body always verifies, and key ORDER must not matter — a canonicalization
    # bug here would make every legitimate retry look like an attack.
    tasks.verify_fingerprint(receipt, receipt.request_body)
    reordered = {k: receipt.request_body[k] for k in reversed(list(receipt.request_body))}
    tasks.verify_fingerprint(receipt, reordered)

    mutated = dict(receipt.request_body)
    mutated['amount_cents'] = receipt.request_body['amount_cents'] + 1
    with pytest.raises(FingerprintMismatch):
        tasks.verify_fingerprint(receipt, mutated)

    # Defence in depth: even if AXIOM dispatched it anyway, the provider must reject.
    created = dispatch(receipt)
    assert created.replayed is False

    with pytest.raises(ProviderError) as ei:
        dispatch(receipt, body=mutated)
    assert ei.value.status == 409
    assert ei.value.retryable is False, 'a 409 fingerprint clash must never be retried'

    assert len(provider.ledger(order_ref=job.order_ref)) == 1
    verdicts = provider.stats()['verdicts']
    assert verdicts.get('rejected_fingerprint', 0) >= 1


def test_w7_idempotency_key_is_derived_from_immutable_columns_only(world):
    """The key cannot depend on anything a restart changes, and cannot be supplied.

    Two receipts for the same (tenant, task, step, step_seq) must derive the same key
    even across a lease change — otherwise the recovering worker mints a NEW key, the
    provider sees a brand-new request, and the $300 goes out twice. The schema makes the
    column GENERATED so this is structural, but the test states the property.
    """
    job = world.enqueue(amount_cents=2_100)
    a, b = world.agent('w7b-a'), world.agent('w7b-b')

    claimed_a = world.claim(a, want=job.id)
    receipt = world.prepare(claimed_a, a, job).receipt

    world.lease_expires()
    claimed_b = world.claim(b, want=job.id)
    recovered = world.live_receipt(job.id)

    assert recovered.idempotency_key == receipt.idempotency_key
    assert claimed_b.lease_epoch != claimed_a.lease_epoch, 'the epoch changed underneath it'

    expected = world.scalar(
        "SELECT 'axm_' || substring(sha256(%s || ':' || %s || ':' || %s || ':' || '1') "
        "FROM 1 FOR 48)",
        (str(world.tenant_id), str(job.id), STEP))
    assert receipt.idempotency_key == expected

    # And it is not writable: the column is GENERATED, so an application-supplied key
    # path cannot be introduced by accident.
    with pytest.raises(psycopg.Error):
        world.execute("UPDATE axiom_action_attempt SET idempotency_key = 'axm_forged' "
                      "WHERE id = %s", (str(receipt.id),))


def test_the_provider_really_does_refund_twice_under_two_keys(world):
    """The control that makes every other assertion in this file mean something.

    A skeptic's first question about a stand-in provider is whether it is quietly
    deduplicating on order_ref, in which case none of the crash-window results prove
    anything about AXIOM. It is not: the same order under two different keys produces two
    refunds and duplicate_check() reports it. That is the failure mode the derived key
    exists to prevent, and it is reachable in one line.

    Cleans up after itself so the ledger stays empty for the session's other assertions —
    which is also the only place in the suite that touches the provider's tables directly.
    """
    job = world.enqueue(amount_cents=30_000)
    a = world.agent('control')
    claimed = world.claim(a, want=job.id)
    receipt = world.prepare(claimed, a, job).receipt

    try:
        first = dispatch(receipt)
        second = provider.create_refund(
            idempotency_key=receipt.idempotency_key + '_resynthesized',
            order_ref=job.order_ref, amount_cents=job.amount_cents,
            request_body=receipt.request_body, latency_ms=0)

        assert second.replayed is False, 'the provider deduped on order_ref, not the key'
        assert second.provider_ref != first.provider_ref
        assert len(provider.ledger(order_ref=job.order_ref)) == 2
        dupes = provider.duplicate_check()
        assert [d['order_ref'] for d in dupes] == [job.order_ref]
        assert dupes[0]['refund_count'] == 2
    finally:
        with provider.pool().connection() as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute('DELETE FROM provider_refund WHERE order_ref = %s', (job.order_ref,))
                cur.execute('DELETE FROM provider_request_log WHERE order_ref = %s',
                            (job.order_ref,))
    assert provider.duplicate_check() == []


# ======================================================= what memory does to recovery

def test_majority_adverse_memory_escalates_instead_of_re_dispatching(world_factory):
    """Memory may veto an act. It may never talk the system into one.

    This is the fused transaction earning its keep: the recovering worker reads the
    receipt AND recalls what happened the last time an agent died at this exact state,
    in one commit, and the recall changes the decision. Three prior recoveries that ended
    in a duplicate effect are enough to stop an unattended re-dispatch.
    """
    w = world_factory()
    for i in range(3):
        w.remember(f'agent died mid-refund on a duplicate_charge task and a second agent '
                   f're-planned from the transcript; customer was refunded twice ({i})',
                   outcome=Outcome.DUPLICATE_EFFECT)

    job = w.enqueue(amount_cents=95_000)
    a, b = w.agent('esc-dies'), w.agent('esc-recovers')
    claimed_a = w.claim(a, want=job.id)
    w.prepare(claimed_a, a, job)

    w.lease_expires()
    claimed_b = w.claim(b, want=job.id)
    plan = w.recover(claimed_b, b)

    assert plan.action == 'ESCALATE'
    assert plan.receipt is not None, 'escalation must still carry the receipt it refused'
    assert len(plan.evidence_ids) == 3, 'the decision must name the evidence it used'
    assert provider.ledger(order_ref=job.order_ref) == [], 'it dispatched anyway'

    # The worker's only correct response is terminal. A retry loop around an ambiguous
    # external effect is how you get the double refund this system exists to prevent.
    db.tx(lambda cur: tasks.dead_letter(
        cur, task=claimed_b, agent_id=b, reason=plan.rationale))
    assert w.task_row(job.id)['state'] == str(TaskState.DEAD_LETTER)


def test_a_single_adverse_memory_cannot_veto_a_resend(world_factory):
    """The other side of the same rule: one bad prior is not a majority.

    Without this the design would be trivially safe and useless — any single unlucky
    memory would strand every subsequent recovery on a human. The threshold is stated in
    tasks.recover() and asserted here so a refactor cannot quietly move it.
    """
    w = world_factory()
    w.remember('recovery on a fraud_suspected chargeback needed a human to reconcile',
               outcome=Outcome.HUMAN_REQUIRED)
    for i in range(3):
        w.remember(f'agent died mid-refund on a duplicate_charge task; re-dispatched under '
                   f'the same idempotency key; provider replayed the original ({i})',
                   outcome=Outcome.RESOLVED)

    job = w.enqueue(amount_cents=12_500)
    a, b = w.agent('veto-dies'), w.agent('veto-recovers')
    claimed_a = w.claim(a, want=job.id)
    w.prepare(claimed_a, a, job)

    w.lease_expires()
    plan = w.recover(w.claim(b, want=job.id), b)
    assert plan.action == 'RESEND'
    assert len(plan.recalled) == 4


def test_an_expired_lease_nobody_stole_is_still_safe_to_settle(world):
    """The fence, not the clock, is the invariant — and this is the contrapositive of W5.

    A worker whose lease lapsed while it was inside a slow provider call has NOT lost
    ownership; it has only lost its head start. If nobody took over, the epoch is
    unchanged and its settle must be accepted. Rejecting it would turn every slow refund
    into an orphaned receipt for no safety gain.
    """
    job = world.enqueue(amount_cents=3_200)
    a = world.agent('slow-but-alive')
    claimed = world.claim(a, want=job.id)
    receipt = world.prepare(claimed, a, job).receipt
    landed = dispatch(receipt)

    world.lease_expires()                     # lease lapsed; nobody claimed it
    assert world.task_row(job.id)['lease_epoch'] == claimed.lease_epoch

    world.settle(claimed, a, receipt, landed)
    assert world.task_row(job.id)['state'] == str(TaskState.SUCCEEDED)
    assert len(provider.ledger(order_ref=job.order_ref)) == 1
