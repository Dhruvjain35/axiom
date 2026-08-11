"""AXIOM :: the append-only journal.

Never updated, never deleted. Every state transition in the system writes one row here
in the SAME transaction as the transition itself, which is what makes the audit trail
an actual guarantee rather than a logging convention that a `continue` statement can skip.

The per-subject sequence is gap-free by construction:

    seq = coalesce((SELECT max(seq) FROM axiom_event WHERE <subject>), 0) + 1

computed inside the writing transaction. Deliberately not a global sequence (that is a
single-range hotspot for the whole cluster) and deliberately not unique_rowid() (gaps
make "did we lose an event?" unanswerable, which defeats the purpose of having a
journal). Contention is per-subject only, and the fencing token already serializes
writers to a given task — so in practice this costs nothing.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import psycopg

_INSERT = """
INSERT INTO axiom_event (
    tenant_id, subject_type, subject_id, seq, event_type, from_state, to_state,
    actor, lease_epoch, mission_id, task_id, attempt_id, detail)
VALUES (
    %(tenant)s, %(stype)s, %(sid)s,
    coalesce((SELECT max(seq) FROM axiom_event
              WHERE tenant_id = %(tenant)s
                AND subject_type = %(stype)s
                AND subject_id = %(sid)s), 0) + 1,
    %(etype)s, %(from_state)s, %(to_state)s, %(actor)s, %(epoch)s,
    %(mission)s, %(task)s, %(attempt)s, %(detail)s)
RETURNING id, seq
"""


def append(
    cur: psycopg.Cursor,
    *,
    tenant_id: uuid.UUID,
    subject_type: str,
    subject_id: uuid.UUID,
    event_type: str,
    actor: str,
    from_state: str | None = None,
    to_state: str | None = None,
    lease_epoch: int | None = None,
    mission_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    attempt_id: uuid.UUID | None = None,
    detail: dict[str, Any] | None = None,
) -> tuple[uuid.UUID, int]:
    """Append one event. Returns (id, seq).

    `actor` is 'agent:<uuid>' | 'human:<email>' | 'system' — enforced by convention and
    asserted in tests, not by the schema, because the set grows.
    """
    cur.execute(_INSERT, {
        'tenant': str(tenant_id),
        'stype': subject_type,
        'sid': str(subject_id),
        'etype': event_type,
        'from_state': str(from_state) if from_state else None,
        'to_state': str(to_state) if to_state else None,
        'actor': actor,
        'epoch': lease_epoch,
        'mission': str(mission_id) if mission_id else None,
        'task': str(task_id) if task_id else None,
        'attempt': str(attempt_id) if attempt_id else None,
        'detail': json.dumps(detail or {}),
    })
    row = cur.fetchone()
    return row['id'], row['seq']


def replay(cur: psycopg.Cursor, *, tenant_id: uuid.UUID, subject_type: str,
           subject_id: uuid.UUID) -> list[dict]:
    """Every event for one subject, in order. Uses axiom_event_replay."""
    cur.execute("""
        SELECT seq, event_type, from_state, to_state, actor, lease_epoch,
               attempt_id, detail, occurred_at
        FROM axiom_event
        WHERE tenant_id = %s AND subject_type = %s AND subject_id = %s
        ORDER BY seq ASC
    """, (str(tenant_id), subject_type, str(subject_id)))
    return cur.fetchall()


def timeline(cur: psycopg.Cursor, *, tenant_id: uuid.UUID, limit: int = 200,
             mission_id: uuid.UUID | None = None) -> list[dict]:
    """The global audit timeline, newest first. Uses the hash-sharded time index."""
    if mission_id:
        cur.execute("""
            SELECT subject_type, subject_id, seq, event_type, from_state, to_state,
                   actor, task_id, attempt_id, detail, occurred_at
            FROM axiom_event
            WHERE tenant_id = %s AND mission_id = %s
            ORDER BY occurred_at DESC
            LIMIT %s
        """, (str(tenant_id), str(mission_id), limit))
    else:
        cur.execute("""
            SELECT subject_type, subject_id, seq, event_type, from_state, to_state,
                   actor, task_id, attempt_id, detail, occurred_at
            FROM axiom_event
            WHERE tenant_id = %s
            ORDER BY occurred_at DESC
            LIMIT %s
        """, (str(tenant_id), limit))
    return cur.fetchall()
