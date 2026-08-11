# AXIOM

**Durable memory for agents that take real actions.**

An agent is told to resolve 30 order exceptions. It issues a $300 refund to customer #18.
Then the process dies — OOM, deploy, spot reclamation — *before* it records that the refund
succeeded.

It restarts. What happens to customer #18?

In most agent frameworks, the answer is: nobody knows. The framework reconstructs context
from a conversation transcript, sees an unfinished task, and refunds again. That gap is the
entire distance between an agent demo and an agent you would let near a payments API.

AXIOM closes it.

---

## The idea

> Memory is not saved chat history. Memory is what makes autonomous **action** safe.

Agent memory is usually treated as recall — remember the user's name, remember the last ten
turns. That framing is why agents are unsafe to automate with. The memory that actually
matters in production is the memory of **what the agent has already done**, and it has to be
durable across the crash, correct under concurrency, and auditable afterward.

AXIOM models four classes of memory with **different authority**:

| Class | Question it answers | Where it lives | Authority |
| --- | --- | --- | --- |
| **Episodic** | What happened the last time we saw this? | `axiom_memory` (`EPISODIC`) | advises |
| **Semantic** | What past situations resemble this one? | `axiom_memory` (`SEMANTIC`) | advises |
| **Procedural** | What policy applies here, and which version of it? | `axiom_policy` | authorizes |
| **Execution** | What has this agent already *done* — irreversibly, in the real world? | `axiom_task` + `axiom_action_attempt` | **constrains** |

The first three advise. The fourth constrains.

**Vector memory tells the agent what it *could* do; transactional execution state decides
what it *may* do.**

That distinction is enforced by the type system and the schema, not by convention.
`axiom/llm.py` returns a `Triage` proposal and physically cannot mint an idempotency key;
only `tasks.prepare()` can authorize an act, and the key it mints is a `GENERATED STORED`
column the application never supplies.

---

## Measured results

Not a description of what it should do. This is the output of `scripts/chaos_demo.py`, run
on 2026-08-11 against **CockroachDB Cloud v26.2.5** (cluster `axiom-memory`, BASIC plan,
AWS `us-east-1`), with `AXIOM_OFFLINE=1` so the run needs no model credentials:

```
====================================================================
AXIOM chaos demo — result
====================================================================
  wall clock                94.1s
  workers SIGKILLed       30
  worker restarts         42
  approvals answered      3   (policy sent them to a human)
  tasks terminal          30/30   {'SUCCEEDED': 27, 'DEAD_LETTER': 3}
  ----------------------------------------------------------------
  refunds created         18
  dollars moved           $2,042.04
  idempotent replays      6   (re-sends the provider absorbed)
  provider verdicts       {'created': 18, 'replayed': 6}
  DUPLICATE REFUNDS       0
====================================================================

PASS: 30 kills, 6 re-sends absorbed by the provider, 0 duplicate refunds.
```

The same run against an isolated single-node **v26.2.3** on a laptop: 33 kills, 5 replays,
0 duplicates, 62.4 s. The result does not depend on the topology; the Cloud run is simply
the one whose latency and contention are real.

Alongside it, on the same Cloud cluster:

| | |
| --- | --- |
| `scripts/preflight.py` | **16/16 blocking gates pass** (1 advisory characterization) |
| `pytest` | **49 passed** in 222 s — every crash window and structural invariant |

## The counterexample

The claim "most agent frameworks would refund twice here" is easy to assert and easy to
discount, so AXIOM ships the comparison instead. `scripts/counterexample.py` runs the same
order, through the same crash, at the same instant, against the same provider — once with a
conversation-transcript agent and once with AXIOM:

```
================================================================================
                      TRANSCRIPT MEMORY                   AXIOM
================================================================================
killed in W4          yes                                 yes
memory consulted      2 transcript turns                  receipt + 5 recalled memories
policy gate           none — refunds $300 unattended      sent to a human first
recovery decision     retry — cannot know if it landed    RESEND under the same key
idempotency key       newly generated each attempt        axm_3e9d1a3bfdb24e74c11de9…
fence (lease_epoch)   n/a                                 2 -> 3

REFUNDS CREATED       2                                   1
idempotent replays    0                                   1
DOLLARS OUT           $600.00                             $300.00
================================================================================
```

