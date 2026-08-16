# AXIOM — Devpost submission

Final copy for the CockroachDB × AWS Hackathon form.

Every status line below is the true one as of **2026-08-15**, reconciled against a live run:
`pytest -q` with `AWS_*` unset entirely → **260 passed, 3 skipped**; `scripts/preflight.py` →
**16/16 blocking gates**; `scripts/uptime_check.sh` → **6/6 against both public URLs**. Where
something is not built, it says so — the entry's whole argument is that systems should tell
the truth about what they have done, and a submission that overstates would be arguing
against itself.

**Contents:** §1 pitch · §2 what it does · §3 how we built it · §4 challenges · §5 what we
learned · §6 what's next · §7 required disclosure · §8 submitter's checklist · §9 the video

---

## 0. Check it without reading anything else

| | |
| --- | --- |
| **Demo — primary** | **https://axiom-one-sage.vercel.app** |
| **Demo — AWS** | **https://nq0i2ob395.execute-api.us-east-2.amazonaws.com** (API Gateway → Lambda) |
| **Stripe's own receipt** | **https://axiom-one-sage.vercel.app/stripe-receipt** — 302 to `pay.stripe.com`, **no Stripe account needed** |
| **Repo** | https://github.com/Dhruvjain35/axiom — public, Apache-2.0 |
| **Video** | 2:38, screen recording of the deployed system (§9) |

Both URLs are the same code and the same CockroachDB cluster; the AWS one is there because
the sponsor's platform should be checkable directly. Open the primary one first — it is the
board judges have been exercising, so it has history on it.

On either, press **RUN THE PROOF**. Seven steps, live, against the same API every panel on
the page reads: seed → crash mid-refund → the mission runs → a human authorizes → wait out
the lease → recover under the same key → **read the provider's own ledger**. It narrates only
what it has observed in an API response; if a beat's evidence does not arrive it says so in
those words and carries the failure through to the verdict.

---

## 1. Elevator pitch

*(Devpost tagline. Under 200 characters.)*

> An agent refunds $300, crashes before recording it, and restarts. AXIOM makes the second
> refund impossible — execution state and semantic memory commit in one CockroachDB
> transaction.

Alternates, if a shorter one is needed:

- *Memory is not chat history. Memory is what makes autonomous action safe.*
- *Vector recall tells the agent what it could do; transactional execution state decides what
  it may do.*

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

Concretely: when a worker picks up a task abandoned by a dead peer, one serializable
transaction re-checks the fencing token, point-reads the durable receipt of what the dead
worker had already done, runs an ANN search over episodic memory for what happened the last
time an agent died at this exact execution state, decides re-send / escalate / re-plan, and
commits the transition with its evidence attached. **One commit.** Then it re-dispatches under
the same derived idempotency key, and the payment provider — a genuinely separate database
AXIOM cannot enlist in its transaction — returns the original refund instead of making a
second one.

It also ships the things that make an agent deployable rather than demoable: hard mission
spend caps enforced by a `CHECK` constraint, human-in-the-loop approvals as single-use
capability tokens, memory quarantine that takes effect atomically at commit, provenance and
trust tiers on every memory, multi-tenancy from row one, and an append-only journal where
every state transition is written in the same transaction as the transition itself.

**Measured on CockroachDB Cloud v26.2.5** (`axiom-memory`, BASIC, AWS `us-east-1`),
`AXIOM_OFFLINE=1`:

```
  workers SIGKILLed       30          tasks terminal      30/30
  worker restarts         42          refunds created     18   ($2,042.04)
  idempotent replays      6           DUPLICATE REFUNDS   0
```

AXIOM's books and the provider's independent ledger reconcile exactly: 18 receipts against 18
refund rows, 18 distinct idempotency keys on both sides, `spent_cents` 204,204 against
`sum(amount_cents)` 204,204, and zero orders refunded more than once.

**The counterparty says it too, and it is Stripe.** `axiom/stripe_provider.py` points the
provider seam at a real Stripe sandbox (`livemode: false`). Charge
`ch_3U4A9yAwRnm0fQgO0yMnQJJz`, refund `re_3U4A9yAwRnm0fQgO0kOsC6Id`, **1 refund on that
charge, 0 duplicates**. The load-bearing part is the second call: the idempotency key AXIOM
had committed to CockroachDB *before* the crash was handed back to Stripe from a plain
terminal outside AXIOM, and Stripe returned `idempotent-replayed: true` naming the earlier
call as `original-request: req_8j6Q6lmQ5Y3ccx` (this call: `req_6YcdVgQok3Aw3R`). Two calls,
one refund, **stated by the party AXIOM cannot enlist in a transaction**. The receipt is
Stripe's own hosted page and needs no Stripe login: `/stripe-receipt` on the deployment
redirects to it. Details and the exact commands are in `docs/STRIPE.md`.

And the comparison ships with it. `scripts/counterexample.py` runs a **fair**
transcript-memory agent — `fsync`'d durable transcript, re-read on restart, checks for prior
completion, records intent before acting — through the identical crash at the identical
instant against the identical provider. It pays **$600** for one order. AXIOM pays **$300**.

