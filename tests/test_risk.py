"""AXIOM :: the authority model is not money-shaped, and the money case did not move.

Two obligations, and they pull against each other, which is why they are tested together
in one file rather than "the new feature" over here and "regression" over there.

    1. A policy must be able to refuse an act it has no dollar figure for — deleting
       40,000 records, emailing 40,000 people, dropping a production database.
    2. Every policy written before db/004_risk.sql must decide EXACTLY what it decided
       before. Not approximately. The live demo, the chaos script and 92 other tests all
       run through `Policy.authorizes(amount_cents)`.

The test that carries the most weight is `test_a_policy_authored_for_one_domain_never_
authorizes_another`. Everything else here is a property of the design; that one is the
property that makes the design safe, because the failure it forbids is silent. A ceiling
denominated in dollars, shown an act denominated in recipients, does not error and does
not warn — it evaluates `NULL <= 20000`, says yes, and the email goes out.
"""

from __future__ import annotations

import json

import psycopg
import pytest

from axiom import db, policy as policy_mod, risk
from axiom.risk import (
    Grant, MalformedGrant, MalformedRisk, Measurement, Reversibility, Risk,
    UnmeasuredAction,
)

from conftest import POLICY_ID

REV, COMP, IRR = (Reversibility.REVERSIBLE, Reversibility.COMPENSABLE,
                  Reversibility.IRREVERSIBLE)

# Units that have nothing to do with money, used to prove the point.
RECIPIENTS = risk.COMMS_RECIPIENTS
ROWS = risk.DATA_ROWS
SUBJECTS = risk.DATA_SUBJECTS
DATABASES = risk.INFRA_PRODUCTION_RESOURCES


def make_policy(*, max_cents: int = 0, requires_approval: bool = False,
                grants: tuple[Grant, ...] = ()) -> policy_mod.Policy:
    """A Policy built in memory. The DB round trip is tested separately, on purpose:
    the decision logic should be assertable without a cluster, and the persistence
    should be assertable without re-deriving the decision logic."""
    return policy_mod.Policy(
        policy_id='test_authority', version=1, body={}, max_auto_action_cents=max_cents,
        requires_approval=requires_approval, content_sha256='x', signature=None,
        signed_by=None, risk_grants=grants)


# =================================================== 1. THE MONEY CASE DID NOT MOVE

@pytest.mark.parametrize('amount, ceiling, expected', [
    (None,      20000, True),    # a step that moves no money: `(None or 0) <= max`
    (0,         20000, True),
    (1,         20000, True),
    (19999,     20000, True),
    (20000,     20000, True),    # the boundary is inclusive, and it stays inclusive
    (20001,     20000, False),
    (300_00,    20000, False),   # the $300 refund the README is about
    (None,          0, True),    # a policy that authorizes nothing still allows nothing
    (1,             0, False),
])
def test_the_refund_path_decides_exactly_what_it_decided_before(amount, ceiling, expected):
    """The pre-004 rule, reproduced value for value.

    This is deliberately a table and not a property: the old implementation was one line,
    `(amount_cents or 0) <= self.max_auto_action_cents`, and the only useful assertion is
    that the general model agrees with it at every boundary that line had.
    """
    assert make_policy(max_cents=ceiling).authorizes(amount) is expected


def test_requires_approval_still_beats_everything():
    """The operator's kill switch. No grant, in any unit, may talk past it."""
    p = make_policy(max_cents=10_000_00, requires_approval=True,
                    grants=(Grant(ROWS, 1_000_000, REV),))
    assert p.authorizes(1) is False
    assert p.authorizes(Risk.of(ROWS, 1, reversibility=REV)) is False
    assert p.decide(1).grounds == ('requires_approval',)


def test_the_money_ceiling_is_a_grant_and_not_a_branch():
    """`max_auto_action_cents` must be visible in the general vocabulary, or the claim
    that dollars are one unit among several is a comment rather than a fact."""
    grants = make_policy(max_cents=20000).effective_grants
    assert Grant(risk.MONEY_USD_CENTS, 20000, IRR) in grants
    # ...and the synthesized grant must NOT gate on reversibility, because the model it
    # translates had no such gate. Inventing one would retroactively tighten every
    # already-published policy.
    money = [g for g in grants if g.unit == risk.MONEY_USD_CENTS]
    assert [g.max_reversibility for g in money] == [IRR]


