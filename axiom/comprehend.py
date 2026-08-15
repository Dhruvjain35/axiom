"""AXIOM :: Amazon Comprehend — an NLP opinion, admitted at trust tier 1.

THE CRITICAL DESIGN CONSTRAINT, STATED BEFORE ANYTHING ELSE
===========================================================
**Comprehend's output may inform WHAT is proposed. It may never affect what is
PERMITTED.**

Authority in this system comes from two places and neither of them is a language model:
the pinned policy version (procedural memory, `axiom_policy`) and the durable execution
receipt (`axiom_action_attempt`). A hosted NLP service that reads an untrusted customer
sentence is `Trust.TOOL_OUTPUT` — tier 1 of four — and tier 1 is not allowed to move a
ceiling. If a key phrase can change what an agent is permitted to do, then anyone who can
write into the exception description can change it too, and that is the exact
vulnerability this whole project argues against.

So the boundary is not a convention. It is `assert_cannot_widen()`, which every
augmentation must survive, and it enforces one sentence:

    Every field Comprehend is allowed to touch is either (a) read by no authority
    decision and no request body at all — `reason`, `confidence` — or (b) touched only
    on a task that, after the augmentation, will NOT act.

Concretely, against the three fields that reach an authority decision or the wire:

    action          may move only toward 'escalate'. Toward a human is the one direction
                    an NLP service gets to push. It can never start an act.
    amount_cents    this is the integer tasks.prepare() checks against
                    max_auto_action_cents. It may not move at all — not up, and
                    ESPECIALLY not down, because lowering an amount under the ceiling is
                    self-authorization wearing a helpful face. The single exception is
                    going to 0 alongside a cancelled act.
    exception_kind  reaches request_body['reason'] and therefore request_fingerprint,
                    which is crash window W7. It may only be filled in where the rule
                    table found nothing — and a task with no kind escalates, so a
                    Comprehend-supplied kind can never reach a request body.

Run the argument backwards to see why that last one matters: triage runs again if a task
returns to READY, Comprehend is a hosted service and need not answer identically twice,
and a re-triage that produced a different `exception_kind` under the same idempotency key
is a W7 hard stop — same key, different intent. Confining the kind to non-acting tasks
removes the possibility rather than testing for it.

WHAT COMPREHEND ACTUALLY CONTRIBUTES, WITHOUT INFLATION
=======================================================
Comprehend does not know what a refund is. There is no general-purpose e-commerce
classifier in the API; the thing that would classify these texts is a Custom
Classification endpoint, which is provisioned by the hour and therefore does not exist in
this account (see `scripts/comprehend_demo.py` for the cost arithmetic). What the free,
on-demand APIs return is extraction: noun phrases, typed entity spans, and a sentiment
label with scores.

The mapping from that extraction to AXIOM's exception vocabulary is AXIOM's lexicon,
below — applied to what Comprehend extracted rather than to the raw string. That is a
real difference and a small one, and it is stated plainly here rather than being dressed
up: the honest claim is "an AWS AI service reads the exception and its output narrows
what the agent may do unattended", not "Comprehend classifies the exception".

Where it earns its place, measured on the real seed corpus:

  * `late_delivery` beats `fraud_suspected` in `llm._KIND_RULES` because the rule table
    is ordered and first match wins. So "delivery delayed nine days and an unauthorized
    charge appeared on the stolen card" triages as an unattended refund. Comprehend
    extracts "an unauthorized charge" as a key phrase, the lexicon below puts fraud
    FIRST, and the augmentation narrows the act to a human. That is the rule-based path
    being wrong in the expensive direction and an AWS service catching it.
  * An `unclassified` exception gets a kind, which is what its memory's `context_key`
    becomes — and `ctx_exception('unclassified')` is a bucket no future recall usefully
    hits.
  * Sentiment is the one signal nothing in this repo produces. It is used only to lower
    confidence on an ambiguous text, which is recorded on the memory row and authorizes
    nothing.

OFF BY DEFAULT, AND THE REASON IS MONEY
=======================================
`AXIOM_COMPREHEND=1` turns it on, read per call rather than frozen into `settings` at
import. Three reasons, and the third one is the one that decided it:

  * the invariant suite stays hermetic with no AWS credentials and no network no matter
    what is exported in the shell it runs from;
  * a deployed function can be switched without a code change;
  * **these calls are billed on this account.** Comprehend's 50,000 units/month is a
    twelve-month free-tier offer and the deployment account does not have a twelve-month
    free tier — see the measured evidence in the billing block below. A 30-task chaos run
    with this on is 270 units, $0.027. Small, real, and not the $0.00 this account is
    supposed to hold.

With the flag off, `llm.triage()` is byte-for-byte the rule-based path it has always
been — the fallback is not a degraded mode here, it is the thing that decides.
"""

