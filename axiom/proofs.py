"""AXIOM :: the evidence, moved inside the product.

Everything strongest about this project was, until this module existed, a command line
away. A script that proves memory changes a recovery decision. A script that runs the
same crash against real Stripe and then asks STRIPE what happened. A script that does it
again in a workload where the risk is people rather than dollars. A judge spends about
three minutes on a URL and never runs a script, so all of that evidence was — for the
purpose of being believed — invisible.

This module is those proofs as ordinary functions. `axiom/api.py` exposes them over HTTP;
`scripts/memory_decides.py` and `scripts/stripe_proof.py` call the SAME functions and
render the same result as text. That is the entire reason the module exists rather than
the API growing its own copy: two implementations of a correctness demonstration will
drift, and the first anyone hears about it is when the browser says PASS on camera while
the terminal says INCONCLUSIVE.

Five rules this module keeps
----------------------------
1. **Every proof runs in a tenant of its own, and deletes it afterwards.**
   `_new_tenant()` mints `axiom-proof-<hex>` per run and `_wipe_tenant()` removes every
   row it created, in a `finally`. Two reasons, and the second is the one that bites:
   a judge pressing the button forty times must not leave forty quarantined memories
   degrading the demo everyone else is looking at; and two judges pressing it at the
   same second must not have their memories recalled into each other's recovery. The
   recovery path recalls by (tenant, class, context_key) and `tasks.recover` pins
   context_key to `state:ACTION_PREPARED`, so a per-run TENANT is the only isolation
   available without changing the engine — which this task does not own and should not.

2. **The proof claims its own task inside the transaction that enqueues it.**
   `tasks.claim()` is deliberately not tenant-scoped — workers are shared infrastructure
   — so between an enqueue's COMMIT and this process's claim there is a window in which
   a live demo worker can take the proof's task and run it. Doing both in one
   transaction closes that window without weakening anything: the claim is the identical
   CAS on the identical fence, over a row nobody else can see yet.

3. **Everything is bounded.** Each proof carries a `Budget` and checks it between
   phases; a proof that runs out of time returns what it has, with the phase it stopped
   in, rather than holding a serverless request open until the platform kills it.

4. **A proof that did not prove its claim says so.** `verdict` is `PASS` only when every
   assertion the proof is about actually held. INCONCLUSIVE is a first-class outcome —
   the same bar `scripts/chaos_demo.py` holds itself to — because a demo that always
   prints PASS is not evidence, it is a picture of the word PASS.

5. **Nothing here decides anything about an irreversible act.** These functions drive
   the protocol; they do not reimplement it. Every authority decision is still made
   inside `tasks.prepare()`, every recovery decision inside `tasks.recover()`, and the
   quarantine still takes effect at COMMIT inside the transaction that asked. If this
   module could reach any of that, the proofs would be proving this file rather than the
   system.
"""

from __future__ import annotations

import functools
import json
import logging
import pathlib
import time
import typing as t
import uuid

from . import db, embeddings, memory, policy as policy_mod, tasks
from .config import SYSTEM_TENANT, settings
from .models import (AttemptState, MemoryClass, Outcome, RetrievalClass, TaskState,
                     Trust, ctx_state)
from .risk import MONEY_USD_CENTS
from .seed import PRIOR_RECOVERIES

log = logging.getLogger('axiom.proofs')

# Beside the code, not in docs/ or scripts/, and that is a deployment fact rather than a
# preference: vercel.json's `excludeFiles` trims both of those out of the function bundle,
# so a measurement file living there would be present in the repository and missing in
# production — the single worst place for a number a judge is reading.
MEASUREMENTS_PATH = pathlib.Path(__file__).resolve().parent / 'measurements.json'


# ============================================================================ shared

class ProofTimeout(RuntimeError):
    """A proof ran out of its wall-clock budget. Carries the phase it stopped in."""

    def __init__(self, phase: str, budget: float):
        super().__init__(f'proof exceeded its {budget:.0f}s budget at phase {phase!r}')
        self.phase = phase


class Budget:
    """A deadline, checked between phases and never inside one.

    Never inside one on purpose: the only thing worse than a proof that runs long is a
    proof abandoned halfway through a dispatch, which is precisely the state this project
    exists to make recoverable and precisely the state a demo should not be creating on a
    shared database for fun.
    """

    def __init__(self, seconds: float):
        self.seconds = seconds
        self._deadline = time.monotonic() + seconds
        self._t0 = time.monotonic()

    @property
    def left(self) -> float:
        return self._deadline - time.monotonic()

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)

    def check(self, phase: str) -> None:
        if self.left <= 0:
            raise ProofTimeout(phase, self.seconds)


def _new_tenant(kind: str) -> tuple[uuid.UUID, str]:
    """A tenant id and slug for one proof run. The slug prefix is what the reaper scans."""
    run = uuid.uuid4().hex[:8]
    return uuid.uuid4(), f'axiom-proof-{kind}-{run}'


def _wipe_tenant(tenant_id: uuid.UUID, agent_ids: t.Sequence[uuid.UUID] = ()) -> None:
    """Delete everything one proof run created. Never raises.

    Order is dependency order, and one line of it is subtle: attempts are deleted BEFORE
    memories because `axiom_action_attempt.licensed_by_memory_id` has a foreign key into
    `axiom_memory`. The self-references on memory (supersedes / superseded_by) are
    unlinked first for the same reason `tests/conftest.py` does it — a single DELETE
    would have to satisfy both sides of a self-FK in one statement.
    """
    def _wipe(cur):
        t_ = (str(tenant_id),)
        cur.execute('DELETE FROM axiom_event WHERE tenant_id = %s', t_)
        cur.execute('UPDATE axiom_memory SET supersedes = NULL, superseded_by = NULL, '
                    'superseded_at = NULL WHERE tenant_id = %s', t_)
        cur.execute('DELETE FROM axiom_approval WHERE tenant_id = %s', t_)
        cur.execute('DELETE FROM axiom_action_attempt WHERE tenant_id = %s', t_)
        cur.execute('DELETE FROM axiom_memory WHERE tenant_id = %s', t_)
        cur.execute('DELETE FROM axiom_task WHERE tenant_id = %s', t_)
        cur.execute('DELETE FROM axiom_mission WHERE tenant_id = %s', t_)
        cur.execute('DELETE FROM axiom_policy WHERE tenant_id = %s', t_)
        cur.execute('DELETE FROM axiom_tenant WHERE id = %s', t_)
        if agent_ids:
            ids = [str(a) for a in agent_ids]
            cur.execute('DELETE FROM axiom_event WHERE tenant_id = %s AND '
                        'subject_id = ANY(%s::UUID[])', (str(SYSTEM_TENANT), ids))
            cur.execute('DELETE FROM axiom_agent WHERE id = ANY(%s::UUID[])', (ids,))

    try:
        db.tx(_wipe)
    except Exception as e:                       # noqa: BLE001 — cleanup never masks a result
        log.warning('proof cleanup failed for tenant %s: %s: %s',
                    tenant_id, type(e).__name__, e)


