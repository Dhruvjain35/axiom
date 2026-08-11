# AXIOM — Handoff

**Written 2026-08-10. Deadline 2026-08-18 17:00 EDT. You have ~8 days.**

You are picking up a hackathon project mid-build. This document is the complete brief:
what the competition wants, what we are building and why, what is actually on disk right
now, what has been proven versus assumed, and what to do next in what order.

Read the whole thing before touching code. It is long on purpose — the alternative is you
rediscovering the same three traps that already cost a night.

**Working style for this project:** blunt, no glazing. If something in here is wrong, say
so and say why. Do not tell the operator the build is going well when it isn't. Every
claim in this document is marked ✅ VERIFIED, ⚠️ ASSUMED, or ❌ DISPROVEN — maintain that
discipline in anything you add.

---

## 0. First 30 minutes

Do these in order before writing any application code.

1. Read this file end to end.
2. Read `db/001_schema.sql` end to end. Every non-obvious choice has a `WHY` comment.
   The comments are the design doc; do not change a line without reading its comment.
3. **`git init` has run but there are zero commits. Commit everything immediately.**
   The entire project currently exists only as untracked files on one laptop.
   ```bash
   cd ~/axiom && git add -A && git commit -m "AXIOM: schema, preflight, docs"
   ```
   `.gitignore` now excludes `.env`, keys, the **339 MB** local cockroach binary, the
   single-node runtime dumps (`heap_profiler/`, `pprof_dump/`, …) and `preflight.log`.
   `preflight.log` is deliberately untracked because it contains the live cluster hostname
   and SQL username and the repo is going public — it stays on disk as a local artifact, and
   §5.2 below summarises everything it proves. **Run `git status` and confirm the staged set
   is only source before you commit.**
4. Run the preflight (§5.1). **Nothing in §6 is safe to build until it goes green.**
5. Skim §7 (the build plan) and §8 (traps) so you know what you are aiming at.

---

# PART I — THE COMPETITION

## 1. Hard facts

✅ VERIFIED — fetched from the official Devpost pages on 2026-08-10.

| | |
| --- | --- |
| Event | **CockroachDB × AWS Hackathon — Build with Agentic Memory** |
| URL | https://cockroachdb-ai.devpost.com/ |
| Submission period | Jun 30 2026 10:00 ET → **Aug 18 2026 17:00 ET** |
| Judging period | **Aug 19 → Sep 15 2026** |
| Winners announced | on or around **Sep 21 2026** |
| Registrants | **3,282** |
| Prizes | $8,750 total — 1st **$5,000** + blog feature + swag; 2nd **$2,500**; 3rd **$1,250** |
| Tracks | **None.** One pool, three places. |

**The month-long judging window is a hard operational requirement, not a formality.**
The demo URL must answer requests on Sep 15. Budget and architect for a month of uptime
from day one. (This exact trap has bitten a previous project — see the GAFFER post-mortem.)

## 2. Rules that constrain us

✅ VERIFIED — quoted from https://cockroachdb-ai.devpost.com/rules

- **Newly created:** *"Projects must be newly created by the Entrant during the Submission
  Period."* Standard frameworks, libraries, starter templates and AI coding assistants are
  explicitly permitted, with disclosure of any pre-existing code incorporated. AXIOM was
  scaffolded 2026-08-10 — comfortably inside the window. **Keep the git history public and
  honest; it is the proof.**
- **Open source:** *"The repository must be public and open source by including an open
  source license file (we recommend MIT or Apache 2.0)."* → `LICENSE` (Apache-2.0) is
  already in the repo. ✅
- **Age:** *"Individuals who are at least 18 years old (or have reached the age of majority
  in their jurisdiction of residence at the time of entry)."* There is no parent/guardian
  provision. **See §11 — this is an open item for the operator to resolve, not for you to
  decide.**
- **IP:** entrant retains ownership; sponsor gets a non-exclusive license for judging and
  three years of promotional use.

## 3. What must be built, and what must be submitted

✅ VERIFIED.

> *"Build an agentic application that uses CockroachDB as its persistent memory layer,
> deployed on AWS."*

**CockroachDB — minimum 2 of 4. AXIOM does all 4:**

| Requirement | How AXIOM uses it | Docs |
| --- | --- | --- |
| Cloud Managed MCP Server | The **Audit Agent** connects over Managed MCP under a scoped **read-only service account** and answers "did we ever refund order X twice?" in natural language, against the same live database — no ETL, no second store. | https://www.cockroachlabs.com/docs/cockroachcloud/connect-to-the-cockroachdb-cloud-mcp-server |
| Distributed Vector Indexing | Two C-SPANN vector indexes on `axiom_memory.embedding` (recovery-path and broad-recall). | https://www.cockroachlabs.com/docs/v26.2/cockroachdb-and-ai.html |
| ccloud CLI (Agent-Ready) | Cluster provisioning + schema migration in `scripts/` and in the README setup path, so a judge can reproduce the whole cluster from the CLI. | https://www.cockroachlabs.com/docs/cockroachcloud/ccloud-get-started |
| Agent Skills Repo (OSS) | Adopt the published skills; the honest, differentiating move is to **contribute one back** (see §9.6). | https://github.com/cockroachlabs/cockroachdb-skills |

