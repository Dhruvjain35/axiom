# AXIOM — Devpost submission

Final copy for the CockroachDB × AWS Hackathon form, plus the video shot list.

Every status line below is the true one as of **2026-08-11**, reconciled against a live test
run (`pytest -q` → 178 passed) and the deployment record in `deploy/lambda/README.md`. Where
something is not built, it says so — the entry's whole argument is that systems should tell
the truth about what they have done, and a submission that overstates would be arguing
against itself.

**Contents:** §1 pitch · §2 what it does · §3 how we built it · §4 challenges · §5 what we
learned · §6 what's next · §7 required disclosure · §8 checklist · **§9 video shot list**

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

**Measured on 2026-08-11 against CockroachDB Cloud v26.2.5** (`axiom-memory`, BASIC, AWS
`us-east-1`), `AXIOM_OFFLINE=1`:

```
  workers SIGKILLed       30          tasks terminal      30/30
  worker restarts         42          refunds created     18   ($2,042.04)
  idempotent replays      6           DUPLICATE REFUNDS   0
```

On the same cluster: `preflight.py` **16/16 blocking gates**; `pytest` **178 passed**.

AXIOM's books and the provider's independent ledger reconcile exactly: 18 receipts against 18
refund rows, 18 distinct idempotency keys on both sides, `spent_cents` 204,204 against
`sum(amount_cents)` 204,204, and zero orders refunded more than once.

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

**The database is the design.** `db/001_schema.sql` is 748 lines and most of it is `WHY`
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
  keep the claim scan index-only.
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

**The engine** is ~3,000 lines of Python over psycopg3 (plus ~1,700 for the HTTP API and the
audit agent, and ~1,800 of tests). Five protocols — claim, prepare, dispatch, settle, recover
— each one transaction except dispatch, which by necessity has none. `db.tx()` takes a
callable rather than being a context manager, because a `40001` retry has to re-execute the
whole body and a context manager cannot re-run the block it wraps.

**We proved the platform before building on it.** `scripts/preflight.py` is 17 gates that
assert on query *plans*, not output, because a degraded plan returns correct rows and nothing
else would catch it. The gate that mattered most: *is a memory written inside a transaction
returned by an ANN search in that same transaction, with the vector index still in use?* Yes
to both. That is what makes the fused recovery transaction real rather than aspirational.

**Seven crash windows, seven tests.** `tests/test_crash_windows.py` does not assert that AXIOM
works; it assembles the exact conditions under which the design would corrupt state — an
expired lease mid-refund, two executors racing one fence, a recovered agent that
re-synthesized a different request body, threads racing one budget — and asserts that the
system refuses. All 178 tests pass. Two began life as strict `xfail`s pinning real defects the
suite found; both are fixed and those tests now guard the fix.

**The demo is also a test.** `scripts/chaos_demo.py` runs a real mission while `SIGKILL`ing a
random live worker every 1.8 seconds — no signal handler, no `finally`, no polite lease
release, which is what an OOM kill and a spot reclamation actually look like. The audit runs
against the provider's separate database. The script fails on zero replays, because a run
where no crash landed in the dangerous window proved nothing.

**Stack:** CockroachDB Cloud v26.2.5 (SERIALIZABLE, C-SPANN vector indexes, `AS OF SYSTEM
TIME`), provisioned and migrated with the `ccloud` CLI; Python 3.14 / psycopg3; AWS Lambda
(two arm64 functions, $0, deployed and working) behind an Amazon API Gateway HTTP API (the
public URL, $0); embeddings from a deterministic 1024-dimension local sketch, with Bedrock
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
changed. The demo answered 1,187 approvals for 3 tasks before this was caught. Both bugs lived
in the approval path, the one path a happy-path demo never touches.

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
HTTP API and one curl. The demo has been public ever since — see §7.

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
if a single row matched neither. None did. *A `DEFAULT` on a provenance column is a claim with
a schema behind it and nobody's name on it.*

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

1. **Put the test suite in CI.** All seven windows have a regression test and all 178 tests
   pass, but they pass when a human runs them. Until they run on every commit, "cannot
   regress" is not earned. Cheapest remaining credibility win on the project.
