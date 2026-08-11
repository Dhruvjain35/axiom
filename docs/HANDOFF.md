# AXIOM — Handoff

**Written 2026-08-10. Last updated 2026-08-10 (build session 2). Deadline 2026-08-18 17:00 EDT.**

> **Status in one line:** the engine is BUILT and PROVEN — 30/30 tasks completed through
> 12 SIGKILLs with zero duplicate refunds — and the core CockroachDB claim is verified.
> Jump to §5 for exactly what exists, §5.4 for what was proven, §5.5 for the two real bugs
> that running it (rather than reading it) uncovered.

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

1. Read this file end to end.
2. Read `db/001_schema.sql` end to end. Every non-obvious choice has a `WHY` comment.
   The comments are the design doc; do not change a line without reading its comment.
3. Bring the environment up and prove it still works before changing anything:

   ```bash
   cd ~/axiom

   # local CockroachDB (the Cloud cluster password is still unavailable — see §11)
   mkdir -p .local-crdb && cd .local-crdb
   nohup ../cockroach-v26.2.3.darwin-10.9-amd64/cockroach start-single-node \
       --insecure --store=./data --listen-addr=localhost:26257 --http-addr=localhost:8081 \
       > crdb.log 2>&1 &
   cd ..

   export DATABASE_URL='postgresql://root@localhost:26257/axiom?sslmode=disable'
   export AXIOM_OFFLINE=1        # deterministic embeddings + rule triage, no AWS needed

   ./.venv/bin/python scripts/chaos_demo.py --workers 3 --kill-every 1.8 --quiet
   ```

   That last command is the whole project in one line. It must end in `PASS:` with
   `DUPLICATE REFUNDS 0`. If it does not, stop and fix that before anything else.
4. `git log` — the work is committed now. Keep it that way; commit after every working step.
5. Skim §7 (what is left) and §8 (traps).

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

**Repo:** `~/axiom`, committed (`856e4c2` + follow-ups). Still local-only — no GitHub
remote yet (§11).

```
~/axiom
├── axiom/                   THE ENGINE — built and working this session
│   ├── config.py            settings; SYSTEM_TENANT, EMBED_DIMS=1024, SHARD_COUNT=16
│   ├── db.py                pool, tx() with 40001 retry + full jitter, vector_literal()
│   ├── models.py            enums mirroring the schema; CLAIMABLE_STATES is the single
│   │                        source of truth for the claim predicate
│   ├── embeddings.py        Bedrock Titan V2 (verified live) + deterministic offline mode
│   ├── llm.py               Bedrock Claude triage + rule-based offline mode
│   ├── events.py            append-only journal, gap-free per-subject seq
│   ├── memory.py            write / recall / quarantine / effects_licensed_by
│   ├── policy.py            procedural memory, versioned, one ACTIVE enforced by index
│   ├── tasks.py             THE CORE: the five protocols + approvals + failures
│   ├── provider.py          the external world (separate DB) + chaos injection
│   ├── worker.py            the process you are meant to kill
│   └── seed.py              demo tenant, policy, mission, 30 exceptions, 10 prior memories
├── db/001_schema.sql        747 lines — APPLIED CLEAN, first try
├── db/003_provider.sql      the provider ledger, in its own database
├── scripts/preflight.py     17 gates — 16 blocking green, 1 advisory characterization
├── scripts/chaos_demo.py    the headline demo — PASSES
├── docs/HANDOFF.md          this file
├── .venv/                   psycopg[binary], psycopg_pool, boto3, fastapi, uvicorn, pytest
└── .local-crdb/             local single-node cluster (gitignored)
```

Built in parallel by a follow-up agent fan-out and **verified adversarially** — see §5.6
for the honest verdict on each: `tests/`, `axiom/api.py`, `axiom/audit_mcp.py`, `web/`
(Mission Control), `Dockerfile` + `docker-compose.yml` + `deploy/`, and the rewritten
`README.md` with `docs/ARCHITECTURE.md`, `docs/CRASH_WINDOWS.md`, `docs/SUBMISSION.md`.

### 5.1 Two clusters, and which one you can reach

**Local (works right now, use this):** CockroachDB **v26.2.3**, insecure single node at
`localhost:26257`, started from the vendored binary. The schema, the provider database,
the whole engine and the chaos demo all run against it. Vector indexing is enabled and
the ANN path is confirmed in use.