**AWS — minimum 1 service. AXIOM uses three:**

- **Amazon Bedrock** — planner/decider LLM + `amazon.titan-embed-text-v2:0` embeddings (1024-d).
- **ECS Fargate** — worker agents. Chosen over Lambda *specifically because you must be
  able to kill a worker on camera*; you cannot SIGKILL a Lambda for a demo.
- **S3** (+ ALB/CloudFront as needed) — static Mission Control assets, artifact storage.

**Deliverables:**

1. Public open-source repo with README and setup instructions.
2. **Functional demo app URL** (must survive to Sep 15).
3. **Video, strictly under 3 minutes**, on YouTube or Vimeo.
4. Explicit identification of which CockroachDB and AWS tools were used.
5. Optional but do it: architecture diagram + feedback to the sponsors.

## 4. Judging criteria, and how each one is actually won

✅ VERIFIED — five criteria, **equally weighted**, judges' sole discretion. Exact names:

1. Agentic Memory Design
2. Technological Implementation
3. Real-World Impact
4. Product Readiness
5. Creativity & Originality

Equal weighting is the strategic fact of this competition. A project that is a 10 on
implementation and a 3 on impact loses to one that is a 7 across the board. **Do not let
any criterion sit below a 7 while polishing another to a 10.**

Map every hour of remaining work to one of these:

| Criterion | What wins it here | Concretely |
| --- | --- | --- |
| **Agentic Memory Design** | Four memory classes with different *authority*, not one embeddings table. Episodic/semantic/procedural **advise**; execution memory **constrains**. Provenance, trust tiers, supersession, quarantine, valid-time vs transaction-time. | §6.2, `axiom_memory`, `axiom_policy` |
| **Technological Implementation** | The fused transaction: execution state + semantic recall commit together under SERIALIZABLE. Fencing tokens. Partial indexes as a hotspot answer. Generated idempotency keys. | §6.5, §6.6 |
| **Real-World Impact** | "Did the agent refund $300 twice?" needs zero explanation. Every company deploying agents against a payments API has this problem *today*. | §6.1, the demo |
| **Product Readiness** | Multi-tenant from row one, budget caps, human-in-the-loop approvals, dead-letter, audit trail, a test suite that asserts invariants by trying to violate them, honest failure-mode docs. | §7 Day 5-6, §9.1 |
| **Creativity & Originality** | The reframe itself: *memory is not recall, memory is what makes action safe.* Plus the crash-window table — nobody else will submit a document that enumerates every crash window and its defined outcome. | §6.6 |

**The rubric test for any proposed feature:** could single-node Postgres do this? If yes,
it scores nothing on criterion #1 and little on #2. Build it only if it serves #4 or #5.

---

# PART II — WHAT WE ARE BUILDING

## 5. Where we left off — exact state of the repository

**Repo:** `~/axiom` — local only, no remote, **zero commits**, everything untracked.

```
~/axiom
├── LICENSE                  Apache-2.0                                    ✅ done
├── README.md                thesis, architecture, "what it does not claim" ✅ good draft
├── .gitignore               secrets, venvs, tfstate, .claude/             ✅ done
├── db/001_schema.sql        747 lines, fully commented                    ⚠️ NEVER EXECUTED
├── scripts/preflight.py     9 gates, rewritten in Python                  ❌ NEVER RUN
├── preflight.log            output of an EARLIER shell/SQL probe          ⚠️ read §5.2
├── docs/HANDOFF.md          this file
└── cockroach-v26.2.3…/      local binary + heap_profiler/, pprof_dump/,
                             goroutine_dump/ — throwaway artifacts of a
                             local `cockroach start-single-node`. Ignore
                             or delete; do not commit.
```

**There is no application code.** No API, no worker, no planner, no Bedrock call, no UI,
no Dockerfile, no IaC, nothing deployed. Days 1–8 of the plan in §7 are all still ahead.

### 5.1 The cluster

✅ VERIFIED from `preflight.log`: a real CockroachDB Cloud cluster already exists.

- Host: `axiom-memory-31580.j77.aws-us-east-1.cockroachlabs.cloud:26257`
- SQL user: `adam`
- Version: **CockroachDB CCL v26.2.5** (cluster version 26.2), AWS `us-east-1`
- `default_transaction_isolation` = **serializable** ✅
- `feature.vector_index.enabled` settable to `true` on this tier ✅
- `VECTOR(1024)` column type accepted ✅
- `CREATE VECTOR INDEX … vector_cosine_ops` accepted; backfill job completed ✅