from __future__ import annotations

import math
import os
import threading
import time
from dataclasses import dataclass, replace

# ------------------------------------------------------------------- the billing model
#
# Comprehend's published pricing for DetectKeyPhrases / DetectEntities / DetectSentiment:
# a unit is 100 characters, each request is charged a MINIMUM of 3 units, and the rate is
# $0.0001 per unit for the first 10M units in a month.
#
# THIS IS NOT FREE ON THIS ACCOUNT, and the correction is worth more than the checkbox
# was. Comprehend's 50,000 units/month is a TWELVE-MONTH free-tier offer, not an
# always-free one, and the deployment account has no twelve-month free tier at all — it
# was opened after AWS replaced that programme with account credits, and its credits are
# spent. Measured, not assumed:
#
#   aws freetier get-account-plan-state
#       -> accountPlanType "PAID", accountPlanRemainingCredits $0.00
#   aws freetier get-free-tier-usage --query 'freeTierUsages[].freeTierType'
#       -> 12 rows, all "Always Free". No "12 Months Free" row exists.
#   aws ce get-cost-and-usage ... --group-by USAGE_TYPE (S3, 2026-08-12)
#       -> 3 Requests-Tier1 billed $0.000015. A twelve-month free tier covers 2,000/month.
#
# So every unit counted here is a real unit at $0.0001. That is the reason `enabled()`
# defaults to off and the reason nothing turns it on unattended: 30 tasks is 270 units is
# $0.027 per chaos run, which is small and is not zero, and "small" is not the standard
# this account is held to.
#
# `units_for` counts characters rather than UTF-8 bytes because the pricing page is
# written in characters. For the seed corpus the two are identical (it is all ASCII); for
# non-ASCII input this under-counts against the API's own 100 KB byte limit, which is why
# MAX_CHARS below is well inside it.
UNIT_CHARS = 100
MIN_UNITS_PER_REQUEST = 3
USD_PER_UNIT = 0.0001

#: AWS's published Comprehend free-tier allowance — kept as documentation of the offer,
#: NOT as a claim about this account. See the block above: this account does not have it.
FREE_TIER_UNITS_PER_MONTH_IF_ELIGIBLE = 50_000

#: How much of a description is sent. A triage description is one sentence; anything
#: longer is a paste, and a paste is unbounded cost for extraction that gets no better.
MAX_CHARS = 2_000

#: Written on the memory/audit side so a reader can tell which service produced a signal,
#: the same way embeddings.MODEL_ID names the vector space.
SERVICE_ID = 'aws-comprehend-detect-v1'

UNCLASSIFIED = 'unclassified'
ESCALATE = 'escalate'

#: Kinds that mean "a human decides", matching the seed policy's `escalate_kinds`. A
#: match here is the only thing that lets Comprehend change an action.
ESCALATING_KINDS = frozenset({'fraud_suspected'})

# Extraction -> AXIOM's exception vocabulary.
#
# FRAUD IS FIRST, and that ordering is the point of the file rather than an accident.
# llm._KIND_RULES is also an ordered first-match-wins table and it puts `late_delivery`
# above `fraud_suspected`, so a text carrying both signals self-authorizes a refund. A
# second ordered table that resolves the tie the other way is only useful if the escalating
# kind wins, because this table is permitted to escalate and is not permitted to act.
_KIND_LEXICON: tuple[tuple[str, tuple[str, ...]], ...] = (
    ('fraud_suspected', ('fraud', 'unauthorized', 'stolen', 'chargeback')),
    ('duplicate_charge', ('duplicate charge', 'double charge', 'twice', 'second charge',
                          'duplicate')),
    ('not_delivered', ('never arrived', 'not received', 'missing package',
                       'undelivered')),
    ('damaged', ('damaged', 'cracked', 'broken', 'crushed', 'defective')),
    ('wrong_item', ('wrong item', 'incorrect item', 'wrong size', 'wrong product')),
    ('late_delivery', ('late delivery', 'delayed delivery', 'late shipment', 'delay')),
)