**CockroachDB Cloud (exists, currently unreachable):**
`axiom-memory-31580.j77.aws-us-east-1.cockroachlabs.cloud:26257`, user `adam`,
**v26.2.5**, AWS `us-east-1`. ✅ VERIFIED serializable-by-default, vector indexing
enabled, `VECTOR(1024)` accepted, `CREATE VECTOR INDEX … vector_cosine_ops` backfilled.
**The password is not stored anywhere in this environment**, so nothing has been run
against it since. Getting it (or minting a fresh SQL user with `ccloud`, which also
exercises a required hackathon tool) is the top item in §11.

⚠️ `001_schema.sql`'s header still says *"Target: v25.4+ … validated on v25.4.14"*. It has
now actually been validated on **v26.2.3** locally and its DDL was accepted on **v26.2.5**
in the Cloud. Update that header to say what was really run.

### 5.2 AWS is live

✅ VERIFIED this session with real calls:

- Credentials work — account `704229156617`, IAM user `solace-dev`, `us-east-1`.
- **Bedrock Titan V2** (`amazon.titan-embed-text-v2:0`) returns a **1024-d** embedding,
  matching the `VECTOR(1024)` the schema pins.
- **Bedrock Claude** models are available in-region, including
  `anthropic.claude-sonnet-4-5-20250929-v1:0` (the configured planner).

So the "≥1 AWS service" requirement is satisfiable with real calls, not aspiration.
Nothing has been *deployed* — no ECS, no ALB, no billable resources created.

### 5.3 THE CORE CLAIM IS PROVEN

The question §5.3 of the previous handoff called "the single most important open
question" now has an answer, and it is the good one.

```
=== gate 6: THE CORE CLAIM — write + semantic recall in ONE transaction ===
  [PASS] uncommitted memory is recallable in-transaction :: UNCOMMITTED: agent died at ACTION_PREPARED
  [PASS] in-transaction recall still uses the index
```

A memory written inside a transaction **is** returned by an ANN search in that same
transaction, **and the plan still uses the vector index** rather than degrading to a
scan. The fused recovery path in §6.5(e) is therefore real, and the README may say so.

Full preflight result — **16/16 blocking gates pass**, one advisory:

| Gate | Result |
| --- | --- |
| serializable by default | ✅ |
| `feature.vector_index.enabled` settable | ✅ |
| `VECTOR(1024)` column | ✅ |
| vector index backfill | ✅ 82s for 5,000 rows |
| cosine `<=>` uses the index @ 5,000 rows | ✅ `vector search` + `prefix spans` |
| L2 `<->` uses the index | ✅ |
| **subquery search vector uses the index** | ⚠️ **NO — advisory** |
| **bound-parameter search vector uses the index** | ✅ **YES — so app code is fine** |
| ANN returns rows / no cross-tenant leakage | ✅ |
| **uncommitted memory recallable in-transaction** | ✅ |
| **in-transaction recall still uses the index** | ✅ |
| rewind (`AS OF SYSTEM TIME`) | ✅ 4,501 rows recovered post-delete |
| ANN query works `AS OF SYSTEM TIME` | ✅ (must be on the **top-level** statement) |
| `gc.ttlseconds` readable | ✅ 14400 (4h) locally |
| follower reads | ✅ |
| `vector_search_beam_size` tunable | ✅ |

Three findings worth carrying forward:

1. **A subquery search vector defeats the vector index; a bound parameter does not.** The
   old log's `FULL SCAN` had two confounds (subquery + 4 rows); both are now isolated. The
   rule is enforced in exactly one place, `db.vector_literal()`.
2. **`AS OF SYSTEM TIME` must sit on the top-level statement.** Wrapping an AOST select in
   `SELECT count(*) FROM (…)` fails. `db.tx(as_of=…)` sets it for the whole transaction.
3. **`sin()` has no `DECIMAL` overload** in CockroachDB — `generate_series` yields INT and
   `INT * DECIMAL` is DECIMAL, so vector fixtures need an explicit `::FLOAT8`.

### 5.4 What the chaos demo actually proves

`scripts/chaos_demo.py` seeds a mission, spawns workers, and **SIGKILL**s a random live
one every 1.8 s — no signal handler, no `finally`, no polite lease release, exactly what
an OOM kill or a spot reclamation looks like. Real measured run:

```
  wall clock                23.3s
  workers SIGKILLed         12
  worker restarts           16
  approvals answered         3   (policy sent them to a human)
  tasks terminal          30/30   {'SUCCEEDED': 27, 'DEAD_LETTER': 3}
  ----------------------------------------------------------------
  refunds created           18
  dollars moved         $2,042.04
  idempotent replays         2   (re-sends the provider absorbed)
  provider verdicts     {'created': 18, 'replayed': 2}
  DUPLICATE REFUNDS          0

PASS: 12 kills, 2 re-sends absorbed by the provider, 0 duplicate refunds.
```

An earlier, harsher run: **87 SIGKILLs, 95 restarts, 5 replays, 0 duplicates.**

Two things make this evidence rather than theatre:

- **The provider is a genuinely separate database** (`db/003_provider.sql`), reached over
  its own connection with autocommit, which AXIOM *cannot* enlist in its transactions.
  That is the real relationship an application has with a payments API, minus the network.
  A fake provider inside our transaction would make the demo pass and prove nothing.
- **The script fails on zero replays.** A run where no crash landed in the dangerous
  window proved nothing, so `INCONCLUSIVE` is a distinct, loud outcome from `PASS`.

### 5.5 Two real bugs, found by running it

Both were invisible on the page and obvious the moment the thing ran. Keep the scar
tissue in the comments; they are the best argument in the codebase for the test suite.

**Bug 1 — an exception rolled back the transaction that recorded the decision.**
`prepare()` signalled "this needs a human" by raising `NeedsApproval`. The exception
propagated out of `db.tx()`, so the connection context manager **rolled back** — throwing
away the `axiom_approval` row and the `AWAITING_APPROVAL` transition the same transaction
had just written. The task snapped back to `READY`, got re-claimed, parked again, and
looped forever while `axiom_approval` stayed **empty**. Symptom: three tasks stuck at
`lease_epoch 9` with zero approvals in the table. Fix: `prepare()` now returns a
`PrepareResult(receipt | approval_id)`. *An exception is a fine way to abort a transaction
and a terrible way to return a value from one.*

**Bug 2 — the approval was granted and then ignored.** `consume_approval()` existed and
nothing called it. An approved task was re-claimed, re-evaluated against the **unchanged**
policy ceiling, and parked again — the policy had not moved and never would; the approval
was the thing that changed. The demo answered 1,187 approvals for 3 tasks before this was
caught. Fix: `prepare()` now burns the single-use decision token *before* the authority
check, so a human decision authorizes exactly one action and cannot be replayed into a
second refund by a restarting worker.

Note what both have in common: they are failures of the **approval** path, the one path
the happy-path demo never touched. That is exactly the class of bug the W1–W7 suite exists
to catch.

### 5.6 Verified status of the parallel workstreams

⚠️ Each of these was built by a subagent and then re-run by a separate adversarial
verifier, because a build report is not evidence. Treat any row below marked PARTIAL as
work in progress, and read that workstream's own notes before trusting it.

*(This table is filled in at the end of the session — see the run summary appended in
§12. If §12 is missing, the fan-out did not complete and you should verify each of
`tests/`, `axiom/api.py`, `web/`, `deploy/`, and `README.md` yourself before relying on
them.)*


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

## 7. What is left — 7 days

Day 1 of the original plan is **done**: preflight green, schema applied, engine built,
chaos demo passing, everything committed. Re-ordered by risk retired per hour from here.

**Now (highest value, do first)**
1. **Get the Cloud cluster back.** Everything so far runs on a local single node. The
   submission needs the Cloud cluster (it is the "distributed" in distributed vector
   indexing, and `ccloud` is a required tool). Mint a fresh SQL user via `ccloud`, apply
   `001_schema.sql` + `003_provider.sql`, and re-run **preflight** and the **chaos demo**
   against Cloud. Expect differences: real network latency, real contention, `40001` rates
   that a single node never produces. Quote Cloud numbers in the README, not laptop numbers.
2. **Push to a public GitHub repo.** The "newly created during the submission period" rule
   is proven by the history, and the history is currently on one laptop.
3. **Re-run the invariant suite against Cloud**, not just locally.

**Then (scores directly)**
4. Deploy to ECS Fargate + ALB, get the demo URL live and *stable*, and put an uptime
   monitor on it that alerts. It must answer on Sep 15.
5. Managed MCP Server + read-only service account → the Audit Agent, running for real
   against the Cloud cluster rather than in local-fallback mode.
