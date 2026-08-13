#!/usr/bin/env python3
"""AXIOM :: the second-domain chaos demo — bulk outbound messaging.

The same crash, at the same instant, in a workload where money is the wrong risk axis.

    python scripts/demo_domain2.py --workers 3 --kill-every 1.8

What it does
------------
1. Seeds a mission of outbound campaigns, a broadcast authority policy whose ceiling is
   measured in RECIPIENTS rather than dollars, and a body of prior memories about
   crashed sends.
2. Spawns N `python -m axiom.domains.runtime --domain broadcast` processes.
3. SIGKILLs a random live worker every `--kill-every` seconds. SIGKILL, not SIGTERM: no
   signal handler, no finally block, no politely released lease. This is what an OOM
   kill, a spot reclamation and a `docker kill` all look like.
4. Restarts each killed worker, exactly as ECS would.
5. Stops when every task is terminal, then audits the RELAY's own books.

The audit is the claim
----------------------
The relay is a separate database AXIOM cannot enlist in its transactions, and its
delivery table holds one row per person who received something — with deliberately NO
unique constraint on (campaign, recipient), because a real ESP has none either. If the
design is wrong, the crashes produce second copies in real inboxes and they show up here:

    recipients messaged twice: 0        replayed requests: > 0

Replays above zero is what proves the crashes landed in the dangerous window and that
recovery genuinely re-sent under the same key. A run with zero replays proved nothing, so
this script fails loudly on that too — the same bar scripts/chaos_demo.py holds itself to.
"""

from __future__ import annotations

import argparse
import os
import random
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from axiom import db, embeddings, memory, policy, tasks             # noqa: E402
from axiom.domains import broadcast, relay                          # noqa: E402
from axiom.models import (                                          # noqa: E402
    TERMINAL_STATES, MemoryClass, Outcome, TaskState, Trust, ctx_exception, ctx_state,
)
from axiom.risk import COMMS_RECIPIENTS, Grant, Reversibility       # noqa: E402

# A tenant of its own, deliberately NOT the refund demo's. axiom_mission.spent_cents is
# one counter, so a mission that mixed refunds and broadcasts would be adding dollars to
# recipients — see the note in axiom/domains/__init__.py. One mission, one risk axis.
#
# uuid5, not a pretty 2222-2222 literal, and that is scar tissue. The first version used
# '22222222-...' and its reset() quietly deleted the 2,500-row durable corpus that
# tests/test_recall_plan.py builds under exactly that id — a demo script silently
# demolishing a test fixture, with nothing failing to say so (the suite rebuilds it, and
# pays 2,500 embeddings for the privilege). Repeated-digit UUIDs look reserved and are
# not; a name-derived one cannot collide with another workload on a shared cluster.
BROADCAST_TENANT = uuid.uuid5(uuid.NAMESPACE_DNS, 'broadcast.axiom.demo')

# The authority ceiling, IN RECIPIENTS. A campaign that would reach more people than
# this stops and waits for a human, no matter how little it costs to send. That is the
# whole point: the same number expressed in dollars (about five cents of SES) would clear
# any money-shaped policy in the system without anyone being asked.
UNATTENDED_CEILING = 2_000
BLAST_RADIUS_BUDGET = 30_000

# (campaign_ref, description, kind, segment, audience, suppressed)
CAMPAIGNS: list[tuple[str, str, str, str, int, int]] = [
    ('CMP-2001', 'spring launch announcement to the active-buyer segment',
     'promotional_blast', 'active_buyers', 4800, 260),
    ('CMP-2002', 'order confirmation digest resend for yesterday checkout errors',
     'transactional_notice', 'checkout_errors_2026_08_11', 1140, 0),
    ('CMP-2003', 'win-back offer for customers dormant since January',
     'winback', 'dormant_180d', 3600, 410),
    ('CMP-2004', 'service incident status update for the affected region',
     'service_incident', 'eu_west_customers', 920, 0),
    ('CMP-2005', 'newsletter for the opted-in subscriber list',
     'promotional_blast', 'newsletter_optin', 2400, 120),
    ('CMP-2006', 'promo blast to a purchased list with no consent on record',
     'consent_gap', 'acquired_list_q3', 15000, 0),
    ('CMP-2007', 'price increase notice with contract change language',
     'regulated_copy', 'enterprise_accounts', 310, 0),
    ('CMP-2008', 'shipping update for delayed orders in the north-east',
     'transactional_notice', 'delayed_ne', 1750, 30),
    ('CMP-2009', 'flash sale announcement for the weekend',
     'promotional_blast', 'sale_optin', 2600, 200),
    ('CMP-2010', 'dormant subscriber reactivation campaign',
     'winback', 'dormant_365d', 1450, 90),
    ('CMP-2011', 'security advisory for all customers',
     'service_incident', 'all_customers', 3300, 0),
    ('CMP-2012', 'newsletter announcement for August',
     'promotional_blast', 'newsletter_optin', 880, 0),
]

