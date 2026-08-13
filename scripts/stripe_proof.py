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
4. Waits out the lease, lets a second worker claim the orphaned task, read the receipt,
   and re-dispatch under the SAME key.
5. Asks Stripe: how many refunds exist for that charge?

The answer that matters is not "one". It is "one, and Stripe told us the second request
was a replay" — the `idempotent-replayed: true` header. Zero duplicates with zero replays
would only mean nothing was ever retried.

Note what AXIOM is and is not doing here. Stripe already refuses to double-charge a
repeated idempotency key; that is Stripe's contribution and it is a good one. But Stripe
can only honour a key it is handed, and an agent that regenerates its key after a crash
gets a second refund from a provider that was willing to prevent one. The key surviving
the crash is the part AXIOM supplies.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from axiom import db, embeddings, stripe_provider, tasks       # noqa: E402
from axiom.models import AttemptState, Outcome, TaskState      # noqa: E402
from axiom.provider import ProviderCrash                       # noqa: E402
from axiom.seed import DEMO_TENANT                             # noqa: E402

AMOUNT = 30_000        # $300.00 — the number the whole project opens with


def main() -> int:
    ap = argparse.ArgumentParser(description='AXIOM vs a real Stripe sandbox')
    ap.add_argument('--amount-cents', type=int, default=AMOUNT)
    args = ap.parse_args()

    if not stripe_provider.available():
        sys.exit('set AXIOM_STRIPE_KEY to a Stripe test secret key (sk_test_...)')

    run = uuid.uuid4().hex[:6]
    order = f'AXM-STRIPE-{run}'
    print(f'\n  order {order} · ${args.amount_cents / 100:,.2f} · Stripe test mode\n')

    # ---------------------------------------------------------------- 1. a real charge
    charge = stripe_provider.create_test_charge(args.amount_cents, order)
    print(f'  1  charge created            {charge}')

    # ------------------------------------------------------- 2. an ordinary AXIOM task
    agent_a = db.tx(lambda cur: tasks.register_agent(cur, worker_ref=f'stripe-a-{run}'))
    agent_b = db.tx(lambda cur: tasks.register_agent(cur, worker_ref=f'stripe-b-{run}'))
    mission = db.tx(lambda cur: tasks.create_mission(
        cur, tenant_id=DEMO_TENANT, title='Stripe proof',
        goal='one refund, one crash, one real provider',
        budget_cents=args.amount_cents * 4, created_by='human:stripe-proof'))

    payload = {'order_ref': order, 'amount_cents': args.amount_cents,
               'charge_id': charge, 'description': 'duplicate charge on customer card',
               'exception_kind': 'duplicate_charge'}
    task_id = db.tx(lambda cur: tasks.enqueue(
        cur, tenant_id=DEMO_TENANT, mission_id=mission, task_type='refund',
        dedupe_key=f'order:{order}:refund', payload=payload))

    claimed = db.tx(lambda cur: tasks.claim(cur, agent_id=agent_a, task_id=task_id))
    request_body = {'order_ref': order, 'amount_cents': args.amount_cents,
                    'currency': 'usd', 'charge_id': charge}

    def _prepare(task, agent):
        return db.tx(lambda cur: tasks.prepare(
            cur, task=task, agent_id=agent, step_name='refund',
            provider_name='stripe', operation='refunds.create',
            request_body=request_body, amount_cents=args.amount_cents))

    prepared = _prepare(claimed, agent_a)
    if prepared.parked:      # $300 exceeds the demo policy's unattended ceiling
        db.tx(lambda cur: tasks.decide_approval(
            cur, tenant_id=DEMO_TENANT, approval_id=prepared.approval_id, approved=True,
            decided_by='ops@axiom.demo', note='stripe proof'))
        claimed = db.tx(lambda cur: tasks.claim(cur, agent_id=agent_a, task_id=task_id))
        prepared = _prepare(claimed, agent_a)
        print('  2  policy stopped it, a human approved')

    receipt = prepared.receipt
    print(f'  3  receipt committed         {receipt.idempotency_key[:38]}…')

    # ------------------------------------------ 4. the refund lands, then worker A dies
    crashed = False
    try:
        stripe_provider.create_refund(
            idempotency_key=receipt.idempotency_key, order_ref=order,
            amount_cents=args.amount_cents, request_body=request_body,
            charge_id=charge, chaos_post=1.0)
    except ProviderCrash:
        crashed = True
    print(f'  4  refund sent to Stripe, worker A KILLED before recording it '
          f'({"crashed" if crashed else "NO CRASH — inconclusive"})')

    # ------------------------------------------------------- 5. worker B takes it over
    db.tx(lambda cur: cur.execute(
        "UPDATE axiom_task SET available_at = now() - INTERVAL '1 second' WHERE id = %s",
        (str(claimed.id),)))
    recovered = db.tx(lambda cur: tasks.claim(cur, agent_id=agent_b, task_id=task_id))
    situation = 'duplicate_charge: duplicate charge on customer card'
    vec = embeddings.embed_list(situation)
    plan = db.tx(lambda cur: tasks.recover(
        cur, task=recovered, agent_id=agent_b, situation_embedding=vec,
        step_name='refund'))
    print(f'  5  worker B recovered        fence e{claimed.lease_epoch} -> '
          f'e{recovered.lease_epoch} · {plan.action}')

    result = stripe_provider.create_refund(
        idempotency_key=plan.receipt.idempotency_key, order_ref=order,
        amount_cents=args.amount_cents, request_body=plan.receipt.request_body,
        charge_id=charge)
    print(f'  6  re-sent under the SAME key → {result.provider_ref}  '
          f'{"REPLAYED by Stripe" if result.replayed else "CREATED (!!)"}')

    db.tx(lambda cur: tasks.settle(
        cur, task=recovered, agent_id=agent_b, receipt=plan.receipt,
        outcome_state=AttemptState.SUCCEEDED, task_state=TaskState.SUCCEEDED,
        response_body=result.body, provider_ref=result.provider_ref,
        http_status=result.status,
        memory_content=f'{situation} | stripe replayed {result.provider_ref}',
        memory_embedding=embeddings.embed_list(
            f'{situation} | stripe replayed {result.provider_ref}'),
        memory_outcome=Outcome.RESOLVED))

    for a in (agent_a, agent_b):
        db.tx(lambda cur, a=a: tasks.stop_agent(cur, agent_id=a))

    # ------------------------------------------------------------- 7. ask STRIPE, not us
    time.sleep(1.2)                       # Stripe's list API is read-after-write eventual
    mine = [r for r in stripe_provider.ledger(100) if r['order_ref'] == order]
    dupes = stripe_provider.duplicate_check([order])

    print('\n' + '=' * 74)
    print("  STRIPE'S OWN LEDGER — not AXIOM's")
    print('=' * 74)
    for r in mine:
        print(f"    {r['provider_ref']}  {r['order_ref']}  "
              f"${r['amount_cents'] / 100:>9,.2f}  {r['status']}")
    print('-' * 74)
    print(f'    refunds for this order        {len(mine)}')
    print(f'    Stripe reported a replay      {result.replayed}')
    print(f'    duplicate refunds             {len(dupes)}')
    print(f'    charge                        {charge}')
    print('=' * 74)

    ok = crashed and len(mine) == 1 and result.replayed and not dupes
    if ok:
        print('\n  PASS — the money moved once. Stripe confirms the second request was a '
              'replay,\n  not a second refund. The key survived the crash.\n')
    else:
        # An honest failure mode: if the crash did not fire, or Stripe did not report a
        # replay, this run demonstrated nothing and must not print PASS.
        print(f'\n  INCONCLUSIVE — crashed={crashed} refunds={len(mine)} '
              f'replayed={result.replayed} duplicates={len(dupes)}\n')
    db.close_pool()
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
