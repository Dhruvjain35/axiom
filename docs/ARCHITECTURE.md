# AXIOM — Architecture

The deep version. Every claim here is traceable to a file; where a design decision has a
`WHY` comment in `db/001_schema.sql`, that comment is the source and this document does not
contradict it.

---

## 1. The shape of the system

```
                                   Operator
                                      │
                          create mission / decide approvals
                                      │
                                      ▼
     ┌─────────────────────────────────────────────────────────────────────┐
     │                          WORKER AGENTS                              │
     │  axiom/worker.py — the process you are meant to kill                │
     │                                                                     │
     │    claim ──► (recover | triage) ──► prepare ──► dispatch ──► settle │
     │      │              │                  │           │           │    │
     │      │              │                  │      NO TXN HERE      │    │
     └──────┼──────────────┼──────────────────┼───────────┼───────────┼────┘
            │              │                  │           │           │
            │  Embeddings + triage            │           │           │
            │  offline sketch, not Bedrock    │           │           │
            │  (axiom/embeddings.py, llm.py)  │           │           │
            │                                 │           │           │
            ▼              ▼                  ▼           │           ▼
     ┌───────────────────────────────────────────────────────────────────────┐
     │                            CockroachDB                                │
     │                     SERIALIZABLE, every transaction                   │
     │                                                                       │
     │  ┌─────────────────────┐  ┌──────────────────────┐  ┌──────────────┐  │
     │  │ axiom_task          │  │ axiom_action_attempt │  │ axiom_policy │  │
     │  │ EXECUTION memory    │  │ idempotency receipts │  │ PROCEDURAL   │  │
     │  │ state machine       │  │ GENERATED key        │  │ versioned,   │  │
     │  │ lease_epoch fence   │  │ one live per step    │  │ 1 ACTIVE     │  │
     │  │ CONSTRAINS          │  │ CONSTRAINS           │  │ AUTHORIZES   │  │
     │  └─────────────────────┘  └──────────────────────┘  └──────────────┘  │
     │                                                                       │
     │  ┌─────────────────────────────────────┐  ┌────────────────────────┐  │
     │  │ axiom_memory                        │  │ axiom_event            │  │
     │  │ EPISODIC + SEMANTIC — ADVISES       │  │ append-only journal    │  │
     │  │ VECTOR(1024), 2× C-SPANN index      │  │ gap-free per subject   │  │
     │  │ retrieval_class = admissibility     │  │ hash-sharded timeline  │  │
     │  │   folded into the index prefix      │  │ never updated/deleted  │  │
     │  └─────────────────────────────────────┘  └────────────────────────┘  │
     │                                                                       │
     │  ┌─────────────────────┐  ┌──────────────────────┐  ┌──────────────┐  │
     │  │ axiom_approval      │  │ axiom_agent          │  │ axiom_mission│  │
     │  │ single-use token    │  │ heartbeat, shards    │  │ hard budget  │  │
     │  └─────────────────────┘  └──────────────────────┘  └──────────────┘  │
     └───────────────────────────────────┬───────────────────────────────────┘
                                         │
                    ══════════ TRANSACTION BOUNDARY ══════════
                    the provider CANNOT be enlisted. This is the
                    entire problem the system exists to solve.
                                         │
                                         ▼
                       ┌──────────────────────────────────┐
                       │  payment provider                │
                       │  separate database, separate     │
                       │  connection, autocommit          │
                       │                                  │
                       │  provider_refund                 │
                       │    UNIQUE (idempotency_key)      │
                       │    replay_count                  │
                       │  provider_request_log            │
                       │    every request it ever saw     │
                       └──────────────────────────────────┘
```

The dashed line is the thesis. Everything above it is transactional and recoverable.
Everything below it is a fact about the world that AXIOM can only record, never undo. The
job of the architecture is to make the crossing survivable.

---

## 2. The task state machine