# Prior recoveries this tenant has "lived through". Not decoration: a recovery path that
# recalls nothing is indistinguishable from one with no memory at all, and the fused
# transaction in tasks.recover() has to find something for the demo to prove its thesis.
# Two are adverse, which is what makes the ESCALATE branch reachable — and both are about
# regulated_copy / consent_gap, so they stay far in vector space from an ordinary
# promotional recovery rather than talking the system out of every re-send.
PRIOR_RECOVERIES: list[tuple[str, Outcome]] = [
    ('agent died mid-broadcast on a promotional_blast task; re-dispatched under the same '
     'idempotency key; the relay replayed the original send and nobody received a second '
     'copy', Outcome.RESOLVED),
    ('agent died mid-broadcast on a transactional_notice task; the re-send returned the '
     'original message reference and the delivery log showed exactly one copy per '
     'recipient', Outcome.RESOLVED),
    ('agent died after dispatch on a winback task; the receipt was still PREPARED; the '
     're-send confirmed the campaign had already left', Outcome.RESOLVED),
    ('worker crashed on a regulated_copy send and a second agent re-planned from the '
     'transcript instead of the receipt; the entire enterprise segment received the '
     'notice twice', Outcome.DUPLICATE_EFFECT),
    ('recovery on a consent_gap campaign could not determine relay state and required a '
     'human to reconcile the delivery log by hand', Outcome.HUMAN_REQUIRED),
]

PRIOR_SEMANTIC: list[tuple[str, str]] = [
    ('promotional_blast', 'promotional blasts go only to segments with recorded consent '
                          'and never twice in the same week'),
    ('transactional_notice', 'transactional notices are safe to re-send under the same '
                             'key; the relay dedupes and recipients see one copy'),
    ('consent_gap', 'a purchased list with no consent on record is never messaged; the '
                    'campaign is suppressed and the list is quarantined'),
    ('regulated_copy', 'copy containing pricing or contract changes is held for legal '
                       'review before anything leaves'),
    ('service_incident', 'incident notices go immediately to the affected region and are '
                         'exempt from marketing quiet hours'),
]


# ------------------------------------------------------------------------ seeding

def reset() -> None:
    """Wipe this tenant's rows and the relay's ledger.

    The relay is wiped for the same reason scripts/chaos_demo.py wipes the payment
    provider: the idempotency key is derived from the TASK id, so a re-seeded run mints
    new keys and is, correctly, a genuinely new set of actions. Auditing it against the
    previous run's deliveries would be comparing two different intents.
    """
    def _wipe(cur):
        for table in ('axiom_event', 'axiom_approval', 'axiom_action_attempt',
                      'axiom_memory', 'axiom_task', 'axiom_mission', 'axiom_policy'):
            cur.execute(f'DELETE FROM {table} WHERE tenant_id = %s',
                        (str(BROADCAST_TENANT),))
    db.tx(_wipe)
    relay.reset()