#: Below this, the winning sentiment label is a coin flip and the text is ambiguous.
#: Measured on the seed corpus: three of twenty texts land under it.
AMBIGUOUS_BELOW = 0.60


class AuthorityWidened(AssertionError):
    """An augmentation tried to make the proposal MORE permissive than the rule-based one.

    Deliberately fatal, and deliberately not caught in `llm.triage()`. This cannot be
    raised by Comprehend being slow, throttled, absent or wrong — `classify()` absorbs all
    of that and returns an unavailable result. It can only be raised by a defect in
    `augment()` itself, and a defect that widens what an agent may do unattended is not a
    thing to degrade gracefully around. The task fails, nothing is dispatched, and a human
    sees it.
    """


# ============================================================================ THE CLIENT

_lock = threading.Lock()
_client = None


def _comprehend():
    """Lazily built, exactly like embeddings._bedrock().

    boto3 is imported inside the function so an offline run needs no AWS SDK config at
    all, and the timeouts are short because this call sits on the triage path: a hung
    socket has to surface in seconds rather than hold a Lambda invocation open until the
    platform kills it. Two attempts, standard mode — bounded, because a retry storm
    against a service that is down buys nothing when the rule-based answer is already in
    hand.
    """
    global _client
    with _lock:
        if _client is None:
            import boto3
            from botocore.config import Config
            from .config import settings
            _client = boto3.client(
                'comprehend', region_name=settings.aws_region,
                config=Config(retries={'max_attempts': 2, 'mode': 'standard'},
                              connect_timeout=2, read_timeout=5))
    return _client


def enabled() -> bool:
    """Whether the triage path may call Comprehend at all.

    Read from the environment per call rather than off the frozen `settings` object. That
    is not laziness: it means a shell that happens to export AXIOM_COMPREHEND cannot make
    the invariant suite reach the network, because tests/conftest.py sets it to 0 and this
    function will see that.
    """
    return os.environ.get('AXIOM_COMPREHEND', '').strip().lower() in ('1', 'true', 'yes', 'on')


def units_for(text: str, *, requests: int = 3) -> int:
    """Billing units for `requests` Detect* calls over `text`. Charged, not estimated."""
    per = max(MIN_UNITS_PER_REQUEST, math.ceil(len(text) / UNIT_CHARS))
    return per * requests


def usd_for(units: int) -> float:
    return units * USD_PER_UNIT


# =========================================================================== THE SIGNALS

@dataclass(frozen=True)
class Signals:
    """What Comprehend said, plus what it cost to ask. Never an authorization.

    `available` is False for every failure mode there is — no credentials, throttled,
    timed out, boto3 not installed, the service having a bad afternoon. A caller cannot
    tell them apart and should not need to: the correct response to all of them is the
    same, which is to use the rule-based proposal unchanged.
    """
    available: bool
    text: str
    key_phrases: tuple[str, ...] = ()
    entities: tuple[tuple[str, str], ...] = ()       # (Type, Text)
    sentiment: str = ''                              # POSITIVE|NEGATIVE|NEUTRAL|MIXED
    sentiment_score: float = 0.0                     # score of the winning label
    kinds: tuple[str, ...] = ()                      # lexicon matches, in lexicon order
    units: int = 0                                   # billed units actually consumed
    calls: int = 0                                   # Detect* requests that returned
    latency_ms: float = 0.0                          # wall clock for the whole set
    request_ms: tuple[float, ...] = ()               # and per request, which is the unit
                                                     # a p50 is actually meaningful over
    error: str | None = None
    service_id: str = SERVICE_ID

    @property
    def kind_hint(self) -> str | None:
        """The single kind this extraction most supports, or None."""
        return self.kinds[0] if self.kinds else None

    @property
    def escalating(self) -> bool:
        """Did the extraction turn up something a human is supposed to decide?"""
        return any(k in ESCALATING_KINDS for k in self.kinds)

    @property
    def ambiguous(self) -> bool:
        """The sentiment classifier would not commit. Advisory, and only ever lowers."""
        return bool(self.sentiment) and (
            self.sentiment == 'MIXED' or self.sentiment_score < AMBIGUOUS_BELOW)

    @property
    def usd(self) -> float:
        return usd_for(self.units)

    def evidence(self) -> str:
        """One short clause for the `reason` string. Bounded, because this ends up in a
        column and in a UI."""
        bits = []
        if self.key_phrases:
            bits.append('phrases=' + '/'.join(self.key_phrases[:3]))
        if self.entities:
            bits.append('entities=' + '/'.join(f'{t}:{v}' for t, v in self.entities[:2]))
        if self.sentiment:
            bits.append(f'sentiment={self.sentiment}@{self.sentiment_score:.2f}')
        return f'comprehend[{", ".join(bits)}]'[:300]


