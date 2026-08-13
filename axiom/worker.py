"""AXIOM :: the worker agent.

One process, one loop:

    claim -> (recover | plan) -> prepare -> dispatch -> settle

Everything correctness-critical lives in tasks.py. This module's whole job is to be the
thing that can be killed. It is deliberately written so that dying at ANY line leaves
the database in a state the crash-window table already accounts for — which is why
there is no cleanup in a finally block that matters, and why SIGKILL (which skips
finally blocks entirely) is the test case rather than a graceful shutdown.

Run:
    python -m axiom.worker --shards 0,1,2,3
    AXIOM_CHAOS_POST=0.3 python -m axiom.worker      # die 30% of the time after refunds land
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time
import uuid
from typing import Sequence

from . import db, embeddings, events, llm, memory, provider, tasks
from .config import SYSTEM_TENANT, settings
from .models import AttemptState, MemoryClass, Outcome, TaskState, Trust, ctx_exception, ctx_state
from .provider import ProviderCrash, ProviderError
from .tasks import AlreadyLive, BudgetExceeded, FingerprintMismatch, LeaseLost

# PROCESS-level stop, set by SIGTERM/SIGINT. It means "this whole process is going away".
# It is deliberately NOT what Worker.stop() sets: a Worker is one unit of work, and in the
# serverless deployments many Workers live and die inside a single warm process. Setting a
# module-global from an instance method meant the FIRST inline worker to finish poisoned
# every worker that instance ever ran afterwards — they each registered, immediately saw a
# set flag, claimed nothing, and returned `tasks: 0`. The demo looked idle and proved
# nothing, with no error anywhere.
_stop = threading.Event()


def _log(msg: str) -> None:
    print(f'[{time.strftime("%H:%M:%S")}] {msg}', flush=True)


class Worker:
    def __init__(self, shards: Sequence[int] | None = None, worker_ref: str | None = None,
                 chaos_post: float | None = None, chaos_pre: float | None = None):
        # Chaos is passed IN, not read from the environment at dispatch time.
        # `settings` is a frozen dataclass built once at import, so a caller that set
        # AXIOM_CHAOS_POST just before starting a worker changed nothing — which is
        # exactly what the hosted demo did: it asked for a crash on every run and never
        # got one, and nothing errored to say so. An in-process caller must be able to
        # ask for a crash without mutating global state that was already read.
        self.chaos_post = chaos_post
        self.chaos_pre = chaos_pre
        self.shards = list(shards) if shards else []
        self.worker_ref = worker_ref or f'local-{uuid.uuid4().hex[:10]}'
        self.agent_id: uuid.UUID | None = None
        self._held: set[uuid.UUID] = set()
        self._hb: threading.Thread | None = None
        # This worker's own stop flag. Scoped to the instance so one worker finishing
        # cannot end the next one in the same process.
        self._stop = threading.Event()

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self.agent_id = db.tx(lambda cur: tasks.register_agent(
            cur, worker_ref=self.worker_ref, shards=self.shards,
            build_sha=os.environ.get('AXIOM_BUILD_SHA'),
            region=settings.aws_region))
        _log(f'agent {self.agent_id} registered as {self.worker_ref} '
             f'shards={self.shards or "ALL"}')
        self._hb = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._hb.start()

    def _heartbeat_loop(self) -> None:
        """Push the lease forward on everything we hold.

        A daemon thread on purpose: when the main thread dies the heartbeats stop
        immediately, and the tasks this worker held become claimable as soon as their
        available_at passes. No reaper, no tombstone, no cleanup job.
        """
        while not (self._stop.is_set() or _stop.is_set()):
            if self._stop.wait(settings.heartbeat_seconds):
                break
            try:
                held = list(self._held)
                db.tx(lambda cur: tasks.heartbeat(cur, agent_id=self.agent_id,
                                                  held_task_ids=held))
            except Exception as e:            # never let a heartbeat blip kill the worker
                _log(f'heartbeat failed: {type(e).__name__}: {e}')

    def stop(self) -> None:
        self._stop.set()
        try:
            db.tx(lambda cur: tasks.stop_agent(cur, agent_id=self.agent_id))
        except Exception:
            pass

    # ----------------------------------------------------------------- main loop

    def run(self, max_tasks: int | None = None, idle_exit: bool = False,
            deadline_seconds: float | None = None) -> int:
        """Claim and execute until the queue is dry, the count is met, or time runs out.

        `deadline_seconds` exists for the serverless deployments, where a worker is not a
        long-lived process but a bounded slice of one. The deadline is checked only
        BETWEEN tasks, never inside one: a worker that abandoned a task mid-dispatch to
        respect a clock would be manufacturing exactly the crash window this system exists
        to survive, and it would do it on every single invocation. Overrunning the budget
        by one task is correct; the caller sizes the budget with that in mind.
        """
        done = 0
        idle_rounds = 0
        deadline = (time.monotonic() + deadline_seconds) if deadline_seconds else None
        while not (self._stop.is_set() or _stop.is_set()):
            if max_tasks is not None and done >= max_tasks:
                break
            if deadline is not None and time.monotonic() >= deadline:
                break
            claimed = db.tx(lambda cur: tasks.claim(cur, agent_id=self.agent_id,
                                                    shards=self.shards or None))
            if claimed is None:
                idle_rounds += 1
                if idle_exit and idle_rounds > 3:
                    break
                time.sleep(settings.poll_idle_ms / 1000.0)
                continue

            idle_rounds = 0
            self._held.add(claimed.id)
            try:
                self.execute(claimed)
                done += 1
            except LeaseLost as e:
                # Correct behaviour, not an error: this process has been superseded.
                _log(f'  lease lost on {claimed.dedupe_key}: {e}')
            except ProviderCrash as e:
                _log(f'  !! {e}')
                if settings.crash_exits:
                    # Simulated death. os._exit skips every finally block and every
                    # atexit hook, which is the point — a real SIGKILL does the same, and
                    # any cleanup we performed here would be cleanup a real crash never
                    # gets. This is the path the chaos demo and the video use.
                    os._exit(9)
                # SERVERLESS: the worker shares a process with the HTTP request that
                # started it, so os._exit would kill the caller's request too — the
                # browser sees a dead socket and the guided demo stalls with nothing
                # proven. (It did, on the first live deployment: 0 replays for two
                # minutes.) Raising instead abandons the task at exactly the same durable
                # state — ACTION_PREPARED, live receipt, lease still held and no longer
                # heartbeating — which is the ONLY thing recovery reads. What is lost is
                # the guarantee that no Python cleanup ran, and nothing in this class does
                # cleanup that would change the outcome; the fence is in the database.
                #
                # Stated plainly because it is the one place the hosted demo is weaker
                # than the local one: `scripts/chaos_demo.py` sends a real SIGKILL.
                raise
            finally:
                self._held.discard(claimed.id)
        return done

    # ------------------------------------------------------------------ one task

    def execute(self, task: tasks.Claimed) -> None:
        step = 'refund'

        if task.is_recovery:
            self._recover(task, step)
            return

        payload = task.payload
        description = payload.get('description', '')
        order_ref = payload.get('order_ref', task.dedupe_key)
        order_total = int(payload.get('amount_cents', 0))

        # The model proposes. Note this happens OUTSIDE any transaction — it is a
        # network call, and db.tx() may re-execute its callable on a 40001.
        t = llm.triage(description=description, amount_cents=order_total,
                       order_ref=order_ref)

        if t.action != 'refund':
            self._finish_without_effect(task, t, step)
            return

        # Broad semantic recall: has anything like this happened before? Advisory only —
        # it can annotate the receipt with what licensed it, never authorize it.
        situation = f'{t.exception_kind}: {description}'
        sit_vec = embeddings.embed_list(situation)
        prior = db.tx(lambda cur: memory.recall(
            cur, tenant_id=task.tenant_id, embedding=sit_vec,
            memory_class=MemoryClass.SEMANTIC, context_key=None, k=3), readonly=True)
        licensed_by = prior[0].id if prior else None

        request_body = {'order_ref': order_ref, 'amount_cents': t.amount_cents,
                        'currency': 'USD', 'reason': t.exception_kind}

        try:
            prepared = db.tx(lambda cur: tasks.prepare(
                cur, task=task, agent_id=self.agent_id, step_name=step,
                provider_name='payments', operation='refunds.create',
                request_body=request_body, amount_cents=t.amount_cents,
                licensed_by_memory_id=licensed_by))
        except BudgetExceeded as e:
            # The spend cap is a hard boundary on the agent's blast radius. Hitting it
            # ends this task rather than retrying — a retry loop against a budget that
            # only a human can raise is just a busy wait that fills the journal.
            content = f'{situation} | refused: {e}'
            db.tx(lambda cur: tasks.dead_letter(
                cur, task=task, agent_id=self.agent_id, reason=str(e),
                memory_content=content, memory_embedding=embeddings.embed_list(content)))
            _log(f'  {task.dedupe_key}: BUDGET EXHAUSTED — {e}')
            return
        except AlreadyLive:
            _log(f'  {task.dedupe_key}: a live receipt already exists; recovering instead')
            self._recover(task, step)
            return

        if prepared.parked:
            # The transaction COMMITTED an approval row and moved the task to
            # AWAITING_APPROVAL with its lease released. Nothing more to do here.
            _log(f'  {task.dedupe_key}: parked on approval {prepared.approval_id} '
                 f'(${t.amount_cents / 100:.2f} exceeds policy authority)')
            return

        receipt = prepared.receipt
        _log(f'  {task.dedupe_key}: PREPARED {receipt.idempotency_key} '
             f'(${t.amount_cents / 100:.2f})')
        self._dispatch_and_settle(task, receipt, situation, sit_vec, first_try=True)

    # --------------------------------------------------------------- the recovery

    def _recover(self, task: tasks.Claimed, step: str) -> None:
        """Claimed a task that a dead worker left in ACTION_PREPARED."""
        payload = task.payload
        situation = (f'{payload.get("exception_kind", "unknown")}: '
                     f'{payload.get("description", "")}')
        sit_vec = embeddings.embed_list(situation)

        plan = db.tx(lambda cur: tasks.recover(
            cur, task=task, agent_id=self.agent_id, situation_embedding=sit_vec,
            step_name=step))

        _log(f'  {task.dedupe_key}: RECOVER -> {plan.action} '
             f'({len(plan.recalled)} memories) :: {plan.rationale}')

        if plan.action == 'REPLAN':
            db.tx(lambda cur: tasks.fail_retryable(
                cur, task=task, agent_id=self.agent_id, receipt=None,
                error='recovered with no outstanding effect; re-running', backoff_seconds=0))
            return

        if plan.action == 'ESCALATE':
            content = llm.summarize_recovery(
                task_type=task.task_type, step=step, recalled=plan.recalled,
                action='ESCALATE', rationale=plan.rationale)
            db.tx(lambda cur: tasks.dead_letter(
                cur, task=task, agent_id=self.agent_id,
                reason=plan.rationale, memory_content=content, memory_embedding=sit_vec))
            return

        # RESEND: same receipt, same derived idempotency key, same stored request body.
        # Re-synthesizing the body here would be the W7 bug; verify_fingerprint() is the
        # backstop if any future path does.
        receipt = plan.receipt
        assert receipt is not None
        tasks.verify_fingerprint(receipt, receipt.request_body)
        self._dispatch_and_settle(task, receipt, situation, sit_vec, first_try=False)

    # ------------------------------------------------------- dispatch then settle

    def _dispatch_and_settle(self, task: tasks.Claimed, receipt: tasks.Receipt,
                             situation: str, sit_vec: list[float], first_try: bool) -> None:
        """The only place in the system that talks to the outside world.

        Everything before this line is reversible. Everything after it is a fact about
        the world that AXIOM can only record, never undo.
        """
        db.tx(lambda cur: tasks.mark_dispatched(cur, receipt=receipt))

        try:
            result = provider.create_refund(
                idempotency_key=receipt.idempotency_key,
                order_ref=receipt.request_body['order_ref'],
                amount_cents=receipt.amount_cents or 0,
                currency=receipt.currency or 'USD',
                request_body=receipt.request_body,
                chaos_pre=self.chaos_pre, chaos_post=self.chaos_post)
        except ProviderError as e:
            if e.retryable:
                db.tx(lambda cur: tasks.fail_retryable(
                    cur, task=task, agent_id=self.agent_id, receipt=receipt,
                    error=str(e), backoff_seconds=min(30, 2 ** (task.attempt + 1))))
                _log(f'  {task.dedupe_key}: retryable provider error: {e}')
            else:
                content = (f'provider terminally rejected a {task.task_type} on '
                           f'{situation}: {e}')
                db.tx(lambda cur: tasks.dead_letter(
                    cur, task=task, agent_id=self.agent_id, reason=str(e),
                    memory_content=content, memory_embedding=sit_vec))
                _log(f'  {task.dedupe_key}: TERMINAL provider error: {e}')
            return

        verb = 'REPLAYED' if result.replayed else 'CREATED'
        # Every path that reaches here ended with exactly one real effect, whether this
        # worker created it or the provider replayed one an earlier worker had already
        # caused — so the outcome is RESOLVED either way. This was written as a ternary
        # whose branches were identical, which read like a decision and was not one.
        outcome = Outcome.RESOLVED
        content = (
            f'{situation} | recovered={not first_try} | provider {verb} '
            f'{result.provider_ref} for {receipt.amount_cents} cents under key '
            f'{receipt.idempotency_key}'
            if not first_try else
            f'{situation} | refund {result.provider_ref} for {receipt.amount_cents} cents '
            f'completed on the first attempt')

        db.tx(lambda cur: tasks.settle(
            cur, task=task, agent_id=self.agent_id, receipt=receipt,
            outcome_state=AttemptState.SUCCEEDED, task_state=TaskState.SUCCEEDED,
            response_body=result.body, provider_ref=result.provider_ref,
            http_status=result.status, memory_content=content,
            memory_embedding=embeddings.embed_list(content), memory_outcome=outcome,
            result={'provider_ref': result.provider_ref, 'replayed': result.replayed,
                    'amount_cents': receipt.amount_cents}))

        _log(f'  {task.dedupe_key}: SETTLED {verb} {result.provider_ref}'
             + ('  <- idempotent replay, no second refund' if result.replayed else ''))

    # ----------------------------------------------------------- non-acting paths

    def _finish_without_effect(self, task: tasks.Claimed, t: llm.Triage, step: str) -> None:
        """reship / escalate: no money moves, so there is no receipt to mint.

        Still writes a SEMANTIC memory, because "we saw this and chose not to refund" is
        exactly the kind of prior the next triage should be able to recall.
        """
        content = f'{t.exception_kind}: {task.payload.get("description", "")} -> {t.action} ({t.reason})'
        vec = embeddings.embed_list(content)
        state = TaskState.SUCCEEDED if t.action == 'reship' else TaskState.DEAD_LETTER

        def _apply(cur):
            memory.write(
                cur, tenant_id=task.tenant_id, memory_class=MemoryClass.SEMANTIC,
                context_key=ctx_exception(t.exception_kind), content=content,
                embedding=vec, outcome=Outcome.RESOLVED if t.action == 'reship'
                else Outcome.HUMAN_REQUIRED, source='system:execution',
                trust_level=Trust.FIRST_PARTY, confidence=t.confidence,
                mission_id=task.mission_id, task_id=task.id, agent_id=self.agent_id,
                actor=f'agent:{self.agent_id}')
            cur.execute("""
                UPDATE axiom_task SET state = %s, lease_owner = NULL, result = %s,
                       updated_at = now()
                WHERE id = %s AND lease_epoch = %s
            """, (str(state), f'{{"action":"{t.action}"}}', str(task.id), task.lease_epoch))
            if cur.rowcount != 1:
                raise LeaseLost(f'fence moved on {task.id}')
            events.append(cur, tenant_id=task.tenant_id, subject_type='task',
                          subject_id=task.id, event_type=f'task.{t.action}',
                          actor=f'agent:{self.agent_id}', from_state=str(TaskState.LEASED),
                          to_state=str(state), lease_epoch=task.lease_epoch,
                          mission_id=task.mission_id, task_id=task.id,
                          detail={'action': t.action, 'reason': t.reason,
                                  'exception_kind': t.exception_kind})
        db.tx(_apply)
        _log(f'  {task.dedupe_key}: {t.action.upper()} (no external effect)')


# ------------------------------------------------------------------------- entry

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='AXIOM worker agent')
    ap.add_argument('--shards', default='', help='comma-separated shard ids, e.g. 0,1,2,3')
    ap.add_argument('--ref', default=None, help='worker_ref (ECS task ARN in production)')
    ap.add_argument('--max-tasks', type=int, default=None)
    ap.add_argument('--idle-exit', action='store_true',
                    help='exit once the queue is drained (used by tests and the demo)')
    args = ap.parse_args(argv)

    shards = [int(s) for s in args.shards.split(',') if s.strip() != '']
    w = Worker(shards=shards, worker_ref=args.ref)

    def _sig(signum, _frame):
        _log(f'signal {signum}: draining')
        _stop.set()
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    w.start()
    try:
        n = w.run(max_tasks=args.max_tasks, idle_exit=args.idle_exit)
        _log(f'processed {n} tasks')
    finally:
        w.stop()
        db.close_pool()
        provider.close_pool()
    return 0


if __name__ == '__main__':
    sys.exit(main())