**What it does not claim:** exactly-once execution of external side effects. That guarantee is
not available to any system that calls a network API it does not control. AXIOM provides
durable, idempotent, **effectively-once** execution, and every crash window has a defined,
documented and tested outcome.

---

## 3. How we built it

**The database is the design.** `db/001_schema.sql` is 754 lines and most of it is `WHY`
comments, because the load-bearing decisions are schema decisions:

- **The idempotency key is a `GENERATED STORED` column** derived from immutable inputs
  `(tenant_id, task_id, step_name, step_seq)`. The single most lethal bug in this class of
  system is a key minted at call time from a UUID, a timestamp, or the worker id — the
  recovering worker mints a different key and the $300 goes out twice. Making it computed
  removes that possibility from the codebase rather than from the code review.
- **The claim index is PARTIAL and never sees a `DELETE`.** CockroachDB's own hotspot guidance
  names queues as an anti-pattern: they require write-ordered indexing, and deleting rows as
  they are read accumulates ordered garbage behind the live data. AXIOM's answer is one index
  — partial on non-terminal states so finished work *leaves* it, prefixed by an
  application-assigned `shard` so the queue head is N ranges, and `STORING` the columns that
  keep the claim scan index-only. **Measured payoff** (`scripts/scale_bench.py --to 100000`,
  single local node v26.2.3): completed work grew **3,334×** (30 → 100,030) while claim
  latency grew **1.33×** (p50 20.58 → 27.34 ms), because rows in the claim index stayed at
  30. Scoped honestly: that is a statement about the index, not about distributed throughput.
- **`shard` is an explicit computed column, not `USING HASH`,** so a worker can be pinned to a
  shard subset the way a Kafka consumer group is. `USING HASH` appears exactly once, on the
  genuinely monotonic event timeline.
- **Memory admissibility is a vector index PREFIX column.** `retrieval_class` is computed from
  `quarantined`, `superseded_by` and `trust_level`, so a quarantined memory sits in a
  different partition of the ANN index and never enters the candidate set. Post-filtering an
  ANN result silently returns fewer than `LIMIT` rows and misses true nearest neighbours —
  that is a wrong answer, not a slow query.
- **The fencing token, not the lease, is the correctness mechanism.** A lease expiring does not
  stop a GC-paused worker already inside a refund HTTP call. Every write after the claim
  re-checks a per-row monotonic `lease_epoch`.

**The engine.** `axiom/` is 14,064 lines of Python over psycopg3, of which the HTTP API and
the audit agent are 2,637 (`api.py` 1,645, `audit_mcp.py` 992); `tests/` is 6,272. Five
protocols — claim, prepare, dispatch, settle, recover — each one transaction except dispatch,
which by necessity has none. `db.tx()` takes a callable rather than being a context manager,
because a `40001` retry has to re-execute the whole body and a context manager cannot re-run
the block it wraps.

**We proved the platform before building on it.** `scripts/preflight.py` is 17 gates that
assert on query *plans*, not output, because a degraded plan returns correct rows and nothing
else would catch it. 16 are blocking and all 16 pass; the 17th (vector-space sanity) is
advisory. The gate that mattered most: *is a memory written inside a transaction returned by
an ANN search in that same transaction, with the vector index still in use?* Yes to both.
That is what makes the fused recovery transaction real rather than aspirational.

**Seven crash windows, seven tests.** `tests/test_crash_windows.py` does not assert that AXIOM
works; it assembles the exact conditions under which the design would corrupt state — an
expired lease mid-refund, two executors racing one fence, a recovered agent that
re-synthesized a different request body, threads racing one budget — and asserts that the
system refuses. Two began life as strict `xfail`s pinning real defects the suite found; both
are fixed and those tests now guard the fix.

**The suite is hermetic.** 260 pass and 3 skip with `AWS_*` unset entirely — no credentials,
no network, nothing to configure. The 3 skips are live-AWS tests behind an explicit opt-in
flag, because ambient credentials are not consent to spend.

**And it runs on every commit.** `.github/workflows/ci.yml` stands up a real CockroachDB
v25.4.14 per push and pull request, applies `001 → 003 → 002 → 004 → 005` (that order is not
cosmetic: 002 grants on objects 003 creates), runs the whole suite, then runs
`scripts/chaos_demo.py` under **real `SIGKILL`**, then runs `scripts/preflight.py` to assert
the optimizer still chooses the vector index. Half of what these tests assert is
CockroachDB-specific — SERIALIZABLE retries, partial-index predicates, generated columns, a
vector index that must be *chosen* rather than merely exist — so a stand-in database would
prove nothing. A second workflow, `uptime.yml`, checks both public URLs every 30 minutes from
GitHub's infrastructure rather than from inside the deployment, because a monitor that dies
with the thing it monitors reports nothing.