2. **A public URL that survives the judging window,** on the `deploy/free-tier/` EC2 path,
   which uses no Lambda resource policy and is therefore unaffected by the 403 — plus an
   uptime check, and a token gate on `POST /api/demo/reset` before anything is public.
3. **Point `axiom/provider.py` at Stripe test mode behind a flag** and run the counterexample
   against it once. "The external ledger here is Stripe" removes the last "it's all
   simulated" objection in one change.
4. **Open the Agent Skills PR.** The skill is written and passes upstream validation; the
   proposal issue and the conversation with maintainers are the remaining step.
5. **Multi-region.** `REGIONAL BY ROW` and a survival goal, then re-measure. Nothing today
   demonstrates surviving the loss of a region.
6. **`AS OF SYSTEM TIME` as a product feature** — "what did the agent believe at 14:32:07, and
   why did it act?", with historical ANN against a past timestamp. Bounded by `gc.ttlseconds`
   and read-only, which is exactly why `valid_from` / `valid_until` exist as the durable audit
   axis.
7. **Compensation.** `COMPENSATED` and `compensates_task_id` exist in the schema and nothing
   writes them. An effect that must be *undone* rather than *not repeated* is out of scope
   today.

---

## 7. Required disclosure fields

### CockroachDB tools used — 3 in the running system, 4th written

The form requires a minimum of two of four.

| Tool | Status | Use |
| --- | --- | --- |
| **Distributed Vector Indexing** | **In use, verified on Cloud** | Two C-SPANN indexes on `axiom_memory.embedding`. `axiom_memory_ann_by_context` pins four prefix columns for the recovery path; `axiom_memory_ann_by_tenant` serves broad recall. `vector_cosine_ops` explicit — omitting the opclass silently gives L2 and a `<=>` query then full-scans. Index use asserted from `EXPLAIN` showing a `vector search` node with prefix spans, in `tests/test_recall_plan.py`. |
| **Cloud Managed MCP Server** | **In use, verified against the live server** | `axiom/audit_mcp.py` talks to `https://cockroachlabs.cloud/mcp` over streamable HTTP with a scoped service-account API key and the `mcp-cluster-id` header, discovering tool argument names from `tools/list` rather than guessing. `python -m axiom.audit_mcp --mode mcp "was any order ever refunded twice?"` returns *"Yes — 2 order(s) have more than one refund row: CE-BASELINE-… x4"*, correctly catching the **baseline** agent's double refunds while every AXIOM order has none. Containment is three layers: the `axiom_audit` role has `SELECT` and nothing else, a statement guard allows only a single `SELECT`/`WITH`, and the login is `default_transaction_read_only`. No automated test covers this path — it needs a live cluster and a key. |
| **ccloud CLI** | **In use, verified** | The cluster every measured result ran on (`axiom-memory`, BASIC, AWS `us-east-1`, v26.2.5) is administered entirely through `ccloud`: `auth login`, `cluster list`, `cluster user create axiom_app`, `cluster connection-string`. `scripts/provision_ccloud.sh` wraps provisioning plus all three migrations. |
| **Agent Skills Repo** | **Skill written and validated; no PR opened** | `skills/cockroachdb-application-development/implementing-crash-safe-work-queues/` — 390 lines capturing the pattern this project proves: partial claim index, explicit shard column over `USING HASH`, fencing token over lease, never `DELETE`, `GENERATED STORED` idempotency key, receipt-before-call. Laid out to match `cockroachlabs/cockroachdb-skills` exactly and passes their own `scripts/validate-spec.py --strict` with zero errors and zero warnings. Their `CONTRIBUTING.md` asks contributors to propose in an issue and agree scope with maintainers first, so the PR is not open. |

### AWS services used — 6 genuinely in use, of 11 listed

The form requires a minimum of one. Lambda, API Gateway, EventBridge Scheduler, CloudWatch,
SNS and X-Ray are in the running system; the other five are listed as what they are, and each
row says so. The count is the number of `in_use: true` entries in `axiom/measurements.json`,
not a judgement call.

