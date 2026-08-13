"""AXIOM :: the domain seam.

Read this module first if you arrived by asking "does this only work for refunds?".

The answer, measured rather than asserted: the ENGINE was already generic. In
axiom/tasks.py the word "refund" appears eight times and seven of them are in comments;
the eighth is a default argument. axiom/{db,events,config,embeddings,memory}.py contain
it zero times. CLAIM / PREPARE / DISPATCH / SETTLE / RECOVER never knew what they were
protecting — they protect *an irreversible external call*, and money was only ever the
example.

What was NOT generic was the EDGE: worker.execute() hardcoded step='refund' and called
provider.create_refund, llm.triage() spoke refund/reship/escalate, and the memory
sentences said "cents". This package is that edge, extracted:

    Domain   what the side effect IS, how to describe the situation for memory,
             what the triage vocabulary is, and what the risk descriptor is
    runtime  the claim->recover|plan->prepare->dispatch->settle loop, domain-parameterized
    refunds  the EXISTING flow, expressed through the protocol, behaviour unchanged
    broadcast a genuinely different workload whose risk axis is not money at all

THE PART THAT IS STILL REFUND-SHAPED, STATED PLAINLY
----------------------------------------------------
db/004_risk.sql and axiom/risk.py generalized the AUTHORITY MODEL while this package was
being written: a policy now holds grants over (unit, magnitude, reversibility), so
'comms.recipients' is a first-class thing to be authorized in and dollars are one unit
among several. Every domain here declares its unit and its reversibility, and
broadcast.py registers a `@risk.measurer` so the blast radius of a send is derived from
the request body rather than proposed by the agent.

What is NOT yet wired is the CALL: `tasks.prepare()` still takes `amount_cents: int` and
reaches the general model through `Policy.authorizes()`'s compatibility bridge, which
translates that integer into `money.usd_cents`. tasks.py is not a file this task owns, so
until it passes a `Risk`, a non-money workload has exactly two options — leave the money
columns NULL and lose the authority model entirely, or put its own magnitude in them.

`RiskAxis` below is the second option made explicit rather than smuggled. A broadcast
receipt carries amount_cents=4600 and currency='RCP', which reads as "4,600 recipients",
and that is the number the policy ceiling and the mission budget actually govern today.
It works, it is tested, and it is still a column-naming lie — the honest reading of that
receipt is `Risk.of('comms.recipients', 4600, reversibility=IRREVERSIBLE)`, which the
domain can now produce and which nothing in the engine asks it for yet.

One rule holds regardless, and is worth stating because violating it produces a number
that means nothing: A MISSION MAY NOT MIX RISK AXES. spent_cents is a single counter, so
adding dollars to recipients in it is arithmetic on incompatible units. One mission, one
axis — enforced by convention and by the demo, not by the database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence, runtime_checkable

from .. import risk as risk_mod
from ..models import TaskState
from ..risk import Reversibility, Risk


# ============================================================================ RISK

@dataclass(frozen=True)
class RiskAxis:
    """The quantity the authority model measures, and the unit it is measured in.

    A policy answers exactly one question — "may the machine do this much, unattended?"
    — and the ONLY thing that changes between workloads is what "this much" counts. For
    refunds it is cents. For a bulk send it is people who receive a message they cannot
    be made to un-receive.

    Two vocabularies live here because the system currently has two. `unit`/`code`/`noun`
    describe how the magnitude is carried through the money-named columns that
    tasks.prepare() still writes; `risk_unit`/`reversibility` describe the same act in
    axiom/risk.py's general model, which is what a policy grant is actually written
    against. They are two spellings of one fact, and the second is the one that survives.
    """
    unit: str        # column-level name of the quantity: 'cents', 'recipients'
    code: str        # THREE chars, stored in the currency column: 'USD', 'RCP'
    noun: str        # what a human calls it in a sentence: 'dollars', 'recipients'
    risk_unit: str   # axiom/risk.py unit: 'money.usd_cents', 'comms.recipients'
    reversibility: Reversibility = Reversibility.IRREVERSIBLE

    def render(self, n: int | None) -> str:
        """Format a quantity for a log line or a memory sentence."""
        n = n or 0
        if self.unit == 'cents':
            return f'${n / 100:,.2f}'
        return f'{n:,} {self.noun}'

    def descriptor(self, magnitude: int, description: str = '') -> Risk:
        """The same magnitude, said in the vocabulary a policy grant is written in.

        Nothing in the engine asks for this yet — tasks.prepare() takes an int. It exists
        so that the day prepare() takes a Risk, every domain already produces one, and so
        the tests can check the authority decision under the general model instead of
        only under the money bridge.
        """
        return Risk.of(self.risk_unit, magnitude, reversibility=self.reversibility,
                       description=description)


MONEY = RiskAxis(unit='cents', code='USD', noun='dollars',
                 risk_unit=risk_mod.MONEY_USD_CENTS,
                 reversibility=Reversibility.IRREVERSIBLE)


# ========================================================================== INTENT

@dataclass(frozen=True)
class Intent:
    """What the deciding model PROPOSES. It cannot authorize itself.

    This is llm.Triage generalized. The type is the seam: a domain returns a proposal,
    and only tasks.prepare() can turn a proposal into an authorized act. An agent
    architecture in which the model's output can reach the outside world without
    crossing a transaction is the failure mode this whole project argues against, so
    the boundary is expressed as two different types rather than as a code review rule.
    """
    action: str                 # domain vocabulary: 'refund' | 'send' | ...
    acts: bool                  # does this action touch the outside world?
    risk_units: int             # in the domain's RiskAxis unit; 0 when acts is False
    kind: str                   # snake_case situation category, for memory context keys
    reason: str
    confidence: float = 0.8
    # Where a NON-acting decision leaves the task. SUCCEEDED means "handled, no external
    # effect was needed"; DEAD_LETTER means "a human has to look at this".
    terminal_state: TaskState = TaskState.SUCCEEDED


@dataclass(frozen=True)
class Effect:
    """What the outside world says happened. Shaped exactly like provider.ProviderResult
    because that shape was already right: a reference, a status, a body, and — the field
    the entire thesis rests on — whether the external system RECOGNIZED the key and
    declined to act a second time."""
    ref: str
    status: int
    body: dict[str, Any]
    replayed: bool


@dataclass(frozen=True)
class AuditReport:
    """The independent verdict, read back out of the external system.

    Deliberately read from the EXTERNAL side. AXIOM asserting that AXIOM did not double
    act is worth nothing; the claim is only meaningful when the system that holds the
    irreversible effects is the one answering.
    """
    effects: int                       # rows the external system created
    risk_units: int                    # cents moved / recipients messaged
    replays: int                       # re-sends the external system absorbed
    verdicts: dict[str, int]           # created / replayed / rejected_fingerprint
    duplicates: list[dict]             # MUST be empty. The headline.
    duplicate_label: str               # how to print a non-empty duplicates list


# ========================================================================== DOMAIN

@runtime_checkable
class Domain(Protocol):
    """One workload. Everything the generic loop needs and nothing it does not.

    Note what is absent: no state transitions, no idempotency key, no receipt, no
    policy lookup, no memory write. A domain cannot reach any of them, which is what
    makes "add a workload" a safe operation — the parts a new workload could get wrong
    are the parts it is not handed.
    """

    name: str            # 'refunds'
    task_type: str       # matches axiom_task.task_type
    step_name: str       # matches axiom_action_attempt.step_name
    policy_id: str       # which procedural memory authorizes this workload
    provider_name: str   # external system id, recorded on the receipt
    operation: str       # 'refunds.create' | 'messages.send'
    risk: RiskAxis

    def describe(self, payload: dict) -> str:
        """The human sentence for this unit of work, from its payload alone."""

    def subject_ref(self, payload: dict) -> str:
        """The external system's identifier for the thing being acted on — an order, a
        campaign. Used to scope the audit to one run rather than to the whole ledger."""

    def triage(self, payload: dict) -> Intent:
        """Propose an action. May call a model. Never inside a transaction."""

    def situation(self, payload: dict, intent: Intent) -> str:
        """The text that gets embedded and becomes this task's memory key."""

    def recovery_situation(self, payload: dict) -> str:
        """The same text, rebuilt from the payload ALONE.

        A recovering worker has not triaged and must not: triage is a model call, and a
        model asked the same question twice can answer it differently. Recovery reads
        the receipt, not the model.
        """

    def request_body(self, payload: dict, intent: Intent) -> dict:
        """The exact bytes sent outside. Hashed into request_fingerprint, stored on the
        receipt, and re-sent verbatim on recovery."""

    def dispatch(self, *, idempotency_key: str, request_body: dict, risk_units: int,
                 chaos_pre: float | None = None,
                 chaos_post: float | None = None) -> Effect:
        """Call the outside world. The ONE method that may not be re-run freely."""

    def settled_memory(self, *, situation: str, idempotency_key: str, risk_units: int,
                       effect: Effect, first_try: bool) -> str:
        """The sentence that co-commits with the terminal transition."""

    def audit(self, subject_refs: Sequence[str] | None = None) -> AuditReport:
        """Read the external system's own books."""