def reap_stale_tenants(max_age_minutes: int = 30, limit: int = 3) -> int:
    """Delete proof tenants a previous run failed to clean up. Bounded, best effort.

    The `finally` in each proof is the primary mechanism and it covers every failure this
    process can observe. It does not cover the failure it cannot: a serverless instance
    frozen or killed mid-proof, which leaves a tenant with a handful of rows and no owner.
    Over four weeks of unattended judging that is the difference between a database that
    stays the size of the demo and one that quietly does not.

    `limit` is small because this runs on the request path. Older orphans are collected by
    later requests; nothing here needs to catch up in one pass.
    """
    def _find(cur):
        cur.execute("""
            SELECT id FROM axiom_tenant
            WHERE slug LIKE 'axiom-proof-%%' AND created_at < now() - %s::INTERVAL
            LIMIT %s
        """, (f'{int(max_age_minutes)} minutes', int(limit)))
        return [r['id'] for r in cur.fetchall()]

    try:
        stale = db.tx(_find, readonly=True)
    except Exception as e:                       # noqa: BLE001
        log.warning('proof reaper could not list stale tenants: %s: %s',
                    type(e).__name__, e)
        return 0
    for tid in stale:
        _wipe_tenant(tid)
    if stale:
        log.info('proof reaper removed %d abandoned proof tenant(s)', len(stale))
    return len(stale)


def _enqueue_and_claim(*, tenant_id: uuid.UUID, mission_id: uuid.UUID,
                       agent_id: uuid.UUID, task_type: str, dedupe_key: str,
                       payload: dict) -> tasks.Claimed:
    """Enqueue one task and claim it in the SAME transaction. See rule 2 up top."""
    def _apply(cur):
        task_id = tasks.enqueue(cur, tenant_id=tenant_id, mission_id=mission_id,
                                task_type=task_type, dedupe_key=dedupe_key,
                                payload=payload, actor='system:proof')
        if task_id is None:                      # unreachable: the tenant is one run old
            raise RuntimeError(f'dedupe_key {dedupe_key} already exists in a fresh tenant')
        claimed = tasks.claim(cur, agent_id=agent_id, task_id=task_id)
        if claimed is None:
            raise RuntimeError(f'could not claim {dedupe_key} in the transaction that '
                               f'created it')
        return claimed
    return db.tx(_apply)


def _take_over(*, task_id: uuid.UUID, agent_id: uuid.UUID) -> tasks.Claimed:
    """Expire the dead worker's lease and claim the orphaned task, in one transaction.

    A real successor waits out the lease; `scripts/stripe_proof.py` does the same UPDATE
    for the same reason. Twenty seconds of a judge's three minutes spent watching nothing
    is not a demonstration of anything, and the lease is explicitly a LIVENESS
    optimization — the fencing token is what makes the takeover safe, and the claim below
    bumps it exactly as a takeover after a genuine crash would.
    """
    def _apply(cur):
        cur.execute("UPDATE axiom_task SET available_at = now() - INTERVAL '1 second' "
                    'WHERE id = %s', (str(task_id),))
        claimed = tasks.claim(cur, agent_id=agent_id, task_id=task_id)
        if claimed is None:
            raise RuntimeError(f'task {task_id} could not be re-claimed after the crash')
        return claimed
    return db.tx(_apply)


def _recall_plan(tenant_id: uuid.UUID, *, context_key: str | None) -> tuple[str, bool | None]:
    """The live EXPLAIN of the recall statement that just ran, and whether it used the index.

    Not decoration, and not cached: identical rows come back when the plan degrades to a
    full primary-key scan, so nothing in the RESULT of a recall could ever reveal the
    regression. Only the plan does. `None` means "we could not check", which is a
    different claim from "it degraded" and must not be rendered as the same thing.
    """
    literal = db.vector_literal(embeddings.embed_list(SITUATION))
    params: dict[str, t.Any] = {
        'vec': literal, 'tenant': str(tenant_id), 'cls': str(MemoryClass.EPISODIC),
        'rc': str(RetrievalClass.ACTIONABLE),
        'fetch': settings.recall_k * settings.recall_overfetch,
    }
    if context_key is not None:
        params['ck'] = context_key
    sql = memory.recall_sql_for_explain(context_key=context_key is not None)
    try:
        plan = db.tx(lambda cur: db.explain(cur, sql, params), readonly=True)
    except Exception as e:                       # noqa: BLE001 — a failed EXPLAIN is not a verdict
        return f'EXPLAIN unavailable: {type(e).__name__}: {e}'[:300], None
    return plan, db.uses_vector_index(plan)


def _recalled_json(recalled: t.Sequence[memory.Recalled],
                   planted: t.Container[uuid.UUID] = ()) -> list[dict]:
    """Memories as the UI needs them: what they say, how close they were, and whether
    they are the kind of memory the recovery path counts as adverse.

    `adverse` is not a display flag invented here — it is exactly the predicate
    `tasks.recover()` aggregates over (DUPLICATE_EFFECT or HUMAN_REQUIRED). Showing it
    beside the similarity is what lets a viewer check the arithmetic of the decision
    instead of taking the rationale sentence on faith.
    """
    out = []
    for r in recalled:
        out.append({
            'id': str(r.id),
            'content': r.content,
            'outcome': r.outcome,
            'similarity': round(r.similarity, 4),
            'distance': round(r.distance, 4),
            'trust_level': r.trust_level,
            'source': r.source,
            'adverse': r.outcome in (str(Outcome.DUPLICATE_EFFECT),
                                     str(Outcome.HUMAN_REQUIRED)),
            'planted_by_this_proof': r.id in planted,
        })
    return out