**State the cost honestly on the form: it is cents, not zero.** `aws freetier
get-account-plan-state` reports `accountPlanType: PAID` with `$0.00` remaining credits, and
`get-free-tier-usage` returns twelve entries of which **every one is "Always Free" and none is
"12 Months Free"** — so every AWS free tier that is a twelve-month offer has expired on this
account. Always free here: Lambda, CloudWatch, SNS, SQS, KMS, Glue, SES. **Billed here: API
Gateway, X-Ray, Comprehend.** Month-to-date across all services: **$0.0001021066** (measured
2026-08-14). Projection through Sep 15: **under $1.00**, guarded by an AWS Budget
`axiom-zero-spend` at $1.00 alerting at 1% / 50% / 100%, so the owner is emailed at one cent.
Earlier drafts of this document said `$0.00/month`; that was true of the services in use when
it was written and is not true now.

| Service | Status | Use |
| --- | --- | --- |
| **AWS Lambda** | **Deployed, working, and public** | `axiom-api` (FastAPI behind Mangum, serving both the API and Mission Control from `/var/task/web`) and `axiom-worker`, `us-east-2`, arm64, python3.13, 512 MB, against CockroachDB Cloud in `us-east-1`. Cold start `INIT` 1447–2258 ms; warm `/api/health` 169 ms across two cross-region queries; `/api/crash-windows` 2.7 ms; peak 149 MB of 512. Freeze/thaw tested at 17 s / 30 s / 73 s / 220 s / 14 min — no 500 in any state. 15 tests cover the worker handler. **Genuinely $0**: Lambda's 1M requests + 400,000 GB-s/month is one of this account's twelve *Always Free* entries, and the 11.2 MB ZIP is under the direct-upload limit, so there is no S3, ECR, ALB or NAT. |
| **Amazon API Gateway** | **In use — this is the public demo URL. Billed here.** | HTTP API `axiom-api-http` (`nq0i2ob395`, `us-east-2`), payload format 2.0, one `$default` route to `axiom-api`, `$default` stage with auto-deploy, throttled to 20 req/s burst 40 so a crawler cannot run up a bill. It exists because this account blocks anonymous Lambda Function URLs and API Gateway is not subject to that restriction. **Not free here**: the 1M HTTP-API requests/month allowance is a twelve-month offer and this account has no twelve-month tier, so requests bill at $1.00/M from the first one. An idle API still bills nothing — API Gateway is per-request and has no hour hand. Reproducible from `deploy/lambda/apigateway.sh`, which is idempotent and takes `--destroy`. |
| **Amazon EventBridge Scheduler** | **In use — it is what survives four unattended weeks** | Schedule `axiom-worker-sweep`, `rate(5 minutes)`, ENABLED, target `axiom-worker` with `{"mode":"drain","seconds":45,"idle_exit":true}`. Verified firing three times in six minutes. Judging is Aug 19 – Sep 15 with nobody watching; AXIOM recovers a stalled queue the moment *any* worker runs, and this is the thing that runs one, so a judge on Sep 3 does not open a board frozen since Aug 23. `mode=drain`, deliberately not chaos — a background process that killed itself on a timer would make the error alarm meaningless. |
| **Amazon CloudWatch** | **In use — 5 alarms, 1 dashboard, and it has already paged a human** | `axiom-api-errors`, `axiom-api-throttles`, `axiom-http-5xx`, `axiom-worker-errors`, `axiom-worker-silent`, plus the `axiom-ops` dashboard. Thresholds are loose on purpose: the demo crashes its worker **by design** at crash window W4, so the worker-error alarm needs >30 errors in 15 minutes twice rather than >0, or every judge pressing RUN MISSION pages the owner. Proven end to end rather than assumed — an alarm was driven into ALARM deliberately and the email arrived. Always Free here: 10 alarms, 5 GB logs. |
| **Amazon SNS** | **In use — alarm delivery. Subscription PENDING until a human clicks confirm.** | Topic `axiom-ops-alerts`, email to the account owner. Always Free here: 1,000 notifications/month against five alarms designed to emit single digits. **The caveat belongs on the form as much as here**: the subscription reads `PendingConfirmation` until the confirmation link is clicked, so the alerting path is **not yet proven end to end for the current subscription** — the alarms will fire correctly and the notification will go nowhere. Re-running `observability.sh` replaces a confirmed subscription with a pending one, a defect in the script rather than in SNS. |
| **AWS X-Ray** | **In use — the crash-and-recovery path as a clickable trace. Billed here.** | Active tracing; 5 traces recorded in a 30-minute verification window, worker logs carrying `traced=True` with X-Ray trace ids. Subsegments wrap **PREPARE, the provider dispatch, SETTLE and the recovery recall** — the four boundaries this entire submission is about — annotated with task id, crash window and whether the provider reported an idempotent replay, so a judge can *filter* the console for a replayed recovery instead of scrolling for one. Sampling bounded at 5% plus a 1/sec reservoir. **Not free here**: the 100,000 traces/month allowance is a twelve-month offer that has expired on this account, so traces bill at $5.00/M from the first one. A few thousand traces across judging is cents. |
| **Amazon Bedrock** | **Reachable and verified from this account; NOT USABLE — the quota is structurally zero** | Model access **is** enabled on the deployment account and both models answer: `amazon.titan-embed-text-v2:0` returns the 1024-dimension embedding the schema's `VECTOR(1024)` pins (`axiom/embeddings.py`), and `anthropic.claude-sonnet-4-5` replies for exception triage (`axiom/llm.py`). Neither can be used. On-demand inference for Titan V2 is **0.0 requests/minute and 0.0 tokens/minute** — quota `L-26C560CE`, **`Adjustable: false`**, so it cannot be raised by request — and it reads 0.0 in `us-east-1`, `us-east-2` and `us-west-2` alike (`aws service-quotas list-service-quotas --service-code bedrock`). A sustained probe got **0 of 10 calls through in 87.4 s, every one a `ThrottlingException`**. Isolated single calls do sometimes succeed on a burst allowance, which is precisely why a one-off probe looks like proof and is not. **Batch inference is available and was deliberately not used**: it takes 100,000 records per job but requires a minimum of 100, and the real memory corpus is 10 distinct seed texts — padding it to clear the minimum would buy the checkbox by embedding meaningless strings. So both functions run `AXIOM_OFFLINE=1`, every quoted measurement used the deterministic stand-in, and every `axiom_memory` row now names the space it is actually in. |
| **CloudFront** | **Distribution exists, serves no traffic, superseded by API Gateway** | Created attempting a public front door via Origin Access Control, which did not solve the 403 because OAC is also a Function URL resource-policy grant. It is not among this account's twelve Always Free entries, but it serves zero requests and zero bytes, so it bills nothing and was left in place. |
| **ECS Fargate / ALB / S3** | **Infrastructure written, never applied** | `Dockerfile`, `deploy/terraform/{ecs,alb,network,iam,logs}.tf`, `deploy/ecs/`. No cluster, service or task definition has been created — every one of those bills per hour rather than per request. |
| **Amazon SES** | **Sender verified, one real send confirmed — not yet dispatching** | Sender identity verified and a send confirmed to Amazon's mailbox simulator (`MessageId 010f01a0028ad822-…`), which requires no recipient verification and touches no real inbox. Sandbox: 200 messages/day. SES **is** one of this account's Always Free entries. `in_use` stays false because the second worked example still uses the simulated relay — reachable is not deployed, and this submission does not blur those two words to lift a count. |
| **Amazon Comprehend** | **Wired behind an authority boundary, OFF by default, billed here** | `axiom/comprehend.py` runs DetectKeyPhrases + DetectEntities + DetectSentiment over an exception description and may only **narrow** what the rule table proposed: toward escalate, never toward acting; it cannot move `amount_cents` at all; it cannot raise its own confidence. `assert_cannot_widen()` enforces it. It found a real ordering bug in the rule table (`late_delivery` above `fraud_suspected`, so a fraud text triaged as an unattended refund). `AXIOM_COMPREHEND` is unset on Vercel and Lambda, so it bills nothing while judges use the demo. **Not free here**: 50,000 units/month is a twelve-month offer that has expired on this account. $0.0001/unit; ~$0.09 spent on measurement. |

