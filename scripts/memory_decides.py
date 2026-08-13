#!/usr/bin/env python3
"""AXIOM :: the moment memory changes the answer.

    python scripts/memory_decides.py

Every other demo in this repository proves the EXECUTION half: a crash cannot cause a
second refund. This one proves the half the project is actually named after, and it is the
half that is easiest to fake. A system can retrieve memories, print them, and then ignore
them entirely — the recovery would look identical. Read the chaos demo's own output and
you will find recall reported on every recovery and never once changing the decision:

    "re-dispatching under the same key (5 comparable recoveries recalled, none adverse)"

That sentence is true and it is also unfalsifiable as written. So this script runs the
SAME recovery three times against the SAME crashed task, changing only what is in memory:

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
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from axiom import db, embeddings, memory, tasks                      # noqa: E402
from axiom.db import uses_vector_index                               # noqa: E402
from axiom.models import (MemoryClass, Outcome, RetrievalClass,      # noqa: E402
                          TaskState, Trust, ctx_state)
from axiom.seed import DEMO_TENANT                                   # noqa: E402

SITUATION = 'duplicate_charge: duplicate charge on customer card'

# Deliberately phrased like the memories seed.py writes, because similarity is the whole
# mechanism: a memory that does not come back at high rank cannot change a decision, and
# an adverse memory about an unrelated situation SHOULD NOT change it.
ADVERSE = [
    'agent died mid-refund on a duplicate_charge task; the recovering worker re-planned '
    'from the transcript instead of the receipt and the customer was refunded twice',
    'agent died mid-refund on a duplicate_charge task; a second refund reached the '
    'provider before the first was recorded; duplicate effect confirmed on the ledger',
]


def recover_once(task, agent_id, vec) -> tasks.RecoveryPlan:
    """One recovery, in one transaction. Identical call every time."""
    return db.tx(lambda cur: tasks.recover(
        cur, task=task, agent_id=agent_id, situation_embedding=vec, step_name='refund'))


def main() -> int:
    ap = argparse.ArgumentParser(description='does memory actually decide anything?')
    ap.add_argument('--keep', action='store_true',
                    help='leave the adverse memories quarantined rather than deleting')
    args = ap.parse_args()

    run = uuid.uuid4().hex[:6]
    order = f'MEM-{run}'
    vec = embeddings.embed_list(SITUATION)

    # ---------------------------------------------- a task stopped at the crash instant
    agent = db.tx(lambda cur: tasks.register_agent(cur, worker_ref=f'mem-{run}'))
    mission = db.tx(lambda cur: tasks.create_mission(
        cur, tenant_id=DEMO_TENANT, title='Does memory decide?',
        goal='same crash, same task, different memory', budget_cents=100_000,
        created_by='human:memory-demo'))
    task_id = db.tx(lambda cur: tasks.enqueue(
        cur, tenant_id=DEMO_TENANT, mission_id=mission, task_type='refund',
        dedupe_key=f'order:{order}:refund',
        payload={'order_ref': order, 'amount_cents': 15_000,
                 'description': 'duplicate charge on customer card',
                 'exception_kind': 'duplicate_charge'}))

    claimed = db.tx(lambda cur: tasks.claim(cur, agent_id=agent, task_id=task_id))
    prepared = db.tx(lambda cur: tasks.prepare(
        cur, task=claimed, agent_id=agent, step_name='refund',
        provider_name='payments', operation='refunds.create',
        request_body={'order_ref': order, 'amount_cents': 15_000, 'currency': 'USD'},
        amount_cents=15_000))
    receipt = prepared.receipt
    # No provider call and no settle: the task is now sitting in ACTION_PREPARED with a
    # live receipt. That is crash window W4, held still so it can be interrogated.

    print(f'\n  task {order} is stopped at the crash instant (W4)')
    print(f'  receipt {receipt.idempotency_key[:44]}…')
    print(f'  nothing below changes the task, the receipt, the fence, or the policy.\n')

    line = '  ' + '─' * 76

    # ------------------------------------------------------------------- 1. as it stands
    p1 = recover_once(claimed, agent, vec)
    print(line)
    print(f'  1  memory as seeded                        -> \033[1m{p1.action}\033[0m')
    print(f'     {p1.rationale}')

    # ------------------------------------------- 2. two adverse memories at high rank
    for text in ADVERSE:
        memory.remember(
            db.tx, tenant_id=DEMO_TENANT, memory_class=MemoryClass.EPISODIC,
            context_key=ctx_state(TaskState.ACTION_PREPARED), content=text,
            outcome=Outcome.DUPLICATE_EFFECT, source='system:execution',
            trust_level=Trust.FIRST_PARTY, actor='human:memory-demo')

    p2 = recover_once(claimed, agent, vec)
    print(line)
    print(f'  2  + 2 memories of a DUPLICATE EFFECT      -> \033[1m{p2.action}\033[0m')
    print(f'     {p2.rationale}')

    # ------------------------------- 3. quarantine them, in ONE transaction, and re-ask
    def _quarantine_and_reask(cur):
        """Both the quarantine AND the recovery, in a single transaction.

        Doing them together is not a shortcut, it is the assertion: the recall below runs
        in the same transaction that just moved those rows to a different partition of
        the vector index, and it does not see them. There is no interval to lose a race in.
        """
        n = 0
        for r in memory.recall(cur, tenant_id=DEMO_TENANT, embedding=vec,
                               memory_class=MemoryClass.EPISODIC,
                               context_key=ctx_state(TaskState.ACTION_PREPARED),
                               retrieval_class=RetrievalClass.ACTIONABLE, k=8):
            if r.outcome == str(Outcome.DUPLICATE_EFFECT):
                memory.quarantine(cur, tenant_id=DEMO_TENANT, memory_id=r.id,
                                  reason='demo: shown to be a mis-attributed outcome',
                                  by='human:memory-demo')
                n += 1
        plan = tasks.recover(cur, task=claimed, agent_id=agent,
                             situation_embedding=vec, step_name='refund')
        return n, plan

    quarantined, p3 = db.tx(_quarantine_and_reask)
    print(line)
    print(f'  3  those {quarantined} quarantined, SAME transaction  -> \033[1m{p3.action}\033[0m')
    print(f'     {p3.rationale}')
    print(line)

    # ------------------------------------------------------------------- the query plan
    def _plan(cur):
        sql = memory.recall_sql_for_explain(context_key=True)
        return db.explain(cur, sql, None) if False else db.explain(
            cur, sql.replace('%(tenant)s', f"'{DEMO_TENANT}'")
                    .replace('%(cls)s', "'EPISODIC'")
                    .replace('%(ck)s', f"'{ctx_state(TaskState.ACTION_PREPARED)}'")
                    .replace('%(rc)s', "'ACTIONABLE'")
                    .replace('%(vec)s', f"'{db.vector_literal(vec)}'")
                    .replace('%(fetch)s', '20'))
    plan_text = db.tx(_plan, readonly=True)
    used = uses_vector_index(plan_text)
    print(f'\n  recall query plan — vector index used: {used}')
    for ln in plan_text.splitlines():
        if any(k in ln for k in ('vector search', 'prefix spans', 'scan', 'table:')):
            print(f'    {ln.strip()}')

    # ------------------------------------------------------------------------- verdict
    ok = (p1.action == 'RESEND' and p2.action == 'ESCALATE'
          and p3.action == 'RESEND' and used)
    print()
    if ok:
        print('  PASS — memory changed the decision in both directions, and the '
              'quarantine\n  took effect inside the transaction that asked. '
              'The index was used, not scanned.\n')
    else:
        # If the votes did not flip, this run proved nothing. Say so rather than
        # printing a pass over a demo that did not demonstrate its claim.
        print(f'  INCONCLUSIVE — {p1.action} / {p2.action} / {p3.action}, '
              f'index_used={used}\n')

    db.tx(lambda cur: tasks.stop_agent(cur, agent_id=agent))
    db.close_pool()
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