**The baseline is not a strawman, and that is the point.** It persists its transcript to
disk with `fsync`, re-reads it on restart rather than starting blank, checks for evidence it
already acted, and records its intent *before* calling the provider — which is the best you
can do without a transaction. It still pays out twice, for a structural reason:

> After the crash, its transcript says *"I intended to refund order X"* and contains no
> completion. Two worlds are consistent with that and it cannot tell them apart: the call
> never went out, or the call went out and the process died before the write. It has to
> guess. And it cannot reuse the original idempotency key, because nothing ever minted that
> key anywhere durable.

AXIOM faces the identical ambiguity and does not have to guess, because the receipt and the
state transition committed together. "Did I already act?" is a question the database
answers, not one the agent infers from prose.

The run is deterministic — both agents are killed at exactly window W4 — so this is an
argument, not an anecdote. The script fails loudly with `INCONCLUSIVE` rather than `PASS` if
the baseline does not actually double-refund, so a rigged run cannot masquerade as a result.

The demo **SIGKILL**s a random live worker every 1.8 seconds. Not `SIGTERM` — no signal
handler runs, no `finally` block runs, no lease is politely released. That is what an OOM
kill, a spot reclamation and a `docker kill` all look like.

What makes this evidence rather than theatre:

- **The provider is a genuinely separate database** (`db/003_provider.sql`), reached over
  its own connection with autocommit, which AXIOM *cannot* enlist in its transactions. That
  is the real relationship an application has with a payments API, minus the network. A fake
  provider inside our transaction would make the demo pass and prove nothing.
- **The script fails on zero replays.** A run where no crash happened to land in the
  dangerous window proved nothing, so `INCONCLUSIVE` is a distinct, loud outcome from `PASS`.

### The independent cross-check

AXIOM's own books and the provider's ledger were reconciled after the run. They agree, and
neither was derived from the other:

| | AXIOM | Provider |
| --- | --- | --- |
| Successful receipts / refund rows | 18 | 18 |
| Distinct idempotency keys | 18 | 18 |
| Distinct orders refunded | — | 18 |
| Money committed | `spent_cents` = 204,204 | `sum(amount_cents)` = 204,204 |
| Orders refunded more than once | — | **0** |

30 tasks were claimed 46 times (`sum(lease_epoch)`), so **16 claims were takeovers of a task
whose previous owner had been killed**. The highest a single task's fence reached was 6. The
journal recorded 245 events, 13 of them `task.recovered`.

### One task, in full

`ORD-1027` was killed twice, at the worst possible moment both times. Its journal
(`axiom_event`, one row per transition, written in the same transaction as the transition):

| seq | event | from | to | lease_epoch |
| --- | --- | --- | --- | --- |
| 1 | `task.enqueued` | | READY | |
| 2 | `task.claimed` | | LEASED | 1 |
| 3 | `attempt.prepared` | LEASED | ACTION_PREPARED | 1 |
| 4 | `task.claimed` | | ACTION_PREPARED | 2 |
| 5 | `task.recovered` → **RESEND** | ACTION_PREPARED | ACTION_PREPARED | 2 |
| 6 | `task.claimed` | | ACTION_PREPARED | 3 |
| 7 | `task.recovered` → **RESEND** | ACTION_PREPARED | ACTION_PREPARED | 3 |
| 8 | `attempt.settled` | ACTION_PREPARED | SUCCEEDED | 3 |

Both recoveries re-dispatched under the identical key
`axm_5722c72bd44fc74f50f50496727bca809f65585d63cfb98c`. The provider's own request log —
which AXIOM never writes to — saw three requests and made one refund:

```
verdict     http_status   received_at
created     201           02:55:50.119
replayed    200           02:56:10.112
replayed    200           02:56:30.223
```

Ledger: one row, `re_da08deb5287c47899857`, `$169.40`, `replay_count = 2`.

Three requests. One effect. That is the whole thesis.

---

## The crash-window table

This is the correctness spec. Every window has a defined outcome, the outcome is a
consequence of commit ordering rather than a hope about timing, and **every window has a test
that tries to cause the failure and fails** (`tests/test_crash_windows.py`). One page per
window — what may exist in the world at that instant, what guarantees the result, and what
covers it — is in [docs/CRASH_WINDOWS.md](docs/CRASH_WINDOWS.md).