# ================================================================ PROOF 1: memory

# The situation the crashed task is in, phrased the way the seeded corpus is phrased.
# Similarity is the whole mechanism: a memory that does not come back at high rank cannot
# change a decision, and an adverse memory about an unrelated situation SHOULD NOT.
SITUATION = 'duplicate_charge: duplicate charge on customer card'

ADVERSE = (
    'agent died mid-refund on a duplicate_charge task; the recovering worker re-planned '
    'from the transcript instead of the receipt and the customer was refunded twice',
    'agent died mid-refund on a duplicate_charge task; a second refund reached the '
    'provider before the first was recorded; duplicate effect confirmed on the ledger',
)

MEMORY_PROOF_CENTS = 15_000        # $150 — under the ceiling, so the proof is about memory
PROOF_CEILING_CENTS = 20_000       # the demo policy's ceiling, kept identical on purpose


@functools.lru_cache(maxsize=1)
def _prior_vectors() -> tuple[tuple[str, Outcome, tuple[float, ...]], ...]:
    """The seeded corpus, embedded once per process.

    Deliberately `axiom.seed.PRIOR_RECOVERIES` rather than a private copy: step 1 of this
    proof is "the decision the live demo would make", and it is only that if the memories
    it recalls are the memories the live demo has. `embeddings.embed` is itself LRU-cached,
    so a warm instance re-runs this proof with zero embedding calls.
    """
    return tuple((c, o, embeddings.embed(c)) for c, o in PRIOR_RECOVERIES)


def _refund_world(cur, *, tenant_id: uuid.UUID, slug: str, title: str, goal: str,
                  budget_cents: int) -> uuid.UUID:
    """Tenant, refund policy, mission and the prior-recovery corpus — in ONE transaction.

    The policy is a copy of the demo's, ceiling and all, so neither proof can be accused
    of having been given an authority model written to make it pass. The corpus is the
    demo's too: a recovery that recalls nothing is indistinguishable from a recovery with
    no memory at all, and both proofs are partly about what the recovery recalled.
    """
    cur.execute("""INSERT INTO axiom_tenant (id, slug, display_name)
                   VALUES (%s, %s, 'AXIOM proof run')""", (str(tenant_id), slug))
    policy_mod.publish(
        cur, tenant_id=tenant_id, policy_id='refund_authority', version=1,
        body={'description': 'Autonomous refund authority for order exceptions',
              'max_auto_action_cents': PROOF_CEILING_CENTS,
              'rationale': 'A refund above $200 is a business decision, not an '
                           'operational one, and gets a human. Identical to the demo '
                           'policy, so the proof is not about a policy written to pass.'},
        max_auto_action_cents=PROOF_CEILING_CENTS, requires_approval=False,
        created_by='system:proof', activate=True)
    mission_id = tasks.create_mission(
        cur, tenant_id=tenant_id, title=title, goal=goal, budget_cents=budget_cents,
        created_by='system:proof')
    for content, outcome, v in _prior_vectors():
        memory.write(cur, tenant_id=tenant_id, memory_class=MemoryClass.EPISODIC,
                     context_key=ctx_state(TaskState.ACTION_PREPARED), content=content,
                     embedding=v, outcome=outcome, source='system:execution',
                     trust_level=Trust.FIRST_PARTY, mission_id=mission_id,
                     actor='system:proof')
    return mission_id