### The public URL — state this plainly on the form

**https://nq0i2ob395.execute-api.us-east-2.amazonaws.com/**

```console
$ curl https://nq0i2ob395.execute-api.us-east-2.amazonaws.com/api/health
{"ok":true,"db":true,"provider":true,"version":"0.1.0","offline":true,"errors":{}}
```

Anonymous, unsigned, and it costs cents a month rather than the `$0.00` this line used to
claim. It is served through API Gateway rather than a Lambda Function URL, and that detail is
worth one line on the form because a judge who finds the 403 on the Function URL should not
conclude the deployment is broken.

**This AWS account refuses anonymous access to Lambda
Function URLs, and the refusal is account-level, not a defect in the policy.** The controlled
experiment: one function, one unchanged resource-policy statement granting
`lambda:InvokeFunctionUrl` — a role *with* a matching identity policy gets **200**; the same
role with the identity policy removed and the resource policy untouched gets **403**. So
resource-based grants on a Function URL are not honored on this account, and both free public
paths are exactly that kind of grant. Ruled out: propagation (a public statement stood for 15
minutes), policy syntax (`aws lambda add-permission` writes the statement AWS itself dictates,
and `iam simulate-principal-policy` returns `allowed`), and SCPs (the account is in no
organization). The account was created hours before the deployment and is pending activation.

