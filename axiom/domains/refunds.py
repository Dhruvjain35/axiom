"""AXIOM :: the refund domain — the original workload, expressed through the seam.

Nothing here is new behaviour. Every string, every default, and every branch is lifted
verbatim from axiom/worker.py's execute()/_recover()/_dispatch_and_settle(), because the
point of this file is to prove that the extraction was a REFACTOR and not a rewrite: the
92-test invariant suite exercises the same engine, and the memory sentences a judge sees
in the demo are byte-for-byte the ones the demo has always printed.

Two things are worth pointing at while they are still side by side with the second
domain in the next file:

  * `risk = MONEY`. The policy ceiling this domain is judged against
    (max_auto_action_cents) genuinely means dollars here. That is the only workload for
    which the column name is honest.

  * `dispatch()` is a one-line delegation to provider.create_refund. The domain does not
    own the idempotency key, the receipt, or the decision to re-send — it owns the call.
"""

from __future__ import annotations

from typing import Sequence

from .. import llm, provider
from ..models import TaskState
from . import MONEY, AuditReport, Effect, Intent, register


class RefundDomain:
    name = 'refunds'
    task_type = 'refund'
    step_name = 'refund'
    policy_id = 'refund_authority'
    provider_name = 'payments'
    operation = 'refunds.create'
    risk = MONEY

    # ---------------------------------------------------------------- description

    def describe(self, payload: dict) -> str:
        return payload.get('description', '')

    def subject_ref(self, payload: dict) -> str:
        # dedupe_key is the fallback worker.execute() used, kept because a task enqueued
        # without an explicit order_ref still has to name the thing it is refunding.
        return payload.get('order_ref') or payload.get('dedupe_key', '')

    # --------------------------------------------------------------------- triage

    def triage(self, payload: dict) -> Intent:
        t = llm.triage(description=self.describe(payload),
                       amount_cents=int(payload.get('amount_cents', 0)),
                       order_ref=self.subject_ref(payload))
        return Intent(
            action=t.action,
            acts=(t.action == 'refund'),
            risk_units=t.amount_cents,
            kind=t.exception_kind,
            reason=t.reason,
            confidence=t.confidence,
            # reship resolves the exception without money moving; escalate is a human's
            # problem and ends in DEAD_LETTER, which is where worker.py put it.
            terminal_state=(TaskState.SUCCEEDED if t.action == 'reship'
                            else TaskState.DEAD_LETTER),
        )

    # --------------------------------------------------------------- memory text

    def situation(self, payload: dict, intent: Intent) -> str:
        return f'{intent.kind}: {self.describe(payload)}'

    def recovery_situation(self, payload: dict) -> str:
        # Rebuilt from the payload alone — a recovering worker must not re-triage.
        return f'{payload.get("exception_kind", "unknown")}: {payload.get("description", "")}'

    # -------------------------------------------------------------- the request

    def request_body(self, payload: dict, intent: Intent) -> dict:
        return {'order_ref': self.subject_ref(payload),
                'amount_cents': intent.risk_units,
                'currency': 'USD',
                'reason': intent.kind}

    def dispatch(self, *, idempotency_key: str, request_body: dict, risk_units: int,
                 chaos_pre: float | None = None,
                 chaos_post: float | None = None) -> Effect:
        r = provider.create_refund(
            idempotency_key=idempotency_key,
            order_ref=request_body['order_ref'],
            amount_cents=risk_units,
            currency=request_body.get('currency', 'USD'),
            request_body=request_body,
            chaos_pre=chaos_pre, chaos_post=chaos_post)
        return Effect(ref=r.provider_ref, status=r.status, body=r.body,
                      replayed=r.replayed)

    def settled_memory(self, *, situation: str, idempotency_key: str, risk_units: int,
                       effect: Effect, first_try: bool) -> str:
        # Verbatim from worker._dispatch_and_settle, including the ternary's shape: the
        # recovered sentence names the key (it is the evidence that the re-send was the
        # same act), the first-attempt sentence does not.
        verb = 'REPLAYED' if effect.replayed else 'CREATED'
        return (
            f'{situation} | recovered={not first_try} | provider {verb} '
            f'{effect.ref} for {risk_units} cents under key {idempotency_key}'
            if not first_try else
            f'{situation} | refund {effect.ref} for {risk_units} cents '
            f'completed on the first attempt')

    # ---------------------------------------------------------------- the audit

    def audit(self, subject_refs: Sequence[str] | None = None) -> AuditReport:
        s = provider.stats(subject_refs)
        return AuditReport(
            effects=int(s['refunds']),
            risk_units=int(s['total_cents']),
            replays=int(s['replays']),
            verdicts=s['verdicts'],
            duplicates=provider.duplicate_check(subject_refs),
            duplicate_label='orders refunded more than once',
        )


DOMAIN = register(RefundDomain())