**The demo is also a test.** `scripts/chaos_demo.py` runs a real mission while `SIGKILL`ing a
random live worker every 1.8 seconds — no signal handler, no `finally`, no polite lease
release, which is what an OOM kill and a spot reclamation actually look like. The audit runs
against the provider's separate database. The script fails on zero replays, because a run
where no crash landed in the dangerous window proved nothing.

**Stack:** CockroachDB Cloud v26.2.5 (SERIALIZABLE, C-SPANN vector indexes, `AS OF SYSTEM
TIME`), provisioned and migrated with the `ccloud` CLI; Python and psycopg3 — CI pins 3.12,
Lambda runs python3.13, the dev venv is 3.14; AWS Lambda (two arm64 functions) behind an
Amazon API Gateway HTTP API, and the same code on Vercel; Stripe test mode behind the
provider seam; embeddings from a deterministic 1024-dimension local sketch, with Bedrock
Titan V2 behind the same interface and unusable on this account (§7); vanilla-JS Mission
Control with no build step.

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
changed. The demo answered the same three tasks' approvals over and over before this was
caught. Both bugs lived in the approval path, the one path a happy-path demo never touches.

**A subquery search vector silently defeats the vector index.** The plan degrades to a full
primary-key scan, which looks perfect on 200 demo rows and collapses at scale. A bound
parameter is fine. The fix was to isolate the variable in preflight, then enforce the rule in
exactly one audited function.

**A wrong ANN result looks exactly like a right one.** Post-filtering on `quarantined = false`
returns fewer rows than `LIMIT` and drops true neighbours, silently. Folding admissibility into
a computed prefix column was the only fix that makes the failure unrepresentable rather than
merely avoided.

**Three MCP defects that no mock could have found.** The Managed MCP Server rejects a
`cluster_id` argument when the `mcp-cluster-id` header is set; its rows arrive one envelope
deeper than expected (a text block containing `{"rows": [...]}`), which decoded cleanly and so
raised nothing until every caller died on a `KeyError` one frame from the mistake; and the
catalog's keyword router substring-matched, so "effects" outranked "unsettled" and answered a
question nobody asked. All three appeared on the first live connection.

**Concurrent agents sharing one local cluster corrupted a measurement.** A run that looked like
an 18-receipt / 9-refund discrepancy turned out to be another process calling the demo's reset
mid-run and truncating the provider ledger. The final numbers were re-measured on an isolated
cluster. Worth recording, because the instinct on seeing that discrepancy was to doubt the
design, and the correct move was to go and find out.

**A public Lambda URL that AWS will not grant — and the door that was not locked.** Anonymous
access to the Function URL returns 403, and a controlled experiment (identity policy on → 200,
identity policy off with the resource policy unchanged → 403, reproduced on a throwaway
hello-world function in two regions) showed the refusal is account-level on an account created
hours earlier, not a defect in the code or the policy. The mistake was treating that as a
verdict on public access rather than on one service: API Gateway needs a different grant
(`lambda:InvokeFunction` for a named service principal, not `lambda:InvokeFunctionUrl` for an
anonymous one) and is not subject to the restriction at all. Testing that took one throwaway
HTTP API and one curl. The demo has been public ever since.

**Bedrock was reachable, answered, and still could not be used — and the first explanation we
gave for that was wrong.** The account was written up as having no model access. It has model
access; both models answer. What it does not have is throughput: on-demand inference for Titan
Text Embeddings V2 is 0.0 requests per minute on a quota AWS marks `Adjustable: false`, in
every region checked. The reason this took a while to see is that isolated single calls
succeed, on a burst allowance — so the probe that was supposed to settle the question kept
returning a real 1024-dimension vector. Ten calls in a row is the test that decides it: 0 of
10 in 87.4 s, all `ThrottlingException`. *A capability check that runs once measures the burst
allowance, not the quota.*

**The memory table claimed a Titan embedding it never held.**
`axiom_memory.embedding_model` was declared `NOT NULL DEFAULT 'amazon.titan-embed-text-v2:0'`
and no insert path ever set it, so every row on the demo cluster asserted Titan V2 while
holding blake2b sketches and test fixtures. Nothing computed a wrong answer — both sides of
every cosine comparison were the same embedder — but a column default had been quietly
authoring a claim nobody wrote, in the one project whose entire thesis is that it does not
overclaim. Every row was reclassified **by measurement**, not by assumption: a row is the
offline sketch if `cos(stored, offline_embed(its own content)) > 0.99999`, the test fixture if
it reproduces `sin(r*0.7 + d*0.013)` to `1e-6`, and the relabel was written to refuse to run
if a single row matched neither. None did. Zero rows are unlabelled today. *A `DEFAULT` on a
provenance column is a claim with a schema behind it and nobody's name on it.*

---

## 5. What we learned

- **Commit ordering is a stronger tool than retry logic.** Because the receipt commits before a
  call can go out, "did an effect possibly happen?" becomes a point read on a partial index
  rather than a question about timing. Every crash window gets a decidable answer from one
  structural decision.
