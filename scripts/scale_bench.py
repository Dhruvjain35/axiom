#!/usr/bin/env python3
"""AXIOM :: does the queue design actually hold up, or does it just read well?

    python scripts/scale_bench.py --to 100000

db/001_schema.sql makes a specific, checkable claim, and until now it was only ARGUED.
CockroachDB's own "Understand hotspots" page names queues as an anti-pattern: they
"require data to be ordered by write, which necessitates indexing in a way that is likely
to create a hotspot", and deleting rows as they are read "tends to accumulate an ordered
set of garbage data behind the live data".

The schema's answer is three-part, and each part is measurable:

  (a) the claim index is PARTIAL on non-terminal states, so finished work LEAVES the index
  (b) tasks are NEVER deleted, so no MVCC tombstones accumulate behind the queue head
  (c) the index is prefixed by an application-assigned shard, so the head is N ranges

If (a) is real, then claim latency is a function of OUTSTANDING work, not of total work
ever done — and a queue that has processed a million tasks claims exactly as fast as one
that has processed a thousand. That is the entire argument, and it is either true or it is
not. This script settles it by loading completed tasks and timing a real claim after each
step, using the real query from axiom/tasks.py.

Honest about what this is: a single-node local cluster, so these numbers are about the
INDEX, not about distributed throughput. A flat line here does not prove the design scales
across a cluster; it proves that finished work stops being scanned, which is the specific
claim the schema comments make. Numbers from a laptop are labelled as such.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from axiom import db, tasks                        # noqa: E402
from axiom.models import CLAIMABLE_STATES          # noqa: E402
from axiom.seed import DEMO_TENANT                 # noqa: E402

_CLAIMABLE_SQL = ', '.join(f"'{s}'" for s in CLAIMABLE_STATES)


def load_completed(n: int, batch: int = 2000) -> None:
    """Insert n tasks already in a terminal state.

    Terminal on arrival because that is the population being tested: work the system has
    finished with. Generated server-side — shipping n rows over the wire would measure the
    client's socket, not the database.
    """
    mission = db.tx(lambda cur: tasks.create_mission(
        cur, tenant_id=DEMO_TENANT, title=f'scale fixture {n}',
        goal='completed work that must stop being scanned',
        budget_cents=0, created_by='system:bench'))

    done = 0
    while done < n:
        take = min(batch, n - done)
        lo = done
        db.tx(lambda cur, lo=lo, take=take: cur.execute("""
            INSERT INTO axiom_task
                (tenant_id, mission_id, task_type, dedupe_key, payload, state)
            SELECT %s, %s, 'refund',
                   'bench:' || %s || ':' || i::STRING,
                   '{"order_ref":"BENCH"}'::JSONB,
                   'SUCCEEDED'
            FROM generate_series(%s, %s) AS g(i)
        """, (str(DEMO_TENANT), str(mission), uuid.uuid4().hex[:8],
              lo + 1, lo + take)))
        done += take


def time_claim(agent_id, reps: int = 40) -> tuple[float, float]:
    """Median and p95 milliseconds for one real claim.

    Uses the production statement from tasks.claim(), not a simplified stand-in — a
    benchmark of a query nobody runs measures nothing. Each claim is immediately released
    so the measured population never shrinks.
    """
    times = []
    for _ in range(reps):
        t0 = time.perf_counter()
        claimed = db.tx(lambda cur: tasks.claim(cur, agent_id=agent_id))
        times.append((time.perf_counter() - t0) * 1000)
        if claimed:
            db.tx(lambda cur, c=claimed: cur.execute(
                """UPDATE axiom_task SET state = 'READY', lease_owner = NULL,
                          available_at = now() WHERE id = %s""", (str(c.id),)))
    times.sort()
    return statistics.median(times), times[int(len(times) * 0.95) - 1]


def index_rows() -> tuple[int, int]:
    """(rows in the partial claim index, total task rows).

    The gap between these two numbers IS the design. If the schema's claim is right, the
    left number stays tiny while the right one grows without bound.
    """
    def _q(cur):
        cur.execute(f"""
            SELECT count(*) FILTER (WHERE state IN ({_CLAIMABLE_SQL})) AS indexed,
                   count(*) AS total
            FROM axiom_task WHERE tenant_id = %s
        """, (str(DEMO_TENANT),))
        r = cur.fetchone()
        return r['indexed'], r['total']
    return db.tx(_q, readonly=True)


def main() -> int:
    ap = argparse.ArgumentParser(description='does completed work stop costing anything?')
    ap.add_argument('--to', type=int, default=100_000, help='completed tasks to reach')
    ap.add_argument('--steps', type=int, default=5)
    ap.add_argument('--reps', type=int, default=40)
    args = ap.parse_args()

    agent = db.tx(lambda cur: tasks.register_agent(
        cur, worker_ref=f'bench-{uuid.uuid4().hex[:6]}'))

    # A small, constant amount of OUTSTANDING work. This is what the claim index should
    # contain no matter how much finished work piles up beside it.
    db.tx(lambda cur: cur.execute(
        """UPDATE axiom_task SET state = 'READY', lease_owner = NULL, available_at = now()
           WHERE tenant_id = %s AND state IN ('SUCCEEDED','DEAD_LETTER') AND id IN (
               SELECT id FROM axiom_task WHERE tenant_id = %s LIMIT 30)""",
        (str(DEMO_TENANT), str(DEMO_TENANT))))

    print(f'\n  {"completed":>11}  {"in index":>9}  {"claim p50":>10}  {"claim p95":>10}')
    print('  ' + '─' * 46)

    rows = []
    per_step = args.to // args.steps
    for step in range(args.steps + 1):
        if step:
            load_completed(per_step)
        indexed, total = index_rows()
        p50, p95 = time_claim(agent, args.reps)
        rows.append((total, indexed, p50, p95))
        print(f'  {total:>11,}  {indexed:>9,}  {p50:>9.2f}ms  {p95:>9.2f}ms')

    print('  ' + '─' * 46)
    first_p50 = rows[0][2]
    last_p50 = rows[-1][2]
    growth = (last_p50 / first_p50) if first_p50 else 0
    total_growth = rows[-1][0] / max(rows[0][0], 1)

    print(f'\n  total tasks grew      {total_growth:>8.1f}x')
    print(f'  claim latency grew    {growth:>8.2f}x')
    print(f'  rows in claim index   {rows[0][1]:,} -> {rows[-1][1]:,}')

    # The claim: latency tracks OUTSTANDING work, not total work. Allow real slack — this
    # is a laptop, other things are running, and a 2x wobble on a sub-millisecond
    # measurement is noise rather than a trend. A genuinely broken design would not be 2x
    # worse after a 100x load increase, it would be orders of magnitude worse.
    ok = growth < 3.0 and rows[-1][1] < 200
    print()
    if ok:
        print(f'  PASS — {total_growth:.0f}x the completed work, {growth:.2f}x the claim '
              f'latency.\n  Finished tasks leave the partial index, so they stop being '
              f'scanned. Measured on a\n  single local node: this is a statement about '
              f'the INDEX, not about cluster throughput.\n')
    else:
        print(f'  FAIL — latency grew {growth:.2f}x with {rows[-1][1]:,} rows still in '
              f'the claim index.\n  The partial-index argument in db/001_schema.sql does '
              f'not hold as written.\n')

    db.tx(lambda cur: tasks.stop_agent(cur, agent_id=agent))
    db.close_pool()
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
