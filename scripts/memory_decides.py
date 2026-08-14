#!/usr/bin/env python3
"""AXIOM :: the moment memory changes the answer.

    python scripts/memory_decides.py

Every other demo in this repository proves the EXECUTION half: a crash cannot cause a
second refund. This one proves the half the project is actually named after, and it is the
half that is easiest to fake. A system can retrieve memories, print them, and then ignore
them entirely — the recovery would look identical. Read the chaos demo's own output and
you will find recall reported on every recovery and never once changing the decision:

    "re-dispatching under the same key (5 comparable recoveries recalled, none adverse)"

That sentence is true and it is also unfalsifiable as written. So this runs the SAME
recovery three times against the SAME crashed task, changing only what is in memory:

    1  five comparable recoveries, none adverse            -> RESEND
    2  add two DUPLICATE_EFFECT memories at high similarity -> ESCALATE
    3  quarantine those two, inside ONE transaction         -> RESEND again

Nothing about the task changes between runs. Not the receipt, not the fence, not the
policy, not the amount. The only thing that moves is memory, and the decision moves with
it — in both directions.

Step 3 is the one worth staring at. Quarantining is a plain UPDATE, but `quarantined`
feeds `retrieval_class`, which is a computed STORED column used as a VECTOR INDEX PREFIX.
So the row physically moves to a different partition of the index inside that transaction.
There is no reindex, no cache to invalidate, and no window in which a memory known to be
poisoned is still steering an irreversible act. At COMMIT it is simply gone from the
candidate set — not filtered out of the results afterwards, which would silently return
fewer than LIMIT rows and miss true neighbours.

The plan is printed at the end because "we used the vector index" is exactly the kind of
claim that is easy to make and easy to have quietly stopped being true.

WHERE THE LOGIC LIVES, AND WHY IT IS NOT IN THIS FILE
-----------------------------------------------------
In `axiom/proofs.py`, because `POST /api/proof/memory` runs the identical proof from the
browser and two copies of a correctness demonstration will drift — the first anyone hears
about it being the day the page says PASS on camera while this script says INCONCLUSIVE.
This file is the terminal's renderer for that one implementation.

Two things changed when it moved, both for the sake of the public endpoint, and both make
this script better-behaved too:

  * each run builds and deletes its OWN tenant, seeded with the same corpus as the demo,
    so running this no longer leaves quarantined memories in the tenant Mission Control
    is showing. `--keep` leaves the tenant behind for inspection.
  * step 3 quarantines exactly the two memories the run planted, by id, rather than
    everything adverse that came back — a proof that erodes its own corpus a little on
    every press is not one you can leave on a public URL for a month.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from axiom import db, proofs                                        # noqa: E402

BOLD, OFF = '\033[1m', '\033[0m'


def main() -> int:
    ap = argparse.ArgumentParser(description='does memory actually decide anything?')
    ap.add_argument('--keep', action='store_true',
                    help="leave the run's tenant in place instead of deleting it")
    args = ap.parse_args()

    out = proofs.memory_decides(keep=args.keep, budget_seconds=120.0)

    print(f'\n  task {out["order_ref"]} is stopped at the crash instant (W4)')
    print(f'  receipt {str(out["idempotency_key"])[:44]}…')
    print('  nothing below changes the task, the receipt, the fence, or the policy.\n')

    line = '  ' + '─' * 76
    for s in out['steps']:
        print(line)
        print(f'  {s["n"]}  {s["label"]:<40} -> {BOLD}{s["action"]}{OFF}')
        print(f'     {s["rationale"]}')
        for r in s['recalled']:
            mark = '!' if r['adverse'] else ' '
            print(f'     {mark} {r["similarity"]:.3f}  {r["outcome"]:<18} '
                  f'{r["content"][:60]}…')
    print(line)

    print(f'\n  recall query plan — vector index used: {out["plan_uses_vector_index"]}')
    for ln in out['plan'].splitlines():
        if any(k in ln for k in ('vector search', 'prefix spans', 'scan', 'table:')):
            print(f'    {ln.strip()}')

    print()
    if out['verdict'] == 'PASS':
        print('  PASS — memory changed the decision in both directions, and the '
              'quarantine\n  took effect inside the transaction that asked. '
              'The index was used, not scanned.\n')
    else:
        # If the votes did not flip, this run proved nothing. Say so rather than printing
        # a pass over a demo that did not demonstrate its claim.
        actions = ' / '.join(s['action'] for s in out['steps']) or 'no steps ran'
        print(f'  INCONCLUSIVE — {actions}, '
              f'index_used={out["plan_uses_vector_index"]}'
              + (f'\n  {out["error"]}' if out.get('error') else '') + '\n')

    if args.keep:
        print(f'  tenant {out["tenant_id"]} kept.\n')

    db.close_pool()
    return 0 if out['verdict'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