```
                    enqueue
                       │
                       ▼
   PENDING ──dep met──► READY ◄────────────────┐
                          │                    │ fail_retryable
                          │ claim (epoch++)    │ (attempt++, backoff
                          ▼                    │  written into available_at)
                       LEASED ─────────────────┤
                          │                    │
        ┌─────────────────┼────────────────────┴──────────┐
        │                 │                               │
        │ policy refuses  │ prepare()                     │ triage says
        │ to self-auth    │ receipt COMMITS               │ reship/escalate
        ▼                 ▼                               ▼
  AWAITING_APPROVAL   ACTION_PREPARED                 SUCCEEDED /
  lease RELEASED      ══════════════════              DEAD_LETTER
  available_at =      EXTERNAL EFFECT                 (no receipt was
    approval expiry   AUTHORIZED / may be              ever minted)
        │             IN FLIGHT RIGHT NOW
        │                 │
        │ human decides   │  dispatch (no txn) → settle (fenced txn)
        │ + token burned  │  or: recover → RESEND / ESCALATE / REPLAN
        └────────►────────┤
                          ▼
              SUCCEEDED / FAILED / CANCELLED / DEAD_LETTER   (terminal)
```

Two rules the code may never break, restated from `axiom/tasks.py`:

**I1. No external side effect is authorized while a task is `LEASED`.** The receipt commits
*first*, which is what moves the task to `ACTION_PREPARED`; only then may a call go out. A
crash before the receipt therefore *cannot* have caused an effect. That is not a hope about
timing, it is a consequence of commit ordering.

**I2. `ACTION_PREPARED` means "an effect may exist in the world."** Recovery from that state
is never "start over". It is "re-dispatch under the same key, or reconcile, or escalate".

`CLAIMABLE_STATES` in `axiom/models.py` is the single source of truth for both the claim
predicate and the partial index predicate, interpolated into the SQL rather than retyped.
If those two ever drift apart the optimizer stops using `axiom_task_claimable` and the claim
loop silently becomes a full table scan — correct results, catastrophic performance, no
error anywhere.

---

## 3. The five protocols

Each is one transaction unless stated otherwise. Every one goes through `db.tx()`, which
retries `40001` with exponential backoff and full jitter. Under SERIALIZABLE a serialization
failure is not an error condition; it is the system working.

`db.tx()` takes a **callable**, not a context manager, and the distinction is load-bearing:
a retry has to re-execute the whole body, and a context manager physically cannot re-run the
block it wraps. Everything inside the callable must therefore be idempotent on replay — no
HTTP calls, no file writes. That constraint is exactly why DISPATCH is the one protocol with
no transaction around it.

### (a) CLAIM — take ownership, bump the fence

One statement, compare-and-swap on the fencing token (`axiom/tasks.py:claim`):

```sql
WITH candidate AS (
    SELECT id, lease_epoch
    FROM axiom_task
    WHERE shard = ANY(%(shards)s::INT2[])       -- or `true` for a stealing worker
      AND available_at <= now()
      AND state IN ('READY','LEASED','ACTION_PREPARED','AWAITING_APPROVAL')
      AND attempt < max_attempts
    ORDER BY available_at ASC
    LIMIT 1
)
UPDATE axiom_task t
SET lease_epoch  = t.lease_epoch + 1,
    lease_owner  = %(agent)s,
    available_at = now() + %(lease)s::INTERVAL,
    state        = CASE WHEN t.state IN ('READY','AWAITING_APPROVAL')
                        THEN 'LEASED'::task_state ELSE t.state END,
    updated_at   = now()
FROM candidate c
WHERE t.id = c.id AND t.lease_epoch = c.lease_epoch
RETURNING t.id, t.tenant_id, t.mission_id, t.task_type, t.dedupe_key, t.state,
          t.lease_epoch, t.attempt, t.max_attempts, t.payload,
          t.policy_id, t.policy_version;
```

Three things to notice.

**The `CASE` preserves `ACTION_PREPARED`.** Claiming a task that a dead worker left mid-act
does *not* reset it to `LEASED`. The claim returns it still in `ACTION_PREPARED`, which is
how `Claimed.is_recovery` knows to route into `recover()` instead of re-planning. Resetting
the state here would erase the single most important fact in the system.

**`available_at` does double duty** as earliest-run-time and lease expiry, so the predicate
`available_at <= now()` means "ready to run **or** the previous owner is dead". That is what
lets AXIOM have no reaper process — and a reaper matters here, because it would be a
periodic large multi-row transaction landing on exactly the rows the claim loop is trying to
scan.