- **The safe default is to re-send, not to re-plan.** Re-sending under a derived key costs
  nothing when the provider dedupes, and it is the only way to turn "unknown" into "known".
  Memory is allowed to override that default in one direction only — toward escalation. Memory
  may never talk the system into an act.
- **Give the model less to do and the system gets safer.** Triage returns a proposal and cannot
  mint a key, cannot decide whether it is allowed to act, and never sees the receipt table. The
  seam is enforced by the type signature.
- **A demo that cannot fail proves nothing.** Making `INCONCLUSIVE` a distinct outcome from
  `PASS` was the change that turned the chaos run into evidence rather than theatre.
- **Assert on plans, not on output.** Every performance-critical property in this system — index
  selection, prefix spans, opclass choice — degrades silently while returning correct rows.
- **Build the counterexample.** Judges and users grade against a mental baseline. Supplying a
  *fair* baseline, and letting it win if it can, is worth more than any amount of asserting
  that the naive approach fails.

---

## 6. What's next

Ordered by value, honestly.

1. **Multi-region.** `REGIONAL BY ROW` and a survival goal, then re-measure. Nothing today
   demonstrates surviving the loss of a region, and the claim-index result in §3 is about an
   index, not about distributed throughput.
2. **Real embeddings, when an account can serve them.** Everything runs on the deterministic
   local sketch because Bedrock's on-demand quota here is structurally zero (§7). The seam is
   already the right shape — `axiom/embeddings.py` swaps the provider without touching the
   schema — but the corpus has to be re-embedded and the recall quality re-measured, not
   assumed to carry over.
3. **`AS OF SYSTEM TIME` as a product feature** — "what did the agent believe at 14:32:07, and
   why did it act?", with historical ANN against a past timestamp. Bounded by `gc.ttlseconds`
   and read-only, which is exactly why `valid_from` / `valid_until` exist as the durable audit
   axis.
4. **Compensation.** `COMPENSATED` and `compensates_task_id` exist in the schema and nothing
   writes them. An effect that must be *undone* rather than *not repeated* is out of scope
   today.
5. **Get the Agent Skills PR merged.** It is open upstream (§7) and the remaining work is a
   maintainer's review, not ours.

---

## 7. Required disclosure fields

### CockroachDB tools — 3 in the running system, of 4

The form requires a minimum of two of four. The count below is the number of `in_use: true`
entries in `axiom/measurements.json`, and `in_use` there means **part of the running deployed
system today** — not "reachable", not "written", not "submitted".

| Tool | Status | Use |
| --- | --- | --- |
| **Distributed Vector Indexing** | **In use, verified on Cloud** | Two C-SPANN indexes on `axiom_memory.embedding`. `axiom_memory_ann_by_context` pins four prefix columns for the recovery path; `axiom_memory_ann_by_tenant` serves broad recall. `vector_cosine_ops` explicit — omitting the opclass silently gives L2 and a `<=>` query then full-scans. Index use is asserted from `EXPLAIN` **at request time**, not assumed: `/api/health` reports `vector_index.in_use`, and `tests/test_recall_plan.py` asserts a `vector search` node with prefix spans. |
| **Cloud Managed MCP Server** | **In use, verified against the live server** | `axiom/audit_mcp.py` talks to `https://cockroachlabs.cloud/mcp` over streamable HTTP with a scoped service-account API key and the `mcp-cluster-id` header, discovering tool argument names from `tools/list` rather than guessing. `python -m axiom.audit_mcp --mode mcp "was any order ever refunded twice?"` returns *"Yes — 2 order(s) have more than one refund row: CE-BASELINE-… x4"*, correctly catching the **baseline** agent's double refunds while every AXIOM order has none. Containment is three layers: the `axiom_audit` role has `SELECT` and nothing else, a statement guard allows only a single `SELECT`/`WITH`, and the login is `default_transaction_read_only`. No automated test covers this path — it needs a live cluster and a key. |
| **ccloud CLI** | **In use, verified** | The cluster every measured result ran on (`axiom-memory`, BASIC, AWS `us-east-1`, v26.2.5) is administered entirely through `ccloud`: `auth login`, `cluster list`, `cluster user create axiom_app`, `cluster connection-string`. `scripts/provision_ccloud.sh` wraps provisioning plus all three migrations. |
| **Agent Skills Repo** | **Written, validated, and submitted upstream — PR open, not merged. `in_use: false`.** | `skills/cockroachdb-application-development/implementing-crash-safe-work-queues/` captures the pattern this project proves: partial claim index, explicit shard column over `USING HASH`, fencing token over lease, never `DELETE`, `GENERATED STORED` idempotency key, receipt-before-call. It passes `cockroachlabs/cockroachdb-skills`' own `scripts/validate-spec.py --strict` with zero errors and zero warnings, verified against upstream `main` at `e14e86d`, and is **submitted as [cockroachlabs/cockroachdb-skills#23](https://github.com/cockroachlabs/cockroachdb-skills/pull/23)** (607 insertions, two files, mergeable). It stays `in_use: false` on purpose: an unmerged pull request to somebody else's repository is not part of the running system, however much counting it would flatter the total. |

