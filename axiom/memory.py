"""AXIOM :: episodic + semantic memory.

The half of the system that ADVISES. Nothing in this module may authorize an
irreversible act on its own — that power belongs to axiom_policy (procedural) and to
the receipt in axiom_action_attempt (execution). What lives here answers "what happened
last time?" and "what does this resemble?", with provenance attached so the answer can
be weighed rather than merely believed.

Two rules that are easy to break and expensive to debug:

1. **Embed BEFORE opening the transaction.** db.tx() re-executes its callable on 40001,
   and an embedding call inside it would re-hit Bedrock on every retry. Every function
   here therefore takes a vector, never a string to be embedded. `remember()` is the
   convenience wrapper that embeds first and then hands the vector to the transaction.

2. **Never post-filter an ANN result on a non-prefix column.** The vector search returns
   `target count` candidates and a WHERE applied afterwards discards some of them, so
   you silently get fewer rows than LIMIT and miss true nearest neighbours. That is a
   wrong answer, not a slow query. Admissibility (quarantined / superseded / trust) is
   folded into the computed `retrieval_class` prefix column so inadmissible memories are
   in a different partition of the index and never enter the candidate set at all.
   Valid-time is the one unavoidable post-filter — a computed column cannot call now() —
   and is compensated for by over-fetching (settings.recall_overfetch).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Sequence

import psycopg

from . import events
from .config import EMBED_DIMS, settings
from .db import vector_literal
from .embeddings import content_sha256, embed_list
from .models import MemoryClass, Outcome, RetrievalClass, Trust

_COLS = """id, memory_class, context_key, content, outcome, resolution, source,
           trust_level, confidence, occurred_at, task_id, attempt_id, mission_id,
           policy_id, policy_version, valid_from, valid_until, retrieval_class"""


@dataclass(frozen=True)
class Recalled:
    id: uuid.UUID
    content: str
    outcome: str
    resolution: dict
    source: str
    trust_level: int
    confidence: float
    distance: float
    task_id: uuid.UUID | None
    attempt_id: uuid.UUID | None
    context_key: str

    @property
    def similarity(self) -> float:
        """Cosine distance -> similarity, for humans and for the UI."""
        return 1.0 - self.distance


def _row_to_recalled(r: dict) -> Recalled:
    return Recalled(
        id=r['id'], content=r['content'], outcome=r['outcome'],
        resolution=r['resolution'] or {}, source=r['source'],
        trust_level=r['trust_level'], confidence=float(r['confidence']),
        distance=float(r['distance']), task_id=r.get('task_id'),
        attempt_id=r.get('attempt_id'), context_key=r['context_key'],
    )


# ------------------------------------------------------------------------- writing

def write(
    cur: psycopg.Cursor,
    *,
    tenant_id: uuid.UUID,
    memory_class: MemoryClass,
    context_key: str,
    content: str,
    embedding: Sequence[float],
    outcome: Outcome = Outcome.UNKNOWN,
    source: str,
    trust_level: int = Trust.FIRST_PARTY,
    confidence: float = 1.0,
    resolution: dict[str, Any] | None = None,
    mission_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    attempt_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
    policy_id: str | None = None,
    policy_version: int | None = None,
    source_ref: str | None = None,
    supersedes: uuid.UUID | None = None,
    actor: str = 'system',
) -> uuid.UUID:
    """Insert one memory INSIDE the caller's transaction.

    Called from the settle path with the same cursor that writes the terminal task
    state. That co-commit is the property the whole project rests on: memory cannot
    disagree with execution state, because there is no interval in which one exists
    and the other does not.
    """
    if len(embedding) != EMBED_DIMS:
        raise ValueError(f'embedding must be {EMBED_DIMS}-d')

    cur.execute("""
        INSERT INTO axiom_memory (
            tenant_id, memory_class, context_key, content, content_sha256,
            embedding, outcome, resolution, source, source_ref, trust_level,
            confidence, mission_id, task_id, attempt_id, created_by_agent_id,
            policy_id, policy_version, supersedes)
        VALUES (%(tenant)s, %(cls)s, %(ck)s, %(content)s, %(sha)s,
                %(vec)s::VECTOR(1024), %(outcome)s, %(resolution)s, %(source)s,
                %(source_ref)s, %(trust)s, %(conf)s, %(mission)s, %(task)s,
                %(attempt)s, %(agent)s, %(policy)s, %(pver)s, %(supersedes)s)
        RETURNING id
    """, {
        'tenant': str(tenant_id), 'cls': str(memory_class), 'ck': context_key,
        'content': content, 'sha': content_sha256(content),
        'vec': vector_literal(embedding), 'outcome': str(outcome),
        'resolution': json.dumps(resolution or {}), 'source': source,
        'source_ref': source_ref, 'trust': int(trust_level), 'conf': float(confidence),
        'mission': str(mission_id) if mission_id else None,
        'task': str(task_id) if task_id else None,
        'attempt': str(attempt_id) if attempt_id else None,
        'agent': str(agent_id) if agent_id else None,
        'policy': policy_id, 'pver': policy_version,
        'supersedes': str(supersedes) if supersedes else None,
    })
    mem_id = cur.fetchone()['id']

    # Close the supersession chain in the same transaction. Under SERIALIZABLE two
    # writers cannot both point at the same predecessor and fork the chain — the second
    # gets a 40001 and re-reads. tests/test_invariants.py asserts exactly that.
    if supersedes:
        cur.execute("""
            UPDATE axiom_memory
            SET superseded_by = %s, superseded_at = now()
            WHERE tenant_id = %s AND id = %s AND superseded_by IS NULL
        """, (str(mem_id), str(tenant_id), str(supersedes)))
        if cur.rowcount != 1:
            raise ConflictingSupersession(
                f'memory {supersedes} was already superseded by another writer')

    events.append(
        cur, tenant_id=tenant_id, subject_type='memory', subject_id=mem_id,
        event_type='memory.written', actor=actor, mission_id=mission_id,
        task_id=task_id, attempt_id=attempt_id,
        detail={'context_key': context_key, 'outcome': str(outcome),
                'trust_level': int(trust_level), 'supersedes': str(supersedes) if supersedes else None},
    )
    return mem_id


class ConflictingSupersession(RuntimeError):
    """Two writers raced to supersede the same memory and this one lost."""


def remember(tx_fn, **kw) -> uuid.UUID:
    """Embed outside the transaction, then write inside it.

    Usage:
        mem_id = memory.remember(db.tx, tenant_id=..., content=..., ...)

    Kept as a thin helper rather than magic: most real writes happen inside a larger
    transaction (settle, recover) and should call write() directly with that cursor.
    """
    content = kw['content']
    vec = embed_list(content)
    return tx_fn(lambda cur: write(cur, embedding=vec, **kw))


# ------------------------------------------------------------------------ recalling

def recall(
    cur: psycopg.Cursor,
    *,
    tenant_id: uuid.UUID,
    embedding: Sequence[float],
    memory_class: MemoryClass,
    context_key: str | None = None,
    retrieval_class: RetrievalClass = RetrievalClass.ACTIONABLE,
    k: int | None = None,
    as_of_valid: bool = True,
) -> list[Recalled]:
    """Approximate-nearest-neighbour recall.

    With `context_key` this pins all four prefix columns of axiom_memory_ann_by_context
    (tenant_id, memory_class, context_key, retrieval_class) — the recovery path.
    Without it, three prefix columns of axiom_memory_ann_by_tenant — broad recall.

    Every prefix column is pinned to an EXACT value. A range predicate on a prefix
    column (`trust_level >= 2`) disables the vector index entirely, which is why trust
    is folded into retrieval_class rather than filtered here.
    """
    k = k or settings.recall_k
    fetch = k * settings.recall_overfetch     # over-fetch to survive the valid-time filter
    vec = vector_literal(embedding)

    if context_key is not None:
        sql = f"""
            SELECT {_COLS}, embedding <=> %(vec)s::VECTOR(1024) AS distance
            FROM axiom_memory
            WHERE tenant_id = %(tenant)s
              AND memory_class = %(cls)s
              AND context_key = %(ck)s
              AND retrieval_class = %(rc)s
            ORDER BY embedding <=> %(vec)s::VECTOR(1024)
            LIMIT %(fetch)s
        """
        params = {'vec': vec, 'tenant': str(tenant_id), 'cls': str(memory_class),
                  'ck': context_key, 'rc': str(retrieval_class), 'fetch': fetch}
    else:
        sql = f"""
            SELECT {_COLS}, embedding <=> %(vec)s::VECTOR(1024) AS distance
            FROM axiom_memory
            WHERE tenant_id = %(tenant)s
              AND memory_class = %(cls)s
              AND retrieval_class = %(rc)s
            ORDER BY embedding <=> %(vec)s::VECTOR(1024)
            LIMIT %(fetch)s
        """
        params = {'vec': vec, 'tenant': str(tenant_id), 'cls': str(memory_class),
                  'rc': str(retrieval_class), 'fetch': fetch}

    cur.execute(sql, params)
    rows = cur.fetchall()

    if as_of_valid:
        # The only legitimate post-ANN filter: valid time is time-varying and a computed
        # column cannot call now(). This is why we over-fetched.
        #
        # This compared `valid_until > occurred_at` for most of the project's life, which
        # is a NO-OP: axiom_memory_valid_ck already guarantees valid_until > valid_from,
        # and occurred_at defaults alongside it, so the predicate was true for every row
        # it was asked about. An expired memory — valid_until five days in the past — came
        # back as ACTIONABLE and could license a refund. The comment above described the
        # intent correctly and the code did not implement it, which is the worst version of
        # this bug: three other artifacts (the schema comment, ARCHITECTURE.md, and the
        # recall_overfetch setting that exists solely to survive this filter) all documented
        # behaviour that never happened.
        #
        # now() comes from the DATABASE, not from Python. The workers, the API and the
        # Lambda run on different clocks; validity is a property of the data, so it is
        # judged against the clock that owns the data.
        cur.execute('SELECT now() AS t')
        now = cur.fetchone()['t']
        rows = [r for r in rows
                if r['valid_until'] is None or r['valid_until'] > now]

    return [_row_to_recalled(r) for r in rows[:k]]


def recall_sql_for_explain(context_key: bool = True) -> str:
    """The exact recall statement, for tests that assert on the query PLAN.

    tests/test_recall_plan.py runs EXPLAIN over this and requires a `vector search` node
    with `prefix spans`. Correct rows come back either way when the plan degrades to a
    full scan, so nothing except an explicit plan assertion would catch the regression.
    """
    where = ("tenant_id = %(tenant)s AND memory_class = %(cls)s "
             "AND context_key = %(ck)s AND retrieval_class = %(rc)s") if context_key else (
             "tenant_id = %(tenant)s AND memory_class = %(cls)s AND retrieval_class = %(rc)s")
    return f"""SELECT id FROM axiom_memory WHERE {where}
               ORDER BY embedding <=> %(vec)s::VECTOR(1024) LIMIT %(fetch)s"""


# --------------------------------------------------------------------- governance

def quarantine(cur: psycopg.Cursor, *, tenant_id: uuid.UUID, memory_id: uuid.UUID,
               reason: str, by: str) -> None:
    """Quarantine a memory. Takes effect AT COMMIT, atomically, for every later recall.

    `quarantined` feeds the computed `retrieval_class`, which is a vector index PREFIX
    column — so this UPDATE physically moves the row to a different partition of the
    index inside this transaction. There is no reindex step, no cache to invalidate,
    and no window in which a poisoned memory is still retrievable. Demonstrate this
    live; it is the most counterintuitive good property in the design.
    """
    cur.execute("""
        UPDATE axiom_memory
        SET quarantined = true, quarantined_at = now(), quarantined_by = %s,
            quarantine_reason = %s
        WHERE tenant_id = %s AND id = %s AND quarantined = false
    """, (by, reason, str(tenant_id), str(memory_id)))
    if cur.rowcount != 1:
        raise ValueError(f'memory {memory_id} not found or already quarantined')

    events.append(cur, tenant_id=tenant_id, subject_type='memory', subject_id=memory_id,
                  event_type='memory.quarantined', actor=by,
                  detail={'reason': reason})


def effects_licensed_by(cur: psycopg.Cursor, *, tenant_id: uuid.UUID,
                        memory_id: uuid.UUID) -> list[dict]:
    """Every real-world effect this memory authorized.

    The query you run the moment you discover a memory was poisoned. Backed by the
    partial index axiom_attempt_by_license, which exists for exactly this question.
    """
    cur.execute("""
        SELECT a.id, a.task_id, a.step_name, a.provider, a.operation, a.amount_cents,
               a.currency, a.attempt_state, a.provider_ref, a.prepared_at, a.settled_at
        FROM axiom_action_attempt a
        WHERE a.tenant_id = %s AND a.licensed_by_memory_id = %s
        ORDER BY a.prepared_at DESC
    """, (str(tenant_id), str(memory_id)))
    return cur.fetchall()


def get(cur: psycopg.Cursor, *, tenant_id: uuid.UUID, memory_id: uuid.UUID) -> dict | None:
    cur.execute(f"""
        SELECT {_COLS}, content_sha256, quarantined, quarantine_reason, superseded_by,
               embedding_model, created_at
        FROM axiom_memory WHERE tenant_id = %s AND id = %s
    """, (str(tenant_id), str(memory_id)))
    return cur.fetchone()


def browse(cur: psycopg.Cursor, *, tenant_id: uuid.UUID, limit: int = 100,
           include_inadmissible: bool = True) -> list[dict]:
    """Memory browser for Mission Control. Shows quarantined/superseded rows too —
    the point of the UI is to make the admissibility gate visible, not to hide it."""
    sql = f"""SELECT {_COLS}, quarantined, quarantine_reason, superseded_by
              FROM axiom_memory WHERE tenant_id = %s"""
    if not include_inadmissible:
        sql += " AND retrieval_class = 'ACTIONABLE'"
    sql += ' ORDER BY created_at DESC LIMIT %s'
    cur.execute(sql, (str(tenant_id), limit))
    return cur.fetchall()
