#!/usr/bin/env python3
"""AXIOM :: the chaos demo.

Runs a real mission while killing the workers, then checks the external ledger.

    python scripts/chaos_demo.py --workers 3 --kill-every 2.5

What it does
------------
1. Seeds a mission of order exceptions and a body of prior memories.
2. Spawns N worker processes.
3. SIGKILLs a random live worker every `--kill-every` seconds. SIGKILL, not SIGTERM:
   no signal handler runs, no finally block runs, no lease is politely released. This
   is what an OOM kill, a spot reclamation, and a `docker kill` all look like.
4. Restarts each killed worker, exactly as ECS would.
5. Stops when every task reaches a terminal state, then audits.

The audit is the claim
----------------------
The provider is a separate database that AXIOM cannot enlist in its transactions. If
the design is wrong, the crashes produce double refunds and they show up here. The
demo's assertion is not "it did not crash" — it crashed dozens of times on purpose —
it is:

    duplicate refunds: 0        replayed requests: > 0

Replays above zero is what proves the crashes actually landed in the dangerous window
and that recovery genuinely re-sent under the same key. A run with zero replays proved
nothing, so this script fails loudly on that too.
"""

from __future__ import annotations

import argparse
import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from axiom import db, provider, seed as seed_mod, tasks     # noqa: E402
from axiom.models import TERMINAL_STATES                    # noqa: E402


def _env(chaos_pre: float, chaos_post: float) -> dict:
    e = dict(os.environ)
    e['AXIOM_CHAOS_PRE'] = str(chaos_pre)
    e['AXIOM_CHAOS_POST'] = str(chaos_post)
    e['PYTHONUNBUFFERED'] = '1'
    return e


def spawn(i: int, env: dict, quiet: bool) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, '-m', 'axiom.worker', '--ref', f'chaos-worker-{i}'],
        cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL if quiet else None,
        stderr=subprocess.DEVNULL if quiet else None,
    )


def auto_approve(note: str = 'auto-approved by the chaos demo operator') -> int:
    """Stand in for the human on the approvals queue.

    Refunds above the policy's authority ceiling park on a human by design, so an
    unattended demo would stall forever on exactly the tasks that prove the
    human-in-the-loop path works. This answers them the way an operator would — through
    the same decide_approval() the API calls, burning the same single-use token — rather
    than by reaching into the tables.
    """
    def _decide(cur):
        pending = tasks.pending_approvals(cur, tenant_id=seed_mod.DEMO_TENANT)
        for a in pending:
            tasks.decide_approval(
                cur, tenant_id=seed_mod.DEMO_TENANT, approval_id=a['id'],
                approved=True, decided_by='ops@acme.example', note=note)
        return len(pending)
    return db.tx(_decide)


def outstanding() -> tuple[int, int, dict]:
    def _q(cur):
        cur.execute("""
            SELECT state, count(*) AS n FROM axiom_task
            WHERE tenant_id = %s GROUP BY state
        """, (str(seed_mod.DEMO_TENANT),))
        return {r['state']: r['n'] for r in cur.fetchall()}
    by_state = db.tx(_q, readonly=True)
    total = sum(by_state.values())
    done = sum(n for s, n in by_state.items() if s in {str(t) for t in TERMINAL_STATES})
    return done, total, by_state