def _match_kinds(phrases: tuple[str, ...]) -> tuple[str, ...]:
    """Run AXIOM's lexicon over Comprehend's extraction, in lexicon order.

    Over the EXTRACTION, not over the raw text: matching the raw string again would just
    be llm._KIND_RULES with extra latency and a bill.
    """
    hay = ' | '.join(p.lower() for p in phrases)
    return tuple(kind for kind, terms in _KIND_LEXICON if any(t in hay for t in terms))


def classify(text: str) -> Signals:
    """Three Detect* calls over one exception description. NEVER raises.

    Sequential rather than concurrent on purpose. Measured at ~60 ms p50 per call from a
    laptop, so ~180 ms for the set — cheaper than the thread pool it would take to overlap
    them, and this runs once per task, outside every transaction.
    """
    text = (text or '').strip()[:MAX_CHARS]
    if not text:
        return Signals(available=False, text='', error='empty text')

    started = time.perf_counter()
    units = calls = 0
    per: list[float] = []

    def _timed(fn):
        nonlocal units, calls
        t0 = time.perf_counter()
        out = fn(Text=text, LanguageCode='en')
        per.append((time.perf_counter() - t0) * 1000)
        units, calls = units + units_for(text, requests=1), calls + 1
        return out

    try:
        c = _comprehend()
        kp = _timed(c.detect_key_phrases)['KeyPhrases']
        ents = _timed(c.detect_entities)['Entities']
        sent = _timed(c.detect_sentiment)
    except Exception as e:                                             # noqa: BLE001
        # Everything: NoCredentialsError, ThrottlingException, a read timeout, boto3 not
        # installed. The rule-based proposal is already computed and correct; an exception
        # here must not cost a task. Units for the calls that DID return are still
        # reported, because a partial run still gets billed.
        return Signals(available=False, text=text, units=units, calls=calls,
                       latency_ms=(time.perf_counter() - started) * 1000,
                       request_ms=tuple(per),
                       error=f'{type(e).__name__}: {e}'[:300])

    phrases = tuple(p['Text'] for p in kp)
    entities = tuple((e['Type'], e['Text']) for e in ents)
    label = sent['Sentiment']
    return Signals(
        available=True, text=text,
        key_phrases=phrases, entities=entities,
        sentiment=label,
        sentiment_score=float(sent['SentimentScore'].get(label.capitalize(), 0.0)),
        kinds=_match_kinds(phrases + tuple(v for _, v in entities)),
        units=units, calls=calls, request_ms=tuple(per),
        latency_ms=(time.perf_counter() - started) * 1000)


# ======================================================================== THE BOUNDARY

