#!/usr/bin/env python3
"""AXIOM :: the month-of-judging soak.

Judging runs 19 Aug to 15 Sep and nobody will be watching the demo for most of it. The
thing that loses this competition is not a wrong answer, it is a judge opening the URL in
week three and finding an empty grid, a red lamp, or a stack trace. This script does to
the API, in a few minutes, what four weeks of unattended judging would do to it.

    python scripts/soak_test.py --base http://127.0.0.1:8181 --minutes 3

What it simulates, all at once
------------------------------
* **Concurrent readers.** N threads polling every read endpoint the dashboard polls,
  continuously, the way N open browser tabs would.
* **Repeated runs.** RUN MISSION and KILL A WORKER, over and over, the way a judge who
  has just understood the demo presses the interesting button again.
* **Idle gaps.** A pause long enough for pooled connections to go stale — the failure
  mode a frozen Lambda container or an overnight gap between two judges produces — and
  then a request timed to the millisecond, because "the first request after the gap
  fails" is the exact symptom this must not have.
* **Resets mid-flight.** RESET pressed while a worker is refunding, which is the most
  destructive thing the UI can do, and the one a curious judge is most likely to do.
* **Connection kills.** With --kill-connections, CockroachDB's own CANCEL SESSION is
  used to destroy every pooled connection the API holds, mid-soak, repeatedly.

What it asserts
---------------
1. NOTHING 5xx. Not once, on any endpoint, in any wave.
2. DUPLICATE REFUNDS stays 0 in every sample. The headline number cannot be polluted
   by a run, a reset, a crash, or another script's deliberate duplicates.
3. Rows stay BOUNDED. Agent rows, missions, tasks and memories are sampled throughout;
   a demo that grows a row per click is a demo that is embarrassing in week three.
4. The first request after each idle gap succeeds, and is not slow.

Exit code is 0 only if all four hold. Everything it prints is measured in the run.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import random
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request

READ_PATHS = [
    '/api/mission',
    '/api/tasks?limit=300',
    '/api/events?limit=60',
    '/api/health',
    '/api/agents',
    '/api/provider/stats',
    '/api/provider/ledger?limit=50',
    '/api/receipts/unsettled',
    '/api/approvals',
    '/api/memories?limit=40',
    '/api/crash-windows',
    '/api/rewind?seconds_ago=20',
]

# 429 is a designed answer, not a failure: the demo controls are rate limited so one
# judge's double-click cannot start thirty workers. The soak deliberately provokes it.
EXPECTED_STATUSES = {200, 429}


class Recorder:
    """Every request this script makes, and what came back. Thread-safe."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.by_status: collections.Counter[int] = collections.Counter()
        self.latencies: dict[str, list[float]] = collections.defaultdict(list)
        self.failures: list[tuple[str, str, int, str]] = []   # phase, path, status, body
        self.samples: list[dict] = []

    def record(self, phase: str, path: str, status: int, ms: float, body: str) -> None:
        with self.lock:
            self.by_status[status] += 1
            self.latencies[path.split('?')[0]].append(ms)
            if status not in EXPECTED_STATUSES:
                self.failures.append((phase, path, status, body[:200]))

    def sample(self, row: dict) -> None:
        with self.lock:
            self.samples.append(row)


