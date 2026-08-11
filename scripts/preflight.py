#!/usr/bin/env python3
"""
AXIOM :: preflight

Proves or kills every load-bearing assumption in the architecture, with real
assertions rather than printed output a human has to squint at.

    pip install "psycopg[binary]"
    export DATABASE_URL='postgresql://user:PASSWORD@host:26257/defaultdb?sslmode=verify-full'
    python3 scripts/preflight.py

Exit code 0 means every gate passed. Non-zero means stop and read the failures;
do not build application schema on top of a red preflight.

Why this is Python and not a .sql file
--------------------------------------
Three of the gates below are inexpressible in static SQL:
  * AS OF SYSTEM TIME requires a constant evaluable at plan time, so a .sql file
    cannot capture cluster_logical_timestamp() and feed it back later. Using a
    relative interval instead is what produced the 42P01 in the first run: '-10s'
    landed before the test table existed.
  * The fixture row count has to escalate until the optimizer prefers the index.
  * The search vector must be a literal for the ANN path to be selected, and a
    literal 1024-dim vector is 1024 numbers that have to be generated.
"""

from __future__ import annotations

import os
import sys
import time
import math
import textwrap

try:
    import psycopg
except ImportError:
    sys.exit('missing dependency:  pip install "psycopg[binary]"')

DIMS = 1024                     # Bedrock Titan Text Embeddings V2
TENANT_A = '00000000-0000-0000-0000-000000000001'
TENANT_B = '00000000-0000-0000-0000-000000000002'
ROW_LADDER = [5_000, 25_000]    # escalate until the optimizer picks the index
BATCH = 1_000                   # rows per INSERT; keeps the cross-join intermediate sane

results: list[tuple[str, bool, str, bool]] = []   # (name, passed, detail, advisory)


def gate(name: str, passed: bool, detail: str = '', advisory: bool = False) -> bool:
    """A gate is BLOCKING by default: a red one must stop the build.

    `advisory=True` marks a characterization probe — a question whose answer changes
    how we write the code but does not, either way, mean the platform is broken.
    Those are recorded and printed but never fail the run, so a non-zero exit always
    means something is genuinely wrong.
    """
    results.append((name, passed, detail, advisory))
    mark = ('INFO' if passed else 'NOTE') if advisory else ('PASS' if passed else 'FAIL')
    print(f'  [{mark}] {name}' + (f' :: {detail}' if detail else ''), flush=True)
    return passed


def probe_vector(seed: float = 0.31) -> str:
    """A deterministic query vector, formatted as a SQL literal.

    Must be a literal: the docs' worked example passes
    ORDER BY embedding <-> '[0.00644,-0.00866,...]'. The first preflight run
    passed a subquery here and the optimizer fell back to a full primary-key
    scan. Gate 6 tests that hypothesis directly rather than assuming it.
    """
    vals = [math.sin(seed * 0.7 + d * 0.013) for d in range(1, DIMS + 1)]
    return '[' + ','.join(f'{v:.6f}' for v in vals) + ']'


def plan(cur, sql: str, params: tuple | None = None) -> str:
    cur.execute('EXPLAIN ' + sql, params)
    return '\n'.join(r[0] for r in cur.fetchall())


def ann_param(op: str) -> str:
    """The form application code actually wants to use: a BOUND PARAMETER.

    This is the gate that decides the data-access layer. If a placeholder defeats
    index selection the way a subquery does (gate 4), then every ANN call site has
    to interpolate a 1024-number literal into the SQL string, and that has to be
    done in exactly one audited helper — never ad hoc at a call site.
    """
    return f"""SELECT id, situation FROM axiom_preflight
               WHERE tenant_id = '{TENANT_A}'
               ORDER BY embedding {op} %s::VECTOR({DIMS})
               LIMIT 5"""


def uses_vector_index(plan_text: str) -> bool:
    """The docs show the ANN path as a '• vector search' node with prefix spans.
    A fallback shows the pkey with 'FULL SCAN' in spans."""
    return 'vector search' in plan_text.lower()


# ---------------------------------------------------------------- SQL fixtures