def test_a_negative_amount_no_longer_slips_under_the_ceiling():
    """The one deliberate difference from pre-004 behaviour, asserted so it is a decision.

    `-500000 <= 20000` was true, so a sign error upstream did not merely evade the
    ceiling by a little — it evaded it by any amount at all. Magnitudes are unsigned, so
    the same action is now judged at 500000 and parks on a human.
    """
    p = make_policy(max_cents=20000)
    assert p.authorizes(-500_000) is False
    assert p.authorizes(-5_000) is True      # still under the ceiling by magnitude


# ========================================== 2. AN ACT WITH NO DOLLAR FIGURE AT ALL

def test_a_non_money_action_over_the_ceiling_requires_approval():
    """40,000 recipients under a policy that self-sends to 500."""
    p = make_policy(grants=(Grant(RECIPIENTS, 500, IRR),))
    d = p.decide(Risk.of(RECIPIENTS, 40_000, reversibility=IRR,
                         description='win-back campaign'))
    assert d.authorized is False
    assert d.grounds == ('magnitude',)
    assert '40000' in d.reason and RECIPIENTS in d.reason


def test_the_same_action_at_a_smaller_magnitude_does_not():
    """Same operation, same permanence, same policy — only the number changed.

    This pair is the whole argument for magnitude being a first-class input. "Send email"
    is not a risk level; 200 recipients and 40,000 recipients are.
    """
    p = make_policy(grants=(Grant(RECIPIENTS, 500, IRR),))
    assert p.authorizes(Risk.of(RECIPIENTS, 200, reversibility=IRR)) is True
    assert p.authorizes(Risk.of(RECIPIENTS, 500, reversibility=IRR)) is True
    assert p.authorizes(Risk.of(RECIPIENTS, 501, reversibility=IRR)) is False


def test_an_irreversible_act_is_gated_more_tightly_than_a_reversible_one_of_the_same_size():
    """Identical unit, identical magnitude, opposite answers.

    A policy that soft-deletes ten thousand rows without asking and hard-deletes a
    hundred and one only with a human. No single dollar ceiling can express this, and no
    single risk score can either without secretly picking an exchange rate between size
    and permanence.
    """
    p = make_policy(grants=(Grant(ROWS, 10_000, REV), Grant(ROWS, 100, IRR)))

    assert p.authorizes(Risk.of(ROWS, 500, reversibility=REV)) is True
    assert p.authorizes(Risk.of(ROWS, 500, reversibility=IRR)) is False

    # And the reason names the ceiling that actually applies at this permanence — 100,
    # not 10000 — or the operator edits the wrong number.
    d = p.decide(Risk.of(ROWS, 500, reversibility=IRR))
    assert d.grounds == ('magnitude',)
    assert '100' in d.reason and 'IRREVERSIBLE' in d.reason

    # Below the tighter ceiling, permanence stops mattering.
    assert p.authorizes(Risk.of(ROWS, 100, reversibility=IRR)) is True


def test_reversibility_can_refuse_on_its_own_with_size_to_spare():
    """The orthogonal case: comfortably within the size ceiling, refused for permanence."""
    p = make_policy(grants=(Grant(ROWS, 10_000, REV),))
    d = p.decide(Risk.of(ROWS, 12, reversibility=IRR))
    assert d.authorized is False
    assert d.grounds == ('reversibility',)
    assert 'REVERSIBLE' in d.reason

    assert p.authorizes(Risk.of(ROWS, 12, reversibility=REV)) is True
    # COMPENSABLE sits between the two, and the ordering is the enum's only real content.
    assert p.authorizes(Risk.of(ROWS, 12, reversibility=COMP)) is False


def test_a_grant_tolerating_the_worst_case_tolerates_the_better_ones():
    p = make_policy(grants=(Grant(ROWS, 10, IRR),))
    for rev in (REV, COMP, IRR):
        assert p.authorizes(Risk.of(ROWS, 10, reversibility=rev)) is True