**Password is not in the repo and not in the environment.** `DATABASE_URL` is unset and
`psycopg` is not installed. Before anything else:

```bash
python3 -m venv ~/axiom/.venv && source ~/axiom/.venv/bin/activate
pip install "psycopg[binary]"
export DATABASE_URL='postgresql://adam:<PASSWORD>@axiom-memory-31580.j77.aws-us-east-1.cockroachlabs.cloud:26257/defaultdb?sslmode=verify-full'
python3 scripts/preflight.py    # exit 0 required
```

Ask the operator for the password, or mint a fresh SQL user from the Cloud console /
`ccloud` — minting a new one via `ccloud` is better anyway, because it exercises a required
tool and gets the command into the README.

⚠️ Note the version drift: `001_schema.sql`'s header says *"Target: CockroachDB v25.4+ …
validated on v25.4.14"* but the live cluster is **v26.2.5**. Update the header once the
schema actually applies cleanly, and cite the version you really ran it on.

### 5.2 What the old `preflight.log` actually proves — read this carefully

The log is from an **earlier shell/SQL probe, not from `scripts/preflight.py`**. Steps 1–7
passed. Then:

- ❌ **STEP 8 — the vector index was NOT used.** The plan is
  `scan … table: axiom_preflight@axiom_preflight_pkey, spans: FULL SCAN`, with
  `missing stats`, plus an index recommendation for a plain covering index. Two confounds:
  the search vector was passed as a **subquery** rather than a literal, and the table held
  **~4 rows** with no statistics. Both are plausible causes and the log cannot separate
  them.
- ❌ **STEP 9 — "THE CORE CLAIM" did not pass.** It printed the same two rows as step 7;
  the row inserted-but-uncommitted inside the transaction **never appeared in the results**.
  Whether that is because it fell outside top-k or because in-transaction recall genuinely
  misses uncommitted rows, the log does not say. **Treat it as unproven, not as passed.**
- ❌ **STEP 10 — errored** `42P01 relation "axiom_preflight" does not exist`. Self-inflicted:
  `AS OF SYSTEM TIME '-10s'` pointed at an instant before the table was created. Not a
  cluster limitation.

`scripts/preflight.py` was written to settle all three, and is a good piece of work — it
escalates the fixture count (5,000 → 25,000 rows), isolates the literal-vs-subquery
variable as its own gate, and captures a real `cluster_logical_timestamp()` instead of
using a relative interval. It has **never been executed**.

### 5.3 The single most important open question

> **Can a single SERIALIZABLE transaction write a memory and then run an
> index-accelerated ANN recall that sees it?**

This is the load-bearing claim of the entire project. If in-transaction ANN recall silently
falls back to a full scan, the fused recovery path still works *correctly* — but the
performance story dies, and with it a chunk of criterion #2. If uncommitted rows are not
visible to the vector index at all, the design needs restructuring (fallback in §9.5).

**Gate 6 of `preflight.py` answers this. Run it first. Report the answer honestly in the
README either way** — a documented, measured limitation reads as engineering maturity;
a claim the judges can disprove reads as fabrication.

---

## 6. The system

### 6.1 The thesis

> **Memory is not saved chat history. Memory is what makes autonomous *action* safe.**

The scenario, which must stay this simple: an agent is told to resolve 30 order exceptions.
It issues a $300 refund to customer #18. The process dies — OOM, deploy, spot reclamation —
*before* it records that the refund succeeded. It restarts. What happens to customer #18?

In most agent frameworks nobody knows. The framework rebuilds context from a transcript,
sees unfinished work, and refunds again. That gap is the whole distance between an agent
demo and an agent you would let near a payments API.

**Domain: e-commerce refunds / order exceptions. Locked. Deliberately generic.** "Did it
refund $300 twice?" needs zero setup in a 3-minute video. Do not make the domain cleverer
than the systems idea — the systems idea is the entry.

### 6.2 Four memory classes with different authority

| Class | Question it answers | Where it lives | Authority |
| --- | --- | --- | --- |
| **Episodic** | What happened last time we saw this? | `axiom_memory` (`EPISODIC`) | advises |
| **Semantic** | What past situations resemble this one? | `axiom_memory` (`SEMANTIC`) | advises |
| **Procedural** | What policy applies, and which version? | `axiom_policy` | authorizes |
| **Execution** | What has this agent already *done*, irreversibly? | `axiom_task` + `axiom_action_attempt` | **constrains** |

The first three advise. The fourth constrains. **Vector memory tells the agent what it
*could* do; transactional execution state decides what it *may* do.** That sentence is the
project. Put it in the video.

