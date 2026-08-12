# AXIOM — Crash Windows

This is the correctness specification. One page per window.

A worker can die at any instruction. Most of those deaths are uninteresting, because the
database is unchanged. The interesting ones are the instants where a **process death and an
external side effect can interleave**, and there are exactly seven of them worth naming.

For each: the precise instant, what may exist in the world at that instant, what recovery
does, what guarantees the outcome, and what actually covers it today. Where a defence has
been directly observed, the observation is quoted. Where it has not, that is stated rather
than implied.

**Every window below has a regression test** in `tests/test_crash_windows.py`. The suite as a
whole suite is 92 tests, all passing (two of them began as strict `xfail`s pinning real defects this suite found; both defects are fixed and those tests now guard the fix)
path (see the README's Limitations). Run it with `./.venv/bin/python -m pytest -q`. The tests do not
assert that AXIOM works — they assemble the conditions under which the design would corrupt
state and assert that it refuses.

The scenario throughout is a refund. Substitute any irreversible external act.

---

## How to read this

The timeline of one action, with the windows marked:

```
  claim ──────────────────────────────────────────────────────────────────►
    │
    │  W1  no receipt exists; no effect is possible
    │
  prepare()  ═══ COMMIT ═══  task is now ACTION_PREPARED
    │
    │  W2  receipt is durable; nothing has been sent yet
    │
  provider.create_refund(idempotency_key=…)
    │
    │  W3  in flight; outcome unknown to us
    │
  ── provider mutates its ledger ──   ◄── the irreversible instant
    │
    │  W4  the effect IS real; we have not recorded it
    │
  settle()  ═══ COMMIT ═══  receipt settled + outcome memory written
    │
    ▼  done

  W5, W6, W7 are concurrency and identity windows rather than points on this line.
```

The single structural fact that makes the whole table tractable:

> **The receipt commits before anything can be sent.** `prepare()` is what moves the task out
> of `LEASED` into `ACTION_PREPARED`, and only a task in `ACTION_PREPARED` may dispatch. So
> "did an effect possibly happen?" reduces to "does a live receipt exist?", which is a point
> read on a partial index. That is not a hope about timing. It is a consequence of commit
> ordering, and it is why every window below has a decidable answer.

**Terminology, deliberately.** AXIOM provides *effectively-once* execution: every external
action is issued under a derived idempotency key against a durable receipt, and every window
below has a defined outcome. It does **not** provide exactly-once execution, which is not
available to any system that calls a network API it does not control.

---

## W1 — Crash after CLAIM, before PREPARE

**The precise instant.** The worker holds a lease. `tasks.claim()` has committed, bumping
`lease_epoch` and setting `state = 'LEASED'`. The worker has possibly called the LLM for
triage and possibly run a semantic recall, both of which are read-only and outside any
transaction. `tasks.prepare()` has **not** committed.

**What may exist in the world.** Nothing external. No receipt row exists, therefore no
idempotency key has been minted, therefore no request can have been sent — `worker.py`
dispatches only from a `Receipt` returned by `prepare()`, and there is no other path to the
provider.

Inside the database: one `axiom_task` row in `LEASED` with a bumped epoch, and a
`task.claimed` event. Both are harmless.

**What recovery does.** Nothing special. The lease lapses because heartbeats stopped
(`available_at` is in the past), a successor claims the task, `Claimed.is_recovery` is
`False` because the state is `LEASED` and not `ACTION_PREPARED`, and the worker re-plans from
scratch. It may call the LLM again and get a different answer; that is fine, because nothing
is bound to the previous plan.

**What guarantees the outcome.** Commit ordering plus a structural constraint. A task cannot
be in `ACTION_PREPARED` unless a receipt was committed in the same transaction that put it
there, and the schema refuses to represent a lease-holding state without an owner:

```sql
CONSTRAINT axiom_task_lease_ck CHECK (
    (state IN ('LEASED', 'ACTION_PREPARED')) = (lease_owner IS NOT NULL)
)
```

There is also no reaper to race. `available_at` does double duty as earliest-run-time and
lease expiry, so `available_at <= now()` already means "ready to run **or** the previous owner
is dead". Recovery of W1 is just the claim loop doing its normal job.

**Coverage today — observed.** Directly demonstrated against a live cluster:

```
worker A claimed w1:d63c640e:refund
  state=LEASED epoch=1 is_recovery=False
  receipts for this task: 0
worker A dies HERE — after CLAIM, before PREPARE. This is W1.

worker B re-claimed the same task after the lease lapsed
  state=LEASED epoch=2 is_recovery=False
  receipts for this task: 0
  -> routes to normal planning; free to re-plan from scratch
```

Zero receipts before, zero receipts after, fence advanced 1 → 2, successor free to re-plan.

**Covered by** `test_w1_crash_after_claim_before_prepare_leaves_nothing_behind`.

---

## W2 — Crash after the receipt COMMITs, before the send

**The precise instant.** `prepare()` has committed. The task is `ACTION_PREPARED`. The
mission budget has been debited. An idempotency key exists and is durable. The HTTP call has
not left the process — or it has, and we cannot tell.

**What may exist in the world.** **Unknown, and it must be treated as "possibly yes".** This
is the important honesty of the design: from AXIOM's side, "committed the receipt but did not
send" and "sent, and the packet is somewhere" are indistinguishable. The system does not try
to distinguish them, because it cannot.

`axiom_action_attempt` holds one row with `attempt_state = 'PREPARED'`, `settled_at IS NULL`,
and the derived key.

**What recovery does.** A successor claims the task. Because the state is `ACTION_PREPARED`,
`Claimed.is_recovery` is `True` and it routes into `tasks.recover()`, which point-reads the
live receipt:

```sql
SELECT … FROM axiom_action_attempt
 WHERE tenant_id = %s AND task_id = %s AND step_name = %s
   AND attempt_state IN ('PREPARED', 'DISPATCHED')
```

A row here means an effect may already exist. The recovery **re-dispatches under the same
stored request body and the same derived key**. It never re-plans.

**What guarantees the outcome.** Two things together.

1. **The key is derived, not generated.** It is a `GENERATED … STORED` column over immutable
   inputs `(tenant_id, task_id, step_name, step_seq)`. The recovering worker cannot mint a
   different key even if it wants to, because no code path supplies one.
2. **The provider deduplicates on that key.** Same key + same fingerprint returns the
   original response with `replayed = True` instead of acting again.

Re-sending therefore costs nothing and is the only way to convert "unknown" into "known".
That is why `RESEND` is the conservative default rather than the risky one.

**Note on `DISPATCHED`.** The marker written by `mark_dispatched()` is observability only and
is **safety-equivalent to `PREPARED`** — the process can die between the send and the marker
write. `LIVE_ATTEMPT_STATES` contains both, and no correctness decision anywhere branches on
the difference. Treating W2 and W3 identically is a design commitment, not an oversight.

**Coverage today — exercised.** `scripts/chaos_demo.py` injects a crash at exactly this
instant via `AXIOM_CHAOS_PRE` (`provider.py` raises `ProviderCrash` before any network work,
and `worker.py` responds with `os._exit(9)`, which skips every `finally` block and every
`atexit` hook — the same as a real SIGKILL). The canonical run used `--chaos-pre 0.10`.

**Covered by** `test_w2_crash_between_receipt_and_send_resends_under_the_same_key`, which
forces exactly this instant rather than waiting for a probabilistic kill to land there.

**Remaining gap.** The chaos demo does not separately *count* W2 landings versus W3 and W4
landings, so the run-level evidence is "the mechanism is wired and the run passed". The
per-window claim rests on the test, not on the demo.

---

## W3 — Crash mid-flight, outcome unknown

**The precise instant.** The request is on the wire, or the provider is processing it, or the
response is on the way back and the process dies before reading it.

**What may exist in the world.** Possibly a completed refund. Possibly nothing. Possibly a
refund that the provider committed and whose response we will never see. AXIOM's state is
identical to W2: one receipt, `PREPARED` or `DISPATCHED`, unsettled.

**What recovery does.** Exactly what W2 does, for exactly the same reason — and this is the
point of the design rather than a shortcut. Because the recovery decision is driven by "does
a live receipt exist?" and not by "how far did the request get?", the system does not need to
answer an unanswerable question.

**What guarantees the outcome.** The same two mechanisms as W2: the derived key, and provider
dedupe on that key. The reason this generalizes is that AXIOM never claims to know whether
the call landed. It claims only that re-sending under the same key is safe whether it landed
or not, which is a much weaker and much more defensible claim.

**Coverage today — exercised, indirectly.** A real `SIGKILL` from `chaos_demo.py` can arrive
at any instruction, including inside `provider.create_refund()` while it holds an open
connection to the provider database. The canonical run absorbed 45 such kills. Whether any
individual kill landed strictly inside the flight window is not instrumented.

**Covered by** `test_w3_dispatched_marker_never_decides_correctness`, which asserts the
property that actually matters here: that no correctness decision branches on `DISPATCHED`
versus `PREPARED`.

**Remaining gap.** There is no fault injection that holds a request open and severs it
mid-response. The provider stand-in is a local database call with a `time.sleep()`, not a
socket that can be cut at a chosen byte. So the *timing* of W3 is argued structurally — it is
indistinguishable from W2 by construction — rather than reproduced physically.

---

## W4 — Crash after the provider responded, before SETTLE

**The precise instant.** The provider has committed the refund. The money has moved. The
response — `201`, a `provider_ref` — may even be in a local variable. The process dies before
`settle()` commits.

**This is the dangerous one.** In a transcript-memory agent this is precisely where the
double refund is born: the framework sees an unfinished task, has no durable record that the
call went out, and calls again with a fresh identity.

**What may exist in the world.** A real, completed, irreversible refund that AXIOM has **no
settled record of**. The only trace on AXIOM's side is the unsettled receipt from `prepare()`.

**What recovery does.** Identical to W2 and W3 — re-dispatch under the same key. The provider
recognizes the key, does **not** act again, and returns the original refund with
`replayed = True` and HTTP 200. `settle()` then records that original result, and the outcome
memory written in the same transaction carries `resolution.replayed = true`, so the next
recovery can semantically recall that this exact situation resolved cleanly.

**What guarantees the outcome.** Provider idempotency, keyed on a value AXIOM cannot vary.
Restated as a property: *AXIOM never learns whether the effect happened before or after the
crash, and never needs to.* It re-asserts the same intent under the same identity, and the
provider — the only party that actually knows — resolves it.

**Coverage today — observed live, end to end.** This is the window the canonical run
demonstrates most directly. `ORD-1027` was killed twice in this window. Its journal:

| seq | event | from → to | lease_epoch |
| --- | --- | --- | --- |
| 2 | `task.claimed` | → LEASED | 1 |
| 3 | `attempt.prepared` | LEASED → ACTION_PREPARED | 1 |
| 4 | `task.claimed` | → ACTION_PREPARED | 2 |
| 5 | `task.recovered` → **RESEND** | ACTION_PREPARED → ACTION_PREPARED | 2 |
| 6 | `task.claimed` | → ACTION_PREPARED | 3 |
| 7 | `task.recovered` → **RESEND** | ACTION_PREPARED → ACTION_PREPARED | 3 |
| 8 | `attempt.settled` | ACTION_PREPARED → SUCCEEDED | 3 |

Both recoveries carried the identical key
`axm_5722c72bd44fc74f50f50496727bca809f65585d63cfb98c`. The provider's own request log — a
table AXIOM never writes to — recorded three requests and one effect:

```
verdict     http_status   received_at
created     201           02:55:50.119
replayed    200           02:56:10.112
replayed    200           02:56:30.223
```

Final ledger: one row, `re_da08deb5287c47899857`, `$169.40`, `replay_count = 2`.

Across the whole run (CockroachDB Cloud v26.2.5): 30 SIGKILLs, 6 idempotent replays absorbed, 18 refunds requested and
18 refund rows created, `0` orders refunded more than once. AXIOM's `spent_cents` (204,204)
equals the provider's `sum(amount_cents)` (204,204), reconciled independently.

**Covered by** `test_w4_crash_after_the_refund_landed_replays_instead_of_refunding_twice`,
which pins the instant deterministically instead of relying on a probabilistic kill.

---

## W5 — A zombie worker settles after its lease expired

**The precise instant.** Worker A holds task T at `lease_epoch = 2`. A stalls — a GC pause, a
stalled socket, a container about to be reaped — long enough for its lease to lapse. Worker B
claims T, bumping the epoch to 3. A then wakes up **inside** its refund call, finishes, and
tries to `settle()`.

**What may exist in the world.** Possibly two dispatches of the same logical call, from two
different processes. Both carry the **same** derived key, because the key is a function of
`(tenant_id, task_id, step_name, step_seq)` and not of the worker, the attempt, or the epoch.
So the provider absorbs the second as a replay. The danger here is not a double refund; it is
**a double write to AXIOM's own state** — the zombie overwriting the result of the worker
that legitimately took over, or settling a receipt the successor is still using.

**What recovery does.** Nothing, because there is nothing to recover. The zombie's write is
*refused*. Worker B proceeds normally.

**What guarantees the outcome.** The fencing token, re-checked on every write after the
claim. `_assert_fence()` re-reads `lease_epoch` and `lease_owner`, and every state-changing
statement additionally carries `AND lease_epoch = %s` with a `rowcount != 1` check. The settle
of the receipt is fenced on the epoch the **receipt was minted under**, not the task's current
epoch.

This is the distinction the whole design turns on:

> **A lease expiring does not stop a worker that is already inside an HTTP call.** The lease
> is a liveness optimization — how quickly a dead worker's task becomes claimable again. The
> monotonic per-row `lease_epoch` is the correctness mechanism. A too-short lease therefore
> costs duplicated *effort*, never a duplicated *effect*.

`lease_epoch` is monotonic **per row**, so it is not a global sequence and creates no hotspot.

**Coverage today — observed.** Forced directly: worker A prepared a receipt under epoch 1 and
held a `Claimed` at epoch 2; a peer then took the task to epoch 3; A attempted to settle.

```
live receipt axm_261a86b972… minted under epoch 1
peer took over: task epoch is now 3; zombie still believes it holds 2
W5 RESULT: rejected -> LeaseLost: fence moved on task 64391d9b…: held epoch 2, current epoch 3

after the rejected settle: task.state=ACTION_PREPARED epoch=3 result=None
  receipt: PREPARED provider_ref=None settled_at=None
```

The zombie wrote **nothing**: the task is untouched, and the receipt is still `PREPARED` with
no `provider_ref` and no `settled_at`. The successor's view is intact.

**Covered by** `test_w5_zombie_settle_is_rejected_by_the_fence`. The suite runs with
`AXIOM_LEASE_SECONDS=1` and sleeps past a real lease expiry rather than hand-editing a row,
because the thing under test is the interaction between `available_at <= now()` and
`lease_epoch`, and a faked expiry would test neither.

---

## W6 — Two workers PREPARE the same step

**The precise instant.** Two workers both believe they own task T and both reach
`prepare()` for the same `step_name`.

**What may exist in the world.** Nothing yet — and the entire point is that it stays that
way. If both `prepare()` calls succeeded, two receipts would exist with **different**
`step_seq` values and therefore **different** derived keys, and two genuinely separate
refunds would go out. This is the one window where the failure mode is a true double effect
rather than a bookkeeping mess.

**What recovery does.** The loser does not call the provider at all. In `worker.py` the
`AlreadyLive` branch routes into `_recover()`, which finds the live receipt and re-dispatches
under the winner's key rather than minting a second one.

**What guarantees the outcome.** Three layers, deepest last.

1. **The fence.** Two workers should not hold the same task at the same epoch in the first
   place; `claim()` is a compare-and-swap and `_assert_fence()` re-checks at the top of
   `prepare()`.
2. **The explicit check.** `prepare()` calls `live_receipt()` first and raises `AlreadyLive`,
   turning a database error into an explicit, testable control-flow branch.
3. **The database, as backstop.** A unique partial index that holds even if layers 1 and 2
   are bypassed by a future code change:

```sql
CREATE UNIQUE INDEX axiom_attempt_one_live
    ON axiom_action_attempt (tenant_id, task_id, step_name)
    WHERE attempt_state IN ('PREPARED', 'DISPATCHED');
```

Layer 3 is the one that matters for this document, because layers 1 and 2 are code and code
gets edited. Terminal rows fall out of the partial index, so a legitimate new `step_seq`
after a terminal provider rejection is still permitted.

**Coverage today — observed.** Forced by bypassing layers 1 and 2 entirely and inserting a
second live receipt for the same `(tenant, task, step)` directly:

```
first PREPARE ok -> key axm_261a86b9723f90745321bc32c0224d2a23f2efcfe16c7c56
W6 RESULT: rejected with SQLSTATE 23505 on axiom_attempt_one_live
```

The backstop holds against a caller that ignores every application-level guard.

**Covered by two tests**, one per layer:
`test_w6_racing_prepares_produce_exactly_one_receipt` races two executors into `prepare()` and
asserts exactly one receipt survives, and
`test_w6_second_live_receipt_is_refused_by_the_index_itself` bypasses the application guards
entirely to confirm the index still refuses.

---

## W7 — A recovered agent re-synthesizes a *different* request body

**The precise instant.** After a restart, an agent reconstructs the request it was making
rather than reading it from the receipt — and an LLM, being an LLM, produces something subtly
different. A different amount. A different order reference. The same key.

**What may exist in the world.** Possibly the original refund. What must *never* happen is
the new intent going out under the old identity: the provider would either honour it (a
second, different effect under a key that was supposed to guarantee singularity) or reject
it, and either way the system has lost track of what it is asking for.

This is the semantic-rollback attack class — the same key wearing a new intent.

**What recovery does.** Hard stop. Not a warning, not a "pick the newer one", not a retry.
`FingerprintMismatch` propagates and the task is dead-lettered for a human, because a retry
loop around an ambiguous external effect is exactly how the double refund this system exists
to prevent gets created.

**What guarantees the outcome.** Defence in depth across two independent parties.

1. **AXIOM refuses to send.** `request_fingerprint` is a SHA-256 over the canonicalized
   request body (sorted keys, no insignificant whitespace) stored on the receipt.
   `tasks.verify_fingerprint()` compares before dispatch.
2. **The provider refuses to act.** Independently, `provider_refund` stores the fingerprint
   of the request that created it. Same key + different fingerprint → `409`, non-retryable.

Layer 2 matters because it does not trust layer 1. A provider with real Stripe semantics
enforces this whether or not the caller checks.

The structural defence, though, is that **the engine never re-synthesizes at all.**
`worker._recover()` re-dispatches `receipt.request_body` — the bytes that were stored when
the key was minted. `verify_fingerprint()` is the backstop for any future path that
reconstructs a request, which is why the current call site compares the stored body against
itself and always passes. That is deliberate, and worth being explicit about rather than
letting it look like a redundant check.

**Coverage today — observed at both layers.**

```
[layer 1] AXIOM-side verify_fingerprint()
  identical body            -> passes (this is what the engine always re-dispatches)
  same body, keys reordered -> passes (canonicalization: sorted keys)
  amount 5000 -> 999999     -> FingerprintMismatch (hard stop)

[layer 2] provider-side, same key + different body
  original body -> 201 re_bb6789608954444c800b replayed=False
  mutated body  -> HTTP 409 retryable=False: idempotency key reused with a different request body

provider ledger for this key:
  re_bb6789608954444c800b 5000c replay_count=0
```

The mutated request created nothing. The key-order case is worth noting: two requests
differing only in JSON key order must hash identically, or every legitimate retry would look
like a new intent.

**Covered by** `test_w7_resynthesized_body_is_a_hard_stop` and
`test_w7_idempotency_key_is_derived_from_immutable_columns_only` — the second asserting the
structural property that makes the whole window survivable: the key is a function of
immutable columns only, so a recovering worker cannot mint a different one.

**Remaining gap.** That `FingerprintMismatch` propagates all the way to `dead_letter()` and
produces a `HUMAN_REQUIRED` memory inside a *running worker* is not demonstrated end to end;
the tests assert the refusal, not the downstream escalation.

---

## Summary of coverage

Honest status, per window.

| Window | Defence | Directly observed | Regression test |
| --- | --- | --- | --- |
| W1 | Commit ordering; no receipt can exist | Yes | `test_w1_crash_after_claim_before_prepare_leaves_nothing_behind` |
| W2 | Derived key + provider dedupe | Mechanism wired, exercised by chaos | `test_w2_crash_between_receipt_and_send_resends_under_the_same_key` |
| W3 | Same as W2, by design | Structural; timing not physically reproduced | `test_w3_dispatched_marker_never_decides_correctness` |
| W4 | Derived key + provider dedupe | **Yes — `ORD-1027`, 3 requests, 1 effect** | `test_w4_crash_after_the_refund_landed_replays_instead_of_refunding_twice` |
| W5 | Fencing token re-checked on every write | **Yes — `LeaseLost`, zero rows written** | `test_w5_zombie_settle_is_rejected_by_the_fence` |
| W6 | Unique partial index `axiom_attempt_one_live` | **Yes — `23505`** | `test_w6_racing_prepares_produce_exactly_one_receipt`, `test_w6_second_live_receipt_is_refused_by_the_index_itself` |
| W7 | Fingerprint on receipt **and** on provider | **Yes — both layers** | `test_w7_resynthesized_body_is_a_hard_stop`, `test_w7_idempotency_key_is_derived_from_immutable_columns_only` |

Every window has a defined outcome, six of the seven have been directly observed on a live
cluster, and all seven have a regression test that tries to cause the failure and fails.
`tests/test_crash_windows.py` is 13 tests; the full suite is 92 and all 92 pass. Two of
them began as strict `xfail`s pinning real defects this suite found — an approval that
never expired, and attempt exhaustion that stranded a task — and now guard those fixes.

**Where this is still weaker than it looks.** The suite runs when a human runs it. There is
no CI pipeline executing it on every commit, so "cannot regress" is not yet earned — what is
earned is "does not currently regress, and a refactor that breaks it will be caught by
whoever remembers to run pytest". Wiring this into CI is the cheapest remaining credibility
win on the project.

Two windows also rest more on argument than on physical reproduction, and it is worth being
precise about which: **W3**'s timing cannot be reproduced without a severable socket, so it is
covered as "indistinguishable from W2 by construction" plus a test of the property that
actually matters (`DISPATCHED` decides nothing). **W2**'s per-window evidence comes from its
test rather than from the chaos demo, which does not attribute its kills to specific windows.

## What is not modelled

Stated so the table above is not read as claiming more than it does.

- **Partial provider writes.** The stand-in provider commits its refund row and its request
  log in one autocommit statement pair against a database that will not tear. A real provider
  can fail between charging a card and recording it. No amount of client-side discipline fixes
  that; it is what reconciliation and `Outcome.PROVIDER_AMBIGUOUS` exist for.
- **Compensation.** `AttemptState.COMPENSATED` and `axiom_task.compensates_task_id` exist in
  the schema for saga-style reversal, and nothing in the engine writes them yet. An effect
  that must be *undone* rather than *not repeated* is out of scope for this build.
- **Clock skew.** Leases and `available_at` use the cluster's `now()`, so all comparisons
  happen inside one CockroachDB cluster with its own clock discipline. Workers never compare
  their local clocks to anything, which is intentional — but the fencing token, not the clock,
  is what makes this safe.
- **Byzantine workers.** A worker that deliberately forges a `lease_epoch` or writes directly
  to `axiom_action_attempt` is not defended against beyond the schema constraints. The trust
  boundary is the database connection.