def test_every_measurement_must_clear_so_blast_radius_needs_no_new_concept():
    """"Drop one production table holding 40,000 customers" is two measurements.

    A policy may drop a table and still have nothing to say about customer records; the
    action is refused on the measurement it does not govern, which is how blast radius
    gets counted without inventing a third axis for it.
    """
    p = make_policy(grants=(Grant(DATABASES, 1, IRR),))
    action = Risk.compound({DATABASES: 1, SUBJECTS: 40_000}, reversibility=IRR,
                           description='drop legacy customers table')

    d = p.decide(action)
    assert d.authorized is False
    assert d.grounds == ('ungoverned',)
    assert SUBJECTS in d.reason

    # Grant the second unit and the same action goes through — proving the refusal was
    # about that measurement and not about the shape of a compound descriptor.
    p2 = make_policy(grants=(Grant(DATABASES, 1, IRR), Grant(SUBJECTS, 50_000, IRR)))
    assert p2.authorizes(action) is True


def test_an_undescribed_action_is_never_self_authorized():
    """A descriptor with no measurements is not "small", it is unanswered.

    Note this is NOT the legacy `amount_cents=None` path — that one produces a real
    measurement of zero cents. Reaching here means a new call site handed over an empty
    Risk, which is a bug in that call site and must behave like one.
    """
    p = make_policy(max_cents=10_000_00, grants=(Grant(ROWS, 1_000_000, IRR),))
    d = p.decide(Risk.compound({}, reversibility=REV))
    assert d.authorized is False
    assert d.grounds == ('unmeasured',)


# ========================================================= 3. THE ONE THAT MATTERS

def test_a_policy_authored_for_one_domain_never_authorizes_another():
    """THE TEST THIS FILE EXISTS FOR.

    Before db/004_risk.sql this failure was silent and total. `refund_authority` says
    "up to $200 unattended". Hand it an action that sends one email and the old model
    computed `(None or 0) <= 20000`, returned True, and the agent sent it — not because
    anyone decided that was acceptable, but because the policy had no vocabulary in which
    to disagree and a missing number reads as zero.

    The rule that fixes it is stated as a refusal, not a warning: a measurement in a unit
    the policy does not grant is denied outright. At magnitude 1. When reversible. With
    dollars to spare in the budget. Authority does not generalize across domains by
    accident, ever.
    """
    refund_authority = make_policy(max_cents=100_000)      # $1,000 of unattended refunds

    for magnitude, rev in ((1, REV), (1, IRR), (40_000, IRR)):
        d = refund_authority.decide(Risk.of(RECIPIENTS, magnitude, reversibility=rev))
        assert d.authorized is False, (
            f'a refund policy authorized {magnitude} {RECIPIENTS} — authority leaked '
            f'across domains, which is the exact bug 004 exists to close')
        assert d.grounds == ('ungoverned',)
        assert 'refusal, not a default' in d.reason

    # The leak is symmetric, and the symmetry matters: an email policy has no opinion
    # about money either, and "no opinion" must not read as "yes" in that direction
    # either. Its money ceiling is the default 0, so even one cent parks on a human.
    campaign_authority = make_policy(grants=(Grant(RECIPIENTS, 50_000, IRR),))
    assert campaign_authority.authorizes(Risk.money(1)) is False
    assert campaign_authority.authorizes(1) is False        # the legacy int path too

    # And a policy denominated in one currency has said nothing about another. Money is
    # several units, not one, for the same reason.
    usd = make_policy(max_cents=100_000)
    assert usd.authorizes(Risk.money(5_000, currency='USD')) is True
    assert usd.authorizes(Risk.money(5_000, currency='EUR')) is False


def test_a_typo_in_a_unit_fails_closed_in_both_directions():
    """The cost of an open unit vocabulary, asserted rather than assumed.

    A misspelt unit in a POLICY grants authority over nothing; a misspelt unit in an
    ACTION is ungoverned and parks. Neither direction can widen authority, which is what
    makes "no migration to govern a new domain" an acceptable trade.
    """
    typo_in_policy = make_policy(grants=(Grant('comms.recipient', 50_000, IRR),))
    assert typo_in_policy.authorizes(Risk.of(RECIPIENTS, 10, reversibility=IRR)) is False

    correct_policy = make_policy(grants=(Grant(RECIPIENTS, 50_000, IRR),))
    assert correct_policy.authorizes(Risk.of('comms.recipient', 10, reversibility=IRR)) is False


# ================================================================ 4. THE VOCABULARY

