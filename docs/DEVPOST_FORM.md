# Devpost form — paste-ready

Every field on the CockroachDB × AWS submission form, in the order the form asks for it.
Copy the block under each heading. Character counts are checked against the form's limits.

`docs/SUBMISSION.md` is the long internal version — the reasoning, the measurements and the
audit trail. **This** file is what goes in the boxes.

---

# GENERAL INFO

## ✱ Project name — 48 / 60

```
AXIOM — crash-safe agentic memory on CockroachDB
```

## ✱ Elevator pitch — 182 / 200

```
An agent refunds $300, crashes before recording it, restarts, and refunds again. AXIOM makes that impossible: execution state and vector memory commit in one CockroachDB transaction.
```

---

# PROJECT DETAILS  *(public project page)*

## ✱ About the project — markdown

```markdown
## Inspiration

An agent that only writes text can be wrong. An agent that moves money can be wrong
*expensively*, and the failure is boring: the process dies between doing a thing and
recording that it did it.

Almost every agent framework remembers by keeping a transcript of its own reasoning. That
is fine for a chatbot and dangerous for anything that touches the world, because a
transcript records what the agent was *thinking*, not what actually *happened*. Restart it
after a crash and it re-reads its notes, finds an unfinished task, and does the thing
again. The customer is refunded twice. Nobody notices until the money is gone.

We wanted to know what agent memory would look like if you designed it for that failure
instead of around it.

## What it does

AXIOM is an execution and memory layer for agents that take irreversible actions. It
splits memory into four classes with **different authority**:

| Class | Answers | Authority |
| --- | --- | --- |
| Episodic | What happened last time an agent stood exactly here? | advises |
| Semantic | What is generally true of this kind of exception? | advises |
| Procedural | Which pinned policy permits this act — or stops it? | **authorizes** |
| Execution | What has this agent already done, irreversibly? | **constrains** |

The first two advise. The last two can stop the agent. **Vector recall tells an agent what
it *could* do; transactional execution state decides what it *may* do.**

The mechanism is one sentence: before the agent touches the outside world, AXIOM commits a
receipt saying what it is about to do and the idempotency key it will do it under. That key
is a `GENERATED STORED` column derived from immutable inputs, so there is no code path that
can mint a different one at call time. After a crash, the recovering worker reads that
receipt and re-sends under the same key, and the provider returns the original effect
instead of making a second one.

The interesting part is the recovery decision itself. One serializable transaction
re-checks the fencing token, point-reads the durable receipt, runs an ANN search over
episodic memory for what happened the last time an agent died at this exact execution
state, decides resend / escalate / re-plan, and commits the transition **with its evidence
attached**. One commit. Memory and outcome cannot disagree, because there is no interval in
which one exists and the other does not.

## How we built it

CockroachDB is not the store behind the agent; it *is* the agent's memory, and the two
jobs that matter happen in the same transaction:

- **Distributed vector indexing** — `VECTOR(1024)` embeddings under two C-SPANN indexes,
  built `vector_cosine_ops` explicitly. Omitting the opclass silently gives L2, and a
  `<=>` query then full-scans — a wrong answer that looks like a right one. Index use is
  asserted from `EXPLAIN` at request time rather than assumed.
- **Serializable isolation** with `40001` retry and full jitter, so two workers cannot both
  recover the same task.
- **Partial and prefix indexes** — the claim index only contains outstanding work, so claim
  cost tracks work-in-flight rather than work-ever-done.
- **A computed prefix column** for memory quarantine, so removing a memory from the index
  takes effect atomically at COMMIT rather than as a post-filter someone can forget.
- **Cloud managed MCP server** — a model can ask the cluster questions in English, contained
  three ways: a SELECT-only role, a single-statement guard, and
  `default_transaction_read_only`.
- **ccloud CLI** provisions the cluster and applies all five migrations.

On AWS: **Lambda** runs the agent workers and the API, **API Gateway** is the public front
door, **EventBridge Scheduler** sweeps the queue every five minutes so the demo survives
four unattended weeks of judging, **CloudWatch** and **SNS** alarm and page, and **X-Ray**
traces the recovery path — `axiom.PREPARE`, `axiom.dispatch` and `axiom.SETTLE`, annotated
with `crash_window=W4`, so the exact instant this project is about is filterable in the
console.

## Challenges we ran into

**The provider is outside the transaction, and no amount of database rigour changes that.**
Exactly-once execution of a remote side effect is not available to anyone. So the guarantee
is stated as what it is — *effectively-once* — and every crash window W1 through W7 has a
documented, tested outcome instead of a hope.

**Proving the memory was load-bearing rather than decorative.** It is easy to build a system
that recalls things and never acts differently for having recalled them. So the demo runs
one stopped refund through recovery three times with the same receipt, same fence, same
policy, same amount, changing only the memory table. Resend → escalate → resend. If the
verdict were identical all three times, the memory here would be decoration.

**Amazon Bedrock turned out to be unusable, and saying so was better than hiding it.**
Titan V2 answers on this account, but its on-demand quota is 0.0 requests/min, quota
`L-26C560CE`, `Adjustable=FALSE` in three regions — a sustained probe got 0 of 10 calls.
Batch inference needs ≥100 records and the real corpus is 10 seed texts, so padding a job to
clear the minimum would have bought a checkbox by embedding meaningless strings. Embeddings
run on a deterministic local model instead, and every row records which vector space it
belongs to.

**A table that lied about itself.** `axiom_memory.embedding_model` had a `DEFAULT` naming
Titan and no insert path ever set it, so every row claimed a provenance nothing had checked.
Nothing computed a wrong answer — but a project arguing that systems should tell the truth
about what they did does not get to ship that. Rows were reclassified by measurement, the
default was dropped so forgetting the column is now an error, and a preflight gate asserts
the corpus holds one vector space.

## What we learned

**Memory design is authority design.** The useful question is not "what can the agent
recall" but "what is allowed to change its mind, and what is allowed to stop it". Recall and
permission are different substances, and a transcript is the worst available place to keep
the record of an irreversible act.

**Money is often the wrong risk axis.** Forty thousand marketing emails cost about four
dollars and sail through a $200 unattended ceiling; a $300 refund to one person stops and
waits for a human. The cheap act is the dangerous one. So authority is denominated in the
act's own unit — `comms.recipients`, not cents — and the second workload in the demo proves
the same guarantee holds where there is no money at all.

**A negative result is worth measuring properly.** Bedrock's quota, the ten-times SES
pricing error we found in our own source, the tracing mode we had described wrongly — each
was caught by checking rather than asserting, and each is in the repo with the command that
found it.

## What's next

Batch-embedding through Bedrock if the account's quota is ever raised; a second real
provider behind the same contract; and the Agent Skills contribution
(cockroachlabs/cockroachdb-skills#23) merged rather than merely open.
```

