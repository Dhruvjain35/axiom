"""AXIOM :: the deciding LLM (Amazon Bedrock).

Two jobs, both deliberately small:

    triage(exception)  -> which action, how much, why
    plan(goal, orders) -> which exceptions are worth acting on

What the model is NOT allowed to do is as important as what it does. It never mints an
idempotency key, never decides whether it is allowed to act (that is procedural memory),
and never sees the receipt table. It proposes; the state machine disposes. An agent
architecture where the model can talk itself into an irreversible act is the failure
mode this whole project is arguing against, so the seam is enforced by the type
signature: triage() returns a proposal, and only tasks.prepare() can authorize one.

Offline mode returns a deterministic rule-based triage so the invariant suite runs with
no credentials and no network. The engine cannot tell the difference.

On this deployment the rule table is not a stand-in, it is the decider: Bedrock's
on-demand quota for this account is structurally zero, so nothing ever reaches Claude.
Amazon Comprehend does answer on this account, so `axiom/comprehend.py` runs alongside
the rules when AXIOM_COMPREHEND=1 and NARROWS what they propose — it can send an act to a
human, and it can label an exception the rules could not, and it can do nothing else.
Read that module's docstring for why narrowing is the only direction it is given, and for
why the flag is off by default (those calls are billed on this account; the free tier
everyone quotes for Comprehend is a twelve-month offer this account does not have).
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass

from . import comprehend
from .config import settings

_lock = threading.Lock()
_client = None


def _bedrock():
    global _client
    with _lock:
        if _client is None:
            import boto3
            _client = boto3.client('bedrock-runtime', region_name=settings.aws_region)
    return _client


@dataclass(frozen=True)
class Triage:
    action: str           # 'refund' | 'reship' | 'escalate'
    amount_cents: int
    reason: str
    exception_kind: str   # namespaced for memory context keys
    confidence: float = 0.8


_SYSTEM = """You triage e-commerce order exceptions for an autonomous operations agent.

You do not execute anything. You propose one action; a policy engine decides whether the
agent may take it unattended, and a durable state machine decides whether it already has.

Choose exactly one action:
  refund   - the customer should get money back
  reship   - send a replacement instead of refunding
  escalate - a human must look at this

Reply with ONLY a JSON object, no prose, no code fence:
{"action":"refund","amount_cents":3000,"exception_kind":"duplicate_charge","reason":"one sentence","confidence":0.0-1.0}

amount_cents must be 0 unless action is "refund". exception_kind is a lowercase
snake_case category such as duplicate_charge, not_delivered, damaged, wrong_item,
late_delivery, or fraud_suspected."""


_KIND_RULES: tuple[tuple[re.Pattern, str, str], ...] = (
    (re.compile(r'duplicate|charged twice|double charge', re.I), 'duplicate_charge', 'refund'),
    (re.compile(r'not received|never arrived|marked delivered', re.I), 'not_delivered', 'refund'),
    (re.compile(r'damaged|broken|cracked', re.I), 'damaged', 'reship'),
    (re.compile(r'wrong item|incorrect item|wrong size', re.I), 'wrong_item', 'reship'),
    (re.compile(r'late|delayed', re.I), 'late_delivery', 'refund'),
    (re.compile(r'fraud|unauthorized|stolen card', re.I), 'fraud_suspected', 'escalate'),
)


def _offline_triage(description: str, amount_cents: int) -> Triage:
    for pattern, kind, action in _KIND_RULES:
        if pattern.search(description):
            return Triage(
                action=action,
                amount_cents=amount_cents if action == 'refund' else 0,
                reason=f'matched {kind} on the exception description',
                exception_kind=kind, confidence=0.75)
    return Triage('escalate', 0, 'no rule matched this exception', 'unclassified', 0.4)


def _narrowed(base: Triage, description: str) -> Triage:
    """Hand the proposal to Amazon Comprehend, which may only make it smaller.

    Two things are deliberately absent. There is no try/except: `comprehend.classify()`
    already absorbs every operational failure there is and returns an unavailable result
    that `augment()` passes through untouched, so the only exception that can escape here
    is `AuthorityWidened` — a defect in the augmentation itself — and that one must not be
    swallowed. And there is no second decision point: this function cannot reach the
    policy, the receipt, or the budget, so whatever it returns still has to survive
    tasks.prepare().
    """
    if not comprehend.enabled():
        return base
    return comprehend.augment(base, comprehend.classify(description))


def triage(*, description: str, amount_cents: int, order_ref: str) -> Triage:
    """Propose an action for one order exception.

    Two stages, and they are not interchangeable. `_propose` decides; `_narrowed` is only
    allowed to take away. Comprehend runs OUTSIDE the model's try/except so that an
    `AuthorityWidened` from the augmentation cannot be laundered into the escalate
    fallback and disappear.
    """
    base = _propose(description=description, amount_cents=amount_cents,
                    order_ref=order_ref)
    return _narrowed(base, description)


def _propose(*, description: str, amount_cents: int, order_ref: str) -> Triage:
    """The deciding half: Bedrock Claude, or the rule table standing in for it."""
    if settings.offline:
        return _offline_triage(description, amount_cents)

    prompt = (f'Order {order_ref}. Order total: {amount_cents} cents.\n'
              f'Exception reported: {description}')
    body = json.dumps({
        'anthropic_version': 'bedrock-2023-05-31',
        'max_tokens': 300,
        'temperature': 0,
        'system': _SYSTEM,
        'messages': [{'role': 'user', 'content': prompt}],
    })
    try:
        resp = _bedrock().invoke_model(modelId=settings.llm_model, body=body)
        text = json.loads(resp['body'].read())['content'][0]['text'].strip()
        # Models occasionally wrap JSON in a fence despite instructions.
        if text.startswith('```'):
            text = text.strip('`')
            text = text[text.find('{'):]
        data = json.loads(text[text.find('{'):text.rfind('}') + 1])
        action = str(data.get('action', 'escalate')).lower()
        if action not in ('refund', 'reship', 'escalate'):
            action = 'escalate'
        return Triage(
            action=action,
            amount_cents=int(data.get('amount_cents', 0) or 0) if action == 'refund' else 0,
            reason=str(data.get('reason', ''))[:500],
            exception_kind=str(data.get('exception_kind', 'unclassified')).lower(),
            confidence=float(data.get('confidence', 0.7)),
        )
    except Exception as e:
        # A model failure must never become an unattended action. Escalating is the only
        # safe default when the thing that was supposed to decide did not.
        return Triage('escalate', 0, f'triage unavailable ({type(e).__name__}); escalating',
                      'unclassified', 0.0)


def summarize_recovery(*, task_type: str, step: str, recalled: list, action: str,
                       rationale: str) -> str:
    """One line of prose describing a recovery, which becomes the CONTENT of an episodic
    memory and therefore the text that gets embedded and recalled next time.

    Kept template-driven rather than model-generated on purpose: this string is the
    retrieval key for future recoveries, and letting a model vary its phrasing run to
    run would make semantically identical situations drift apart in vector space.
    """
    return (f'agent died mid-{step} on a {task_type} task; recovery chose {action}; '
            f'{rationale}')