def test_reversibility_is_ordered_and_the_order_is_its_content():
    assert REV.severity < COMP.severity < IRR.severity
    assert risk.reversibility('irreversible') is IRR
    with pytest.raises(MalformedRisk):
        risk.reversibility('MOSTLY_REVERSIBLE')
    with pytest.raises(MalformedRisk):
        risk.reversibility('')


@pytest.mark.parametrize('bad', ['money', 'money.', '.cents', 'money.usd cents',
                                 '9money.cents', 'money.usd.cents', ''])
def test_a_unit_that_is_not_domain_dot_noun_is_rejected(bad):
    with pytest.raises(MalformedRisk):
        risk.unit(bad)


def test_case_and_whitespace_are_normalized_rather_than_rejected():
    """A policy author who typed ' Money.USD_Cents ' meant the money unit and should get
    it. Rejecting that would push authors toward copy-paste, which is how a unit ends up
    misspelt in the one clause nobody re-read."""
    assert risk.unit(' Money.USD_Cents ') == risk.MONEY_USD_CENTS


def test_a_magnitude_is_a_count_and_not_a_judgement():
    with pytest.raises(MalformedRisk):
        Measurement(ROWS, -1)
    with pytest.raises(MalformedRisk):
        Measurement(ROWS, 1.5)          # type: ignore[arg-type]
    with pytest.raises(MalformedRisk):
        Measurement(ROWS, True)         # bool is an int and must not sneak through
    assert Measurement(ROWS, 0).magnitude == 0


def test_one_action_measures_each_unit_once():
    """Two magnitudes for one unit is not a compound action, it is an ambiguity — and
    an ambiguity in an authority input resolves in whichever direction the loop happens
    to take."""
    with pytest.raises(MalformedRisk):
        Risk((Measurement(ROWS, 10), Measurement(ROWS, 40_000)), IRR)


def test_a_grant_missing_its_reversibility_is_malformed_not_defaulted():
    """A default would have to be permissive to keep pre-004 policies honest, and a
    silently permissive default on the permanence axis is not a default worth having."""
    with pytest.raises(MalformedGrant, match='max_reversibility'):
        Grant.from_json({'unit': ROWS, 'max_magnitude': 10})
    with pytest.raises(MalformedGrant, match='max_magnitude'):
        Grant.from_json({'unit': ROWS, 'max_reversibility': 'IRREVERSIBLE'})
    with pytest.raises(MalformedGrant):
        Grant.from_json({'unit': ROWS, 'max_magnitude': 10, 'max_reversibility': 'SORT_OF'})
    with pytest.raises(MalformedGrant):
        Grant(ROWS, -1, IRR)
    # 0 is a legitimate grant meaning "never unattended, at any size".
    assert Grant(ROWS, 0, IRR).max_magnitude == 0


def test_grants_round_trip_through_json_unchanged():
    grants = (Grant(ROWS, 10_000, REV), Grant(RECIPIENTS, 500, IRR))
    assert risk.grants_from_json(json.loads(json.dumps(risk.grants_to_json(grants)))) == grants
    assert risk.grants_from_json(None) == ()      # a pre-004 row
    assert risk.grants_from_json([]) == ()        # a policy that states no general grants
    with pytest.raises(MalformedGrant):
        risk.grants_from_json({'unit': ROWS})     # an object is not an array
    with pytest.raises(MalformedGrant):
        risk.grants_from_json('[]')               # unparsed column text


def test_a_risk_descriptor_round_trips_for_the_approval_queue():
    """axiom_approval.risk is what a human reads before ruling. It has to survive JSONB."""
    r = Risk.compound({DATABASES: 1, SUBJECTS: 40_000}, reversibility=IRR,
                      description='drop legacy customers table')
    assert Risk.from_json(json.loads(json.dumps(r.to_json()))) == r
    assert r.to_json()['measurements'] == {DATABASES: 1, SUBJECTS: 40_000}
    assert r.to_json()['reversibility'] == 'IRREVERSIBLE'


# ================================================== 5. THE AGENT MAY NOT SIZE ITSELF

def test_an_operation_nobody_taught_axiom_to_measure_cannot_be_measured():
    """Fails closed, loudly. An operation nobody has sized is the operation nobody has
    thought about, and "assume it is small" is how that gets discovered in production."""
    assert risk.is_measurable('refunds.create')
    assert not risk.is_measurable('databases.drop')
    with pytest.raises(UnmeasuredAction, match='databases.drop'):
        risk.measure('databases.drop', {})