def assert_cannot_widen(base, out) -> None:
    """THE boundary. Refuse any augmentation that is more permissive than the rules were.

    Four clauses, one per field that can reach an authority decision or the wire. A fifth
    field — `reason` — is unchecked because nothing reads it: it is written to the memory
    row and shown to a human, and it reaches neither `tasks.prepare()` nor
    `request_body`. That asymmetry is the design, not an oversight.
    """
    # 1. ACTION. Toward a human is the only direction an NLP service may push. Anything
    #    else — escalate -> refund, reship -> refund — is Comprehend starting or enlarging
    #    an irreversible act on the strength of a customer-supplied sentence.
    if out.action != base.action and out.action != ESCALATE:
        raise AuthorityWidened(
            f'comprehend moved the action {base.action!r} -> {out.action!r}; the only '
            f'transition it may cause is toward {ESCALATE!r}')

    # 2. MAGNITUDE. amount_cents is the integer tasks.prepare() checks against
    #    max_auto_action_cents and debits from the mission budget. Raising it is obviously
    #    wrong; LOWERING it is the subtle attack, because $300 needs a human under the
    #    seed policy and $150 does not. It moves only to 0, and only when the act is off.
    if out.amount_cents != base.amount_cents and not (
            out.action == ESCALATE and out.amount_cents == 0):
        raise AuthorityWidened(
            f'comprehend moved amount_cents {base.amount_cents} -> {out.amount_cents}; '
            f'that integer is the policy ceiling check and is not Comprehend\'s to move')

    # 3. IDENTITY OF THE ACT. exception_kind is hashed into request_fingerprint via
    #    request_body['reason'] (crash window W7). Filling in a kind is allowed exactly
    #    where the rules had none AND the resulting task does not act, so a
    #    Comprehend-supplied kind is structurally unable to reach a request body.
    if out.exception_kind != base.exception_kind and not (
            base.exception_kind == UNCLASSIFIED and out.action == ESCALATE):
        raise AuthorityWidened(
            f'comprehend moved exception_kind {base.exception_kind!r} -> '
            f'{out.exception_kind!r} on an acting proposal; that string is hashed into '
            f'request_fingerprint and a re-triage that changes it is a W7 hard stop')

    # 4. CONFIDENCE. Advisory — it is stored on the memory row — but it only ever goes
    #    down, because a service that can raise its own confidence score has graded its
    #    own paper.
    if out.confidence > base.confidence:
        raise AuthorityWidened(
            f'comprehend raised confidence {base.confidence} -> {out.confidence}')


def augment(base, signals: Signals | None):
    """Let Comprehend narrow a rule-based (or model-based) proposal. Pure.

    Takes and returns whatever proposal type the caller passed — `llm.Triage` today —
    via `dataclasses.replace`, so this module never imports the thing it constrains.

    Three effects, in the order they are applied, all of them narrowing:

      1. an escalating kind in the extraction that the ordered rule table did not reach
         cancels the act and sends it to a human;
      2. an `unclassified` exception (which already escalates) gets a kind, so its memory
         lands in a `context_key` a future recall can hit;
      3. an ambiguous sentiment lowers confidence.

    Returns `base` unchanged when there is nothing to say, which is the common case and
    the one the fallback depends on.
    """
    if signals is None or not signals.available:
        return base

    out = base
    notes: list[str] = []

    # (1) The narrowing that matters. Note what is NOT here: no branch turns an escalate
    #     into an act, and there is no path from a key phrase to a larger amount.
    if signals.escalating and base.exception_kind not in ESCALATING_KINDS \
            and base.action != ESCALATE:
        hit = next(k for k in signals.kinds if k in ESCALATING_KINDS)
        out = replace(out, action=ESCALATE, amount_cents=0)
        notes.append(f'comprehend found {hit} the rule table did not reach')

    # (2) A kind for a task that was going to a human anyway. Only where the rules found
    #     nothing, so this can never re-shape a request body.
    if base.exception_kind == UNCLASSIFIED and out.action == ESCALATE and signals.kind_hint:
        out = replace(out, exception_kind=signals.kind_hint)
        notes.append(f'comprehend proposes kind {signals.kind_hint}')

    # (3) Sentiment, used the only way it honestly can be. Every one of these texts is
    #     negative — a customer writing in is not happy — so negativity itself carries no
    #     information. What does is the classifier refusing to commit.
    if signals.ambiguous:
        out = replace(out, confidence=min(out.confidence, 0.5))
        notes.append(f'sentiment {signals.sentiment} @ {signals.sentiment_score:.2f} '
                     f'is not decisive')

    if out is base:
        return base

    out = replace(out, reason=f'{base.reason}; {"; ".join(notes)} | '
                              f'{signals.evidence()}'[:500])
    assert_cannot_widen(base, out)
    return out