| # | Crash point | Effect possible? | Recovery action | What guarantees the outcome |
| --- | --- | --- | --- | --- |
| **W1** | After CLAIM, before PREPARE | No | Re-claim with a new epoch; re-plan freely | Nothing was authorized: the receipt commits *before* the task can leave `LEASED` |
| **W2** | After receipt COMMIT, before the send | Yes, unknowably | Re-dispatch under the **same** derived key | Provider dedupes on the key; effectively-once |
| **W3** | Mid-flight, outcome unknown | Yes | Re-dispatch under the same key | Same as W2 — the two are indistinguishable to us, and deliberately treated identically |
| **W4** | Provider responded, before SETTLE | **Yes — the effect landed** | Re-dispatch under the same key; provider returns the *original* refund; settle records it | Exactly one real-world effect. Observed live: `ORD-1027` above |
| **W5** | Zombie worker settles after its lease expired | Yes | Its settle is rejected on a stale `lease_epoch` | The fence, not the lease, is the invariant |
| **W6** | Two workers PREPARE the same step | No | Loser gets `23505` | Unique partial index `axiom_attempt_one_live` |
| **W7** | Recovered LLM re-synthesizes a *different* request body | Yes | `request_fingerprint` mismatch → **hard stop**, escalate | Same key + different intent is not a retry |

W5 is the subtle one, and worth saying out loud: **a lease expiring does not stop a
GC-paused worker that is already inside a refund HTTP call.** The lease is a liveness
optimization. The monotonic per-row `lease_epoch` is the correctness guarantee, re-checked
by every write after the claim (`tasks._assert_fence`).

---

## Why this needs CockroachDB

Because execution state and semantic memory commit in a **single serializable transaction**.

When a worker recovers a task orphaned by a dead peer, `tasks.recover()` does all of this
once, atomically:

1. re-checks the fencing token,
2. point-reads the durable receipt of what the dead worker had already done,
3. runs an ANN search over episodic memory for what happened the *last* time an agent died
   at this exact execution state, filtered to memories it is allowed to act on,
4. decides RESEND / ESCALATE / REPLAN by aggregating over the recalled outcomes,
5. appends the decision and its evidence to the journal.

**One commit.** Split that across a workflow engine plus a vector database and four
specific things break:

- **Partial application.** The budget debit, the receipt insert, the journal append and the
  outcome memory must be all-or-nothing. Split them and you get a receipt with no budget
  debit (silent overspend) or a debit with no receipt (money marked spent that never moved).
- **No serialization point for admissibility.** AXIOM's quarantine is an `UPDATE` to a
  computed column that is a *vector index prefix* — the poisoned row physically moves to a
  different partition of the index, at commit, atomically. Verified live: inside one
  transaction, quarantining the top hit (a `DUPLICATE_EFFECT` memory at cosine similarity
  0.8359) removed it from the candidate set of the very next recall in that same
  transaction. Across two stores there is no instant at which "quarantine wins" becomes
  true, so an agent can act on evidence that was revoked while it was reading it.
- **You cannot read your own uncommitted memory.** Preflight gate 6 proves CockroachDB can:
  a memory written inside a transaction *is* returned by an ANN search in that same
  transaction, **and the plan still uses the vector index** rather than degrading to a scan.
  A vector database bolted onto a workflow engine has no such read.
- **Stale evidence with no transaction to close the window.** The agent resumes on memory
  that has already been superseded, and nothing in the architecture can detect it.

Durable execution engines store history that is opaque and not semantically queryable.
Vector databases have no transactions to join. One store, one commit, or you are racing.

The ANN path is not assumed. `EXPLAIN` of the recovery recall, on the same cluster as the
run above:

```
└── • vector search
      table: axiom_memory@axiom_memory_ann_by_context
      target count: 20
      prefix spans: [/'1111…1111'/'EPISODIC'/'state:ACTION_PREPARED'/'ACTIONABLE'
                   - /'1111…1111'/'EPISODIC'/'state:ACTION_PREPARED'/'ACTIONABLE']
```

All four prefix columns pinned to exact values. Not a `scan`.

---

## Architecture