**Zero rows returned is normal.** Either nothing is claimable, or another worker won the
CAS. The caller must treat both as "try again", never as an error.

### (b) PREPARE — mint the receipt

The transaction that authorizes an irreversible act. Order matters and is not negotiable
(`axiom/tasks.py:prepare`):

1. Re-check the fence (`_assert_fence`).
2. Refuse if a live receipt already exists for this `(task, step)` → `AlreadyLive`.
3. Load and **pin** the policy version to the task, so an entire attempt is judged against
   one policy version even if a new one is published mid-flight.
4. Authority check. If the machine may not self-authorize, first try to burn a single-use
   approval token; failing that, create the approval row, move the task to
   `AWAITING_APPROVAL`, release the lease, and **return** — `PrepareResult(receipt=None,
   approval_id=...)`.
5. Debit the mission budget.
6. `INSERT` the receipt. The idempotency key is generated by the database.
7. Journal, and move the task to `ACTION_PREPARED`.

Only after this commits may a provider call go out.

Step 4 returns rather than raises, and there is scar tissue behind that. Parking for approval
used to raise `NeedsApproval`, which propagated out of `db.tx()`, so the connection context
manager **rolled back** — discarding the approval row and the `AWAITING_APPROVAL` transition
the same transaction had just written. The task snapped back to `READY`, was re-claimed,
parked again, and looped forever while `axiom_approval` stayed empty. *An exception is a
fine way to abort a transaction and a terrible way to return a value from one.*

Step 5 expresses the cap **twice**, on purpose:

```sql
UPDATE axiom_mission
SET spent_cents = spent_cents + %(amt)s, updated_at = now()
WHERE id = %(mission)s AND tenant_id = %(tenant)s
  AND spent_cents + %(amt)s <= budget_cents
RETURNING spent_cents, budget_cents;
```

plus, in the schema:

```sql
CONSTRAINT axiom_mission_budget_ck CHECK (spent_cents >= 0 AND spent_cents <= budget_cents)
```

The `WHERE` clause is the graceful path: it declines the debit and leaves the transaction
usable, so the caller can dead-letter the task with a real explanation. The `CHECK` is the
guarantee: it holds even against a future code path that forgets the predicate, at the cost
of aborting the transaction. **Control flow from the predicate, correctness from the
constraint — never the reverse**, since a constraint violation poisons the transaction it
fires in.

That mission row is deliberately contended. Two workers racing to spend the last $50 cannot
both win; one takes a 40001 and re-reads. A budget *is* a shared resource and serializing on
it is the point, not a bug. If it ever became the bottleneck the standard fix is a sharded
counter, which trades exactness at the boundary for throughput — and "we chose to serialize
here" is a better answer than an eventually-consistent budget that lets you overspend.

### (c) DISPATCH — no transaction, by necessity

The only place in the system that talks to the outside world
(`axiom/worker.py:_dispatch_and_settle`). Everything before this line is reversible;
everything after it is a fact.

`attempt_state = 'DISPATCHED'` is written best-effort before the call and is **safety-
equivalent to `PREPARED`**. The process can die between the send and that write, so no
correctness decision may ever branch on the difference. It exists so a human watching a
dashboard can tell "about to call" from "called". `LIVE_ATTEMPT_STATES` in `models.py`
contains both, and every query that asks "might an effect exist?" uses that tuple.

### (d) SETTLE — record the outcome and the memory, together

Fenced, one transaction (`axiom/tasks.py:settle`):

```
BEGIN;
  _assert_fence(task, agent, epoch)
  UPDATE axiom_action_attempt SET attempt_state, response_body, provider_ref,
         http_status, settled_at = now()
    WHERE id = %s AND lease_epoch = %s        -- rowcount != 1  =>  LeaseLost
  UPDATE axiom_task SET state = terminal, result, lease_owner = NULL
    WHERE id = %s AND lease_epoch = %s        -- rowcount != 1  =>  LeaseLost
  INSERT axiom_memory   ← the outcome memory, embedded, SAME TRANSACTION
  INSERT axiom_event
COMMIT;
```