### AWS services — 6 genuinely in use, of 11 listed

The form requires a minimum of one. Lambda, API Gateway, EventBridge Scheduler, CloudWatch,
SNS and X-Ray are in the running system; the other five are listed as what they are, and each
row says so. Again, the count is the number of `in_use: true` entries in
`axiom/measurements.json`, not a judgement call.

**State the cost honestly on the form: it is cents, not zero — and the bill is dominated by
the act of measuring the bill.** `aws freetier get-account-plan-state` reports
`accountPlanType: PAID` with `$0.00` remaining credits, and `get-free-tier-usage` returns
twelve entries of which **every one is "Always Free" and none is "12 Months Free"** — so every
AWS free tier that is a twelve-month offer has expired on this account. Always free here:
Lambda, CloudWatch, SNS, SQS, KMS, Glue, SES. **Billed here: API Gateway, X-Ray, Comprehend.**
Month to date: **$0.12**, of which **$0.12 is AWS Cost Explorer API calls made while checking
the bill**. The product itself has cost **$0.0002 in 15 days**. Projection through Sep 15:
under $1.00, guarded by an AWS Budget `axiom-zero-spend` at $1.00 alerting at 1% / 50% / 100%,
so the owner is emailed at one cent. Earlier drafts of this document said `$0.00/month`; that
was true of the services in use when it was written and is not true now.