```
                        Operator
                           │
                           ▼
                   API / Orchestrator
                           │
              ┌────────────┴────────────┐
              ▼                         ▼
        Amazon Bedrock            Worker agents
        Titan V2 embeddings       claim → prepare → dispatch → settle
        Claude triage             (the process you are meant to kill)
              │                         │
              └────────────┬────────────┘
                           ▼
              ┌────────────────────────────┐
              │        CockroachDB         │
              │                            │
              │  axiom_task           ─────┼──  EXECUTION memory (constrains)
              │  axiom_action_attempt ─────┼──  idempotency receipts
              │  axiom_policy         ─────┼──  PROCEDURAL memory (authorizes)
              │  axiom_memory         ─────┼──  EPISODIC + SEMANTIC (advises)
              │    2× C-SPANN vector index │
              │  axiom_event          ─────┼──  append-only journal
              └────────────┬───────────────┘
                           │
                           │  no shared transaction — this is the point
                           ▼
              ┌────────────────────────────┐
              │  payment provider (separate │
              │  database, own connection)  │
              │  Stripe idempotency semantics
              └────────────────────────────┘
```

The full version — the five protocols with their real SQL, the state machine, and why each
index is shaped the way it is — is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Setup

Verified end to end on 2026-08-11 by running these exact commands against a clean cluster:
macOS 26.4 (arm64), Python 3.14.5, CockroachDB v26.2.3 locally and v26.2.5 on Cloud.

> **Running against CockroachDB Cloud instead?** Skip steps 1–2 and provision with the
> `ccloud` CLI — this is also how the measured results above were produced:
>
> ```bash
> brew install cockroachdb/tap/ccloud
> ccloud auth login                                    # opens a browser
> ccloud cluster create basic axiom-memory --cloud aws --region us-east-1
> ccloud cluster user create axiom-memory axiom_app --password "$(openssl rand -base64 24)"
> ccloud cluster connection-string axiom-memory --sql-user axiom_app
>
> # Cloud uses its own CA, so fetch the cluster cert once:
> curl --create-dirs -o ~/.postgresql/root.crt \
>   "https://cockroachlabs.cloud/clusters/<CLUSTER_ID>/cert"
> ```
>
> Then use `?sslmode=verify-full` in `DATABASE_URL` and continue from step 3.
> `scripts/provision_ccloud.sh` wraps all of this.

**1. Get CockroachDB v25.4 or newer.** Vector indexing went GA in v25.4; it was Preview and
default-off before that. The schema asserts rather than assumes, so an older cluster fails
loudly instead of silently degrading.

```bash
curl -O https://binaries.cockroachdb.com/cockroach-v26.2.3.darwin-10.9-amd64.tgz
tar -xzf cockroach-v26.2.3.darwin-10.9-amd64.tgz
```

**2. Start a single node.**

```bash
mkdir -p .local-crdb
nohup ./cockroach-v26.2.3.darwin-10.9-amd64/cockroach start-single-node \
    --insecure --store=.local-crdb/data \
    --listen-addr=localhost:26257 --http-addr=localhost:8081 \
    > .local-crdb/crdb.log 2>&1 &
```

**3. Apply the schema.** Connect to `defaultdb` for this step — `001_schema.sql` creates the
`axiom` database itself, so it cannot be applied through a connection to a database that
does not exist yet.

```bash
CR=./cockroach-v26.2.3.darwin-10.9-amd64/cockroach
BOOT='postgresql://root@localhost:26257/defaultdb?sslmode=disable'

$CR sql --url "$BOOT" -f db/001_schema.sql       # 9 tables, 2 vector indexes
$CR sql --url "$BOOT" -f db/003_provider.sql     # the external world, separate database
$CR sql --url "$BOOT" -f db/002_audit_role.sql   # read-only role for the audit agent
```

**Apply `003` before `002`.** `002` grants the audit role `CONNECT` on the `provider`
database, which `003` creates; running them in numeric order fails with
`ERROR: database "provider" does not exist`.

All three print `NOTICE: waiting for job(s) to complete` while index backfills run. That is
normal; the statements block until the jobs finish. `002` is only needed if you want to run
the audit agent; the engine and the chaos demo do not use it.

**4. Python environment.**

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/pip install pytest==9.1.1     # only to run the test suite
```

If you only want the engine and the chaos demo in offline mode, two packages are enough —
`boto3` is imported lazily and is needed only when you point AXIOM at real Bedrock, and
FastAPI is only needed for the API:

```bash
./.venv/bin/pip install "psycopg[binary]" psycopg_pool
```

**5. Run the demo.**

```bash
export DATABASE_URL='postgresql://root@localhost:26257/axiom?sslmode=disable'
export AXIOM_OFFLINE=1     # deterministic embeddings + rule-based triage, no AWS needed

