"""AXIOM :: demo fixtures.

Creates the tenant, the refund-authority policy, a mission, thirty order exceptions,
and — importantly — a body of PRIOR memories.

The prior memories are not decoration. A recovery path that recalls nothing is
indistinguishable from a recovery path that has no memory at all, so a demo with an
empty axiom_memory table proves the plumbing and none of the thesis. These rows are what
the fused transaction actually finds when it asks "what happened the last time an agent
died here?".

    python -m axiom.seed --reset
"""

from __future__ import annotations

import argparse
import random
import uuid

from . import db, embeddings, memory, policy, provider, tasks
from .config import SYSTEM_TENANT
from .models import MemoryClass, Outcome, TaskState, Trust, ctx_exception, ctx_state

DEMO_TENANT = uuid.UUID('11111111-1111-1111-1111-111111111111')

EXCEPTIONS: list[tuple[str, str, int]] = [
    ('duplicate charge on customer card',                    'duplicate_charge', 30000),
    ('package marked delivered but not received',            'not_delivered',     8900),
    ('customer charged twice for order',                     'duplicate_charge',  4500),
    ('item arrived damaged, box crushed',                    'damaged',          12500),
    ('wrong item shipped, received size M instead of L',     'wrong_item',        6700),
    ('delivery late by nine days, customer requests refund',  'late_delivery',    3200),
    ('unauthorized charge, customer reports stolen card',    'fraud_suspected',  95000),
    ('package never arrived, tracking stopped in transit',   'not_delivered',    15400),
    ('double charge after retry at checkout',                'duplicate_charge',  2100),
    ('screen cracked on arrival',                            'damaged',          44900),
]


def _pick(i: int) -> tuple[str, str, int]:
    desc, kind, amount = EXCEPTIONS[i % len(EXCEPTIONS)]
    # Vary the amount so the policy's authority ceiling actually bites on some tasks and
    # the human-approval path is exercised by the demo rather than only by a test.
    jitter = 1 + ((i * 37) % 23) / 100.0
    return desc, kind, int(amount * jitter)


# Prior recoveries the system has "lived through". Two of them are adverse, which is
# what lets the ESCALATE branch of tasks.recover() be reachable in a live demo.
PRIOR_RECOVERIES: list[tuple[str, Outcome]] = [
    ('agent died mid-refund on a duplicate_charge task; re-dispatched under the same '
     'idempotency key; provider replayed the original refund; no second effect',
     Outcome.RESOLVED),
    ('agent died mid-refund on a not_delivered task; re-dispatch returned the original '
     'refund reference; ledger showed exactly one refund', Outcome.RESOLVED),
    ('agent died after dispatch on a late_delivery task; receipt was still PREPARED; '
     're-send confirmed the effect had already landed', Outcome.RESOLVED),
    ('worker crashed on a fraud_suspected refund and a second agent re-planned from the '
     'transcript instead of the receipt; customer was refunded twice',
     Outcome.DUPLICATE_EFFECT),
    ('recovery on a fraud_suspected chargeback could not determine provider state and '
     'required a human to reconcile by hand', Outcome.HUMAN_REQUIRED),
]

PRIOR_SEMANTIC: list[tuple[str, str]] = [
    ('duplicate_charge', 'duplicate charge disputes are almost always resolved by '
                         'refunding the second charge in full'),
    ('not_delivered', 'packages marked delivered but not received are refunded once '
                      'carrier investigation exceeds seven days'),
    ('damaged', 'damaged goods are normally reshipped rather than refunded when the '
                'item is still in stock'),
    ('fraud_suspected', 'suspected fraud is never auto-refunded; it goes to the risk '
                        'team with the charge frozen'),
    ('wrong_item', 'wrong item shipped is resolved by a replacement plus a prepaid '
                   'return label'),
]