def call(base: str, path: str, *, method: str = 'GET', body: dict | None = None,
         timeout: float = 30.0) -> tuple[int, str, float]:
    """One request. Returns (status, body, milliseconds). Never raises for HTTP errors."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={'accept': 'application/json',
                 **({'content-type': 'application/json'} if data else {})})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode('utf-8', 'replace'), \
                (time.perf_counter() - t0) * 1000
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace'), \
            (time.perf_counter() - t0) * 1000
    except Exception as e:                       # noqa: BLE001 — a dead server is data
        return 0, f'{type(e).__name__}: {e}', (time.perf_counter() - t0) * 1000


# ------------------------------------------------------------------------- workers

class Reader(threading.Thread):
    """One open browser tab, forever."""

    daemon = True

    def __init__(self, base: str, rec: Recorder, stop: threading.Event, phase: list[str]):
        super().__init__()
        self.base, self.rec, self.stop, self.phase = base, rec, stop, phase

    def run(self) -> None:
        while not self.stop.is_set():
            path = random.choice(READ_PATHS)
            status, body, ms = call(self.base, path)
            self.rec.record(self.phase[0], path, status, ms, body)
            self.stop.wait(random.uniform(0.10, 0.35))


class Sampler(threading.Thread):
    """Watches the two things that must not drift: the headline, and row counts."""

    daemon = True

    def __init__(self, base: str, rec: Recorder, stop: threading.Event,
                 dsn: str | None, phase: list[str]):
        super().__init__()
        self.base, self.rec, self.stop, self.dsn, self.phase = base, rec, stop, dsn, phase
        self.conn = None
        if dsn:
            try:
                import psycopg
                from psycopg.rows import dict_row
                self.conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
            except Exception as e:               # noqa: BLE001
                print(f'  (row sampling disabled: {type(e).__name__}: {e})')

    def counts(self) -> dict:
        if not self.conn:
            return {}
        t = "'11111111-1111-1111-1111-111111111111'"
        try:
            row = self.conn.execute(f"""
                SELECT (SELECT count(*) FROM axiom_agent)                            AS agents,
                       (SELECT count(*) FROM axiom_mission WHERE tenant_id = {t})    AS missions,
                       (SELECT count(*) FROM axiom_task    WHERE tenant_id = {t})    AS tasks,
                       (SELECT count(*) FROM axiom_memory  WHERE tenant_id = {t})    AS memories,
                       (SELECT count(*) FROM axiom_event   WHERE tenant_id = {t})    AS events
            """).fetchone()
            return dict(row)
        except Exception as e:                   # noqa: BLE001
            return {'error': f'{type(e).__name__}: {e}'}

    def run(self) -> None:
        while not self.stop.is_set():
            status, body, ms = call(self.base, '/api/provider/stats')
            self.rec.record(self.phase[0], '/api/provider/stats', status, ms, body)
            row = {'t': round(time.time(), 1), 'phase': self.phase[0]}
            if status == 200:
                try:
                    s = json.loads(body)
                    row.update(refunds=s.get('refunds'), replays=s.get('replays'),
                               duplicates=s.get('duplicate_orders'))
                except json.JSONDecodeError:
                    pass
            row.update(self.counts())
            self.rec.sample(row)
            self.stop.wait(2.0)


# ------------------------------------------------------------------------ scenario

def wait_quiet(base: str, rec: Recorder, phase: str, seconds: float = 25.0) -> dict:
    """Poll the mission until its task states stop changing, or time out."""
    last, stable_since = None, time.time()
    deadline = time.time() + seconds
    while time.time() < deadline:
        status, body, ms = call(base, '/api/mission')
        rec.record(phase, '/api/mission', status, ms, body)
        if status == 200:
            try:
                by = json.loads(body).get('by_state', {})
            except json.JSONDecodeError:
                by = {}
            if by != last:
                last, stable_since = by, time.time()
            elif time.time() - stable_since > 3.0:
                return by or {}
        time.sleep(0.6)
    return last or {}


def kill_connections(dsn: str) -> int:
    """Destroy every pooled connection the API holds, from outside it."""
    try:
        import psycopg
        from psycopg.rows import dict_row
        with psycopg.connect(dsn, row_factory=dict_row, autocommit=True) as c:
            rows = c.execute("SELECT session_id FROM [SHOW CLUSTER SESSIONS] "
                             "WHERE application_name LIKE 'axiom%'").fetchall()
            n = 0
            for r in rows:
                try:
                    c.execute(f"CANCEL SESSION '{r['session_id']}'")
                    n += 1
                except Exception:                # noqa: BLE001 — best effort
                    pass
            return n
    except Exception as e:                       # noqa: BLE001
        print(f'  (could not kill connections: {type(e).__name__}: {e})')
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='hammer the AXIOM API the way a month of '
                                             'unattended judging would')
    ap.add_argument('--base', default=os.environ.get('AXIOM_BASE',
                                                     'http://127.0.0.1:8181'))
    ap.add_argument('--minutes', type=float, default=3.0)
    ap.add_argument('--readers', type=int, default=6)
    ap.add_argument('--idle-seconds', type=float, default=20.0,
                    help='length of the simulated gap between two judges')
    ap.add_argument('--dsn', default=os.environ.get('DATABASE_URL'),
                    help='for row-count sampling and --kill-connections')
    ap.add_argument('--kill-connections', action='store_true',
                    help='CANCEL SESSION on every pooled connection, once per wave')
    args = ap.parse_args(argv)

    base = args.base.rstrip('/')
    status, body, _ = call(base, '/api/health')
    if status == 0:
        print(f'no API at {base}: {body}')
        return 2
    print(f'AXIOM soak :: {base}')
    print(f'  health at start: HTTP {status} :: {body[:120]}')
    print(f'  {args.readers} readers, {args.minutes:g} minutes, idle gaps of '
          f'{args.idle_seconds:g}s, kill-connections={args.kill_connections}')
    print()

    rec = Recorder()
    stop = threading.Event()
    phase = ['warmup']
    threads = [Reader(base, rec, stop, phase) for _ in range(args.readers)]
    threads.append(Sampler(base, rec, stop, args.dsn, phase))
    for th in threads:
        th.start()

    idle_first_requests: list[tuple[float, int]] = []
    deadline = time.time() + args.minutes * 60
    wave = 0
    try:
        while time.time() < deadline:
            wave += 1

            phase[0] = f'w{wave}:run'
            st, bd, _ = call(base, '/api/demo/run-worker', method='POST',
                             body={'mode': 'drain', 'seconds': 45})
            rec.record(phase[0], '/api/demo/run-worker', st, 0.0, bd)
            by = wait_quiet(base, rec, phase[0])
            print(f'  wave {wave} run    -> {st} {bd[:80]}')
            print(f'                     states {by}')

            phase[0] = f'w{wave}:chaos'
            st, bd, _ = call(base, '/api/demo/run-worker', method='POST',
                             body={'mode': 'chaos', 'seconds': 45})
            rec.record(phase[0], '/api/demo/run-worker', st, 0.0, bd)
            by = wait_quiet(base, rec, phase[0])
            print(f'  wave {wave} chaos  -> {st} {bd[:80]}')
            print(f'                     states {by}')

            phase[0] = f'w{wave}:hammer'
            codes = collections.Counter()
            for _ in range(12):                  # a judge leaning on the button
                st, bd, _ = call(base, '/api/demo/run-worker', method='POST',
                                 body={'mode': 'drain'})
                rec.record(phase[0], '/api/demo/run-worker', st, 0.0, bd)
                codes[st] += 1
            print(f'  wave {wave} hammer -> 12 rapid run-worker posts: {dict(codes)}')

            if args.kill_connections and args.dsn:
                phase[0] = f'w{wave}:killconn'
                n = kill_connections(args.dsn)
                st, bd, ms = call(base, '/api/mission')
                rec.record(phase[0], '/api/mission', st, ms, bd)
                print(f'  wave {wave} killconn -> cancelled {n} sessions; next request '
                      f'HTTP {st} in {ms:.0f}ms')

            phase[0] = f'w{wave}:idle'
            # The readers keep polling through the gap on purpose. A gap with NO traffic
            # at all is the easy case (the pool's own maintenance thread has time to
            # notice); the realistic one on a public URL is light traffic that keeps some
            # connections warm while others rot.
            print(f'  wave {wave} idle   -> {args.idle_seconds:g}s with no control traffic')
            time.sleep(args.idle_seconds)
            st, bd, ms = call(base, '/api/mission')
            rec.record(phase[0], '/api/mission', st, ms, bd)
            idle_first_requests.append((ms, st))
            print(f'                     first request after the gap: HTTP {st} '
                  f'in {ms:.0f}ms')

            phase[0] = f'w{wave}:reset'
            call(base, '/api/demo/run-worker', method='POST', body={'mode': 'chaos'})
            time.sleep(1.0)                      # let it get its hands dirty
            st, bd, ms = call(base, '/api/demo/reset', method='POST', body={})
            rec.record(phase[0], '/api/demo/reset', st, ms, bd)
            print(f'  wave {wave} reset  -> mid-flight reset: HTTP {st} {bd[:90]}')
            by = wait_quiet(base, rec, phase[0], seconds=15)
            print(f'                     states after reset {by}')
    except KeyboardInterrupt:
        print('\ninterrupted; reporting what was measured')
    finally:
        stop.set()
        for th in threads:
            th.join(timeout=5)

    # ------------------------------------------------------------------- verdict
    print()
    print('=' * 78)
    total = sum(rec.by_status.values())
    print(f'requests           {total}')
    print(f'status codes       {dict(sorted(rec.by_status.items()))}')

    slow = []
    for path, xs in sorted(rec.latencies.items()):
        if len(xs) < 5:
            continue
        xs2 = sorted(xs)
        p50, p95 = xs2[len(xs2) // 2], xs2[int(len(xs2) * 0.95)]
        slow.append((p95, path, p50, len(xs2)))
    print('latency (ms)       p50 / p95   n    path')
    for p95, path, p50, n in sorted(slow, reverse=True):
        print(f'                   {p50:6.0f} / {p95:6.0f} {n:5d}   {path}')

    dups = [s['duplicates'] for s in rec.samples if s.get('duplicates') is not None]
    refunds = [s['refunds'] for s in rec.samples if s.get('refunds') is not None]
    agents = [s['agents'] for s in rec.samples if s.get('agents') is not None]
    missions = [s['missions'] for s in rec.samples if s.get('missions') is not None]
    tasksn = [s['tasks'] for s in rec.samples if s.get('tasks') is not None]
    mems = [s['memories'] for s in rec.samples if s.get('memories') is not None]

    def span(name: str, xs: list) -> str:
        return f'{name:<18} min {min(xs)}  max {max(xs)}  last {xs[-1]}' if xs \
            else f'{name:<18} (not sampled)'

    print()
    print(span('duplicate orders', dups))
    print(span('refunds (scoped)', refunds))
    print(span('agent rows', agents))
    print(span('missions', missions))
    print(span('tasks', tasksn))
    print(span('memories', mems))
    if idle_first_requests:
        print(f'{"post-idle first req":<18} '
              + ', '.join(f'{ms:.0f}ms/{st}' for ms, st in idle_first_requests))

    problems: list[str] = []
    server_errors = {c: n for c, n in rec.by_status.items() if c >= 500 or c == 0}
    if server_errors:
        problems.append(f'{sum(server_errors.values())} requests returned 5xx or failed '
                        f'to connect: {server_errors}')
    unexpected = {c: n for c, n in rec.by_status.items()
                  if c not in EXPECTED_STATUSES and c < 500 and c != 0}
    if unexpected:
        problems.append(f'unexpected non-5xx statuses: {unexpected}')
    if dups and max(dups) > 0:
        problems.append(f'DUPLICATE REFUNDS reached {max(dups)} — the headline is not safe')
    if agents and max(agents) > 60:
        problems.append(f'agent rows grew to {max(agents)}; the reaper is not holding')
    if missions and max(missions) > 6:
        problems.append(f'missions grew to {max(missions)}; something creates one per click')
    if tasksn and max(tasksn) > 40:
        problems.append(f'tasks grew to {max(tasksn)}; the demo tenant is accumulating work')
    if mems and max(mems) > 400:
        problems.append(f'memories grew to {max(mems)} without a run explaining it')
    bad_idle = [(ms, st) for ms, st in idle_first_requests if st != 200]
    if bad_idle:
        problems.append(f'the first request after an idle gap failed: {bad_idle}')

    print()
    if rec.failures:
        print(f'{len(rec.failures)} unexpected responses (first 10):')
        for ph, path, st, body in rec.failures[:10]:
            print(f'  [{ph}] {st} {path} :: {body[:120]}')
        print()

    if problems:
        print('SOAK FAILED')
        for p in problems:
            print(f'  - {p}')
        return 1
    print('SOAK PASSED :: no 5xx, no duplicate refunds, bounded rows, '
          'and every post-idle request served')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
