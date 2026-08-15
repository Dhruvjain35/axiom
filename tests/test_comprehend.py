"""AXIOM :: the Comprehend boundary.

These tests are not about whether Amazon Comprehend is good at reading English. They are
about the one property that has to hold whether it is good or not: **an NLP service's
opinion cannot widen what the agent is permitted to do.**

So the shape of the file is deliberate. Almost every test here runs offline, with no AWS
credentials and no network, because the invariant is a property of `augment()` — a pure
function — and a property you can only check by calling a hosted service is a property
nobody checks. The two `test_live_*` tests are the only ones that reach AWS, and they skip
unless BOTH credentials and `AXIOM_COMPREHEND_LIVE=1` are present.

The adversarial tests matter most. Each row of
`test_the_boundary_refuses_every_way_of_widening` is a plausible future refactor — one
that trims an amount to be helpful, one that promotes an escalation to an act — and the
assertion is that `assert_cannot_widen` raises, rather than that some reviewer notices.
"""

from __future__ import annotations

import os
from dataclasses import replace

import pytest

from axiom import comprehend, llm
from axiom.comprehend import AuthorityWidened, Signals
from axiom.llm import Triage
from axiom.seed import EXCEPTIONS

# The rule-based proposals the augmentation is allowed to narrow and nothing else. One
# acting, one already at a human, one the rule table could not name.
#: $150 — deliberately UNDER the seed policy's $200 self-authorization ceiling, so this is
#: a proposal the machine would carry out with no human anywhere near it.
ACTING = Triage('refund', 15_000, 'matched duplicate_charge on the exception description',
                'duplicate_charge', 0.75)
ESCALATED = Triage('escalate', 0, 'matched fraud_suspected', 'fraud_suspected', 0.75)
UNKNOWN = Triage('escalate', 0, 'no rule matched this exception', 'unclassified', 0.4)


def sig(**kw) -> Signals:
    """A Comprehend response without Comprehend. Defaults are decisive and uninteresting,
    so each test states only the signal it is actually about."""
    base = dict(available=True, text='t', key_phrases=(), entities=(),
                sentiment='NEGATIVE', sentiment_score=0.95, kinds=(),
                units=9, calls=3, latency_ms=180.0)
    base.update(kw)
    return Signals(**base)


# ===================================================================== the flag is off

def test_triage_is_byte_for_byte_the_rule_path_when_comprehend_is_off():
    """The fallback is not a degraded mode; with the flag off it is the whole system."""
    assert not comprehend.enabled(), 'conftest must keep the suite hermetic'
    for description, kind, amount in EXCEPTIONS:
        got = llm.triage(description=description, amount_cents=amount, order_ref='ORD-1')
        assert got == llm._offline_triage(description, amount)
        assert got.exception_kind == kind, 'the rule table and the seed corpus disagree'


def test_enabled_reads_the_environment_per_call_not_the_frozen_settings(monkeypatch):
    """`settings` is frozen at import, so a flag living there could not be flipped by a
    test or by a Lambda without a redeploy. This one can, which is why conftest can
    guarantee the suite never reaches AWS."""
    assert comprehend.enabled() is False
    monkeypatch.setenv('AXIOM_COMPREHEND', '1')
    assert comprehend.enabled() is True
    monkeypatch.setenv('AXIOM_COMPREHEND', 'no')
    assert comprehend.enabled() is False


def test_unavailable_signals_change_nothing():
    """Every failure mode Comprehend has — no credentials, throttled, timed out, boto3
    missing — arrives here as the same object, and it has to be a no-op."""
    for s in (None, Signals(available=False, text='t', error='NoCredentialsError'),
              Signals(available=False, text='t', units=3, calls=1, error='ReadTimeout')):
        for base in (ACTING, ESCALATED, UNKNOWN):
            assert comprehend.augment(base, s) is base


# ================================================================ the narrowing effects

def test_an_escalating_phrase_cancels_an_act_the_ordered_rule_table_authorized():
    """The defect this integration exists for.

    `llm._KIND_RULES` is ordered and first match wins, and `late_delivery` sits above
    `fraud_suspected` in it. So a text carrying both signals proposes an UNATTENDED
    refund. Comprehend's extraction is matched fraud-first, and the act goes to a human.
    """
    text = ('delivery delayed nine days and an unauthorized charge appeared on the '
            'stolen card')
    base = llm._offline_triage(text, 15_000)
    assert (base.action, base.exception_kind) == ('refund', 'late_delivery'), (
        'the rule table no longer has the ordering defect this test is about')

    # Exactly what Comprehend returned for this text on 2026-08-14, transcribed.
    out = comprehend.augment(base, sig(
        key_phrases=('delivery', 'nine days', 'an unauthorized charge'),
        entities=(('QUANTITY', 'nine days'),),
        kinds=comprehend._match_kinds(
            ('delivery', 'nine days', 'an unauthorized charge', 'nine days'))))

    assert out.action == 'escalate'
    assert out.amount_cents == 0
    assert 'fraud_suspected' in out.reason
    assert out.exception_kind == 'late_delivery', (
        'the kind must NOT be rewritten on a task the rules already named — that string '
        'is hashed into request_fingerprint')


