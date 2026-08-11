#!/usr/bin/env python3
"""AXIOM :: the counterexample.

Runs the SAME mission, through the SAME crash, against the SAME provider — once with a
conversation-transcript agent and once with AXIOM — and prints both ledgers side by side.

    python scripts/counterexample.py

The crash is not random here. Both agents are killed at exactly the same instant: after the
provider has committed the refund and before the agent records it (crash window W4). That is
the one instant where the two designs genuinely differ, so making it deterministic is the
difference between an argument and an anecdote.

What you should expect
----------------------
    baseline (transcript memory)   2 refunds for the same order   <- $600 out the door
    AXIOM    (execution memory)    1 refund, 1 idempotent replay

Both agents behave *reasonably* given what they can know. The baseline is not misconfigured
and it is not naive: it persists its transcript with fsync, re-reads it on restart, and
records its intent before acting. It still double-refunds, because after the crash its
memory cannot distinguish "the call never went out" from "the call went out and I died", and
because it has no durable receipt to recover the original idempotency key from.

That is the entire thesis, demonstrated rather than asserted:
memory that is not transactionally coupled to action cannot make action safe.
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import sys
import tempfile
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from axiom import db, embeddings, provider, tasks           # noqa: E402
from axiom.baseline import TranscriptAgent                  # noqa: E402
from axiom.models import AttemptState, Outcome, TaskState   # noqa: E402
from axiom.provider import ProviderCrash                    # noqa: E402
from axiom.seed import DEMO_TENANT                          # noqa: E402

# Fresh order refs per run. The provider ledger is append-only and shared — reusing a
# fixed ref would accumulate refunds across runs and inflate the baseline's count, which
# would be a rigged comparison even though the mechanism is real. (This is not
# hypothetical: the first run of this script left two rows behind and the second reported
# four.) Unique refs also mean the script never has to DELETE from the external world.
_RUN = uuid.uuid4().hex[:6]
ORDER_BASELINE = f'CE-BASELINE-{_RUN}'
ORDER_AXIOM = f'CE-AXIOM-{_RUN}'
AMOUNT = 30_000          # $300.00 — the number from the README's opening question
DESCRIPTION = 'duplicate charge on customer card'


# ------------------------------------------------------------------ the baseline run

def run_baseline(workdir: pathlib.Path) -> dict:
    """Crash after the refund lands, restart from the transcript, act again."""
    path = workdir / 'transcript.json'

    # --- process 1: dies in window W4 ---
    agent = TranscriptAgent(path)
    crashed = False
    try:
        agent.resolve(order_ref=ORDER_BASELINE, amount_cents=AMOUNT,
                      description=DESCRIPTION, chaos_post=1.0)   # certain death, post-effect
    except ProviderCrash:
        crashed = True

    # --- process 2: a fresh process, reading the same durable transcript ---
    revived = TranscriptAgent(path)          # re-reads from disk, exactly as on restart
    second = revived.resolve(order_ref=ORDER_BASELINE, amount_cents=AMOUNT,
                             description=DESCRIPTION, chaos_post=0.0)

    ledger = provider.ledger(order_ref=ORDER_BASELINE)
    return {
        'crashed': crashed,
        'reasoning': second.reasoning,
        'ledger': ledger,
        'refund_count': len(ledger),
        'total_cents': sum(int(r['amount_cents']) for r in ledger),
        'replays': sum(int(r['replay_count']) for r in ledger),
        'transcript_turns': len(revived.transcript.turns),
    }


# --------------------------------------------------------------------- the AXIOM run

def run_axiom() -> dict:
    """The identical crash, against the identical provider, through the engine."""
    agent_a = db.tx(lambda cur: tasks.register_agent(
        cur, worker_ref=f'ce-a-{uuid.uuid4().hex[:6]}'))
    agent_b = db.tx(lambda cur: tasks.register_agent(
        cur, worker_ref=f'ce-b-{uuid.uuid4().hex[:6]}'))

    mission_id = db.tx(lambda cur: tasks.create_mission(
        cur, tenant_id=DEMO_TENANT, title='Counterexample', goal='one refund, one crash',
        budget_cents=100_000, created_by='human:counterexample'))

    dedupe = f'order:{ORDER_AXIOM}:refund:{uuid.uuid4().hex[:6]}'
    db.tx(lambda cur: tasks.enqueue(
        cur, tenant_id=DEMO_TENANT, mission_id=mission_id, task_type='refund',
        dedupe_key=dedupe,
        payload={'order_ref': ORDER_AXIOM, 'amount_cents': AMOUNT,
                 'description': DESCRIPTION, 'exception_kind': 'duplicate_charge'}))

    # --- worker A: claim, PREPARE (receipt commits), dispatch, then die in W4 ---
    claimed = db.tx(lambda cur: tasks.claim(cur, agent_id=agent_a))
    def _prepare(task, agent):
        return db.tx(lambda cur: tasks.prepare(
            cur, task=task, agent_id=agent, step_name='refund',
            provider_name='payments', operation='refunds.create',
            request_body={'order_ref': ORDER_AXIOM, 'amount_cents': AMOUNT,
                          'currency': 'USD', 'reason': 'duplicate_charge'},
            amount_cents=AMOUNT))

    prepared = _prepare(claimed, agent_a)

    # $300 exceeds the refund_authority policy's $200 unattended ceiling, so AXIOM stops
    # and asks a human BEFORE any money moves. Keep this in the demo rather than tuning it
    # away: it is a second, independent difference from the baseline, which has no notion
    # of authority at all and simply refunds $300 on its own say-so.
    gated = prepared.parked
    if gated:
        db.tx(lambda cur: tasks.decide_approval(
            cur, tenant_id=DEMO_TENANT, approval_id=prepared.approval_id,
            approved=True, decided_by='ops@acme.example',
            note='counterexample: operator authorizes the $300 refund'))
        claimed = db.tx(lambda cur: tasks.claim(cur, agent_id=agent_a))
        prepared = _prepare(claimed, agent_a)

    receipt = prepared.receipt

    crashed = False
    try:
        provider.create_refund(
            idempotency_key=receipt.idempotency_key, order_ref=ORDER_AXIOM,
            amount_cents=AMOUNT, request_body=receipt.request_body,
            chaos_post=1.0, latency_ms=30)      # same certain death, same instant
    except ProviderCrash:
        crashed = True                          # worker A is gone; it never settled

    # --- worker B: the lease lapses, B claims the task in ACTION_PREPARED ---
    db.tx(lambda cur: cur.execute(
        "UPDATE axiom_task SET available_at = now() - INTERVAL '1 second' WHERE id = %s",
        (str(claimed.id),)))
    recovered = db.tx(lambda cur: tasks.claim(cur, agent_id=agent_b))

    situation = f'duplicate_charge: {DESCRIPTION}'
    vec = embeddings.embed_list(situation)

    # THE FUSED TRANSACTION: read the receipt AND recall what happened last time an agent
    # died at this exact state AND transition, in one serializable commit.
    plan = db.tx(lambda cur: tasks.recover(
        cur, task=recovered, agent_id=agent_b, situation_embedding=vec,
        step_name='refund'))

    result = provider.create_refund(
        idempotency_key=plan.receipt.idempotency_key, order_ref=ORDER_AXIOM,
        amount_cents=AMOUNT, request_body=plan.receipt.request_body, latency_ms=30)

    db.tx(lambda cur: tasks.settle(
        cur, task=recovered, agent_id=agent_b, receipt=plan.receipt,
        outcome_state=AttemptState.SUCCEEDED, task_state=TaskState.SUCCEEDED,
        response_body=result.body, provider_ref=result.provider_ref,
        http_status=result.status,
        memory_content=f'{situation} | recovered; provider replayed {result.provider_ref}',
        memory_embedding=embeddings.embed_list(
            f'{situation} | recovered; provider replayed {result.provider_ref}'),
        memory_outcome=Outcome.RESOLVED))

    ledger = provider.ledger(order_ref=ORDER_AXIOM)
    return {
        'crashed': crashed,
        'action': plan.action,
        'rationale': plan.rationale,
        'recalled': len(plan.recalled),
        'idempotency_key': plan.receipt.idempotency_key,
        'replayed': result.replayed,
        'ledger': ledger,
        'refund_count': len(ledger),
        'total_cents': sum(int(r['amount_cents']) for r in ledger),
        'replays': sum(int(r['replay_count']) for r in ledger),
        'fence_before': claimed.lease_epoch,
        'fence_after': recovered.lease_epoch,
        'gated': gated,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description='AXIOM vs transcript memory, same crash')
    ap.add_argument('--keep', action='store_true', help='keep the baseline transcript file')
    args = ap.parse_args()

    workdir = pathlib.Path(tempfile.mkdtemp(prefix='axiom-counterexample-'))
    try:
        print(f'One order. ${AMOUNT / 100:.2f}. Both agents killed at the SAME instant:\n'
              f'after the provider committed the refund, before the agent recorded it.\n')

        base = run_baseline(workdir)
        ax = run_axiom()

        w = 36

        def cell(text: str) -> str:
            """Truncate to the column so a long reason cannot shunt the AXIOM column
            off its alignment — the table is the artifact people screenshot."""
            return text if len(text) <= w - 2 else text[:w - 3] + '…'

        print('=' * 80)
        print(f'{"":<22}{"TRANSCRIPT MEMORY":<{w}}{"AXIOM"}')
        print('=' * 80)
        rows = [
            ('killed in W4', 'yes' if base['crashed'] else 'no',
             'yes' if ax['crashed'] else 'no'),
            ('memory consulted', f'{base["transcript_turns"]} transcript turns',
             f'receipt + {ax["recalled"]} recalled memories'),
            ('policy gate', 'none — refunds $300 unattended',
             'sent to a human first' if ax['gated'] else 'within policy authority'),
            ('recovery decision', 'retry — cannot know if it landed',
             f'{ax["action"]} under the same key'),
            ('idempotency key', 'newly generated each attempt',
             ax['idempotency_key'][:26] + '…'),
            ('fence (lease_epoch)', 'n/a', f'{ax["fence_before"]} -> {ax["fence_after"]}'),
            ('', '', ''),
            ('REFUNDS CREATED', str(base['refund_count']), str(ax['refund_count'])),
            ('idempotent replays', str(base['replays']), str(ax['replays'])),
            ('DOLLARS OUT', f'${base["total_cents"] / 100:,.2f}',
             f'${ax["total_cents"] / 100:,.2f}'),
        ]
        for label, a, b in rows:
            print(f'{label:<22}{cell(a):<{w}}{b}')
        print('=' * 80)

        overcharge = base['total_cents'] - ax['total_cents']
        print(f'\nbaseline reasoning : {base["reasoning"]}')
        print(f'AXIOM  rationale   : {ax["rationale"]}')
        print(f'\nThe customer was overcharged ${overcharge / 100:,.2f} by the baseline '
              f'and ${0:,.2f} by AXIOM.')

        ok = base['refund_count'] == 2 and ax['refund_count'] == 1 and ax['replays'] >= 1
        if ok:
            print('\nPASS: same crash, same provider, same instant — two refunds vs one.')
        else:
            # Do not let a passing-looking table hide an inconclusive run.
            print(f'\nINCONCLUSIVE: expected baseline=2 refunds and AXIOM=1 with >=1 replay; '
                  f'got baseline={base["refund_count"]}, axiom={ax["refund_count"]}, '
                  f'replays={ax["replays"]}.')
        return 0 if ok else 1
    finally:
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)
        db.close_pool()
        provider.close_pool()


if __name__ == '__main__':
    raise SystemExit(main())
