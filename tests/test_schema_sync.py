"""AXIOM :: the SQL is authoritative; Python must agree with it.

db/001_schema.sql is the source of truth for the vocabulary and for every structural
guarantee. axiom/models.py mirrors it for the convenience of Python call sites, and
axiom/config.py duplicates one number (SHARD_COUNT) that the schema computes. Both
duplications are deliberate, and both are exactly the kind that rot silently — a drift
surfaces as a 22P02 on the one code path nobody exercised, months later.

So these tests read the live database and compare. Nothing here is mocked; every
assertion is against `SHOW CREATE TABLE` or `enum_range` on the cluster the engine is
about to run against. The comments in config.py and models.py promise this file exists.
"""

from __future__ import annotations

import re

import pytest

from axiom import db
from axiom.config import EMBED_DIMS, SHARD_COUNT
from axiom.models import (
    AgentStatus, ApprovalState, AttemptState, CLAIMABLE_STATES, LIVE_ATTEMPT_STATES,
    MemoryClass, MissionState, Outcome, PolicyStatus, RetrievalClass, TaskState,
)

from conftest import query


def create_stmt(table: str) -> str:
    def _q(cur):
        cur.execute(f'SHOW CREATE TABLE {table}')
        return cur.fetchone()['create_statement']
    return db.tx(_q, readonly=True)


def enum_labels(type_name: str) -> list[str]:
    def _q(cur):
        cur.execute(f'SELECT unnest(enum_range(NULL::{type_name}))::STRING AS label')
        return [r['label'] for r in cur.fetchall()]
    return db.tx(_q, readonly=True)


def line_for(stmt: str, needle: str) -> str:
    match = [ln.strip() for ln in stmt.splitlines() if needle in ln]
    assert match, f'{needle!r} not found in the schema'
    return match[0]


# ================================================================= enum vocabularies

@pytest.mark.parametrize('sql_type, py_enum', [
    ('mission_state', MissionState),
    ('task_state', TaskState),
    ('attempt_state', AttemptState),
    ('approval_state', ApprovalState),
    ('agent_status', AgentStatus),
    ('policy_status', PolicyStatus),
])
def test_python_enums_match_the_database(sql_type, py_enum):
    """Order matters as well as membership: enum comparison in SQL is by ordinal."""
    assert enum_labels(sql_type) == [str(v) for v in py_enum]


def test_string_vocabularies_match_their_check_constraints():
    """memory_class / outcome are STRING + CHECK rather than enums, and still must agree.

    The recovery decision AGGREGATES over outcome, so a label Python can produce but the
    CHECK rejects would fail at the worst possible moment: inside the settle transaction,
    after the refund has already left.
    """
    stmt = create_stmt('axiom_memory')

    classes = re.findall(r"'([A-Z_]+)':::STRING", line_for(stmt, 'axiom_memory_class_ck'))
    assert classes == [str(v) for v in MemoryClass]

    outcomes = re.findall(r"'([A-Z_]+)':::STRING", line_for(stmt, 'axiom_memory_outcome_ck'))
    assert outcomes == [str(v) for v in Outcome]

    computed = line_for(stmt, 'retrieval_class STRING NOT NULL AS')
    labels = set(re.findall(r"'([A-Z_]+)':::STRING", computed))
    assert labels == {str(v) for v in RetrievalClass}


# ============================================================== structural promises

def test_shard_count_matches_the_generated_expression():
    """config.SHARD_COUNT is duplicated from SQL on purpose; this is the check that pays for it.

    A mismatch would not error. Workers pinned to shards 0-7 would simply stop seeing
    half the queue, and the symptom would be "some tasks never run" with nothing in the
    logs — the config.py comment promises this test rather than trusting a comment.
    """
    stmt = create_stmt('axiom_task')
    shard_line = line_for(stmt, 'shard INT2 NOT NULL AS')
    modulus = re.search(r'mod\(.*?,\s*(\d+)', shard_line)
    assert modulus, f'could not parse the shard expression: {shard_line}'
    assert int(modulus.group(1)) == SHARD_COUNT

    # And it is genuinely computed from immutable inputs, so a row's shard cannot move.
    assert 'tenant_id' in shard_line and 'dedupe_key' in shard_line
    assert 'STORED' in shard_line