def test_the_shipped_refund_measurer_reproduces_the_money_descriptor():
    r = risk.measure('refunds.create',
                     {'order_ref': 'ORD-9', 'amount_cents': 30000, 'currency': 'USD',
                      'reason': 'duplicate_charge'})
    assert r == Risk.money(30000, description=r.description)
    assert r.reversibility is IRR
    assert 'ORD-9' in r.description

    # And it is the same descriptor the legacy bridge builds, so wiring measure() into
    # tasks.prepare() later cannot change any decision the engine makes today.
    assert r.measurements == Risk.from_amount_cents(30000).measurements


def test_two_definitions_of_how_risky_an_operation_is_are_refused():
    """Whichever one loses would lose silently, and which one loses would depend on
    import order."""
    with pytest.raises(MalformedRisk, match='already has'):
        risk.measurer('refunds.create')(lambda body: Risk.money(0))


# ============================================================= 6. THROUGH THE DATABASE

def test_grants_survive_publish_and_reload(world):
    """The decision logic is worth nothing if the authority does not persist verbatim."""
    grants = (Grant(ROWS, 10_000, REV), Grant(ROWS, 100, IRR),
              Grant(RECIPIENTS, 500, COMP))

    db.tx(lambda cur: policy_mod.publish(
        cur, tenant_id=world.tenant_id, policy_id='data_retention_authority', version=1,
        body={'description': 'GDPR erasure worker'}, max_auto_action_cents=0,
        requires_approval=False, created_by='human:test@axiom.invalid',
        risk_grants=grants))

    loaded = db.tx(lambda cur: policy_mod.active(
        cur, tenant_id=world.tenant_id, policy_id='data_retention_authority'))

    assert loaded.risk_grants == grants
    assert loaded.authorizes(Risk.of(ROWS, 500, reversibility=REV)) is True
    assert loaded.authorizes(Risk.of(ROWS, 500, reversibility=IRR)) is False
    # A retention policy has no authority over money, and no amount of budget changes that.
    assert loaded.authorizes(Risk.money(1)) is False


def test_a_policy_row_written_before_004_still_decides_identically(world):
    """The conftest fixture publishes exactly the pre-004 shape: a money ceiling and
    nothing else. It must read back with no grants and behave as it always did."""
    loaded = db.tx(lambda cur: policy_mod.active(
        cur, tenant_id=world.tenant_id, policy_id=POLICY_ID))

    assert loaded.risk_grants == ()
    assert loaded.max_auto_action_cents == world.policy_max_cents
    assert loaded.authorizes(world.policy_max_cents) is True
    assert loaded.authorizes(world.policy_max_cents + 1) is False
    assert loaded.authorizes(None) is True
    # ...and it still refuses the domain it was never given authority over.
    assert loaded.authorizes(Risk.of(RECIPIENTS, 1, reversibility=REV)) is False


def test_publish_normalizes_grants_so_the_stored_policy_is_already_valid(world):
    """A policy is a durable, signable artifact. Storing a clause nobody parsed means the
    first time anyone learns it is malformed is inside the transaction about to act on it.
    """
    db.tx(lambda cur: policy_mod.publish(
        cur, tenant_id=world.tenant_id, policy_id='campaign_authority', version=1,
        body={'description': 'marketing sends'}, max_auto_action_cents=0,
        requires_approval=False, created_by='human:test@axiom.invalid',
        risk_grants=[{'unit': ' Comms.Recipients ', 'max_magnitude': 500,
                      'max_reversibility': 'irreversible'}]))

    stored = world.rows("SELECT risk_grants FROM axiom_policy WHERE tenant_id = %s "
                        "AND policy_id = 'campaign_authority'", (str(world.tenant_id),))
    assert stored[0]['risk_grants'] == [
        {'unit': RECIPIENTS, 'max_magnitude': 500, 'max_reversibility': 'IRREVERSIBLE'}]

    with pytest.raises(MalformedGrant):
        db.tx(lambda cur: policy_mod.publish(
            cur, tenant_id=world.tenant_id, policy_id='broken_authority', version=1,
            body={}, max_auto_action_cents=0, requires_approval=False,
            created_by='human:test@axiom.invalid',
            risk_grants=[{'unit': RECIPIENTS, 'max_magnitude': 500}]))
    assert world.rows("SELECT 1 FROM axiom_policy WHERE tenant_id = %s AND "
                      "policy_id = 'broken_authority'", (str(world.tenant_id),)) == []