def main() -> int:
    ap = argparse.ArgumentParser(description='AXIOM chaos demo')
    ap.add_argument('--workers', type=int, default=3)
    ap.add_argument('--tasks', type=int, default=30)
    ap.add_argument('--kill-every', type=float, default=2.5,
                    help='seconds between SIGKILLs; 0 disables killing')
    ap.add_argument('--chaos-pre', type=float, default=0.10,
                    help='P(die after PREPARE, before dispatch) — window W2')
    ap.add_argument('--chaos-post', type=float, default=0.25,
                    help='P(die after the refund lands, before settle) — window W4')
    ap.add_argument('--timeout', type=float, default=180.0)
    ap.add_argument('--quiet', action='store_true', help='hide worker logs')
    ap.add_argument('--no-seed', action='store_true')
    ap.add_argument('--auto-approve', action='store_true', default=True,
                    help='answer the approvals queue as an operator would')
    ap.add_argument('--no-auto-approve', dest='auto_approve', action='store_false')
    args = ap.parse_args()

    if not args.no_seed:
        seed_mod.reset()
        out = seed_mod.seed(n_tasks=args.tasks)
        print(f'seeded mission {out["mission_id"]}: {out["tasks"]} tasks, '
              f'{out["memories"]} prior memories\n')

    env = _env(args.chaos_pre, args.chaos_post)
    procs = {i: spawn(i, env, args.quiet) for i in range(args.workers)}
    print(f'spawned {args.workers} workers; killing one every {args.kill_every}s '
          f'(SIGKILL, no cleanup)\n')

    kills = 0
    restarts = 0
    approved = 0
    t0 = time.time()
    next_kill = t0 + args.kill_every

    try:
        while time.time() - t0 < args.timeout:
            done, total, by_state = outstanding()
            if total and done == total:
                break

            # Restart anything that died — on its own from chaos, or from our SIGKILL.
            for i, p in list(procs.items()):
                if p.poll() is not None:
                    procs[i] = spawn(i, env, args.quiet)
                    restarts += 1

            if args.auto_approve:
                approved += auto_approve()

            if args.kill_every and time.time() >= next_kill:
                victim = random.choice(list(procs))
                p = procs[victim]
                if p.poll() is None:
                    os.kill(p.pid, signal.SIGKILL)
                    kills += 1
                    print(f'  >> SIGKILL worker {victim} (pid {p.pid}) '
                          f'[{done}/{total} tasks terminal]')
                next_kill = time.time() + args.kill_every

            time.sleep(0.25)
    finally:
        for p in procs.values():
            if p.poll() is None:
                p.terminate()
        for p in procs.values():
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()

    # ------------------------------------------------------------------- the audit
    done, total, by_state = outstanding()
    stats = provider.stats()
    dupes = provider.duplicate_check()

    print('\n' + '=' * 68)
    print('AXIOM chaos demo — result')
    print('=' * 68)
    print(f'  wall clock              {time.time() - t0:6.1f}s')
    print(f'  workers SIGKILLed       {kills}')
    print(f'  worker restarts         {restarts}')
    print(f'  approvals answered      {approved}   (policy sent them to a human)')
    print(f'  tasks terminal          {done}/{total}   {by_state}')
    print('  ' + '-' * 64)
    print(f'  refunds created         {stats["refunds"]}')
    print(f'  dollars moved           ${int(stats["total_cents"]) / 100:,.2f}')
    print(f'  idempotent replays      {stats["replays"]}   '
          f'(re-sends the provider absorbed)')
    print(f'  provider verdicts       {stats["verdicts"]}')
    print(f'  DUPLICATE REFUNDS       {stats["duplicate_orders"]}')
    print('=' * 68)

    ok = True
    if dupes:
        ok = False
        print('\nFAIL: orders refunded more than once:')
        for d in dupes:
            print(f'  {d["order_ref"]}: {d["refund_count"]} refunds, '
                  f'{d["total_cents"]} cents')
    if done != total:
        ok = False
        print(f'\nFAIL: {total - done} tasks never reached a terminal state: {by_state}')
    if kills and int(stats['replays']) == 0:
        # Not a correctness failure, but the run proved nothing: no crash landed in the
        # window where an effect was already real. Say so rather than claiming a pass.
        ok = False
        print('\nINCONCLUSIVE: zero idempotent replays — no crash landed after a refund '
              'had already reached the provider. Raise --chaos-post or --kill-every.')

    if ok:
        print(f'\nPASS: {kills} kills, {stats["replays"]} re-sends absorbed by the '
              f'provider, 0 duplicate refunds.')
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
