# AXIOM — the case, criterion by criterion

The hackathon scores five criteria, **equally weighted**. This document takes each one in
turn and states three things: what AXIOM actually does, the file or command that proves it,
and the honest limitation. It is also the self-check the project was audited against, which
is why the scores below include the bad one.

Written for a judge with fifteen minutes. Every claim here points at something runnable.

> **If you read one thing, read the last section — [Where this is weakest](#where-this-is-weakest).**
> It is first-person about the holes, because a judge who finds an overclaim discounts
> everything else, and there are enough real results here that it would be a bad trade.

---

## The one-paragraph version

An agent issues a $300 refund, crashes before recording it, restarts. Does it refund twice?
AXIOM commits execution state and semantic memory in **one serializable CockroachDB
transaction**, so the answer is a database fact rather than an inference from a conversation
transcript. Four memory classes carry different authority — episodic and semantic *advise*,
procedural *authorizes*, execution state *constrains*. Measured: 30 tasks through 30
`SIGKILL`s, 6 idempotent replays absorbed by an external provider, **0 duplicate refunds**,
against a fair baseline that double-refunds the same order at the same crash instant.

---

## 1. Agentic Memory Design — audited 9/10

### What it does

Memory is modelled by **authority**, not by storage format. Four classes:

| Class | Question | Where | Authority |
| --- | --- | --- | --- |
| Episodic | What happened last time we saw this? | `axiom_memory` (`EPISODIC`) | advises |
| Semantic | What past situations resemble this one? | `axiom_memory` (`SEMANTIC`) | advises |
| Procedural | Which policy applies, at which version? | `axiom_policy` | authorizes |
| Execution | What has this agent already *done*, irreversibly? | `axiom_task` + `axiom_action_attempt` | **constrains** |

The separation is enforced by the schema and the type system, not by convention:

- `axiom/llm.py` returns a `Triage` proposal and **physically cannot mint an idempotency
  key**. Only `tasks.prepare()` authorizes an act, and the key it mints is a
  `GENERATED STORED` column no application code can supply.
- Memory is allowed to override the default in **one direction only** — toward escalation.
  Recalled evidence can stop an act. It can never talk the system into one.
- **Admissibility is a vector-index prefix column.** `retrieval_class` is computed from
  `quarantined`, `superseded_by` and `trust_level`, so a quarantined memory lives in a
  different partition of the ANN index and never enters the candidate set. Post-filtering an
  ANN result silently returns fewer rows than `LIMIT` and drops true nearest neighbours —
  that is a wrong answer, not a slow query.
- Recovery is **one commit**: re-check the fence, point-read the durable receipt, ANN-search
  episodic memory for what happened the last time an agent died at this exact execution
  state, decide RESEND / ESCALATE / REPLAN, append the decision and its evidence to the
  journal. All of it, or none of it.
- Every irreversible act records `licensed_by_memory_id`. If a memory is later found to be
  poisoned, that column enumerates every real-world effect it authorized.

### Verify it

| Claim | Where |
| --- | --- |
| Four classes, different authority | `db/001_schema.sql` — `axiom_memory`, `axiom_policy`, `axiom_task`, `axiom_action_attempt` |
| Admissibility is a prefix column | `db/001_schema.sql`, `retrieval_class` + `axiom_memory_ann_by_context` |
| Recall really uses the index | `./.venv/bin/python -m pytest -q tests/test_recall_plan.py` — asserts on `EXPLAIN`, not output |
| The fused recovery transaction | `axiom/tasks.py::recover`, `axiom/memory.py::recall` |
| Quarantine takes effect at commit | `scripts/preflight.py` gate 6, and the live run recorded in `README.md` |

### Honest limitation

- **Offline embeddings are a deterministic hash sketch, not Titan.** They preserve enough
  structure for ranking to be meaningful and for tests to be exact. Recall *quality* under
  `AXIOM_OFFLINE=1` is not evidence about recall quality under Titan V2, and the headline
  numbers were all measured offline. Bedrock is reachable from the deployment account and
  answers, but its on-demand quota there is 0.0 requests/minute and not adjustable — §4.
- **Until 2026-08-13 the corpus said otherwise, and that was a defect.**
  `axiom_memory.embedding_model` carried `NOT NULL DEFAULT 'amazon.titan-embed-text-v2:0'`
  and no insert path ever set it, so every row claimed Titan while holding sketches and
  fixtures. Every row has since been reclassified by *measurement* — the offline sketch if
  `cos(stored, offline_embed(its own content)) > 0.99999`, the test fixture if it reproduces
  `sin(r*0.7 + d*0.013)` to `1e-6`, and zero rows matched neither. The corpus now reads
  11 × `offline-blake2b-sketch-v1` and 2,500 × `synthetic-sine-fixture-v1`, the latter being
  `tests/test_recall_plan.py`'s corpus in its own tenant — which is what makes "the index is
  still chosen at 2,500 rows" checkable on the live cluster. `db/005_embedding_space.sql`
  drops the default, so forgetting the model is now an error rather than a lie.
- **"Memory may only escalate" is a design decision, not a proven-optimal one.** It is the
  conservative choice. Nothing here measures what it costs in tasks that could have been
  completed automatically.
- **One workload.** The design is argued for e-commerce refunds. Multi-tenant hotspot
  behaviour under genuinely high throughput is reasoned from CockroachDB's documentation,
  not measured here.

---

## 2. Technological Implementation — audited 8/10

### What it does

**The database is the design.** `db/001_schema.sql` is 748 lines and most of it is `WHY`
comments. The load-bearing decisions:

- **The idempotency key is a `GENERATED STORED` column** derived from
  `(tenant_id, task_id, step_name, step_seq)` — all immutable. The lethal bug in this class
  of system is a key minted at call time from a UUID, timestamp, worker id, attempt number
  or lease epoch: the recovering worker mints a *different* key and the $300 goes out twice.
  Computing it removes that from the codebase rather than from the code review.
- **The claim index is PARTIAL and never sees a `DELETE`.** CockroachDB's own hotspot
  guidance names queues as an anti-pattern. AXIOM's answer is one index, partial on
  non-terminal states so finished work *leaves* it, prefixed by an application-assigned
  `shard` so the queue head is N ranges, `STORING` the columns that keep the claim scan
  index-only.
- **`shard` is an explicit computed column, not `USING HASH`,** so a worker can be pinned to
  a shard subset. `USING HASH` appears exactly once, on the genuinely monotonic event
  timeline.
- **The fencing token, not the lease, is the correctness mechanism.** A lease expiring does
  not stop a GC-paused worker already inside a refund HTTP call. Every write after the claim
  re-checks a per-row monotonic `lease_epoch`.
- **`available_at` does double duty** as earliest-run-time and lease expiry, so
  `available_at <= now()` means "ready **or** the owner is dead" — and there is no reaper
  process, because a reaper is a periodic large multi-row transaction landing on exactly the
  rows the claim loop wants.

**Seven crash windows, seven tests.** `tests/test_crash_windows.py` does not assert that
AXIOM works. It assembles the conditions under which the design would corrupt state — an
expired lease mid-refund, two workers holding one fence, a recovered agent that
re-synthesized a different request body, threads racing one budget — and asserts the system
refuses. The full spec is `docs/CRASH_WINDOWS.md`.

**Assert on plans, not on output.** Index selection, prefix spans and opclass choice all
degrade silently while returning correct rows. `scripts/preflight.py` is 17 gates that read
`EXPLAIN`.

### Verify it

```bash
export DATABASE_URL='postgresql://root@localhost:26257/axiom?sslmode=disable'
export AXIOM_OFFLINE=1

./.venv/bin/python -m pytest -q
#   178 passed
#   13 crash-window · 17 invariant · 15 lambda-worker · 5 recall-plan · 14 schema-sync

DATABASE_URL='postgresql://root@localhost:26257/defaultdb?sslmode=disable' \
  ./.venv/bin/python scripts/preflight.py
#   16/16 blocking gates, 1 advisory, exit 0
```

`178 passed in 43.09s` is a run made while writing this document, on the local v26.2.3 node.
The 222-second figure quoted elsewhere is the same suite against CockroachDB Cloud.

### Honest limitation

- **The MCP path has no automated test.** It needs a live cluster and a service-account key,
  so it cannot run unattended. Everything about it was verified by hand, once.
- **The Cloud cluster is BASIC, single-region `aws-us-east-1`.** Latency and `40001`
  contention are real, but nothing here demonstrates surviving the loss of a region. No
  `REGIONAL BY ROW`, no survival goal.
- **Row-level security is written and commented out** in `db/001_schema.sql`, deliberately: a
  misconfigured `FORCE RLS` returns zero rows *silently*, which is the worst thing to
  discover mid-demo. The tenant boundary today is `tenant_id NOT NULL` everywhere, leading
  every access-path index, with a mandatory predicate in every query.
- **`gc.ttlseconds` is 4500 on that cluster**, so the `AS OF SYSTEM TIME` rewind reaches back
  75 minutes, not arbitrarily. That is why `valid_from` / `valid_until` exist as the durable
  audit axis — MVCC history is a convenience, not the record.

---

## 3. Real-World Impact — audited 7/10

### What it does

The problem is not hypothetical and it is not about chatbots. Any agent authorized to move
money, ship goods, provision infrastructure or send mail has the same failure: the process
dies between the effect and the record of the effect, and nothing in a conversation
transcript can distinguish "the call never went out" from "the call went out and I died."

**AXIOM ships the comparison instead of asserting it.** `scripts/counterexample.py` runs the
same order, through the same crash, at the same instant, against the same provider — once
with a transcript-memory agent and once with AXIOM:

```
                      TRANSCRIPT MEMORY                   AXIOM
killed in W4          yes                                 yes
policy gate           none — refunds $300 unattended      sent to a human first
recovery decision     retry — cannot know if it landed    RESEND under the same key
REFUNDS CREATED       2                                   1
DOLLARS OUT           $600.00                             $300.00
```

**The baseline is not a strawman, and that is the point.** It `fsync`s its transcript,
re-reads it on restart rather than starting blank, checks for evidence it already acted, and
records intent *before* calling — the best you can do without a transaction. It still pays
twice, structurally.

Beyond the refund: hard mission spend caps enforced by a `CHECK` constraint, human-in-the-loop
approvals as single-use capability tokens, multi-tenancy from row one, an append-only journal
where every transition is written in the transaction that performs it, and an audit agent
that answers natural-language questions in SQL under a **database-enforced read-only role**
(`db/002_audit_role.sql`) — `SELECT` and nothing else, plus a statement guard, plus
`default_transaction_read_only`.

### Verify it

| Claim | Where |
| --- | --- |
| The baseline double-refunds | `./.venv/bin/python scripts/counterexample.py` |
| The baseline is fair | `axiom/baseline.py` — read it; the fairness is in the code |
| Spend cap is a constraint | `db/001_schema.sql`, `axiom_mission` budget `CHECK` |
| Approvals are single-use | `axiom/tasks.py::consume_approval`, `axiom_approval_one_pending` |
| Audit containment | `db/002_audit_role.sql`, `axiom/audit_mcp.py` |

### Honest limitation

- **The provider is simulated.** It implements Stripe's idempotency semantics faithfully —
  same key + same fingerprint replays; same key + different fingerprint is rejected `409` —
  in a separate database over a separate connection that AXIOM cannot enlist in its
  transactions. But it is not Stripe, and no real money moved. Pointing `axiom/provider.py`
  at Stripe test mode behind a flag is the single change that would remove this objection,
  and it is not built.
- **No one has run this but its author.** There are no users, no pilot, no third-party
  deployment. The impact argument is an argument.
- **One vertical.** Refunds. The design generalizes on paper; nothing here demonstrates it.

---

## 4. Product Readiness — audited 3/10, and this is the weak axis

The audit was blunt about this: with five equally weighted criteria, a 3 costs more than the
other four earn. It is stated first here rather than buried.

### What is actually true

- **Deployed on AWS Lambda, for cents a month, and it works.** Two functions (`axiom-api`, `axiom-worker`)
  in `us-east-2`, arm64, python3.13, talking to CockroachDB Cloud in `us-east-1`. Measured on
  the real deployment: cold start `INIT` 1447–2258 ms, warm `/api/health` 169 ms (two
  cross-region queries), `/api/crash-windows` 2.7 ms, peak memory 149 MB of 512 MB. Freeze/thaw
  was tested rather than reasoned about — invoke, wait 17 s / 30 s / 73 s / 220 s / 14 min,
  invoke again; no request in any state returned a 500. `/`, `/styles.css`, `/api/mission`,
  `/api/crash-windows` and `POST /api/memories/recall` all answer 200. Full numbers and the
  method: `deploy/lambda/README.md`.
- **Nothing bills at rest — and it is not $0.00, which this section claimed until
  2026-08-14.** The account's own free-tier state was read rather than assumed:
  `aws freetier get-account-plan-state` returns `accountPlanType: PAID` with `$0.00`
  remaining credits, and `get-free-tier-usage` returns **twelve entries, every one "Always
  Free" and not one "12 Months Free"**. So every AWS free tier that is a twelve-month offer
  has expired here, and the old claim leaned on one of them. Always free on this account:
  Lambda, CloudWatch, SNS, SQS, KMS, Glue, SES. **Billed: API Gateway ($1.00/M requests),
  X-Ray ($5.00/M traces), Comprehend ($0.0001/unit, and off by default).**
  **Month-to-date, all services: $0.0001021066. Projected through Sep 15: under $1.00**,
  with an AWS Budget `axiom-zero-spend` at $1.00 alerting at 1% / 50% / 100% — the owner is
  emailed at one cent. The ZIP is 11.2 MB, under the 50 MB direct-upload limit, so there is
  still no bucket, no ECR, no ALB, no NAT, and nothing here has an hour hand. A project
  whose whole argument is that it measures rather than asserts does not get to round its own
  bill down to a rounder number.
- **The demo is instrumented to survive four unattended weeks, and one link is unclicked.**
  An **EventBridge Scheduler** sweep (`axiom-worker-sweep`, `rate(5 minutes)`, target
  `axiom-worker` in `drain` mode for 45 s with `idle_exit`) keeps the queue draining, so a
  judge arriving on Sep 3 does not open a board frozen since Aug 23 — verified firing three
  times in six minutes. **Five CloudWatch alarms** (`axiom-api-errors`,
  `axiom-api-throttles`, `axiom-http-5xx`, `axiom-worker-errors`, `axiom-worker-silent`) page
  over **SNS**, with thresholds loose enough to survive AXIOM's own design: the demo crashes
  its worker on purpose at W4, so the worker alarm needs >30 errors in 15 minutes twice
  rather than >0. **X-Ray** traces the crash-and-recovery path with subsegments on PREPARE,
  the provider dispatch, SETTLE and the recovery recall, annotated with task id, crash window
  and whether the provider reported an idempotent replay. **The honest caveat:** the SNS
  email subscription requires a human to click AWS's confirmation link and currently reads
  `PendingConfirmation`, so **the alerting path is not yet proven end to end for this
  subscription** — the alarms will enter ALARM and the notification will go nowhere.
- **The engine, API, worker, Mission Control UI, audit agent, chaos harness and counterexample
  all run**, from a shell or from Lambda.

### The public URL, and the blocker it had to route around

**https://nq0i2ob395.execute-api.us-east-2.amazonaws.com/**

```console
$ curl https://nq0i2ob395.execute-api.us-east-2.amazonaws.com/api/health
{"ok":true,"db":true,"provider":true,"version":"0.1.0","offline":true,"errors":{}}
```

Anonymous, unsigned. An HTTP API in front of the same `axiom-api` Lambda, built by
[`deploy/lambda/apigateway.sh`](../deploy/lambda/apigateway.sh).

It is served through API Gateway rather than a Lambda Function URL for a reason worth
stating, because the alternative is a judge finding the 403 themselves and drawing the
wrong conclusion. **This AWS account refuses anonymous access to Lambda Function URLs,
and the refusal is account-level.** It is not the code. The controlled experiment — one
function, one unchanged resource policy granting `lambda:InvokeFunctionUrl`:

| Setup | Result |
| --- | --- |
| Role **with** an identity policy allowing `lambda:InvokeFunctionUrl` | **200** |
| Same role, identity policy removed, resource policy unchanged | **403** |

So resource-based grants on a Function URL are not honored on this account. Both free public
paths are exactly that kind of grant, and both 403: auth type `NONE` (a resource policy
granting `Principal: "*"`, tested in two regions, on this function and on a throwaway
hello-world function, over a 15-minute window) and CloudFront + Origin Access Control (a
resource policy granting `cloudfront.amazonaws.com`, with the distribution `Deployed` and the
OAC signing correctly). Ruled out: propagation, policy syntax — `aws lambda add-permission`
writes the statement AWS itself dictates, and `iam simulate-principal-policy` returns
`allowed` — and SCPs, since the account is in no organization. The account was created hours
before the deployment and is pending activation; public Lambda URLs are an obvious abuse
vector for a new account.

**API Gateway is a different service and is not subject to that restriction.** Both front
doors need a Lambda resource policy statement, but not the same grant: a Function URL needs
`lambda:InvokeFunctionUrl` for an anonymous principal, evaluated by the Function URL front
end, which is what this account withholds; API Gateway needs `lambda:InvokeFunction` for
the named service principal `apigateway.amazonaws.com`, evaluated by the Lambda control
plane, which is honored normally. Both doors are live on the same function right now, so
the restriction is demonstrable rather than asserted:

| Front door on `axiom-api` | Anonymous `GET /api/health` |
| --- | --- |
| Function URL, auth `NONE` **and** a resource policy granting `Principal: "*"` | **403** |
| HTTP API, `$default` route, same function, same moment | **200** |

**The function is also testable with the gateway out of the picture, over a signature:**

```bash
./.venv/bin/python deploy/lambda/signed_curl.py /api/health
./.venv/bin/python deploy/lambda/signed_curl.py -X POST /api/memories/recall \
    -d '{"query":"refund policy for delayed orders","k":3}'
```

`deploy/free-tier/` was the fallback that does not depend on a Lambda resource policy: one
EC2 instance with a public IP, ~$10.40/month. Written, never applied, and no longer needed.

### The rest of the readiness gap

- **No CI.** The 178 tests pass when a human runs them. "Passes when run" is weaker than
  "cannot regress", and that gap is exactly the property this project sells.
- **The AWS URL is not the monitored one.** `scripts/uptime_check.sh` asserts the demo is
  *usable* rather than merely reachable and passes 6/6 against the gateway when run by hand,
  and `.github/workflows/uptime.yml` runs it every 30 minutes through the judging window —
  but its `BASE` points at the Vercel deployment, so a break in the AWS one during
  Aug 19 – Sep 15 is silent. Point `BASE` at whichever URL is submitted, or add a second job.
- **`POST /api/demo/reset` is unauthenticated** and CORS is `allow_origins=['*']`. This was
  an open item until a public URL existed; two now do, so it is a decision, and it was made
  in the judge's favour — Mission Control's buttons send no token, so gating the routes
  removes RESET and RUN MISSION from the person the demo exists for. Bounded by design:
  reset re-seeds rather than empties, each route has a minimum interval, none can create
  unbounded work. `AXIOM_DEMO_TOKEN` closes it at the cost of the buttons.

---

## 5. Creativity & Originality — audited 9/10

### What it does

The original contribution is the **reframe**, and it is falsifiable rather than rhetorical:

> Memory is not saved chat history. Memory is what makes autonomous **action** safe.
>
> Vector memory tells the agent what it *could* do; transactional execution state decides
> what it *may* do.

Three artifacts follow from it that are unusual in this category:

1. **The crash-window table is treated as a correctness specification**, not documentation.
   Seven windows, each with a defined outcome that is a consequence of commit ordering rather
   than a hope about timing, each with a test that *tries to cause the failure and fails*.
   `docs/CRASH_WINDOWS.md` is one page per window.
2. **The counterexample is a shipped artifact.** Most entries assert that the naive approach
   fails. This one builds the fair naive approach, runs it through the identical crash, and
   shows it paying $600 where AXIOM pays $300 — and the script prints `INCONCLUSIVE` rather
   than `PASS` if the baseline fails to double-refund, so a rigged run cannot masquerade as a
   result.
3. **A demo that is allowed to fail.** `scripts/chaos_demo.py` fails on zero idempotent
   replays, because a run where no crash landed in the dangerous window proved nothing.

### Verify it

`docs/CRASH_WINDOWS.md` · `tests/test_crash_windows.py` · `scripts/counterexample.py` ·
`skills/cockroachdb-application-development/implementing-crash-safe-work-queues/SKILL.md`

### Honest limitation

**None of the individual mechanisms is novel, and it would be embarrassing to imply
otherwise.** Fencing tokens are Kleppmann's, 2016. Idempotency keys are Stripe's. The outbox
pattern is a decade old. Partial indexes and computed columns are documented CockroachDB
features. What is new here is the composition — fusing execution state and vector recall into
one serializable transaction, and organizing memory by authority rather than by recency — and
the insistence that each of those choices carry a test that tries to break it. That is a real
contribution. It is not an invention.

---

## The audit, and what was done about it

An independent rubric audit scored the entry before this document existed:

| Criterion | Score |
| --- | --- |
| Agentic Memory Design | 9 / 10 |
| Technological Implementation | 8 / 10 |
| Real-World Impact | 7 / 10 |
| **Product Readiness** | **3 / 10** |
| Creativity & Originality | 9 / 10 |
| **Mean (equal weights)** | **7.2** |

Its central finding: because the criteria are equally weighted, the 3 costs more than the
other four earn. Lifting only Product Readiness to a 7 moves the entry to 8.0 without
touching a line of engine code — so that is where the remaining effort went.

| # | Gap the audit found | Status |
| --- | --- | --- |
| 1 | Docs contradicted each other — README said "49 tests" (it is 92) and "Nothing is deployed" (Lambda is); `SUBMISSION.md` publicly said the repo was unpushed, `ccloud` unused, MCP unexercised | **Done.** README and `docs/SUBMISSION.md` reconciled to one status, verified against a live test run and the deployment record |
| 2 | Fourth CockroachDB tool was design-intent only | **Done.** `skills/cockroachdb-application-development/implementing-crash-safe-work-queues/` — 390 lines, passes the upstream `validate-spec.py --strict` with zero warnings. **PR not opened**; that is a conversation with maintainers, not a command |
| 3 | `memory.py` valid-time filter compared `valid_until` to `occurred_at` instead of `now()`, so it never filtered | Owned by the engine workstream — **confirm before submitting** |
| 4 | `counterexample.py` claimed any claimable task in the tenant, so it crashed on a second run | Owned by the engine workstream — **confirm before submitting** |
| 5 | One button in the UI did not reproduce the headline: `RUN MISSION` yielded 0 replays while the dashboard printed `DUPLICATE REFUNDS 0` above it | Owned by the UI workstream — **confirm before submitting** |
| 6 | No CI | Not done. Stated as a limitation in §2 rather than papered over |
| 7 | No reachable demo URL | **Solved.** Public and anonymous at https://nq0i2ob395.execute-api.us-east-2.amazonaws.com/ — the Function URL restriction was routed around with API Gateway, §4 |
| 8 | Provider is simulated | Not done. Stated in §3 |

---

## Where this is weakest

Read this before going looking. Nothing below is a surprise to the authors.

1. **The AWS demo URL's alerting is one unclicked link away from working, and it is one
   region deep.** It is live and anonymous (§4) and costs cents against a $1.00 budget, so
   nothing lapses for non-payment. Two things still watch it imperfectly:
   `.github/workflows/uptime.yml` points its `BASE` at the Vercel deployment rather than this
   URL, and the five CloudWatch alarms that *do* watch this one deliver over an SNS
   subscription still reading `PendingConfirmation`. Until somebody clicks that confirmation
   email, a break during Aug 19 – Sep 15 is still silent — the alarm state changes and the
   mail goes nowhere. Gateway and function are both `us-east-2` against a single-region
   `us-east-1` cluster; nothing here survives a region loss.
2. **The provider is simulated.** The idempotency semantics are faithful and the provider is
   genuinely outside AXIOM's transaction, but no real money moved, so "0 duplicate refunds"
   is a statement about a database AXIOM does not write to — not about Stripe.
3. **The tests pass when a human runs them.** No CI. For a project whose pitch is "cannot
   regress by construction", that is the most ironic gap on the list.
4. **The headline numbers were measured with `AXIOM_OFFLINE=1`.** Deterministic embeddings and
   rule-based triage, no Bedrock in the loop, so the runs are hermetic and reproducible. That
   is the right call for a crash-safety measurement and it does mean the quoted runs are not
   evidence about Titan-quality recall. Bedrock is **enabled on the deployment account and
   both models answer** — `amazon.titan-embed-text-v2:0` returns a real 1024-d vector and
   `anthropic.claude-sonnet-4-5` replies — but the on-demand quota for Titan V2 is **0.0
   requests/minute and 0.0 tokens/minute**, quota `L-26C560CE` with `Adjustable: false`, the
   same in `us-east-1`, `us-east-2` and `us-west-2`. A sustained probe got 0 of 10 calls
   through in 87.4 s, all `ThrottlingException`; isolated single calls do land, on a burst
   allowance, which is why one probe looks like proof and is not. Batch inference is
   available and was not used: its 100-record minimum against a 10-text corpus would have
   meant embedding filler to earn the checkbox. So both Lambda functions run offline too.
5. **One measured workload, one region, one cluster plan.** BASIC, single-region. Nothing here
   survives a region loss, and nothing here was run at a throughput that would test the
   sharding claims.
6. **The MCP integration was verified by hand, once.** It works — it caught the baseline
   agent's double refunds against the live Cloud endpoint — and it has no automated test.

## What AXIOM must never be read as claiming

**Not exactly-once.** That guarantee is unavailable to any system calling a network API it
does not control, and any project claiming it is either wrong or talking about something else.
AXIOM provides durable, idempotent, **effectively-once** execution: every external action is
issued under a derived idempotency key against a durable receipt, and every crash window has a
defined and tested outcome. **Effectively-once, never exactly-once.**

## Open item for the operator, not for engineering

---

## Run everything yourself in five minutes

```bash
export DATABASE_URL='postgresql://root@localhost:26257/axiom?sslmode=disable'
export AXIOM_OFFLINE=1

./.venv/bin/python -m pytest -q                    # 178 passed
./.venv/bin/python scripts/counterexample.py       # baseline $600, AXIOM $300
./.venv/bin/python scripts/chaos_demo.py --workers 3 --kill-every 1.8 --quiet
                                                   # must end PASS, DUPLICATE REFUNDS 0
```

Run them in that order on a freshly seeded database. `counterexample.py` claims a task from
the demo tenant's queue, so running it while a chaos run is mid-flight — or on a database left
dirty by an interrupted one — can have it pick up a task that is not its own. Re-seed
(`scripts/chaos_demo.py` seeds itself) if it behaves oddly. That coupling is item 4 in the
audit table above and is owned by the engine workstream.

Setup from a clean machine is in the README. If any of it does not do what this document says,
that is a defect and the document is wrong — which is the standard the whole project is
arguing for.
