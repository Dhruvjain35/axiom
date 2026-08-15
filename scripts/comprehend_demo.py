#!/usr/bin/env python3
"""AXIOM :: what Amazon Comprehend actually says about the real seeded exceptions.

    AWS_REGION=us-east-2 python scripts/comprehend_demo.py
    AWS_REGION=us-east-2 python scripts/comprehend_demo.py --json > /tmp/comprehend.json

Needs AWS credentials and nothing else — no database, no cluster, no seeding. It reads
the corpus straight out of `axiom/seed.py` (EXCEPTIONS, PRIOR_RECOVERIES, PRIOR_SEMANTIC)
because a demonstration over texts written to make the demonstration work is not a
demonstration.

WHAT IT PRINTS, AND WHY EACH COLUMN IS THERE
--------------------------------------------
For every text: the key phrases, typed entities and sentiment Comprehend returned, the
kind AXIOM's lexicon derives from that extraction, the kind the rule-based classifier in
`llm.py` derives from the raw string, and — for the ten EXCEPTIONS rows, which carry a
hand-written ground-truth kind in seed.py — whether either of them got it right.

Then the arithmetic: units consumed and what they cost. Every number below is counted from
the calls this process actually made — see the billing section, because the answer is not
the one this integration was started on.

The disagreements are the interesting part and they are printed rather than summarized
away. Some of them are Comprehend being wrong; at least one of them is the RULE TABLE
being wrong in the expensive direction, which is the reason this integration exists:
`llm._KIND_RULES` is ordered and first-match-wins, `late_delivery` sits above
`fraud_suspected`, and so a text carrying both signals proposes an unattended refund.
`--adversarial` runs exactly that text through the whole triage path, end to end.

THIS SCRIPT SPENDS MONEY, WHICH IS NOT WHAT WE EXPECTED
-------------------------------------------------------
Comprehend's 50,000 units/month free tier is a TWELVE-MONTH offer, and the deployment
account does not have a twelve-month free tier: `freetier get-account-plan-state` returns
`accountPlanType: "PAID"` with $0.00 remaining credits, and `get-free-tier-usage` returns
twelve rows of which every single one is `"Always Free"`. A three-request S3 month on the
same account was billed. So these calls are charged at the published $0.0001/unit.

One full run of this script is 21 texts x 3 requests x 3 units = 189 units = $0.0189.
That is printed before any call goes out and again afterwards, and `--limit` bounds it.
Nothing here is described as free, because it is not.

WHY NOT A CUSTOM CLASSIFIER
---------------------------
Comprehend has Custom Classification, and a model trained on labelled exception text
would beat both the lexicon and the rule table. It is not used here, and the reason is
money rather than taste: custom classification inference needs a real-time endpoint, and
an endpoint is billed per inference-unit-hour whether or not anything calls it — roughly
$0.0005/second, about $1.30/day, for as long as it exists, against an account with about
$3 available and a judging window that runs to September 15. The on-demand Detect* APIs
bill per request and cost nothing when idle, which is the difference that matters: the
worst case for this design is a run nobody meant to make, and the worst case for an
endpoint is a Tuesday.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from axiom import comprehend, llm                                     # noqa: E402
from axiom.seed import EXCEPTIONS, PRIOR_RECOVERIES, PRIOR_SEMANTIC   # noqa: E402

# The rule table orders late_delivery above fraud_suspected, so this triages as an
# unattended $300 refund. It is not a trick sentence: an order can be late and the card
# can also have been stolen.
ADVERSARIAL = ('delivery delayed nine days and an unauthorized charge appeared on the '
               'stolen card')
#: $150, deliberately UNDER the seed policy's $200 self-authorization ceiling. At $300 the
#: policy parks the act on a human anyway and the demonstration proves nothing; at $150 the
#: rule-based path really would refund a suspected-fraud exception unattended.
ADVERSARIAL_AMOUNT = 15_000

BAR = '=' * 78


def _corpus() -> list[tuple[str, str, str]]:
    """(source, text, ground_truth_kind). '-' where seed.py records no kind."""
    rows = [('EXCEPTIONS', d, k) for d, k, _ in EXCEPTIONS]
    rows += [('PRIOR_RECOVERIES', t, '-') for t, _ in PRIOR_RECOVERIES]
    rows += [('PRIOR_SEMANTIC', t, k) for k, t in PRIOR_SEMANTIC]
    return rows


def run(*, adversarial: bool, limit: int | None = None) -> dict:
    rows: list[dict] = []
    latencies: list[float] = []          # per classify(): what one triage pays
    request_latencies: list[float] = []  # per Detect* request: what the service costs

    corpus = _corpus()
    if limit is not None:
        corpus = corpus[:limit]
    # Appended AFTER --limit so a bounded run still shows it, and the end-to-end section
    # below reuses this row's Signals rather than paying for a second identical call —
    # every unit this process spends is counted exactly once, in one place.
    if adversarial:
        corpus.append(('ADVERSARIAL', ADVERSARIAL, 'fraud_suspected'))
    signals: dict[str, object] = {}

    # Say what it will cost BEFORE spending it. This account is not on a free tier for
    # Comprehend (see the module docstring), so an unannounced run is an unannounced bill.
    projected = sum(comprehend.units_for(t) for _, t, _ in corpus)
    print(f'{len(corpus)} texts, {len(corpus) * 3} Detect* requests, {projected} units, '
          f'${comprehend.usd_for(projected):.4f} at the published rate. '
          f'This account is billed for it.\n')

    for source, text, truth in corpus:
        started = time.perf_counter()
        s = comprehend.classify(text)
        wall = (time.perf_counter() - started) * 1000
        if not s.available:
            print(f'FAILED on {text[:48]!r}: {s.error}')
            return {'ok': False, 'error': s.error}
        latencies.append(wall)
        request_latencies.extend(s.request_ms)
        signals[text] = s

        rule = llm._offline_triage(text, 30_000)
        rows.append({
            'source': source, 'text': text, 'truth': truth,
            'key_phrases': list(s.key_phrases),
            'entities': [list(e) for e in s.entities],
            'sentiment': s.sentiment, 'sentiment_score': round(s.sentiment_score, 3),
            'ambiguous': s.ambiguous,
            'comprehend_kind': s.kind_hint, 'comprehend_kinds': list(s.kinds),
            'rule_kind': rule.exception_kind, 'rule_action': rule.action,
            'units': s.units, 'calls': s.calls,
            'latency_ms': round(wall, 1),
        })

    # -------------------------------------------------------------- per-text detail
    print(BAR)
    print('WHAT AMAZON COMPREHEND RETURNED, over the real seed corpus')
    print(BAR)
    for r in rows:
        print(f'\n[{r["source"]}] {r["text"][:96]}')
        print(f'  key phrases  {r["key_phrases"] or "(none)"}')
        print(f'  entities     {[f"{t}:{v}" for t, v in r["entities"]] or "(none)"}')
        flag = '  AMBIGUOUS' if r['ambiguous'] else ''
        print(f'  sentiment    {r["sentiment"]} @ {r["sentiment_score"]}{flag}')
        print(f'  kind: comprehend={r["comprehend_kind"] or "-":<17} '
              f'rules={r["rule_kind"]:<17} truth={r["truth"]}')

    # ------------------------------------------------------------------ agreement
    graded = [r for r in rows if r['truth'] != '-']
    both = [r for r in graded if r['comprehend_kind'] == r['rule_kind']]
    c_right = [r for r in graded if r['comprehend_kind'] == r['truth']]
    r_right = [r for r in graded if r['rule_kind'] == r['truth']]
    c_silent = [r for r in graded if r['comprehend_kind'] is None]
    disagree = [r for r in graded if r['comprehend_kind'] != r['rule_kind']]

    print(f'\n{BAR}')
    print('AGREEMENT with the existing rule-based classifier')
    print(BAR)
    print(f'  texts with a ground-truth kind in seed.py   {len(graded)}')
    print(f'  comprehend and the rules agree              {len(both)}/{len(graded)}')
    print(f'  comprehend matches ground truth             {len(c_right)}/{len(graded)}')
    print(f'  the rule table matches ground truth         {len(r_right)}/{len(graded)}')
    print(f'  comprehend produced no kind at all          {len(c_silent)}/{len(graded)}')
    if disagree:
        print('\n  DISAGREEMENTS — printed, not hidden:')
        for r in disagree:
            verdict = ('comprehend right' if r['comprehend_kind'] == r['truth'] else
                       'rules right' if r['rule_kind'] == r['truth'] else
                       'comprehend silent' if r['comprehend_kind'] is None else
                       'both wrong')
            print(f'    {r["text"][:60]:<62} '
                  f'{str(r["comprehend_kind"]):<17} vs {r["rule_kind"]:<17} '
                  f'truth={r["truth"]:<17} -> {verdict}')

    # ------------------------------------------------------------------ the money
    units = sum(r['units'] for r in rows)
    calls = sum(r['calls'] for r in rows)
    usd = comprehend.usd_for(units)
    print(f'\n{BAR}')
    print('WHAT IT COST — counted from the calls this process made')
    print(BAR)
    print(f'  texts                       {len(rows)}')
    print(f'  Detect* requests            {calls}   '
          f'(DetectKeyPhrases + DetectEntities + DetectSentiment per text)')
    print(f'  billed units                {units}   '
          f'(100 chars = 1 unit, minimum {comprehend.MIN_UNITS_PER_REQUEST} per request)')
    print(f'  published rate              ${comprehend.USD_PER_UNIT:.4f}/unit')
    print(f'  COST OF THIS RUN            {units} x ${comprehend.USD_PER_UNIT:.4f} '
          f'= ${usd:.4f}   <- charged, not waived')
    run30 = 30 * comprehend.units_for('x' * 60)
    print(f'  a 30-task chaos run         {run30} units = ${comprehend.usd_for(run30):.4f}')
    print()
    print(f'  AWS publishes a {comprehend.FREE_TIER_UNITS_PER_MONTH_IF_ELIGIBLE:,}-unit/month '
          f'Comprehend free tier and it does NOT apply here.')
    print('  That allowance is a TWELVE-MONTH offer, and this account has no twelve-month')
    print('  free tier — it postdates the programme and its credits are spent. Measured:')
    print('    aws freetier get-account-plan-state')
    print('      -> accountPlanType "PAID", accountPlanRemainingCredits $0.00')
    print("    aws freetier get-free-tier-usage --query 'freeTierUsages[].freeTierType'")
    print('      -> 12 rows, every one "Always Free". No "12 Months Free" row exists.')
    print('  Confirm the line item once Cost Explorer catches up (it lags ~24h):')
    print('    aws ce get-cost-and-usage --time-period Start=<today> End=<tomorrow> \\')
    print('      --granularity DAILY --metrics UnblendedCost \\')
    print('      --filter \'{"Dimensions":{"Key":"SERVICE","Values":["Amazon Comprehend"]}}\'')

    def _pct(xs: list[float]) -> dict:
        xs = sorted(xs)
        return {'n': len(xs), 'p50': round(statistics.median(xs), 1),
                'p95': round(xs[min(len(xs) - 1, int(len(xs) * 0.95))], 1),
                'max': round(max(xs), 1), 'min': round(min(xs), 1)}

    per_call = _pct(request_latencies)
    per_text = _pct(latencies)
    print(f'\n{BAR}')
    print('LATENCY — measured wall clock, no estimates')
    print(BAR)
    print(f'  per Detect* request   n={per_call["n"]:<4} p50 {per_call["p50"]:>6.1f} ms   '
          f'p95 {per_call["p95"]:>6.1f} ms   max {per_call["max"]:>6.1f} ms')
    print(f'  per classify()        n={per_text["n"]:<4} p50 {per_text["p50"]:>6.1f} ms   '
          f'p95 {per_text["p95"]:>6.1f} ms   max {per_text["max"]:>6.1f} ms')
    print('  classify() is three sequential requests. It sits on the triage path once per '
          'task,')
    print('  outside every transaction — db.tx() may re-run its callable on a 40001 and '
          'this must not.')

    # ------------------------------------------------- the reason it is wired in
    adv = None
    if adversarial:
        print(f'\n{BAR}')
        print('THE NARROWING, END TO END')
        print(BAR)
        base = llm._offline_triage(ADVERSARIAL, ADVERSARIAL_AMOUNT)
        s = signals[ADVERSARIAL]              # already paid for in the pass above
        out = comprehend.augment(base, s)
        comprehend.assert_cannot_widen(base, out)
        ceiling = 20_000               # the seed policy's max_auto_action_cents
        print(f'  text        {ADVERSARIAL}')
        print(f'  rules       {base.action.upper()} {base.amount_cents} cents '
              f'({base.exception_kind}) — '
              f'{"parks on a human anyway" if base.amount_cents > ceiling else "UNATTENDED: under the policy ceiling, no human sees it"}')
        print(f'  comprehend  phrases={list(s.key_phrases)} kinds={list(s.kinds)}')
        print(f'  narrowed    {out.action.upper()} {out.amount_cents} cents '
              f'({out.exception_kind})')
        print(f'  reason      {out.reason}')
        print('  assert_cannot_widen() passed: nothing here made the agent MORE able '
              'to act.')
        adv = {'rule_action': base.action, 'rule_amount_cents': base.amount_cents,
               'narrowed_action': out.action, 'narrowed_amount_cents': out.amount_cents,
               'key_phrases': list(s.key_phrases), 'kinds': list(s.kinds)}

    return {
        'ok': True, 'rows': rows,
        'agreement': {
            'graded': len(graded), 'agree': len(both),
            'comprehend_correct': len(c_right), 'rules_correct': len(r_right),
            'comprehend_silent': len(c_silent),
            'disagreements': [
                {'text': r['text'], 'comprehend': r['comprehend_kind'],
                 'rules': r['rule_kind'], 'truth': r['truth']} for r in disagree],
        },
        'cost': {'texts': len(rows), 'requests': calls, 'units': units,
                 'usd_per_unit': comprehend.USD_PER_UNIT,
                 'usd_charged': round(usd, 6),
                 'free_tier_applies': False,
                 'free_tier_note': (
                     'Comprehend\'s 50,000 units/month is a 12-month offer; this account '
                     'has no 12-month free tier (freetier get-account-plan-state -> PAID, '
                     '$0.00 credits; get-free-tier-usage -> 12 rows, all "Always Free")')},
        'latency_ms': {'per_request': per_call, 'per_classify': per_text},
        'adversarial': adv,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--json', action='store_true',
                    help='emit the machine-readable result instead of the report')
    ap.add_argument('--no-adversarial', dest='adversarial', action='store_false',
                    help='skip the ordered-rule-table demonstration')
    ap.add_argument('--limit', type=int, default=None, metavar='N',
                    help='only the first N texts — this run costs real money, see --help')
    args = ap.parse_args(argv)

    if args.json:
        # Keep stdout clean for the JSON: send the human report to stderr.
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            out = run(adversarial=args.adversarial, limit=args.limit)
        sys.stderr.write(buf.getvalue())
        print(json.dumps(out, indent=2))
    else:
        out = run(adversarial=args.adversarial, limit=args.limit)
    return 0 if out.get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(main())
