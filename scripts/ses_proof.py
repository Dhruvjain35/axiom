#!/usr/bin/env python3
"""AXIOM :: the same crash, against a provider that offers no protection at all.

    AXIOM_SES=1 AWS_PROFILE=axiom python scripts/ses_proof.py

WHY THIS IS THE STRONGER PROOF
------------------------------
`scripts/stripe_proof.py` runs crash window W4 against real Stripe, and the honest reading
of that result is a SPLIT of credit: Stripe refuses to honour a repeated idempotency key,
AXIOM makes the key survive the crash. A skeptic can say the provider did the hard part.

Amazon SES gives you nothing to split. There is no idempotency key on SendEmail, no replay
header, no dedupe window, no "did I already send this?" endpoint. Call it twice and two
emails arrive, each with its own MessageId, and neither can be recalled. So this run has no
external help in it at all:

    two dispatches under one key  ->  ONE MessageId
    because the second dispatch never reached SES, because AXIOM's store already
    held the key, because the key was committed before the first message left.

ASK SES WHAT IT DID, RATHER THAN ASKING AXIOM
---------------------------------------------
Two pieces of testimony, and the obvious one is worthless here. `SentLast24Hours` did not
move at all across a run that sent two messages — measured, 1.0 before and 1.0 after —
because mailbox-simulator mail deliberately does not count against the sending quota. That
exclusion is exactly what makes the simulator safe to use and exactly what disqualifies the
quota counter as evidence about it. This script reports it as account state and rests
nothing on it.

What does count is the MessageId, which SES mints at acceptance and AXIOM cannot fabricate,
and CloudWatch's AWS/SES `Send` metric, which SES publishes and which DOES include simulator
traffic — measured at Sum 2.0 for the minute a two-recipient run executed, against two
dispatches. `--witness 300` polls for it; it publishes a few minutes late, so the verdict
rests on the synchronous evidence and this is the cross-check.

WHAT IT SENDS, AND TO WHOM
--------------------------
Amazon's mailbox simulator only — success@simulator.amazonses.com and labelled variants.
Those addresses need no verification, touch no real inbox, and do not affect the account's
bounce or complaint metrics. Nothing here will ever mail a third party: `axiom/ses.py`
refuses any other address, this account is in the SES sandbox (200/day, recipients must be
verified), and a demo that emails strangers is a demo that gets its account suspended
during judging. Two messages per run, by default.

WHY IT SETS AXIOM_OFFLINE
-------------------------
Offline swaps Bedrock for the deterministic local stand-ins, and on this account that is
not a test convenience: Bedrock's on-demand quota is structurally zero (L-26C560CE, not
adjustable), so an "online" run cannot embed a memory or triage a campaign at all. SES is
armed separately and deliberately — see axiom/ses.py — which is why AXIOM_SES=1 must be
typed by a human and is never defaulted here.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# BEFORE any axiom import: `settings` is a frozen dataclass built at import time, so this
# has to be in os.environ before axiom.config is first imported anywhere in the process.
os.environ.setdefault('AXIOM_OFFLINE', '1')

from axiom import db, embeddings, policy as policy_mod, proofs, ses, tasks   # noqa: E402
from axiom.domains import broadcast, relay                                   # noqa: E402
from axiom.models import AttemptState, Outcome, TaskState                    # noqa: E402
from axiom.provider import ProviderCrash                                     # noqa: E402
from axiom.risk import COMMS_RECIPIENTS, Grant, Reversibility                # noqa: E402

# Two recipients rather than one, so the per-recipient audit query has more than a single
# address to group by, and both of them are the simulator. Both are also distinct, because
# a campaign that mails the same address twice ON PURPOSE would make the headline query
# meaningless.
RECIPIENTS = ['success@simulator.amazonses.com',
              'success+axiom@simulator.amazonses.com']

CEILING = 2_000          # recipients the agent may reach unattended
BUDGET = 30_000          # recipients this mission may touch in total

DESC = 'service incident status update for the affected region'


def _hr(title: str) -> None:
    print('\n' + '=' * 74)
    print(f'  {title}')
    print('=' * 74)


def main() -> int:
    ap = argparse.ArgumentParser(description='AXIOM vs Amazon SES, which has no '
                                             'idempotency to lean on')
    ap.add_argument('--recipients', type=int, default=len(RECIPIENTS),
                    help=f'how many simulator addresses to mail (1..{len(RECIPIENTS)})')
    ap.add_argument('--keep', action='store_true',
                    help="leave the run's tenant in place instead of deleting it")
    ap.add_argument('--json', action='store_true', help='machine-readable result only')
    ap.add_argument('--witness', type=int, default=0, metavar='SECONDS',
                    help='poll CloudWatch AWS/SES Send for this long as an independent '
                         'cross-check; it publishes a few minutes late (try 300)')
    args = ap.parse_args()

    quiet = args.json
    def say(*a):
        if not quiet:
            print(*a)

    # ------------------------------------------------------------------ preflight
    if not ses.enabled():
        sys.exit('AXIOM_SES is not set. This script sends REAL email; arm it explicitly:\n'
                 '    AXIOM_SES=1 AWS_PROFILE=axiom python scripts/ses_proof.py')
    ok, why = ses.available()
    if not ok:
        sys.exit(f'SES is not reachable from this process: {why}')

    recipients = RECIPIENTS[:max(1, min(args.recipients, len(RECIPIENTS)))]
    for a in recipients:
        ses.guard(a)                       # refuse before anything is built, not after

    acct = ses.account()
    before = float(acct['sent_last_24_hours'] or 0)
    say(f'\n  Amazon SES · {acct["region"]} · sender {ses.sender()}')
    say(f'  sandbox={acct["sandbox"]}  quota={acct["max_24_hour_send"]:.0f}/day  '
        f'rate={acct["max_send_rate"]}/s  sent in the last 24h: {before:.0f}')
    say(f'  recipients: {", ".join(recipients)}  (mailbox simulator; no real inbox)')

    t0 = time.monotonic()
    tenant_id, slug = proofs._new_tenant('ses')
    run = slug.rsplit('-', 1)[-1]
    ref = f'SES-PRF-{run.upper()}'
    d = broadcast.DOMAIN
    agents: list[uuid.UUID] = []
    steps: list[dict] = []
    crashed = False
    error: str | None = None
    first_ids: list[str] = []
    replay: dict = {}
    dispatches = 0
    idem_key: str | None = None
    send_minute: datetime | None = None

    def step(n: int, label: str, detail: str) -> None:
        steps.append({'n': n, 'label': label, 'detail': detail})
        say(f'\n  {n}  {label}\n     {detail}')

    try:
        relay.ensure_schema()

        # -------------------------------------------------- 1. a world with a policy
        def _world(cur):
            cur.execute("""INSERT INTO axiom_tenant (id, slug, display_name)
                           VALUES (%s, %s, 'AXIOM SES proof')""", (str(tenant_id), slug))
            policy_mod.publish(
                cur, tenant_id=tenant_id, policy_id=d.policy_id, version=1,
                body={'description': 'Autonomous outbound messaging authority',
                      'risk_axis': 'recipients',
                      'max_auto_action_recipients': CEILING,
                      'rationale': 'A send that reaches more than 2,000 people is a '
                                   'reputational decision, not an operational one.'},
                risk_grants=[Grant(COMMS_RECIPIENTS, CEILING, Reversibility.IRREVERSIBLE)],
                max_auto_action_cents=CEILING, requires_approval=False,
                created_by='system:proof', activate=True)
            return tasks.create_mission(
                cur, tenant_id=tenant_id, title='Send the incident notice',
                goal='mail every recipient exactly once, across a crash',
                budget_cents=BUDGET, created_by='system:proof')

        mission_id = db.tx(_world)
        agent_a = db.tx(lambda cur: tasks.register_agent(cur, worker_ref=f'ses-a-{run}'))
        agent_b = db.tx(lambda cur: tasks.register_agent(cur, worker_ref=f'ses-b-{run}'))
        agents += [agent_a, agent_b]

        payload = {'campaign_ref': ref, 'description': DESC,
                   'campaign_kind': 'service_incident', 'segment': 'affected_region',
                   'recipient_count': len(recipients), 'suppressed_count': 0,
                   # The real-gateway shape: an explicit address list, carried in the
                   # request body and therefore covered by request_fingerprint.
                   'recipients': recipients}
        claimed = proofs._enqueue_and_claim(
            tenant_id=tenant_id, mission_id=mission_id, agent_id=agent_a,
            task_type=d.task_type, dedupe_key=f'campaign:{ref}:broadcast', payload=payload)

        intent = d.triage(payload)
        situation = d.situation(payload, intent)
        vec = embeddings.embed_list(situation)
        if not intent.acts:
            raise RuntimeError(f'triage refused to send: {intent.action} ({intent.reason})')
        step(1, 'triage PROPOSED a send; it cannot authorize one',
             f'{intent.action} · {intent.kind} · {intent.risk_units} recipients')

        # ---------------------------------------------- 2. the receipt, before the act
        request_body = d.request_body(payload, intent)
        prepared = db.tx(lambda cur: tasks.prepare(
            cur, task=claimed, agent_id=agent_a, step_name=d.step_name,
            provider_name=d.provider_name, operation=d.operation,
            request_body=request_body,
            risk=d.risk.descriptor(intent.risk_units, intent.reason),
            amount_cents=intent.risk_units, currency=d.risk.code, policy_id=d.policy_id))
        if prepared.parked:
            raise RuntimeError('parked on approval; the ceiling is set wrong for this run')
        receipt = prepared.receipt
        idem_key = receipt.idempotency_key
        step(2, 'receipt committed BEFORE any email left',
             f'{receipt.idempotency_key}\n     generated in the database from immutable '
             f'columns — not by the process that is about to die')

        # --------------------------------- 3. SES accepts the mail, then worker A dies
        db.tx(lambda cur: tasks.mark_dispatched(cur, receipt=receipt))
        # The instant the first message leaves, kept to the minute. CloudWatch's Send
        # metric is ACCOUNT-WIDE, so a window that reaches back further would count the
        # previous proof run's messages as this one's — it did, exactly once, and reported
        # 3 for a two-message run. The window now starts at the minute this dispatch
        # happened, which excludes anything earlier and merges only with another send in
        # the same 60 seconds.
        send_minute = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        try:
            dispatches += 1
            d.dispatch(idempotency_key=receipt.idempotency_key,
                       request_body=receipt.request_body,
                       risk_units=intent.risk_units, chaos_post=1.0)
        except ProviderCrash:
            crashed = True
        step(3, 'the email is REAL and AXIOM never recorded it',
             'crash window W4: SES accepted the message, the response was thrown away '
             'with the process,\n     and AXIOM\'s own record of the MessageId does not '
             'exist' if crashed else 'NO CRASH FIRED — this run proves nothing')

        # ------------------------------------------------- 4. worker B takes it over
        recovered = proofs._take_over(task_id=claimed.id, agent_id=agent_b)
        plan = db.tx(lambda cur: tasks.recover(
            cur, task=recovered, agent_id=agent_b,
            situation_embedding=embeddings.embed_list(d.recovery_situation(payload)),
            step_name=d.step_name))
        step(4, 'worker B recovered from the RECEIPT, not from a transcript',
             f'fence e{claimed.lease_epoch} -> e{recovered.lease_epoch} · {plan.action} · '
             f'{plan.rationale[:120]}')
        if plan.action != 'RESEND':
            raise RuntimeError(f'recovery chose {plan.action}; expected RESEND')

        # ------------------------------------------- 5. re-dispatch under the SAME key
        dispatches += 1
        effect = d.dispatch(idempotency_key=plan.receipt.idempotency_key,
                            request_body=plan.receipt.request_body,
                            risk_units=intent.risk_units)
        replay = dict(effect.body)
        first_ids = list(replay.get('message_ids') or [])
        step(5, 're-dispatched under the SAME key — and SES was never called',
             f'{effect.ref} · replayed={effect.replayed} · SES accepted '
             f'{replay.get("ses_accepted")} message(s) on this call\n     '
             f'MessageIds recovered from the relay\'s store: '
             + ('; '.join(first_ids) if first_ids else 'none'))

        content = d.settled_memory(situation=situation,
                                   idempotency_key=plan.receipt.idempotency_key,
                                   risk_units=intent.risk_units, effect=effect,
                                   first_try=False)
        db.tx(lambda cur: tasks.settle(
            cur, task=recovered, agent_id=agent_b, receipt=plan.receipt,
            outcome_state=AttemptState.SUCCEEDED, task_state=TaskState.SUCCEEDED,
            response_body=effect.body, provider_ref=effect.ref,
            http_status=effect.status, memory_content=content,
            memory_embedding=embeddings.embed_list(content),
            memory_outcome=Outcome.RESOLVED,
            result={'provider_ref': effect.ref, 'replayed': effect.replayed,
                    'message_ids': first_ids}))

    except Exception as e:                       # noqa: BLE001
        error = f'{type(e).__name__}: {e}'[:300]
        if not quiet:
            import traceback
            traceback.print_exc()
    finally:
        if not args.keep:
            proofs._wipe_tenant(tenant_id, agents)

    # ------------------------------------------------------- ask SES, not ourselves
    after = ses.sent_last_24h()

    # THE INDEPENDENT WITNESS, and the first version of this script had the wrong one.
    # SentLast24Hours does not move for mailbox-simulator traffic — measured, 1.0 before
    # and 1.0 after a run that sent two messages — because simulator mail deliberately does
    # not count against the quota, which is exactly what makes the simulator safe. That is
    # reported below as account state and proves nothing about a send.
    #
    # AWS/SES `Send` in CloudWatch DOES count it. It lags by a few minutes, so it is polled
    # only when a human asks for it with --witness; the run's verdict rests on evidence
    # that exists synchronously (SES-minted MessageIds and the relay's per-recipient books)
    # and this is the cross-check that cannot be forged from inside AXIOM.
    witness: float | None = None
    if args.witness and send_minute is not None:
        deadline = time.monotonic() + args.witness
        say(f'\n  polling CloudWatch AWS/SES Send from {send_minute:%H:%M}Z for up to '
            f'{args.witness}s (it publishes a few minutes late) ...')
        while True:
            witness = ses.cloudwatch_sends(start=send_minute,
                                           end=datetime.now(timezone.utc))
            if witness is not None and witness >= len(recipients):
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(15)

    ledger = []
    with relay.pool().connection() as conn, conn.cursor() as cur:
        cur.execute("""SELECT recipient, message_id, COALESCE(channel,'simulated') AS channel
                       FROM relay_delivery WHERE campaign_ref = %s ORDER BY delivered_at""",
                    (ref,))
        ledger = [dict(r) for r in cur.fetchall()]
    dupes = relay.duplicate_recipients([ref])
    stats = relay.stats([ref])

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    expected = len(recipients)
    ok = bool(crashed and not error and not dupes
              and len(ledger) == expected
              and len({r['message_id'] for r in ledger}) == expected
              and replay.get('ses_accepted') == 0
              # Only when it was actually asked for AND published. None is "not yet", not
              # zero, and a proof that treated a pending datapoint as a failure would be
              # reporting CloudWatch's publish latency as a correctness result.
              and (witness is None or witness == float(expected)))

    if not quiet:
        _hr('WHAT AMAZON SAYS HAPPENED — not AXIOM\'s books')
        print(f'    dispatches AXIOM made         {dispatches}')
        print(f'    MessageIds SES minted         {len({r["message_id"] for r in ledger})}')
        print(f'    SES sends on the 2nd dispatch {replay.get("ses_accepted")}')
        if witness is not None:
            print(f'    CloudWatch AWS/SES Send       {witness:.0f}   '
                  f'(SES\'s own metric, account-wide, this region)')
        elif args.witness:
            print('    CloudWatch AWS/SES Send       not published yet '
                  '(it lags a few minutes; "not yet" is not "zero")')
        print(f'    SentLast24Hours {before:.0f} -> {after:.0f}      '
              f'unchanged BY DESIGN: mailbox-simulator mail does not')
        print('                                  count against the quota, which is what '
              'makes it safe')

        _hr("THE RELAY'S OWN BOOKS — one row per person who received something")
        for r in ledger:
            print(f'    {r["channel"]:<10} {r["recipient"]:<44} {r["message_id"]}')
        print('-' * 74)
        print(f'    deliveries                    {stats["deliveries"]}')
        print(f'    replayed requests             {stats["replays"]}')
        print(f'    recipients messaged twice     {len(dupes)}')
        print(f'    verdicts                      {stats["verdicts"]}')
        print(f'    latency of the real sends     '
              f'{replay.get("ses_latency_ms") or "—"} ms')
        print(f'    cost of this run              ${ses.cost_usd(expected):.6f}   '
              f'({expected} messages at $0.10 per 1,000)')
        print('=' * 74)

        if ok:
            print('\n  PASS — AXIOM dispatched twice and exactly '
                  f'{expected} email(s) exist.\n  Amazon SES has no idempotency key, no '
                  'replay flag and no dedupe window: the second\n  dispatch was stopped by '
                  'a key AXIOM committed to durable storage before the first\n  message '
                  'left. There is no shared credit in this result.\n')
        else:
            print(f'\n  INCONCLUSIVE — crashed={crashed} deliveries={len(ledger)} '
                  f'message_ids={len({r["message_id"] for r in ledger})} '
                  f'witness={witness} duplicates={len(dupes)}'
                  + (f'\n  {error}' if error else '') + '\n')

    out = {
        'verdict': 'PASS' if ok else 'INCONCLUSIVE',
        'campaign_ref': ref,
        'recipients': recipients,
        'dispatches': dispatches,
        'crashed_at_w4': crashed,
        'idempotency_key': idem_key,
        'message_ids': first_ids,
        'ses_accepted_on_second_dispatch': replay.get('ses_accepted'),
        'ses_sent_last_24h_before': before,
        'ses_sent_last_24h_after': after,
        'ses_quota_counts_simulator_mail': False,
        'cloudwatch_ses_send': witness,
        'ses_latency_ms': replay.get('ses_latency_ms'),
        'ses_region': acct['region'],
        'sandbox': acct['sandbox'],
        'deliveries': len(ledger),
        'distinct_message_ids': len({r['message_id'] for r in ledger}),
        'recipients_messaged_twice': len(dupes),
        'replays': int(stats['replays']),
        'verdicts': stats['verdicts'],
        'cost_usd': ses.cost_usd(expected),
        'price_per_1000_usd': ses.PRICE_PER_1000_USD,
        'elapsed_ms': elapsed_ms,
        'steps': steps,
    }
    if error:
        out['error'] = error
    if args.json:
        print(json.dumps(out, indent=2, default=str))

    db.close_pool()
    relay.close_pool()
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