./.venv/bin/python scripts/chaos_demo.py --workers 3 --kill-every 1.8
```

It seeds itself, spawns three workers, kills one every 1.8 seconds, restarts them, and stops
when every task is terminal. It must end in `PASS:` with `DUPLICATE REFUNDS 0`. Expect
roughly 85 seconds.

Add `--quiet` to hide worker logs. For a calm run you must disable all three chaos sources —
`--kill-every` only stops the external SIGKILLs, while `--chaos-pre` and `--chaos-post` make
the worker kill *itself* at the two instants that matter:

```bash
./.venv/bin/python scripts/chaos_demo.py \
    --workers 1 --tasks 6 --kill-every 0 --chaos-pre 0 --chaos-post 0
```

Verified: 6/6 tasks terminal, 4 refunds, 0 kills, in 2.9 seconds.

**6. Run the invariant suite.**

```bash
./.venv/bin/python -m pytest -q
```

49 tests, **all 49 passing** — verified on both the local node and CockroachDB Cloud.
Two of them began as strict `xfail`s pinning real defects this suite found (an approval
that never self-healed, and attempt exhaustion that stranded a task in `READY`); both are
fixed in `axiom/tasks.py` and those tests now guard the fix.

The suite does not assert that AXIOM works;
it assembles the exact conditions under which the design would corrupt state — an expired
lease mid-refund, two workers holding the same fence, a recovered agent that re-synthesized a
different request body, threads racing one budget — and asserts that the system refuses. All
seven crash windows have a test (`tests/test_crash_windows.py`).

**7. Optional — verify the platform assumptions yourself.**

```bash
DATABASE_URL='postgresql://root@localhost:26257/defaultdb?sslmode=disable' \
  ./.venv/bin/python scripts/preflight.py
