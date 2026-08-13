"""AXIOM :: procedural memory.

Explicit, versioned, signable operating procedure — the memory class that AUTHORIZES.

Terminology note, stated deliberately because it departs from a cited source: CoALA
(arXiv:2309.02427) defines procedural memory as LLM weights plus agent source code.
AXIOM uses "procedural" in the MemP / Voyager skill-library / Agent-Workflow-Memory
sense instead: explicit, versioned, deprecable operating procedure that a human wrote
and can point at. Flagging the departure rather than quietly redefining a cited term.

The governing version is PINNED to the task at claim time and carried through PREPARE,
so an entire attempt is judged against one policy version even if a new one is
published mid-flight. Otherwise an agent could be authorized under v2, crash, and
recover under a v3 that would never have permitted the act it already committed.

Since db/004_risk.sql a policy's authority is no longer denominated only in dollars. It
is a list of grants over (unit, magnitude, reversibility) — see axiom/risk.py for the
argument. max_auto_action_cents survives untouched and keeps meaning exactly what it
meant, because it is now READ AS one such grant rather than checked beside them; that is
the whole trick, and it is why every pre-004 policy row still decides identically.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

import psycopg

from . import events, risk as risk_mod
from .models import PolicyStatus
from .risk import Grant, Reversibility, Risk


@dataclass(frozen=True)
class Policy:
    policy_id: str
    version: int
    body: dict
    max_auto_action_cents: int
    requires_approval: bool
    content_sha256: str
    signature: str | None
    signed_by: str | None

    # The general authority. Defaulted so that any construction site that predates
    # db/004_risk.sql still builds a valid Policy, and so a row whose risk_grants column
    # is NULL — which is every row published before that migration — is not a special
    # case anywhere but here.
    risk_grants: tuple[Grant, ...] = ()

    @property
    def is_signed(self) -> bool:
        """A signed policy is the highest trust tier.

        Verifying provenance is what lets a policy outrank a memory: recency does not
        win an argument with authority. (The signature scheme itself is out of scope for
        the hackathon build — what matters architecturally is that the authority
        decision consults provenance, not recency.)
        """
        return bool(self.signature and self.signed_by)

    @property
    def effective_grants(self) -> tuple[Grant, ...]:
        """The stated grants, plus the money ceiling READ AS one of them.

        This property is where "generalize without breaking the money case" actually
        happens. max_auto_action_cents is not consulted by a separate branch in the
        decision; it is translated into the same shape as everything else and thrown into
        the same pile:

            money.usd_cents <= max_auto_action_cents, when at most IRREVERSIBLE

        IRREVERSIBLE is the faithful translation, not a convenient one. The pre-004 model
        had no reversibility gate at all, so a translation that invented one would quietly
        tighten every policy already published — and a migration that changes what a
        signed policy MEANS is worse than one that changes what it says.

        THE SYNTHESIZED GRANT IS NOT UNCONDITIONAL, and it was, which was a hole.

        Injecting it into every policy made money the one unit that could never be
        ungoverned. Measured before this was fixed: a policy whose only stated grant was
        `comms.recipients`, carrying an inherited `max_auto_action_cents = 50000`,
        self-authorized 49,999 cents of IRREVERSIBLE money movement — because the money
        grant was appended to it silently. The stated rule of this model is "an ungoverned
        unit is a refusal", and money was quietly exempt from it. The special case had not
        disappeared; it had moved up one layer, which is worse, because the layer it moved
        to is not where anybody looks for it.

        So the ceiling is read as a grant only when the policy has NOT stated its authority
        in the new vocabulary:

          - no risk_grants (every policy published before db/004_risk.sql, and every
            policy that still thinks in dollars): synthesize it, and those policies decide
            exactly as they always did. That compatibility is the whole point of the
            migration being additive.
          - risk_grants present: the policy is speaking the general language, so it must
            say what it authorizes — including money, if it authorizes any. Silence about
            a unit means no authority over it.

        A policy that mentions neither still authorizes an action that moves nothing:
        magnitude 0 clears a ceiling of 0, which is how `authorizes(None)` behaved before.
        """
        if self.risk_grants:
            return self.risk_grants
        money = Grant(risk_mod.MONEY_USD_CENTS, self.max_auto_action_cents,
                      Reversibility.IRREVERSIBLE)
        return (money,)

    def decide(self, action: Risk | int | None) -> risk_mod.Decision:
        """The authority decision, with its reasons attached.

        `requires_approval` short-circuits everything: it is the operator's kill switch
        and no grant may talk past it. Everything else is the general rule in risk.decide.
        """
        if self.requires_approval:
            return risk_mod.Decision(
                False,
                f'policy {self.policy_id} v{self.version} requires approval for every '
                f'action it governs',
                (risk_mod.Unmet(None, 'requires_approval',
                                'the policy self-authorizes nothing'),))
        return risk_mod.decide(self.effective_grants, _as_risk(action))

    def authorizes(self, action: Risk | int | None) -> bool:
        """Can the machine act alone on this?

        Still returns a plain bool, and still accepts a bare `amount_cents`, because
        tasks.prepare() reads `if not pol.authorizes(amount_cents)` and a compatibility
        break there is a change to the one transaction that authorizes irreversible acts.
        A caller that wants the reason instead of the verdict calls `decide()`.
        """
        return self.decide(action).authorized


def _as_risk(action: Risk | int | None) -> Risk:
    """Accept a risk descriptor, or bridge a legacy `amount_cents` into one.

    The bridge is not a fallback path with its own rules — it builds a real descriptor and
    hands it to the same decision function every other caller uses. See
    Risk.from_amount_cents for exactly which old behaviours it reproduces and the one it
    deliberately tightens.
    """
    return action if isinstance(action, Risk) else Risk.from_amount_cents(action)


class NoActivePolicy(RuntimeError):
    """No ACTIVE version exists for this policy id.

    A hard failure on purpose. The alternative — falling back to a permissive default
    when procedural memory is missing — is how an agent ends up authorizing a $300
    refund because a config row was absent.
    """


def active(cur: psycopg.Cursor, *, tenant_id: uuid.UUID, policy_id: str) -> Policy:
    """The one ACTIVE version. axiom_policy_one_active makes 'one' a database fact:
    activating v3 without retiring v2 is a 23505, not an ambiguous authority model."""
    cur.execute("""
        SELECT policy_id, version, body, max_auto_action_cents, requires_approval,
               content_sha256, signature, signed_by, risk_grants
        FROM axiom_policy
        WHERE tenant_id = %s AND policy_id = %s AND status = 'ACTIVE'
    """, (str(tenant_id), policy_id))
    row = cur.fetchone()
    if not row:
        raise NoActivePolicy(f'no ACTIVE version of policy {policy_id!r}')
    return _hydrate(row)


def at_version(cur: psycopg.Cursor, *, tenant_id: uuid.UUID, policy_id: str,
               version: int) -> Policy:
    """A specific version — what the PREPARE path uses once a version is pinned."""
    cur.execute("""
        SELECT policy_id, version, body, max_auto_action_cents, requires_approval,
               content_sha256, signature, signed_by, risk_grants
        FROM axiom_policy WHERE tenant_id = %s AND policy_id = %s AND version = %s
    """, (str(tenant_id), policy_id, version))
    row = cur.fetchone()
    if not row:
        raise NoActivePolicy(f'policy {policy_id!r} v{version} does not exist')
    return _hydrate(row)


def _hydrate(row: dict) -> Policy:
    """One row -> one Policy. Both loaders go through here so they cannot drift.

    A malformed grant raises MalformedGrant HERE, inside the transaction that was about
    to authorize something, which is the only place the failure is cheap: the transaction
    rolls back, the task re-parks, and nothing left the building. Parsing it leniently
    would mean an unparseable clause silently contributes no authority — indistinguishable
    from a clause the author never wrote, and therefore the wrong kind of quiet.
    """
    return Policy(
        policy_id=row['policy_id'], version=row['version'], body=row['body'],
        max_auto_action_cents=row['max_auto_action_cents'],
        requires_approval=row['requires_approval'],
        content_sha256=row['content_sha256'], signature=row['signature'],
        signed_by=row['signed_by'],
        risk_grants=risk_mod.grants_from_json(row.get('risk_grants')),
    )


def publish(cur: psycopg.Cursor, *, tenant_id: uuid.UUID, policy_id: str, version: int,
            body: dict, max_auto_action_cents: int, requires_approval: bool,
            created_by: str, activate: bool = True,
            signature: str | None = None, signed_by: str | None = None,
            risk_grants: Sequence[Grant] | Sequence[dict] | None = None) -> None:
    """Publish a version, optionally retiring the incumbent and activating this one —
    in ONE transaction, so there is never an instant with zero or two active versions.

    `risk_grants` is the general authority (axiom/risk.py). Omitting it publishes a policy
    whose only authority is the money ceiling, which is precisely what every policy in the
    system was before db/004_risk.sql — so the default keeps every existing caller honest
    without a signature change at the call site.

    Grants are normalized THROUGH Grant here rather than written as handed in. A policy is
    a durable, versioned, signable artifact; storing a dict nobody validated would mean
    the first time anyone finds out a clause is malformed is inside the transaction that
    was about to act on it.
    """
    canonical = json.dumps(body, sort_keys=True, separators=(',', ':'))
    sha = hashlib.sha256(canonical.encode()).hexdigest()

    parsed = risk_mod.grants_from_json(
        [g.to_json() if isinstance(g, Grant) else g for g in (risk_grants or [])])
    grants_json = json.dumps(risk_mod.grants_to_json(parsed))

    if activate:
        cur.execute("""
            UPDATE axiom_policy SET status = 'RETIRED', effective_until = now()
            WHERE tenant_id = %s AND policy_id = %s AND status = 'ACTIVE'
        """, (str(tenant_id), policy_id))

    cur.execute("""
        INSERT INTO axiom_policy (
            tenant_id, policy_id, version, status, body, max_auto_action_cents,
            requires_approval, content_sha256, signature, signed_by, created_by,
            risk_grants)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (str(tenant_id), policy_id, version,
          str(PolicyStatus.ACTIVE if activate else PolicyStatus.DRAFT),
          json.dumps(body), max_auto_action_cents, requires_approval, sha,
          signature, signed_by, created_by, grants_json))

    events.append(cur, tenant_id=tenant_id, subject_type='policy',
                  subject_id=uuid.uuid5(uuid.NAMESPACE_OID, f'{tenant_id}:{policy_id}'),
                  event_type='policy.published', actor=created_by,
                  to_state=str(PolicyStatus.ACTIVE if activate else PolicyStatus.DRAFT),
                  detail={'policy_id': policy_id, 'version': version,
                          'max_auto_action_cents': max_auto_action_cents,
                          'requires_approval': requires_approval, 'sha256': sha,
                          # The journal records the authority as published, so "what was
                          # this agent allowed to do on the 14th" is answerable from the
                          # event stream alone, without trusting the current table state.
                          'risk_grants': risk_mod.grants_to_json(parsed)})


def history(cur: psycopg.Cursor, *, tenant_id: uuid.UUID, policy_id: str) -> list[dict]:
    cur.execute("""
        SELECT version, status, max_auto_action_cents, requires_approval, risk_grants,
               content_sha256, signed_by, effective_from, effective_until, created_by
        FROM axiom_policy WHERE tenant_id = %s AND policy_id = %s
        ORDER BY version DESC
    """, (str(tenant_id), policy_id))
    return cur.fetchall()