**API Gateway needs a different grant and is not subject to any of that**: `lambda:Invoke`
`Function` for the named service principal `apigateway.amazonaws.com`, evaluated by the
Lambda control plane, rather than `lambda:InvokeFunctionUrl` for an anonymous principal,
evaluated by the Function URL front end. Both doors are live on the same function, which
makes the restriction demonstrable rather than merely asserted:

| Front door on `axiom-api` | Anonymous `GET /api/health` |
| --- | --- |
| Function URL, auth `NONE` **and** a resource policy granting `Principal: "*"` | **403** |
| HTTP API, `$default` route, same function, same moment | **200** |

The function also answers signed HTTP requests directly, with the gateway out of the picture:

```bash
./.venv/bin/python deploy/lambda/signed_curl.py /api/health
```

### Other required fields

- **Repository:** https://github.com/Dhruvjain35/axiom — public, Apache-2.0 (`LICENSE`
  present). The "newly created during the submission period" rule is evidenced by the commit
  history.
- **Demo URL:** **https://nq0i2ob395.execute-api.us-east-2.amazonaws.com/** — live, anonymous,
  **$0.0001021066 month-to-date and projected under $1.00 through Sep 15**, must stay up
  through judging. A 5-minute EventBridge sweep keeps its queue draining so it does not go
  stale unattended. Re-create with `./deploy/lambda/apigateway.sh` if it is ever torn down;
  the URL changes if the API is recreated, so tear it down only deliberately.
- **Video:** *(under 3:00 — shot list in §9.)*

---

## 8. Pre-submission checklist

Ordered by how badly it hurts to get it wrong.

```
[ ] Repo public, history intact, LICENSE present                       — DONE
[ ] ≥2 CockroachDB tools genuinely used, §7 true on the day            — 3 in use, 4th written
[ ] ≥1 AWS service genuinely used                                      — 6 of 11: Lambda, API Gateway,
                                                                         EventBridge Scheduler, CloudWatch,
                                                                         SNS, X-Ray
[ ] Cost stated as cents, not $0.00 — the account is PAID with no
    12-month free tier, so API Gateway / X-Ray / Comprehend bill        — $0.0001021066 MTD, budget $1.00
[ ] CLICK THE SNS CONFIRMATION EMAIL. Until someone does, five alarms
    fire into a void and the demo can break silently for four weeks.    — PENDING
[ ] Video recorded, UNDER 3:00, link tested in a logged-out browser
[ ] Video says "effectively-once, not exactly-once" OUT LOUD
[ ] Video shows a worker being SIGKILLed on camera
[ ] Video shows the PROVIDER's ledger for the duplicate check, not AXIOM's
[ ] Numbers on screen re-measured on the cluster shown in the video
[ ] Demo URL live                                                      — DONE, apigatewayv2
[ ] /api/demo/reset: DECIDE. Public and ungated today (deliberate — the
    UI's buttons need it). Set AXIOM_DEMO_TOKEN to close it, and accept
    that RESET/RUN MISSION stop working for a judge if you do.
[ ] Uptime check on whatever URL is submitted, alerting through Sep 15
[ ] README / SUBMISSION / JUDGING agree on every number                — DONE 2026-08-11
[ ] Submit a day early
```