def test_the_pinned_version_carries_its_own_authority(world):
    """Publishing v2 must not retroactively change what v1 permitted.

    tasks.prepare() pins policy_version for the whole attempt precisely so an act is
    judged against one immutable rule set. That guarantee has to cover the grants too, or
    the general model reintroduces the problem versioning already solved.
    """
    def _publish(version: int, grants: tuple[Grant, ...]):
        db.tx(lambda cur: policy_mod.publish(
            cur, tenant_id=world.tenant_id, policy_id='erasure_authority',
            version=version, body={'v': version}, max_auto_action_cents=0,
            requires_approval=False, created_by='human:test@axiom.invalid',
            risk_grants=grants))

    _publish(1, (Grant(ROWS, 10_000, IRR),))
    _publish(2, (Grant(ROWS, 10, IRR),))

    v1 = db.tx(lambda cur: policy_mod.at_version(
        cur, tenant_id=world.tenant_id, policy_id='erasure_authority', version=1))
    v2 = db.tx(lambda cur: policy_mod.at_version(
        cur, tenant_id=world.tenant_id, policy_id='erasure_authority', version=2))

    big = Risk.of(ROWS, 5_000, reversibility=IRR)
    assert v1.authorizes(big) is True
    assert v2.authorizes(big) is False

    # And the journal records the authority as published, so the question is answerable
    # from the event stream without trusting the current table state.
    published = world.rows(
        "SELECT detail FROM axiom_event WHERE tenant_id = %s AND subject_type = 'policy' "
        "AND event_type = 'policy.published' ORDER BY seq", (str(world.tenant_id),))
    grants_in_journal = [d['detail'].get('risk_grants') for d in published
                         if d['detail'].get('policy_id') == 'erasure_authority']
    assert grants_in_journal == [
        [{'unit': ROWS, 'max_magnitude': 10_000, 'max_reversibility': 'IRREVERSIBLE'}],
        [{'unit': ROWS, 'max_magnitude': 10, 'max_reversibility': 'IRREVERSIBLE'}]]


def test_a_malformed_grant_in_the_database_stops_the_attempt(world):
    """Someone will eventually hand-edit a policy row. Parsing leniently would mean the
    unparseable clause silently contributes no authority — indistinguishable from a clause
    the author never wrote, which is the wrong kind of quiet for an authority model."""
    db.tx(lambda cur: policy_mod.publish(
        cur, tenant_id=world.tenant_id, policy_id='hand_edited_authority', version=1,
        body={}, max_auto_action_cents=0, requires_approval=False,
        created_by='human:test@axiom.invalid', risk_grants=(Grant(ROWS, 10, IRR),)))

    world.execute("""UPDATE axiom_policy SET risk_grants = '[{"unit": "data.rows"}]'::JSONB
                     WHERE tenant_id = %s AND policy_id = 'hand_edited_authority'""",
                  (str(world.tenant_id),))

    with pytest.raises(MalformedGrant):
        db.tx(lambda cur: policy_mod.active(
            cur, tenant_id=world.tenant_id, policy_id='hand_edited_authority'))


def test_the_schema_refuses_a_grants_column_that_is_not_an_array(world):
    """The one thing about grants that will never change is that they are a list, so that
    is the one thing the database enforces. Everything above it is vocabulary, and
    vocabulary must not need a migration to grow."""
    db.tx(lambda cur: policy_mod.publish(
        cur, tenant_id=world.tenant_id, policy_id='shape_check_authority', version=1,
        body={}, max_auto_action_cents=0, requires_approval=False,
        created_by='human:test@axiom.invalid'))

    # CockroachDB reports the constraint's EXPRESSION rather than its name, so assert on
    # the predicate; asserting on the name would pass against a differently-named
    # constraint that checked something else entirely.
    with pytest.raises(psycopg.errors.CheckViolation) as excinfo:
        world.execute("""UPDATE axiom_policy SET risk_grants = '{"unit": "data.rows"}'::JSONB
                         WHERE tenant_id = %s AND policy_id = 'shape_check_authority'""",
                      (str(world.tenant_id),))
    assert "jsonb_typeof(risk_grants) = 'array'" in str(excinfo.value)
