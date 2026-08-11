#!/usr/bin/env python3
"""AXIOM :: one command that proves the safety claims, and prints the proof.

    python scripts/verify_invariants.py

Runs two things and reports them as one verdict:

  1. the invariant suite (tests/) — every crash window and every structural invariant,
     each asserted by trying to violate it;
  2. the chaos demo (scripts/chaos_demo.py) — a real mission, real worker processes,
     real SIGKILLs, audited against the external provider ledger afterwards.

They answer different questions and neither is sufficient alone. The suite proves the
protocol refuses each specific violation under conditions constructed to cause it; the
chaos run proves the whole system survives being killed at instants nobody chose. A
green suite with a red chaos run means the tests are testing the wrong thing.

The summary at the end is written to be pasted into the README verbatim.

Exit code 0 means every invariant held and the ledger showed zero duplicate refunds.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from axiom import db                                             # noqa: E402

# Group order is the order a reader should meet the evidence: the crash-window table
# first, because that is the claim; the plan assertions last, because they guard a
# regression nothing else can see.
GROUPS: list[tuple[str, str]] = [
    ('test_crash_windows', 'crash windows (W1-W7)'),
    ('test_invariants', 'execution, budget, approval, memory governance'),
    ('test_schema_sync', 'schema and Python agree'),
    ('test_recall_plan', 'ANN query plan'),
]

PASS, FAIL, XFAIL, SKIP = 'PASS', 'FAIL', 'XFAIL', 'SKIP'


@dataclass
class Case:
    module: str
    name: str
    verdict: str
    seconds: float
    detail: str = ''

    @property
    def tag(self) -> str:
        m = re.match(r'test_(w[1-7])_', self.name)
        return m.group(1).upper() if m else ''

    @property
    def label(self) -> str:
        body = re.sub(r'^test_(w[1-7]_)?', '', self.name)
        # Underscores are word separators in the function name and meaningful inside a
        # parametrize id, so only the former get rewritten.
        head, bracket, params = body.partition('[')
        return head.replace('_', ' ').strip() + (bracket + params if bracket else '')


# ------------------------------------------------------------------- preconditions

def live_workers() -> list[str]:
    """A running worker claims this suite's tasks and settles them out from under it.

    claim() is deliberately not tenant-scoped — workers are shared infrastructure — so
    this is a genuine precondition, not test tidiness. Failing here with a clear message
    beats a crash-window test failing for a reason that is not a bug.
    """
    def _q(cur):
        cur.execute("""
            SELECT worker_ref FROM axiom_agent
            WHERE status IN ('STARTING', 'ALIVE')
              AND heartbeat_at > now() - INTERVAL '30 seconds'
        """)
        return [r['worker_ref'] for r in cur.fetchall()]
    return db.tx(_q, readonly=True)


# --------------------------------------------------------------------------- pytest

def run_pytest(extra: list[str], quiet: bool) -> tuple[int, list[Case], str]:
    with tempfile.TemporaryDirectory() as tmp:
        xml_path = Path(tmp) / 'invariants.xml'
        cmd = [sys.executable, '-m', 'pytest', '-q', '--junit-xml', str(xml_path)] + extra
        proc = subprocess.run(cmd, cwd=str(ROOT), text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if not quiet:
            print(proc.stdout, end='')
        cases = parse_junit(xml_path) if xml_path.exists() else []
        return proc.returncode, cases, proc.stdout


def parse_junit(path: Path) -> list[Case]:
    root = ET.parse(path).getroot()
    out: list[Case] = []
    for tc in root.iter('testcase'):
        verdict, detail = PASS, ''
        for child in tc:
            if child.tag in ('failure', 'error'):
                verdict = FAIL
                detail = (child.get('message') or '').strip().splitlines()[0][:160]
            elif child.tag == 'skipped':
                kind = child.get('type') or ''
                verdict = XFAIL if 'xfail' in kind.lower() else SKIP
                # Kept whole: an xfail reason names a real defect and gets printed in
                # full at the bottom. Truncating it there would hide the diagnosis.
                detail = (child.get('message') or '').strip()
        out.append(Case(module=(tc.get('classname') or '').split('.')[-1],
                        name=tc.get('name') or '?',
                        verdict=verdict, seconds=float(tc.get('time') or 0.0),
                        detail=detail))
    return out


# ----------------------------------------------------------------------- chaos demo

CHAOS_FIELDS = {
    'workers SIGKILLed': r'workers SIGKILLed\s+(\d+)',
    'tasks terminal': r'tasks terminal\s+(\S+)',
    'refunds created': r'refunds created\s+(\d+)',
    'idempotent replays': r'idempotent replays\s+(\d+)',
    'duplicate refunds': r'DUPLICATE REFUNDS\s+(\d+)',
}


def run_chaos(quiet: bool, tasks: int) -> tuple[int, dict[str, str]]:
    cmd = [sys.executable, 'scripts/chaos_demo.py', '--workers', '3',
           '--tasks', str(tasks), '--kill-every', '2.5', '--quiet']
    proc = subprocess.run(cmd, cwd=str(ROOT), text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if not quiet:
        print(proc.stdout, end='')
    found = {}
    for label, pattern in CHAOS_FIELDS.items():
        m = re.search(pattern, proc.stdout)
        found[label] = m.group(1) if m else '?'
    return proc.returncode, found


# --------------------------------------------------------------------------- report

def rule(char: str = '-', width: int = 78) -> str:
    return char * width


def render(cases: list[Case], chaos: tuple[int, dict[str, str]] | None,
           pytest_rc: int, elapsed: float) -> bool:
    by_module: dict[str, list[Case]] = {}
    for c in cases:
        by_module.setdefault(c.module, []).append(c)

    print('\n' + rule('='))
    print('AXIOM :: invariant verification')
    print(rule('='))

    for module, heading in GROUPS:
        group = by_module.pop(module, [])
        if not group:
            continue
        print(f'\n{heading}')
        print(rule())
        for c in group:
            tag = f'{c.tag:<3}' if c.tag else '   '
            note = c.detail if c.verdict == FAIL else ''
            print(f'  [{c.verdict:<5}] {tag} {c.label}'
                  + (f'\n            -> {note}' if note else ''))
    for module, group in by_module.items():          # anything added later, unclassified
        print(f'\n{module}')
        print(rule())
        for c in group:
            print(f'  [{c.verdict:<5}]     {c.label}')

    passed = sum(1 for c in cases if c.verdict == PASS)
    failed = [c for c in cases if c.verdict == FAIL]
    xfailed = [c for c in cases if c.verdict == XFAIL]
    skipped = [c for c in cases if c.verdict == SKIP]

    print('\n' + rule('='))
    print(f'invariants: {passed}/{len(cases)} passed, {len(failed)} failed, '
          f'{len(xfailed)} known-broken (xfail), {len(skipped)} skipped   [{elapsed:.1f}s]')

    ok = pytest_rc == 0 and not failed
    if chaos is not None:
        rc, fields = chaos
        print(rule())
        print('chaos demo (real processes, real SIGKILLs, external ledger audited):')
        for label, value in fields.items():
            print(f'  {label:<22} {value}')
        ok = ok and rc == 0
        print(f'  verdict                {"PASS" if rc == 0 else "FAIL"}')

    print(rule('='))
    verdict = 'ALL INVARIANTS HELD' if ok else 'BROKEN — see the failures above'
    if ok and xfailed:
        # Never let a green banner absorb a known defect. An xfail is a bug with a test
        # attached, not a pass, and the summary has to read that way.
        verdict += f' — with {len(xfailed)} KNOWN DEFECT(S), listed below'
    print('VERDICT: ' + verdict)
    print(rule('='))

    if xfailed:
        print('\nknown-broken invariants (xfail — real defects with a test attached,')
        print('NOT passes; each will turn into a hard failure the moment it is fixed):')
        for c in xfailed:
            print(f'\n  {c.module}::{c.name}')
            print(textwrap.indent(textwrap.fill(c.detail, width=72), '    '))

    print('\n--- README block ---------------------------------------------------------\n')
    print('| # | invariant | verdict |')
    print('| --- | --- | --- |')
    for module, _ in GROUPS:
        for c in [c for c in cases if c.module == module]:
            print(f'| {c.tag or "-"} | {c.label} | {c.verdict} |')
    if chaos is not None:
        rc, fields = chaos
        print(f'| - | chaos: {fields["workers SIGKILLed"]} SIGKILLs, '
              f'{fields["idempotent replays"]} idempotent replays, '
              f'{fields["duplicate refunds"]} duplicate refunds | '
              f'{"PASS" if rc == 0 else "FAIL"} |')
    print('\n--------------------------------------------------------------------------')
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description='run every AXIOM invariant and report')
    ap.add_argument('--no-chaos', action='store_true',
                    help='invariant suite only; skip the kill-the-workers run')
    ap.add_argument('--tasks', type=int, default=30, help='chaos demo mission size')
    ap.add_argument('-k', dest='select', default=None, help='pytest -k selector')
    ap.add_argument('--quiet', action='store_true', help='summary only')
    args = ap.parse_args()

    print(f'database: {os.environ.get("DATABASE_URL", "(unset — using the local default)")}')
    busy = live_workers()
    if busy:
        print(f'\nREFUSING TO RUN: a worker is alive on this cluster ({", ".join(busy)}).\n'
              'It will claim the suite\'s tasks and settle them out from under it. '
              'Stop the workers and re-run.')
        return 2

    t0 = time.time()
    extra = ['-k', args.select] if args.select else []
    rc, cases, _ = run_pytest(extra, args.quiet)
    if not cases:
        print('\npytest produced no results — the suite did not run. Output above.')
        return rc or 1

    chaos = None
    if not args.no_chaos:
        print('\nrunning the chaos demo (this kills worker processes on purpose)...')
        chaos = run_chaos(args.quiet, args.tasks)

    ok = render(cases, chaos, rc, time.time() - t0)
    db.close_pool()
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