### 6.3 Why this genuinely needs CockroachDB

Because execution state and semantic memory **commit in one serializable transaction**.

When a worker recovers a task orphaned by a dead peer it does several things at once: reads
the durable receipt of what the dead worker had already done, semantically retrieves what
happened the *last* time an agent died at this exact point in this kind of operation, checks
the governing policy version, debits the mission budget, appends to the journal, and
transitions state. **One commit.**

Split that across a workflow engine plus a vector database and you get a window where the
agent resumes on memory that has already been superseded, with no transaction to close it.
Durable-execution engines store history that is opaque and not semantically queryable.
Vector databases have no transactions to join. One store, one commit, or you are racing.

**If the two halves never touch in the same transaction, this project is "durable
execution, again" and it does not place.** Guard that property in code review.

### 6.4 Task state machine

```
PENDING ──dependency met──► READY ──claim(epoch++)──► LEASED
                                                        │
                        ┌───────────────────────────────┼───────────────────────┐
                        ▼                               ▼                       ▼
              AWAITING_APPROVAL              ACTION_PREPARED               FAILED /
              (lease released,               (receipt committed;           CANCELLED
               available_at = expiry)         EXTERNAL EFFECT              DEAD_LETTER
                        │                     AUTHORIZED)                  (terminal)
                        │                          │
                 approved + token            dispatch → settle
                        │                          │
                        └──────────►──────────► SUCCEEDED
```

Two rules the code must never break:

1. **No external side effect is authorized in `LEASED`.** The receipt is committed *first*,
   which moves the task to `ACTION_PREPARED`; only then may an HTTP call go out. A crash
   before the receipt therefore *cannot* have caused an effect.
2. **`ACTION_PREPARED` means "an effect may exist in the world."** Recovery from that state
   is never "start over"; it is "re-dispatch under the same key, or reconcile."

### 6.5 The five protocols

Every one of these is a single transaction unless stated otherwise.

**(a) CLAIM** — one statement, CAS on the fencing token:

```sql
WITH candidate AS (
    SELECT id, lease_epoch
    FROM axiom_task
    WHERE shard = ANY($1::INT2[])
      AND available_at <= now()
      AND state IN ('READY','LEASED','ACTION_PREPARED','AWAITING_APPROVAL')
      AND attempt < max_attempts
    ORDER BY available_at ASC
    LIMIT 1
)
UPDATE axiom_task t
SET lease_epoch  = t.lease_epoch + 1,
    lease_owner  = $2::UUID,
    available_at = now() + $3::INTERVAL,
    state        = CASE WHEN t.state = 'READY' THEN 'LEASED' ELSE t.state END,
    updated_at   = now()
FROM candidate c
WHERE t.id = c.id AND t.lease_epoch = c.lease_epoch
RETURNING t.id, t.state, t.lease_epoch, t.payload, t.policy_id, t.policy_version;
```

The `WHERE state IN (…)` list must **exactly match** the partial-index predicate on
`axiom_task_claimable` or the optimizer will not use the index. Zero rows returned means
another worker won the CAS — that is normal; back off and retry, do not treat it as an error.
A `40001` retry error is also normal under SERIALIZABLE — every transaction in this system
needs a retry wrapper.

**(b) PREPARE** — mint the receipt. The transaction that authorizes an irreversible act:

```
BEGIN;
  re-verify lease_epoch (fence)
  load ACTIVE policy version, pin it to the task
  ANN recall: "have we seen this situation before?"  (advisory)
  IF amount > policy.max_auto_action_cents OR policy.requires_approval
      → create axiom_approval, task → AWAITING_APPROVAL, release lease; COMMIT; return
  UPDATE axiom_mission SET spent_cents = spent_cents + $amount   -- CHECK enforces the cap
  INSERT axiom_action_attempt (…)   -- idempotency_key is GENERATED, never supplied
  INSERT axiom_event ('attempt.prepared')
  UPDATE axiom_task SET state = 'ACTION_PREPARED'
COMMIT;
```

Only after this commit may an HTTP request be issued. `23505` on
`axiom_attempt_one_live` means a peer already holds a live receipt for this step — abort,
do not call the provider.

**(c) DISPATCH** — outside the transaction, by necessity. Send the provider call with
`Idempotency-Key: <receipt.idempotency_key>`. Record `dispatched_at` best-effort only; the
schema comment is explicit that `DISPATCHED` is **safety-equivalent to `PREPARED`** — never
branch on it for correctness.

**(d) SETTLE** — one transaction, fenced:

```
BEGIN;
  verify lease_epoch unchanged (else abort — a zombie is writing)
  UPDATE axiom_action_attempt SET attempt_state = SUCCEEDED|FAILED_*, provider_ref,
         response_body, http_status, settled_at = now()
  UPDATE axiom_task SET state = terminal-or-next, result, updated_at
  INSERT axiom_event
  INSERT axiom_memory  ← the outcome memory, embedded, SAME TRANSACTION
COMMIT;
```