## ✱ Built with — tags

```
cockroachdb, cockroachdb-cloud, aws-lambda, amazon-api-gateway, amazon-eventbridge,
amazon-cloudwatch, amazon-sns, aws-x-ray, python, fastapi, psycopg3, vector-search,
c-spann, sql, stripe, vercel, github-actions, remotion, playwright, docker
```

## "Try it out" links

```
https://axiom-one-sage.vercel.app
https://nq0i2ob395.execute-api.us-east-2.amazonaws.com
https://axiom-one-sage.vercel.app/stripe-receipt
https://github.com/Dhruvjain35/axiom
```

## ✱ Video demo link

```
(paste the YouTube / Vimeo URL after uploading out/axiom-demo.mp4 — 2:38, public or unlisted)
```

---

# ADDITIONAL INFO  *(judges and organizers)*

## ✱ URL to your functional demo application

```
https://axiom-one-sage.vercel.app
```

## Testing credentials or instructions

```
No login, no credentials, nothing to install.

Press RUN THE PROOF at the top of the page. Seven steps, about a minute, entirely live
against CockroachDB Cloud: seed the exceptions, crash a worker mid-refund, wait out the
dead worker's lease, recover under the same idempotency key, then read the payment
provider's own ledger. The number to watch is DUPLICATE REFUNDS, top right.

Then, if you have three more minutes:

  Tab 3, RUN THE EXPERIMENT — the same stopped refund recovered three times with the same
  receipt, same fence, same policy and same amount. Only the memory table changes.
  RESEND -> ESCALATE -> RESEND, three serializable transactions in one request.

  Tab 2, RUN AGAINST STRIPE — the identical crash against a real Stripe sandbox, then
  https://axiom-one-sage.vercel.app/stripe-receipt opens Stripe's OWN hosted receipt for
  that refund. No Stripe account needed. One refund, not two, stated by the counterparty.

  Tab 6, RUN A BROADCAST — the same engine where the ceiling is 2,000 recipients rather
  than dollars, and nobody is messaged twice.

The same system is also live on AWS at
https://nq0i2ob395.execute-api.us-east-2.amazonaws.com — same code, same cluster.

The demo controls are deliberately ungated so you can press them. RESET re-seeds rather
than empties; nothing you click can break it.
```

