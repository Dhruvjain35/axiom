"""AXIOM :: risk — the vocabulary an authority decision is made in.

Procedural memory AUTHORIZES. Until db/004_risk.sql it could only authorize in dollars:
a policy asked "is this under max_auto_action_cents?" and nothing else. That is the right
question for a refund and no question at all for deleting 40,000 records, sending 40,000
emails, dropping a production database, or revoking an engineer's access — for those,
amount_cents is NULL, `NULL <= 20000` is vacuously satisfied, and the agent proceeds
unattended because the policy had no words in which to refuse.

THE MODEL
---------
An action is described by FACTS:

    measurements   one or more (unit, magnitude) pairs
                   money.usd_cents=30000 · comms.recipients=40000 · data.rows=12
    reversibility  REVERSIBLE | COMPENSABLE | IRREVERSIBLE

A policy holds AUTHORITY over those facts as a list of grants, each reading "for unit U I
self-authorize up to magnitude M, provided the act is no worse than reversibility R".

Two axes, because they are genuinely orthogonal and neither determines the other.
Soft-deleting 10,000 rows is enormous and costs a click to undo; hard-deleting 12 rows is
tiny and permanent. Collapsing them into one number — a "risk score", a tier — only works
if somebody picks an exchange rate between size and permanence, and an exchange rate that
nobody wrote down is not a policy, it is a guess with a threshold on it.

WHY THE CALLER MAY NOT NAME A TIER
----------------------------------
The tempting shape is `authorize(risk='HIGH')`. It is the same mistake as an
application-supplied idempotency key, and it fails the same way: whoever names the tier
has already made the authority decision, and the caller is the component we do not trust.
So the split is absolute — the call site states only measurable facts, and the policy,
which a human wrote and versioned and can sign, supplies every judgement about them.

`measure()` closes the loop one turn further. A measurement function is registered per
OPERATION and derives the descriptor from the request body, so the agent does not get to
describe its own act at all; it only gets to submit a request, and that request is already
fingerprinted into the receipt. An understated blast radius therefore becomes a query
against the journal rather than a theory about what the model was thinking.

THE RULE THAT MAKES ALL OF THIS SAFE
------------------------------------
A measurement in a unit the policy does not grant is a REFUSAL. Not a warning, not a
default-allow: the action parks on a human even at magnitude 1, even when reversible. A
refund policy shown `comms.recipients` has no opinion about email, and "no opinion" must
never read as "yes". This is the same stance as NoActivePolicy — missing procedural
memory is a hard stop, because the alternative is an agent that authorized something on
the grounds that a config key was absent.

HONEST STATUS
-------------
The engine does not call `measure()` yet. `tasks.prepare()` still passes `amount_cents`
and reaches the general model through `Policy.authorizes()`'s compatibility bridge, which
is a faithful translation (see `Risk.from_amount_cents`) but a translation. Wiring
`measure()` into the DISPATCH path, and writing `Risk.to_json()` into
`axiom_action_attempt.risk`, are changes to modules this one does not own.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable


class MalformedGrant(ValueError):
    """A policy's stated authority could not be parsed.

    Raised at LOAD time, inside the transaction that was about to authorize something, so
    a typo in a published policy aborts that attempt instead of silently contributing a
    grant that matches nothing (or, worse, one that matches more than intended). Loud is
    the correct volume: the alternative is an authority model that degrades quietly.
    """


class MalformedRisk(ValueError):
    """An action described itself in a way that is not a description."""


class UnmeasuredAction(RuntimeError):
    """No measurement function is registered for this operation.

    Deliberately fatal rather than falling back to "assume it is small". An operation
    nobody has taught AXIOM to measure is exactly the operation nobody has thought about.
    """


# ================================================================== REVERSIBILITY

class Reversibility(StrEnum):
    """Can this be undone, and by whom?

    Not a property of the API being called — a property of the act IN CONTEXT.
    `DELETE FROM orders` is REVERSIBLE with a restorable backup and IRREVERSIBLE at 3am
    when the backup job has been failing for a week. The same endpoint therefore does not
    always carry the same reversibility, which is why this is supplied per action and
    judged by policy rather than baked into a provider integration.
    """

    #: The actor can restore the prior state itself, completely, with nobody's consent.
    #: A soft delete, a feature flag, an unpublished draft, a paused schedule.
    REVERSIBLE = 'REVERSIBLE'

    #: The prior state is gone, but a second action offsets the harm — the saga
    #: compensation case. Money can be re-charged; a shipment can be recalled. The world
    #: remembers that it happened and the offset costs something, which is precisely the
    #: difference from REVERSIBLE.
    COMPENSABLE = 'COMPENSABLE'

    #: Nothing available to the actor restores or offsets it. An email that has been
    #: read, a hard DROP with no backup, a leaked credential, a revoked certificate that
    #: clients already cached, a payout to an external account.
    IRREVERSIBLE = 'IRREVERSIBLE'

    @property
    def severity(self) -> int:
        """Ordinal, so a policy ceiling is a plain `<=`.

        The ordering is the entire semantic content of this enum: a grant that tolerates
        IRREVERSIBLE necessarily tolerates COMPENSABLE and REVERSIBLE, never the reverse.
        """
        return _SEVERITY[self]


_SEVERITY: dict[Reversibility, int] = {
    Reversibility.REVERSIBLE: 0,
    Reversibility.COMPENSABLE: 1,
    Reversibility.IRREVERSIBLE: 2,
}


def reversibility(value: Reversibility | str) -> Reversibility:
    """Parse a label, refusing anything not in the vocabulary.

    A misspelt label must never resolve to a permissive value, so there is no fallback
    and no case-insensitive guessing beyond upper-casing the whole token.
    """
    if isinstance(value, Reversibility):
        return value
    try:
        return Reversibility(str(value).strip().upper())
    except ValueError:
        raise MalformedRisk(
            f'{value!r} is not a reversibility; expected one of '
            f'{", ".join(r.value for r in Reversibility)}') from None


# =========================================================================== UNITS

# The unit vocabulary is OPEN and lives in Python, not in a SQL enum or a lookup table.
# Governing a new kind of act must cost a policy edit, never a migration — a schema change
# on the live cluster is the last thing that should stand between an operator and "stop
# letting the agent do that unattended".
#
# The cost of openness is that a typo is a unit nobody grants. That is the right failure:
# a misspelt unit in a POLICY grants authority over nothing, and a misspelt unit in an
# ACTION is ungoverned and parks on a human. Both directions fail closed.
_UNIT_RE = re.compile(r'^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$')


def unit(name: str) -> str:
    """Canonical form: `domain.noun`, lowercase. Rejected loudly if it is not."""
    canon = str(name).strip().lower()
    if not _UNIT_RE.match(canon):
        raise MalformedRisk(
            f'{name!r} is not a unit; expected lowercase domain.noun such as '
            f'{MONEY_USD_CENTS!r} or {COMMS_RECIPIENTS!r}')
    return canon


def money_unit(currency: str = 'USD') -> str:
    """Money is denominated PER CURRENCY, so it is several units rather than one.

    A policy that self-authorizes 20000 usd_cents has said nothing about euros, and under
    the ungoverned-unit rule it therefore refuses them. That is the correct reading of a
    ceiling somebody wrote while thinking in dollars.
    """
    return unit(f'money.{currency}_cents')


# Well-known units. Constants because a typo in a literal is a silent denial, and named
# constants turn it into an ImportError. The list is illustrative, not exhaustive — any
# string matching _UNIT_RE is a unit, which is the point.
MONEY_USD_CENTS = 'money.usd_cents'          # the pre-004 authority model, now one unit
COMMS_RECIPIENTS = 'comms.recipients'        # people who will receive a message
DATA_ROWS = 'data.rows'                      # records written, altered, or destroyed
DATA_SUBJECTS = 'data.subjects'              # distinct people those records are about
INFRA_PRODUCTION_RESOURCES = 'infra.production_resources'   # databases, clusters, buckets
ACCESS_PRINCIPALS = 'access.principals'      # identities granted or revoked
EXTERNAL_CALLS = 'external.calls'            # requests to a third party we cannot recall


# ==================================================================== MEASUREMENTS

@dataclass(frozen=True, order=True)
class Measurement:
    """How much of one thing this action does."""
    unit: str
    magnitude: int

    def __post_init__(self) -> None:
        object.__setattr__(self, 'unit', unit(self.unit))
        if not isinstance(self.magnitude, int) or isinstance(self.magnitude, bool):
            raise MalformedRisk(f'magnitude for {self.unit} must be an int, '
                                f'got {type(self.magnitude).__name__}')
        if self.magnitude < 0:
            # A magnitude is unsigned by definition: "how big" has no direction. Callers
            # holding a signed quantity take its absolute value on the way in — see
            # Risk.from_amount_cents for why that is a tightening and not a fudge.
            raise MalformedRisk(f'magnitude for {self.unit} is negative ({self.magnitude})')

    def __str__(self) -> str:
        return f'{self.magnitude} {self.unit}'


@dataclass(frozen=True)
class Risk:
    """What an action is about to do, stated as facts and nothing else.

    Multiple measurements are how blast radius stops needing its own concept: "drop one
    production table holding 40,000 customers" is `infra.production_resources=1` AND
    `data.subjects=40000`, and a policy that may drop a table but has nothing to say about
    customer records still parks on a human. Every measurement must clear; a single
    ungoverned one is a refusal for the whole action.
    """
    measurements: tuple[Measurement, ...]
    reversibility: Reversibility
    description: str = ''

    def __post_init__(self) -> None:
        seen: dict[str, int] = {}
        for m in self.measurements:
            if m.unit in seen:
                raise MalformedRisk(f'{m.unit} measured twice ({seen[m.unit]} and '
                                    f'{m.magnitude}); one action, one magnitude per unit')
            seen[m.unit] = m.magnitude
        # Sorted so two descriptions of the same action are the same value: this object is
        # hashed, compared in tests, and serialized into an audit row.
        object.__setattr__(self, 'measurements', tuple(sorted(self.measurements)))
        object.__setattr__(self, 'reversibility', reversibility(self.reversibility))

    # ------------------------------------------------------------------ constructors

    @classmethod
    def of(cls, unit_name: str, magnitude: int, *,
           reversibility: Reversibility | str,
           description: str = '') -> Risk:
        """The single-measurement case, which is most of them."""
        return cls((Measurement(unit_name, magnitude),), reversibility, description)

    @classmethod
    def compound(cls, measurements: Mapping[str, int], *,
                 reversibility: Reversibility | str,
                 description: str = '') -> Risk:
        """Several measurements of one act — the blast-radius case."""
        return cls(tuple(Measurement(u, m) for u, m in measurements.items()),
                   reversibility, description)

    @classmethod
    def money(cls, cents: int, *, currency: str = 'USD',
              reversibility: Reversibility | str = Reversibility.IRREVERSIBLE,
              description: str = '') -> Risk:
        """Money moving out. IRREVERSIBLE by default, which is the honest reading:
        AXIOM exists because you cannot un-send a $300 refund, only issue a new charge
        the customer has to agree to."""
        return cls.of(money_unit(currency), abs(int(cents)),
                      reversibility=reversibility, description=description)

    @classmethod
    def from_amount_cents(cls, cents: int | None, *, currency: str = 'USD',
                          description: str = '') -> Risk:
        """THE COMPATIBILITY BRIDGE. Every pre-004 call site arrives through here.

        It must reproduce the old comparison exactly for every value the old comparison
        could see, and it does, with one deliberate exception:

          * `None` becomes `money.usd_cents = 0`, not an empty description. The old code
            read `(amount_cents or 0) <= max`, so a step that moves no money was
            authorized by any policy; a zero-magnitude money measurement is authorized by
            the synthesized money grant for the same reason. Same answer, now said out
            loud rather than falling out of a coalesce.
          * a NEGATIVE amount is judged at its absolute value. Under the old comparison
            EVERY negative amount self-authorized, because `-500000 <= 20000` is true no
            matter how large the number is; a sign error anywhere upstream — including
            `int(data['amount_cents'])` straight out of an unclamped LLM response in
            llm.py — therefore bypassed the ceiling entirely rather than tripping it.
            Judging |cents| is strictly tighter than what shipped and never looser, and a
            sign error on money is the exact bug class this project exists to catch.
        """
        return cls.money(0 if cents is None else int(cents), currency=currency,
                         description=description)

    # ------------------------------------------------------------------ serialization

    def to_json(self) -> dict[str, Any]:
        """The shape that drops into axiom_approval.risk and axiom_action_attempt.risk.

        A mapping rather than a list of pairs because this is read by humans in the
        approval queue at least as often as by code.
        """
        return {'measurements': {m.unit: m.magnitude for m in self.measurements},
                'reversibility': str(self.reversibility),
                'description': self.description}

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> Risk:
        if not isinstance(raw, Mapping):
            raise MalformedRisk(f'a risk descriptor must be an object, got {type(raw).__name__}')
        measurements = raw.get('measurements') or {}
        if not isinstance(measurements, Mapping):
            raise MalformedRisk('measurements must be an object of unit -> magnitude')
        return cls.compound(measurements,
                            reversibility=raw.get('reversibility', ''),
                            description=str(raw.get('description') or ''))

    def __str__(self) -> str:
        body = ', '.join(str(m) for m in self.measurements) or 'nothing measured'
        return f'{body} [{self.reversibility}]'


# ========================================================================== GRANTS

@dataclass(frozen=True)
class Grant:
    """One clause of a policy's authority: how much of what, and how permanent.

    Several grants for the SAME unit is not a conflict, it is the mechanism. A policy
    reading

        data.rows   <= 10000  when REVERSIBLE
        data.rows   <=   100  when IRREVERSIBLE

    soft-deletes ten thousand rows without asking and hard-deletes a hundred and one only
    with a human. Same unit, same magnitudes, different answer — which is the requirement
    that a single dollar ceiling could not express at all.
    """
    unit: str
    max_magnitude: int
    max_reversibility: Reversibility

    def __post_init__(self) -> None:
        object.__setattr__(self, 'unit', unit(self.unit))
        object.__setattr__(self, 'max_reversibility', reversibility(self.max_reversibility))
        if not isinstance(self.max_magnitude, int) or isinstance(self.max_magnitude, bool):
            raise MalformedGrant(f'max_magnitude for {self.unit} must be an int, '
                                 f'got {type(self.max_magnitude).__name__}')
        if self.max_magnitude < 0:
            # 0 is meaningful and useful — "never unattended, at any size" — so the floor
            # is 0 rather than 1. Negative is not a tighter grant, it is a typo that would
            # match nothing and confuse whoever read the policy next.
            raise MalformedGrant(f'max_magnitude for {self.unit} is negative '
                                 f'({self.max_magnitude}); use 0 to grant nothing')

    def covers(self, measurement: Measurement, rev: Reversibility) -> bool:
        """Both axes, both hard. Neither compensates for the other."""
        return (measurement.unit == self.unit
                and measurement.magnitude <= self.max_magnitude
                and rev.severity <= self.max_reversibility.severity)

    def to_json(self) -> dict[str, Any]:
        return {'unit': self.unit, 'max_magnitude': self.max_magnitude,
                'max_reversibility': str(self.max_reversibility)}

    @classmethod
    def from_json(cls, raw: Any) -> Grant:
        if not isinstance(raw, Mapping):
            raise MalformedGrant(f'a grant must be an object, got {type(raw).__name__}')
        for required in ('unit', 'max_magnitude', 'max_reversibility'):
            if required not in raw:
                # max_reversibility is required rather than defaulted on purpose. Any
                # default would have to be the permissive end to keep pre-004 policies
                # honest, and a silently permissive default on the axis that separates
                # "undo it" from "it is gone" is not a default worth having.
                raise MalformedGrant(f'grant is missing {required!r}: {dict(raw)!r}')
        try:
            return cls(str(raw['unit']), int(raw['max_magnitude']),
                       reversibility(raw['max_reversibility']))
        except (MalformedRisk, TypeError, ValueError) as e:
            raise MalformedGrant(f'{e} (in grant {dict(raw)!r})') from None

    def __str__(self) -> str:
        return f'{self.unit} <= {self.max_magnitude} when at most {self.max_reversibility}'


def grants_from_json(raw: Any) -> tuple[Grant, ...]:
    """Parse a policy's risk_grants column. NULL and [] both mean 'no general grants'.

    NULL is what every row published before db/004_risk.sql reads, because the column was
    added without a backfill; [] is what a policy that deliberately states no general
    authority reads. They behave identically, and the money ceiling still applies to both
    — see Policy.effective_grants.
    """
    if raw is None:
        return ()
    if isinstance(raw, (str, bytes)):
        # psycopg returns JSONB as parsed Python, but a caller handing us the raw column
        # text is a plausible mistake and a confusing one to debug three layers down.
        raise MalformedGrant('risk_grants arrived as text; it must be parsed JSON')
    if not isinstance(raw, (list, tuple)):
        raise MalformedGrant(f'risk_grants must be an array, got {type(raw).__name__}')
    return tuple(Grant.from_json(g) for g in raw)


def grants_to_json(grants: tuple[Grant, ...] | list[Grant]) -> list[dict[str, Any]]:
    return [g.to_json() for g in grants]


# ======================================================================== DECISION

@dataclass(frozen=True)
class Unmet:
    """One measurement the policy would not cover, and the ground for refusing it.

    Structured, not just prose, because this is what a human reads in the approval queue
    and what a reviewer greps for when asking why an agent stopped. `ground` is one of
    'ungoverned' | 'magnitude' | 'reversibility' | 'unmeasured' | 'requires_approval'.
    """
    measurement: Measurement | None
    ground: str
    detail: str


@dataclass(frozen=True)
class Decision:
    """The authority answer, with its reasons attached."""
    authorized: bool
    reason: str
    unmet: tuple[Unmet, ...] = ()

    @property
    def grounds(self) -> tuple[str, ...]:
        return tuple(u.ground for u in self.unmet)


_AUTHORIZED = Decision(True, 'within policy')


def decide(grants: tuple[Grant, ...], risk: Risk) -> Decision:
    """May the machine act alone on this?

    Every measurement must be covered by at least one grant. The most permissive covering
    grant wins, which is what lets a policy state a general ceiling and a tighter one for
    the permanent version of the same act.
    """
    if not risk.measurements:
        # An action that declines to say how big it is has not been described, and an
        # undescribed act is never self-authorized. Note that the compatibility bridge
        # cannot produce this: `from_amount_cents(None)` says "0 cents", which is a
        # measurement. Reaching here means a new call site passed a Risk with nothing in
        # it, which is a bug in that call site and should behave like one.
        return Decision(False,
                        'the action states no measurable magnitude; an undescribed act '
                        'is never taken unattended',
                        (Unmet(None, 'unmeasured', 'no measurements supplied'),))

    governed = {g.unit for g in grants}
    unmet: list[Unmet] = []

    for m in risk.measurements:
        for_unit = [g for g in grants if g.unit == m.unit]
        if any(g.covers(m, risk.reversibility) for g in for_unit):
            continue

        if not for_unit:
            unmet.append(Unmet(m, 'ungoverned', (
                f'this policy holds no authority over {m.unit} '
                f'(it governs: {", ".join(sorted(governed)) or "nothing"}); an ungoverned '
                f'unit is a refusal, not a default')))
            continue

        # WHICH AXIS ACTUALLY FAILED. Worth the extra few lines: a reason that names the
        # wrong axis sends an operator to edit a number that was never the problem.
        # Ordering matters — a grant that tolerates this act's permanence is the one whose
        # ceiling the operator would have to raise, so it is the ceiling worth quoting.
        big_enough = [g for g in for_unit if m.magnitude <= g.max_magnitude]
        permanent_enough = [g for g in for_unit
                            if risk.reversibility.severity <= g.max_reversibility.severity]

        if permanent_enough:
            widest = max(g.max_magnitude for g in permanent_enough)
            unmet.append(Unmet(m, 'magnitude', (
                f'{m} exceeds the unattended ceiling of {widest} {m.unit} for an act '
                f'that is {risk.reversibility}')))
        else:
            furthest = max((g.max_reversibility for g in (big_enough or for_unit)),
                           key=lambda r: r.severity)
            unmet.append(Unmet(m, 'reversibility', (
                f'{m} is within this policy\'s size ceiling but the act is '
                f'{risk.reversibility}; unattended it goes no further than '
                f'{furthest} at that size')))

    if not unmet:
        return _AUTHORIZED
    return Decision(False, '; '.join(u.detail for u in unmet), tuple(unmet))


# ============================================================ MEASUREMENT REGISTRY

# operation -> the human-written function that derives a descriptor from a request body.
#
# This is where the agent stops being allowed to describe its own act. It submits a
# request body; a function somebody reviewed turns that body into measurements. The
# request body is already SHA-256'd into axiom_action_attempt.request_fingerprint, so the
# derivation is reproducible from the audit trail: "the agent understated the blast
# radius" is a query, not an argument.
_MEASURERS: dict[str, Callable[[Mapping[str, Any]], Risk]] = {}


def measurer(operation: str) -> Callable[[Callable[[Mapping[str, Any]], Risk]],
                                         Callable[[Mapping[str, Any]], Risk]]:
    """Register the measurement function for one operation, e.g. 'refunds.create'."""
    op = str(operation).strip()
    if not op:
        raise MalformedRisk('an operation name is required')

    def _register(fn: Callable[[Mapping[str, Any]], Risk]):
        existing = _MEASURERS.get(op)
        if existing is not None and existing is not fn:
            # Two definitions of how risky an operation is means one of them is losing
            # silently, and which one depends on import order. Refuse.
            raise MalformedRisk(f'operation {op!r} already has a measurement function '
                                f'({existing.__module__}.{existing.__qualname__})')
        _MEASURERS[op] = fn
        return fn
    return _register


def measure(operation: str, request_body: Mapping[str, Any]) -> Risk:
    """Derive the risk descriptor for a proposed call. Fails closed on the unknown."""
    fn = _MEASURERS.get(str(operation).strip())
    if fn is None:
        raise UnmeasuredAction(
            f'no measurement function is registered for {operation!r}; AXIOM will not '
            f'authorize an operation nobody has said how to size. Register one with '
            f'@risk.measurer({operation!r}).')
    return fn(request_body)


def is_measurable(operation: str) -> bool:
    return str(operation).strip() in _MEASURERS


@measurer('refunds.create')
def _measure_refund(request_body: Mapping[str, Any]) -> Risk:
    """The one operation AXIOM ships with, expressed in the general model.

    IRREVERSIBLE rather than COMPENSABLE is a judgement worth defending: a refund can in
    principle be offset by a new charge, but that charge needs the customer's agreement
    and a payments processor that will accept it, neither of which is available to the
    agent. "Compensable only with someone else's consent" is not compensable by the actor,
    and the actor is who this classification is about.
    """
    return Risk.money(int(request_body.get('amount_cents') or 0),
                      currency=str(request_body.get('currency') or 'USD'),
                      description=(f'refund {request_body.get("order_ref", "?")}: '
                                   f'{request_body.get("reason", "unspecified")}'))