---

## 9. Video shot list — target 2:52, hard limit 3:00

Real screen recording. Clean audio. No slide deck of bullets, no stock music under narration,
no logo intro, no team introduction, no roadmap. The system on screen, doing the thing.

### Before you hit record

**Record against the deployed system.** There is a public URL now —
https://nq0i2ob395.execute-api.us-east-2.amazonaws.com/ — so the browser pane should be
pointed at it rather than at localhost, and the address bar is worth one deliberate beat
on camera. The one thing the deployment cannot do on film is the SIGKILL: you cannot
`kill -9` a Lambda invocation, so the crash demo is still driven through
`POST /api/demo/run-worker {mode:chaos}`, which kills the worker at W4 inside the
invocation. If you record the SIGKILL locally instead, say so once, without apologising:

> "This is running locally against CockroachDB Cloud; the same code is deployed on AWS Lambda."

**Terminal setup.** Two panes, side by side, same window. Font at a size that is legible at
720p — test it by recording ten seconds and watching it at 720p before you record the real
thing. Dark background, no transparency, no fancy prompt.

- **Left pane (big):** the chaos demo.
- **Right pane:** the provider's ledger. Have this connected and ready *before* you start.

Environment, in both panes:

```bash
cd ~/axiom
export DATABASE_URL='postgresql://root@localhost:26257/axiom?sslmode=disable'   # or Cloud
export AXIOM_OFFLINE=1
```

Dry-run the whole thing once, unrecorded, end to end. Re-measure any number you plan to say
against the run you are about to show. **Never narrate a figure from a different run than the
one on screen.**

### The shots

| # | Time | On screen | Beat |
| --- | --- | --- | --- |
| 1 | 0:00–0:15 | Mission Control, 30 tiles | The question |
| 2 | 0:15–0:27 | The four-class table | The reframe |
| 3 | 0:27–1:25 | Terminal, chaos demo running, kills scrolling | Kill workers on camera |
| 4 | 1:25–1:50 | Provider's request log + ledger, right pane | Three requests, one effect |
| 5 | 1:50–2:15 | `counterexample.py` output | $600 vs $300 |
| 6 | 2:15–2:40 | Schema / crash-window table | Why CockroachDB, and the disclaimer |
| 7 | 2:40–2:52 | `DUPLICATE REFUNDS 0` | The line |

---

**SHOT 1 — 0:00–0:15 — The question.**
*On screen: Mission Control, the 30-tile grid, idle. No cursor movement.*

> "An agent is resolving thirty order exceptions. It issues a three-hundred-dollar refund to
> customer eighteen — then the process dies, before it records that the refund succeeded.
>
> It restarts. Does customer eighteen get refunded twice?
>
> In most agent frameworks, nobody knows."

---

**SHOT 2 — 0:15–0:27 — The reframe.**
*On screen: the four-class table. Static. No animation, no transition.*

> "Agent memory is usually treated as recall. The memory that matters in production is the
> memory of what the agent has already **done**.
>
> Episodic, semantic and procedural memory **advise**. Execution state **constrains**."

---

**SHOT 3 — 0:27–1:25 — The demo. This is the video.**
*On screen: full terminal. Run it live:*

```bash
./.venv/bin/python scripts/chaos_demo.py --workers 3 --kill-every 1.8
```

> "Thirty exceptions. Three workers. I'm killing one every 1.8 seconds — SIGKILL, so no
> cleanup handler runs, no `finally` block runs, no lease is politely released. That's what an
> OOM kill and a spot reclamation actually look like."