```

17 gates, 16 blocking. It loads 5,000 rows of synthetic 1024-dimension vectors, builds both
opclasses, and asserts on query *plans* rather than on output — including the one that
matters most, "is a memory written inside a transaction recallable by an ANN search in that
same transaction, with the index still in use?" My run: **16/16 blocking gates passed**, one
advisory note, vector index backfill 84.3s for 5,000 rows. Exit code 0.

**Running against AWS Bedrock instead of offline mode:** drop `AXIOM_OFFLINE`, set
`AWS_REGION`, and ensure model access is enabled for `amazon.titan-embed-text-v2:0` and
`anthropic.claude-sonnet-4-5-20250929-v1:0`. The engine cannot tell the difference — that is
the point of the provider interfaces in `embeddings.py` and `llm.py`.

---

## What is in the repo

| Path | What it is |
| --- | --- |
| `db/001_schema.sql` | 9 tables, 2 C-SPANN vector indexes. Every non-obvious choice carries a `WHY` comment; the comments are the design doc. |
| `db/002_audit_role.sql` | The read-only role the audit agent runs as. Database-enforced containment. |
| `db/003_provider.sql` | The external payment provider, in its own database. Separate on purpose. |
| `axiom/tasks.py` | The core. Five protocols: claim, prepare, dispatch, settle, recover. Plus approvals, budget, dead-letter. |
| `axiom/memory.py` | Episodic + semantic memory: write, recall, quarantine, `effects_licensed_by`. |
| `axiom/policy.py` | Procedural memory. Versioned, signable, exactly one ACTIVE version enforced by a unique partial index. |
| `axiom/db.py` | The pool, `tx()` with 40001 retry and full jitter, and the single audited place a vector becomes SQL. |
| `axiom/worker.py` | The process you are meant to kill. |
| `axiom/provider.py` | The external world, plus chaos injection at the three instants that matter. |
| `axiom/events.py` | Append-only journal, gap-free per-subject sequence. |
| `axiom/seed.py` | Demo tenant, policy, mission, 30 exceptions, 10 prior memories. |
| `axiom/api.py` | HTTP API over the engine. |
| `axiom/audit_mcp.py` | The audit agent: natural-language questions answered in SQL against the live database, under a read-only identity. |
| `tests/` | 49 tests, all passing (13 crash-window, 17 invariant, 5 recall-plan, 14 schema-sync). `test_crash_windows.py` covers W1–W7; `test_invariants.py` races the fence, the budget and the supersession chain; `test_recall_plan.py` asserts on query plans; `test_schema_sync.py` checks the enums against the live database. |
| `web/` | Mission Control front end. |
| `deploy/terraform/`, `Dockerfile` | Deployment infrastructure. Written, not applied — see Limitations. |
| `scripts/chaos_demo.py` | The headline demo. |
| `scripts/preflight.py` | 17 gates against a live cluster: 16 blocking (all pass on Cloud) + 1 advisory. |
| `docs/ARCHITECTURE.md` | The deep version: protocols, SQL, index design. |
| `docs/CRASH_WINDOWS.md` | One page per crash window, W1–W7, with what covers each. |

---

## CockroachDB tools used

The hackathon asks for a minimum of two of the four. Status is stated honestly per row —
what is wired and verified, and what is not.

| Tool | Status | How AXIOM uses it |
| --- | --- | --- |
| **Distributed Vector Indexing** | **In use, verified on Cloud** | Two C-SPANN indexes on `axiom_memory.embedding`: `axiom_memory_ann_by_context` (four prefix columns, the recovery path) and `axiom_memory_ann_by_tenant` (broad recall). `vector_cosine_ops` written explicitly, because omitting the opclass silently gives L2 and a `<=>` query then full-scans. Index use asserted from `EXPLAIN`, not assumed. |
| **Cloud Managed MCP Server** | **In use, verified against the live server** | `axiom/audit_mcp.py` speaks to the Managed MCP Server at `https://cockroachlabs.cloud/mcp` over streamable HTTP with a scoped service-account API key and the `mcp-cluster-id` header, discovering each tool's argument names from `tools/list` rather than guessing them. Verified end to end: `python -m axiom.audit_mcp --mode mcp "was any order ever refunded twice?"` returns *"Yes — 2 order(s) have more than one refund row: CE-BASELINE-… x4"* — correctly catching the **baseline** agent's double refunds while every AXIOM order has none. Containment is three independent layers: the `axiom_audit` role (`db/002_audit_role.sql`) has `SELECT` and nothing else, a statement guard rejects anything that is not a single `SELECT`/`WITH`, and the login is `default_transaction_read_only`. A LOCAL mode over a plain read-only connection answers the identical questions when no key is present. |
| **ccloud CLI** | **In use, verified** | The cluster the measured results ran on (`axiom-memory`, BASIC, AWS `us-east-1`, v26.2.5) is administered entirely through `ccloud`: `auth login`, `cluster list`, `cluster user create axiom_app`, `cluster connection-string`. `scripts/provision_ccloud.sh` wraps provisioning + all three migrations, and the Cloud path in Setup is the CLI transcript that actually worked. |
| **Agent Skills Repo** | **Not yet used** | Design intent: contribute a crash-safe-queue skill capturing the partial-index / fencing-token / never-DELETE pattern. |

## AWS services used

| Service | Status | How AXIOM uses it |
| --- | --- | --- |
| **Amazon Bedrock** | **Code paths built; live calls verified in an earlier session, not in the runs quoted here** | `amazon.titan-embed-text-v2:0` for 1024-dimension embeddings (matching the `VECTOR(1024)` the schema pins) in `axiom/embeddings.py`; `anthropic.claude-sonnet-4-5-20250929-v1:0` for exception triage in `axiom/llm.py`. Every number in this README was measured with `AXIOM_OFFLINE=1`, which swaps both for deterministic local stand-ins so the demo is hermetic. |
| **ECS Fargate** | **Infrastructure written, not applied** | `Dockerfile` and `deploy/terraform/` exist. No cluster, service or task definition has been created. Fargate is chosen over Lambda specifically because you must be able to SIGKILL a worker on camera. |
| **S3 / ALB** | **Infrastructure written, not applied** | `deploy/terraform/alb.tf`, `network.tf`, `iam.tf`, `logs.tf`. Nothing provisioned; there is no public URL. |

---

## What it does not claim

AXIOM does **not** provide exactly-once execution of external side effects. That guarantee
is not available to any system that calls a network API it does not control, and any project
claiming it is either wrong or not talking about the same thing.

