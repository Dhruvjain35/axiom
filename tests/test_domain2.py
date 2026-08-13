"""AXIOM :: the crash-window guarantees, in a workload that is not about money.

tests/test_crash_windows.py proves the engine cannot double-refund. This file proves the
same engine cannot double-SEND, which is a different and less forgiving claim: a refund
ledger holds one row per refund, but the relay holds one row per person who received
something, and a second delivery is not a duplicated number, it is a second copy in a
real inbox.

Nothing in axiom/tasks.py, memory.py, policy.py or db.py was changed to make these pass.
That is the load-bearing sentence of the whole file — if the engine had needed a patch to
carry a non-money workload, the generality claim would be a rewrite dressed as a seam.

Three things are asserted here that the refund suite cannot assert:

  * the authority ceiling governs RECIPIENTS, and a send that would clear any dollar
    policy in the system (it costs about two cents) still stops for a human;
  * the mission budget is a blast-radius cap, refusing the campaign that would take the
    mission past the number of human beings a human authorized it to touch;
  * the relay would CHEERFULLY double-send. test_the_relay_will_double_send_on_a_new_key
    is the counterexample that makes every other test in this file mean something: the
    external system has no per-recipient uniqueness, so the only thing standing between a
    crash and a second copy is the derived idempotency key.

Harness note: tests/conftest.py sets AXIOM_OFFLINE, a one-second lease and zero provider
latency BEFORE axiom.config is imported, and pytest imports it first. Those settings
apply here too — the sleeps in this file are real lease expiries, not mocks.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest

from axiom import db, embeddings, memory, policy as policy_mod, tasks
from axiom.config import SYSTEM_TENANT, settings
from axiom.domains import for_task_type, known
from axiom.domains import broadcast as bc
from axiom.domains import relay
from axiom.domains.runtime import DomainWorker
from axiom.models import AttemptState, MemoryClass, Outcome, TaskState, Trust
from axiom.provider import ProviderError
from axiom.risk import (
    COMMS_RECIPIENTS, MONEY_USD_CENTS, Grant, Reversibility, Risk, measure,
)
from axiom.tasks import BudgetExceeded, FingerprintMismatch, LeaseLost

DOMAIN = bc.DOMAIN
STEP = DOMAIN.step_name
POLICY_ID = DOMAIN.policy_id


# ============================================================================= world

class BWorld:
    """One tenant, one broadcast mission, one policy whose ceiling counts PEOPLE.

    Thin on purpose, like tests/conftest.py's World: each test calls the engine directly
    through db.tx so it reads as a specification of the protocol rather than of a helper.
    """

    def __init__(self, tenant_id: uuid.UUID, mission_id: uuid.UUID,
                 budget_recipients: int, ceiling_recipients: int):
        self.tenant_id = tenant_id
        self.mission_id = mission_id
        self.budget = budget_recipients
        self.ceiling = ceiling_recipients
        self.agent_ids: list[uuid.UUID] = []
        self.campaign_refs: list[str] = []

    # ------------------------------------------------------------------ construction

    def agent(self, ref: str | None = None) -> uuid.UUID:
        aid = db.tx(lambda cur: tasks.register_agent(
            cur, worker_ref=ref or f'test-bc-{uuid.uuid4().hex[:10]}', shards=[]))
        self.agent_ids.append(aid)
        return aid

    def enqueue(self, *, audience: int = 1_000, suppressed: int = 0,
                description: str = 'newsletter for the opted-in subscriber list',
                kind: str = 'promotional_blast', task_type: str = 'broadcast',
                max_attempts: int = 5) -> dict:
        # A fresh campaign ref per task. The relay ledger is append-only and shared
        # across the whole session, so tests scope every audit to their own campaigns
        # instead of resetting a table another test may be mid-way through.
        ref = f'CMP-T-{uuid.uuid4().hex[:12].upper()}'
        self.campaign_refs.append(ref)
        payload = {'campaign_ref': ref, 'description': description,
                   'campaign_kind': kind, 'segment': 'test_segment',
                   'recipient_count': audience, 'suppressed_count': suppressed}

        def _apply(cur):
            tid = tasks.enqueue(cur, tenant_id=self.tenant_id, mission_id=self.mission_id,
                                task_type=task_type,
                                dedupe_key=f'campaign:{ref}:{task_type}',
                                payload=payload, max_attempts=max_attempts,
                                actor='system:test')
            # Backdated so this row sorts first in the claim order; claim() is not
            # tenant-scoped and a cluster holding demo data would otherwise hand back
            # somebody else's task.
            cur.execute("UPDATE axiom_task SET available_at = now() - INTERVAL '30 days' "
                        "WHERE id = %s", (str(tid),))
            return tid
        tid = db.tx(_apply)
        assert tid is not None, 'fixture enqueue was deduped; campaign_ref collision'
        return {'id': tid, 'ref': ref, 'payload': payload}

    # -------------------------------------------------------------------- the protocol

    def claim(self, agent_id: uuid.UUID, task_id: uuid.UUID) -> tasks.Claimed:
        c = db.tx(lambda cur: tasks.claim(cur, agent_id=agent_id, task_id=task_id))
        assert c is not None, f'could not claim {task_id}'
        return c

    def prepare(self, claimed: tasks.Claimed, agent_id: uuid.UUID, job: dict,
                *, intent=None) -> tasks.PrepareResult:
        payload = job['payload']
        intent = intent or DOMAIN.triage(payload)
        body = DOMAIN.request_body(payload, intent)
        return db.tx(lambda cur: tasks.prepare(
            cur, task=claimed, agent_id=agent_id, step_name=STEP,
            provider_name=DOMAIN.provider_name, operation=DOMAIN.operation,
            request_body=body,
            # Ask in RECIPIENTS. This passed only amount_cents until prepare() learned to
            # take a risk descriptor, which meant the decision that gated a send reached
            # the policy through the money bridge — the thing domains/__init__ called "a
            # column-naming lie". A policy that grants recipients now correctly refuses to
            # authorize money, so a caller that still asks in dollars gets parked, and
            # this helper asking properly is what makes these tests test the real path.
            risk=DOMAIN.risk.descriptor(intent.risk_units, intent.reason),
            amount_cents=intent.risk_units,
            currency=DOMAIN.risk.code, policy_id=POLICY_ID))

    def recover(self, claimed: tasks.Claimed, agent_id: uuid.UUID,
                situation: str = 'promotional_blast: newsletter for the opted-in list'):
        return db.tx(lambda cur: tasks.recover(
            cur, task=claimed, agent_id=agent_id, step_name=STEP,
            situation_embedding=embeddings.embed_list(situation)))

    def settle(self, claimed: tasks.Claimed, agent_id: uuid.UUID,
               receipt: tasks.Receipt, effect, *, first_try: bool = True) -> uuid.UUID:
        content = DOMAIN.settled_memory(
            situation='promotional_blast: test campaign',
            idempotency_key=receipt.idempotency_key,
            risk_units=receipt.amount_cents or 0, effect=effect, first_try=first_try)
        return db.tx(lambda cur: tasks.settle(
            cur, task=claimed, agent_id=agent_id, receipt=receipt,
            outcome_state=AttemptState.SUCCEEDED, task_state=TaskState.SUCCEEDED,
            response_body=effect.body, provider_ref=effect.ref, http_status=effect.status,
            memory_content=content, memory_embedding=embeddings.embed_list(content),
            memory_outcome=Outcome.RESOLVED,
            result={'provider_ref': effect.ref, 'replayed': effect.replayed}))

    @staticmethod
    def lease_expires() -> None:
        import time
        time.sleep(settings.lease_seconds + 0.35)

    # ------------------------------------------------------------------------ reading

    def rows(self, sql: str, params=()) -> list[dict]:
        def _q(cur):
            cur.execute(sql, tuple(params))
            return cur.fetchall()
        return db.tx(_q, readonly=True)

    def task_row(self, task_id: uuid.UUID) -> dict:
        return self.rows('SELECT * FROM axiom_task WHERE id = %s', (str(task_id),))[0]

    def receipts(self, task_id: uuid.UUID) -> list[dict]:
        return self.rows('SELECT * FROM axiom_action_attempt WHERE task_id = %s '
                         'ORDER BY step_seq', (str(task_id),))

    def spent(self) -> tuple[int, int]:
        r = self.rows('SELECT spent_cents, budget_cents FROM axiom_mission WHERE id = %s',
                      (str(self.mission_id),))[0]
        return int(r['spent_cents']), int(r['budget_cents'])

    def remember(self, content: str, *, outcome: Outcome = Outcome.RESOLVED,
                 context_key: str = 'state:ACTION_PREPARED',
                 memory_class: MemoryClass = MemoryClass.EPISODIC) -> uuid.UUID:
        vec = embeddings.embed_list(content)
        return db.tx(lambda cur: memory.write(
            cur, tenant_id=self.tenant_id, memory_class=memory_class,
            context_key=context_key, content=content, embedding=vec, outcome=outcome,
            source='system:execution', trust_level=Trust.FIRST_PARTY,
            actor='system:test'))

    # the relay, scoped to this world's campaigns only
    def relay_stats(self) -> dict:
        return relay.stats(self.campaign_refs)

    def duplicates(self) -> list[dict]:
        return relay.duplicate_recipients(self.campaign_refs)

    def deliveries(self, ref: str) -> int:
        with relay.pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT count(*) AS n FROM relay_delivery WHERE campaign_ref = %s',
                            (ref,))
                return int(cur.fetchone()['n'])

    def sends(self, ref: str) -> list[dict]:
        return relay.ledger(ref)


def _create(budget_recipients: int, ceiling_recipients: int,
            requires_approval: bool = False) -> BWorld:
    tenant_id = uuid.uuid4()

    def _apply(cur):
        cur.execute("INSERT INTO axiom_tenant (id, slug, display_name) VALUES (%s, %s, %s)",
                    (str(tenant_id), f'bctest-{tenant_id.hex[:12]}', 'domain 2 suite'))
        policy_mod.publish(
            cur, tenant_id=tenant_id, policy_id=POLICY_ID, version=1,
            body={'description': 'test broadcast authority', 'risk_axis': 'recipients',
                  'max_auto_action_recipients': ceiling_recipients},
            # The clause that says what it means (db/004_risk.sql)...
            risk_grants=[Grant(COMMS_RECIPIENTS, ceiling_recipients,
                               Reversibility.IRREVERSIBLE)],
            # ...and the same number in the money column. It no longer DECIDES anything —
            # prepare() asks in recipients now — but it is kept equal so that
            # test_the_policy_says_the_same_thing_in_both_vocabularies still has two
            # vocabularies to compare, and so a pre-004 reader of this row is not misled.
            max_auto_action_cents=ceiling_recipients, requires_approval=requires_approval,
            created_by='human:test@axiom.invalid', activate=True)
        return tasks.create_mission(
            cur, tenant_id=tenant_id, title='domain 2 suite',
            goal='prove the crash windows hold when the risk axis is people',
            budget_cents=budget_recipients, created_by='human:test@axiom.invalid')

    mission_id = db.tx(_apply)
    return BWorld(tenant_id, mission_id, budget_recipients, ceiling_recipients)


def _destroy(w: BWorld) -> None:
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
            cur.execute('DELETE FROM axiom_event WHERE tenant_id = %s '
                        'AND subject_id = ANY(%s::UUID[])', (str(SYSTEM_TENANT), ids))
            cur.execute('DELETE FROM axiom_agent WHERE id = ANY(%s::UUID[])', (ids,))
    db.tx(_wipe)
    # The relay ledger is left alone. Its rows are the evidence, they are scoped by
    # campaign ref, and deleting them would make a failing run harder to diagnose.


@pytest.fixture
def bworld() -> BWorld:
    """Ceiling 2,000 recipients, budget 50,000 — the acting path, uninterrupted."""
    w = _create(budget_recipients=50_000, ceiling_recipients=2_000)
    try:
        yield w
    finally:
        _destroy(w)


@pytest.fixture
def tight_budget() -> BWorld:
    """Budget 5,000 recipients: the blast-radius cap bites on the second campaign."""
    w = _create(budget_recipients=5_000, ceiling_recipients=100_000)
    try:
        yield w
    finally:
        _destroy(w)


def dispatch(receipt: tasks.Receipt, *, body: dict | None = None,
             chaos_pre: float = 0.0, chaos_post: float = 0.0):
    """Exactly what the runtime does to the outside world, and nothing else.

    `body` defaults to the receipt's stored request body. Overriding it is how the W7
    test forges different copy under an existing key.
    """
    b = body if body is not None else receipt.request_body
    r = relay.send(idempotency_key=receipt.idempotency_key,
                   campaign_ref=b['campaign_ref'], segment=b.get('segment', 'all'),
                   recipient_count=receipt.amount_cents or 0, request_body=b,
                   latency_ms=0, chaos_pre=chaos_pre, chaos_post=chaos_post)
    from axiom.domains import Effect
    return Effect(ref=r['id'], status=r['http_status'], body=r, replayed=r['replayed'])


# ======================================================================== the seam

def test_registry_carries_both_workloads():
    assert set(known()) >= {'refund', 'broadcast'}
    assert for_task_type('refund').name == 'refunds'
    assert for_task_type('broadcast').name == 'broadcast'
    # An unknown workload is a real answer, not an exception: the runtime hands the task
    # back rather than guessing what to do with it.
    assert for_task_type('provision_cluster') is None


def test_refund_domain_still_says_exactly_what_worker_py_says():
    """The extraction must be a refactor. These are the constants axiom/worker.py passes
    to tasks.prepare() on every refund; if any of them drifted, the refund flow expressed
    through the seam would be a different flow wearing the same name."""
    d = for_task_type('refund')
    assert d.step_name == 'refund'
    assert d.provider_name == 'payments'
    assert d.operation == 'refunds.create'
    assert d.policy_id == 'refund_authority'
    assert (d.risk.unit, d.risk.code) == ('cents', 'USD')

    payload = {'order_ref': 'ORD-1', 'amount_cents': 30_000,
               'description': 'customer charged twice for order',
               'exception_kind': 'duplicate_charge'}
    intent = d.triage(payload)
    assert (intent.action, intent.acts, intent.risk_units) == ('refund', True, 30_000)
    assert d.request_body(payload, intent) == {
        'order_ref': 'ORD-1', 'amount_cents': 30_000, 'currency': 'USD',
        'reason': 'duplicate_charge'}
    # The recovery situation is rebuilt from the payload alone — no second triage.
    assert d.recovery_situation(payload) == 'duplicate_charge: customer charged twice for order'


def test_broadcast_triage_speaks_its_own_vocabulary_and_trims_the_blast_radius():
    d = DOMAIN
    send = d.triage({'campaign_ref': 'C1', 'description': 'flash sale announcement',
                     'recipient_count': 5_000, 'suppressed_count': 400})
    assert (send.action, send.acts) == ('send', True)
    # THE number the policy will judge: audience minus suppression, computed from the
    # payload rather than proposed by the model.
    assert send.risk_units == 4_600

    suppress = d.triage({'campaign_ref': 'C2', 'recipient_count': 9_000,
                         'description': 'promo blast to a purchased list with no consent '
                                        'on record'})
    assert (suppress.action, suppress.acts, suppress.risk_units) == ('suppress', False, 0)
    assert suppress.terminal_state == TaskState.SUCCEEDED

    hold = d.triage({'campaign_ref': 'C3', 'recipient_count': 40,
                     'description': 'price increase notice with contract change language'})
    assert (hold.action, hold.acts) == ('hold', False)
    assert hold.terminal_state == TaskState.DEAD_LETTER

    # An unclassifiable brief must NEVER default to sending.
    unknown = d.triage({'campaign_ref': 'C4', 'recipient_count': 12_000,
                        'description': 'zzzz'})
    assert (unknown.action, unknown.acts) == ('hold', False)


# ================================================================= the authority axis

def test_the_measurer_sizes_a_send_from_the_request_body():
    """The agent submits a request; the SYSTEM decides how big that request is.

    Second operation AXIOM knows how to size, after refunds.create. The derivation runs
    on the body that is already fingerprinted into the receipt, so an understated blast
    radius is a query against the journal rather than a theory.
    """
    body = {'campaign_ref': 'CMP-9', 'segment': 'active', 'recipient_count': 4_600,
            'template_sha': 'abc', 'reason': 'promotional_blast'}
    r = measure('messages.send', body)
    assert r.measurements == Risk.of(COMMS_RECIPIENTS, 4_600,
                                     reversibility=Reversibility.IRREVERSIBLE).measurements
    assert r.reversibility == Reversibility.IRREVERSIBLE, 'you cannot un-send an email'
    # And the domain's own axis produces the identical descriptor.
    assert DOMAIN.risk.descriptor(4_600).measurements == r.measurements


def test_the_policy_says_the_same_thing_in_both_vocabularies(bworld: BWorld):
    """A recipients policy decides about recipients — and refuses to decide about money.

    This test used to assert the opposite of its second half. It pinned
    `pol.authorizes(1000) is True` — an INT, which the model reads as money.usd_cents —
    on a policy whose only grant is comms.recipients, and called that "the two
    vocabularies agreeing". They were not agreeing; a money grant was being injected into
    every policy behind the decision, so money was the one unit that could never be
    ungoverned. A policy that had never said a word about dollars self-authorized
    irreversible dollar movement up to a ceiling it had inherited by accident.

    Now `effective_grants` synthesizes the money ceiling ONLY for policies that state no
    grants at all — every pre-004 row, which must keep deciding exactly as it did. A
    policy fluent in the general vocabulary is taken at its word, including its silences.
    """
    pol = db.tx(lambda cur: policy_mod.active(
        cur, tenant_id=bworld.tenant_id, policy_id=POLICY_ID))

    over, under = 4_600, 1_000
    # In its own unit it decides, both ways.
    assert pol.decide(DOMAIN.risk.descriptor(over)).authorized is False
    assert pol.decide(DOMAIN.risk.descriptor(under)).authorized is True

    # Asked in DOLLARS, this policy has no opinion — and no opinion is a refusal, not a
    # default-allow. A bare int is money, so both of these are correctly refused even
    # though 1,000 is under the recipient ceiling: it is not a number of recipients.
    assert pol.authorizes(over) is False
    assert pol.authorizes(under) is False

    # And the rule that makes the general model safe: a unit the policy never granted is
    # a refusal, not a default-allow. This tenant's policy has nothing to say about rows.
    assert pol.decide(Risk.of('data.rows', 1,
                              reversibility=Reversibility.IRREVERSIBLE)).authorized is False


def test_a_refund_policy_has_no_opinion_about_email_and_says_no():
    """The failure the money-shaped model could not even express.

    A policy written for refunds — dollars only — shown a 40,000-recipient send. Under the
    pre-004 model amount_cents would be NULL, `NULL <= 20000` vacuously true, and the
    agent would have self-authorized the largest act in this repo. Under grants it parks.
    """
    refund_only = policy_mod.Policy(
        policy_id='refund_authority', version=1, body={},
        max_auto_action_cents=20_000, requires_approval=False,
        content_sha256='x', signature=None, signed_by=None)

    assert refund_only.authorizes(150_00) is True             # a $150 refund: fine
    assert refund_only.authorizes(300_00) is False            # $300 is over the ceiling
    blast = Risk.of(COMMS_RECIPIENTS, 40_000,
                    reversibility=Reversibility.IRREVERSIBLE)
    assert refund_only.decide(blast).authorized is False
    assert any(MONEY_USD_CENTS != u.unit for u in blast.measurements)


def test_the_ceiling_counts_people_not_dollars(bworld: BWorld):
    """A 4,600-recipient send costs about two cents and stops for a human anyway.

    This is the entire argument for a non-money risk axis in one assertion: under a
    money-shaped policy the SES cost of this campaign clears a $200 ceiling by four
    orders of magnitude, and the campaign is the more damaging of the two actions.
    """
    aid = bworld.agent()
    job = bworld.enqueue(audience=5_000, suppressed=400)
    claimed = bworld.claim(aid, job['id'])
    result = bworld.prepare(claimed, aid, job)

    assert result.parked, 'a 4,600-recipient send must not be authorized unattended'
    assert result.receipt is None
    assert bworld.task_row(job['id'])['state'] == str(TaskState.AWAITING_APPROVAL)

    pending = db.tx(lambda cur: tasks.pending_approvals(cur, tenant_id=bworld.tenant_id))
    assert len(pending) == 1
    # The approval carries the quantity in the money-named column. Asserted so the
    # smuggling is visible in a test rather than only in a comment.
    assert pending[0]['proposed_amount_cents'] == 4_600
    # Nothing left the building.
    assert bworld.deliveries(job['ref']) == 0


def test_under_the_ceiling_the_machine_acts_alone(bworld: BWorld):
    aid = bworld.agent()
    job = bworld.enqueue(audience=1_200, suppressed=200)
    claimed = bworld.claim(aid, job['id'])
    result = bworld.prepare(claimed, aid, job)

    assert not result.parked
    r = result.receipt
    assert r.amount_cents == 1_000
    # The unit code rides in axiom_action_attempt.currency, which is STRING(3) and named
    # for money. 'RCP' is not a currency; the column should be risk_unit.
    assert r.currency == 'RCP'
    assert r.provider == 'relay' and r.operation == 'messages.send'


def test_the_mission_budget_is_a_blast_radius_cap(tight_budget: BWorld):
    """5,000 people authorized for the whole mission. The third campaign is refused."""
    w = tight_budget
    aid = w.agent()

    for expected in (2_000, 4_000):
        job = w.enqueue(audience=2_000)
        claimed = w.claim(aid, job['id'])
        assert not w.prepare(claimed, aid, job).parked
        assert w.spent()[0] == expected

    job = w.enqueue(audience=2_000)
    claimed = w.claim(aid, job['id'])
    with pytest.raises(BudgetExceeded) as e:
        w.prepare(claimed, aid, job)
    assert '5000' in str(e.value)
    # Refused BEFORE any receipt existed, so no send could ever have been authorized.
    assert w.receipts(job['id']) == []
    assert w.deliveries(job['ref']) == 0
    assert w.spent() == (4_000, 5_000)


# =================================================================== the crash windows

def test_w2_crash_before_the_send_leaves_nobody_messaged(bworld: BWorld):
    """Died after PREPARE, before anything left. The receipt exists; no one was emailed.

    The commit ORDER is what makes this knowable rather than hopeful: the receipt commits
    first and only then may a call go out, so a crash before the send provably caused no
    effect.
    """
    aid = bworld.agent()
    job = bworld.enqueue(audience=900)
    claimed = bworld.claim(aid, job['id'])
    receipt = bworld.prepare(claimed, aid, job).receipt

    with pytest.raises(relay.RelayCrash):
        dispatch(receipt, chaos_pre=1.0)

    assert bworld.deliveries(job['ref']) == 0
    assert bworld.sends(job['ref']) == []
    assert bworld.task_row(job['id'])['state'] == str(TaskState.ACTION_PREPARED)

    # A successor claims it after the lease lapses and recovers.
    bworld.lease_expires()
    aid2 = bworld.agent()
    claimed2 = bworld.claim(aid2, job['id'])
    assert claimed2.is_recovery
    plan = bworld.recover(claimed2, aid2)
    assert plan.action == 'RESEND'
    assert plan.receipt.idempotency_key == receipt.idempotency_key

    effect = dispatch(plan.receipt)
    assert effect.replayed is False            # nothing had been sent, so this creates
    bworld.settle(claimed2, aid2, plan.receipt, effect, first_try=False)

    assert bworld.deliveries(job['ref']) == 900
    assert len(bworld.sends(job['ref'])) == 1
    assert bworld.duplicates() == []


def test_w4_crash_after_the_send_does_not_message_anyone_twice(bworld: BWorld):
    """Died after 900 people already had it, before AXIOM recorded anything.

    The dangerous window. Recovery re-sends under the SAME derived key and the relay
    hands back the original send instead of putting a second copy in 900 inboxes.
    """
    aid = bworld.agent()
    job = bworld.enqueue(audience=900)
    claimed = bworld.claim(aid, job['id'])
    receipt = bworld.prepare(claimed, aid, job).receipt

    with pytest.raises(relay.RelayCrash):
        dispatch(receipt, chaos_post=1.0)

    # The effect is REAL and AXIOM does not know it.
    assert bworld.deliveries(job['ref']) == 900
    assert bworld.task_row(job['id'])['state'] == str(TaskState.ACTION_PREPARED)
    assert bworld.receipts(job['id'])[0]['attempt_state'] == str(AttemptState.PREPARED)

    bworld.lease_expires()
    aid2 = bworld.agent()
    claimed2 = bworld.claim(aid2, job['id'])
    plan = bworld.recover(claimed2, aid2)
    assert plan.action == 'RESEND'

    effect = dispatch(plan.receipt)
    assert effect.replayed is True, 'the relay must recognize the key, not re-send'
    bworld.settle(claimed2, aid2, plan.receipt, effect, first_try=False)

    assert bworld.deliveries(job['ref']) == 900, 'a second copy went out'
    sends = bworld.sends(job['ref'])
    assert len(sends) == 1 and sends[0]['replay_count'] == 1
    assert bworld.duplicates() == []
    assert bworld.task_row(job['id'])['state'] == str(TaskState.SUCCEEDED)


def test_w7_different_copy_under_the_same_key_is_refused(bworld: BWorld):
    """A recovered agent that re-writes the message is a NEW intent wearing an OLD key.

    In a refund the forged field is an amount. Here it is the copy itself, which is why
    template_sha is in the request body: 'same key, same people, same words' is the thing
    the fingerprint has to mean.
    """
    aid = bworld.agent()
    job = bworld.enqueue(audience=500)
    claimed = bworld.claim(aid, job['id'])
    receipt = bworld.prepare(claimed, aid, job).receipt
    dispatch(receipt)                                   # the real send lands

    forged = dict(receipt.request_body)
    forged['template_sha'] = hashlib.sha256(b'rewritten subject line').hexdigest()[:16]

    # The engine's own guard fires before anything is sent...
    with pytest.raises(FingerprintMismatch):
        tasks.verify_fingerprint(receipt, forged)

    # ...and the relay refuses it independently, which is what makes the guard a
    # backstop rather than the only defence.
    with pytest.raises(ProviderError) as e:
        dispatch(receipt, body=forged)
    assert e.value.status == 409 and e.value.retryable is False

    assert bworld.deliveries(job['ref']) == 500
    assert bworld.duplicates() == []


def test_a_zombie_cannot_settle_a_send_the_successor_owns(bworld: BWorld):
    """The fence, not the lease, is the correctness mechanism.

    A worker frozen inside the relay call comes back to find the task reassigned. Its
    settle must be refused, and the campaign must still have gone out exactly once.
    """
    aid = bworld.agent()
    job = bworld.enqueue(audience=700)
    zombie = bworld.claim(aid, job['id'])
    receipt = bworld.prepare(zombie, aid, job).receipt
    effect = dispatch(receipt)                          # the send lands, then we stall

    bworld.lease_expires()
    aid2 = bworld.agent()
    successor = bworld.claim(aid2, job['id'])
    assert successor.lease_epoch > zombie.lease_epoch

    with pytest.raises(LeaseLost):
        bworld.settle(zombie, aid, receipt, effect)

    # The legitimate successor finishes the job, under the same key.
    plan = bworld.recover(successor, aid2)
    assert plan.action == 'RESEND'
    effect2 = dispatch(plan.receipt)
    assert effect2.replayed is True
    bworld.settle(successor, aid2, plan.receipt, effect2, first_try=False)

    assert bworld.deliveries(job['ref']) == 700
    assert bworld.duplicates() == []


def test_memory_can_veto_a_resend_but_only_toward_escalation(bworld: BWorld):
    """The fused transaction reads the receipt AND recalls, in one commit.

    Seed the tenant's episodic memory with recoveries that ended badly and the same
    live receipt now produces ESCALATE instead of RESEND. Memory may only ever talk the
    system OUT of an act, never into one.
    """
    situation = ('agent died mid-broadcast on a promotional_blast task; the segment '
                 'received the campaign twice')
    for _ in range(3):
        bworld.remember(situation, outcome=Outcome.DUPLICATE_EFFECT)

    aid = bworld.agent()
    job = bworld.enqueue(audience=800)
    claimed = bworld.claim(aid, job['id'])
    receipt = bworld.prepare(claimed, aid, job).receipt
    with pytest.raises(relay.RelayCrash):
        dispatch(receipt, chaos_post=1.0)

    bworld.lease_expires()
    aid2 = bworld.agent()
    claimed2 = bworld.claim(aid2, job['id'])
    plan = db.tx(lambda cur: tasks.recover(
        cur, task=claimed2, agent_id=aid2, step_name=STEP,
        situation_embedding=embeddings.embed_list(situation)))

    assert plan.action == 'ESCALATE'
    assert plan.evidence_ids, 'an escalation with no evidence is just a guess'
    assert bworld.deliveries(job['ref']) == 800     # still exactly one copy each


# ================================================== the key, and the counterexample

def test_the_idempotency_key_knows_nothing_about_money(bworld: BWorld):
    """Generated by the database from (tenant, task, step, step_seq).

    No amount, no currency, no workload name — which is precisely why a second domain
    needed no schema change to get the same guarantee. Recomputed here rather than
    trusted, because a key derived at call time is the single most lethal bug in this
    class of system.
    """
    aid = bworld.agent()
    job = bworld.enqueue(audience=640)
    claimed = bworld.claim(aid, job['id'])
    receipt = bworld.prepare(claimed, aid, job).receipt

    raw = f'{bworld.tenant_id}:{job["id"]}:{STEP}:1'
    expected = 'axm_' + hashlib.sha256(raw.encode()).hexdigest()[:48]
    assert receipt.idempotency_key == expected
    assert str(receipt.amount_cents) not in receipt.idempotency_key


def test_the_relay_will_double_send_on_a_new_key(bworld: BWorld):
    """THE COUNTEREXAMPLE. Without a stable derived key, everyone gets it twice.

    The relay has no unique constraint on (campaign, recipient) — a real ESP does not
    have one either — so this test double-messages 300 people on purpose. It is what
    makes every other assertion in this file mean something: the external system is not
    quietly doing AXIOM's job.
    """
    ref = f'CMP-DUP-{uuid.uuid4().hex[:10].upper()}'
    bworld.campaign_refs.append(ref)
    body = {'campaign_ref': ref, 'segment': 'test', 'recipient_count': 300,
            'template_sha': 'deadbeefdeadbeef', 'reason': 'promotional_blast'}

    for _ in range(2):
        # A fresh key each time — exactly what an agent that re-plans from a transcript
        # instead of a receipt would produce.
        relay.send(idempotency_key='axm_' + uuid.uuid4().hex, campaign_ref=ref,
                   segment='test', recipient_count=300, request_body=body,
                   latency_ms=0, chaos_pre=0.0, chaos_post=0.0)

    assert bworld.deliveries(ref) == 600
    dupes = relay.duplicate_recipients([ref])
    assert len(dupes) == 300
    assert all(d['deliveries'] == 2 for d in dupes)


# ============================================================ the heterogeneous queue

def test_a_broadcast_worker_hands_back_another_workload_untouched(bworld: BWorld):
    """tasks.claim() has no task_type predicate, so this WILL happen on a shared queue.

    The release must leave the row exactly as it found it apart from the fence, which is
    crash window W1 — the one window in which no external effect can exist.
    """
    aid_owner = bworld.agent()
    job = bworld.enqueue(audience=100, task_type='refund',
                         description='customer charged twice for order')

    w = DomainWorker(DOMAIN, worker_ref=f'test-release-{uuid.uuid4().hex[:8]}')
    w.agent_id = aid_owner
    bworld.agent_ids.append(aid_owner)

    claimed = bworld.claim(aid_owner, job['id'])
    assert claimed.task_type == 'refund'
    before = bworld.task_row(job['id'])
    assert before['state'] == str(TaskState.LEASED)

    w._release_foreign(claimed)

    after = bworld.task_row(job['id'])
    assert after['state'] == str(TaskState.READY)
    assert after['lease_owner'] is None
    assert after['attempt'] == before['attempt'], 'a handback is not a failed attempt'
    assert bworld.receipts(job['id']) == [], 'nothing was prepared for a foreign task'
    assert w.foreign_releases == 1

    # And the journal says so, so an operator can see where the wasted work went.
    events = [r['event_type'] for r in bworld.rows(
        "SELECT event_type FROM axiom_event WHERE task_id = %s ORDER BY seq",
        (str(job['id']),))]
    assert events[-1] == 'task.released'


def test_a_mid_flight_foreign_task_keeps_its_owner(bworld: BWorld):
    """axiom_task_lease_ck makes 'ACTION_PREPARED with no owner' unrepresentable.

    So a worker of the wrong domain cannot disown a task that is mid-flight; the most it
    may do is make it immediately claimable and let the fence sort out the succession.
    That is exactly the state a crashed worker leaves behind, which the engine already
    knows how to handle. The first version of _release_foreign() set lease_owner = NULL
    unconditionally and took a CHECK violation the first time it met one of these.
    """
    aid = bworld.agent()
    job = bworld.enqueue(audience=250)
    claimed = bworld.claim(aid, job['id'])
    bworld.prepare(claimed, aid, job)          # -> ACTION_PREPARED, live receipt

    bworld.lease_expires()
    aid2 = bworld.agent()
    stranger = bworld.claim(aid2, job['id'])
    assert stranger.state == TaskState.ACTION_PREPARED

    w = DomainWorker(DOMAIN, worker_ref=f'test-release2-{uuid.uuid4().hex[:8]}')
    w.agent_id = aid2
    w._release_foreign(stranger)               # pretend this worker cannot handle it

    row = bworld.task_row(job['id'])
    assert row['state'] == str(TaskState.ACTION_PREPARED)
    assert row['lease_owner'] is not None, 'CHECK axiom_task_lease_ck forbids orphaning'
    # Still recoverable by the next worker that can actually do the job.
    assert bworld.claim(bworld.agent(), job['id']).is_recovery
