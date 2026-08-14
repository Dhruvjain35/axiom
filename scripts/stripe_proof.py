#!/usr/bin/env python3
"""AXIOM :: the same crash, against real Stripe.

    AXIOM_STRIPE_KEY=sk_test_... python scripts/stripe_proof.py

Everything else in this repository proves the guarantee against a payment provider that
this repository also wrote. That is a fair way to build the thing and an unconvincing way
to finish arguing about it: a reader is entitled to suspect the simulator was written to
agree with the system. So this runs the identical crash — window W4, after the provider
has committed and before AXIOM records it — against Stripe's real API, and then asks
STRIPE what happened rather than asking AXIOM.

What it does
------------
1. Creates a real test charge, so there is something to refund.
2. Enqueues it as an ordinary AXIOM task and lets the engine claim and PREPARE it, which
   derives the idempotency key in the database from immutable columns.
3. Sends the refund to Stripe under that key — then CRASHES before recording it. The
   money has genuinely moved. AXIOM does not know.
4. Takes the orphaned task over with a second worker, which reads the receipt and
   re-dispatches under the SAME key.
5. Asks Stripe: how many refunds exist for that charge?

The answer that matters is not "one". It is "one, and Stripe told us the second request
was a replay" — the `idempotent-replayed: true` header. Zero duplicates with zero replays
would only mean nothing was ever retried.

Note what AXIOM is and is not doing here. Stripe already refuses to double-charge a
repeated idempotency key; that is Stripe's contribution and it is a good one. But Stripe
can only honour a key it is handed, and an agent that regenerates its key after a crash
gets a second refund from a provider that was willing to prevent one. The key surviving
the crash is the part AXIOM supplies.

WHERE THE LOGIC LIVES, AND WHY IT IS NOT IN THIS FILE
-----------------------------------------------------
In `axiom/proofs.py`, because `POST /api/proof/stripe` runs the identical proof from the
browser, and a correctness demonstration implemented twice is a correctness demonstration
that will eventually disagree with itself on camera. This file renders that one
implementation for a terminal.

Each run works in a tenant of its own and deletes it afterwards, so neither this script
nor the endpoint leaves scratch missions in the tenant Mission Control is showing.
`--keep` keeps it. The refund itself is REAL and stays in the Stripe sandbox on purpose —
it is the evidence, it carries `metadata[axiom_order_ref]` and
`metadata[axiom_idempotency_key]`, and it moves no real money.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from axiom import db, proofs                                       # noqa: E402

AMOUNT = 30_000        # $300.00 — the number the whole project opens with


def main() -> int:
    ap = argparse.ArgumentParser(description='AXIOM vs a real Stripe sandbox')
    ap.add_argument('--amount-cents', type=int, default=AMOUNT)
    ap.add_argument('--keep', action='store_true',
                    help="leave the run's tenant in place instead of deleting it")
    args = ap.parse_args()

    if not proofs.stripe_available():
        sys.exit('set AXIOM_STRIPE_KEY to a Stripe test secret key (sk_test_...)')

    out = proofs.stripe_proof(amount_cents=args.amount_cents, keep=args.keep,
                              budget_seconds=180.0)

    print(f'\n  order {out["order_ref"]} · ${args.amount_cents / 100:,.2f} · '
          f'Stripe test mode\n')
    for s in out['steps']:
        print(f'  {s["n"]}  {s["label"]}')
        print(f'     {s["detail"]}')

    print('\n' + '=' * 74)
    print("  STRIPE'S OWN LEDGER — not AXIOM's")
    print('=' * 74)
    for r in out['ledger']:
        print(f"    {r['provider_ref']}  {r['order_ref']}  "
              f"${r['amount_cents'] / 100:>9,.2f}  {r['status']}")
    print('-' * 74)
    print(f'    refunds for this order        {out["refunds_for_order"]}')
    print(f'    Stripe reported a replay      {out["replayed"]}')
    print(f'    duplicate refunds             {out["duplicates"]}')
    print(f'    charge                        {out["charge_id"]}')
    print(f'    dashboard                     {out["dashboard_url"]}')
    print('=' * 74)

    if out['verdict'] == 'PASS':
        print('\n  PASS — the money moved once. Stripe confirms the second request was a '
              'replay,\n  not a second refund. The key survived the crash.\n')
    else:
        # An honest failure mode: if the crash did not fire, or Stripe did not report a
        # replay, this run demonstrated nothing and must not print PASS.
        print(f'\n  INCONCLUSIVE — crashed={out["crashed"]} '
              f'refunds={out["refunds_for_order"]} replayed={out["replayed"]} '
              f'duplicates={out["duplicates"]}'
              + (f'\n  {out["error"]}' if out.get('error') else '') + '\n')

    db.close_pool()
    return 0 if out['verdict'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