Writing the outcome memory here is not a nice-to-have. Co-committing it with the terminal
state transition is what makes it **impossible for memory to disagree with execution state**
— there is no interval in which the refund is recorded but the lesson is not, or vice versa.
Moving it to a background job for throughput would destroy the entire differentiator.

Note the settle updates the receipt `WHERE lease_epoch = receipt.lease_epoch` — the epoch the
*receipt* was minted under, not the task's current one. That is the W5 defence: a zombie
holding a stale epoch cannot overwrite the result of the worker that legitimately took over.

The embedding is computed **before** the transaction opens. `db.tx()` re-executes its
callable on 40001, and an embedding call inside it would re-hit Bedrock on every retry. Every
function in `memory.py` therefore takes a vector, never a string to be embedded.

### (e) RECOVER — the fused transaction

The function the entire project exists to make possible (`axiom/tasks.py:recover`):

```
BEGIN;
  _assert_fence(task, agent, epoch)

  -- what did the dead worker already do?
  SELECT … FROM axiom_action_attempt
   WHERE tenant_id = $1 AND task_id = $2 AND step_name = $3
     AND attempt_state IN ('PREPARED','DISPATCHED')        -- partial-index point read

  -- what happened the LAST time an agent died at this exact state?
  SELECT …, embedding <=> $vec::VECTOR(1024) AS distance
    FROM axiom_memory
   WHERE tenant_id = $1
     AND memory_class = 'EPISODIC'
     AND context_key = 'state:ACTION_PREPARED'
     AND retrieval_class = 'ACTIONABLE'
   ORDER BY embedding <=> $vec::VECTOR(1024)
   LIMIT $k * 4                                            -- over-fetch, see §5

  -- aggregate the recalled outcomes into a decision
  INSERT axiom_event ('task.recovered', action, rationale, evidence ids)
COMMIT;
then act: re-dispatch under the SAME key / escalate / re-plan
```

Read the receipt **and** the semantic memory **and** commit the transition together. That is
the fusion. If a refactor ever splits this into two transactions, the project's central claim
becomes false.

The decision is deliberately conservative and asymmetric:

```python
votes  = [r.outcome for r in recalled]
danger = sum(1 for v in votes if v in (Outcome.DUPLICATE_EFFECT, Outcome.HUMAN_REQUIRED))
if danger and danger >= len(votes) / 2:
    plan = RecoveryPlan('ESCALATE', ...)
else:
    plan = RecoveryPlan('RESEND', ...)
```

A live receipt means an effect *may* exist, and the safe default is always to re-send under
the same key — the provider dedupes, so re-sending costs nothing and is the only way to
convert "unknown" into "known". **Memory can override that default in one direction only:
toward escalation.** Memory may never talk the system *into* an act. This is why
`Outcome` is a constrained enum rather than free text: the recovery decision aggregates over
that column, and free text would mean asking an LLM to interpret prose before deciding
whether to re-dispatch a $300 refund. A memory may only vote in ways the state machine
already understands.

No live receipt means the previous owner settled and died before transitioning, or the
receipt reached a terminal state. Nothing is outstanding, so `REPLAN` is safe.

---

## 4. Index design

### 4.1 The claim index is PARTIAL, and that is the answer to a documented anti-pattern

CockroachDB's own "Understand hotspots" guidance names queues specifically: they *"require
data to be ordered by write, which necessitates indexing in a way that is likely to create a
hotspot"*, and deleting rows as they are read *"tends to accumulate an ordered set of garbage
data behind the live data."*

AXIOM's answer is three-part, and all three are visible in one index:

```sql
CREATE INDEX axiom_task_claimable
    ON axiom_task (shard ASC, available_at ASC)
    STORING (state, tenant_id, mission_id, lease_epoch, lease_owner,
             attempt, max_attempts, task_type)
    WHERE state IN ('READY', 'LEASED', 'ACTION_PREPARED', 'AWAITING_APPROVAL');
```

- **PARTIAL** — terminal tasks fall *out* of the index. The claim index shrinks as work
  completes instead of growing forever.
- **We never `DELETE` a task.** State transitions only. So no MVCC tombstones accumulate
  behind the head of the queue. The partial predicate gives us the shrinkage that a `DELETE`
  would give, without the garbage.