def memory_decides(*, budget_seconds: float = 30.0, keep: bool = False) -> dict:
    """Run the SAME recovery three times against the SAME crashed task, changing only memory.

        1  the corpus as seeded                                 -> RESEND
        2  + two DUPLICATE_EFFECT memories at high similarity   -> ESCALATE
        3  those two quarantined, inside ONE transaction        -> RESEND

    Nothing else moves between the three. Not the task, not the receipt, not the
    idempotency key, not the fence, not the policy, not the amount. The only variable is
    what is in memory, and the decision moves with it in BOTH directions — which is the
    claim that separates a system whose memory decides something from one that retrieves
    memories, prints them, and then ignores them. The chaos demo's own output cannot tell
    those two apart; this can.

    Step 3 is the one to stare at. `quarantined` feeds the computed `retrieval_class`,
    which is a VECTOR INDEX PREFIX column, so a plain UPDATE physically moves those rows
    to a different partition of the index inside the transaction — and the recall that
    follows, in that same transaction, does not see them. There is no reindex, no cache to
    invalidate, and no window in which a memory known to be poisoned is still steering an
    irreversible act.

    `keep=True` leaves the run's tenant in place for inspection. The default deletes it.
    """
    b = Budget(budget_seconds)
    tenant_id, slug = _new_tenant('mem')
    run = slug.rsplit('-', 1)[-1]
    order = f'MEM-{run}'
    agents: list[uuid.UUID] = []
    steps: list[dict] = []
    quarantined = 0
    plan_text, plan_used = '', None
    keys: set[str] = set()
    error: str | None = None

    try:
        # Embeddings BEFORE any transaction: db.tx() re-executes its callable on a 40001,
        # and embedding inside it would re-hit Bedrock on every retry.
        vec = embeddings.embed_list(SITUATION)
        adverse = [(txt, embeddings.embed(txt)) for txt in ADVERSE]
        b.check('embed')

        mission_id = db.tx(lambda cur: _refund_world(
            cur, tenant_id=tenant_id, slug=slug, title='Does memory decide?',
            goal='same crash, same task, different memory', budget_cents=100_000))
        agents.append(db.tx(lambda cur: tasks.register_agent(
            cur, worker_ref=f'proof-mem-{run}')))
        b.check('seed')

        claimed = _enqueue_and_claim(
            tenant_id=tenant_id, mission_id=mission_id, agent_id=agents[0],
            task_type='refund', dedupe_key=f'order:{order}:refund',
            payload={'order_ref': order, 'amount_cents': MEMORY_PROOF_CENTS,
                     'description': 'duplicate charge on customer card',
                     'exception_kind': 'duplicate_charge'})
        prepared = db.tx(lambda cur: tasks.prepare(
            cur, task=claimed, agent_id=agents[0], step_name='refund',
            provider_name='payments', operation='refunds.create',
            request_body={'order_ref': order, 'amount_cents': MEMORY_PROOF_CENTS,
                          'currency': 'USD'},
            amount_cents=MEMORY_PROOF_CENTS))
        # No provider call and no settle. The task now sits in ACTION_PREPARED with a live
        # receipt: crash window W4, held still so it can be interrogated three times.
        receipt = prepared.receipt
        b.check('prepare')

        def _recover(cur):
            return tasks.recover(cur, task=claimed, agent_id=agents[0],
                                 situation_embedding=vec, step_name='refund')

        # ------------------------------------------------------------- 1. as it stands
        p1 = db.tx(_recover)
        steps.append(_memory_step(1, 'memory as seeded', p1))
        keys.add(p1.receipt.idempotency_key if p1.receipt else '')
        b.check('recover-1')

        # ------------------------------------ 2. two adverse memories at high similarity
        def _plant(cur):
            return [memory.write(
                cur, tenant_id=tenant_id, memory_class=MemoryClass.EPISODIC,
                context_key=ctx_state(TaskState.ACTION_PREPARED), content=txt,
                embedding=v, outcome=Outcome.DUPLICATE_EFFECT,
                source='system:execution', trust_level=Trust.FIRST_PARTY,
                mission_id=mission_id, actor='system:proof') for txt, v in adverse]

        planted = set(db.tx(_plant))
        p2 = db.tx(_recover)
        steps.append(_memory_step(2, 'plus two memories of a DUPLICATE EFFECT', p2, planted))
        keys.add(p2.receipt.idempotency_key if p2.receipt else '')
        b.check('recover-2')

        # ------------------------- 3. quarantine them, IN ONE TRANSACTION, and re-ask
        def _quarantine_and_reask(cur):
            """Both the quarantine AND the recovery, in a single transaction.

            Not a shortcut — the assertion. The recall inside `tasks.recover()` below runs
            in the same transaction that just moved those rows to a different partition of
            the vector index, and it does not see them. There is no interval to lose a race
            in.

            Quarantining BY ID rather than "everything adverse that came back" is the one
            deliberate difference from a hand-run script: this endpoint is public, and a
            proof that quarantines whatever it happens to recall would erode its own corpus
            a little on every press.
            """
            n = 0
            for mem_id in planted:
                memory.quarantine(cur, tenant_id=tenant_id, memory_id=mem_id,
                                  reason='proof: shown to be a mis-attributed outcome',
                                  by='system:proof')
                n += 1
            return n, tasks.recover(cur, task=claimed, agent_id=agents[0],
                                    situation_embedding=vec, step_name='refund')

        quarantined, p3 = db.tx(_quarantine_and_reask)
        steps.append(_memory_step(
            3, f'those {quarantined} quarantined, SAME transaction', p3, planted))
        keys.add(p3.receipt.idempotency_key if p3.receipt else '')
        b.check('recover-3')

        # ------------------------------------------------------------- the query plan
        plan_text, plan_used = _recall_plan(
            tenant_id, context_key=ctx_state(TaskState.ACTION_PREPARED))

    except ProofTimeout as e:
        error = str(e)
    except Exception as e:                       # noqa: BLE001 — a proof reports, never 500s
        log.exception('memory proof failed')
        error = f'{type(e).__name__}: {e}'[:300]
    finally:
        if not keep:
            _wipe_tenant(tenant_id, agents)

    actions = [s['action'] for s in steps]
    verdict = 'PASS' if (actions == ['RESEND', 'ESCALATE', 'RESEND']
                         and plan_used is True) else 'INCONCLUSIVE'
    out = {
        'steps': steps,
        'plan_uses_vector_index': plan_used,
        'plan': plan_text,
        'quarantined': quarantined,
        'verdict': verdict,
        # The three recoveries ran against ONE receipt. If this is ever false the proof is
        # not the proof it says it is — a different key means a different act.
        'idempotency_key': next(iter(keys - {''}), None),
        'key_unchanged': len(keys - {''}) <= 1,
        'order_ref': order,
        'tenant_id': str(tenant_id),
        'cleaned_up': not keep,
        'elapsed_ms': b.elapsed_ms,
        'expected': ['RESEND', 'ESCALATE', 'RESEND'],
    }
    if error:
        out['error'] = error
    return out


def _memory_step(n: int, label: str, plan: tasks.RecoveryPlan,
                 planted: t.Container[uuid.UUID] = ()) -> dict:
    return {'n': n, 'label': label, 'action': plan.action, 'rationale': plan.rationale,
            'recalled': _recalled_json(plan.recalled, planted),
            'adverse_recalled': sum(1 for r in plan.recalled
                                    if r.outcome in (str(Outcome.DUPLICATE_EFFECT),
                                                     str(Outcome.HUMAN_REQUIRED))),
            'recalled_count': len(plan.recalled)}


# ================================================================ PROOF 2: Stripe

STRIPE_PROOF_CENTS = 30_000        # $300.00 — the number the whole project opens with


def stripe_available() -> bool:
    from . import stripe_provider
    return stripe_provider.available()