*Now stop talking and let it run. Ten to fifteen seconds of kills scrolling past, uncut. Do
not speed it up. Do not cut away. This shot is the entire argument and a judge needs to see
that it is real.*

> "Watch order ten twenty-seven. The refund reached the provider — the money moved — and the
> worker died before it could record that. Worst possible instant."

*Bring up the journal for that task — the eight-row `axiom_event` sequence.*

> "Another worker claims it. In **one transaction**, it reads the receipt of what the dead
> worker did, semantically recalls what happened the last time an agent died at this exact
> execution state, and decides: re-send, under the same derived key.
>
> Then it got killed again. Same decision. Same key."

---

**SHOT 4 — 1:25–1:50 — The provider's own books.**
*On screen: the right pane. The **provider's** request log and ledger — the database AXIOM
cannot enlist in a transaction and never writes to. Say that out loud; it is the point.*

> "This is the provider's log — a separate database, on its own connection, that AXIOM cannot
> enlist in its transactions. Exactly the relationship you have with a payments API.
>
> It saw **three** requests for that order. It made **one** refund.
>
> Across the whole run: thirty kills, thirty of thirty tasks finished, eighteen refunds
> requested, eighteen refunds created, six re-sends absorbed. **Zero duplicates** — and both
> ledgers reconcile to the cent, two thousand and forty-two dollars four cents on each side."

*Replace those figures with the ones from the run you just recorded if they differ.*

---

**SHOT 5 — 1:50–2:15 — The counterexample.**
*On screen: run it, or show the output of a run from moments earlier.*

```bash
./.venv/bin/python scripts/counterexample.py
```

> "Is that hard? Here's a fair baseline: a transcript-memory agent that `fsync`s its
> transcript, re-reads it on restart, checks whether it already acted, and records its intent
> *before* it calls. The best you can do without a transaction.
>
> Same order. Same crash. Same instant. It pays **six hundred dollars**. AXIOM pays three
> hundred.
>
> It can't tell 'the call never went out' from 'the call went out and I died' — and it can't
> reuse the original key, because nothing ever wrote that key down."

---

**SHOT 6 — 2:15–2:40 — Why CockroachDB, and the disclaimer.**
*On screen: the schema — `retrieval_class` and the vector index prefix — then the
crash-window table.*

> "This needs one database, because the receipt and the memory commit **together**. Split it
> across a workflow engine and a vector store and there's a window where the agent resumes on
> memory that's already been revoked, with no transaction to close it. Here, quarantine is an
> update to a computed column that is a **prefix of the vector index** — the row physically
> moves, and it's gone from the candidate set inside the same transaction.
>
> Seven crash windows. Every one has a defined outcome and a test that tries to cause the
> failure and fails.
>
> And to be precise: this is **effectively-once, not exactly-once**. No system that calls an
> API it doesn't control can promise exactly-once. What it can promise is a derived key, a
> durable receipt, and a defined outcome in every window."

---

**SHOT 7 — 2:40–2:52 — The line.**
*On screen: cut to the final ledger. `DUPLICATE REFUNDS  0`.*

> "Memory is not what the agent remembers.
>
> It's what makes the agent safe to run."

*Hold on the zero for two seconds. End. No outro card, no music sting.*

### Rules for the recording

- **Kill a worker on camera and do not cut away from it.** It is the entire demo.
- **Show the provider's ledger, not AXIOM's, for the duplicate check.** The whole argument is
  that the external party — the one AXIOM cannot enlist in a transaction — agrees.
- **Say "effectively-once, not exactly-once" out loud.** A distributed-systems judge trusts the
  project more for the disclaimer and stops trusting it instantly without one. Do not let
  "exactly-once" slip out anywhere else in the narration.
- **Re-measure before you narrate.** Every figure spoken must come from the run being shown.
- **No glow, no floating dots, no emoji, no purple-blue gradients.** Dense, restrained, fast,
  well-aligned.
- **Check the length before uploading.** 3:00 is a hard limit; 2:52 leaves no room for a
  rambled sentence, so if a take runs long, cut narration rather than the kill sequence.