def seed() -> dict:
    # Embeddings first: db.tx() re-runs its callable on a 40001, and embedding inside
    # would re-hit Bedrock on every retry.
    prior_vecs = [(c, o, embeddings.embed_list(c)) for c, o in PRIOR_RECOVERIES]
    sem_vecs = [(k, c, embeddings.embed_list(c)) for k, c in PRIOR_SEMANTIC]

    def _apply(cur):
        cur.execute("""
            INSERT INTO axiom_tenant (id, slug, display_name)
            VALUES (%s, 'northwind', 'Northwind Lifecycle Marketing')
            ON CONFLICT (id) DO NOTHING
        """, (str(BROADCAST_TENANT),))

        policy.publish(
            cur, tenant_id=BROADCAST_TENANT, policy_id='broadcast_authority', version=1,
            body={'description': 'Autonomous outbound messaging authority',
                  'risk_axis': 'recipients',
                  'max_auto_action_recipients': UNATTENDED_CEILING,
                  'hold_kinds': ['regulated_copy', 'unclassified'],
                  'rationale': 'A send that reaches more than 2,000 people is a '
                               'reputational decision, not an operational one, and gets '
                               'a human. Dollars are not the axis: the same send costs '
                               'about two cents.'},
            # THE HONEST CLAUSE. db/004_risk.sql made authority a list of grants over
            # (unit, magnitude, reversibility), so this policy can finally say what it
            # means: up to 2,000 people, even though the act can never be undone.
            risk_grants=[Grant(COMMS_RECIPIENTS, UNATTENDED_CEILING,
                               Reversibility.IRREVERSIBLE)],
            # AND the same ceiling in the money column, because tasks.prepare() still
            # passes an int and reaches the decision through the money bridge. Drop this
            # line today and every campaign parks on a human — the bridge would read
            # `money.usd_cents = 4600` against a policy granting zero cents. It comes out
            # the moment prepare() passes a Risk; until then, both clauses say 2,000 and
            # the tests assert they agree.
            max_auto_action_cents=UNATTENDED_CEILING, requires_approval=False,
            created_by='human:lifecycle@northwind.example', activate=True,
            signature='demo-signature', signed_by='human:cmo@northwind.example')

        mission_id = tasks.create_mission(
            cur, tenant_id=BROADCAST_TENANT, title="Send today's outbound campaigns",
            goal=f'Deliver {len(CAMPAIGNS)} campaigns without messaging anyone twice',
            # budget_cents holding a RECIPIENT budget: the hard cap on how many human
            # beings this mission may touch in total, whatever the agent decides.
            budget_cents=BLAST_RADIUS_BUDGET,
            created_by='human:lifecycle@northwind.example')

        for content, outcome, vec in prior_vecs:
            memory.write(cur, tenant_id=BROADCAST_TENANT,
                         memory_class=MemoryClass.EPISODIC,
                         context_key=ctx_state(TaskState.ACTION_PREPARED),
                         content=content, embedding=vec, outcome=outcome,
                         source='system:execution', trust_level=Trust.FIRST_PARTY,
                         actor='system:seed')

        for kind, content, vec in sem_vecs:
            memory.write(cur, tenant_id=BROADCAST_TENANT,
                         memory_class=MemoryClass.SEMANTIC,
                         context_key=ctx_exception(kind), content=content, embedding=vec,
                         outcome=Outcome.RESOLVED, source='human:operator',
                         trust_level=Trust.VERIFIED, actor='system:seed')

        created = 0
        for ref, desc, kind, segment, audience, suppressed in CAMPAIGNS:
            tid = tasks.enqueue(
                cur, tenant_id=BROADCAST_TENANT, mission_id=mission_id,
                task_type='broadcast', dedupe_key=f'campaign:{ref}:broadcast',
                payload={'campaign_ref': ref, 'description': desc,
                         'campaign_kind': kind, 'segment': segment,
                         'recipient_count': audience, 'suppressed_count': suppressed},
                actor='system:seed')
            created += 1 if tid else 0

        return {'tenant_id': str(BROADCAST_TENANT), 'mission_id': str(mission_id),
                'tasks': created, 'memories': len(prior_vecs) + len(sem_vecs)}

    return db.tx(_apply)


# -------------------------------------------------------------------- the harness

def _env(chaos_pre: float, chaos_post: float) -> dict:
    e = dict(os.environ)
    e['AXIOM_CHAOS_PRE'] = str(chaos_pre)
    e['AXIOM_CHAOS_POST'] = str(chaos_post)
    e['PYTHONUNBUFFERED'] = '1'
    return e