| Service | Status | Use |
| --- | --- | --- |
| **AWS Lambda** | **Deployed, public, and serving traffic** | `axiom-api` (FastAPI behind Mangum, serving both the API and Mission Control from `/var/task/web`) and `axiom-worker`, `us-east-2`, arm64, python3.13, 512 MB, against CockroachDB Cloud in `us-east-1`. **289 worker invocations in the last 24 hours** — this is a running deployment, not a deployed artifact. Cold start `INIT` 1447–2258 ms; warm `/api/health` 169 ms across two cross-region queries; `/api/crash-windows` 2.7 ms; peak 149 MB of 512. 15 tests cover the worker handler. **Genuinely $0**: Lambda's 1M requests + 400,000 GB-s/month is one of this account's twelve *Always Free* entries, and the 11.2 MB ZIP is under the direct-upload limit, so there is no S3, ECR, ALB or NAT. |
| **Amazon API Gateway** | **In use — the public AWS front door. Billed here.** | HTTP API `axiom-api-http` (`nq0i2ob395`, `us-east-2`), payload format 2.0, one `$default` route to `axiom-api`, `$default` stage with auto-deploy, throttled to 20 req/s burst 40 so a crawler cannot run up a bill. It exists because this account blocks anonymous Lambda Function URLs and API Gateway is not subject to that restriction. **Not free here**: the 1M HTTP-API requests/month allowance is a twelve-month offer and this account has no twelve-month tier, so requests bill at $1.00/M from the first one. An idle API still bills nothing — API Gateway is per-request and has no hour hand. Reproducible from `deploy/lambda/apigateway.sh`, which is idempotent and takes `--destroy`. |
| **Amazon EventBridge Scheduler** | **In use — it is what survives four unattended weeks** | Schedule `axiom-worker-sweep`, `rate(5 minutes)`, ENABLED, target `axiom-worker` with `{"mode":"drain","seconds":45,"idle_exit":true}`. Verified firing on the live schedule: invocations observed at 18:18 and 18:23. Judging is Aug 19 – Sep 15 with nobody watching; AXIOM recovers a stalled queue the moment *any* worker runs, and this is the thing that runs one, so a judge on Sep 3 does not open a board frozen since Aug 23. `mode=drain`, deliberately not chaos — a background process that killed itself on a timer would make the error alarm meaningless. |
| **Amazon CloudWatch** | **In use — 5 alarms, 1 dashboard, and it has already paged a human** | `axiom-api-errors`, `axiom-api-throttles`, `axiom-http-5xx`, `axiom-worker-errors`, `axiom-worker-silent`, plus the `axiom-ops` dashboard. Thresholds are loose on purpose: the demo crashes its worker **by design** at crash window W4, so the worker-error alarm needs >30 errors in 15 minutes twice rather than >0, or every judge pressing RUN MISSION pages the owner. Proven end to end rather than assumed — an alarm was driven into ALARM deliberately and the email arrived. Always Free here: 10 alarms, 5 GB logs. |
| **Amazon SNS** | **In use — alarm delivery. Subscription reads `PendingConfirmation`.** | Topic `axiom-ops-alerts`, email to the account owner. Always Free here: 1,000 notifications/month against five alarms designed to emit single digits. **The caveat belongs on the form as much as here**: an alarm email did arrive at a confirmed subscription, and then re-running `observability.sh` replaced that subscription with a pending one — a defect in the script rather than in SNS. Until someone clicks the confirmation link, the alarms fire correctly and the notification goes nowhere. |
| **AWS X-Ray** | **In use — the crash-and-recovery path as a clickable trace. Billed here.** | **Active tracing on `axiom-worker` only; `axiom-api` is deliberately `PassThrough`.** That is a cost decision, not an oversight: an Active API behind a browser tab that polls records a trace per poll, ~$1.73/month **per open tab** — over the $1.00 budget on one tab — while the worker's ~8,640 scheduled invocations a month cost about four cents. Verified by reading a real trace rather than by trusting the instrumentation: **`1-6a7fb524-4be35a4b20bb6f981abc4b6a`** carries `axiom.PREPARE`, `axiom.dispatch` and `axiom.SETTLE` subsegments under an `axiom.drain` span — **the three boundaries this submission is about** — annotated with `task_id`, `idempotency_key`, `crash_window`, `provider_status`, `prepare_outcome`, `attempt_state`. Annotations are filterable, so a judge can query `crash_window = W4` and land on the exact instant the project is about instead of reading a paragraph claiming it happens. The same trace shows three refunds over the $200 ceiling with `prepare_outcome = parked_on_approval` — the policy engine visible as telemetry. 5 traces in the last hour; sampling bounded at 5% plus a 1/sec reservoir. **Not free here**: $5.00/M traces recorded, projected $0.043/month. |
| **Amazon Bedrock** | **Reachable and verified from this account; NOT USABLE — the quota is structurally zero. `in_use: false`.** | Model access **is** enabled and both models answer: `amazon.titan-embed-text-v2:0` returns the 1024-dimension embedding the schema's `VECTOR(1024)` pins (`axiom/embeddings.py`), and `anthropic.claude-sonnet-4-5` replies for exception triage (`axiom/llm.py`). Neither can be used. On-demand inference for Titan V2 is **0.0 requests/minute and 0.0 tokens/minute** — quota `L-26C560CE`, **`Adjustable: false`**, so it cannot be raised by request — and it reads 0.0 in `us-east-1`, `us-east-2` and `us-west-2` alike (`aws service-quotas list-service-quotas --service-code bedrock`). A sustained probe got **0 of 10 calls through in 87.4 s, every one a `ThrottlingException`**. Isolated single calls do sometimes succeed on a burst allowance, which is precisely why a one-off probe looks like proof and is not. **Batch inference is available and was deliberately not used**: it allows 100,000 records per job but requires a minimum of 100, and the real memory corpus is 10 distinct seed texts — padding it to clear that minimum would buy the checkbox by embedding meaningless strings. So both functions run `AXIOM_OFFLINE=1`, every quoted measurement used the deterministic stand-in, and every `axiom_memory` row now names the space it is actually in. |
| **Amazon Comprehend** | **Wired behind an authority boundary, OFF by default. `in_use: false`. Billed here.** | `axiom/comprehend.py` runs DetectKeyPhrases + DetectEntities + DetectSentiment over an exception description and may only **narrow** what the rule table proposed: toward escalate, never toward acting; it cannot move `amount_cents` at all; it cannot raise its own confidence. `assert_cannot_widen()` enforces it. It found a real ordering bug in the rule table (`late_delivery` above `fraud_suspected`, so a fraud text triaged as an unattended refund) — A/B against the real engine: `AXIOM_COMPREHEND=0` settles and moves the money, `=1` escalates and mints zero receipts. `in_use` is false because `AXIOM_COMPREHEND` is unset on both Vercel and Lambda, so it bills nothing while judges use the demo. **Not free here**: 189 units × $0.0001 = **$0.019** spent on measurement. |
| **Amazon SES** | **Sender verified, one real send confirmed — not yet dispatching. `in_use: false`.** | Sender identity verified and a send confirmed to Amazon's mailbox simulator (`MessageId 010f01a0028ad822-005d6883-ca07-4c68-8189-afd522fbc98d-000000`), which requires no recipient verification and touches no real inbox. Sandbox: 200 messages/day. SES **is** one of this account's Always Free entries. `in_use` stays false because the second worked example still uses the simulated relay — reachable is not deployed, and this submission does not blur those two words to lift a count. |
| **CloudFront** | **Distribution exists, serves no traffic, superseded by API Gateway. `in_use: false`.** | Created attempting a public front door via Origin Access Control, which did not solve the 403 because OAC is also a Function URL resource-policy grant. It serves zero requests and zero bytes, so it bills nothing, and was left in place. |
| **ECS Fargate / ALB / S3** | **Infrastructure written, never applied. `in_use: false`.** | `Dockerfile`, `deploy/terraform/{ecs,alb,network,iam,logs}.tf`, `deploy/ecs/`. No cluster, service or task definition has been created — every one of those bills per hour rather than per request. |

### The public URLs — state both plainly on the form

- **Primary — https://axiom-one-sage.vercel.app**
- **AWS — https://nq0i2ob395.execute-api.us-east-2.amazonaws.com**