6. Record the video (§10). Under 3 minutes. Deployed system on screen, not localhost.
7. Devpost writeup from `docs/SUBMISSION.md`.

**If time remains, in this order:** the counterexample panel (§9.3) → contribute a skill
upstream (§9.6) → `AS OF SYSTEM TIME` rewind as a product feature (§9.4) → multi-region
(§9.7, first to be cut).

**Submit Aug 17, not Aug 18.**

**Never cut:** the fused transaction, the kill-a-worker demo, the crash-window table, the
invariant suite, the video.


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
    (✅ Already confirmed working in `us-east-1` on account `704229156617`.)

Learned the hard way this session — all four cost real time:

11. **Never signal a decision out of `db.tx()` with an exception.** The rollback takes the
    row that recorded the decision with it. Return a result object. See §5.5 bug 1.
12. **A granted permission that nothing consumes is not a permission.** If a check fails,
    something must *change* before the next attempt, or the task loops forever against an
    unchanged rule. See §5.5 bug 2.
13. **`SET LOCAL x = %s` fails with a bound parameter** — the value arrives as a string and
    CockroachDB rejects it ("requires an integer value"). Interpolate a validated `int()`.
14. **A demo that never exercises the human-in-the-loop path is not a passing demo.** Both
    real bugs lived exclusively in the approval branch, which the happy path never touched.
    Whatever branch your demo skips is where your bugs are.

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
2. **The Cloud cluster password — the top blocker.** Everything currently runs on a local
   single node. `axiom-memory-31580.j77.aws-us-east-1.cockroachlabs.cloud` exists and was
   verified in an earlier session as user `adam`, but the password is stored nowhere here.
   Prefer minting a fresh SQL user with `ccloud` so the command lands in the README and a
   required tool gets exercised. Until this is resolved, no number in the submission can
   honestly be labelled "CockroachDB Cloud".
3. **AWS budget approval for a month of uptime.** Credentials work (account
   `704229156617`, IAM user `solace-dev`) and Bedrock is enabled, but **nothing has been
   deployed and no billable resource has been created** — that is deliberately the
   operator's call. See `deploy/COST.md` for the real line items. Note the account name
   suggests it is shared with another project; confirm before adding cost to it.
4. **GitHub repo** — public, under which account. The repo is committed locally with no
   remote. The public history is what proves the "newly created" rule.
5. **Team or solo** on the Devpost submission.
6. **Whether to keep the demo's LLM path on Bedrock Claude or run the whole demo offline.**
   Offline is deterministic, free, and hermetic; Bedrock is what the hackathon rewards.
   Current default is Bedrock for the demo, offline for tests. Confirm that is what you want.

---

## Appendix A — Green-light checklist

Nothing ships until every line is checked.

`[x]` = done and personally verified on the LOCAL cluster this session.
`[c]` = done locally, **still to be re-verified against CockroachDB Cloud.**

```
[x] preflight.py exits 0; the core claim is answered YES (§5.3)
[x] 001_schema.sql applies clean to a fresh database
[x] SHOW CREATE TABLE axiom_task shows NO column families
[x] EXPLAIN shows `vector search` + `prefix spans` (not `scan`) at 5,000 rows
[x] SIGKILL a worker mid-refund → exactly one provider effect, verified in the ledger
[x] 30/30 tasks terminal through 12 SIGKILLs, 0 duplicate refunds
[x] LICENSE present; git history exists inside the submission window
[ ] W1–W7 each have a test that tries to break the invariant and fails
[ ] Two workers racing one step → loser gets 23505, no second call
[ ] Stale-epoch settle is rejected
[ ] Budget cap cannot be exceeded by concurrent PREPAREs
[ ] Quarantining a memory removes it from retrieval in the same transaction
[ ] Cross-tenant read returns zero rows; in-tenant read returns rows
[ ] Approval token is single-use; approved task proceeds exactly once
[c] preflight + chaos demo re-run against CockroachDB CLOUD, numbers quoted from Cloud
[ ] repo pushed public to GitHub
[ ] Demo URL live on AWS, reachable from a machine that is not the dev laptop
[ ] Uptime monitor on the demo URL, alerting the operator, through Sep 15
[ ] Audit Agent running over the real Managed MCP Server, not the local fallback
[ ] README: setup a stranger can follow, arch diagram, crash-window table, limitations
[ ] README states "effectively-once", never "exactly-once"
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
