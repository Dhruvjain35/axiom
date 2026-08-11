"""AXIOM :: assertions on the query PLAN, not on the rows.

Every other test in this suite can pass while the recall path is quietly broken, because
a degraded plan returns the SAME rows — it just full-scans the memory table to get them.
That is invisible on a 200-row demo and fatal at scale, and no correctness assertion
anywhere can catch it. Only EXPLAIN can.

Two failure modes are guarded here, both of which have actually happened in this project:

  * the ANN path not being selected at all (preflight's first run, on a small table with
    no statistics), and
  * a SUBQUERY search vector silently defeating index selection, which is why
    db.vector_literal() exists and why every ANN call site passes a bound parameter.

Measured on this cluster (CockroachDB CCL v26.2.3, single node, local)
---------------------------------------------------------------------
The optimizer chose `vector search` with prefix spans at EVERY row count tried — 0, 10,
100 rows for a fresh tenant and 2,500 rows for the durable corpus below, with and
without a fresh ANALYZE. No minimum corpus size turned out to be necessary to make the
plan assertion meaningful, so the ladder is kept as a regression guard rather than as a
search for a threshold. Preflight's original false negative is explained by its other
two variables (a subquery search vector, and a table with no vector index at all), not
by row count alone.

Why the 2,500-row corpus is not torn down
-----------------------------------------
Deleting a vector-indexed row costs ~38 ms on this build (two C-SPANN indexes to
maintain), so tearing down 2,500 rows would add ~100 s of pure teardown to every run and
assure nobody of anything. The corpus therefore lives under a fixed fixture tenant, is
built once, and is reused by every later run — which also means the plan assertion gets
stronger, not weaker, the longer the project runs. To reclaim it:

    DELETE FROM axiom_memory WHERE tenant_id = '22222222-2222-2222-2222-222222222222';
"""

from __future__ import annotations

import uuid

import pytest

from axiom import db, embeddings, memory
from axiom.config import EMBED_DIMS
from axiom.models import MemoryClass, RetrievalClass

from conftest import World, _create_world, _destroy_world

CORPUS_TENANT = uuid.UUID('22222222-2222-2222-2222-222222222222')
CORPUS_ROWS = 2_500
CONTEXT_KEY = 'state:ACTION_PREPARED'
QUERY_TEXT = 'agent died mid-refund on a duplicate_charge task; what happened last time?'

# Generated server-side: shipping 2,500 x 1,024 floats over the wire would dominate the
# runtime of this file and prove nothing about the plan.
_LOAD = f"""
INSERT INTO axiom_memory (tenant_id, memory_class, context_key, content, content_sha256,
                          embedding, outcome, source, trust_level)
SELECT %(tenant)s::UUID, 'EPISODIC', %(ck)s,
       'synthetic recovery ' || r::STRING, sha256(r::STRING),
       array_agg(sin((r * 0.7 + d * 0.013)::FLOAT8) ORDER BY d)::VECTOR({EMBED_DIMS}),
       'RESOLVED', 'system:execution', 2
FROM generate_series(%(lo)s, %(hi)s) AS rows(r)
CROSS JOIN generate_series(1, {EMBED_DIMS}) AS dims(d)
GROUP BY r
"""


def load(tenant_id: uuid.UUID, lo: int, hi: int) -> None:
    for start in range(lo, hi + 1, 500):
        stop = min(start + 499, hi)
        db.tx(lambda cur: cur.execute(
            _LOAD, {'tenant': str(tenant_id), 'ck': CONTEXT_KEY, 'lo': start, 'hi': stop}))
    # Without statistics the optimizer has no basis for preferring any plan, and the
    # first preflight run produced a false negative for exactly this reason.
    db.tx(lambda cur: cur.execute('ANALYZE axiom_memory'))


def plan_for(tenant_id: uuid.UUID, *, context_key: bool = True, k: int = 5) -> str:
    vec = db.vector_literal(embeddings.embed_list(QUERY_TEXT))
    params = {'tenant': str(tenant_id), 'cls': str(MemoryClass.EPISODIC),
              'rc': str(RetrievalClass.ACTIONABLE), 'vec': vec, 'fetch': k * 4}
    if context_key:
        params['ck'] = CONTEXT_KEY
    return db.tx(lambda cur: db.explain(
        cur, memory.recall_sql_for_explain(context_key=context_key), params), readonly=True)


@pytest.fixture(scope='module')
def corpus() -> World:
    """The durable 2,500-row fixture tenant. Built once, topped up if short, never dropped."""
    def _ensure(cur):
        cur.execute("""
            INSERT INTO axiom_tenant (id, slug, display_name)
            VALUES (%s, 'axiom-ann-plan-fixture', 'ANN plan fixture (durable)')
            ON CONFLICT (id) DO NOTHING
        """, (str(CORPUS_TENANT),))
        cur.execute('SELECT count(*) AS n FROM axiom_memory WHERE tenant_id = %s',
                    (str(CORPUS_TENANT),))
        return int(cur.fetchone()['n'])

    have = db.tx(_ensure)
    if have < CORPUS_ROWS:
        load(CORPUS_TENANT, have + 1, CORPUS_ROWS)
    return World(CORPUS_TENANT, uuid.uuid4(), 0, 0)


