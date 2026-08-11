# AXIOM — Devpost submission draft

Draft copy for the CockroachDB × AWS Hackathon submission form, plus the video script.

**Read the checklist in §8 before submitting.** Several fields below describe work that is
built but not yet exercised, or written but not yet deployed, and they are marked as such.
Submitting them as finished would overstate the project — a bad trade for an entry whose
whole argument is that systems should tell the truth about what they have done.

*Status as of 2026-08-11: the engine, the test suite (49 passing, including regression tests over two defects it found and that are now fixed
known defects, all seven crash windows covered),
the chaos demo, the HTTP API, the audit agent and Mission Control exist and run. Nothing is
deployed, nothing has run against a distributed cluster, and there is no public demo URL or
video yet.*

---

## 1. Elevator pitch

*(Devpost's tagline field. Under 200 characters.)*

> An agent refunds $300, crashes before recording it, and restarts. AXIOM makes the second
> refund impossible — execution state and semantic memory commit in one CockroachDB
> transaction.

Alternates:

- *Memory is not chat history. Memory is what makes autonomous action safe.*
- *Crash-safe agent memory: vector recall tells the agent what it could do; transactional
  execution state decides what it may do.*

---

## 2. What it does

An agent is told to resolve 30 order exceptions. It issues a $300 refund to customer #18.
Then the process dies — OOM, deploy, spot reclamation — before it records that the refund
succeeded. It restarts. What happens to customer #18?

In most agent frameworks, nobody knows. The framework reconstructs context from a
conversation transcript, sees an unfinished task, and refunds again.

AXIOM is an execution and memory layer that makes that outcome structurally impossible. It
models four classes of agent memory with **different authority**:

| Class | Question it answers | Authority |
| --- | --- | --- |
| Episodic | What happened the last time we saw this? | advises |
| Semantic | What past situations resemble this one? | advises |
| Procedural | What policy applies, and which version? | authorizes |
| Execution | What has this agent already *done*, irreversibly? | **constrains** |

The first three advise. The fourth constrains. **Vector memory tells the agent what it could
do; transactional execution state decides what it may do.**

Concretely, when a worker picks up a task abandoned by a dead peer, one serializable
transaction reads the durable receipt of what the dead worker had already done, runs an ANN
search over episodic memory for what happened the last time an agent died at this exact
execution state, decides re-send / escalate / re-plan, and commits the transition with its
evidence attached. One commit. Then it re-dispatches under the same derived idempotency key,
and the payment provider — a genuinely separate database AXIOM cannot enlist in its
transaction — returns the original refund instead of making a second one.

It also ships the things that make an agent deployable rather than demoable: hard mission
spend caps enforced by a `CHECK` constraint, human-in-the-loop approvals as single-use
capability tokens, memory quarantine that takes effect atomically at commit, provenance and
trust tiers on every memory, multi-tenancy from row one, and an append-only journal where
every state transition is written in the same transaction as the transition itself.

**Measured, on 2026-08-11, against a single-node CockroachDB v26.2.3:**

```
  workers SIGKILLed       45
  worker restarts         56
  tasks terminal          30/30
  refunds created         18
  dollars moved           $2,042.04
  idempotent replays      7
  DUPLICATE REFUNDS       0
```

AXIOM's books and the provider's independent ledger reconcile exactly: 18 receipts against
18 refund rows, 18 distinct idempotency keys on both sides, `spent_cents` 204,204 against
`sum(amount_cents)` 204,204, and zero orders refunded more than once.

**What it does not claim:** exactly-once execution of external side effects. That guarantee
is not available to any system that calls a network API it does not control. AXIOM provides
durable, idempotent, **effectively-once** execution, and every crash window has a defined and
documented outcome.

---

## 3. How we built it

**The database is the design.** `db/001_schema.sql` is 747 lines and most of it is `WHY`
comments, because the load-bearing decisions are schema decisions:

- **The idempotency key is a `GENERATED STORED` column** derived from immutable inputs
  `(tenant_id, task_id, step_name, step_seq)`. The single most lethal bug in this class of
  system is a key minted at call time from a UUID, a timestamp, or the worker id — the
  recovering worker mints a different key and the $300 goes out twice. Making it computed
  removes that possibility from the codebase rather than from the code review.
- **The claim index is PARTIAL and never sees a `DELETE`.** CockroachDB's own hotspot
  guidance names queues as an anti-pattern: they require write-ordered indexing, and deleting
  rows as they are read accumulates ordered garbage behind the live data. AXIOM's answer is
  one index — partial on non-terminal states so finished work *leaves* it, prefixed by an
  application-assigned `shard` so the queue head is N ranges, and `STORING` the columns that
  keep the claim scan index-only.
- **`shard` is an explicit computed column, not `USING HASH`,** so a worker can be pinned to
  a shard subset the way a Kafka consumer group is. `USING HASH` appears exactly once, on the
  genuinely monotonic event timeline.
- **Memory admissibility is a vector index PREFIX column.** `retrieval_class` is computed
  from `quarantined`, `superseded_by` and `trust_level`, so a quarantined memory is in a
  different partition of the ANN index and never enters the candidate set. Post-filtering an
  ANN result silently returns fewer than `LIMIT` rows and misses true nearest neighbours —
  that is a wrong answer, not a slow query.
- **The fencing token, not the lease, is the correctness mechanism.** A lease expiring does
  not stop a GC-paused worker already inside a refund HTTP call. Every write after the claim
  re-checks a per-row monotonic `lease_epoch`.

**The engine** is ~3,000 lines of Python over psycopg3 (plus ~1,700 for the HTTP API and
the audit agent, and ~1,800 of tests). Five protocols — claim, prepare, dispatch, settle,
recover — each one transaction except dispatch, which by necessity has none. `db.tx()` takes
a callable rather than being a context manager, because a `40001` retry has to re-execute the
whole body and a context manager cannot re-run the block it wraps.

**We proved the platform before building on it.** `scripts/preflight.py` is 17 gates that
assert on query *plans*, not output, because a degraded plan returns correct rows and nothing
else would catch it. The gate that mattered most: *is a memory written inside a transaction
returned by an ANN search in that same transaction, with the vector index still in use?* Yes
to both. That is what makes the fused recovery transaction real rather than aspirational.

**Seven crash windows, seven tests.** `tests/test_crash_windows.py` does not assert that
AXIOM works; it assembles the exact conditions under which the design would corrupt state — an
expired lease mid-refund, two executors racing one fence, a recovered agent that
re-synthesized a different request body — and asserts that the system refuses. All 49 pass. Two of them began life as strict `xfail`s pinning real defects the suite
found; both defects are fixed and those tests now guard the fix.

**The demo is also a test.** `scripts/chaos_demo.py` runs a real mission while `SIGKILL`ing a
random live worker every 1.8 seconds — no signal handler, no `finally`, no polite lease
release, which is what an OOM kill and a spot reclamation actually look like. The audit is
run against the provider's separate database. The script fails on zero replays, because a run
where no crash landed in the dangerous window proved nothing.

**Stack:** CockroachDB v26.2.3 (SERIALIZABLE, C-SPANN vector indexes, `AS OF SYSTEM TIME`),
Python 3.14 / psycopg3, Amazon Bedrock (Titan Text Embeddings V2 at 1024 dimensions, Claude
Sonnet for triage).

---

## 4. Challenges we ran into

**An exception rolled back the transaction that recorded the decision.** `prepare()` signalled
"this needs a human" by raising `NeedsApproval`. The exception propagated out of `db.tx()`, so
the connection context manager rolled back — discarding the approval row and the
`AWAITING_APPROVAL` transition the same transaction had just written. The task snapped back to
`READY`, was re-claimed, parked again, and looped forever while the approvals table stayed
empty. The fix was to return a `PrepareResult` instead. *An exception is a fine way to abort a
transaction and a terrible way to return a value from one.*

**The approval was granted and then ignored.** `consume_approval()` existed and nothing called
it. An approved task got re-claimed, re-evaluated against the unchanged policy ceiling, and
parked again — the policy had not moved and never would; the approval was the thing that
changed. The demo answered 1,187 approvals for 3 tasks before this was caught. Both bugs were
in the approval path, the one path a happy-path demo never touches.

**A subquery search vector silently defeats the vector index.** The plan degrades to a full
primary-key scan, which looks perfect on 200 demo rows and collapses at scale. A bound
parameter is fine. The fix was to isolate the variable in preflight, then enforce the rule in
exactly one audited function.

**A wrong ANN result looks exactly like a right one.** Post-filtering on `quarantined = false`
returns fewer rows than `LIMIT` and drops true neighbours, silently. Folding admissibility
into a computed prefix column was the only fix that makes the failure unrepresentable rather
than merely avoided.

**Concurrent agents sharing one local cluster corrupted a measurement.** A run that looked
like an 18-receipt / 9-refund discrepancy turned out to be another process calling the demo's
reset mid-run and truncating the provider ledger. The final numbers were re-measured on an
isolated cluster. Worth recording, because the instinct on seeing that discrepancy was to
doubt the design, and the correct move was to go and find out.

---

## 5. What we learned

- **Commit ordering is a stronger tool than retry logic.** Because the receipt commits before
  a call can go out, "did an effect possibly happen?" becomes a point read on a partial index
  rather than a question about timing. Every crash window gets a decidable answer from one
  structural decision.
- **The safe default is to re-send, not to re-plan.** Re-sending under a derived key costs
  nothing when the provider dedupes, and it is the only way to turn "unknown" into "known".
  Memory is allowed to override that default in one direction only — toward escalation.
  Memory may never talk the system into an act.
- **Give the model less to do and the system gets safer.** Triage returns a proposal and
  cannot mint a key, cannot decide whether it is allowed to act, and never sees the receipt
  table. The seam is enforced by the type signature.
- **A demo that cannot fail proves nothing.** Making `INCONCLUSIVE` a distinct outcome from
  `PASS` was the change that made the chaos run evidence rather than theatre.
- **Assert on plans, not on output.** Every performance-critical property in this system —
  index selection, prefix spans, opclass choice — degrades silently while returning correct
  rows.

---

## 6. What's next

Ordered by value, honestly.

1. **Put the test suite in CI.** All seven windows have a regression test and all 49 tests
   pass, but they pass when a human runs them. Until they run on every commit, "cannot
   regress" is not earned. Cheapest remaining credibility win on the project.
2. **Run everything against a distributed CockroachDB Cloud cluster** and re-quote the
   numbers. Single-node measurements understate real contention, real network latency, and
   real distributed vector-index maintenance.
3. **Deploy to ECS Fargate behind an ALB** and keep a public demo URL alive through the
   judging window, with a synthetic uptime check that alerts. `Dockerfile` and
   `deploy/terraform/` are written and have never been applied.
4. **Exercise the Managed MCP transport for real.** The audit agent works today over a local
   read-only connection; its Cloud MCP path has never made a live connection because the
   service-account key is unavailable here. Untested against the real endpoint is untested.
5. **The counterexample panel.** Run the same mission through a naive transcript-memory agent
   and show it refunding customer #18 twice, side by side with AXIOM's ledger. Judges grade
   against a mental baseline; supplying the baseline makes the difference undeniable.
6. **`AS OF SYSTEM TIME` as a product feature** — "what did the agent believe at 14:32:07, and
   why did it act?", with historical ANN against a past timestamp. Caveat honestly: AOST is
   bounded by `gc.ttlseconds` and yields a read-only transaction, which is exactly why
   `valid_from` / `valid_until` exist as the durable audit axis.
7. **Compensation.** `COMPENSATED` and `compensates_task_id` exist in the schema and nothing
   writes them. An effect that must be *undone* rather than *not repeated* is out of scope
   today.

---

## 7. Required disclosure fields

### CockroachDB tools used

*The form asks which were used. Minimum two of four. **One is fully verified and a second is
built but unexercised against the Cloud endpoint** — see the checklist in §8. Do not submit
this section until the statuses are true on the day.*

| Tool | Status | Use |
| --- | --- | --- |
| **Distributed Vector Indexing** | **In use, verified** | Two C-SPANN indexes on `axiom_memory.embedding`. `axiom_memory_ann_by_context` pins four prefix columns for the recovery path; `axiom_memory_ann_by_tenant` serves broad recall. `vector_cosine_ops` explicit. Index use asserted from `EXPLAIN` output showing a `vector search` node with prefix spans. |
| **Cloud Managed MCP Server** | **Built; Cloud transport unexercised** | `axiom/audit_mcp.py` speaks to the Managed MCP Server over streamable HTTP with a scoped read-only service-account key, discovering tool argument names from `tools/list` rather than guessing. LOCAL mode over a read-only connection **is** verified end to end. Containment is three independent layers: the `axiom_audit` role has `SELECT` and nothing else, a statement guard allows only a single `SELECT`/`WITH`, and the login is `default_transaction_read_only`. **The Cloud path has never connected** — the service-account key is not available in this environment. |
| **ccloud CLI** | **Not yet used** | Planned: cluster provisioning and migration reproducible from the CLI. |
| **Agent Skills Repo** | **Not yet used** | Planned: contribute a crash-safe-queue skill capturing the partial-index / fencing-token / never-`DELETE` pattern. |

### AWS services used

| Service | Status | Use |
| --- | --- | --- |
| **Amazon Bedrock** | **Built; live calls verified in an earlier session, not in the quoted runs** | `amazon.titan-embed-text-v2:0` for 1024-dimension embeddings; `anthropic.claude-sonnet-4-5-20250929-v1:0` for exception triage. Quoted measurements used `AXIOM_OFFLINE=1` deterministic stand-ins so the demo is hermetic. |
| **ECS Fargate** | **Infrastructure written, never applied** | `Dockerfile` + `deploy/terraform/`. Fargate over Lambda because you must be able to SIGKILL a worker on camera. |
| **S3 / ALB** | **Infrastructure written, never applied** | `deploy/terraform/{alb,network,iam,logs}.tf`. Nothing provisioned; no public URL exists. |

### Other required fields

- **Repository:** public, Apache-2.0 (`LICENSE` present). **TODO: not yet pushed to a public
  remote.** The "newly created during the submission period" rule is proven by the commit
  history, and the history currently exists on one machine.
- **Demo URL:** **TODO — does not exist.** Must survive through the judging window.
- **Video:** **TODO — not recorded.** Script in §9. Hard limit 3:00.

---

## 8. Pre-submission checklist

Ordered by how badly it hurts to get it wrong.

```
[ ] CockroachDB tools: at least TWO genuinely used, and §7 updated to match reality
    (vector indexing is solid; the MCP audit agent needs ONE real Cloud connection
     to move from "built" to "used" — that single connection is the cheapest way
     to satisfy the two-of-four minimum honestly)
[ ] Repo pushed public, history intact, LICENSE present
[ ] Demo URL live and reachable from a machine that is not the dev laptop
[ ] Uptime monitor on the demo URL, alerting, through the end of judging
[ ] Video recorded, under 3:00, link tested in a logged-out browser
[ ] Video says "effectively-once, not exactly-once" OUT LOUD
[ ] Numbers in the README re-measured on whatever cluster the video shows
[ ] README limitations section still accurate at submission time
[ ] Every "not yet built" in §7 either built, or still marked as such
[ ] Submit a day early
```

Two open items that are the operator's call, not an engineering decision:

1. **Eligibility.** The rules require entrants to be 18+ or at the age of majority in their
   jurisdiction, with no parent/guardian provision. Resolve it explicitly before submitting.
2. **Cluster credentials.** The Cloud cluster exists and accepted the DDL, but its password
   is not available in this environment. Minting a fresh SQL user via `ccloud` resolves this
   and exercises a required tool at the same time.

---

## 9. Video script — 2:55, hard limit 3:00

Real screen recording. Clean audio. No slide deck of bullet points, no stock music under
narration, no logo intro, no team introductions, no roadmap. The system on screen, doing the
thing.

---

**0:00–0:18 — The question.** *(terminal, mission running)*

> "An agent is resolving thirty order exceptions. It issues a three-hundred dollar refund to
> customer eighteen. Then the process dies — before it records that the refund succeeded.
>
> It restarts. Does customer eighteen get refunded twice?
>
> In most agent frameworks, nobody knows."

---

**0:18–0:38 — The reframe.** *(the four-class table, on screen, no animation)*

> "Agent memory is usually treated as recall. Remember the user's name, remember the last ten
> turns.
>
> The memory that actually matters in production is the memory of what the agent has already
> *done*. AXIOM has four classes. Episodic, semantic and procedural **advise**. Execution
> state **constrains**.
>
> Vector memory tells the agent what it *could* do. Transactional execution state decides what
> it *may* do."

---

**0:38–1:38 — The demo.** *(full screen terminal, split with the provider ledger)*

> "Thirty exceptions. Three workers. I'm killing one every 1.8 seconds — SIGKILL, so no
> cleanup handler runs. That's what an OOM kill looks like."

*(kills scroll past; let it run visibly, do not cut)*

> "Watch this one. Order 1027. The refund reached the provider — the money moved — and the
> worker died before recording it. Worst possible instant."

*(highlight the journal for ORD-1027)*

> "Another worker claims it. In **one transaction** it reads the receipt of what the dead
> worker did, semantically recalls what happened the last time an agent died at this exact
> execution state, and decides: re-send, under the same derived key.
>
> It got killed again. Same decision. Same key."

*(the provider's request log on screen)*

> "The provider saw three requests. It made **one** refund.
>
> Forty-five kills. Thirty of thirty tasks finished. Eighteen refunds requested, eighteen
> refunds created. Zero duplicates. And AXIOM's ledger and the provider's ledger reconcile to
> the cent — two thousand and forty-two dollars, four cents, on both sides."

---

**1:38–2:18 — Why CockroachDB.** *(the schema, then a live query)*

> "This needs one database, because the receipt and the memory commit **together**. Split it
> across a workflow engine and a vector store and there's a window where the agent resumes on
> memory that's already been revoked — with no transaction to close it.
>
> Watch what revocation looks like here."

*(run the quarantine, in one transaction)*

> "That memory is a duplicate-refund incident, top of the recall at 0.83 similarity. I
> quarantine it — and inside the *same transaction*, it's gone from the candidate set.
> `retrieval_class` is a computed column, and it's a **prefix column of the vector index**, so
> the row physically moves. No reindex. No cache. It takes effect at commit.
>
> And the queue is CockroachDB's own documented anti-pattern, solved: the claim index is
> partial, so finished work leaves it, and we never delete a row, so nothing accumulates
> behind the head."

---

**2:18–2:45 — Production posture.** *(crash-window table on screen)*

> "Seven crash windows. Every one has a defined outcome. The receipt commits before anything
> can be sent, so 'did an effect happen' is a point read, not a guess. A zombie worker's write
> is rejected on a stale fencing token — I've forced that one; it writes nothing. Two workers
> preparing the same step: the loser gets a unique-violation from the database, not a second
> refund.
>
> And to be precise about what this is: **effectively-once, not exactly-once.** No system that
> calls an API it doesn't control can promise exactly-once. What it can promise is a derived
> key, a durable receipt, and a defined outcome in every window.
>
> Spend caps enforced by a constraint. Approvals as single-use tokens. Multi-tenant from row
> one."

---

**2:45–2:55 — The line.**

> "Memory is not what the agent remembers.
>
> It's what makes the agent safe to run."

*(cut to the ledger: `DUPLICATE REFUNDS  0`. Hold two seconds. End.)*

---

### Notes for the recording

- **Kill a worker on camera.** It is the entire demo. Do not cut away from it.
- Show the **provider's** ledger, not AXIOM's, for the duplicate check. The whole argument is
  that the external party — the one AXIOM cannot enlist in a transaction — agrees.
- Say "effectively-once, not exactly-once" out loud. A distributed-systems judge trusts the
  project more for the disclaimer and distrusts it instantly without one.
- If the deployed system is live by recording time, record against it, not localhost. A judge
  who suspects localhost discounts the entry.
- Style: no glow, no floating dots, no emoji, no purple-blue gradients. Dense, restrained,
  fast, well-aligned.
- Re-measure the numbers on whatever cluster appears on screen. Do not narrate figures from a
  different run than the one being shown.
