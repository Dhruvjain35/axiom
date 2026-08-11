"""AXIOM :: execution memory — the durable state machine.

The half of the system that CONSTRAINS. Five protocols live here:

    CLAIM    take ownership, bump the fence                       (1 transaction)
    PREPARE  mint the idempotency receipt; authorize the act      (1 transaction)
    DISPATCH call the outside world                               (NO transaction)
    SETTLE   record what happened + write the outcome memory      (1 transaction)
    RECOVER  read receipt + recall memory + transition, fused     (1 transaction)

Two invariants that every line here exists to protect:

  I1. No external side effect is authorized while a task is LEASED. The receipt commits
      FIRST, which moves the task to ACTION_PREPARED; only then may a call go out. So a
      crash before the receipt *cannot* have caused an effect — that is not a hope about
      timing, it is a consequence of commit ordering.

  I2. The fence, not the lease, is the correctness mechanism. A lease expiring does not
      stop a GC-paused worker that is already inside a refund HTTP call. Every write
      after the claim re-checks lease_epoch, so the zombie's settle is rejected while
      the legitimate successor's succeeds.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Sequence

import psycopg

from . import events, memory, policy as policy_mod, provider
from .config import SYSTEM_TENANT, settings
from .db import vector_literal
from .models import (
    CLAIMABLE_STATES, LIVE_ATTEMPT_STATES, AttemptState, ApprovalState, MemoryClass,
    Outcome, RetrievalClass, TaskState, Trust, ctx_state,
)

# Interpolated from models.CLAIMABLE_STATES so the claim predicate and the partial index
# predicate cannot drift apart. If they ever disagree the optimizer stops using
# axiom_task_claimable and the claim loop silently becomes a full table scan — correct
# results, catastrophic performance, no error anywhere.
_CLAIMABLE_SQL = ', '.join(f"'{s}'" for s in CLAIMABLE_STATES)
_LIVE_ATTEMPT_SQL = ', '.join(f"'{s}'" for s in LIVE_ATTEMPT_STATES)


class LeaseLost(RuntimeError):
    """The fence moved: another worker owns this task now.

    Not an error in the operational sense — it is the system correctly refusing a write
    from a process that has been superseded. Workers catch it and go back to claiming.
    """


class AlreadyLive(RuntimeError):
    """A live receipt already exists for this (task, step). Do NOT call the provider."""


class BudgetExceeded(RuntimeError):
    """The mission's hard spend cap refused this action.

    A first-class outcome, not a crash. An agent that runs out of authorized budget
    mid-mission must stop spending and say so — the alternative is an agent whose
    blast radius is bounded only by how long it stays up.
    """


class FingerprintMismatch(RuntimeError):
    """Crash window W7: the recovered agent synthesized a DIFFERENT request body under
    an existing idempotency key. Same key + different intent is not a retry. Hard stop."""


@dataclass
class Claimed:
    id: uuid.UUID
    tenant_id: uuid.UUID
    mission_id: uuid.UUID
    task_type: str
    dedupe_key: str
    state: TaskState
    lease_epoch: int
    attempt: int
    max_attempts: int
    payload: dict
    policy_id: str | None
    policy_version: int | None

    @property
    def is_recovery(self) -> bool:
        """Claimed in ACTION_PREPARED => a previous owner may already have acted."""
        return self.state == TaskState.ACTION_PREPARED


@dataclass
class PrepareResult:
    """The two ways PREPARE can succeed, as DATA rather than as control flow.

    Both outcomes are transactional writes that must COMMIT: either a receipt now
    authorizes an external call, or an approval row now parks the task on a human.
    Signalling the second case with an exception would roll back the row that records
    it — see the comment in prepare().
    """
    receipt: 'Receipt | None'
    approval_id: uuid.UUID | None = None

    @property
    def parked(self) -> bool:
        return self.receipt is None


@dataclass
class Receipt:
    id: uuid.UUID
    task_id: uuid.UUID
    step_name: str
    step_seq: int
    idempotency_key: str
    attempt_state: AttemptState
    provider: str
    operation: str
    amount_cents: int | None
    currency: str | None
    request_body: dict
    request_fingerprint: str
    lease_epoch: int
    provider_ref: str | None = None


# ============================================================================ AGENTS

def register_agent(cur: psycopg.Cursor, *, worker_ref: str, shards: Sequence[int] = (),
                   kind: str = 'worker', build_sha: str | None = None,
                   region: str | None = None) -> uuid.UUID:
    """Register (or re-register) this process in the worker pool.

    Pool rows live under the SYSTEM tenant so tenant_id stays NOT NULL everywhere with
    no nullable exception. Re-registering under the same worker_ref reuses the row,
    which is what makes an ECS task that restarts in place keep its identity.
    """
    cur.execute("""
        INSERT INTO axiom_agent (tenant_id, worker_ref, kind, status, shards, build_sha, region)
        VALUES (%s, %s, %s, 'ALIVE', %s, %s, %s)
        ON CONFLICT (tenant_id, worker_ref) DO UPDATE
        SET status = 'ALIVE', heartbeat_at = now(), shards = excluded.shards,
            started_at = now(), stopped_at = NULL, build_sha = excluded.build_sha
        RETURNING id
    """, (str(SYSTEM_TENANT), worker_ref, kind, list(shards), build_sha, region))
    agent_id = cur.fetchone()['id']
    events.append(cur, tenant_id=SYSTEM_TENANT, subject_type='agent', subject_id=agent_id,
                  event_type='agent.registered', actor=f'agent:{agent_id}',
                  to_state='ALIVE', detail={'worker_ref': worker_ref, 'shards': list(shards)})
    return agent_id


def heartbeat(cur: psycopg.Cursor, *, agent_id: uuid.UUID,
              held_task_ids: Sequence[uuid.UUID] = ()) -> None:
    """Prove liveness and push the lease forward on every task this worker holds.

    Each agent updates ONLY its own row, so heartbeats create exactly zero cross-worker
    contention — there is no shared "workers" row to hammer, which is the usual way a
    heartbeat design becomes the hotspot it was meant to avoid.
    """
    cur.execute("UPDATE axiom_agent SET heartbeat_at = now() WHERE id = %s", (str(agent_id),))
    if held_task_ids:
        # Fenced: only extend leases we still actually own.
        cur.execute(f"""
            UPDATE axiom_task
            SET available_at = now() + %s::INTERVAL, updated_at = now()
            WHERE id = ANY(%s::UUID[]) AND lease_owner = %s
              AND state IN ('LEASED', 'ACTION_PREPARED')
        """, (f'{settings.lease_seconds} seconds',
              [str(t) for t in held_task_ids], str(agent_id)))


def drain_agent(cur: psycopg.Cursor, *, agent_id: uuid.UUID) -> None:
    cur.execute("UPDATE axiom_agent SET status = 'DRAINING' WHERE id = %s", (str(agent_id),))


def stop_agent(cur: psycopg.Cursor, *, agent_id: uuid.UUID) -> None:
    cur.execute("""UPDATE axiom_agent SET status = 'DEAD', stopped_at = now()
                   WHERE id = %s""", (str(agent_id),))
    events.append(cur, tenant_id=SYSTEM_TENANT, subject_type='agent', subject_id=agent_id,
                  event_type='agent.stopped', actor=f'agent:{agent_id}', to_state='DEAD')


# ======================================================================== MISSIONS

def create_mission(cur: psycopg.Cursor, *, tenant_id: uuid.UUID, title: str, goal: str,
                   budget_cents: int, created_by: str) -> uuid.UUID:
    cur.execute("""
        INSERT INTO axiom_mission (tenant_id, title, goal, state, budget_cents, created_by)
        VALUES (%s, %s, %s, 'RUNNING', %s, %s) RETURNING id
    """, (str(tenant_id), title, goal, budget_cents, created_by))
    mid = cur.fetchone()['id']
    events.append(cur, tenant_id=tenant_id, subject_type='mission', subject_id=mid,
                  event_type='mission.created', actor=created_by, to_state='RUNNING',
                  mission_id=mid, detail={'title': title, 'budget_cents': budget_cents})
    return mid


def enqueue(cur: psycopg.Cursor, *, tenant_id: uuid.UUID, mission_id: uuid.UUID,
            task_type: str, dedupe_key: str, payload: dict,
            max_attempts: int = 5, actor: str = 'system') -> uuid.UUID | None:
    """Create one task. Returns None if this exact real-world exception is already queued.

    The unique index on (tenant_id, dedupe_key) means the planner physically cannot
    enqueue the same exception twice — the first line of defence against a double
    refund, and it costs nothing. An LLM planner that hallucinates a duplicate order
    gets a no-op instead of a second $300.
    """
    cur.execute("""
        INSERT INTO axiom_task (tenant_id, mission_id, task_type, dedupe_key, payload,
                                max_attempts, state)
        VALUES (%s, %s, %s, %s, %s, %s, 'READY')
        ON CONFLICT (tenant_id, dedupe_key) DO NOTHING
        RETURNING id, shard
    """, (str(tenant_id), str(mission_id), task_type, dedupe_key, json.dumps(payload),
          max_attempts))
    row = cur.fetchone()
    if not row:
        return None
    events.append(cur, tenant_id=tenant_id, subject_type='task', subject_id=row['id'],
                  event_type='task.enqueued', actor=actor, to_state='READY',
                  mission_id=mission_id, task_id=row['id'],
                  detail={'dedupe_key': dedupe_key, 'task_type': task_type,
                          'shard': row['shard']})
    return row['id']


# =========================================================================== CLAIM

def claim(cur: psycopg.Cursor, *, agent_id: uuid.UUID,
          shards: Sequence[int] | None = None) -> Claimed | None:
    """Take ownership of one claimable task. One statement, CAS on the fence.

    `available_at <= now()` means "ready to run OR the previous owner is dead", because
    available_at does double duty as earliest-run-time and lease expiry. That is what
    lets AXIOM have no reaper process — and a reaper matters here, because it would be a
    periodic large multi-row transaction landing on exactly the rows the claim loop is
    trying to scan.

    Returns None when nothing is claimable OR when another worker won the CAS. The
    caller must treat both as "try again", never as an error.
    """
    shard_pred = 'shard = ANY(%(shards)s::INT2[])' if shards else 'true'
    cur.execute(f"""
        WITH candidate AS (
            SELECT id, lease_epoch
            FROM axiom_task
            WHERE {shard_pred}
              AND available_at <= now()
              AND state IN ({_CLAIMABLE_SQL})
              AND attempt < max_attempts
            ORDER BY available_at ASC
            LIMIT 1
        )
        UPDATE axiom_task t
        SET lease_epoch  = t.lease_epoch + 1,
            lease_owner  = %(agent)s,
            available_at = now() + %(lease)s::INTERVAL,
            state        = CASE WHEN t.state IN ('READY', 'AWAITING_APPROVAL')
                                THEN 'LEASED'::task_state ELSE t.state END,
            updated_at   = now()
        FROM candidate c
        WHERE t.id = c.id AND t.lease_epoch = c.lease_epoch
        RETURNING t.id, t.tenant_id, t.mission_id, t.task_type, t.dedupe_key, t.state,
                  t.lease_epoch, t.attempt, t.max_attempts, t.payload,
                  t.policy_id, t.policy_version
    """, {'shards': list(shards) if shards else None, 'agent': str(agent_id),
          'lease': f'{settings.lease_seconds} seconds'})

    row = cur.fetchone()
    if not row:
        return None

    c = Claimed(
        id=row['id'], tenant_id=row['tenant_id'], mission_id=row['mission_id'],
        task_type=row['task_type'], dedupe_key=row['dedupe_key'],
        state=TaskState(row['state']), lease_epoch=row['lease_epoch'],
        attempt=row['attempt'], max_attempts=row['max_attempts'],
        payload=row['payload'] or {}, policy_id=row['policy_id'],
        policy_version=row['policy_version'],
    )
    events.append(cur, tenant_id=c.tenant_id, subject_type='task', subject_id=c.id,
                  event_type='task.claimed', actor=f'agent:{agent_id}',
                  to_state=str(c.state), lease_epoch=c.lease_epoch,
                  mission_id=c.mission_id, task_id=c.id,
                  detail={'recovery': c.is_recovery, 'attempt': c.attempt})
    return c


def _assert_fence(cur: psycopg.Cursor, task_id: uuid.UUID, agent_id: uuid.UUID,
                  epoch: int) -> None:
    """Re-check the fencing token. Every write after the claim goes through here.

    This is the single most important three lines in the system. A GC pause, a stalled
    network call, or a container that is about to be reaped can all leave a worker
    executing with a lease that has already been reassigned. The lease cannot stop it;
    this check can.
    """
    cur.execute("""SELECT lease_epoch, lease_owner FROM axiom_task WHERE id = %s""",
                (str(task_id),))
    row = cur.fetchone()
    if row is None:
        raise LeaseLost(f'task {task_id} vanished')
    if row['lease_epoch'] != epoch or row['lease_owner'] != agent_id:
        raise LeaseLost(
            f'fence moved on task {task_id}: held epoch {epoch}, '
            f'current epoch {row["lease_epoch"]}')


# ========================================================================= PREPARE

def live_receipt(cur: psycopg.Cursor, *, tenant_id: uuid.UUID, task_id: uuid.UUID,
                 step_name: str) -> Receipt | None:
    """The receipt for an external call that MAY be in flight right now.

    A point read on the partial index axiom_attempt_one_live. Returning a row means:
    an effect may already exist in the world. Never re-plan from scratch after this.
    """
    cur.execute(f"""
        SELECT id, task_id, step_name, step_seq, idempotency_key, attempt_state,
               provider, operation, amount_cents, currency, request_body,
               request_fingerprint, lease_epoch, provider_ref
        FROM axiom_action_attempt
        WHERE tenant_id = %s AND task_id = %s AND step_name = %s
          AND attempt_state IN ({_LIVE_ATTEMPT_SQL})
    """, (str(tenant_id), str(task_id), step_name))
    row = cur.fetchone()
    if not row:
        return None
    return Receipt(
        id=row['id'], task_id=row['task_id'], step_name=row['step_name'],
        step_seq=row['step_seq'], idempotency_key=row['idempotency_key'],
        attempt_state=AttemptState(row['attempt_state']), provider=row['provider'],
        operation=row['operation'], amount_cents=row['amount_cents'],
        currency=row['currency'], request_body=row['request_body'],
        request_fingerprint=row['request_fingerprint'], lease_epoch=row['lease_epoch'],
        provider_ref=row['provider_ref'],
    )


def prepare(
    cur: psycopg.Cursor,
    *,
    task: Claimed,
    agent_id: uuid.UUID,
    step_name: str,
    provider_name: str,
    operation: str,
    request_body: dict,
    amount_cents: int | None,
    currency: str = 'USD',
    policy_id: str = 'refund_authority',
    licensed_by_memory_id: uuid.UUID | None = None,
) -> PrepareResult:
    """Mint the receipt. THE transaction that authorizes an irreversible act.

    Order matters and is not negotiable:
      1. re-check the fence
      2. load and PIN the policy version
      3. authority check -> park for human approval if the machine may not self-authorize
      4. debit the mission budget (the CHECK constraint enforces the cap under contention)
      5. INSERT the receipt — idempotency_key is GENERATED by the database from
         immutable columns and is never supplied by this process
      6. journal + move the task to ACTION_PREPARED

    Only after this commits may a provider call go out.
    """
    _assert_fence(cur, task.id, agent_id, task.lease_epoch)

    # Refuse to mint a second live receipt for the same step. The unique partial index
    # would reject it anyway with 23505; checking first turns a database error into an
    # explicit, testable control-flow branch.
    if live_receipt(cur, tenant_id=task.tenant_id, task_id=task.id, step_name=step_name):
        raise AlreadyLive(f'task {task.id} step {step_name} already has a live receipt')

    pol = (policy_mod.at_version(cur, tenant_id=task.tenant_id, policy_id=policy_id,
                                 version=task.policy_version)
           if task.policy_version is not None
           else policy_mod.active(cur, tenant_id=task.tenant_id, policy_id=policy_id))

    # Pin the version for the whole attempt, so publishing v3 mid-flight cannot change
    # the rules an in-progress action is judged against.
    if task.policy_version is None:
        cur.execute("""UPDATE axiom_task SET policy_id = %s, policy_version = %s
                       WHERE id = %s""", (pol.policy_id, pol.version, str(task.id)))
        task.policy_id, task.policy_version = pol.policy_id, pol.version

    approval_id: uuid.UUID | None = None
    if not pol.authorizes(amount_cents):
        # A human may already have ruled on exactly this action. Burning the single-use
        # decision token is what authorizes crossing the policy ceiling — ONCE. Without
        # this consume step an approved task simply re-parks on the next claim, because
        # the policy ceiling has not moved and never will; the approval, not the policy,
        # is what changed.
        #
        # Consuming rather than merely reading is the point: an approval is a capability,
        # not a standing permission, so a worker that restarts after the token is spent
        # cannot replay a human's decision into a second refund.
        approval_id = consume_approval(cur, tenant_id=task.tenant_id, task_id=task.id,
                                       step_name=step_name)

    if approval_id is None and not pol.authorizes(amount_cents):
        # RETURNED, never raised.
        #
        # This was a real bug and it is worth keeping the scar tissue visible: parking
        # for approval used to raise NeedsApproval, which propagated out of db.tx(), so
        # the connection context manager ROLLED BACK — discarding the approval row and
        # the AWAITING_APPROVAL transition that the same transaction had just written.
        # The task returned to READY, was re-claimed, parked again, and looped forever
        # while axiom_approval stayed empty. An exception is a fine way to abort a
        # transaction and a terrible way to return a value from one.
        approval_id = request_approval(
            cur, task=task, agent_id=agent_id, step_name=step_name,
            reason=(f'amount {amount_cents} exceeds policy {pol.policy_id} v{pol.version} '
                    f'limit {pol.max_auto_action_cents}' if not pol.requires_approval
                    else f'policy {pol.policy_id} v{pol.version} requires approval'),
            proposed_action={'operation': operation, 'request_body': request_body},
            proposed_amount_cents=amount_cents,
            evidence_memory_ids=[licensed_by_memory_id] if licensed_by_memory_id else [],
        )
        return PrepareResult(receipt=None, approval_id=approval_id)

    if amount_cents:
        # Deliberately contended: two workers racing to spend the last $50 cannot both
        # win — one takes a 40001 and re-reads. A budget IS a shared resource and
        # serializing on it is the point, not a bug.
        #
        # The cap is expressed TWICE, on purpose. The WHERE clause is the graceful path:
        # it declines the debit and leaves the transaction usable, so the caller can
        # dead-letter the task with a real explanation. The CHECK constraint on
        # axiom_mission is the guarantee: it holds even against a future code path that
        # forgets this predicate, at the cost of aborting the transaction. Control flow
        # from the predicate, correctness from the constraint — never the reverse, since
        # a constraint violation poisons the transaction it fires in.
        cur.execute("""
            UPDATE axiom_mission
            SET spent_cents = spent_cents + %(amt)s, updated_at = now()
            WHERE id = %(mission)s AND tenant_id = %(tenant)s
              AND spent_cents + %(amt)s <= budget_cents
            RETURNING spent_cents, budget_cents
        """, {'amt': amount_cents, 'mission': str(task.mission_id),
              'tenant': str(task.tenant_id)})
        if cur.rowcount != 1:
            cur.execute("""SELECT spent_cents, budget_cents FROM axiom_mission
                           WHERE id = %s""", (str(task.mission_id),))
            m = cur.fetchone()
            raise BudgetExceeded(
                f'mission budget exhausted: {m["spent_cents"]} of {m["budget_cents"]} '
                f'cents already committed, this action needs {amount_cents} more')

    fp = provider.fingerprint(request_body)
    cur.execute("""
        INSERT INTO axiom_action_attempt (
            tenant_id, task_id, step_name, step_seq, provider, operation,
            amount_cents, currency, request_fingerprint, request_body,
            lease_epoch, prepared_by, licensed_by_memory_id, policy_id, policy_version)
        VALUES (%s, %s, %s,
                coalesce((SELECT max(step_seq) FROM axiom_action_attempt
                          WHERE tenant_id = %s AND task_id = %s AND step_name = %s), 0) + 1,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id, step_seq, idempotency_key, attempt_state
    """, (str(task.tenant_id), str(task.id), step_name,
          str(task.tenant_id), str(task.id), step_name,
          provider_name, operation, amount_cents, currency, fp, json.dumps(request_body),
          task.lease_epoch, str(agent_id),
          str(licensed_by_memory_id) if licensed_by_memory_id else None,
          pol.policy_id, pol.version))
    row = cur.fetchone()

    cur.execute("""UPDATE axiom_task SET state = 'ACTION_PREPARED', updated_at = now()
                   WHERE id = %s AND lease_epoch = %s""", (str(task.id), task.lease_epoch))
    if cur.rowcount != 1:
        raise LeaseLost(f'fence moved while preparing task {task.id}')

    events.append(cur, tenant_id=task.tenant_id, subject_type='task', subject_id=task.id,
                  event_type='attempt.prepared', actor=f'agent:{agent_id}',
                  from_state=str(TaskState.LEASED), to_state=str(TaskState.ACTION_PREPARED),
                  lease_epoch=task.lease_epoch, mission_id=task.mission_id,
                  task_id=task.id, attempt_id=row['id'],
                  detail={'idempotency_key': row['idempotency_key'], 'step': step_name,
                          'step_seq': row['step_seq'], 'amount_cents': amount_cents,
                          'policy': f'{pol.policy_id} v{pol.version}',
                          'authorized_by_approval': str(approval_id) if approval_id else None,
                          'licensed_by_memory_id': str(licensed_by_memory_id)
                          if licensed_by_memory_id else None})

    task.state = TaskState.ACTION_PREPARED
    return PrepareResult(receipt=Receipt(
        id=row['id'], task_id=task.id, step_name=step_name, step_seq=row['step_seq'],
        idempotency_key=row['idempotency_key'],
        attempt_state=AttemptState(row['attempt_state']), provider=provider_name,
        operation=operation, amount_cents=amount_cents, currency=currency,
        request_body=request_body, request_fingerprint=fp, lease_epoch=task.lease_epoch,
    ))


def mark_dispatched(cur: psycopg.Cursor, *, receipt: Receipt) -> None:
    """Observability ONLY.

    attempt_state DISPATCHED is SAFETY-EQUIVALENT to PREPARED: the process can die
    between the send and this write, so no correctness decision may ever branch on the
    difference. It exists so a human watching the dashboard can see the difference
    between "about to call" and "called".
    """
    cur.execute("""
        UPDATE axiom_action_attempt SET attempt_state = 'DISPATCHED', dispatched_at = now()
        WHERE id = %s AND attempt_state = 'PREPARED'
    """, (str(receipt.id),))


# ========================================================================== SETTLE

def settle(
    cur: psycopg.Cursor,
    *,
    task: Claimed,
    agent_id: uuid.UUID,
    receipt: Receipt,
    outcome_state: AttemptState,
    task_state: TaskState,
    response_body: dict | None,
    provider_ref: str | None,
    http_status: int | None,
    memory_content: str,
    memory_embedding: Sequence[float],
    memory_outcome: Outcome,
    result: dict | None = None,
    last_error: str | None = None,
) -> uuid.UUID:
    """Record what happened AND write the outcome memory, in one transaction.

    The memory write is not a nice-to-have here. Co-committing it with the terminal
    state transition is what makes it impossible for memory to disagree with execution
    state — there is no interval in which the refund is recorded but the lesson is not,
    or vice versa. Moving this to a background job for throughput would destroy the
    entire differentiator, so don't.
    """
    _assert_fence(cur, task.id, agent_id, task.lease_epoch)

    cur.execute("""
        UPDATE axiom_action_attempt
        SET attempt_state = %s, response_body = %s, provider_ref = %s,
            http_status = %s, settled_at = now()
        WHERE id = %s AND lease_epoch = %s
    """, (str(outcome_state), json.dumps(response_body) if response_body else None,
          provider_ref, http_status, str(receipt.id), receipt.lease_epoch))
    if cur.rowcount != 1:
        # The receipt was minted under a different fence than the one we hold: a zombie
        # is trying to settle work that has been taken over. Refuse.
        raise LeaseLost(f'receipt {receipt.id} was minted under a different lease epoch')

    cur.execute("""
        UPDATE axiom_task
        SET state = %s, result = %s, last_error = %s, lease_owner = NULL,
            updated_at = now()
        WHERE id = %s AND lease_epoch = %s
    """, (str(task_state), json.dumps(result) if result else None, last_error,
          str(task.id), task.lease_epoch))
    if cur.rowcount != 1:
        raise LeaseLost(f'fence moved while settling task {task.id}')

    mem_id = memory.write(
        cur, tenant_id=task.tenant_id, memory_class=MemoryClass.EPISODIC,
        context_key=ctx_state(TaskState.ACTION_PREPARED), content=memory_content,
        embedding=memory_embedding, outcome=memory_outcome,
        source='system:execution', trust_level=Trust.FIRST_PARTY,
        resolution={'provider_ref': provider_ref, 'http_status': http_status,
                    'step': receipt.step_name, 'replayed': bool(
                        (response_body or {}).get('idempotent_replay'))},
        mission_id=task.mission_id, task_id=task.id, attempt_id=receipt.id,
        agent_id=agent_id, policy_id=task.policy_id, policy_version=task.policy_version,
        actor=f'agent:{agent_id}',
    )

    events.append(cur, tenant_id=task.tenant_id, subject_type='task', subject_id=task.id,
                  event_type='attempt.settled', actor=f'agent:{agent_id}',
                  from_state=str(TaskState.ACTION_PREPARED), to_state=str(task_state),
                  lease_epoch=task.lease_epoch, mission_id=task.mission_id,
                  task_id=task.id, attempt_id=receipt.id,
                  detail={'attempt_state': str(outcome_state), 'provider_ref': provider_ref,
                          'http_status': http_status, 'memory_id': str(mem_id),
                          'outcome': str(memory_outcome)})
    return mem_id


# ========================================================================= RECOVER

@dataclass
class RecoveryPlan:
    """What the fused transaction decided, and the evidence it decided on."""
    action: str                      # 'RESEND' | 'ESCALATE' | 'REPLAN'
    receipt: Receipt | None
    recalled: list[memory.Recalled] = field(default_factory=list)
    rationale: str = ''

    @property
    def evidence_ids(self) -> list[uuid.UUID]:
        return [r.id for r in self.recalled]


def recover(cur: psycopg.Cursor, *, task: Claimed, agent_id: uuid.UUID,
            situation_embedding: Sequence[float], step_name: str) -> RecoveryPlan:
    """THE fused transaction. Read the receipt AND recall memory AND transition, once.

    This is the function the entire project exists to make possible. Splitting it into
    "ask the workflow engine what happened" plus "ask the vector database what it means"
    reintroduces exactly the window AXIOM claims to close: the agent resumes on memory
    that has already been superseded, with no transaction to close it.

    The decision is deliberately conservative. A live receipt means an effect MAY exist,
    and the safe default is always to re-send under the SAME idempotency key — the
    provider dedupes, so re-sending costs nothing and is the only way to convert
    "unknown" into "known". Memory can override that default in one direction only:
    toward escalation.
    """
    _assert_fence(cur, task.id, agent_id, task.lease_epoch)

    receipt = live_receipt(cur, tenant_id=task.tenant_id, task_id=task.id,
                           step_name=step_name)

    recalled = memory.recall(
        cur, tenant_id=task.tenant_id, embedding=situation_embedding,
        memory_class=MemoryClass.EPISODIC, context_key=ctx_state(TaskState.ACTION_PREPARED),
        retrieval_class=RetrievalClass.ACTIONABLE, k=settings.recall_k,
    )

    if receipt is None:
        # Claimed in ACTION_PREPARED but no live receipt: the previous owner settled and
        # died before transitioning, or the receipt reached a terminal state. Nothing is
        # in flight, so replanning is safe.
        plan = RecoveryPlan('REPLAN', None, recalled,
                            'no live receipt: no external effect is outstanding')
    else:
        # Memory votes. Only DUPLICATE_EFFECT and HUMAN_REQUIRED can override the
        # default, and only toward escalation — memory may never talk us INTO an act.
        votes = [r.outcome for r in recalled]
        danger = sum(1 for v in votes
                     if v in (Outcome.DUPLICATE_EFFECT, Outcome.HUMAN_REQUIRED))
        if danger and danger >= len(votes) / 2:
            plan = RecoveryPlan(
                'ESCALATE', receipt, recalled,
                f'{danger}/{len(votes)} comparable recoveries ended in a duplicate '
                f'effect or needed a human; refusing to re-dispatch unattended')
        else:
            plan = RecoveryPlan(
                'RESEND', receipt, recalled,
                f'live receipt {receipt.idempotency_key} exists; re-dispatching under '
                f'the same key ({len(recalled)} comparable recoveries recalled, '
                f'none adverse)')

    events.append(
        cur, tenant_id=task.tenant_id, subject_type='task', subject_id=task.id,
        event_type='task.recovered', actor=f'agent:{agent_id}',
        from_state=str(TaskState.ACTION_PREPARED), to_state=str(task.state),
        lease_epoch=task.lease_epoch, mission_id=task.mission_id, task_id=task.id,
        attempt_id=receipt.id if receipt else None,
        detail={'action': plan.action, 'rationale': plan.rationale,
                'evidence': [str(i) for i in plan.evidence_ids],
                'idempotency_key': receipt.idempotency_key if receipt else None},
    )
    return plan


def verify_fingerprint(receipt: Receipt, resynthesized_body: dict) -> None:
    """Crash window W7.

    After a restart an LLM may re-synthesize the request rather than reusing the stored
    one. If the body differs, this is a NEW intent wearing an OLD idempotency key.
    Detecting it is a hard stop, not a warning — the correct response is to escalate to
    a human, never to pick one of the two bodies and proceed.

    The engine always re-dispatches `receipt.request_body`, so this is defence in depth
    for any future path that reconstructs a request.
    """
    if provider.fingerprint(resynthesized_body) != receipt.request_fingerprint:
        raise FingerprintMismatch(
            f'idempotency key {receipt.idempotency_key} was minted for a different '
            f'request body; refusing to dispatch')


# ======================================================================== FAILURES

def fail_retryable(cur: psycopg.Cursor, *, task: Claimed, agent_id: uuid.UUID,
                   receipt: Receipt | None, error: str, backoff_seconds: int) -> None:
    """Release the task for a later retry, with backoff written into available_at.

    Bumping `attempt` here is what eventually drives the task out of the claim index
    entirely (attempt < max_attempts is part of the claim predicate), so a permanently
    failing task stops being scanned rather than being retried forever.
    """
    _assert_fence(cur, task.id, agent_id, task.lease_epoch)

    if receipt:
        cur.execute("""
            UPDATE axiom_action_attempt
            SET attempt_state = 'FAILED_RETRYABLE', settled_at = now()
            WHERE id = %s AND attempt_state IN ('PREPARED', 'DISPATCHED')
        """, (str(receipt.id),))

    cur.execute("""
        UPDATE axiom_task
        SET state = 'READY', attempt = attempt + 1, lease_owner = NULL,
            available_at = now() + %s::INTERVAL, last_error = %s, updated_at = now()
        WHERE id = %s AND lease_epoch = %s
    """, (f'{backoff_seconds} seconds', error[:500], str(task.id), task.lease_epoch))
    if cur.rowcount != 1:
        raise LeaseLost(f'fence moved while failing task {task.id}')

    events.append(cur, tenant_id=task.tenant_id, subject_type='task', subject_id=task.id,
                  event_type='task.retry_scheduled', actor=f'agent:{agent_id}',
                  from_state=str(task.state), to_state=str(TaskState.READY),
                  lease_epoch=task.lease_epoch, mission_id=task.mission_id, task_id=task.id,
                  detail={'error': error[:500], 'backoff_seconds': backoff_seconds,
                          'attempt': task.attempt + 1})


def dead_letter(cur: psycopg.Cursor, *, task: Claimed, agent_id: uuid.UUID,
                reason: str, memory_content: str | None = None,
                memory_embedding: Sequence[float] | None = None) -> None:
    """Terminal: attempts exhausted, or continuing is unsafe.

    Unsafe-to-continue is the important case. A fingerprint mismatch or an escalation
    the humans never answered must end here rather than in a retry loop, because a
    retry loop around an ambiguous external effect is how you get the double refund
    this system exists to prevent.
    """
    _assert_fence(cur, task.id, agent_id, task.lease_epoch)
    cur.execute("""
        UPDATE axiom_task
        SET state = 'DEAD_LETTER', lease_owner = NULL, last_error = %s, updated_at = now()
        WHERE id = %s AND lease_epoch = %s
    """, (reason[:500], str(task.id), task.lease_epoch))
    if cur.rowcount != 1:
        raise LeaseLost(f'fence moved while dead-lettering task {task.id}')

    if memory_content and memory_embedding is not None:
        memory.write(
            cur, tenant_id=task.tenant_id, memory_class=MemoryClass.EPISODIC,
            context_key=ctx_state(TaskState.ACTION_PREPARED), content=memory_content,
            embedding=memory_embedding, outcome=Outcome.HUMAN_REQUIRED,
            source='system:execution', trust_level=Trust.FIRST_PARTY,
            mission_id=task.mission_id, task_id=task.id, agent_id=agent_id,
            actor=f'agent:{agent_id}',
        )

    events.append(cur, tenant_id=task.tenant_id, subject_type='task', subject_id=task.id,
                  event_type='task.dead_lettered', actor=f'agent:{agent_id}',
                  from_state=str(task.state), to_state=str(TaskState.DEAD_LETTER),
                  lease_epoch=task.lease_epoch, mission_id=task.mission_id,
                  task_id=task.id, detail={'reason': reason[:500]})


# ======================================================================= APPROVALS

def request_approval(cur: psycopg.Cursor, *, task: Claimed, agent_id: uuid.UUID,
                     step_name: str, reason: str, proposed_action: dict,
                     proposed_amount_cents: int | None,
                     evidence_memory_ids: Sequence[uuid.UUID] = (),
                     ttl_seconds: int = 900) -> uuid.UUID:
    """Park the task on a human decision and RELEASE the lease.

    Releasing the lease matters: an approval that nobody answers must not pin a worker.
    The expiry is written into the task's available_at, so an unanswered approval is
    reclaimed by a worker and resolved rather than sitting forever — no approval-expiry
    cron job, the same self-healing trick the lease uses.
    """
    cur.execute("""
        INSERT INTO axiom_approval (
            tenant_id, task_id, mission_id, step_name, reason, proposed_action,
            proposed_amount_cents, evidence_memory_ids, policy_id, policy_version,
            requested_by, expires_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now() + %s::INTERVAL)
        RETURNING id, decision_token, expires_at
    """, (str(task.tenant_id), str(task.id), str(task.mission_id), step_name, reason,
          json.dumps(proposed_action), proposed_amount_cents,
          [str(m) for m in evidence_memory_ids], task.policy_id, task.policy_version,
          str(agent_id), f'{ttl_seconds} seconds'))
    row = cur.fetchone()

    cur.execute("""
        UPDATE axiom_task
        SET state = 'AWAITING_APPROVAL', lease_owner = NULL, available_at = %s,
            updated_at = now()
        WHERE id = %s AND lease_epoch = %s
    """, (row['expires_at'], str(task.id), task.lease_epoch))
    if cur.rowcount != 1:
        raise LeaseLost(f'fence moved while parking task {task.id} for approval')

    events.append(cur, tenant_id=task.tenant_id, subject_type='task', subject_id=task.id,
                  event_type='approval.requested', actor=f'agent:{agent_id}',
                  from_state=str(TaskState.LEASED),
                  to_state=str(TaskState.AWAITING_APPROVAL),
                  lease_epoch=task.lease_epoch, mission_id=task.mission_id,
                  task_id=task.id,
                  detail={'approval_id': str(row['id']), 'reason': reason,
                          'amount_cents': proposed_amount_cents})
    task.state = TaskState.AWAITING_APPROVAL
    return row['id']


def decide_approval(cur: psycopg.Cursor, *, tenant_id: uuid.UUID, approval_id: uuid.UUID,
                    approved: bool, decided_by: str, note: str = '') -> None:
    """A human rules. On approval the task becomes claimable immediately."""
    cur.execute("""
        UPDATE axiom_approval
        SET state = %s, decided_by = %s, decided_at = now(), decision_note = %s
        WHERE tenant_id = %s AND id = %s AND state = 'PENDING'
        RETURNING task_id, mission_id, step_name, proposed_amount_cents
    """, (str(ApprovalState.APPROVED if approved else ApprovalState.REJECTED),
          decided_by, note, str(tenant_id), str(approval_id)))
    row = cur.fetchone()
    if not row:
        raise ValueError(f'approval {approval_id} is not PENDING')

    if approved:
        cur.execute("""UPDATE axiom_task SET available_at = now(), updated_at = now()
                       WHERE id = %s AND state = 'AWAITING_APPROVAL'""", (str(row['task_id']),))
    else:
        cur.execute("""
            UPDATE axiom_task
            SET state = 'CANCELLED', lease_owner = NULL, updated_at = now(),
                last_error = 'rejected by operator'
            WHERE id = %s AND state = 'AWAITING_APPROVAL'
        """, (str(row['task_id']),))

    events.append(cur, tenant_id=tenant_id, subject_type='task', subject_id=row['task_id'],
                  event_type='approval.decided', actor=f'human:{decided_by}',
                  from_state=str(TaskState.AWAITING_APPROVAL),
                  to_state=str(TaskState.READY if approved else TaskState.CANCELLED),
                  mission_id=row['mission_id'], task_id=row['task_id'],
                  detail={'approval_id': str(approval_id), 'approved': approved,
                          'note': note, 'amount_cents': row['proposed_amount_cents']})


def consume_approval(cur: psycopg.Cursor, *, tenant_id: uuid.UUID,
                     task_id: uuid.UUID, step_name: str) -> uuid.UUID | None:
    """Burn the single-use decision token.

    A human decision is a capability, not a standing permission. Consuming the token
    means one approval authorizes exactly one action — an approved refund cannot be
    replayed into a second one by a worker that restarts.
    """
    cur.execute("""
        UPDATE axiom_approval
        SET token_consumed_at = now()
        WHERE tenant_id = %s AND task_id = %s AND step_name = %s
          AND state = 'APPROVED' AND token_consumed_at IS NULL
        RETURNING id
    """, (str(tenant_id), str(task_id), step_name))
    row = cur.fetchone()
    return row['id'] if row else None


def pending_approvals(cur: psycopg.Cursor, *, tenant_id: uuid.UUID,
                      limit: int = 50) -> list[dict]:
    cur.execute("""
        SELECT id, task_id, mission_id, step_name, reason, proposed_action,
               proposed_amount_cents, evidence_memory_ids, requested_at, expires_at,
               policy_id, policy_version
        FROM axiom_approval
        WHERE tenant_id = %s AND state = 'PENDING'
        ORDER BY expires_at ASC LIMIT %s
    """, (str(tenant_id), limit))
    return cur.fetchall()


# ============================================================================ READ

def get_task(cur: psycopg.Cursor, *, tenant_id: uuid.UUID, task_id: uuid.UUID) -> dict | None:
    cur.execute("""
        SELECT t.*, m.title AS mission_title
        FROM axiom_task t JOIN axiom_mission m ON m.id = t.mission_id
        WHERE t.tenant_id = %s AND t.id = %s
    """, (str(tenant_id), str(task_id)))
    return cur.fetchone()


def list_tasks(cur: psycopg.Cursor, *, tenant_id: uuid.UUID,
               mission_id: uuid.UUID | None = None, limit: int = 200) -> list[dict]:
    if mission_id:
        cur.execute("""
            SELECT id, task_type, dedupe_key, state, shard, attempt, max_attempts,
                   lease_epoch, lease_owner, available_at, payload, result, last_error,
                   policy_id, policy_version, updated_at
            FROM axiom_task WHERE tenant_id = %s AND mission_id = %s
            ORDER BY dedupe_key LIMIT %s
        """, (str(tenant_id), str(mission_id), limit))
    else:
        cur.execute("""
            SELECT id, task_type, dedupe_key, state, shard, attempt, max_attempts,
                   lease_epoch, lease_owner, available_at, payload, result, last_error,
                   policy_id, policy_version, updated_at
            FROM axiom_task WHERE tenant_id = %s ORDER BY updated_at DESC LIMIT %s
        """, (str(tenant_id), limit))
    return cur.fetchall()


def mission_summary(cur: psycopg.Cursor, *, tenant_id: uuid.UUID,
                    mission_id: uuid.UUID) -> dict:
    cur.execute("""
        SELECT id, title, goal, state, budget_cents, spent_cents, created_at
        FROM axiom_mission WHERE tenant_id = %s AND id = %s
    """, (str(tenant_id), str(mission_id)))
    m = cur.fetchone()
    if not m:
        return {}
    cur.execute("""
        SELECT state, count(*) AS n FROM axiom_task
        WHERE tenant_id = %s AND mission_id = %s GROUP BY state
    """, (str(tenant_id), str(mission_id)))
    m = dict(m)
    m['by_state'] = {r['state']: r['n'] for r in cur.fetchall()}
    return m


def unsettled_receipts(cur: psycopg.Cursor, *, tenant_id: uuid.UUID) -> list[dict]:
    """The reconciliation worklist: every external call that might be in flight.

    Doubles as an operational answer to "what is this system currently unsure about?",
    which is the question you actually want answered during an incident.
    """
    cur.execute(f"""
        SELECT id, task_id, step_name, step_seq, provider, operation, amount_cents,
               idempotency_key, prepared_at, attempt_state
        FROM axiom_action_attempt
        WHERE tenant_id = %s AND attempt_state IN ({_LIVE_ATTEMPT_SQL})
        ORDER BY prepared_at ASC
    """, (str(tenant_id),))
    return cur.fetchall()