def spawn(i: int, env: dict, quiet: bool, deadline: float) -> subprocess.Popen:
    # --deadline bounds an ORPHAN. If this harness dies unexpectedly its finally block
    # never runs, and the workers it spawned keep claiming forever against a shared
    # cluster — which happened during this build, tripped the invariant suite's
    # exclusive-queue guard, and made one run's audit read another run's rows. The
    # deadline is checked only between tasks, so it can never abandon a live dispatch.
    return subprocess.Popen(
        [sys.executable, '-m', 'axiom.domains.runtime',
         '--domain', 'broadcast', '--ref', f'chaos-broadcast-{i}',
         '--deadline', str(deadline)],
        cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL if quiet else None,
        stderr=subprocess.DEVNULL if quiet else None,
    )


def auto_approve(note: str = 'auto-approved by the demo operator') -> int:
    """Stand in for the human on the approvals queue.

    Campaigns above the recipient ceiling park on a human BY DESIGN, so an unattended
    demo would stall forever on exactly the tasks that prove the authority model works on
    a non-money axis. This answers them through the same decide_approval() the API calls,
    burning the same single-use token, rather than reaching into the tables.
    """
    def _decide(cur):
        pending = tasks.pending_approvals(cur, tenant_id=BROADCAST_TENANT)
        for a in pending:
            tasks.decide_approval(cur, tenant_id=BROADCAST_TENANT, approval_id=a['id'],
                                  approved=True, decided_by='lifecycle@northwind.example',
                                  note=note)
        return len(pending)
    return db.tx(_decide)


# Every read below is scoped to THIS RUN'S MISSION, not to the tenant. Scoping to the
# tenant was wrong in a way that only shows up on a shared cluster: a second process
# working in the same tenant made "12/12 campaigns" read as "14/14" and reported another
# mission's budget. A demo that quietly counts someone else's rows is a demo whose
# headline number cannot be trusted, which is the one thing this project cannot afford.

def outstanding(mission_id: str) -> tuple[int, int, dict]:
    def _q(cur):
        cur.execute("""
            SELECT state, count(*) AS n FROM axiom_task
            WHERE tenant_id = %s AND mission_id = %s GROUP BY state
        """, (str(BROADCAST_TENANT), mission_id))
        return {r['state']: r['n'] for r in cur.fetchall()}
    by_state = db.tx(_q, readonly=True)
    total = sum(by_state.values())
    done = sum(n for s, n in by_state.items() if s in {str(t) for t in TERMINAL_STATES})
    return done, total, by_state


def campaign_refs(mission_id: str) -> list[str]:
    """This run's campaigns. The relay ledger is append-only and shared, so the audit is
    scoped the same way scripts/chaos_demo.py scopes the refund audit to its own orders."""
    def _q(cur):
        cur.execute("""
            SELECT payload->>'campaign_ref' AS ref FROM axiom_task
            WHERE tenant_id = %s AND mission_id = %s
              AND payload->>'campaign_ref' IS NOT NULL
        """, (str(BROADCAST_TENANT), mission_id))
        return [r['ref'] for r in cur.fetchall()]
    return db.tx(_q, readonly=True)


def latest_mission() -> str | None:
    """For --no-seed: the most recent broadcast mission this script created."""
    def _q(cur):
        cur.execute("""
            SELECT id::STRING AS id FROM axiom_mission
            WHERE tenant_id = %s ORDER BY created_at DESC LIMIT 1
        """, (str(BROADCAST_TENANT),))
        row = cur.fetchone()
        return row['id'] if row else None
    return db.tx(_q, readonly=True)


def foreign_releases_since(t0_sql: str) -> int:
    """How many times a broadcast worker claimed someone else's task and handed it back.

    Not a failure — it is crash window W1, the one window in which no effect can exist —
    but it is wasted work, and printing it keeps the missing task_type predicate on
    tasks.claim() visible instead of buried in a handoff document.
    """
    def _q(cur):
        cur.execute("""
            SELECT count(*) AS n FROM axiom_event
            WHERE event_type = 'task.released' AND occurred_at >= %s
        """, (t0_sql,))
        return int(cur.fetchone()['n'])
    return db.tx(_q, readonly=True)