## ✱ URL to your open source and public code repository

```
https://github.com/Dhruvjain35/axiom
```

## ✱ URL to your open-source license file

```
https://github.com/Dhruvjain35/axiom/blob/main/LICENSE
```

## ✱ Which CockroachDB tools are used?  *(must select ≥ 2 — we use 3)*

- ☑ **Distributed vector indexing**
- ☑ **Cloud managed MCP server**
- ☑ **ccloud CLI**
- ☐ Agent Skills repo — *written and submitted as
  [cockroachlabs/cockroachdb-skills#23](https://github.com/cockroachlabs/cockroachdb-skills/pull/23),
  open and not merged, so we do not count it*

## ✱ Which AWS Services are used?  *(must select ≥ 1 — we use 6)*

- ☑ **AWS Lambda**
- ☑ **Amazon API Gateway**
- ☑ **Amazon EventBridge (Scheduler)**
- ☑ **Amazon CloudWatch**
- ☑ **Amazon SNS**
- ☑ **AWS X-Ray**

## ✱ How did you meaningfully integrate the CockroachDB and AWS components?

```
CockroachDB is not storage behind the agent — it is the agent's memory, and the integration
is that two jobs happen in ONE transaction that would otherwise be two systems and a race.

Distributed vector indexing. Memories are VECTOR(1024) under two C-SPANN indexes built
vector_cosine_ops explicitly, because omitting the opclass silently gives L2 and a <=> query
then full-scans — a wrong answer that looks right. The recovery path does not merely store
embeddings: it runs an ANN search over episodic memory INSIDE the same serializable
transaction that reads the idempotency receipt and commits the state transition. That is
the whole design. Split those apart and there is an interval where an agent has acted and
its memory does not know, which is exactly the bug this project exists to remove. Index use
is asserted from EXPLAIN at request time (/api/memories/recall returns
plan_uses_vector_index), and memory quarantine is a computed PREFIX column so revoking a
memory takes effect atomically at COMMIT rather than as a post-filter.

Cloud managed MCP server. axiom/audit_mcp.py speaks to cockroachlabs.cloud/mcp over
streamable HTTP with a scoped service-account key, discovering each tool's arguments from
tools/list. It lets a model ask the cluster questions in English and is contained three
ways — a SELECT-only role, a single-statement guard, and default_transaction_read_only —
because a tool that hands an LLM a database connection is a tool that needs to be unable to
write.

ccloud CLI. scripts/provision_ccloud.sh creates the Basic cluster and applies all five
migrations in dependency order; the measured results come from that cluster.

On AWS, Lambda runs the agent itself: axiom-worker claims tasks, dispatches to the provider
and recovers crashed ones (289 invocations in the last 24 hours), and axiom-api serves the
application. API Gateway is the public front door — the Lambda's own Function URL is
403-blocked at the account level, and both doors are live on the same function right now,
which turns that into a demonstration rather than an assertion. EventBridge Scheduler sweeps
the queue every five minutes so a stalled board self-heals across four unattended weeks of
judging. CloudWatch and SNS alarm on error rate, throttles and worker silence, and have
already paged a human. X-Ray traces the recovery path with subsegments on PREPARE, dispatch
and SETTLE annotated with crash_window=W4 — so the precise instant the argument is about,
where the money has moved and the system does not yet know, is filterable in the console.

Amazon Bedrock is deliberately NOT claimed. It answers on this account but its on-demand
quota is 0.0 requests/min (quota L-26C560CE, Adjustable=FALSE, same in three regions) and a
sustained probe returned 0 of 10 calls, so no vector in this system was produced by it.
```

## ✱ What date did you start this project? (MM-DD-YY)

```
08-10-26
```

## ✱ Any pre-existing code or work incorporated into the project?

```
None. Every line of AXIOM was written during the submission period — the first commit is
08-10-26 and the repository history is public and continuous.

Standard open-source dependencies only, all via pip/npm and listed in requirements.txt and
package.json: FastAPI, psycopg3, boto3, Mangum, pytest, and Remotion plus Playwright for the
demo video. No starter template, no scaffold, no prior project.

AI coding assistance: Claude (Anthropic) via Claude Code was used throughout for
implementation, review and documentation, as permitted. The architecture, the guarantees it
claims, and every measured result are ours and are reproducible from the repository.
```

## Optional: architectural diagram

```
docs/architecture.png — how CockroachDB, the AWS services and the agent interact.
```

## Optional: feedback on CockroachDB AI tools

```
The distributed vector index is the reason this project works, and one detail deserves to be
louder in the docs: vector_cosine_ops must be stated explicitly. Omitting the opclass gives
L2 silently, and a <=> query then full-scans — correct-looking rows, wrong ranking, no error.
We only caught it by asserting index use from EXPLAIN in a preflight gate, and we would
suggest the docs show that assertion as the default pattern rather than an advanced one.

Prefix columns on a vector index are excellent and under-documented. Quarantining a memory by
recomputing a PREFIX column means revocation takes effect atomically at COMMIT instead of as
an application-level post-filter. That is a real safety property for agent memory and we
found it by reading the index-acceleration notes closely rather than from any example.

The managed MCP server is genuinely useful and its containment story deserves a worked
example: SELECT-only role plus single-statement guard plus default_transaction_read_only.
Anyone wiring an LLM to a production cluster needs that, and it is the first thing a
reviewer will ask about.
```

---

# ADDITIONAL INFO — the rest

| Field | Answer |
| --- | --- |
| ✱ Submitter type | **Individual** |
| ✱ Country of residence | **United States** |
| Organization name | *(leave blank)* |
| ✱ Which AI tools did you leverage? | **Claude (Anthropic), via Claude Code** — used for implementation, code review, test design and documentation throughout. Also **Amazon Comprehend**, wired into the exception-triage path behind an authority boundary that permits it to escalate but never to authorize, and **ElevenLabs** for the demo narration. |
| ✱ Level of learning derived | **A lot** *(pick the highest honest option in the dropdown)* |
| ✱ Did you gain AI value for your career? | **Yes** |
| ☑ Not an employee of the sponsor | tick |
| ☑ From an eligible jurisdiction | tick |
| ☑ At least the age of majority | tick |

---

# BEFORE YOU HIT SUBMIT

- [ ] Upload `~/axiom-video/out/axiom-demo.mp4` (2:38) to YouTube or Vimeo, set
      **public or unlisted — not private**, paste the link in *Video demo link*
- [ ] Thumbnail: a 3:2 still. `DUPLICATE REFUNDS 0` on the live board is the frame
- [ ] Image gallery: the board, the memory-decides verdicts, Stripe's receipt page
- [ ] Open your own project page while logged out — confirm the video plays and both
      demo URLs load
- [ ] **Submit Aug 17.** The deadline is Aug 18, 5pm ET; do not spend the buffer