AXIOM provides **durable, idempotent, effectively-once execution**: every external action is
issued under a derived idempotency key against a durable receipt, and every crash window has
a defined and tested outcome. **Effectively-once, never exactly-once.** The distinction is
the difference between a system you can reason about and a marketing claim.

## Limitations

Stated plainly, because a limitations section that only lists comfortable limitations is a
marketing document.

- **The Cloud cluster is BASIC, single-region `aws-us-east-1`.** Every number here was
  measured on it, so latency and 40001 contention are real — but a BASIC cluster is not a
  multi-region deployment, and AXIOM does not yet use `REGIONAL BY ROW` or a survival goal.
  Nothing here demonstrates surviving the loss of a region; that is the obvious next step
  and it is not built.
- **`gc.ttlseconds` is 4500 on that cluster (75 minutes).** The `AS OF SYSTEM TIME` rewind
  feature cannot look further back than the GC window, so "rewind" means the last ~75
  minutes, not arbitrary history. That is exactly why `valid_from` / `valid_until` exist on
  `axiom_memory` as the durable audit axis — MVCC history is a convenience, not the record.
- **Nothing is deployed.** `Dockerfile` and `deploy/terraform/` are written but have never
  been applied. There is no ECS service, no ALB, and no public demo URL. The system runs from
  a shell.
- **The test suite runs against a live cluster, not in CI.** All 49 tests pass — on both the
  local node (24 s) and CockroachDB Cloud (222 s) — and all seven crash windows have one, but
  there is no CI pipeline running them on every commit. "Passes when a human runs it" is
  weaker than "cannot regress".
- **Two defects the suite found, both since fixed — and worth stating because of where
  they lived.** (1) An approval nobody answered never self-healed: nothing in the codebase
  ever set `ApprovalState.EXPIRED`, so the re-park hit `23505` on
  `axiom_approval_one_pending` and the `UniqueViolation` killed the worker. (2) Attempt
  exhaustion stranded a task in `READY` forever — out of the claim index, but never
  transitioned — leaving its receipt on the unsettled worklist and a mission reading
  29/30 complete indefinitely. Both are fixed in `axiom/tasks.py` and both now have
  passing regression tests. Neither was on the refund happy path, which is the lesson: the
  chaos demo never saw the first one because `auto_approve()` answers within 250 ms.
  Whatever branch your demo skips is where your bugs are.
- **The MCP client was written before it could be tested, and it showed.** Three defects
  only appeared on the first live connection: the server rejects a `cluster_id` argument
  when the `mcp-cluster-id` header is set, its rows arrive one envelope deeper than
  expected (a text block containing `{"rows": [...]}`), and the catalog's keyword router
  scored substrings so "effects" outranked "unsettled" and answered a question nobody
  asked. All three are fixed; none was findable against a mock. There is still no
  automated test over the MCP path — it needs a live cluster and a key.
- **The LLM is a small part of this system, deliberately.** Triage proposes an action. It
  never mints a key, never decides whether it may act, and never sees the receipt table. If
  you are looking for prompt engineering, it is not here.
- **The provider is simulated.** It implements Stripe's idempotency semantics faithfully
  (same key + same fingerprint replays, same key + different fingerprint is rejected with
  409) in a separate database over a separate connection, but it is not Stripe. No real
  money moved.
- **Offline embeddings are a deterministic hash sketch**, not Titan. They preserve enough
  structure for recall ranking to be meaningful and for tests to be exact, but recall quality
  under `AXIOM_OFFLINE=1` is not evidence about recall quality under Titan V2.
- **Row-level security is written but commented out** in `db/001_schema.sql`, on purpose. A
  misconfigured `FORCE RLS` returns zero rows *silently*, which is the worst possible failure
  mode to discover during a live demo. The tenant boundary today is `tenant_id NOT NULL`
  everywhere, leading every access-path index, with a mandatory predicate in every query.
- **Single mouse, so to speak: one workload.** The design is argued for e-commerce refunds.
  The claims about hotspot behaviour under a genuinely high-throughput multi-tenant load are
  reasoned from CockroachDB's own documentation, not measured here.

## License

Apache-2.0. See [LICENSE](LICENSE).

Built for the [CockroachDB × AWS Hackathon — Build with Agentic Memory](https://cockroachdb-ai.devpost.com/).