- **Prefixed by `shard`**, so the head of the queue is N ranges instead of 1.

`STORING` keeps the inner `SELECT` index-only, so `attempt < max_attempts` is evaluated
without an index join back to the primary index. That matters beyond latency: the optimizer
refuses to push `LIMIT` into an index join when `SKIP LOCKED` is in play, so a covering index
is what keeps the optional `SKIP LOCKED` claim path viable at all.

`payload` is deliberately **not** stored. A fat JSONB in the claim index doubles storage, and
returning it would push the claim result toward the 16 KiB results-buffer ceiling past which
CockroachDB can no longer auto-retry the statement server-side.

Related, and easy to undo by accident: **`axiom_task` has no `FAMILY` declarations, ever.**
Splitting hot lease columns from the cold JSONB payload is a normal CockroachDB optimization
that a reviewer will suggest, and it silently breaks every `SELECT … FOR UPDATE SKIP LOCKED`
query ("SKIP LOCKED cannot be used for tables with multiple column families"). Verify with
`SHOW CREATE TABLE axiom_task` after any migration.

### 4.2 `shard` is an explicit column, not `USING HASH`

```sql
shard INT2 NOT NULL AS (
    mod(fnv32(crdb_internal.datums_to_bytes(tenant_id::STRING || ':' || dedupe_key)), 16)::INT2
) STORED
```

`fnv32` + `datums_to_bytes` is exactly what CockroachDB's hash-sharded indexes use
internally, so this is not a home-made hash. It is computed rather than application-supplied
so it cannot drift or be forgotten, and it is stable for the life of the row because both
inputs are immutable.

The reason it is not `USING HASH`: **workers must be able to deliberately target a shard
subset.** `axiom_agent.shards INT2[]` gives static work partitioning across the pool,
mirroring a Kafka consumer group, and an empty array means "steal from all shards". An opaque
`crdb_internal_..._shard_N` column cannot be targeted by the application, so `USING HASH`
would buy the same write distribution and lose the ability to partition work.

`USING HASH` appears exactly once in the schema, on the one genuinely monotonic index — the
global audit timeline:

```sql
CREATE INDEX axiom_event_timeline ON axiom_event (occurred_at DESC)
    USING HASH WITH (bucket_count = 8);
```

That is where it belongs: a monotonic key with no need for application-level targeting.
`bucket_count` is kept at or below the node count, because above that the docs note
diminishing returns and the cost is a range scan that must hit and merge every bucket.

Everything else avoids the hotspot by not creating it: **no monotonic primary keys
anywhere.** Every PK is `gen_random_uuid()`. There is not one `SERIAL`, `unique_rowid()`, or
ordered `INT` primary key in the schema.

### 4.3 `idempotency_key` is `GENERATED`, and that is a security control

```sql
idempotency_key STRING NOT NULL AS (
    'axm_' || substring(
        sha256(tenant_id::STRING || ':' || task_id::STRING || ':' ||
               step_name || ':' || step_seq::STRING)
        FROM 1 FOR 48)
) STORED
```

The single most lethal bug in this class of system is a key derived at call time from
`gen_random_uuid()`, a timestamp, the worker id, `attempt`, or `lease_epoch`. The recovering
worker then mints a **different** key, the provider sees a brand-new request, and the $300
goes out twice — the exact failure this project exists to prevent.

Making the key a computed column of **immutable** inputs removes that possibility from the
codebase rather than from the code review. There is no application-supplied key path to
audit, because there is no application-supplied key path.

Two unique constraints back it up:

```sql
CONSTRAINT axiom_attempt_step_uniq UNIQUE (tenant_id, task_id, step_name, step_seq),
CONSTRAINT axiom_attempt_key_uniq  UNIQUE (tenant_id, idempotency_key)
```

The second is redundant with the first *given a correct derivation* — which is precisely why
it is there. It is the assertion that the derivation is correct, checked by the database on
every insert.

And the one that makes W6 unrepresentable:

```sql
CREATE UNIQUE INDEX axiom_attempt_one_live
    ON axiom_action_attempt (tenant_id, task_id, step_name)
    WHERE attempt_state IN ('PREPARED', 'DISPATCHED');
```