def executed_by_other_domain(mission_id: str) -> int:
    """Campaigns that some OTHER workload's worker claimed and executed.

    Not hypothetical, and not a race that needed engineering to provoke: on the shared
    development cluster a plain `axiom.worker` — the refund worker — claimed two of this
    mission's campaigns, ran llm.triage() on a marketing brief, got 'escalate' because no
    refund rule matched, and dead-lettered them. Nothing errored. tasks.claim() has no
    task_type predicate and worker.execute() never checks the task_type it was handed, so
    a heterogeneous cluster silently lets one workload consume another's work.

    It escalated, which is harmless. A payload that happened to carry an amount_cents and
    a description matching a refund rule would have minted a PAYMENTS receipt for a
    marketing campaign instead. That is the finding, and printing it here is more useful
    than burying it in a handoff.
    """
    def _q(cur):
        cur.execute("""
            WITH mine AS (
                SELECT 'agent:' || id::STRING AS actor FROM axiom_agent
                WHERE worker_ref LIKE 'chaos-broadcast-%%'
            )
            SELECT count(DISTINCT task_id) AS n FROM axiom_event
            WHERE tenant_id = %s AND mission_id = %s
              AND actor LIKE 'agent:%%' AND actor NOT IN (SELECT actor FROM mine)
        """, (str(BROADCAST_TENANT), mission_id))
        return int(cur.fetchone()['n'])
    return db.tx(_q, readonly=True)


def cluster_now():
    """The cluster's clock, not this laptop's — the release events are timestamped by
    the database and comparing them to a local time would be comparing two clocks."""
    def _q(cur):
        cur.execute('SELECT now() AS t')
        return cur.fetchone()['t']
    return db.tx(_q, readonly=True)


def spent(mission_id: str) -> tuple[int, int]:
    def _q(cur):
        cur.execute("""SELECT spent_cents, budget_cents FROM axiom_mission
                       WHERE tenant_id = %s AND id = %s""",
                    (str(BROADCAST_TENANT), mission_id))
        r = cur.fetchone()
        return (int(r['spent_cents']), int(r['budget_cents'])) if r else (0, 0)
    return db.tx(_q, readonly=True)