Writing the outcome memory in the settle transaction is what makes it **impossible for
memory to disagree with execution state**. Do not move it to a background job for
throughput; that trade destroys the entire differentiator.

**(e) RECOVER** — the money shot, and the thing the video is about:

```
BEGIN;
  claim orphaned task (epoch++, above)
  point-read the receipt:  SELECT … WHERE task_id = $1 AND attempt_state IN ('PREPARED','DISPATCHED')
  ANN recall pinned to context_key = 'state:ACTION_PREPARED',
                        retrieval_class = 'ACTIONABLE'
  aggregate memory.outcome over the hits → decision
  transition state + append event
COMMIT;
then act on the decision (re-dispatch under the SAME key / reconcile / escalate)
```

Read the receipt **and** the semantic memory **and** commit the transition together. That
is the fusion. If a refactor ever splits this into two transactions, the project's central
claim becomes false.

### 6.6 The crash-window table

**Build this table into the README, the UI, and the video.** No competitor will have one,
and it is simultaneously your correctness spec, your test matrix, and your credibility.

| # | Crash point | Effect possible? | Recovery action | Guarantee |
| --- | --- | --- | --- | --- |
| W1 | After CLAIM, before PREPARE | No | Re-claim with new epoch; re-plan freely | No effect can exist — nothing was authorized |
| W2 | After receipt COMMIT, before HTTP send | Yes (unknowably) | Re-dispatch under the **same** derived key | Provider dedupes; effectively-once |
| W3 | Mid-flight HTTP, outcome unknown | Yes | Re-dispatch under the same key | Same as W2 |
| W4 | Provider responded, before SETTLE | **Yes — the effect landed** | Re-dispatch under the same key; provider returns the *original* refund; settle records it | Exactly one real-world effect |
| W5 | Zombie worker resumes after lease expiry | Yes | Its settle is rejected: stale `lease_epoch` | Fence, not lease, is the invariant |
| W6 | Two workers PREPARE the same step | No | Loser gets `23505` on `axiom_attempt_one_live` | DB-enforced: at most one live receipt |
| W7 | Recovered LLM re-synthesizes a *different* request body | Yes | `request_fingerprint` mismatch under an existing key → **hard stop**, escalate | Same key + different intent is not a retry |

W5 is worth saying out loud because it is the subtle one: **a lease expiring does not stop a
GC-paused worker that is already inside a refund HTTP call.** The lease is an optimization
for liveness; the monotonic per-row `lease_epoch` is the correctness guarantee. W7 is the
defence against the semantic-rollback attack class (`ACRFence`, arXiv:2603.20625).

### 6.7 Schema highlights you must not undo

Read the comments in `db/001_schema.sql` before changing anything. The load-bearing ones:

- **No monotonic PKs anywhere.** Every PK is `gen_random_uuid()`. Not one `SERIAL`.
- **`axiom_task_claimable` is a PARTIAL, STORING index** keyed `(shard, available_at)`.
  Terminal tasks *leave* the index, so it stays permanently small; we never `DELETE` a task,
  so no tombstones accumulate behind the queue head. This is the direct answer to
  CockroachDB's own documented queueing anti-pattern — say so in the submission.
- **`shard` is an explicit computed column, not `USING HASH`**, so a worker can be pinned to
  a shard subset (static partitioning, à la a Kafka consumer group). `USING HASH` appears
  exactly once, on the genuinely monotonic event timeline.
- **`idempotency_key` is a GENERATED STORED column** derived from
  `(tenant_id, task_id, step_name, step_seq)` — all immutable. Deriving it at call time from
  a UUID, timestamp, worker id, `attempt`, or `lease_epoch` is the single most lethal bug in
  this class of system, and the schema makes it unrepresentable. **Never add an
  application-supplied key path.**
- **`retrieval_class` is a computed vector-index PREFIX column.** Quarantined / superseded /
  low-trust memories are in a *different partition of the index* and never enter the ANN
  candidate set. This is correctness, not tidiness: post-filtering an ANN result silently
  returns fewer than `LIMIT` rows and misses true nearest neighbours. Consequence to
  demonstrate: `UPDATE … SET quarantined = true` moves the row in the index *within that
  transaction* — quarantine takes effect at commit, atomically, with no reindex.
- **`vector_cosine_ops` is written explicitly.** Omitting the opclass silently gives L2, and
  a `<=>` query then ignores the index and full-scans — looks perfect on 200 demo rows,
  collapses at scale.
- **No `FAMILY` declarations on `axiom_task`, ever.** Splitting hot lease columns from the
  cold JSONB is a plausible-looking optimization that silently breaks every
  `SELECT … FOR UPDATE SKIP LOCKED` query. Verify with `SHOW CREATE TABLE` after any migration.