@pytest.fixture
def ladder_world() -> World:
    """A throwaway tenant for the small end of the ladder. Torn down, so kept small."""
    w = _create_world(budget_cents=1, policy_max_cents=1, requires_approval=False)
    try:
        yield w
    finally:
        _destroy_world(w)


def test_recall_plan_uses_the_vector_index_from_the_very_first_row(ladder_world):
    """Walk a row-count ladder on a fresh tenant and require the ANN path at every rung.

    The ladder exists because the optimizer's choice is cost-based and could regress with
    a version bump or a statistics change. If a future cluster only picks the index above
    some row count, this test is what tells you the number instead of leaving you to
    discover it in production.
    """
    ladder: dict[int, bool] = {}
    loaded = 0
    for target in (0, 10, 100):
        if target > loaded:
            load(ladder_world.tenant_id, loaded + 1, target)
            loaded = target
        ladder[target] = db.uses_vector_index(plan_for(ladder_world.tenant_id))

    assert all(ladder.values()), (
        f'the ANN path was not selected at every row count: {ladder}\n'
        f'last plan:\n{plan_for(ladder_world.tenant_id)}')


@pytest.mark.slow
def test_recovery_recall_uses_the_context_index_at_scale(corpus):
    """The recovery path's exact query, over 2,500 memories, must be index-accelerated.

    All four prefix columns are pinned to exact values, which is the only way the index
    is usable at all. A range predicate on any of them — `trust_level >= 2` is the
    tempting one — would disable it entirely, which is why trust is folded into the
    computed retrieval_class instead of filtered here.
    """
    plan = plan_for(corpus.tenant_id)
    assert db.uses_vector_index(plan), plan
    assert 'axiom_memory_ann_by_context' in plan, f'wrong index chosen:\n{plan}'
    assert 'FULL SCAN' not in plan
    assert 'prefix spans' in plan


@pytest.mark.slow
def test_broad_recall_plan_uses_the_second_vector_index(corpus):
    """The no-context_key form must hit the tenant-wide index, not degrade to a scan.

    Two vector indexes on one column doubles vector write cost. That trade is only worth
    paying if the second one is actually selected, so assert it rather than assume it.
    """
    plan = plan_for(corpus.tenant_id, context_key=False)
    assert db.uses_vector_index(plan), plan
    assert 'axiom_memory_ann_by_tenant' in plan, f'wrong index chosen:\n{plan}'


@pytest.mark.slow
def test_subquery_search_vector_defeats_the_index(corpus):
    """The negative control, and the reason db.vector_literal() exists.

    preflight gate 4 established this on a scratch table; asserting it here against the
    real schema is what stops someone inlining a `SELECT ... FROM generate_series` into
    an ORDER BY and shipping a full scan that returns perfectly correct rows.
    """
    subquery_form = f"""
        SELECT id FROM axiom_memory
        WHERE tenant_id = %(tenant)s AND memory_class = %(cls)s
          AND context_key = %(ck)s AND retrieval_class = %(rc)s
        ORDER BY embedding <=> (
            SELECT array_agg(sin((0.31 * 0.7 + d * 0.013)::FLOAT8) ORDER BY d)::VECTOR({EMBED_DIMS})
            FROM generate_series(1, {EMBED_DIMS}) AS dims(d))
        LIMIT 20
    """
    plan = db.tx(lambda cur: db.explain(cur, subquery_form, {
        'tenant': str(corpus.tenant_id), 'cls': str(MemoryClass.EPISODIC),
        'ck': CONTEXT_KEY, 'rc': str(RetrievalClass.ACTIONABLE)}), readonly=True)

    assert not db.uses_vector_index(plan), (
        'a subquery search vector now uses the index — good news, but axiom/db.py and '
        f'the README document the opposite and must be updated:\n{plan}')
    assert 'FULL SCAN' in plan


@pytest.mark.slow
def test_recall_actually_retrieves_the_nearest_memory_at_scale(corpus):
    """A plan assertion with no retrieval assertion would be satisfied by returning junk.

    Insert one memory whose embedding is exactly the query vector, bury it in 2,500
    synthetic neighbours, require the ANN to rank it first and to return a full k — then
    delete only that row, which is why this test can afford to write to the durable
    corpus at all.
    """
    text = f'{QUERY_TEXT} :: needle {uuid.uuid4().hex[:12]}'
    needle = corpus.remember(text, context_key=CONTEXT_KEY)
    try:
        hits = corpus.recall(text, context_key=CONTEXT_KEY, k=5)
        assert len(hits) == 5, 'the ANN returned fewer than k rows'
        assert hits[0].id == needle
        assert hits[0].distance == pytest.approx(0.0, abs=1e-6)
        assert hits[0].similarity == pytest.approx(1.0, abs=1e-6)
        assert all(h.distance <= hits[-1].distance for h in hits), 'results are not ranked'
    finally:
        db.tx(lambda cur: cur.execute(
            'DELETE FROM axiom_event WHERE tenant_id = %s AND subject_id = %s',
            (str(CORPUS_TENANT), str(needle))))
        db.tx(lambda cur: cur.execute('DELETE FROM axiom_memory WHERE id = %s', (str(needle),)))