def stripe_proof(*, amount_cents: int = STRIPE_PROOF_CENTS,
                 budget_seconds: float = 90.0, keep: bool = False) -> dict:
    """The same crash — window W4 — against Stripe's real API, in test mode.

    Every other demonstration in this repository proves the guarantee against a payment
    provider this repository also wrote. That is a fair way to build the thing and an
    unconvincing way to finish arguing about it. So this creates a real test charge, lets
    AXIOM mint the receipt, sends the refund, CRASHES before recording it, recovers under
    the same key, and then asks STRIPE what happened rather than asking AXIOM.

    The answer that matters is not "one refund". It is "one refund, and Stripe reported
    the second request as a replay" — the `idempotent-replayed: true` header. Zero
    duplicates with zero replays would only mean nothing was ever retried.

    What AXIOM adds, given that Stripe already refuses to double-charge a repeated key:
    Stripe can only honour a key it is HANDED, and an agent that regenerates its key after
    a crash gets a second refund from a provider that was willing to prevent one. The key
    surviving the crash is AXIOM's contribution, and it is the only part under test here.

    Returns `{'available': False, 'reason': ...}` when no test key is configured. That is
    a 200 and not an error: the deployment either has a sandbox key or it does not, and a
    missing credential is not a failed proof.
    """
    from . import stripe_provider
    from .provider import ProviderCrash

    if not stripe_provider.available():
        return {
            'available': False,
            'reason': 'AXIOM_STRIPE_KEY is not set on this deployment, so no real charge '
                      'can be created. The recorded result of this proof is in the repo '
                      '(scripts/stripe_proof.py).',
            'steps': [], 'charge_id': None, 'refund_id': None, 'replayed': False,
            'refunds_for_order': 0, 'duplicates': 0, 'dashboard_url': None,
            'verdict': 'INCONCLUSIVE',
        }

    b = Budget(budget_seconds)
    tenant_id, slug = _new_tenant('stripe')
    run = slug.rsplit('-', 1)[-1]
    order = f'AXM-PROOF-{run}'
    agents: list[uuid.UUID] = []
    steps: list[dict] = []
    charge = refund_id = None
    replayed = False
    mine: list[dict] = []
    dupes: list[dict] = []
    crashed = False
    error: str | None = None

    def step(n: int, label: str, detail: str) -> None:
        steps.append({'n': n, 'label': label, 'detail': detail})

    try:
        situation = SITUATION
        vec = embeddings.embed_list(situation)

        # ------------------------------------------------------------- 1. a real charge
        charge = stripe_provider.create_test_charge(amount_cents, order)
        step(1, 'a real Stripe charge exists',
             f'{charge} · ${amount_cents / 100:,.2f} · test mode')
        b.check('charge')

        mission_id = db.tx(lambda cur: _refund_world(
            cur, tenant_id=tenant_id, slug=slug, title='Stripe proof',
            goal='one refund, one crash, one real provider',
            budget_cents=amount_cents * 4))
        agent_a = db.tx(lambda cur: tasks.register_agent(cur, worker_ref=f'proof-stripe-a-{run}'))
        agent_b = db.tx(lambda cur: tasks.register_agent(cur, worker_ref=f'proof-stripe-b-{run}'))
        agents += [agent_a, agent_b]

        request_body = {'order_ref': order, 'amount_cents': amount_cents,
                        'currency': 'usd', 'charge_id': charge}
        claimed = _enqueue_and_claim(
            tenant_id=tenant_id, mission_id=mission_id, agent_id=agent_a,
            task_type='refund', dedupe_key=f'order:{order}:refund',
            payload={'order_ref': order, 'amount_cents': amount_cents,
                     'charge_id': charge,
                     'description': 'duplicate charge on customer card',
                     'exception_kind': 'duplicate_charge'})

        def _prepare(task, agent):
            return db.tx(lambda cur: tasks.prepare(
                cur, task=task, agent_id=agent, step_name='refund',
                provider_name='stripe', operation='refunds.create',
                request_body=request_body, amount_cents=amount_cents))

        prepared = _prepare(claimed, agent_a)
        if prepared.parked:
            # $300 exceeds the policy's unattended ceiling, which is the policy working.
            # The approval is a single-use capability, burned by the next prepare().
            db.tx(lambda cur: tasks.decide_approval(
                cur, tenant_id=tenant_id, approval_id=prepared.approval_id, approved=True,
                decided_by='ops@axiom.demo', note='stripe proof'))
            claimed = _take_over(task_id=claimed.id, agent_id=agent_a)
            prepared = _prepare(claimed, agent_a)
            step(2, 'policy stopped it, a human approved',
                 f'${amount_cents / 100:,.2f} exceeds the ${PROOF_CEILING_CENTS / 100:,.0f} '
                 f'unattended ceiling; the approval token was consumed by PREPARE')
        receipt = prepared.receipt
        step(3, 'receipt committed BEFORE the call',
             f'{receipt.idempotency_key} — generated in the database from immutable inputs')
        b.check('prepare')

        # ------------------------------------- 4. the refund lands, then worker A dies
        try:
            stripe_provider.create_refund(
                idempotency_key=receipt.idempotency_key, order_ref=order,
                amount_cents=amount_cents, request_body=request_body,
                charge_id=charge, chaos_post=1.0)
        except ProviderCrash:
            crashed = True
        step(4, 'refund sent, worker A KILLED before recording it',
             'crash window W4: the money has moved in Stripe and AXIOM does not know'
             if crashed else 'NO CRASH FIRED — this run proves nothing')
        b.check('dispatch')

        # ------------------------------------------------- 5. worker B takes it over
        recovered = _take_over(task_id=claimed.id, agent_id=agent_b)
        plan = db.tx(lambda cur: tasks.recover(
            cur, task=recovered, agent_id=agent_b, situation_embedding=vec,
            step_name='refund'))
        step(5, 'worker B recovered from the RECEIPT, not the transcript',
             f'fence e{claimed.lease_epoch} -> e{recovered.lease_epoch} · {plan.action} · '
             f'{len(plan.recalled)} comparable recoveries recalled')
        b.check('recover')

        result = stripe_provider.create_refund(
            idempotency_key=plan.receipt.idempotency_key, order_ref=order,
            amount_cents=amount_cents, request_body=plan.receipt.request_body,
            charge_id=charge)
        refund_id, replayed = result.provider_ref, result.replayed
        step(6, 're-sent under the SAME key',
             f'{result.provider_ref} — {"REPLAYED by Stripe" if replayed else "CREATED (!!)"}')

        db.tx(lambda cur: tasks.settle(
            cur, task=recovered, agent_id=agent_b, receipt=plan.receipt,
            outcome_state=AttemptState.SUCCEEDED, task_state=TaskState.SUCCEEDED,
            response_body=result.body, provider_ref=result.provider_ref,
            http_status=result.status,
            memory_content=f'{situation} | stripe replayed {result.provider_ref}',
            memory_embedding=embeddings.embed_list(
                f'{situation} | stripe replayed {result.provider_ref}'),
            memory_outcome=Outcome.RESOLVED))
        b.check('settle')

        # ------------------------------------------------- 7. ask STRIPE, not ourselves
        time.sleep(1.2)              # Stripe's list API is read-after-write eventual
        ledger = stripe_provider.ledger(100)
        mine = [r for r in ledger if r['order_ref'] == order]
        # duplicate_check() applies exactly this rule; it is inlined against the ledger
        # we already fetched rather than fetching all 100 refunds a second time. A replayed
        # request returns the SAME refund id, so two distinct ids for one order is the only
        # thing that can mean a genuine double refund.
        dupes = [{'order_ref': order, 'refund_count': len(mine),
                  'refund_ids': [r['provider_ref'] for r in mine]}] if len(mine) > 1 else []
        step(7, "STRIPE's own ledger, read back from the API",
             f'{len(mine)} refund(s) for {order} · {len(dupes)} duplicate(s)')

    except ProofTimeout as e:
        error = str(e)
    except Exception as e:                       # noqa: BLE001
        log.exception('stripe proof failed')
        error = f'{type(e).__name__}: {e}'[:300]
    finally:
        if not keep:
            _wipe_tenant(tenant_id, agents)

    ok = bool(crashed and len(mine) == 1 and replayed and not dupes and not error)
    out = {
        'available': True,
        'steps': steps,
        'charge_id': charge,
        'refund_id': refund_id,
        'replayed': replayed,
        'refunds_for_order': len(mine),
        'duplicates': len(dupes),
        'crashed': crashed,
        'amount_cents': amount_cents,
        'order_ref': order,
        # Stripe's own interface, so the claim can be checked in the other party's UI
        # rather than in ours. Requires access to the sandbox account, which is stated
        # rather than implied.
        'dashboard_url': f'https://dashboard.stripe.com/test/payments/{charge}' if charge else None,
        'ledger': mine,
        'verdict': 'PASS' if ok else 'INCONCLUSIVE',
        'elapsed_ms': b.elapsed_ms,
    }
    if error:
        out['error'] = error
    return out