- **`tenant_id NOT NULL` on every table**, leading every secondary index, never leading a
  hot table's PK. RLS is left commented out **on purpose**: a misconfigured `FORCE RLS`
  returns zero rows *silently*, which is the worst thing that can happen during a live demo.
  Enable it only behind a test that asserts both "cross-tenant read denied" and "in-tenant
  read still returns rows."

### 6.8 What AXIOM must never claim

**Never say "exactly-once execution."** No system that calls a network API it does not
control can offer that. The credible, defensible phrasing — already in the README, keep it —
is **durable, idempotent, effectively-once**: every external action is issued under a derived
idempotency key against a durable receipt, and every crash window has a defined and tested
outcome. Judges who know distributed systems will trust the project *more* for the
disclaimer, and instantly distrust it without one.

Same discipline for benchmarks: measured numbers with the measurement method, or no numbers.

---

# PART III — EXECUTION

## 7. Build plan — 8 days

Ordered by *risk retired per hour*, not by what is fun. Dates are aggressive because the
submission must be finished a day early; the demo URL must then survive a month.

**Day 1 (Aug 10-11) — de-risk, then commit**
- `git commit`, create the public GitHub repo, push. Public from the start: it is the proof
  of the "newly created" rule.
- Install deps, get `DATABASE_URL`, **run `preflight.py` until it exits 0**. Fix or document
  every failing gate. Record the answer to §5.3 in the README.
- Apply `db/001_schema.sql` to a fresh `axiom` database. It has never run — expect syntax
  fixes on `fnv32` / `crdb_internal.datums_to_bytes`, the inline `VECTOR INDEX` clauses
  inside `CREATE TABLE`, and the `SET CLUSTER SETTING` line needing admin. `SHOW CREATE
  TABLE axiom_task` afterwards and confirm no column families appeared.
- Write `db/002_seed.sql`: system tenant, one demo tenant, one `refund_authority` policy
  (ACTIVE, `max_auto_action_cents` = e.g. $200), 30 order exceptions.

**Day 2 — the core loop, no LLM yet**
- Python worker: register agent → heartbeat → CLAIM → PREPARE → DISPATCH → SETTLE against a
  **fake provider** with injectable latency and failures. Hard-code the "decision"; no
  Bedrock in the loop yet.
- Retry wrapper for `40001` on every transaction. This is not optional under SERIALIZABLE.
- Prove W1–W6 by killing the process with `SIGKILL` at each point. **If it does not survive
  a kill on day 2, nothing later matters.**

**Day 3 — memory, for real**
- Bedrock: Titan V2 embeddings (1024-d, normalized → cosine) + planner/decider calls.
- Write outcome memories inside the settle transaction. Implement the recovery ANN recall
  with the four pinned prefix columns.
- **`EXPLAIN` the recovery query and confirm the plan contains a `vector search` node with
  `prefix spans`, not a `scan`.** Paste that plan into the README.

**Day 4 — API + Mission Control**
- FastAPI: create mission, list tasks, task detail w/ event timeline, approvals inbox,
  kill-worker endpoint (demo control), memory browser.
- UI: live task grid, per-task state, the crash-window table as a live panel, big red
  **KILL WORKER** button. Design bar per house style — no default AI-staple fonts, no glow,
  no emoji. Deliberate type choice.

**Day 5 — AWS deploy**
- Dockerize; push to ECR; ECS Fargate service for workers + one for the API; ALB; S3 for
  static assets. **Do this on Day 5, not Day 7** — deployment always takes longer than the
  estimate, and the URL must be live and stable well before the deadline.
- Cost check for a month of uptime (§8.6).

**Day 6 — the extras that score**
- Managed MCP Server + read-only service account → Audit Agent.
- Invariant test suite (§9.1) in CI.
- Human approval flow end-to-end, including the single-use `decision_token`.
- Memory quarantine demo: poison a memory, quarantine it, watch it vanish from retrieval
  atomically, then enumerate every effect it licensed via `axiom_attempt_by_license`.

**Day 7 — the submission is the product**
- README rewrite: architecture diagram, crash-window table, setup instructions a judge can
  actually follow, explicit CockroachDB-and-AWS tool list, honest limitations section.
- Record and cut the video (§10). **Under 3 minutes, hard limit.**
- Devpost writeup.

**Day 8 (Aug 17) — submit a day early.** Aug 18 is for disasters only.

**Cut lines, in the order you cut them:** multi-region → compensating-saga tasks →
memory-poisoning demo → MCP Audit Agent → Mission Control polish. **Never cut:** the fused
transaction, the kill-the-worker demo, the crash-window table, the video.

## 8. Traps that will silently kill this