# ======================================================================== REGISTRY

_REGISTRY: dict[str, Domain] = {}
# A separate flag rather than `if not _REGISTRY`, which was wrong and silently so:
# importing axiom.domains.broadcast directly registers ONE domain, after which an
# emptiness check concludes everything is loaded and `for_task_type('refund')` returns
# None. A worker would then hand every refund back to the queue forever.
_loaded = False


def register(domain: Domain) -> Domain:
    """Register by task_type. Returns the domain so a module can bind a singleton."""
    if domain.task_type in _REGISTRY and _REGISTRY[domain.task_type] is not domain:
        raise ValueError(f'two domains claim task_type {domain.task_type!r}')
    _REGISTRY[domain.task_type] = domain
    return domain


def _load() -> None:
    # Imported here, not at module top, because each domain module imports this one.
    # Nothing in either import path opens a connection: the external systems' pools are
    # lazy, so importing the registry costs no round trips.
    global _loaded
    if _loaded:
        return
    _loaded = True
    from . import broadcast, refunds       # noqa: F401


def known() -> dict[str, Domain]:
    _load()
    return dict(_REGISTRY)


def for_task_type(task_type: str) -> Domain | None:
    """The domain that owns this task_type, or None.

    None is a real answer, not an error. A worker built for one workload WILL claim
    another workload's task — axiom_task has a task_type column but tasks.claim() has no
    task_type predicate, so the queue is shared across every workload in the cluster.
    The runtime's job on None is to put the task back untouched; see runtime.py.
    """
    _load()
    return _REGISTRY.get(task_type)