# ============================================================= PROOF 3: broadcast

# Small on purpose: this runs inside an HTTP request, and the relay writes one row per
# recipient. Three campaigns and ~1,600 deliveries is enough for the audit query to mean
# something and small enough that a month of judging does not fill a free-tier database.
BROADCAST_CAMPAIGNS: tuple[tuple[str, str, str, int, int], ...] = (
    ('order confirmation digest resend for yesterday checkout errors',
     'transactional_notice', 'checkout_errors', 420, 0),
    ('spring launch announcement to the active-buyer segment',
     'promotional_blast', 'active_buyers', 1_020, 120),
    ('service incident status update for the affected region',
     'service_incident', 'eu_west_customers', 260, 0),
)

# IN RECIPIENTS. A campaign that would reach more people than this stops and waits for a
# human no matter how little it costs to send — the same number expressed in dollars
# (about five cents of SES) would clear any money-shaped policy in the system without
# anyone being asked.
BROADCAST_CEILING_RECIPIENTS = 2_000
BROADCAST_BUDGET_RECIPIENTS = 30_000


def broadcast_proof(*, budget_seconds: float = 60.0, keep: bool = False) -> dict:
    """The same crash, in a workload where the risk axis is PEOPLE.

    Three campaigns go out through a message relay that is a genuinely separate database
    AXIOM cannot enlist in its transactions. The second one crashes at W4 — after every
    message has left and before AXIOM records it — and is recovered under the same key.

    Then the relay's own books are audited, and the query that matters is not "how many
    sends" but `GROUP BY campaign_ref, recipient HAVING count(*) > 1`: one row per human
    being who received the same campaign twice. `relay_delivery` deliberately has NO
    unique constraint on (campaign, recipient), because a real ESP has none either — if
    the relay refused duplicates it would be doing AXIOM's job and this would prove
    nothing.

    `replays` above zero is as load-bearing as `messaged_twice` being zero: a run where no
    crash landed in the dangerous window demonstrated nothing, and says so.
    """
    from .domains import broadcast
    from .domains import relay
    from .provider import ProviderCrash
    from .risk import COMMS_RECIPIENTS, Grant, Reversibility

    d = broadcast.DOMAIN
    b = Budget(budget_seconds)
    tenant_id, slug = _new_tenant('bcast')
    run = slug.rsplit('-', 1)[-1]
    agents: list[uuid.UUID] = []
    steps: list[dict] = []
    refs: list[str] = []
    crashed_ref: str | None = None
    error: str | None = None
    report = None

    try:
        try:
            relay.ensure_schema()
        except Exception as e:                   # noqa: BLE001 — an absent relay is a fact
            return {
                'available': False,
                'reason': f'the message relay is not reachable on this deployment '
                          f'({type(e).__name__}: {e})'[:300],
                'campaigns': 0, 'recipients': 0, 'replays': 0, 'messaged_twice': 0,
                'risk_unit': d.risk.risk_unit, 'steps': [], 'verdict': 'INCONCLUSIVE',
            }
        b.check('relay')

        def _world(cur):
            cur.execute("""INSERT INTO axiom_tenant (id, slug, display_name)
                           VALUES (%s, %s, 'AXIOM proof run')""", (str(tenant_id), slug))
            policy_mod.publish(
                cur, tenant_id=tenant_id, policy_id=d.policy_id, version=1,
                body={'description': 'Autonomous outbound messaging authority',
                      'risk_axis': 'recipients',
                      'max_auto_action_recipients': BROADCAST_CEILING_RECIPIENTS,
                      'rationale': 'A send that reaches more than 2,000 people is a '
                                   'reputational decision, not an operational one.'},
                # The authority clause that means what it says: up to 2,000 PEOPLE, even
                # though the act can never be undone. tasks.prepare() is handed a Risk by
                # the code below, so this grant — not the money column — is what decides.
                risk_grants=[Grant(COMMS_RECIPIENTS, BROADCAST_CEILING_RECIPIENTS,
                                   Reversibility.IRREVERSIBLE)],
                max_auto_action_cents=BROADCAST_CEILING_RECIPIENTS,
                requires_approval=False, created_by='system:proof', activate=True)
            return tasks.create_mission(
                cur, tenant_id=tenant_id, title="Send today's outbound campaigns",
                goal='deliver three campaigns without messaging anyone twice',
                # A RECIPIENT budget in the money column: the hard cap on how many human
                # beings this mission may touch, whatever the agent decides.
                budget_cents=BROADCAST_BUDGET_RECIPIENTS, created_by='system:proof')

        mission_id = db.tx(_world)
        agent_a = db.tx(lambda cur: tasks.register_agent(cur, worker_ref=f'proof-bcast-a-{run}'))
        agent_b = db.tx(lambda cur: tasks.register_agent(cur, worker_ref=f'proof-bcast-b-{run}'))
        agents += [agent_a, agent_b]
        b.check('seed')

        for i, (desc, kind, segment, audience, suppressed) in enumerate(BROADCAST_CAMPAIGNS):
            ref = f'PRF-{run}-{i + 1}'
            refs.append(ref)
            payload = {'campaign_ref': ref, 'description': desc, 'campaign_kind': kind,
                       'segment': segment, 'recipient_count': audience,
                       'suppressed_count': suppressed}
            claimed = _enqueue_and_claim(
                tenant_id=tenant_id, mission_id=mission_id, agent_id=agent_a,
                task_type=d.task_type, dedupe_key=f'campaign:{ref}:broadcast',
                payload=payload)

            # The model PROPOSES. Outside any transaction — it is a network call, and
            # db.tx() may re-execute its callable on a 40001.
            intent = d.triage(payload)
            if not intent.acts:
                steps.append({'n': i + 1, 'campaign_ref': ref, 'label': 'held by triage',
                              'detail': f'{intent.action}: {intent.reason}',
                              'recipients': 0, 'replayed': False})
                continue

            situation = d.situation(payload, intent)
            request_body = d.request_body(payload, intent)
            prepared = db.tx(lambda cur, c=claimed, rb=request_body, it=intent: tasks.prepare(
                cur, task=c, agent_id=agent_a, step_name=d.step_name,
                provider_name=d.provider_name, operation=d.operation, request_body=rb,
                # The authority question, asked in the domain's own units:
                # comms.recipients=1,020, IRREVERSIBLE — not the integer smuggled through
                # a column named for money.
                risk=d.risk.descriptor(it.risk_units, it.reason),
                amount_cents=it.risk_units, currency=d.risk.code, policy_id=d.policy_id))
            if prepared.parked:
                steps.append({'n': i + 1, 'campaign_ref': ref,
                              'label': 'parked on a human',
                              'detail': f'{intent.risk_units:,} recipients exceeds the '
                                        f'{BROADCAST_CEILING_RECIPIENTS:,}-recipient ceiling',
                              'recipients': 0, 'replayed': False})
                continue

            receipt = prepared.receipt
            crash_here = (i == 1)          # exactly one crash, at the worst instant
            first_try = True
            if crash_here:
                try:
                    d.dispatch(idempotency_key=receipt.idempotency_key,
                               request_body=receipt.request_body,
                               risk_units=intent.risk_units, chaos_post=1.0)
                except ProviderCrash:
                    crashed_ref = ref
                    first_try = False
                claimed = _take_over(task_id=claimed.id, agent_id=agent_b)
                plan = db.tx(lambda cur, c=claimed: tasks.recover(
                    cur, task=c, agent_id=agent_b,
                    situation_embedding=embeddings.embed_list(d.recovery_situation(payload)),
                    step_name=d.step_name))
                receipt = plan.receipt or receipt
                actor, recovery_action = agent_b, plan.action
            else:
                actor, recovery_action = agent_a, None

            effect = d.dispatch(idempotency_key=receipt.idempotency_key,
                                request_body=receipt.request_body,
                                risk_units=intent.risk_units)
            content = d.settled_memory(situation=situation,
                                       idempotency_key=receipt.idempotency_key,
                                       risk_units=intent.risk_units, effect=effect,
                                       first_try=first_try)
            db.tx(lambda cur, c=claimed, r=receipt, e=effect, txt=content: tasks.settle(
                cur, task=c, agent_id=actor, receipt=r,
                outcome_state=AttemptState.SUCCEEDED, task_state=TaskState.SUCCEEDED,
                response_body=e.body, provider_ref=e.ref, http_status=e.status,
                memory_content=txt, memory_embedding=embeddings.embed_list(txt),
                memory_outcome=Outcome.RESOLVED,
                result={'provider_ref': e.ref, 'replayed': e.replayed}))

            steps.append({
                'n': i + 1, 'campaign_ref': ref,
                'label': 'crashed at W4, recovered, re-sent under the same key'
                         if crash_here else 'delivered',
                'detail': f'{intent.risk_units:,} recipients · {effect.ref} · '
                          f'{"REPLAYED by the relay" if effect.replayed else "delivered"}'
                          + (f' · recovery said {recovery_action}' if recovery_action else ''),
                'recipients': intent.risk_units, 'replayed': effect.replayed,
                'idempotency_key': receipt.idempotency_key,
            })
            b.check(f'campaign-{i + 1}')

        # --------------------------------------------------- the relay's own books
        report = d.audit(refs)
        _reap_relay(relay, keep_refs=refs)

    except ProofTimeout as e:
        error = str(e)
    except Exception as e:                       # noqa: BLE001
        log.exception('broadcast proof failed')
        error = f'{type(e).__name__}: {e}'[:300]
    finally:
        if not keep:
            _wipe_tenant(tenant_id, agents)

    sent = [s for s in steps if s.get('recipients')]
    ok = bool(report and report.replays >= 1 and not report.duplicates
              and len(sent) == len(BROADCAST_CAMPAIGNS) and not error)
    out = {
        'campaigns': len(sent),
        'recipients': int(report.risk_units) if report else 0,
        'replays': int(report.replays) if report else 0,
        'messaged_twice': len(report.duplicates) if report else 0,
        'risk_unit': d.risk.risk_unit,
        'noun': d.risk.noun,
        'ceiling': BROADCAST_CEILING_RECIPIENTS,
        'reversibility': str(d.risk.reversibility),
        'crashed_campaign': crashed_ref,
        'campaign_refs': refs,
        'verdicts': report.verdicts if report else {},
        'duplicate_label': report.duplicate_label if report else '',
        'steps': steps,
        'verdict': 'PASS' if ok else 'INCONCLUSIVE',
        'elapsed_ms': b.elapsed_ms,
        'available': True,
    }
    if error:
        out['error'] = error
    return out