def reset() -> None:
    """Wipe demo state. Deletes only the demo tenant's rows, plus the external ledger."""
    def _wipe(cur):
        for table in ('axiom_event', 'axiom_approval', 'axiom_action_attempt',
                      'axiom_memory', 'axiom_task', 'axiom_mission', 'axiom_policy'):
            cur.execute(f'DELETE FROM {table} WHERE tenant_id = %s', (str(DEMO_TENANT),))
        cur.execute('DELETE FROM axiom_event WHERE tenant_id = %s', (str(SYSTEM_TENANT),))
        cur.execute('DELETE FROM axiom_agent WHERE tenant_id = %s', (str(SYSTEM_TENANT),))
    db.tx(_wipe)
    provider.reset()


def seed(n_tasks: int = 30, budget_cents: int = 2500_00) -> dict:
    # Embeddings are computed BEFORE the transactions that use them: db.tx() re-runs its
    # callable on 40001 and embedding inside would re-hit Bedrock on every retry.
    prior_vecs = [(c, o, embeddings.embed_list(c)) for c, o in PRIOR_RECOVERIES]
    sem_vecs = [(k, c, embeddings.embed_list(c)) for k, c in PRIOR_SEMANTIC]

    def _apply(cur):
        cur.execute("""
            INSERT INTO axiom_tenant (id, slug, display_name)
            VALUES (%s, 'acme', 'ACME Commerce') ON CONFLICT (id) DO NOTHING
        """, (str(DEMO_TENANT),))

        policy.publish(
            cur, tenant_id=DEMO_TENANT, policy_id='refund_authority', version=1,
            body={'description': 'Autonomous refund authority for order exceptions',
                  'max_auto_action_cents': 20000,
                  'escalate_kinds': ['fraud_suspected'],
                  'rationale': 'A refund above $200 is a business decision, not an '
                               'operational one, and gets a human.'},
            max_auto_action_cents=20000, requires_approval=False,
            created_by='human:ops@acme.example', activate=True,
            signature='demo-signature', signed_by='human:cfo@acme.example')

        mission_id = tasks.create_mission(
            cur, tenant_id=DEMO_TENANT, title='Resolve today\'s order exceptions',
            goal=f'Resolve {n_tasks} open order exceptions without double-refunding anyone',
            budget_cents=budget_cents, created_by='human:ops@acme.example')

        for content, outcome, vec in prior_vecs:
            memory.write(cur, tenant_id=DEMO_TENANT, memory_class=MemoryClass.EPISODIC,
                         context_key=ctx_state(TaskState.ACTION_PREPARED),
                         content=content, embedding=vec, outcome=outcome,
                         source='system:execution', trust_level=Trust.FIRST_PARTY,
                         actor='system:seed')

        for kind, content, vec in sem_vecs:
            memory.write(cur, tenant_id=DEMO_TENANT, memory_class=MemoryClass.SEMANTIC,
                         context_key=ctx_exception(kind), content=content, embedding=vec,
                         outcome=Outcome.RESOLVED, source='human:operator',
                         trust_level=Trust.VERIFIED, actor='system:seed')

        created = 0
        for i in range(n_tasks):
            desc, kind, amount = _pick(i)
            order = f'ORD-{1000 + i}'
            tid = tasks.enqueue(
                cur, tenant_id=DEMO_TENANT, mission_id=mission_id, task_type='refund',
                dedupe_key=f'order:{order}:refund',
                payload={'order_ref': order, 'description': desc,
                         'exception_kind': kind, 'amount_cents': amount},
                actor='system:seed')
            created += 1 if tid else 0

        return {'tenant_id': str(DEMO_TENANT), 'mission_id': str(mission_id),
                'tasks': created,
                'memories': len(prior_vecs) + len(sem_vecs)}

    return db.tx(_apply)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='seed AXIOM demo data')
    ap.add_argument('--reset', action='store_true', help='wipe demo rows first')
    ap.add_argument('--tasks', type=int, default=30)
    ap.add_argument('--budget-cents', type=int, default=2500_00)
    args = ap.parse_args(argv)

    if args.reset:
        reset()
        print('reset: demo tenant and provider ledger cleared')
    out = seed(n_tasks=args.tasks, budget_cents=args.budget_cents)
    print(f'seeded mission {out["mission_id"]}: {out["tasks"]} tasks, '
          f'{out["memories"]} prior memories, budget ${args.budget_cents / 100:.2f}')
    db.close_pool()
    provider.close_pool()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