Same code, same CockroachDB cluster, two platforms. Both anonymous and unsigned; both pass
`scripts/uptime_check.sh` **6/6** — health, database reachable, provider reachable, demo
mission present, **duplicate effects 0**, and vector index *used, not a scan*. Lead a judge to
the primary one: it is the board that has been exercised and therefore has history on it.

```console
$ curl -s https://axiom-one-sage.vercel.app/api/health \
    | jq '{status, provider: .checks.provider, vector_index: .checks.vector_index}'
{
  "status": "ok",
  "provider": {
    "ok": true,
    "latency_ms": 21.3,
    "refunds_global": 15,
    "replays_global": 0,
    "duplicate_orders_global": 0
  },
  "vector_index": {
    "ok": true,
    "in_use": true,
    "age_seconds": 0.0
  }
}
```

The counters move as judges use the demo; `duplicate_orders_global: 0` is the one that must
not. `vector_index.in_use` is read from a live `EXPLAIN`, not from a constant. The full body
also carries `checks.db`, `checks.demo` (mission, `by_state`, `spent_cents`), `checks.workers`,
`booted_at` and `uptime_seconds`.

One line is worth putting on the form so a judge who finds it does not conclude the deployment
is broken: **this AWS account refuses anonymous access to Lambda Function URLs at the account
level** — a controlled experiment (identity policy on → 200, identity policy off with the
resource policy unchanged → 403; propagation, policy syntax and SCPs each ruled out) showed it
is not a defect in the code or the policy. API Gateway needs a different grant and is not
subject to it. The full forensics are in `README.md`.

### Other required fields

- **Repository:** https://github.com/Dhruvjain35/axiom — public, Apache-2.0 (`LICENSE`
  present). The "newly created during the submission period" rule is evidenced by the commit
  history.
- **Demo URL:** **https://axiom-one-sage.vercel.app** (primary) and
  **https://nq0i2ob395.execute-api.us-east-2.amazonaws.com** (AWS). Live, anonymous, $0.0002 of
  product spend in 15 days and projected under $1.00 through Sep 15. A 5-minute EventBridge
  sweep keeps the queue draining so neither goes stale unattended, and `uptime.yml` checks both
  every 30 minutes from outside. Re-create the AWS one with `./deploy/lambda/apigateway.sh` if
  it is ever torn down; the URL changes if the API is recreated, so tear it down only
  deliberately.
- **Third-party attestation:** **https://axiom-one-sage.vercel.app/stripe-receipt** — 302 to
  Stripe's own hosted receipt page. No Stripe account needed.
- **Video:** 2:38 (§9).

---

## 7b. Answering the five judging criteria directly

Each row is the shortest true answer, plus the artefact that settles it. Nothing here is a
claim a reader has to accept — every line names a command, a file or a URL.

### Agentic Memory Design
*"more than toy queries — state, embeddings, context, or transactional data at real scale"*

Memory here does not advise the agent; part of it **binds** the agent. Four classes with
different authority — episodic and semantic **advise**, procedural **authorizes**, execution
**constrains** — and the last one is the reason a second refund cannot happen. Execution
state and semantic recall are read and written in **one serializable transaction**: the same
commit that decides re-send/escalate also writes the memory of deciding. Embeddings are
`VECTOR(1024)` under two C-SPANN indexes, and index use is asserted from `EXPLAIN` at request
time rather than assumed.

**Check it:** press **RUN THE EXPERIMENT** on tab 3. Same stopped refund recovered three
times — same receipt, same fence, same policy, same amount. The only variable is the memory
table. `RESEND → ESCALATE → RESEND` — three serializable transactions inside one request, typically under a second (the endpoint reports its own `elapsed_ms`; consecutive live runs returned 883 and 888). If the
verdict were identical all three times, the memory in this system would be decoration.

### Technical Implementation
*"distributed vector index, MCP Server, ccloud CLI — correctly and safely"*

All three, and the safety is the interesting part. The **MCP server** (`axiom/audit_mcp.py`)
lets a model ask the cluster questions in English, so it is contained three ways: a
**SELECT-only role**, a **single-statement guard**, and `default_transaction_read_only`. The
**vector indexes** are built `vector_cosine_ops` explicitly — omitting the opclass silently
gives L2, and a `<=>` query then full-scans, which is a wrong answer that looks like a right
one. **ccloud** provisions the cluster and applies all five migrations
(`scripts/provision_ccloud.sh`).

**Check it:** 260 tests, hermetic — they pass with `AWS_*` unset entirely. CI stands up a
real CockroachDB per push, runs them, then runs the chaos demo under real SIGKILLs, then
asserts the vector index is still the chosen plan.

### Real-World Impact
*"meaningful, not just technically impressive"*

The failure is ordinary and expensive: an agent refunds a customer, the process dies before
recording it, and the customer is paid twice. `scripts/counterexample.py` runs a **fair**
transcript-memory agent — durable `fsync`'d transcript, re-read on restart, checks for prior
completion — through the identical crash at the identical instant against the identical
provider. It pays **$600** for one order. AXIOM pays **$300**.