DDL = f"""
CREATE TABLE axiom_preflight (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  UUID NOT NULL,
    situation  STRING NOT NULL,
    embedding  VECTOR({DIMS}) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

# Generated server-side: shipping 25k x 1024 floats over the wire would dominate
# runtime. One in ten rows belongs to TENANT_B so gate 8 can prove isolation.
LOAD = f"""
INSERT INTO axiom_preflight (tenant_id, situation, embedding)
SELECT
    CASE WHEN r %% 10 = 0 THEN '{TENANT_B}'::UUID ELSE '{TENANT_A}'::UUID END,
    'synthetic order exception ' || r::STRING,
    -- Explicit FLOAT8 cast: generate_series yields INT, and INT * DECIMAL is DECIMAL,
    -- for which there is no sin() overload ("array_agg(): unknown signature: sin(decimal)").
    array_agg(sin((r * 0.7 + d * 0.013)::FLOAT8) ORDER BY d)::VECTOR({DIMS})
FROM generate_series(%s, %s) AS rows(r)
CROSS JOIN generate_series(1, {DIMS}) AS dims(d)
GROUP BY r
"""

# Loaded BEFORE the index exists, deliberately: the docs warn that large batch
# inserts of VECTOR degrade when an index is present. Load, then backfill.
COSINE_IDX = 'CREATE VECTOR INDEX axiom_pf_cos ON axiom_preflight (tenant_id, embedding vector_cosine_ops)'
L2_IDX = 'CREATE VECTOR INDEX axiom_pf_l2 ON axiom_preflight (tenant_id, embedding vector_l2_ops)'


def ann_literal(op: str, vec: str) -> str:
    return f"""SELECT id, situation FROM axiom_preflight
               WHERE tenant_id = '{TENANT_A}'
               ORDER BY embedding {op} '{vec}'::VECTOR({DIMS})
               LIMIT 5"""


def ann_subquery(op: str) -> str:
    """The original, broken form. Kept so gate 6 can isolate the variable."""
    return f"""SELECT id, situation FROM axiom_preflight
               WHERE tenant_id = '{TENANT_A}'
               ORDER BY embedding {op} (
                   SELECT array_agg(sin((0.31 * 0.7 + d * 0.013)::FLOAT8) ORDER BY d)::VECTOR({DIMS})
                   FROM generate_series(1, {DIMS}) AS dims(d))
               LIMIT 5"""


# ---------------------------------------------------------------------- driver

def main() -> int:
    dsn = os.environ.get('DATABASE_URL')
    if not dsn:
        sys.exit('set DATABASE_URL (full connection string incl. password, from the Cloud console)')

    vec = probe_vector()
    conn = psycopg.connect(dsn, autocommit=True)
    cur = conn.cursor()

    try:
        print('\n=== identity ===')
        cur.execute('SELECT version()')
        print('  ' + cur.fetchone()[0])
        cur.execute('SHOW default_transaction_isolation')
        iso = cur.fetchone()[0]
        gate('serializable by default', iso.lower().startswith('serializable'), iso)

        print('\n=== gate 1: vector indexes permitted on this cluster tier ===')
        try:
            cur.execute('SET CLUSTER SETTING feature.vector_index.enabled = true')
            cur.execute('SHOW CLUSTER SETTING feature.vector_index.enabled')
            gate('feature.vector_index.enabled settable', bool(cur.fetchone()[0]))
        except Exception as e:
            gate('feature.vector_index.enabled settable', False, str(e)[:160])
            return report()

        # Required to add an index to a non-empty table.
        cur.execute('SET sql_safe_updates = false')

        cur.execute('DROP TABLE IF EXISTS axiom_preflight')
        cur.execute(DDL)
        gate(f'VECTOR({DIMS}) column created', True)

        loaded = 0
        index_gate_passed = False
        cos_plan = l2_plan = sub_plan = ''

        for target in ROW_LADDER:
            print(f'\n=== loading fixtures to {target:,} rows ===')
            t0 = time.time()
            while loaded < target:
                hi = min(loaded + BATCH, target)
                cur.execute(LOAD, (loaded + 1, hi))
                loaded = hi
                print(f'    {loaded:,}/{target:,}', end='\r', flush=True)
            print(f'    {loaded:,} rows in {time.time() - t0:.1f}s        ')

            if not index_gate_passed:
                print('\n=== gate 2: build vector indexes and wait for backfill ===')
                t0 = time.time()
                cur.execute(COSINE_IDX)
                cur.execute(L2_IDX)
                # Do not let EXPLAIN race the schema change. SHOW JOBS WHEN COMPLETE
                # blocks until the listed jobs finish.
                cur.execute("""
                    SHOW JOBS WHEN COMPLETE (
                        SELECT job_id FROM [SHOW JOBS]
                        WHERE job_type = 'SCHEMA CHANGE'
                          AND status NOT IN ('succeeded','failed','canceled','revert-failed')
                    )
                """)
                gate('vector index backfill complete', True, f'{time.time() - t0:.1f}s')

            cur.execute('ANALYZE axiom_preflight')

            print(f'\n=== gate 3: is the ANN path chosen at {loaded:,} rows? ===')
            cos_plan = plan(cur, ann_literal('<=>', vec))
            l2_plan = plan(cur, ann_literal('<->', vec))
            cos_ok = uses_vector_index(cos_plan)
            l2_ok = uses_vector_index(l2_plan)
            print(textwrap.indent(cos_plan, '    | '))
            gate(f'cosine <=> uses vector index @ {loaded:,} rows', cos_ok)
            gate(f'L2 <-> uses vector index @ {loaded:,} rows', l2_ok)

            if cos_ok or l2_ok:
                index_gate_passed = True
                break
            print('    neither operator selected the index; escalating row count')

        if not index_gate_passed:
            gate('vector index path', False,
                 f'still full-scanning at {loaded:,} rows — see plans above')

        print('\n=== gate 4: literal vs subquery search vector (isolating the cause) ===')
        op = '<=>' if uses_vector_index(cos_plan) else '<->'
        sub_plan = plan(cur, ann_subquery(op))
        sub_ok = uses_vector_index(sub_plan)
        gate('subquery search vector ALSO uses the index', sub_ok,
             'row count alone explained the original failure' if sub_ok
             else 'confirmed: a SUBQUERY search vector defeats the index (bound params are fine — gate 4b)',
             advisory=True)

        print('\n=== gate 4b: BOUND PARAMETER search vector (what the app will do) ===')
        par_plan = plan(cur, ann_param(op), (vec,))
        par_ok = uses_vector_index(par_plan)
        gate('bound-parameter search vector uses the index', par_ok,
             'placeholders are safe; use them everywhere' if par_ok
             else 'ANN call sites MUST interpolate a literal vector — see axiom/db.py')

        print('\n=== gate 5: results are correct and tenant-scoped ===')
        cur.execute(ann_literal(op, vec))
        rows = cur.fetchall()
        gate('ANN returns rows', len(rows) > 0, f'{len(rows)} rows')
        cur.execute(f"""SELECT count(*) FROM ({ann_literal(op, vec)}) hits
                        JOIN axiom_preflight p ON p.id = hits.id
                        WHERE p.tenant_id <> '{TENANT_A}'""")
        gate('no cross-tenant leakage', cur.fetchone()[0] == 0)

        print('\n=== gate 6: THE CORE CLAIM — write + semantic recall in ONE transaction ===')
        # If an uncommitted memory is not recallable inside its own transaction,
        # the fused recovery path is impossible and AXIOM needs rethinking.
        conn.autocommit = False
        try:
            c2 = conn.cursor()
            c2.execute(f"""INSERT INTO axiom_preflight (tenant_id, situation, embedding)
                           VALUES ('{TENANT_A}', 'UNCOMMITTED: agent died at ACTION_PREPARED',
                                   '{vec}'::VECTOR({DIMS}))""")
            c2.execute(ann_literal(op, vec))
            hits = [r[1] for r in c2.fetchall()]
            gate('uncommitted memory is recallable in-transaction',
                 any('UNCOMMITTED' in h for h in hits), hits[0] if hits else '')
            in_txn_plan = plan(c2, ann_literal(op, vec))
            gate('in-transaction recall still uses the index',
                 uses_vector_index(in_txn_plan),
                 'falls back to scan for uncommitted rows' if not uses_vector_index(in_txn_plan) else '')
            conn.commit()
        finally:
            conn.autocommit = True

        print('\n=== gate 7: rewind ===')
        # Capture a real timestamp while the data definitely exists, mutate, then
        # read that exact instant back. No relative intervals.
        cur.execute('SELECT cluster_logical_timestamp()::STRING')
        ts = cur.fetchone()[0]
        cur.execute(f"SELECT count(*) FROM axiom_preflight WHERE tenant_id = '{TENANT_A}'")
        before = cur.fetchone()[0]
        time.sleep(2)
        cur.execute(f"DELETE FROM axiom_preflight WHERE tenant_id = '{TENANT_A}'")
        cur.execute(f"SELECT count(*) FROM axiom_preflight WHERE tenant_id = '{TENANT_A}'")
        after = cur.fetchone()[0]
        cur.execute(f"""SELECT count(*) FROM axiom_preflight AS OF SYSTEM TIME '{ts}'
                        WHERE tenant_id = '{TENANT_A}'""")
        rewound = cur.fetchone()[0]
        gate('rewind sees pre-delete state', after == 0 and rewound == before,
             f'now={after} rewound={rewound} expected={before}')

        # Historical semantic search: can we run ANN against a past timestamp?
        # This is the "what did the agent believe at T, and why" feature.
        #
        # AS OF SYSTEM TIME must sit on the TOP-LEVEL statement — wrapping the ANN in
        # `SELECT count(*) FROM (SELECT ... AS OF SYSTEM TIME ...)` fails with
        # "AS OF SYSTEM TIME must be provided on a top-level statement". So the whole
        # statement is timestamped and the rows are counted client-side.
        try:
            cur.execute(f"""SELECT id FROM axiom_preflight
                            AS OF SYSTEM TIME '{ts}'
                            WHERE tenant_id = '{TENANT_A}'
                            ORDER BY embedding {op} '{vec}'::VECTOR({DIMS})
                            LIMIT 5""")
            hist = cur.fetchall()
            gate('ANN query works AS OF SYSTEM TIME', len(hist) > 0, f'{len(hist)} rows at t={ts[:14]}…')
        except Exception as e:
            gate('ANN query works AS OF SYSTEM TIME', False, str(e)[:160])

        print('\n=== gate 8: how far back can rewind actually reach? ===')
        cur.execute('SHOW ZONE CONFIGURATION FROM DATABASE defaultdb')
        zone = '\n'.join(str(r[-1]) for r in cur.fetchall())
        ttl = next((ln.strip() for ln in zone.splitlines() if 'gc.ttlseconds' in ln), '')
        gate('gc.ttlseconds readable', bool(ttl), ttl or zone[:200])

        try:
            cur.execute('SELECT count(*) FROM axiom_preflight AS OF SYSTEM TIME follower_read_timestamp()')
            gate('follower reads available', True, f'{cur.fetchone()[0]} rows')
        except Exception as e:
            gate('follower reads available', False, str(e)[:160])

        print('\n=== gate 9: recall/latency knob ===')
        try:
            cur.execute('SET vector_search_beam_size = 64')
            cur.execute('SHOW vector_search_beam_size')
            gate('vector_search_beam_size tunable', True, f'set to {cur.fetchone()[0]}')
        except Exception as e:
            gate('vector_search_beam_size tunable', False, str(e)[:160])

        return report()

    finally:
        # Cleanup last, always, even on failure — the first run dropped the table
        # before the rewind assertions had run.
        try:
            conn.autocommit = True
            conn.execute('DROP TABLE IF EXISTS axiom_preflight')
        except Exception:
            pass
        conn.close()


def report() -> int:
    blocking = [r for r in results if not r[3]]
    failed = [r for r in blocking if not r[1]]
    notes = [r for r in results if r[3] and not r[1]]
    print('\n' + '=' * 72)
    print(f'{len(blocking) - len(failed)}/{len(blocking)} blocking gates passed')
    for name, _, detail, _ in failed:
        print(f'  FAILED: {name}' + (f' :: {detail}' if detail else ''))
    for name, _, detail, _ in notes:
        print(f'  NOTE:   {name}' + (f' :: {detail}' if detail else ''))
    print('=' * 72)
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
