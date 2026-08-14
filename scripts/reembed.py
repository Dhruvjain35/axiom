#!/usr/bin/env python3
"""AXIOM :: move the memory corpus into the vector space the process actually produces.

    DATABASE_URL=... AXIOM_OFFLINE=0 python scripts/reembed.py --dry-run
    DATABASE_URL=... AXIOM_OFFLINE=0 python scripts/reembed.py

WHY THIS EXISTS
---------------
AXIOM has two embedders: Amazon Bedrock Titan Text Embeddings V2, and a deterministic
local stand-in used when AXIOM_OFFLINE=1 so the invariant suite can run without network
or credentials. They produce vectors of the same shape in different spaces.

A cosine query against a table holding a mixture does not fail. It returns rows — the
wrong rows, ranked confidently, with no error anywhere. That is the single worst failure
mode in this system, because every test that asserts "recall returned something" passes
while recall has quietly stopped meaning anything.

So when the embedder changes, the corpus has to move with it. This is the tool that
moves it, and preflight gate 17 is what refuses to let the two drift apart unnoticed.

WHAT IT DOES
------------
Selects every axiom_memory row whose embedding_model is not the model this process would
produce, re-embeds its content, and writes the vector and the model together in one
statement. Resumable by construction: the work list is defined by the mismatch, so an
interrupted run leaves a smaller work list rather than a corrupt one, and re-running is
always safe.

The reverse direction works too. Running it with AXIOM_OFFLINE=1 moves the corpus back to
the stand-in — which is the rollback, and the reason no copy of the old vectors is kept:
the offline embedder is deterministic, so the previous state is reproducible rather than
merely recoverable.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from axiom import db, embeddings                                    # noqa: E402
from axiom.config import settings                                   # noqa: E402

# Titan Text Embeddings V2, on-demand, us-east-2. Recorded here so the run can print what
# it is about to spend rather than leaving it to be discovered on a bill.
USD_PER_1K_TOKENS = 0.00002
CHARS_PER_TOKEN = 4          # Titan's own rough guidance for English prose


def survey(cur) -> list[dict]:
    cur.execute("""
        SELECT embedding_model AS model,
               count(*)        AS rows,
               sum(length(content))::INT8 AS chars
        FROM axiom_memory GROUP BY 1 ORDER BY 2 DESC
    """)
    return cur.fetchall()


def main() -> int:
    ap = argparse.ArgumentParser(
        description='Re-embed axiom_memory into the active vector space')
    ap.add_argument('--dry-run', action='store_true',
                    help='report what would change and what it would cost; write nothing')
    ap.add_argument('--batch', type=int, default=64,
                    help='rows per transaction (default 64)')
    ap.add_argument('--limit', type=int, default=0,
                    help='stop after N rows; 0 means all')
    args = ap.parse_args()

    target = embeddings.MODEL_ID
    print(f'\n  active embedder   {target}')
    print(f'  offline mode      {settings.offline}')
    print(f'  database          {db.redacted_url() if hasattr(db, "redacted_url") else "(from DATABASE_URL)"}\n')

    with db.pool().connection() as c, c.cursor() as cur:
        rows = survey(cur)
    print('  corpus as it stands')
    stale_rows = stale_chars = 0
    for r in rows:
        mark = 'ok' if r['model'] == target else 'STALE'
        print(f"    {r['model']:<34} {r['rows']:>6} rows  {int(r['chars']):>8} chars  {mark}")
        if r['model'] != target:
            stale_rows += int(r['rows'])
            stale_chars += int(r['chars'])

    if not stale_rows:
        print(f'\n  nothing to do — every row is already {target}\n')
        return 0

    tokens = stale_chars / CHARS_PER_TOKEN
    cost = tokens / 1000 * USD_PER_1K_TOKENS
    print(f'\n  to move           {stale_rows} rows · ~{tokens:,.0f} tokens · '
          f'~${cost:.4f} at {USD_PER_1K_TOKENS}/1k')

    if args.dry_run:
        print('\n  --dry-run: nothing written\n')
        return 0

    # A single SELECT of the ids up front, then batched updates. Holding one long
    # transaction over 2,500 embeddings would sit open across thousands of Bedrock
    # round-trips, which is how a migration ends up fighting the demo it is migrating.
    with db.pool().connection() as c, c.cursor() as cur:
        cur.execute("""SELECT id, tenant_id, content FROM axiom_memory
                       WHERE embedding_model != %s ORDER BY created_at""", (target,))
        work = cur.fetchall()
    if args.limit:
        work = work[:args.limit]

    print(f'\n  re-embedding {len(work)} rows in batches of {args.batch}\n')
    t0 = time.time()
    done = failed = 0
    for i in range(0, len(work), args.batch):
        batch = work[i:i + args.batch]
        vecs = []
        for row in batch:
            try:
                vecs.append((row['id'], row['tenant_id'],
                             db.vector_literal(embeddings.embed_list(row['content']))))
            except Exception as e:                       # noqa: BLE001
                failed += 1
                print(f"    ! {row['id']} {type(e).__name__}: {str(e)[:90]}")

        if vecs:
            # Vector and model in the SAME statement. Writing them separately opens a
            # window in which the row claims a space it is not in, which is the exact
            # condition this script exists to remove.
            with db.pool().connection() as c, c.cursor() as cur:
                cur.executemany("""
                    UPDATE axiom_memory
                    SET embedding = %s::VECTOR(1024), embedding_model = %s
                    WHERE tenant_id = %s AND id = %s
                """, [(v, target, str(t), str(mid)) for mid, t, v in vecs])
            done += len(vecs)

        pct = 100.0 * (i + len(batch)) / len(work)
        print(f'    {done:>6}/{len(work)}  {pct:5.1f}%  {time.time() - t0:6.1f}s')

    with db.pool().connection() as c, c.cursor() as cur:
        after = survey(cur)
    print('\n  corpus now')
    for r in after:
        print(f"    {r['model']:<34} {r['rows']:>6} rows")

    spaces = {r['model'] for r in after}
    print(f'\n  {done} moved · {failed} failed · {time.time() - t0:.1f}s')
    if spaces == {target}:
        print(f'  PASS — the corpus is one space, and it is {target}\n')
        db.close_pool()
        return 0
    print(f'  INCOMPLETE — corpus holds {len(spaces)} spaces: {sorted(spaces)}\n')
    db.close_pool()
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