def main() -> int:
    ap = argparse.ArgumentParser(description='AXIOM second-domain chaos demo')
    ap.add_argument('--workers', type=int, default=3)
    ap.add_argument('--kill-every', type=float, default=1.8,
                    help='seconds between SIGKILLs; 0 disables killing')
    ap.add_argument('--chaos-pre', type=float, default=0.15,
                    help='P(die after PREPARE, before the send leaves) — window W2')
    ap.add_argument('--chaos-post', type=float, default=0.35,
                    help='P(die after the campaign went out, before settle) — window W4')
    ap.add_argument('--timeout', type=float, default=180.0)
    ap.add_argument('--quiet', action='store_true', help='hide worker logs')
    ap.add_argument('--no-seed', action='store_true')
    ap.add_argument('--auto-approve', action='store_true', default=True)
    ap.add_argument('--no-auto-approve', dest='auto_approve', action='store_false')
    args = ap.parse_args()

    if not args.no_seed:
        reset()
        out = seed()
        mission_id = out['mission_id']
        print(f'seeded mission {mission_id}: {out["tasks"]} campaigns, '
              f'{out["memories"]} prior memories')
        print(f'policy broadcast_authority v1: the agent may message up to '
              f'{UNATTENDED_CEILING:,} people unattended; the mission may touch '
              f'{BLAST_RADIUS_BUDGET:,} in total\n')
    else:
        mission_id = latest_mission()
        if mission_id is None:
            print('nothing seeded: run without --no-seed first')
            return 1

    t0_sql = cluster_now()

    env = _env(args.chaos_pre, args.chaos_post)
    worker_deadline = args.timeout + 30.0
    procs = {i: spawn(i, env, args.quiet, worker_deadline)
             for i in range(args.workers)}
    print(f'spawned {args.workers} broadcast workers; killing one every '
          f'{args.kill_every}s (SIGKILL, no cleanup)\n')

    kills = restarts = approved = 0
    t0 = time.time()
    next_kill = t0 + args.kill_every

    try:
        while time.time() - t0 < args.timeout:
            done, total, by_state = outstanding(mission_id)
            if total and done == total:
                break

            for i, p in list(procs.items()):
                if p.poll() is not None:
                    procs[i] = spawn(i, env, args.quiet, worker_deadline)
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
                          f'[{done}/{total} campaigns terminal]')
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
    done, total, by_state = outstanding(mission_id)
    refs = campaign_refs(mission_id)
    report = broadcast.DOMAIN.audit(refs)
    used, budget = spent(mission_id)
    released = foreign_releases_since(t0_sql)
    stolen = executed_by_other_domain(mission_id)

    print('\n' + '=' * 72)
    print('AXIOM domain 2 — bulk outbound messaging, under the same crashes')
    print('=' * 72)
    print(f'  wall clock                {time.time() - t0:6.1f}s')
    print(f'  workers SIGKILLed         {kills}')
    print(f'  worker restarts           {restarts}')
    print(f'  approvals answered        {approved}   (over the '
          f'{UNATTENDED_CEILING:,}-recipient unattended ceiling)')
    print(f'  campaigns terminal        {done}/{total}   {by_state}')
    print(f'  blast radius used         {used:,} of {budget:,} recipients authorized')
    print(f'  foreign tasks handed back {released}   '
          f'(claim() has no task_type predicate)')
    print('  ' + '-' * 68)
    print(f'  campaigns sent            {report.effects}')
    print(f'  people messaged           {report.risk_units:,}')
    print(f'  idempotent replays        {report.replays}   '
          f'(re-sends the relay absorbed)')
    print(f'  relay verdicts            {report.verdicts}')
    print(f'  RECIPIENTS MESSAGED TWICE {len(report.duplicates)}')
    print('=' * 72)

    if stolen:
        print(f'\nNOTE: {stolen} campaign(s) were claimed and executed by a worker from a '
              f'DIFFERENT domain.\n'
              '  tasks.claim() has no task_type predicate and axiom/worker.py never '
              'checks the type it\n  was handed, so a refund worker on the same cluster '
              'runs refund triage on a marketing\n  brief, gets "escalate", and '
              'dead-letters it. Harmless here; a payload carrying an\n  amount_cents '
              'would have minted a PAYMENTS receipt instead. This is the one core change\n'
              '  a second workload genuinely needs — see the handoff.')

    ok = True
    if not refs:
        # The scoping query found nothing, so every number above describes an empty set.
        # Say so instead of printing a confident zero: "0 recipients messaged twice" is
        # trivially true of a run that audited nothing, and that is exactly the shape of
        # a passing demo that proves nothing.
        ok = False
        print('\nFAIL: this mission has no campaigns to audit — the run measured nothing. '
              'Re-seed and re-run.')
    if report.duplicates:
        ok = False
        print(f'\nFAIL: {report.duplicate_label}:')
        for d in report.duplicates[:10]:
            print(f'  {d["campaign_ref"]} -> {d["recipient"]}: {d["deliveries"]} copies')
    if done != total:
        ok = False
        print(f'\nFAIL: {total - done} campaigns never reached a terminal state: {by_state}')
    if kills and report.replays == 0:
        # Not a correctness failure, but the run proved nothing: no crash landed in the
        # window where the messages were already in inboxes. Say so rather than claiming
        # a pass.
        ok = False
        print('\nINCONCLUSIVE: zero idempotent replays — no crash landed after a campaign '
              'had already left the relay. Raise --chaos-post or --kill-every.')

    if ok:
        print(f'\nPASS: {kills} kills, {report.replays} re-sends absorbed by the relay, '
              f'{report.risk_units:,} people messaged, 0 messaged twice.')
        print('The engine did not change. tasks.py, memory.py, policy.py and db.py were '
              'not touched;\nthe idempotency key is generated from '
              '(tenant, task, step, seq) and knows nothing about money.')
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