def _reap_relay(relay, *, keep_refs: t.Sequence[str]) -> None:
    """Delete EARLIER proof runs' rows from the relay. Never raises.

    The relay is an append-only stand-in for an external gateway, and one row per
    recipient is what makes its duplicate query meaningful — which is also what makes it
    grow. This run's deliveries are kept so the ledger can be inspected right after the
    press; every previous run's are collected. Housekeeping of our own litter in a system
    we also wrote, done AFTER the audit that read it, so nothing an assertion depends on
    is ever deleted before it is counted.
    """
    try:
        with relay.pool().connection() as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                for table in ('relay_delivery', 'relay_send'):
                    cur.execute(f"DELETE FROM {table} WHERE campaign_ref LIKE 'PRF-%%' "
                                'AND campaign_ref <> ALL(%s::STRING[])', (list(keep_refs),))
                cur.execute("DELETE FROM relay_request_log WHERE campaign_ref LIKE 'PRF-%%' "
                            'AND campaign_ref <> ALL(%s::STRING[])', (list(keep_refs),))
    except Exception as e:                       # noqa: BLE001
        log.warning('relay reap skipped: %s: %s', type(e).__name__, e)


# =============================================================== the domain index

def domains() -> list[dict]:
    """Every workload the engine knows how to protect, and what its authority is measured in.

    The point of the endpoint is one column: `risk_unit`. Two workloads, two units, one
    engine — dollars for a refund and PEOPLE for a broadcast, with a ceiling denominated
    in each. A platform claim is cheap to make in a README; this is the same claim as a
    row a judge can read.

    The ceiling is read from the LIVE policy table where a policy exists (the refund
    domain's, in the demo tenant a judge is looking at) and falls back to the value this
    module publishes for its own proof runs. `ceiling_source` says which, because a number
    on a screen with an unclear provenance is worse than no number.
    """
    from . import demo_state
    from .domains import known
    from .seed import DEMO_TENANT

    defaults = {'refund': PROOF_CEILING_CENTS, 'broadcast': BROADCAST_CEILING_RECIPIENTS}
    described = {
        'refund': 'Money leaves the company and does not come back on its own. The '
                  'authority ceiling is denominated in dollars; above it a human decides.',
        'broadcast': 'A message reaches people who cannot be made to un-receive it. The '
                     'ceiling is denominated in RECIPIENTS — the same send costs about '
                     'five cents, so a money-shaped policy would never have asked.',
    }

    live: dict[str, dict] = {}
    try:
        def _read(cur):
            cur.execute("""
                SELECT policy_id, max_auto_action_cents, risk_grants
                FROM axiom_policy
                WHERE tenant_id = %s AND status = 'ACTIVE'
            """, (str(DEMO_TENANT),))
            return {r['policy_id']: r for r in cur.fetchall()}
        live = demo_state.tx(_read, readonly=True)
    except Exception as e:                       # noqa: BLE001 — the list is useful anyway
        log.warning('domain ceilings fell back to defaults: %s: %s', type(e).__name__, e)

    out = []
    for d in sorted(known().values(), key=lambda x: x.task_type):
        row = live.get(d.policy_id)
        # `max_auto_action_cents` is denominated in CENTS and nothing else. Reading it for
        # a domain measured in people would render a dollar figure as a recipient count —
        # exactly the category error this endpoint exists to disprove — so a non-money
        # domain takes its ceiling only from the risk_grant carrying its own unit. Today
        # the demo tenant has no broadcast policy and the fallback happens to be right;
        # that is luck, and luck stops being right the moment someone seeds one.
        ceiling, source = defaults.get(d.task_type, 0), 'proof default'
        if row:
            grant = next((g for g in (row['risk_grants'] or [])
                          if g.get('unit') == d.risk.risk_unit), None)
            if grant is not None:
                ceiling, source = int(grant['max_magnitude']), 'live policy'
            elif d.risk.risk_unit == MONEY_USD_CENTS:
                ceiling, source = int(row['max_auto_action_cents']), 'live policy'
        out.append({
            'task_type': d.task_type,
            'name': d.name,
            'risk_unit': d.risk.risk_unit,
            'noun': d.risk.noun,
            'ceiling': ceiling,
            'ceiling_rendered': d.risk.render(ceiling),
            'ceiling_source': source,
            'reversibility': str(d.risk.reversibility),
            'policy_id': d.policy_id,
            'provider': d.provider_name,
            'operation': d.operation,
            'description': described.get(d.task_type, ''),
        })
    return out


# ================================================================== the receipts index

def measurements() -> dict:
    """The committed measurements file, parsed. Never raises.

    Everything in it was MEASURED by running the command recorded beside it. Nothing in
    it is a Python literal that could drift away from the artifact it describes without
    anyone noticing, which is the entire reason it is a file the scripts' output is copied
    into rather than a dict in this module: a number in code looks equally true whether or
    not it was ever true.
    """
    try:
        return json.loads(MEASUREMENTS_PATH.read_text())
    except Exception as e:                       # noqa: BLE001
        log.error('measurements.json unreadable: %s: %s', type(e).__name__, e)
        return {'error': f'{type(e).__name__}: {e}'[:200]}