def test_an_unclassified_exception_gets_a_kind_and_still_does_not_act():
    """The other load-bearing use: `ctx_exception('unclassified')` is a memory bucket no
    future recall usefully hits, and this is what fills it in."""
    out = comprehend.augment(UNKNOWN, sig(key_phrases=('the cracked screen',),
                                          kinds=('damaged',)))
    assert out.exception_kind == 'damaged'
    assert out.action == 'escalate', 'naming a thing is not permission to act on it'
    assert out.amount_cents == 0
    assert out.confidence <= UNKNOWN.confidence


def test_an_ambiguous_sentiment_only_lowers_confidence():
    out = comprehend.augment(ACTING, sig(sentiment='NEUTRAL', sentiment_score=0.51,
                                         kinds=('duplicate_charge',)))
    assert out.confidence == 0.5 and out.confidence < ACTING.confidence
    assert (out.action, out.amount_cents, out.exception_kind) == (
        ACTING.action, ACTING.amount_cents, ACTING.exception_kind)


def test_agreement_is_a_no_op():
    """Comprehend agreeing with the rules must not perturb the proposal at all —
    otherwise the common case would drift the request fingerprint for no reason."""
    out = comprehend.augment(ACTING, sig(key_phrases=('duplicate charge',),
                                         kinds=('duplicate_charge',)))
    assert out is ACTING


# ================================================================== the boundary itself

@pytest.mark.parametrize('why, base, widened', [
    ('started an act nothing had authorized',
     ESCALATED, replace(ESCALATED, action='refund', amount_cents=30_000)),
    ('turned a human decision into a different unattended one',
     UNKNOWN, replace(UNKNOWN, action='reship')),
    ('swapped the act for another act',
     ACTING, replace(ACTING, action='reship')),
    ('raised the amount',
     ACTING, replace(ACTING, amount_cents=60_000)),
    ('lowered the amount under the policy ceiling',
     ACTING, replace(ACTING, amount_cents=7_500)),
    ('graded its own paper',
     ACTING, replace(ACTING, confidence=0.99)),
    ('rewrote the kind on an acting proposal, changing request_fingerprint',
     ACTING, replace(ACTING, exception_kind='fraud_suspected')),
    ('named a kind on a proposal the rules had already named',
     ESCALATED, replace(ESCALATED, exception_kind='damaged')),
])
def test_the_boundary_refuses_every_way_of_widening(why, base, widened):
    """Each of these is a plausible future refactor. None of them may pass."""
    with pytest.raises(AuthorityWidened):
        comprehend.assert_cannot_widen(base, widened)


def test_the_boundary_admits_the_narrowings_the_design_intends():
    """The other half: a check that refuses everything is not a boundary, it is a wall."""
    comprehend.assert_cannot_widen(ACTING, replace(ACTING, action='escalate',
                                                   amount_cents=0))
    comprehend.assert_cannot_widen(ACTING, replace(ACTING, confidence=0.5))
    comprehend.assert_cannot_widen(ACTING, replace(ACTING, reason='anything at all'))
    comprehend.assert_cannot_widen(UNKNOWN, replace(UNKNOWN, exception_kind='damaged'))


def test_lowering_the_amount_is_refused_even_though_it_looks_conservative():
    """Stated separately because it is the one a reviewer waves through.

    The seed policy self-authorizes up to $200. A "helpful" augmentation that trimmed a
    $300 refund to $150 would have converted a human decision into an unattended one, in
    the direction that looks like caution.
    """
    with pytest.raises(AuthorityWidened, match='policy ceiling'):
        comprehend.assert_cannot_widen(ACTING, replace(ACTING, amount_cents=7_500))


def test_zeroing_the_amount_is_allowed_only_together_with_cancelling_the_act():
    comprehend.assert_cannot_widen(ACTING, replace(ACTING, action='escalate',
                                                   amount_cents=0))
    with pytest.raises(AuthorityWidened):
        comprehend.assert_cannot_widen(ACTING, replace(ACTING, amount_cents=0))