def test_claim_predicate_matches_the_partial_index_predicate():
    """The claim query's WHERE and the index's WHERE must agree, or the index goes unused.

    They agree by construction — tasks.py interpolates models.CLAIMABLE_STATES into the
    SQL — but the index predicate lives in a migration that a future hand edit could
    change. If they diverge the claim loop still returns correct rows, which is why only
    an explicit assertion catches it: it silently becomes a full scan of every task ever
    created.
    """
    stmt = create_stmt('axiom_task')
    index_line = line_for(stmt, 'INDEX axiom_task_claimable')
    predicate = index_line.split(' WHERE ', 1)[1]
    states = re.findall(r"'([A-Z_]+)':::public\.task_state", predicate)
    assert set(states) == {str(s) for s in CLAIMABLE_STATES}
    assert 'shard ASC, available_at ASC' in index_line, 'the claim index lost its ordering'
    assert 'payload' not in index_line, (
        'payload in the claim index pushes the result toward the 16 KiB retry ceiling')


def test_live_attempt_states_match_the_one_live_index():
    """"A call may be in flight" is a database fact, not an application convention."""
    stmt = create_stmt('axiom_action_attempt')
    index_line = line_for(stmt, 'UNIQUE INDEX axiom_attempt_one_live')
    states = re.findall(r"'([A-Z_]+)':::public\.attempt_state",
                        index_line.split(' WHERE ', 1)[1])
    assert set(states) == {str(s) for s in LIVE_ATTEMPT_STATES}
    assert '(tenant_id ASC, task_id ASC, step_name ASC)' in index_line


def test_axiom_task_has_no_column_families():
    """SKIP LOCKED and column families are incompatible, permanently.

    Splitting the hot lease columns from the cold JSONB payload is a plausible-looking
    optimization a reviewer will suggest, and it silently breaks every
    `SELECT ... FOR UPDATE SKIP LOCKED` query. Verifying after a migration is the schema
    comment's instruction; this is that verification, automated.
    """
    assert 'FAMILY' not in create_stmt('axiom_task')


def test_idempotency_key_is_derived_only_from_immutable_columns():
    """The single most lethal bug in this class of system, made unrepresentable.

    A key derived at call time from a UUID, a timestamp, the worker id, the attempt
    counter or the lease epoch means the recovering worker mints a DIFFERENT key, the
    provider sees a brand-new request, and the money goes out twice. The column is
    GENERATED from four immutable columns; this asserts that nothing else leaked in.
    """
    line = line_for(create_stmt('axiom_action_attempt'), 'idempotency_key STRING NOT NULL AS')
    for required in ('tenant_id', 'task_id', 'step_name', 'step_seq'):
        assert required in line
    for forbidden in ('lease_epoch', 'prepared_by', 'prepared_at', 'gen_random_uuid',
                      'now()', 'attempt_state', 'random'):
        assert forbidden not in line, f'{forbidden} leaked into the idempotency key'
    assert 'STORED' in line, 'a VIRTUAL key would be recomputed, not remembered'


def test_vector_indexes_are_cosine_and_prefixed_the_way_recall_queries_them():
    """Omitting vector_cosine_ops silently gives L2 and a `<=>` query then full-scans.

    Perfect-looking on 200 demo rows, collapses at scale, and no error anywhere. The
    prefix column list must also match what memory.recall() pins, or the index cannot be
    used at all.
    """
    stmt = create_stmt('axiom_memory')
    by_context = line_for(stmt, 'VECTOR INDEX axiom_memory_ann_by_context')
    by_tenant = line_for(stmt, 'VECTOR INDEX axiom_memory_ann_by_tenant')

    assert '(tenant_id, memory_class, context_key, retrieval_class, embedding vector_cosine_ops)' \
        in by_context
    assert '(tenant_id, memory_class, retrieval_class, embedding vector_cosine_ops)' in by_tenant
    assert 'vector_l2_ops' not in stmt

    assert f'embedding VECTOR({EMBED_DIMS}) NOT NULL' in stmt, (
        'the embedding column and config.EMBED_DIMS disagree')


def test_tenant_id_is_not_nullable_anywhere():
    """A nullable tenant_id is how cross-tenant leaks happen: one forgotten IS NULL branch.

    Shared infrastructure rows use the reserved SYSTEM tenant precisely so this can be
    uniform, with no table exempted.
    """
    rows = query("""
        SELECT table_name, is_nullable FROM information_schema.columns
        WHERE table_schema = 'public' AND column_name = 'tenant_id'
        ORDER BY table_name
    """)
    assert rows, 'no tenant_id columns found — wrong database?'
    assert all(r['is_nullable'] == 'NO' for r in rows), \
        [r['table_name'] for r in rows if r['is_nullable'] != 'NO']
    assert {r['table_name'] for r in rows} >= {
        'axiom_task', 'axiom_memory', 'axiom_action_attempt', 'axiom_approval',
        'axiom_policy', 'axiom_mission', 'axiom_event', 'axiom_agent'}