1. **Post-filtering ANN results.** `WHERE quarantined = false` *after* the vector search
   returns fewer than `LIMIT` rows and misses true neighbours, silently. That is why
   `retrieval_class` is a prefix column. Never filter on a non-prefix column at query time.
2. **Range predicates on a prefix column** (e.g. `trust_level >= 2`) **disable the vector
   index entirely.** Prefix columns must be pinned to exact values. That is why trust is
   folded into `retrieval_class`.
3. **Missing statistics / small tables → full scan.** The optimizer will not choose the ANN
   path on 200 rows. Seed thousands of memories before demoing performance, and run
   `ANALYZE`. This already produced one false negative (§5.2).
4. **Search vector as a subquery.** May prevent index selection. Pass a literal, or a bound
   parameter you have *verified* still produces a `vector search` node.
5. **`40001` retry errors are not bugs.** Under SERIALIZABLE they are the system working.
   Every transaction needs a retry loop with backoff. A demo that throws stack traces
   under contention reads as broken even when it is correct.
6. **Cost and uptime through Sep 15.** ALB (~$18/mo) + Fargate tasks + CockroachDB Cloud.
   Right-size to the smallest thing that stays up, set a billing alarm, and confirm the free
   cluster does not auto-suspend or expire mid-judging. **Put a synthetic uptime check on
   the demo URL and have it alert the operator.**
7. **The 16 KiB results-buffer ceiling.** Past it, CockroachDB can no longer auto-retry a
   statement server-side. Keep fat JSONB out of claim results — the schema already declines
   to store `payload` in the claim index for this reason.
8. **`SKIP LOCKED` + column families are incompatible.** Do not add families to `axiom_task`.
9. **Demoing on a laptop.** The video should show the deployed AWS system. A judge who
   suspects localhost discounts the whole entry.
10. **Bedrock model access is not automatic.** Request access to the Titan embeddings and
    the planner model in the target region *early* — enablement can take hours.

## 9. How to make it better than currently specified

Ranked by score-per-hour.

**9.1 The invariant test suite — highest leverage, do not cut.**
Not "tests pass" but *"here are the seven ways this system could corrupt state, and here is
a test that tries to cause each one and fails."* Each of W1–W7 gets a test that kills a
worker or races two of them at exactly the wrong instant and asserts the outcome. Run them in
CI, put the green badge and the list in the README. This single artifact moves criteria #2
and #4 more than any feature.

**9.2 Chaos in the demo, not just in the tests.** A "CHAOS" toggle in Mission Control that
kills a random worker every N seconds while the mission runs to completion, correctly. Then
show the ledger: 30 exceptions, 30 refunds, **zero duplicates**, with kills marked on the
timeline. That is the screenshot that wins.

**9.3 The counterexample panel.** Run the *same* mission through a naive
transcript-memory agent (a deliberately simple baseline you also write) and show it refund
customer #18 twice. Side by side with AXIOM's ledger. Judges grade against a mental
baseline; supply the baseline yourself and the differentiator becomes undeniable. ~2 hours.

**9.4 `AS OF SYSTEM TIME` as a product feature, not a trick.** A "rewind" control that
answers *"what did the agent believe at 14:32:07, and why did it act?"* — historical ANN
against a past timestamp. Nobody else will do this. Caveat honestly: AOST is bounded by
`gc.ttlseconds` and yields a read-only transaction, which is exactly why `valid_from` /
`valid_until` exist as the durable audit axis.

**9.5 Fallback if §5.3 fails.** If in-transaction ANN recall does not see uncommitted rows,
restructure: recall *first* on committed state, pin the retrieved memory ids into the
transaction, then write — and state plainly in the README that the read is
read-your-committed-state within one serializable transaction. The safety argument survives;
adjust the wording, do not adjust the truth.

**9.6 Contribute a skill upstream.** Write one genuinely useful skill for
`cockroachlabs/cockroachdb-skills` — a crash-safe-queue skill capturing the partial-index /
fence / no-delete pattern is the obvious candidate. An accepted or even open PR is a
credibility artifact no other submission will have, and it satisfies the Agent Skills
requirement in the strongest possible way. ~1 hour, mostly extraction from work already done.

**9.7 Multi-region, only if time genuinely remains.** `REGIONAL BY ROW` on `axiom_task` with
a survival goal, showing a mission surviving a simulated region loss. Highest ceiling on
criteria #2 and #5 and a thing only CockroachDB can do — but it is the first cut when the
schedule slips. Do not start it before Day 6.

## 10. The video — 3 minutes, hard limit

Roughly:

- **0:00–0:20 — the question.** "An agent refunds $300. It crashes before recording it. It
  restarts. Does customer #18 get refunded twice?" No logo, no team intro, no roadmap.