Two workers cannot both hold a live receipt for the same `(task, step)`. The loser of the
race gets `23505`, not a second refund. Terminal rows fall out of the index, so a legitimate
new `step_seq` after a terminal rejection is still allowed.

`step_seq` bumps **only** on an explicit, journalled decision that a genuinely new external
call is required — for example the provider terminally rejected the previous request body. It
never bumps on a retry of the same logical call. Every bump is an event, so "we deliberately
called the provider a second time" is always auditable.

### 4.4 `retrieval_class` is a vector index PREFIX column

This is the subtlest design decision in the schema and the one with the largest correctness
consequence.

```sql
retrieval_class STRING NOT NULL AS (
    CASE
        WHEN quarantined              THEN 'QUARANTINED'
        WHEN superseded_by IS NOT NULL THEN 'SUPERSEDED'
        WHEN trust_level >= 2          THEN 'ACTIONABLE'
        ELSE                                'ADVISORY'
    END
) STORED,

VECTOR INDEX axiom_memory_ann_by_context
    (tenant_id, memory_class, context_key, retrieval_class, embedding vector_cosine_ops)
    WITH (min_partition_size = 16, max_partition_size = 128),

VECTOR INDEX axiom_memory_ann_by_tenant
    (tenant_id, memory_class, retrieval_class, embedding vector_cosine_ops)
    WITH (min_partition_size = 16, max_partition_size = 128)
```

**What breaks if you post-filter instead.** The obvious implementation is to search the
vector index and then apply `WHERE quarantined = false AND superseded_by IS NULL AND
trust_level >= 2` to the results. That is a documented trap. An ANN search returns
`target count` candidates; a `WHERE` applied afterwards discards some of them, so you
silently get **fewer than `LIMIT` rows and miss true nearest neighbours**. In a memory system
that is a wrong answer, not a slow query — and it is a wrong answer that only appears once
enough inadmissible memories accumulate, which is to say in production and not in the demo.

Folding admissibility into a prefix column means an inadmissible memory is in a **different
partition of the index** and never enters the ANN candidate set at all.

**Why trust is folded in rather than filtered.** A range predicate on a prefix column
(`trust_level >= 2`) disables the vector index entirely — index acceleration with filters is
only supported when the filters match prefix columns *pinned to exact values*. So the
threshold is baked into the `CASE` and the query pins `retrieval_class = 'ACTIONABLE'`.

**Why `context_key` is one namespaced column** rather than several optional ones: same
reason. A prefix column must be pinned to an exact value for the index to be used at all, so
the convention is `'state:ACTION_PREPARED'` and `'exception:duplicate_charge'` in a single
column (`models.ctx_state`, `models.ctx_exception`).

**The consequence worth demonstrating.** `UPDATE axiom_memory SET quarantined = true` changes
a prefix column, so the row **moves in the vector index inside that transaction**. Quarantine
takes effect at commit, atomically, for every subsequent retrieval. There is no reindex, no
cache invalidation, no eventual anything. Verified live: inside a single transaction,
quarantining the top hit removed it from the candidate set of the very next recall in that
same transaction.

**The one unavoidable post-filter** is valid time. `valid_from` / `valid_until` are
time-varying and a computed column cannot call `now()`, so they remain a post-ANN filter —
compensated for by over-fetching `k * settings.recall_overfetch` (4× by default) before
applying them. That is the exception that proves the rule, and it is documented as such in
`memory.recall`.

**`vector_cosine_ops` is written explicitly.** Omitting the opclass silently gives you
`vector_l2_ops`, the default; a `<=>` query then ignores the index and full-scans, which
looks perfect on 200 demo rows and collapses at scale. Titan V2 output is normalized, so
cosine is the correct metric — and `embeddings._normalize` asserts it rather than trusting
the provider, because the failure mode of a silently unnormalized vector under a cosine
index is bad neighbours, not an error.

Two vector indexes on one column doubles vector write cost. That is an accepted, measured
trade for having both a pinned recovery path and broad recall.

### 4.5 Vectors are always bound parameters

Preflight gate 4 established that a **subquery** search vector defeats the vector index — the
plan silently degrades to a full primary-key scan. Gate 4b established that a **bound
parameter** is fine. The rule is therefore: vectors are always bound parameters with an
explicit `::VECTOR(1024)` cast, formatted by exactly one function (`db.vector_literal`), and
never assembled at a call site.