def test_no_signal_over_the_whole_seed_corpus_can_widen_anything():
    """The exhaustive version. Every seeded exception, crossed with every shape of
    Comprehend response the code can construct, and the invariant holds on all of them.

    Also asserts the summary property the module docstring claims: if ANYTHING that
    reaches an authority decision or a request body moved, the task no longer acts.
    """
    shapes = [
        sig(kinds=()),
        sig(kinds=('fraud_suspected',), key_phrases=('an unauthorized charge',)),
        sig(kinds=('fraud_suspected', 'duplicate_charge')),
        sig(kinds=('damaged',), sentiment='MIXED', sentiment_score=0.4),
        sig(kinds=('duplicate_charge',), sentiment='POSITIVE', sentiment_score=0.62),
        sig(kinds=('late_delivery',), sentiment='NEUTRAL', sentiment_score=0.51),
        sig(kinds=('not_delivered', 'wrong_item'), entities=(('QUANTITY', 'twice'),)),
    ]
    checked = 0
    for description, _, amount in EXCEPTIONS + [('nothing matches this text', '', 900)]:
        base = llm._offline_triage(description, amount)
        for s in shapes:
            out = comprehend.augment(base, s)
            comprehend.assert_cannot_widen(base, out)
            moved = (out.action, out.amount_cents, out.exception_kind) != (
                base.action, base.amount_cents, base.exception_kind)
            assert not moved or out.action == 'escalate', (
                f'{description!r}: an authority-bearing field moved on a task that '
                f'still acts')
            checked += 1
    assert checked == 77


# ============================================================ the wiring, without AWS

def test_triage_consults_comprehend_when_the_flag_is_on(monkeypatch):
    """The real wiring in llm.py, with the network replaced and nothing else."""
    seen: list[str] = []

    def fake_classify(text: str) -> Signals:
        seen.append(text)
        return sig(key_phrases=('an unauthorized charge',), kinds=('fraud_suspected',))

    monkeypatch.setenv('AXIOM_COMPREHEND', '1')
    monkeypatch.setattr(comprehend, 'classify', fake_classify)

    text = 'delivery delayed nine days, unauthorized charge on a stolen card'
    got = llm.triage(description=text, amount_cents=30_000, order_ref='ORD-9')
    assert seen == [text], 'triage must hand Comprehend the description and nothing else'
    assert got.action == 'escalate' and got.amount_cents == 0


def test_augment_runs_the_boundary_before_it_returns_anything(monkeypatch):
    """The assertion is not decoration a future edit can drop without a test noticing.

    Every non-trivial return from `augment()` passes through `assert_cannot_widen`, with
    the ORIGINAL proposal as the left-hand side — checking the result against itself would
    pass forever and prove nothing.
    """
    seen: list[tuple] = []
    real = comprehend.assert_cannot_widen
    monkeypatch.setattr(comprehend, 'assert_cannot_widen',
                        lambda b, o: (seen.append((b, o)), real(b, o))[1])

    out = comprehend.augment(ACTING, sig(kinds=('fraud_suspected',)))
    assert seen == [(ACTING, out)]
    assert out.action == 'escalate'