- **0:20–0:40 — the reframe.** "Agent memory is treated as recall. The memory that matters
  in production is the memory of what the agent has already *done*." Show the four classes,
  land the line: *vector memory tells the agent what it could do; transactional execution
  state decides what it may do.*
- **0:40–1:40 — the demo.** Mission runs on the deployed AWS system. **Kill a worker on
  camera, mid-refund.** Another worker recovers: it reads the receipt, semantically recalls
  what happened the last time an agent died at this exact state, and resumes — one commit.
  Provider ledger: one refund, not two.
- **1:40–2:20 — why CockroachDB.** The fused transaction. The partial claim index answering
  the documented hotspot. Quarantine a poisoned memory and watch it leave retrieval
  atomically. The MCP Audit Agent answering "was anyone ever refunded twice?" in natural
  language against the same live database.
- **2:20–2:50 — production posture.** Crash-window table, invariant tests green, approvals,
  budget cap, multi-tenancy. Say "effectively-once, not exactly-once" **out loud**.
- **2:50–3:00 — the line.** "Memory is not what the agent remembers. It's what makes the
  agent safe to run."

Real screen recording, clean audio, no stock music over narration, no slide deck of bullet
points. Style rules: no glow, no floating dots, no emoji, crisp glass, fast well-aligned VO.

## 11. Open items for the operator — do not decide these yourself

1. **Eligibility.** The rules require entrants to be 18+ or at the age of majority in their
   jurisdiction, with no parent/guardian provision. The operator is, per project notes, a
   high-school student. **Raise this once, plainly, and let them decide** — options include
   confirming their own status, entering with an eligible team member as the registered
   entrant, or building it anyway as a portfolio and open-source artifact (the engineering
   stands on its own regardless). Do not quietly assume either way, and do not moralize.
2. **The `adam` SQL user / cluster password.** Needed before anything runs. Prefer minting a
   fresh user via `ccloud` so the command lands in the README.
3. **AWS account, region, and budget** for a month of judging uptime; Bedrock model access
   in that region.
4. **GitHub repo** — public, under which account.
5. **Team or solo** on the Devpost submission.

---

## Appendix A — Green-light checklist

Nothing ships until every line is checked.

```
[ ] preflight.py exits 0; §5.3 answered and written into the README
[ ] 001_schema.sql applies clean to a fresh database
[ ] SHOW CREATE TABLE axiom_task shows NO column families
[ ] EXPLAIN of the recovery query shows `vector search` + `prefix spans` (not `scan`)
[ ] W1–W7 each have a test that tries to break the invariant and fails
[ ] SIGKILL a worker mid-refund → exactly one provider effect, verified in the ledger
[ ] Two workers racing one step → loser gets 23505, no second call
[ ] Stale-epoch settle is rejected
[ ] Budget cap cannot be exceeded by concurrent PREPAREs
[ ] Quarantining a memory removes it from retrieval in the same transaction
[ ] Cross-tenant read returns zero rows; in-tenant read returns rows
[ ] Demo URL live on AWS, reachable from a machine that is not the dev laptop
[ ] Uptime monitor on the demo URL, alerting the operator, through Sep 15
[ ] README: setup a stranger can follow, arch diagram, crash-window table, limitations
[ ] README states "effectively-once", never "exactly-once"
[ ] LICENSE present; repo public; history shows creation inside the submission window
[ ] Video < 3:00, uploaded, unlisted-or-public, link tested in a logged-out browser
[ ] Devpost: CockroachDB tools (all 4) and AWS services listed explicitly
[ ] Submitted by Aug 17, not Aug 18
```

## Appendix B — Links

- Hackathon: https://cockroachdb-ai.devpost.com/ · rules: `/rules` · resources: `/resources`
- Managed MCP Server: https://www.cockroachlabs.com/docs/cockroachcloud/connect-to-the-cockroachdb-cloud-mcp-server
- ccloud CLI: https://www.cockroachlabs.com/docs/cockroachcloud/ccloud-get-started
- Agent Skills repo: https://github.com/cockroachlabs/cockroachdb-skills
- CockroachDB + AI / vector search: https://www.cockroachlabs.com/docs/v26.2/cockroachdb-and-ai.html
- Free cluster: https://cockroachlabs.cloud/signup · AWS free tier: https://aws.amazon.com/free/
- Community Slack: https://www.cockroachlabs.com/join-community/

## Appendix C — Status legend for anything you add

- ✅ **VERIFIED** — you ran it and saw the output. Cite where.
- ⚠️ **ASSUMED** — reasonable, untested. Say what would disprove it.
- ❌ **DISPROVEN / FAILED** — say what actually happened, not what was hoped.

Keep the marks accurate even when the answer is inconvenient. The entire project is an
argument that systems should tell the truth about what they have and have not done; a
handoff document that overstates its own status fails its own thesis.