`db.uses_vector_index()` exists so a test can assert on the *plan*, not the output. A
degraded plan returns correct rows, so nothing except an explicit plan assertion would ever
catch the regression.

---

## 5. Multi-tenancy

Every table carries `tenant_id UUID NOT NULL`. Shared infrastructure rows — the worker pool
— live under a reserved SYSTEM tenant (`00000000-0000-0000-0000-000000000000`) so the
`NOT NULL` constraint is uniform and no code path can forget the predicate. A nullable
`tenant_id` is how cross-tenant leaks happen: one forgotten `IS NULL` branch.

`tenant_id` is never the leading primary-key column on a hot table — that would concentrate
all writes for the busiest tenant into one range. It leads *secondary* indexes, where it is a
filter rather than a write-ordering key.

`axiom_policy` is the deliberate exception: `PRIMARY KEY (tenant_id, policy_id, version)`. It
is a tiny, cold, read-mostly table written by humans a handful of times. There is no write
rate to spread, and co-locating a tenant's policies gives a single-range point read on the
hot lookup path. The anti-pattern is a leading low-cardinality column on a *write-heavy*
table, not on any table.

Row-level security is written and commented out in the schema on purpose; see Limitations in
the README.

---

## 6. What the LLM is not allowed to do

`axiom/llm.py` has two jobs and the seam is enforced by the type signature. `triage()`
returns a `Triage` proposal: an action, an amount, a reason, a category. It never mints an
idempotency key, never decides whether the agent is allowed to act (that is procedural
memory), and never sees the receipt table. It proposes; the state machine disposes.

When triage fails, it escalates:

```python
except Exception as e:
    return Triage('escalate', 0, f'triage unavailable ({type(e).__name__}); escalating', ...)
```

A model failure must never become an unattended action. Escalating is the only safe default
when the thing that was supposed to decide did not.

`summarize_recovery()` is template-driven rather than model-generated, deliberately: that
string becomes the *content* of an episodic memory, which is what gets embedded and recalled
next time. Letting a model vary its phrasing run to run would make semantically identical
situations drift apart in vector space.

---

## 7. Governance and provenance

Every memory carries where it came from and how much it should be believed:

- **`source`** — `system:execution` | `human:operator` | `tool:stripe` | `ingest:email`.
- **`trust_level`** — 3 signed policy / verified operator, 2 first-party execution outcome,
  1 tool output, 0 untrusted third-party text. Ordered so the tier comparison is a plain `>=`.
- **`content_sha256`** — proves the text was not edited after the fact.
- **Supersession chain** — nothing is deleted; superseding is a write, and under SERIALIZABLE
  two writers cannot fork the chain (the second gets a 40001, and `memory.write` raises
  `ConflictingSupersession` if the `UPDATE` matched no row).
- **Valid time** (`valid_from` / `valid_until`) is distinct from transaction time, which MVCC
  gives us free via `AS OF SYSTEM TIME`. AOST is bounded by `gc.ttlseconds` and yields a
  read-only transaction, so it can never be the durable audit axis — these columns are.

And the query you run the moment you discover a memory was poisoned
(`memory.effects_licensed_by`, backed by the partial index `axiom_attempt_by_license`):

```sql
SELECT a.id, a.task_id, a.step_name, a.provider, a.operation, a.amount_cents,
       a.attempt_state, a.provider_ref, a.prepared_at, a.settled_at
FROM axiom_action_attempt a
WHERE a.tenant_id = %s AND a.licensed_by_memory_id = %s
ORDER BY a.prepared_at DESC;
```

Every real-world effect that memory authorized, enumerable. That is what
`licensed_by_memory_id` on the receipt is for: rollback traceability from evidence to
irreversible act.

---

## 8. Approvals are capabilities, not permissions

When policy refuses to self-authorize, the task parks and the lease is **released** — an
approval nobody answers must not pin a worker. The approval's expiry is written into the
task's `available_at`, so an unanswered approval is reclaimed by a worker rather than sitting
forever. Same self-healing trick as the lease; no approval-expiry cron.

