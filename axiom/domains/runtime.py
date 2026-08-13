"""AXIOM :: the worker loop, with the workload taken out of it.

This is axiom/worker.py's Worker with every refund-specific line replaced by a call
through the Domain protocol. Same five protocols, same order, same crash windows, same
"dying at any line leaves a state the crash-window table already accounts for" property.
Run it:

    python -m axiom.domains.runtime --domain broadcast --idle-exit
    AXIOM_CHAOS_POST=0.3 python -m axiom.domains.runtime --domain broadcast

WHY THIS FILE EXISTS SEPARATELY FROM worker.py
----------------------------------------------
It should not, permanently. worker.Worker.execute() and this module are now the same
algorithm written twice — the refund one specialized, this one parameterized — and the
right end state is that worker.py keeps its process lifecycle (signals, heartbeat,
serverless deadlines) and delegates the body to `execute_task()` here. That change lands
in worker.py, which this task does not own, so it is written up in the handoff rather
than made. Until it lands, `axiom/domains/refunds.py` running through THIS loop and
worker.py's own loop must produce identical rows, which tests/test_domain2.py asserts
directly against the same protocol calls.

THE ONE THING THAT IS GENUINELY MISSING FROM THE CORE
-----------------------------------------------------
tasks.claim() has no task_type predicate. axiom_task HAS the column; the claim query and
the axiom_task_claimable partial index simply do not look at it, because until now there
was exactly one workload and every worker could do every job. A heterogeneous fleet — a
worker that can issue refunds but must never send email — therefore cannot express that
constraint in the claim, and this loop has to claim first and hand the task back if it
turns out to belong to someone else. That is CORRECT (the release is crash window W1:
the fence moved and nothing else happened) but it is wasted work, and under a queue that
is mostly another workload's it is a lot of wasted work. The fix is a claim-time
predicate plus the same predicate in the partial index. Reported, not made.
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

from .. import db, embeddings, events, memory, tasks
from ..config import settings
from ..models import (
    AttemptState, MemoryClass, Outcome, TaskState, Trust, ctx_exception,
)
from ..provider import ProviderCrash, ProviderError
from ..tasks import AlreadyLive, BudgetExceeded, LeaseLost
from . import Domain, for_task_type, known

# Process-level stop, set by SIGTERM/SIGINT — distinct from a Worker instance's own stop
# flag, for the reason spelled out at the top of axiom/worker.py: many DomainWorkers can
# live and die inside one warm serverless process, and a module global set by one of them
# would silently poison every later one.
_stop = threading.Event()


def _log(msg: str) -> None:
    print(f'[{time.strftime("%H:%M:%S")}] {msg}', flush=True)


class DomainWorker:
    def __init__(self, domain: Domain, shards: Sequence[int] | None = None,
                 worker_ref: str | None = None, chaos_post: float | None = None,
                 chaos_pre: float | None = None):
        self.domain = domain
        # Chaos is passed IN rather than read from the environment at dispatch time:
        # `settings` is frozen at import, so a caller that sets AXIOM_CHAOS_POST just
        # before starting a worker changes nothing.
        self.chaos_post = chaos_post
        self.chaos_pre = chaos_pre
        self.shards = list(shards) if shards else []
        self.worker_ref = worker_ref or f'{domain.name}-{uuid.uuid4().hex[:10]}'
        self.agent_id: uuid.UUID | None = None
        self.foreign_releases = 0
        self._held: set[uuid.UUID] = set()
        self._hb: threading.Thread | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self.agent_id = db.tx(lambda cur: tasks.register_agent(
            cur, worker_ref=self.worker_ref, shards=self.shards,
            build_sha=os.environ.get('AXIOM_BUILD_SHA'), region=settings.aws_region))
        _log(f'agent {self.agent_id} registered as {self.worker_ref} '
             f'domain={self.domain.name} shards={self.shards or "ALL"}')
        self._hb = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._hb.start()

    def _heartbeat_loop(self) -> None:
        while not (self._stop.is_set() or _stop.is_set()):
            if self._stop.wait(settings.heartbeat_seconds):
                break
            try:
                held = list(self._held)
                db.tx(lambda cur: tasks.heartbeat(cur, agent_id=self.agent_id,
                                                  held_task_ids=held))
            except Exception as e:
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
        done = 0
        idle_rounds = 0
        foreign_streak = 0
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

            if claimed.task_type != self.domain.task_type:
                # Someone else's workload. Put it back exactly as we found it and do not
                # count it as progress — see the module docstring.
                #
                # The release sets available_at = now(), which sorts the task to the BACK
                # of the claim order, so a queue that is mostly foreign work drains past
                # this worker rather than trapping it on one row.
                #
                # The backoff grows with the streak because several workers of the wrong
                # domain will otherwise thrash the same foreign rows: a measured run on a
                # cluster holding 37 leftover refund tasks logged 2,892 releases in 65
                # seconds, which is three thousand journal rows written into ANOTHER
                # workload's audit trail to learn nothing. It is not incorrect, it is
                # loud, and the volume is itself the argument for a task_type predicate
                # on claim(). Capped at the idle interval; reset the moment real work
                # arrives.
                self._release_foreign(claimed)
                foreign_streak += 1
                time.sleep(min(0.025 * foreign_streak, settings.poll_idle_ms / 1000.0))
                continue

            idle_rounds = 0
            foreign_streak = 0
            self._held.add(claimed.id)
            try:
                self.execute(claimed)
                done += 1
            except LeaseLost as e:
                _log(f'  lease lost on {claimed.dedupe_key}: {e}')
            except ProviderCrash as e:
                # RelayCrash subclasses this, so one clause covers every external world.
                _log(f'  !! {e}')
                if settings.crash_exits:
                    # os._exit skips every finally block and every atexit hook, which is
                    # the point: a real SIGKILL does the same, and any cleanup performed
                    # here would be cleanup a real crash never gets.
                    os._exit(9)
                raise
            finally:
                self._held.discard(claimed.id)
        return done

    def _release_foreign(self, task: tasks.Claimed) -> None:
        """Hand back a task this worker's domain does not own.

        Safe by construction: claim() bumped the fence and wrote nothing else, which is
        crash window W1 — the one window in which no external effect can exist. The
        release is fenced anyway, because a worker that had already lost the lease has no
        business editing the row it thought it held.

        AN ACTION_PREPARED TASK CANNOT BE DISOWNED, and the schema is right to say so.
        axiom_task_lease_ck asserts

            (state IN ('LEASED','ACTION_PREPARED')) = (lease_owner IS NOT NULL)

        so "orphaned mid-flight with no owner" is unrepresentable. The first version of
        this method set lease_owner = NULL unconditionally and took a CHECK violation on
        the first foreign task that was mid-recovery — a constraint doing exactly the job
        it was written for. The correct handoff for a mid-flight task is therefore to
        leave ownership alone and only make it claimable again: that is precisely the
        state a crashed worker leaves behind, which the lease and the fence already
        handle. It costs the successor one lease interval, which is one more reason the
        real fix is a task_type predicate on claim() rather than anything here.
        """
        self.foreign_releases += 1

        def _apply(cur):
            cur.execute("""
                UPDATE axiom_task
                SET state = CASE WHEN state = 'LEASED' THEN 'READY'::task_state
                                 ELSE state END,
                    lease_owner = CASE WHEN state = 'LEASED' THEN NULL
                                       ELSE lease_owner END,
                    available_at = now(), updated_at = now()
                WHERE id = %s AND lease_epoch = %s
            """, (str(task.id), task.lease_epoch))
            if cur.rowcount != 1:
                return
            events.append(cur, tenant_id=task.tenant_id, subject_type='task',
                          subject_id=task.id, event_type='task.released',
                          actor=f'agent:{self.agent_id}', from_state=str(task.state),
                          lease_epoch=task.lease_epoch, mission_id=task.mission_id,
                          task_id=task.id,
                          detail={'reason': 'task_type is not handled by this worker',
                                  'task_type': task.task_type,
                                  'worker_domain': self.domain.name})
        db.tx(_apply)

    # ------------------------------------------------------------------ one task

    def execute(self, task: tasks.Claimed) -> None:
        d = self.domain
        step = d.step_name
        # The domain sees the payload plus the task's own dedupe_key, which is the only
        # engine-owned field a workload legitimately needs: it is the caller's identifier
        # for the work, and it is the fallback name for the thing being acted on.
        payload = {**task.payload, 'dedupe_key': task.dedupe_key}

        if task.is_recovery:
            self._recover(task, payload)
            return

        # The model proposes. OUTSIDE any transaction — it is a network call, and db.tx()
        # may re-execute its callable on a 40001.
        intent = d.triage(payload)

        if not intent.acts:
            self._finish_without_effect(task, payload, intent)
            return

        situation = d.situation(payload, intent)
        sit_vec = embeddings.embed_list(situation)

        # Broad semantic recall: has anything like this happened before? Advisory only —
        # it can annotate the receipt with what licensed it, never authorize it.
        prior = db.tx(lambda cur: memory.recall(
            cur, tenant_id=task.tenant_id, embedding=sit_vec,
            memory_class=MemoryClass.SEMANTIC, context_key=None, k=3), readonly=True)
        licensed_by = prior[0].id if prior else None

        request_body = d.request_body(payload, intent)

        try:
            prepared = db.tx(lambda cur: tasks.prepare(
                cur, task=task, agent_id=self.agent_id, step_name=step,
                provider_name=d.provider_name, operation=d.operation,
                request_body=request_body,
                # The risk quantity goes into amount_cents and its unit code into
                # currency. For broadcast that reads 4800 / 'RCP' — four thousand eight
                # hundred recipients, in a column named for money. See domains/__init__.
                amount_cents=intent.risk_units, currency=d.risk.code,
                policy_id=d.policy_id, licensed_by_memory_id=licensed_by))
        except BudgetExceeded as e:
            # The cap is a hard boundary on the agent's blast radius, in whatever unit
            # this domain measures blast radius. Hitting it ends the task rather than
            # retrying: a retry loop against a budget only a human can raise is a busy
            # wait that fills the journal.
            content = f'{situation} | refused: {e}'
            db.tx(lambda cur: tasks.dead_letter(
                cur, task=task, agent_id=self.agent_id, reason=str(e),
                memory_content=content, memory_embedding=embeddings.embed_list(content)))
            _log(f'  {task.dedupe_key}: BUDGET EXHAUSTED — {e}')
            return
        except AlreadyLive:
            _log(f'  {task.dedupe_key}: a live receipt already exists; recovering instead')
            self._recover(task, payload)
            return

        if prepared.parked:
            # The transaction COMMITTED an approval row and moved the task to
            # AWAITING_APPROVAL with its lease released. Nothing more to do here.
            _log(f'  {task.dedupe_key}: parked on approval {prepared.approval_id} '
                 f'({d.risk.render(intent.risk_units)} exceeds policy authority)')
            return

        receipt = prepared.receipt
        _log(f'  {task.dedupe_key}: PREPARED {receipt.idempotency_key} '
             f'({d.risk.render(intent.risk_units)})')
        self._dispatch_and_settle(task, receipt, situation, sit_vec, first_try=True)

    # --------------------------------------------------------------- the recovery

    def _recover(self, task: tasks.Claimed, payload: dict) -> None:
        """Claimed a task a dead worker left in ACTION_PREPARED."""
        d = self.domain
        situation = d.recovery_situation(payload)
        sit_vec = embeddings.embed_list(situation)

        plan = db.tx(lambda cur: tasks.recover(
            cur, task=task, agent_id=self.agent_id, situation_embedding=sit_vec,
            step_name=d.step_name))

        _log(f'  {task.dedupe_key}: RECOVER -> {plan.action} '
             f'({len(plan.recalled)} memories) :: {plan.rationale}')

        if plan.action == 'REPLAN':
            db.tx(lambda cur: tasks.fail_retryable(
                cur, task=task, agent_id=self.agent_id, receipt=None,
                error='recovered with no outstanding effect; re-running', backoff_seconds=0))
            return

        if plan.action == 'ESCALATE':
            content = (f'agent died mid-{d.step_name} on a {task.task_type} task; '
                       f'recovery chose ESCALATE; {plan.rationale}')
            db.tx(lambda cur: tasks.dead_letter(
                cur, task=task, agent_id=self.agent_id,
                reason=plan.rationale, memory_content=content, memory_embedding=sit_vec))
            return

        # RESEND: same receipt, same derived idempotency key, same stored request body.
        # Re-synthesizing the body here would be the W7 bug; verify_fingerprint() is the
        # backstop for any future path that reconstructs a request.
        receipt = plan.receipt
        assert receipt is not None
        tasks.verify_fingerprint(receipt, receipt.request_body)
        self._dispatch_and_settle(task, receipt, situation, sit_vec, first_try=False)

    # ------------------------------------------------------- dispatch then settle

    def _dispatch_and_settle(self, task: tasks.Claimed, receipt: tasks.Receipt,
                             situation: str, sit_vec: list[float],
                             first_try: bool) -> None:
        """The only place in this loop that talks to the outside world.

        Everything before this line is reversible. Everything after it is a fact about
        the world that AXIOM can only record, never undo.
        """
        d = self.domain
        db.tx(lambda cur: tasks.mark_dispatched(cur, receipt=receipt))

        try:
            effect = d.dispatch(idempotency_key=receipt.idempotency_key,
                                request_body=receipt.request_body,
                                risk_units=receipt.amount_cents or 0,
                                chaos_pre=self.chaos_pre, chaos_post=self.chaos_post)
        except ProviderError as e:
            if e.retryable:
                db.tx(lambda cur: tasks.fail_retryable(
                    cur, task=task, agent_id=self.agent_id, receipt=receipt,
                    error=str(e), backoff_seconds=min(30, 2 ** (task.attempt + 1))))
                _log(f'  {task.dedupe_key}: retryable external error: {e}')
            else:
                content = (f'external system terminally rejected a {task.task_type} on '
                           f'{situation}: {e}')
                db.tx(lambda cur: tasks.dead_letter(
                    cur, task=task, agent_id=self.agent_id, reason=str(e),
                    memory_content=content, memory_embedding=sit_vec))
                _log(f'  {task.dedupe_key}: TERMINAL external error: {e}')
            return

        # Every path that reaches here ended with exactly ONE real effect, whether this
        # worker caused it or the external system replayed one an earlier worker had
        # already caused. RESOLVED either way.
        content = d.settled_memory(situation=situation,
                                   idempotency_key=receipt.idempotency_key,
                                   risk_units=receipt.amount_cents or 0,
                                   effect=effect, first_try=first_try)
        db.tx(lambda cur: tasks.settle(
            cur, task=task, agent_id=self.agent_id, receipt=receipt,
            outcome_state=AttemptState.SUCCEEDED, task_state=TaskState.SUCCEEDED,
            response_body=effect.body, provider_ref=effect.ref,
            http_status=effect.status, memory_content=content,
            memory_embedding=embeddings.embed_list(content),
            memory_outcome=Outcome.RESOLVED,
            # Key names kept as worker.py wrote them so anything already reading a task
            # result keeps working. 'amount_cents' here carries the domain's risk units.
            result={'provider_ref': effect.ref, 'replayed': effect.replayed,
                    'amount_cents': receipt.amount_cents}))

        verb = 'REPLAYED' if effect.replayed else 'CREATED'
        _log(f'  {task.dedupe_key}: SETTLED {verb} {effect.ref}'
             + ('  <- idempotent replay, no second effect' if effect.replayed else ''))

    # ----------------------------------------------------------- non-acting paths

    def _finish_without_effect(self, task: tasks.Claimed, payload: dict,
                               intent) -> None:
        """The domain decided not to touch the outside world.

        No effect means no receipt to mint — but it still writes a SEMANTIC memory,
        because "we saw this and chose not to act" is exactly the prior the next triage
        should be able to recall.
        """
        d = self.domain
        content = (f'{intent.kind}: {d.describe(payload)} -> {intent.action} '
                   f'({intent.reason})')
        vec = embeddings.embed_list(content)
        state = intent.terminal_state

        def _apply(cur):
            memory.write(
                cur, tenant_id=task.tenant_id, memory_class=MemoryClass.SEMANTIC,
                context_key=ctx_exception(intent.kind), content=content, embedding=vec,
                outcome=(Outcome.RESOLVED if state == TaskState.SUCCEEDED
                         else Outcome.HUMAN_REQUIRED),
                source='system:execution', trust_level=Trust.FIRST_PARTY,
                confidence=intent.confidence, mission_id=task.mission_id,
                task_id=task.id, agent_id=self.agent_id,
                actor=f'agent:{self.agent_id}')
            cur.execute("""
                UPDATE axiom_task SET state = %s, lease_owner = NULL, result = %s,
                       updated_at = now()
                WHERE id = %s AND lease_epoch = %s
            """, (str(state), f'{{"action":"{intent.action}"}}', str(task.id),
                  task.lease_epoch))
            if cur.rowcount != 1:
                raise LeaseLost(f'fence moved on {task.id}')
            events.append(cur, tenant_id=task.tenant_id, subject_type='task',
                          subject_id=task.id, event_type=f'task.{intent.action}',
                          actor=f'agent:{self.agent_id}',
                          from_state=str(TaskState.LEASED), to_state=str(state),
                          lease_epoch=task.lease_epoch, mission_id=task.mission_id,
                          task_id=task.id,
                          detail={'action': intent.action, 'reason': intent.reason,
                                  'kind': intent.kind})
        db.tx(_apply)
        _log(f'  {task.dedupe_key}: {intent.action.upper()} (no external effect)')


# ------------------------------------------------------------------------- entry

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='AXIOM domain worker')
    ap.add_argument('--domain', required=True,
                    help=f'one of: {", ".join(sorted(known()))}')
    ap.add_argument('--shards', default='', help='comma-separated shard ids')
    ap.add_argument('--ref', default=None, help='worker_ref')
    ap.add_argument('--max-tasks', type=int, default=None)
    ap.add_argument('--idle-exit', action='store_true',
                    help='exit once the queue is drained')
    # An UPPER BOUND ON ORPHAN LIFETIME, checked only between tasks. A chaos harness that
    # dies unexpectedly — a broken pipe from `| head`, a terminal closing — leaves its
    # spawned workers running, and orphaned workers on a shared cluster are worse than
    # noise: they keep claiming, they trip the invariant suite's exclusive-queue guard,
    # and they make the next run's numbers somebody else's. That happened during this
    # build. Never checked mid-task, for the reason worker.py gives: a worker that
    # abandoned a dispatch to respect a clock would be manufacturing the exact crash
    # window this system exists to survive.
    ap.add_argument('--deadline', type=float, default=None,
                    help='stop after this many seconds (checked between tasks only)')
    args = ap.parse_args(argv)

    domain = for_task_type(args.domain) or next(
        (d for d in known().values() if d.name == args.domain), None)
    if domain is None:
        ap.error(f'unknown domain {args.domain!r}; known: {", ".join(sorted(known()))}')

    shards = [int(s) for s in args.shards.split(',') if s.strip() != '']
    w = DomainWorker(domain, shards=shards, worker_ref=args.ref)

    def _sig(signum, _frame):
        _log(f'signal {signum}: draining')
        _stop.set()
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    w.start()
    try:
        n = w.run(max_tasks=args.max_tasks, idle_exit=args.idle_exit,
                  deadline_seconds=args.deadline)
        _log(f'processed {n} tasks '
             f'({w.foreign_releases} foreign tasks handed back)')
    finally:
        w.stop()
        db.close_pool()
    return 0


if __name__ == '__main__':
    sys.exit(main())