**Check it without an account:**
[`/stripe-receipt`](https://axiom-one-sage.vercel.app/stripe-receipt) → Stripe's own hosted
page for a real sandbox refund. And the part that matters: the idempotency key AXIOM
committed *before* the crash, handed back to Stripe from a plain terminal outside AXIOM,
returned `idempotent-replayed: true` naming `original-request: req_8j6Q6lmQ5Y3ccx` against
`request-id: req_6YcdVgQok3Aw3R`. **Two calls, one refund — stated by the counterparty, not
by us.**

### Production Readiness
*"secure, observable, scalable — and what happens when things go wrong"*

What happens when things go wrong **is the product**. Seven crash windows, W1–W7, each with a
documented and tested outcome (`docs/CRASH_WINDOWS.md`); the dangerous one is W4, where the
money has moved and the system does not yet know.

- **Observable** — X-Ray traces the recovery path; trace
  `1-6a7fb524-4be35a4b20bb6f981abc4b6a` carries `axiom.PREPARE` / `axiom.dispatch` /
  `axiom.SETTLE` annotated with `crash_window=W4`, so the instant this project is about is
  *filterable* in the console. Five CloudWatch alarms over SNS, which have already paged a
  human. An uptime check runs every 30 minutes against **both** URLs and asserts the demo is
  *usable* — not merely up: mission present, duplicate effects still zero, vector index still
  chosen.
- **Secure** — SELECT-only audit role, single-use approval tokens, tenant id on every access
  path, and a `CHECK` constraint enforcing the mission spend cap in the database rather than
  in application code.
- **Scalable** — claim latency grew **1.33×** while completed work grew **3,334×**, because
  finished tasks leave the partial claim index. Scoped honestly: that is a statement about
  the index, not about distributed throughput.

### Creativity & Originality
*"insight into what makes agentic systems different from traditional apps"*

Two ideas the entry is built on. First, **memory has authority tiers** — the mistake is
treating recall and permission as the same substance, and a transcript is the worst possible
place to keep the record of an irreversible act. Second, **money is often the wrong risk
axis**: forty thousand marketing emails cost about four dollars and clear a $200 unattended
ceiling, while a $300 refund to one person stops for a human. The cheap act is the dangerous
one, so authority is denominated in the act's own unit — `comms.recipients`, not cents.

**Check it:** tab 6. Same engine, no money involved, ceiling of 2,000 recipients, and the
same guarantee holds — nobody is messaged twice.

---

## 8. What the submitter still has to do

Everything below is an action, not a status. Ordered by how badly it hurts to get it wrong.

```
[ ] CLICK THE SNS CONFIRMATION EMAIL. The subscription reads PendingConfirmation, so five
    alarms currently fire into a void and the demo can break silently across a four-week
    judging window. This is the one open item that can actually cost the entry.
[ ] Upload ~/axiom-video/out/axiom-demo.mp4 (2:38) to YouTube or Vimeo, PUBLIC or
    unlisted — not "private" — and open the link in a logged-out browser before pasting it.
[ ] Paste BOTH public URLs on the form, primary first:
      https://axiom-one-sage.vercel.app
      https://nq0i2ob395.execute-api.us-east-2.amazonaws.com
[ ] Paste the repo URL and confirm it is public from a logged-out browser.
[ ] Tick the CockroachDB tools: Distributed Vector Indexing, Cloud Managed MCP Server,
    ccloud CLI. Do NOT tick Agent Skills — the PR is open, not merged (§7).
[ ] Tick the AWS services: Lambda, API Gateway, EventBridge Scheduler, CloudWatch, SNS,
    X-Ray. Do NOT tick Bedrock, Comprehend, SES, CloudFront, ECS/ALB/S3.
[ ] State the cost as cents, not $0.00, if the form asks.
[ ] Reconcile README.md with this document — it still says 178 tests (lines 36, 201, 454,
    689) and "No CI." (line 742). Both are false as of 2026-08-15.
[ ] Re-run ./scripts/uptime_check.sh against both URLs on the morning of submission.
[ ] SUBMIT. Deadline Aug 18 2026, 5pm ET. Submit a day early.
```

---

## 9. The video

**`~/axiom-video/out/axiom-demo.mp4` — 2:38, 1920×1080.**

It is a screen recording of the deployed product, not a recreation and not a slide deck. Its
spine is the product's own seven-step guided proof, running live in the browser, so what a
judge watches is the same thing they get by pressing RUN THE PROOF themselves.

Beats: the problem · what AXIOM is · **the proof, all seven steps** · memory recall · memory
decides · the real Stripe refund · a second, non-money workload · close.

Each narration sentence was cut to the frame its step actually appears on, and all seven were
verified by extracting frames from the finished encode rather than from the timeline — the
same discipline as the rest of the entry: check the artifact you are shipping, not the thing
you believe you rendered.

It says **"effectively-once, not exactly-once"** out loud, because that is the honest
guarantee and a distributed-systems judge should hear the project make its own disclaimer
before they have to ask for it.
