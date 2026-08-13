#!/usr/bin/env python3
"""AXIOM :: the incumbent probe.

    python scripts/incumbent_probe.py

The objection this script exists to answer, stated as fairly as I can:

    "Temporal plus an idempotency key already does the execution half. LangGraph has a
     Postgres checkpointer. Letta has archival memory with vector search. You have
     re-implemented durable execution and called it memory."

Half of that is correct, and the first thing this script does is prove the correct half
against the same provider the rest of the repo uses. **Arm 2 below is a demonstration
that the incumbent answer works.** A comparison that cannot lose is marketing.

What the incumbent answer cannot do is the other half, and the seam is not subtle once
you look at it: durable execution lives in one system and semantic memory lives in
another, so the recovery decision is assembled from two reads of two systems that were
written by two commits. There is no point in time at which the thing the agent believed
was true. This script constructs that state and then shows AXIOM cannot reach it.

    ARM 1  SPLIT-NAIVE      two stores, key minted at call time         -> 2 refunds
    ARM 2  SPLIT-CORRECT    two stores, key derived from durable id     -> 1 refund
    ARM 3  SPLIT + MEMORY   arm 2, plus one concurrent memory revocation
    ARM 4  AXIOM            the identical interleavings, one transaction

Arms 1-3 do not install Temporal, LangGraph or Letta. They model the ONE structural
property those systems share and AXIOM does not: **the number of commit points.**

WHAT THIS MODELS
    * two durable stores with independent transactions and independent commit points
    * a recovery path that must consult both to decide
    * a semantic store whose write becomes queryable only after its own commit
      (and, optionally, after an index-apply lag -- see --semantic-lag; default 0)
    * the real payment provider from axiom/provider.py, with real Stripe-style
      idempotency semantics, in its own database over its own connection

WHAT THIS DOES NOT MODEL, and nobody should read it as
    * Temporal's replay engine, event history, retry policies, timers, signals,
      versioning, or operational maturity. It is not a benchmark. It says nothing
      about anyone's throughput, latency, availability, or engineering quality.
    * LangGraph's Pregel loop or its actual checkpoint serialization.
    * Letta's agent loop or its hybrid retrieval.
    * Any claim that these systems are wrong. They are not. docs/COMPARISON.md names
      the workloads where the honest answer is "use Temporal" and means it.

The interleavings in arm 3 are CHOSEN, not sampled. The claim is not "this happens
often." The claim is "this is reachable in an architecture with two commit points, and
unreachable in one with a single serializable transaction." Frequency is a tuning
question; reachability is a correctness question, and only the second one is settled by
a demo. `--semantic-lag` defaults to 0 precisely so that no result here depends on
assuming a vector store is slow.

The script prints INCONCLUSIVE rather than PASS if any arm fails to produce the
condition it is supposed to produce -- including if the split arm refuses to race.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import random
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from axiom import db, embeddings, memory, policy, provider, tasks    # noqa: E402
from axiom.models import (AttemptState, MemoryClass, Outcome,        # noqa: E402
                          RetrievalClass, TaskState, Trust, ctx_state)
from axiom.provider import ProviderCrash                             # noqa: E402

# The probe gets its OWN tenant, and this is not fastidiousness.
#
# Arm 4 writes DUPLICATE_EFFECT memories on purpose — that is the operator judgement the
# whole arm is about. Written into the demo tenant they would be permanent, admissible,
# first-party evidence in the recall set that the LIVE DEMO's recovery path reads, and
# every future recovery would drift toward ESCALATE because a benchmark ran once. A
# script that changes what the product decides is not a measurement of the product.
#
# The second reason is evidentiary: on a clean tenant the recall set contains exactly the
# memory under test, so "the decision changed" is attributable. On the demo tenant the
# probe's first draft reported ESCALATE in every trial — correctly, but for reasons that
# had nothing to do with the race, which would have been an accidental overclaim.
PROBE_TENANT = uuid.UUID('33333333-3333-3333-3333-333333333333')

# $150. Under the seeded refund_authority ceiling of $200 on purpose: the approval gate
# is a real and separate AXIOM property, demonstrated in scripts/counterexample.py, and
# routing this probe through a human approval would only obscure the one thing it is
# here to isolate -- the seam between execution state and memory.
AMOUNT = 15_000
_RUN = uuid.uuid4().hex[:6]

# The provider ledger is append-only and shared across every script in this repo, so
# every order ref is unique per run. Reusing a fixed ref would let one run's refunds
# inflate the next run's count, which is a rigged comparison even when the mechanism is
# real. scripts/counterexample.py learned this the same way.
ORDER_NAIVE = f'IP-NAIVE-{_RUN}'
ORDER_CORRECT = f'IP-CORRECT-{_RUN}'

# Every order ref this run touches, so the run can take its own rows back out again.
#
# Arm 1 creates a REAL duplicate in the provider ledger on purpose, and that ledger is
# shared with the demo. `provider.stats()` unscoped is what /api/health reports as
# `duplicate_orders_global`, and the one number this project is judged on reading 1
# because a comparison script ran would be self-inflicted. The API defaults to mission
# scope for exactly this reason, but "the safety net caught it" is not a licence to
# drop things into the net. --keep-ledger opts out if you want to inspect the rows.
_ORDER_REFS: list[str] = [ORDER_NAIVE, ORDER_CORRECT]

SITUATION = 'duplicate_charge: agent died after dispatch, outcome unknown'
GOOD_CONTENT = (f'{SITUATION} | probe {_RUN} | recovered by re-sending under the same '
                f'idempotency key; provider replayed the original refund')
BAD_CONTENT = (f'{SITUATION} | probe {_RUN} | REVOKED: for this merchant an unattended '
               f're-send at this state produced a duplicate effect; require a human')


# ======================================================================= the two stores
#
# Two SQLite files. Not a toy stand-in for a database -- SQLite gives real transactions
# with real commit points, which is the only property under test. Each store gets its
# own connection because that is the fact of the architecture being modelled: a workflow
# engine's persistence and a vector database are two systems, and no client library
# exists that can commit to both at once.

class Store:
    """One durable store. One connection. Its own transactions."""

    def __init__(self, path: pathlib.Path, name: str):
        self.name = name
        self.conn = sqlite3.connect(path, isolation_level=None)   # explicit BEGIN/COMMIT
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA synchronous=FULL')              # actually durable

    def begin(self) -> None:
        self.conn.execute('BEGIN IMMEDIATE')

    def commit(self) -> None:
        self.conn.execute('COMMIT')

    def rollback(self) -> None:
        self.conn.execute('ROLLBACK')

    def q(self, sql: str, args: tuple = ()) -> list[tuple]:
        return self.conn.execute(sql, args).fetchall()

    def x(self, sql: str, args: tuple = ()) -> None:
        self.conn.execute(sql, args)

    def close(self) -> None:
        self.conn.close()


class WorkflowStore(Store):
    """System A: the durable-execution store.

    Holds what a workflow engine holds -- which run exists, which step it is on, whether
    that step completed, and an append-only audit log. Deliberately opaque: `detail` is
    a JSON blob, exactly as a Temporal Event History payload or a LangGraph checkpoint
    is a serialized blob. You can read it if you know the run id. You cannot ask it
    "what happened the last time an agent died at this point", because nothing in it is
    an embedding and nothing in it is content-addressable.
    """

    def __init__(self, path: pathlib.Path):
        super().__init__(path, 'workflow-store')
        self.x('''CREATE TABLE IF NOT EXISTS wf_run (
                    run_id TEXT PRIMARY KEY, order_ref TEXT, amount_cents INTEGER,
                    step TEXT, step_state TEXT, provider_ref TEXT)''')
        self.x('''CREATE TABLE IF NOT EXISTS wf_event (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT,
                    kind TEXT, detail TEXT)''')

    def start_run(self, run_id: str, order_ref: str, amount: int) -> None:
        self.begin()
        self.x('INSERT INTO wf_run VALUES (?,?,?,?,?,?)',
               (run_id, order_ref, amount, 'refund', 'STARTED', None))
        self.x('INSERT INTO wf_event (run_id, kind, detail) VALUES (?,?,?)',
               (run_id, 'step.started', json.dumps({'step': 'refund'})))
        self.commit()

    def complete_run(self, run_id: str, provider_ref: str) -> None:
        self.begin()
        self.x('UPDATE wf_run SET step_state=?, provider_ref=? WHERE run_id=?',
               ('COMPLETED', provider_ref, run_id))
        self.x('INSERT INTO wf_event (run_id, kind, detail) VALUES (?,?,?)',
               (run_id, 'step.completed', json.dumps({'provider_ref': provider_ref})))
        self.commit()

    def read_run(self, run_id: str) -> dict | None:
        rows = self.q('SELECT run_id, order_ref, amount_cents, step, step_state, '
                      'provider_ref FROM wf_run WHERE run_id=?', (run_id,))
        if not rows:
            return None
        k = ('run_id', 'order_ref', 'amount_cents', 'step', 'step_state', 'provider_ref')
        return dict(zip(k, rows[0]))

    def append_event(self, run_id: str, kind: str, detail: dict) -> None:
        self.begin()
        self.x('INSERT INTO wf_event (run_id, kind, detail) VALUES (?,?,?)',
               (run_id, kind, json.dumps(detail)))
        self.commit()

    def has_event(self, run_id: str, kind: str) -> bool:
        return bool(self.q('SELECT 1 FROM wf_event WHERE run_id=? AND kind=? LIMIT 1',
                           (run_id, kind)))


class SemanticStore(Store):
    """System B: the vector store.

    Holds embedded episodes with a constrained outcome, which is more than most agent
    memory stacks bother with. Cosine similarity, exact scan -- no ANN approximation,
    because approximation would only make this store look WORSE and the point is to
    give the incumbent architecture its best case.

    `visible_at` models index-apply lag. It defaults to zero. Pinecone documents real
    lag here ("Pinecone is eventually consistent, so there can be a slight delay before
    new or changed records are visible to queries") but this probe assumes none, so that
    nothing below depends on a vector store being slow.
    """

    def __init__(self, path: pathlib.Path, lag_s: float = 0.0):
        super().__init__(path, 'semantic-store')
        self.lag_s = lag_s
        self.x('''CREATE TABLE IF NOT EXISTS mem (
                    id TEXT PRIMARY KEY, content TEXT, outcome TEXT, vec TEXT,
                    revoked INTEGER DEFAULT 0, visible_at REAL DEFAULT 0,
                    revoked_visible_at REAL DEFAULT 0)''')

    def write(self, content: str, outcome: str) -> str:
        mid = uuid.uuid4().hex
        self.x('INSERT INTO mem (id, content, outcome, vec, visible_at) VALUES (?,?,?,?,?)',
               (mid, content, outcome, json.dumps(embeddings.embed_list(content)),
                time.time() + self.lag_s))
        return mid

    def revoke(self, mem_id: str) -> None:
        self.x('UPDATE mem SET revoked=1, revoked_visible_at=? WHERE id=?',
               (time.time() + self.lag_s, mem_id))

    def search(self, query: str, k: int = 5) -> list[dict]:
        """Top-k by cosine similarity over the rows visible right now."""
        qv = embeddings.embed_list(query)
        now = time.time()
        out = []
        for mid, content, outcome, vec, revoked, vis, rvis in self.q(
                'SELECT id, content, outcome, vec, revoked, visible_at, '
                'revoked_visible_at FROM mem'):
            if vis > now:
                continue                                    # write not yet indexed
            if revoked and rvis <= now:
                continue                                    # revocation is visible
            v = json.loads(vec)
            dot = sum(a * b for a, b in zip(qv, v))
            na = math.sqrt(sum(a * a for a in qv)) or 1.0
            nb = math.sqrt(sum(b * b for b in v)) or 1.0
            out.append({'id': mid, 'content': content, 'outcome': outcome,
                        'similarity': dot / (na * nb)})
        out.sort(key=lambda r: -r['similarity'])
        return out[:k]


# ================================================================= ARM 1 / ARM 2
#
# Same crash, same instant (window W4: the provider committed the refund and the process
# died before anything recorded it). The only difference between the two arms is where
# the idempotency key comes from.

def arm_naive(wf: WorkflowStore) -> dict:
    """Key minted at call time. LangGraph ships no key at all; its own durable-execution
    guide says the fix is yours: 'Use idempotency keys or verify existing results to
    avoid unintended duplication.' This arm is what happens when you do not."""
    run_id = f'run-{uuid.uuid4().hex[:10]}'
    wf.start_run(run_id, ORDER_NAIVE, AMOUNT)

    body = {'order_ref': ORDER_NAIVE, 'amount_cents': AMOUNT, 'currency': 'USD'}
    crashed = False
    try:
        # The key exists only in this process's memory. Nothing durable ever saw it.
        provider.create_refund(idempotency_key=f'k-{uuid.uuid4().hex}',
                               order_ref=ORDER_NAIVE, amount_cents=AMOUNT,
                               request_body=body, chaos_post=1.0, latency_ms=20)
    except ProviderCrash:
        crashed = True

    # --- restart. The store says the step started and never completed. LangGraph's
    # docs describe this resumption exactly: "the workflow's resumption will re-run the
    # task". Re-running is CORRECT here; the bug is that the re-run cannot reuse a key
    # that was never written down.
    state = wf.read_run(run_id)
    replan = state['step_state'] != 'COMPLETED'
    result = provider.create_refund(idempotency_key=f'k-{uuid.uuid4().hex}',
                                    order_ref=ORDER_NAIVE, amount_cents=AMOUNT,
                                    request_body=body, latency_ms=20)
    wf.complete_run(run_id, result.provider_ref)

    led = provider.ledger(order_ref=ORDER_NAIVE)
    return {'crashed': crashed, 'replan': replan, 'refunds': len(led),
            'cents': sum(int(r['amount_cents']) for r in led),
            'replays': sum(int(r['replay_count']) for r in led)}


def arm_correct(wf: WorkflowStore) -> dict:
    """Key derived from durable workflow identity.

    This is what Temporal's docs tell you to do, near enough verbatim: "you can use
    `workflowRunId + '-' + activityId`" because "this value will be constant across
    Activity retries, and unique among all Workflows." It works. It is the right answer.
    A repo that pretends otherwise deserves to be discounted.
    """
    run_id = f'run-{uuid.uuid4().hex[:10]}'
    wf.start_run(run_id, ORDER_CORRECT, AMOUNT)

    body = {'order_ref': ORDER_CORRECT, 'amount_cents': AMOUNT, 'currency': 'USD'}
    key = f'{run_id}-refund'          # a pure function of state the store already holds
    crashed = False
    try:
        provider.create_refund(idempotency_key=key, order_ref=ORDER_CORRECT,
                               amount_cents=AMOUNT, request_body=body,
                               chaos_post=1.0, latency_ms=20)
    except ProviderCrash:
        crashed = True

    state = wf.read_run(run_id)
    recomputed = f"{state['run_id']}-refund"          # same key, recomputed from the store
    result = provider.create_refund(idempotency_key=recomputed, order_ref=ORDER_CORRECT,
                                    amount_cents=AMOUNT, request_body=body, latency_ms=20)
    wf.complete_run(run_id, result.provider_ref)

    led = provider.ledger(order_ref=ORDER_CORRECT)
    return {'crashed': crashed, 'key_stable': key == recomputed, 'refunds': len(led),
            'cents': sum(int(r['amount_cents']) for r in led),
            'replays': sum(int(r['replay_count']) for r in led),
            'replayed': result.replayed}


# ========================================================================= ARM 3
#
# Arm 2 established that the split architecture does not double-refund. So the fight is
# not about the money on the happy path; it is about the DECISION on the recovery path.
#
# The situation: run R dispatched a refund and died before recording the outcome. An
# operator then determines that for this merchant an unattended re-send at this state
# has previously produced a duplicate effect, and revokes the memory that said otherwise.
#
# That single human judgement has to land in BOTH systems: an audit event in the
# execution store, and a revocation plus a replacement episode in the semantic store.
# Two stores, two commits. Every schedule below is legal, and each one is a state in
# which a recovering agent reads a world that never existed.

def _decide(saw_event: bool, hits: list[dict]) -> str:
    """The recovery decision, modelled the same way tasks.recover() models it:
    a live receipt means re-send under the same key unless memory objects."""
    adverse = [h for h in hits if h['outcome'] in ('DUPLICATE_EFFECT', 'HUMAN_REQUIRED')]
    return 'ESCALATE' if adverse and len(adverse) >= len(hits) / 2 else 'RESEND'


def arm_split_seam(wf: WorkflowStore, sem: SemanticStore) -> list[dict]:
    """Four schedules. Each one is checked, not narrated."""
    findings: list[dict] = []
    run_id = f'run-{uuid.uuid4().hex[:10]}'
    wf.start_run(run_id, f'IP-SEAM-{_RUN}', AMOUNT)

    def fresh_memory() -> str:
        """A clean pair of stores for each schedule, so schedules cannot contaminate
        each other -- the failure mode that makes a demo look better than it is."""
        sem.begin()
        sem.x('DELETE FROM mem')
        good = sem.write(GOOD_CONTENT, str(Outcome.RESOLVED))
        sem.commit()
        return good

    # ---- S1: writer commits A then B. Reader lands between the commits. ------------
    good = fresh_memory()
    wf.append_event(run_id, 'memory.revoked', {'memory_id': good})   # ops commits A
    saw_event = wf.has_event(run_id, 'memory.revoked')               # reader reads A
    hits = sem.search(SITUATION)                                     # reader reads B
    sem.begin(); sem.revoke(good); sem.write(BAD_CONTENT, str(Outcome.DUPLICATE_EFFECT))
    sem.commit()                                                     # ops commits B
    cited_revoked = any(h['id'] == good for h in hits)
    findings.append({
        'id': 'S1', 'schedule': 'ops COMMIT A -> reader reads A,B -> ops COMMIT B',
        'contradiction': saw_event and cited_revoked,
        'decision': _decide(saw_event, hits),
        'note': 'the audit store already recorded the revocation; the decision cites '
                'the revoked memory as its evidence anyway',
    })

    # ---- S2: writer commits B then A. Same window, mirrored. -----------------------
    good = fresh_memory()
    sem.begin(); sem.revoke(good); sem.write(BAD_CONTENT, str(Outcome.DUPLICATE_EFFECT))
    sem.commit()                                                     # ops commits B
    saw_event = wf.has_event(run_id, 'memory.revoked.s2')            # reader reads A
    hits = sem.search(SITUATION)                                     # reader reads B
    wf.append_event(run_id, 'memory.revoked.s2', {'memory_id': good})  # ops commits A
    escalating_on_nothing = (not saw_event) and _decide(saw_event, hits) == 'ESCALATE'
    findings.append({
        'id': 'S2', 'schedule': 'ops COMMIT B -> reader reads A,B -> ops COMMIT A',
        'contradiction': escalating_on_nothing,
        'decision': _decide(saw_event, hits),
        'note': 'the agent escalates a $150 refund on evidence the execution store has '
                'no record of; the audit trail cannot explain the decision',
    })

    # ---- S3: the writer dies between its two commits. -----------------------------
    good = fresh_memory()
    sem.begin(); sem.revoke(good); sem.write(BAD_CONTENT, str(Outcome.DUPLICATE_EFFECT))
    sem.commit()                                                     # ops commits B
    # <process dies here -- the A-side commit never happens, and never will>
    diverged = (not wf.has_event(run_id, 'memory.revoked.s3')) and \
               not any(h['id'] == good for h in sem.search(SITUATION))
    findings.append({
        'id': 'S3', 'schedule': 'ops COMMIT B -> ops process dies -> A never written',
        'contradiction': diverged,
        'decision': _decide(False, sem.search(SITUATION)),
        'note': 'permanent divergence. Nothing repairs it; you write and operate a '
                'reconciliation job. DBOS says the same about split state: "resolving '
                'discrepancies requires additional infrastructure such as '
                'reconciliation jobs"',
    })

    # ---- S4: the writer is atomic in EACH store. The READER still spans two. ------
    # This is the schedule that cannot be engineered away, and it is the important one.
    good = fresh_memory()
    saw_event = wf.has_event(run_id, 'memory.revoked.s4')            # reader reads A @t0
    wf.append_event(run_id, 'memory.revoked.s4', {'memory_id': good})
    sem.begin(); sem.revoke(good); sem.write(BAD_CONTENT, str(Outcome.DUPLICATE_EFFECT))
    sem.commit()                                                     # both stores updated
    hits = sem.search(SITUATION)                                     # reader reads B @t2
    findings.append({
        'id': 'S4', 'schedule': 'reader reads A -> ops COMMIT A and COMMIT B -> reader reads B',
        'contradiction': (not saw_event) and any(
            h['outcome'] == str(Outcome.DUPLICATE_EFFECT) for h in hits),
        'decision': _decide(saw_event, hits),
        'note': 'each store was updated atomically and the reader still assembled a read '
                'set spanning two timestamps: A as of t0, B as of t2. It corresponds to '
                'no single point in time in either system',
    })
    return findings


# ========================================================================= ARM 4
#
# The same interleavings, through the engine. The mapping is exact:
#
#   S1, S2, S4  ->  D1: one operator transaction writes the event AND the revocation.
#                       A reader on one serializable snapshot sees both or neither.
#                       There is no schedule in which it sees one and not the other,
#                       because there is no schedule -- there is one commit.
#   S3          ->  D2: the operator transaction aborts. Neither write survives.
#                       Divergence is not repaired; it is unrepresentable.
#
# This is not a probabilistic result and it should not be read as one. The trials below
# exist to make the claim FALSIFIABLE, not to establish it: if the predicate ever fired,
# the design would be wrong, and the loop is where you would find out.

def _reset_probe_tenant() -> None:
    """Recreate the probe tenant from nothing. Run before EVERY trial.

    Per-trial and not per-run: trial N's operator writes a DUPLICATE_EFFECT memory that
    would still be admissible during trial N+1's recall, so the second trial would
    escalate because of the first one rather than because of its own race. Isolating
    the trials is what makes each row of the output mean what it says.
    """
    def _wipe(cur):
        for table in ('axiom_event', 'axiom_approval', 'axiom_action_attempt',
                      'axiom_memory', 'axiom_task', 'axiom_mission', 'axiom_policy'):
            cur.execute(f'DELETE FROM {table} WHERE tenant_id = %s', (str(PROBE_TENANT),))
        cur.execute("""
            INSERT INTO axiom_tenant (id, slug, display_name)
            VALUES (%s, 'incumbent-probe', 'Incumbent Probe')
            ON CONFLICT (id) DO NOTHING
        """, (str(PROBE_TENANT),))
        # tasks.prepare() defaults to policy_id='refund_authority' and requires exactly
        # one ACTIVE version. Same $200 ceiling as the seeded demo policy so the probe
        # is not quietly running under different authority than the product.
        policy.publish(
            cur, tenant_id=PROBE_TENANT, policy_id='refund_authority', version=1,
            body={'description': 'probe fixture', 'max_auto_action_cents': 20000},
            max_auto_action_cents=20000, requires_approval=False,
            created_by='human:incumbent_probe', activate=True)
    db.tx(_wipe)


def _setup_axiom_trial() -> dict:
    """One task, crashed in W4, with one ACTIONABLE memory the operator will revoke."""
    _reset_probe_tenant()
    order = f'IP-AXIOM-{_RUN}-{uuid.uuid4().hex[:4]}'
    _ORDER_REFS.append(order)
    agent = db.tx(lambda cur: tasks.register_agent(
        cur, worker_ref=f'probe-{uuid.uuid4().hex[:6]}'))
    mission = db.tx(lambda cur: tasks.create_mission(
        cur, tenant_id=PROBE_TENANT, title='Incumbent probe', goal='one recovery decision',
        budget_cents=100_000, created_by='human:incumbent_probe'))
    task_id = db.tx(lambda cur: tasks.enqueue(
        cur, tenant_id=PROBE_TENANT, mission_id=mission, task_type='refund',
        dedupe_key=f'order:{order}:refund',
        payload={'order_ref': order, 'amount_cents': AMOUNT,
                 'description': 'duplicate charge', 'exception_kind': 'duplicate_charge'}))

    # The memory the operator is about to revoke. Written with the probe's own situation
    # text so it is the nearest neighbour of the recovery query -- which is what a
    # genuinely relevant prior recovery WOULD be. If it fails to come back in the recall
    # the trial proves nothing, and the caller reports that rather than passing.
    good_id = db.tx(lambda cur: memory.write(
        cur, tenant_id=PROBE_TENANT, memory_class=MemoryClass.EPISODIC,
        context_key=ctx_state(TaskState.ACTION_PREPARED), content=GOOD_CONTENT,
        embedding=embeddings.embed_list(GOOD_CONTENT), outcome=Outcome.RESOLVED,
        source='system:execution', trust_level=Trust.FIRST_PARTY, actor='system:probe'))

    claimed = db.tx(lambda cur: tasks.claim(cur, agent_id=agent, task_id=task_id))
    prepared = db.tx(lambda cur: tasks.prepare(
        cur, task=claimed, agent_id=agent, step_name='refund',
        provider_name='payments', operation='refunds.create',
        request_body={'order_ref': order, 'amount_cents': AMOUNT, 'currency': 'USD',
                      'reason': 'duplicate_charge'},
        amount_cents=AMOUNT))
    if prepared.parked:                      # would mean the $200 ceiling moved under us
        raise RuntimeError('probe amount was gated by policy; adjust AMOUNT')

    try:
        provider.create_refund(idempotency_key=prepared.receipt.idempotency_key,
                               order_ref=order, amount_cents=AMOUNT,
                               request_body=prepared.receipt.request_body,
                               chaos_post=1.0, latency_ms=20)         # dies in W4
    except ProviderCrash:
        pass

    # The lease lapses; a second worker takes the task over.
    db.tx(lambda cur: cur.execute(
        "UPDATE axiom_task SET available_at = now() - INTERVAL '1 second' WHERE id = %s",
        (str(task_id),)))
    agent_b = db.tx(lambda cur: tasks.register_agent(
        cur, worker_ref=f'probe-b-{uuid.uuid4().hex[:6]}'))
    return {'order': order, 'task_id': task_id, 'agent_a': agent, 'agent_b': agent_b,
            'good_id': good_id, 'receipt_key': prepared.receipt.idempotency_key}


def _axiom_trial(trial: dict, ops_delay_ms: float, rec_delay_ms: float) -> dict:
    """Race one operator revocation against one recovery. Check the predicate.

    BOTH delays are parameters because a probe that only ever runs the recovery first
    proves nothing. The predicate is `saw_event AND cited_revoked`; if the recovery
    always wins, `saw_event` is always False and the predicate is trivially satisfied
    for the boring reason. The caller drives both orderings and refuses to pass unless
    it observed both -- that guard is the difference between a test and a formality.
    """
    vec = embeddings.embed_list(SITUATION)
    gid = trial['good_id']
    out: dict = {}
    start = threading.Barrier(2)

    def operator() -> None:
        # ONE transaction: revoke the memory, write its replacement, journal the event.
        start.wait()
        time.sleep(ops_delay_ms / 1000.0)
        try:
            db.tx(lambda cur: (
                memory.quarantine(cur, tenant_id=PROBE_TENANT, memory_id=gid,
                                  reason='probe: revoked by operator', by='human:ops'),
                memory.write(cur, tenant_id=PROBE_TENANT, memory_class=MemoryClass.EPISODIC,
                             context_key=ctx_state(TaskState.ACTION_PREPARED),
                             content=BAD_CONTENT,
                             embedding=embeddings.embed_list(BAD_CONTENT),
                             outcome=Outcome.DUPLICATE_EFFECT, source='human:operator',
                             trust_level=Trust.VERIFIED, actor='human:ops')))
            out['ops_committed'] = True
        except Exception as e:                       # noqa: BLE001 -- reported, not hidden
            out['ops_error'] = repr(e)

    def recovery() -> None:
        start.wait()
        time.sleep(rec_delay_ms / 1000.0)

        def _body(cur):
            # Both reads, one snapshot. `saw_event` asks the JOURNAL whether the
            # revocation happened; `recalled` asks the VECTOR INDEX what is admissible.
            # In the split architecture these are two systems. Here they are two
            # SELECTs at one timestamp, and that is the entire argument.
            cur.execute("""
                SELECT count(*) AS n FROM axiom_event
                WHERE tenant_id = %s AND subject_type = 'memory' AND subject_id = %s
                  AND event_type = 'memory.quarantined'
            """, (str(PROBE_TENANT), str(gid)))
            saw_event = cur.fetchone()['n'] > 0
            claimed = tasks.claim(cur, agent_id=trial['agent_b'], task_id=trial['task_id'])
            if claimed is None:
                return {'skipped': 'task not claimable'}
            plan = tasks.recover(cur, task=claimed, agent_id=trial['agent_b'],
                                 situation_embedding=vec, step_name='refund')
            cited = {str(r.id) for r in plan.recalled}
            adverse = sum(1 for r in plan.recalled if r.outcome in (
                str(Outcome.DUPLICATE_EFFECT), str(Outcome.HUMAN_REQUIRED)))
            return {'saw_event': saw_event, 'cited_revoked': str(gid) in cited,
                    'action': plan.action, 'recalled': len(plan.recalled),
                    'adverse': adverse,
                    'key': plan.receipt.idempotency_key if plan.receipt else None}

        try:
            out['rec'] = db.tx(_body)
        except Exception as e:                       # noqa: BLE001
            out['rec_error'] = repr(e)

    t1 = threading.Thread(target=operator)
    t2 = threading.Thread(target=recovery)
    t1.start(); t2.start(); t1.join(); t2.join()
    return out


def _settle_trial(trial: dict) -> None:
    """Finish the task honestly: re-send under the SAME key, settle, retire the agents.

    A probe that leaves tasks stranded in ACTION_PREPARED shows up later as an unsettled
    receipt on Mission Control and as a phantom ALIVE worker that makes the invariant
    suite refuse to run. Whatever a script starts, it finishes.
    """
    try:
        claimed = db.tx(lambda cur: tasks.claim(
            cur, agent_id=trial['agent_b'], task_id=trial['task_id']))
        if claimed is not None:
            plan = db.tx(lambda cur: tasks.recover(
                cur, task=claimed, agent_id=trial['agent_b'],
                situation_embedding=embeddings.embed_list(SITUATION), step_name='refund'))
            if plan.receipt is not None:
                res = provider.create_refund(
                    idempotency_key=plan.receipt.idempotency_key, order_ref=trial['order'],
                    amount_cents=AMOUNT, request_body=plan.receipt.request_body,
                    latency_ms=10)
                note = f'{SITUATION} | probe settled; provider {res.provider_ref}'
                db.tx(lambda cur: tasks.settle(
                    cur, task=claimed, agent_id=trial['agent_b'], receipt=plan.receipt,
                    outcome_state=AttemptState.SUCCEEDED, task_state=TaskState.SUCCEEDED,
                    response_body=res.body, provider_ref=res.provider_ref,
                    http_status=res.status, memory_content=note,
                    memory_embedding=embeddings.embed_list(note),
                    memory_outcome=Outcome.RESOLVED))
    except Exception:                                # noqa: BLE001 -- cleanup is best effort
        pass
    for a in (trial['agent_a'], trial['agent_b']):
        try:
            db.tx(lambda cur, a=a: tasks.stop_agent(cur, agent_id=a))
        except Exception:                            # noqa: BLE001
            pass


def arm_axiom(trials: int) -> dict:
    """D1 over `trials` races, then D2 once."""
    results = []
    for i in range(trials):
        trial = _setup_axiom_trial()

        # Falsifiability guard. If the memory the operator revokes is not in the recall
        # set to begin with, the predicate is vacuous and this trial proves nothing.
        pre = db.tx(lambda cur: memory.recall(
            cur, tenant_id=PROBE_TENANT, embedding=embeddings.embed_list(SITUATION),
            memory_class=MemoryClass.EPISODIC,
            context_key=ctx_state(TaskState.ACTION_PREPARED),
            retrieval_class=RetrievalClass.ACTIONABLE), readonly=True)
        recallable = str(trial['good_id']) in {str(r.id) for r in pre}

        # Alternate which side gets the head start. Odd trials let the recovery in
        # first (it should see neither the event nor the replacement); even trials let
        # the operator commit first (it must then see BOTH). Only the second kind can
        # falsify the predicate, so half the run is spent there by construction rather
        # than by luck.
        ops_first = (i % 2 == 1)
        out = _axiom_trial(
            trial,
            ops_delay_ms=0.0 if ops_first else random.uniform(2.0, 8.0),
            rec_delay_ms=random.uniform(40.0, 70.0) if ops_first else 0.0)
        rec = out.get('rec', {})
        results.append({
            'trial': i + 1, 'order': 'ops first' if ops_first else 'recovery first',
            'recallable': recallable,
            'ops_committed': out.get('ops_committed', False),
            'saw_event': rec.get('saw_event'), 'cited_revoked': rec.get('cited_revoked'),
            'action': rec.get('action'), 'recalled': rec.get('recalled'),
            'adverse': rec.get('adverse'),
            'error': out.get('rec_error') or out.get('ops_error'),
            'contradiction': bool(rec.get('saw_event')) and bool(rec.get('cited_revoked')),
        })
        _settle_trial(trial)

    # ---- D2: the operator transaction aborts mid-way. ----------------------------
    d2_mem = db.tx(lambda cur: memory.write(
        cur, tenant_id=PROBE_TENANT, memory_class=MemoryClass.EPISODIC,
        context_key=ctx_state(TaskState.ACTION_PREPARED),
        content=f'{GOOD_CONTENT} | d2 {uuid.uuid4().hex[:6]}',
        embedding=embeddings.embed_list(GOOD_CONTENT), outcome=Outcome.RESOLVED,
        source='system:execution', trust_level=Trust.FIRST_PARTY, actor='system:probe'))

    class _Died(Exception):
        """Stands in for the operator process dying between two commits -- except that
        in AXIOM there are not two commits to die between."""

    def _half_write(cur):
        memory.quarantine(cur, tenant_id=PROBE_TENANT, memory_id=d2_mem,
                          reason='probe D2', by='human:ops')
        raise _Died()                                # after the revocation, before commit

    try:
        db.tx(_half_write)
    except _Died:
        pass

    d2 = db.tx(lambda cur: {
        'row': memory.get(cur, tenant_id=PROBE_TENANT, memory_id=d2_mem),
    }, readonly=True)
    d2_intact = (d2['row'] is not None and not d2['row']['quarantined']
                 and d2['row']['retrieval_class'] == str(RetrievalClass.ACTIONABLE))

    return {'trials': results, 'd2_intact': d2_intact}


# =========================================================================== output

def _clear_ledger() -> None:
    """Remove ONLY this run's rows from the provider ledger.

    Scoped to `_ORDER_REFS` rather than calling provider.reset(), which truncates the
    whole external world -- including the chaos demo's evidence, which is the artifact
    the README screenshots. A cleanup that destroys somebody else's results is worse
    than no cleanup.
    """
    try:
        with provider.pool().connection() as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute('DELETE FROM provider_refund WHERE order_ref = ANY(%s)',
                            (_ORDER_REFS,))
                cur.execute('DELETE FROM provider_request_log WHERE order_ref = ANY(%s)',
                            (_ORDER_REFS,))
    except Exception as e:                       # noqa: BLE001 -- reported, never fatal
        print(f'  (ledger cleanup failed: {type(e).__name__}: {e} — '
              f'orders {_ORDER_REFS[:2]}… may remain)')


def main() -> int:
    ap = argparse.ArgumentParser(
        description='Model the two-system agent stack, then run the same race '
                    'through AXIOM.')
    ap.add_argument('--trials', type=int, default=4,
                    help='AXIOM races to run; alternates which side commits first '
                         '(default 4, so both orderings run twice)')
    ap.add_argument('--semantic-lag', type=float, default=0.0, metavar='SECONDS',
                    help='index-apply lag for the modelled vector store. DEFAULT 0: no '
                         'result here depends on assuming a vector store is slow.')
    ap.add_argument('--keep', action='store_true', help='keep the modelled store files')
    ap.add_argument('--keep-ledger', action='store_true',
                    help="leave this run's rows in the provider ledger for inspection. "
                         'Off by default: arm 1 creates a real duplicate and the ledger '
                         'is shared with the demo.')
    args = ap.parse_args()

    workdir = pathlib.Path(tempfile.mkdtemp(prefix='axiom-incumbent-'))
    wf = WorkflowStore(workdir / 'workflow.sqlite')
    sem = SemanticStore(workdir / 'semantic.sqlite', lag_s=args.semantic_lag)

    try:
        print('AXIOM :: incumbent probe')
        print(f'  modelled stores : {workdir}')
        print(f'  semantic lag    : {args.semantic_lag:.3f}s '
              f'{"(none assumed)" if args.semantic_lag == 0 else "(ASSUMED)"}')
        print(f'  provider        : axiom/provider.py, its own database, '
              f'Stripe idempotency semantics')
        print()

        naive = arm_naive(wf)
        correct = arm_correct(wf)

        print('=' * 78)
        print('ARM 1 + 2 :: does the two-system stack double-refund? Crash in W4.')
        print('=' * 78)
        w = 34
        print(f'{"":<26}{"SPLIT, key at call time":<{w}}{"SPLIT, key from durable id"}')
        for label, a, b in [
            ('killed after the refund', 'yes' if naive['crashed'] else 'no',
             'yes' if correct['crashed'] else 'no'),
            ('key survives the crash', 'no — it lived in RAM',
             'yes — recomputed from the store'),
            ('provider verdict on retry', 'created a second refund',
             'replayed the original' if correct['replayed'] else 'created a new refund'),
            ('REFUNDS CREATED', str(naive['refunds']), str(correct['refunds'])),
            ('DOLLARS OUT', f'${naive["cents"] / 100:,.2f}',
             f'${correct["cents"] / 100:,.2f}'),
        ]:
            print(f'{label:<26}{a:<{w}}{b}')
        print()
        print('  VERDICT: the incumbent answer WORKS. A durable workflow store plus a')
        print('  deterministic idempotency key does not double-refund, and this repo is')
        print('  not going to pretend otherwise. Temporal documents exactly this recipe.')
        print('  The disagreement is not here. It is below.')
        print()

        seam = arm_split_seam(wf, sem)
        print('=' * 78)
        print('ARM 3 :: the seam. One operator revokes one memory. Two stores, two commits.')
        print('=' * 78)
        for f in seam:
            mark = 'CONTRADICTION' if f['contradiction'] else 'consistent'
            print(f'  {f["id"]}  {f["schedule"]}')
            print(f'      decision: {f["decision"]:<10} {mark}')
            print(f'      {f["note"]}')
            print()
        split_bad = sum(1 for f in seam if f['contradiction'])

        print('=' * 78)
        print(f'ARM 4 :: the same races through tasks.recover(). {args.trials} trials.')
        print('=' * 78)
        ax = arm_axiom(args.trials)
        print(f'  {"trial":<7}{"schedule":<16}{"saw revocation":<16}'
              f'{"cited revoked":<15}{"adverse":<9}{"decision":<10}')
        for t in ax['trials']:
            print(f'  {t["trial"]:<7}{t["order"]:<16}'
                  f'{str(t["saw_event"]):<16}{str(t["cited_revoked"]):<15}'
                  f'{str(t["adverse"]):<9}{str(t["action"]):<10}{t["error"] or ""}')
        axiom_bad = sum(1 for t in ax['trials'] if t['contradiction'])
        vacuous = sum(1 for t in ax['trials'] if not t['recallable'])
        saw_yes = sum(1 for t in ax['trials'] if t['saw_event'] is True)
        saw_no = sum(1 for t in ax['trials'] if t['saw_event'] is False)
        print()
        print(f'  D1  read sets where the journal shows the revocation AND the recall')
        print(f'      still returned the revoked memory  : {axiom_bad}')
        print(f'      orderings observed                 : {saw_yes} after the revocation, '
              f'{saw_no} before it')
        print(f'  D2  operator transaction aborted mid-way; memory left intact and')
        print(f'      still ACTIONABLE                   : {ax["d2_intact"]}')
        print()

        print('=' * 78)
        ok = (naive['refunds'] == 2 and correct['refunds'] == 1 and correct['replays'] >= 1
              and split_bad >= 3 and axiom_bad == 0 and ax['d2_intact']
              and vacuous == 0 and saw_yes >= 1 and saw_no >= 1)
        if ok:
            print(f'PASS  split architecture: {split_bad}/4 schedules produced a read set')
            print( '      that corresponds to no point in time in either store.')
            print(f'      AXIOM: 0/{args.trials} — the journal and the vector index are read')
            print( '      at one timestamp, so they cannot disagree about their own write.')
            # Derived from the rows above, never asserted independently of them: a
            # summary line that can disagree with its own table is worse than no
            # summary line.
            after = {t['action'] for t in ax['trials'] if t['saw_event'] is True}
            before = {t['action'] for t in ax['trials'] if t['saw_event'] is False}
            print()
            print(f'      {saw_yes} trials ran AFTER the revocation committed: each saw the')
            print(f'      journal entry AND lost the revoked memory from the same recall, '
                  f'and decided {"/".join(sorted(after))}.')
            print(f'      {saw_no} ran before it: neither the entry nor the revocation, '
                  f'decided {"/".join(sorted(before))}.')
            print( '      Nothing in between exists. That middle state is S1 above, and it')
            print( '      is the state the split architecture reached on the first try.')
            print()
            print('      Not a probabilistic result. The revocation and its journal entry')
            print('      are ONE commit; a snapshot sees both or neither. The trials exist')
            print('      so that the claim could have failed.')
        else:
            print('INCONCLUSIVE — the run did not produce the conditions it tests.')
            print(f'  arm1 refunds={naive["refunds"]} (want 2)  '
                  f'arm2 refunds={correct["refunds"]} replays={correct["replays"]} (want 1, >=1)')
            print(f'  arm3 contradictions={split_bad}/4 (want >=3)')
            print(f'  arm4 contradictions={axiom_bad} (want 0)  '
                  f'd2_intact={ax["d2_intact"]} (want True)  '
                  f'vacuous_trials={vacuous} (want 0)')
            print(f'  arm4 orderings: {saw_yes} after / {saw_no} before (want >=1 of each)')
            if vacuous:
                print('  A vacuous trial means the revoked memory never appeared in the')
                print('  recall set, so the predicate could not have fired either way.')
            if not (saw_yes and saw_no):
                print('  Without both orderings the predicate was never exercised in the')
                print('  direction that could falsify it. That is not a pass.')
        print('=' * 78)
        return 0 if ok else 1
    finally:
        wf.close()
        sem.close()
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)
        if not args.keep_ledger:
            _clear_ledger()
        db.close_pool()
        provider.close_pool()


if __name__ == '__main__':
    raise SystemExit(main())