**The reclaim is wired; the resolution is not.** This is a known open defect, pinned by
`test_unanswered_approval_is_reclaimed_and_re_escalated` (strict `xfail`). The task is
re-claimed correctly, but policy then refuses again, and because nothing in the codebase ever
sets `ApprovalState.EXPIRED`, `consume_approval()` returns `None` and `request_approval()`
inserts a *second* approval — hitting `23505` on `axiom_approval_one_pending`. `Worker.run()`
catches only `LeaseLost` and `ProviderCrash`, so that `UniqueViolation` kills the process and
the next worker repeats the cycle. The chaos demo never surfaces it because `auto_approve()`
answers every approval within 250 ms. The fix belongs in `tasks.prepare` /
`tasks.request_approval`: expire the stale approval before raising a new one.

`decision_token` is single-use. `consume_approval()` burns it with a conditional `UPDATE`:

```sql
UPDATE axiom_approval SET token_consumed_at = now()
WHERE tenant_id = %s AND task_id = %s AND step_name = %s
  AND state = 'APPROVED' AND token_consumed_at IS NULL
RETURNING id;
```

Consuming rather than merely reading is the point. A human decision is a capability, not a
standing permission, so a worker that restarts after the token is spent cannot replay a
human's decision into a second refund.

This was a real bug once: `consume_approval()` existed and nothing called it. An approved
task was re-claimed, re-evaluated against the **unchanged** policy ceiling, and parked again
— the policy had not moved and never would; the approval was the thing that changed. The
demo answered 1,187 approvals for 3 tasks before it was caught.

`axiom_approval_one_pending` (unique, partial, `WHERE state = 'PENDING'`) means two agents
cannot both raise the same question and get two different humans to answer it differently.

---

## 9. The journal

`axiom_event` is append-only. Never updated, never deleted. Every state transition writes one
row in the **same transaction** as the transition itself, which is what makes the audit trail
a guarantee rather than a logging convention that a `continue` statement can skip.

The per-subject sequence is gap-free by construction:

```sql
seq = coalesce((SELECT max(seq) FROM axiom_event
                WHERE tenant_id = $1 AND subject_type = $2 AND subject_id = $3), 0) + 1
```

computed inside the writing transaction. Deliberately not a global sequence — that is a
single-range hotspot for the whole cluster — and deliberately not `unique_rowid()`, because
gaps make "did we lose an event?" unanswerable, which defeats the purpose of having a
journal. Contention is per-subject only, and the fencing token already serializes writers to
a given task, so in practice it costs nothing.

The canonical run produced 245 events across 75 subjects for 30 tasks: 46 `task.claimed`,
18 `attempt.prepared`, 18 `attempt.settled`, 13 `task.recovered`, 40 `memory.written`, plus
approvals, agent lifecycle and the mission and policy rows.

---

## 10. Things that would quietly destroy this

Collected in one place, because each one returns correct-looking results while being wrong.

1. **Post-filtering an ANN result** on a non-prefix column. Silently returns fewer than
   `LIMIT` rows and misses true neighbours. §4.4.
2. **A range predicate on a prefix column** (`trust_level >= 2`). Disables the vector index
   entirely. §4.4.
3. **A subquery as the search vector.** Defeats index selection; a bound parameter does not.
   §4.5.
4. **Omitting `vector_cosine_ops`.** You silently get L2. §4.4.
5. **Adding `FAMILY` declarations to `axiom_task`.** Breaks `SKIP LOCKED`. §4.1.
6. **Deriving the idempotency key at call time.** The double refund, directly. §4.3.
7. **Letting the claim predicate drift from the partial index predicate.** Silent full scan.
   §2.
8. **Moving the memory write out of the settle transaction** "for throughput". Destroys the
   differentiator. §3(d).
9. **Splitting `recover()` into two transactions.** Makes the central claim false. §3(e).
10. **Treating `40001` as an error.** Under SERIALIZABLE it is the system working. §3.
11. **Branching on `DISPATCHED` vs `PREPARED`** for a correctness decision. The process can
    die between the send and the marker. §3(c).
12. **Embedding inside a transaction.** `db.tx()` re-runs its callable on 40001, so every
    retry re-hits Bedrock. §3(d).