def test_a_widening_augmentation_fails_the_task_rather_than_being_swallowed(monkeypatch):
    """`AuthorityWidened` must reach the caller through llm._narrowed.

    A task that raises is released, retried and eventually dead-lettered with a human
    looking at it. That is the correct outcome for a proposal that failed its own safety
    check, and it is why `_narrowed` has no try/except: catching this would turn a
    provable invariant into a hope. The operational failures — throttles, timeouts, no
    credentials — are absorbed a layer lower, inside `classify()`, which is the whole
    reason this layer can afford to be strict.
    """
    def _buggy(base, signals):
        # A future refactor that "helpfully" trims the refund under the policy ceiling.
        out = replace(base, amount_cents=base.amount_cents // 2)
        comprehend.assert_cannot_widen(base, out)
        return out

    monkeypatch.setenv('AXIOM_COMPREHEND', '1')
    monkeypatch.setattr(comprehend, 'classify', lambda text: sig(kinds=('damaged',)))
    monkeypatch.setattr(comprehend, 'augment', _buggy)

    with pytest.raises(AuthorityWidened):
        llm._narrowed(ACTING, 'anything')


# ================================================================== the billing model

def test_units_follow_the_published_pricing_model():
    """100 characters is a unit, three units is the floor, per request."""
    assert comprehend.units_for('x' * 5, requests=1) == 3          # the 3-unit minimum
    assert comprehend.units_for('x' * 300, requests=1) == 3        # exactly at the floor
    assert comprehend.units_for('x' * 301, requests=1) == 4        # one char over
    assert comprehend.units_for('x' * 1000, requests=1) == 10
    assert comprehend.units_for('x' * 5, requests=3) == 9          # one classify() call
    assert comprehend.usd_for(9) == pytest.approx(0.0009)


def test_a_long_description_cannot_run_up_an_unbounded_bill(monkeypatch):
    """Units are charged per 100 characters, so an unbounded input is an unbounded bill.
    The truncation is a cost ceiling, and it can only ever weaken the extraction — it
    cannot widen authority, because nothing downstream of it can."""
    captured: list[str] = []

    class _Fake:
        def detect_key_phrases(self, Text, LanguageCode):
            captured.append(Text)
            return {'KeyPhrases': []}

        def detect_entities(self, Text, LanguageCode):
            return {'Entities': []}

        def detect_sentiment(self, Text, LanguageCode):
            return {'Sentiment': 'NEUTRAL', 'SentimentScore': {'Neutral': 0.9}}

    monkeypatch.setattr(comprehend, '_comprehend', lambda: _Fake())
    s = comprehend.classify('a ' * 50_000)
    assert len(captured[0]) <= comprehend.MAX_CHARS
    assert s.units == comprehend.units_for(captured[0], requests=3) == 60


def test_classify_never_raises_whatever_the_sdk_does(monkeypatch):
    def _boom():
        raise RuntimeError('no credentials, no network, no boto3, pick one')

    monkeypatch.setattr(comprehend, '_comprehend', _boom)
    s = comprehend.classify('customer charged twice for order')
    assert s.available is False and s.units == 0 and s.calls == 0
    assert 'RuntimeError' in s.error
    assert comprehend.augment(ACTING, s) is ACTING


def test_empty_text_costs_nothing():
    for t in ('', '   ', None):
        s = comprehend.classify(t)
        assert s.available is False and s.units == 0 and s.calls == 0


# ========================================================================== live, on AWS

def _live_reason() -> str:
    """Why the live tests are skipped, or '' when they may run.

    Ambient credentials are NOT enough, and finding that out was the useful part of
    writing this. `pytest -q` on this laptop picked up a shared-credentials-file profile
    for an entirely different AWS account and spent real units there — quietly, on
    somebody else's bill, in a suite whose whole selling point is that it is hermetic. So
    reaching the network is opt-in by name as well as by credential.

    Deliberately does not probe the service to decide: a check that costs money to
    determine whether to skip is a check that runs on every developer's laptop forever.
    """
    if os.environ.get('AXIOM_COMPREHEND_LIVE', '').strip().lower() not in (
            '1', 'true', 'yes', 'on'):
        return ('set AXIOM_COMPREHEND_LIVE=1 to spend real Comprehend units; ambient '
                'credentials alone are not consent to bill an account')
    try:
        import boto3
        if boto3.Session().get_credentials() is None:
            return 'no AWS credentials; Comprehend is not reachable'
    except Exception as e:
        return f'boto3 unavailable: {e}'
    return ''


LIVE_SKIP = _live_reason()


@pytest.mark.skipif(bool(LIVE_SKIP), reason=LIVE_SKIP or 'live')
def test_live_comprehend_reads_a_real_seeded_exception():
    """One of the two tests that spend money, and they really do spend it.

    Three requests, 9 units, $0.0009 — charged. Comprehend's 50,000-unit monthly
    allowance is a twelve-month free-tier offer and the deployment account has no
    twelve-month free tier (`freetier get-account-plan-state` -> PAID, $0.00 credits;
    `get-free-tier-usage` -> twelve rows, all "Always Free"). That is why reaching AWS
    from this suite takes an explicit opt-in and not merely a credential.
    """
    text = 'unauthorized charge, customer reports stolen card'
    s = comprehend.classify(text)
    assert s.available, f'Comprehend did not answer: {s.error}'
    assert s.calls == 3 and s.units == 9
    assert s.latency_ms > 0
    assert s.sentiment in ('POSITIVE', 'NEGATIVE', 'NEUTRAL', 'MIXED')
    assert s.key_phrases, 'DetectKeyPhrases returned nothing for a plainly phrasal text'
    # Not asserting WHICH phrases: that is Comprehend's model and it may retrain. What is
    # asserted is that its output, whatever it is, cannot widen anything.
    base = llm._offline_triage(text, 95_000)
    out = comprehend.augment(base, s)
    comprehend.assert_cannot_widen(base, out)


@pytest.mark.skipif(bool(LIVE_SKIP), reason=LIVE_SKIP or 'live')
def test_live_the_ordered_rule_defect_is_caught_by_a_real_call():
    """End to end against the real service: the rule table authorizes an unattended
    $300 refund on a fraud text, and Amazon Comprehend takes it away."""
    text = ('delivery delayed nine days and an unauthorized charge appeared on the '
            'stolen card')
    base = llm._offline_triage(text, 15_000)
    assert base.action == 'refund'

    s = comprehend.classify(text)
    assert s.available, f'Comprehend did not answer: {s.error}'
    out = comprehend.augment(base, s)
    assert out.action == 'escalate' and out.amount_cents == 0
    comprehend.assert_cannot_widen(base, out)
