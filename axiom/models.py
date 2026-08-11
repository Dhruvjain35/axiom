"""AXIOM :: the vocabulary.

These enums mirror db/001_schema.sql exactly. The SQL is authoritative; this file is a
convenience for Python call sites, and tests/test_schema_sync.py asserts the two agree
by reading the enum labels back out of the live database. A drift between them is the
kind of bug that only shows up as a runtime 22P02 on the one code path nobody exercised.
"""

from __future__ import annotations

from enum import StrEnum


class MissionState(StrEnum):
    PLANNING = 'PLANNING'
    RUNNING = 'RUNNING'
    PAUSED = 'PAUSED'
    SUCCEEDED = 'SUCCEEDED'
    FAILED = 'FAILED'
    CANCELLED = 'CANCELLED'


class TaskState(StrEnum):
    PENDING = 'PENDING'
    READY = 'READY'
    LEASED = 'LEASED'
    AWAITING_APPROVAL = 'AWAITING_APPROVAL'
    ACTION_PREPARED = 'ACTION_PREPARED'
    SUCCEEDED = 'SUCCEEDED'
    FAILED = 'FAILED'
    CANCELLED = 'CANCELLED'
    DEAD_LETTER = 'DEAD_LETTER'


# The exact predicate of the partial index axiom_task_claimable. The CLAIM query's
# WHERE clause must match it CHARACTER FOR CHARACTER in membership or the optimizer
# will not use the index, and the claim loop silently becomes a full scan of every
# task ever created. Defined once, here, and interpolated — never retyped.
CLAIMABLE_STATES: tuple[TaskState, ...] = (
    TaskState.READY,
    TaskState.LEASED,
    TaskState.ACTION_PREPARED,
    TaskState.AWAITING_APPROVAL,
)

TERMINAL_STATES: frozenset[TaskState] = frozenset({
    TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED, TaskState.DEAD_LETTER,
})


class AttemptState(StrEnum):
    PREPARED = 'PREPARED'
    DISPATCHED = 'DISPATCHED'
    SUCCEEDED = 'SUCCEEDED'
    FAILED_RETRYABLE = 'FAILED_RETRYABLE'
    FAILED_TERMINAL = 'FAILED_TERMINAL'
    ABANDONED = 'ABANDONED'
    COMPENSATED = 'COMPENSATED'


# A receipt in either of these states means "an external effect MAY exist in the world".
# DISPATCHED is an observability marker only and is SAFETY-EQUIVALENT to PREPARED:
# never branch on the difference for a correctness decision, because the process can
# die between the send and the marker write.
LIVE_ATTEMPT_STATES: tuple[AttemptState, ...] = (
    AttemptState.PREPARED, AttemptState.DISPATCHED,
)


class ApprovalState(StrEnum):
    PENDING = 'PENDING'
    APPROVED = 'APPROVED'
    REJECTED = 'REJECTED'
    EXPIRED = 'EXPIRED'
    CANCELLED = 'CANCELLED'


class AgentStatus(StrEnum):
    STARTING = 'STARTING'
    ALIVE = 'ALIVE'
    DRAINING = 'DRAINING'
    DEAD = 'DEAD'


class PolicyStatus(StrEnum):
    DRAFT = 'DRAFT'
    ACTIVE = 'ACTIVE'
    RETIRED = 'RETIRED'


class MemoryClass(StrEnum):
    EPISODIC = 'EPISODIC'
    SEMANTIC = 'SEMANTIC'


class Outcome(StrEnum):
    """Constrained vocabulary — the recovery decision AGGREGATES over this column.

    Free text here would mean the recovery path had to ask an LLM to interpret prose
    before deciding whether to re-dispatch a $300 refund. A memory may only vote in
    ways the state machine already understands.
    """
    RESOLVED = 'RESOLVED'                      # replay/resume worked cleanly
    NO_EFFECT = 'NO_EFFECT'                    # proven the call never landed
    DUPLICATE_EFFECT = 'DUPLICATE_EFFECT'      # a double side effect actually occurred
    PROVIDER_AMBIGUOUS = 'PROVIDER_AMBIGUOUS'  # provider state undeterminable
    HUMAN_REQUIRED = 'HUMAN_REQUIRED'          # resolved only by escalation
    UNKNOWN = 'UNKNOWN'


class RetrievalClass(StrEnum):
    """Computed in SQL, never written by the application. Listed here because the
    recovery query PINS it to ACTIONABLE, and a typo in that literal would silently
    widen the candidate set to include quarantined memories."""
    ACTIONABLE = 'ACTIONABLE'
    ADVISORY = 'ADVISORY'
    SUPERSEDED = 'SUPERSEDED'
    QUARANTINED = 'QUARANTINED'


class Trust(int):
    """Provenance tiers. Ordered so comparison is a plain >=."""
    UNTRUSTED = 0        # third-party text: a customer email, a scraped page
    TOOL_OUTPUT = 1      # what a tool said
    FIRST_PARTY = 2      # our own observed execution outcome
    VERIFIED = 3         # signed policy or verified human operator


# Context-key convention for axiom_memory.context_key, which is a VECTOR INDEX PREFIX
# column. A prefix column must be pinned to an EXACT value for the index to be used at
# all, so this is one namespaced column rather than several optional ones.
def ctx_state(state: TaskState | str) -> str:
    """'state:ACTION_PREPARED' — an agent died at this execution state."""
    return f'state:{state}'


def ctx_exception(kind: str) -> str:
    """'exception:duplicate_charge' — a class of business situation."""
    return f'exception:{kind}'
