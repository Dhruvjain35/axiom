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
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass

import psycopg

from . import events
from .models import PolicyStatus


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

    @property
    def is_signed(self) -> bool:
        """A signed policy is the highest trust tier.

        Verifying provenance is what lets a policy outrank a memory: recency does not
        win an argument with authority. (The signature scheme itself is out of scope for
        the hackathon build — what matters architecturally is that the authority
        decision consults provenance, not recency.)
        """
        return bool(self.signature and self.signed_by)

    def authorizes(self, amount_cents: int | None) -> bool:
        """Can the machine act alone at this amount?"""
        if self.requires_approval:
            return False
        return (amount_cents or 0) <= self.max_auto_action_cents


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
               content_sha256, signature, signed_by
        FROM axiom_policy
        WHERE tenant_id = %s AND policy_id = %s AND status = 'ACTIVE'
    """, (str(tenant_id), policy_id))
    row = cur.fetchone()
    if not row:
        raise NoActivePolicy(f'no ACTIVE version of policy {policy_id!r}')
    return Policy(
        policy_id=row['policy_id'], version=row['version'], body=row['body'],
        max_auto_action_cents=row['max_auto_action_cents'],
        requires_approval=row['requires_approval'],
        content_sha256=row['content_sha256'], signature=row['signature'],
        signed_by=row['signed_by'],
    )


def at_version(cur: psycopg.Cursor, *, tenant_id: uuid.UUID, policy_id: str,
               version: int) -> Policy:
    """A specific version — what the PREPARE path uses once a version is pinned."""
    cur.execute("""
        SELECT policy_id, version, body, max_auto_action_cents, requires_approval,
               content_sha256, signature, signed_by
        FROM axiom_policy WHERE tenant_id = %s AND policy_id = %s AND version = %s
    """, (str(tenant_id), policy_id, version))
    row = cur.fetchone()
    if not row:
        raise NoActivePolicy(f'policy {policy_id!r} v{version} does not exist')
    return Policy(
        policy_id=row['policy_id'], version=row['version'], body=row['body'],
        max_auto_action_cents=row['max_auto_action_cents'],
        requires_approval=row['requires_approval'],
        content_sha256=row['content_sha256'], signature=row['signature'],
        signed_by=row['signed_by'],
    )


def publish(cur: psycopg.Cursor, *, tenant_id: uuid.UUID, policy_id: str, version: int,
            body: dict, max_auto_action_cents: int, requires_approval: bool,
            created_by: str, activate: bool = True,
            signature: str | None = None, signed_by: str | None = None) -> None:
    """Publish a version, optionally retiring the incumbent and activating this one —
    in ONE transaction, so there is never an instant with zero or two active versions."""
    canonical = json.dumps(body, sort_keys=True, separators=(',', ':'))
    sha = hashlib.sha256(canonical.encode()).hexdigest()

    if activate:
        cur.execute("""
            UPDATE axiom_policy SET status = 'RETIRED', effective_until = now()
            WHERE tenant_id = %s AND policy_id = %s AND status = 'ACTIVE'
        """, (str(tenant_id), policy_id))

    cur.execute("""
        INSERT INTO axiom_policy (
            tenant_id, policy_id, version, status, body, max_auto_action_cents,
            requires_approval, content_sha256, signature, signed_by, created_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (str(tenant_id), policy_id, version,
          str(PolicyStatus.ACTIVE if activate else PolicyStatus.DRAFT),
          json.dumps(body), max_auto_action_cents, requires_approval, sha,
          signature, signed_by, created_by))

    events.append(cur, tenant_id=tenant_id, subject_type='policy',
                  subject_id=uuid.uuid5(uuid.NAMESPACE_OID, f'{tenant_id}:{policy_id}'),
                  event_type='policy.published', actor=created_by,
                  to_state=str(PolicyStatus.ACTIVE if activate else PolicyStatus.DRAFT),
                  detail={'policy_id': policy_id, 'version': version,
                          'max_auto_action_cents': max_auto_action_cents,
                          'requires_approval': requires_approval, 'sha256': sha})


def history(cur: psycopg.Cursor, *, tenant_id: uuid.UUID, policy_id: str) -> list[dict]:
    cur.execute("""
        SELECT version, status, max_auto_action_cents, requires_approval,
               content_sha256, signed_by, effective_from, effective_until, created_by
        FROM axiom_policy WHERE tenant_id = %s AND policy_id = %s
        ORDER BY version DESC
    """, (str(tenant_id), policy_id))
    return cur.fetchall()
