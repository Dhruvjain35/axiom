# AXIOM vs. the systems you would otherwise reach for

A reasonable engineer looking at this repo has one objection, and it is a good one:

> Temporal plus an idempotency key already does the execution half. LangGraph has a
> Postgres checkpointer. Letta has archival memory with vector search. You have
> re-implemented durable execution and called it memory.

The first two sentences are correct. This page is an attempt to lose that argument
honestly where it should be lost, and to win it precisely where it can be won.

Ground rules I have tried to hold to:

- **Every limitation below is quoted from the vendor's own documentation**, linked at the
  bottom, fetched rather than remembered. Where a doc is silent on something, I say it is
  silent rather than inferring a failure.
- **No strawmen.** Each system is described doing the thing it is good at, configured the
  way its docs tell you to configure it.
- **Each section ends with when to use them instead of AXIOM**, and those sections are not
  decorative. For most workloads that reach this page the honest answer is "use Temporal."
- Nothing here is a benchmark. This says nothing about anyone's throughput, latency,
  availability, or engineering quality, all of which are better than AXIOM's.

---

## The claim, stated so it can be falsified

> **No system below lets an agent ask "what happened the last time an agent died at this
> exact execution state?" *inside the same transaction that decides what to do next.***
>
> Durable-execution engines hold history that is durable but opaque: retrievable by
> workflow id, not by meaning. Vector databases hold meaning but have no transaction for
> the execution state to join. Every real deployment therefore holds both, in two systems,
> written by two commits and read by two reads — and the recovery decision is assembled
> from a pair of answers that were never simultaneously true.

To falsify it, show me a single API call that transitions execution state and runs a
similarity search over past outcomes, atomically, such that a concurrent write is either
entirely visible to both or entirely invisible to both. I could not find one. `axiom/tasks.py::recover()`
is that call, and `scripts/incumbent_probe.py` is the experiment that tries to break it.

**What this claim is not.** It is not "these systems lose your data" — they do not. It is
not "these systems double-charge" — configured as documented, they do not, and
[the probe proves that against the same provider](#the-experiment) before it argues
anything else. The claim is about the *decision*, not the *effect*.

---

## At a glance

| | Durable execution | Semantic recall | Both **in one commit** | Authority to act |
| --- | --- | --- | --- | --- |
| **Temporal** | yes, best in class | no (see Visibility, below) | n/a — separate persistence | no |
| **Restate** | yes | no | n/a — embedded K/V, separate store | no |
| **DBOS** | yes | not built in | **yes, for Postgres steps** | no |
| **LangGraph** | checkpoint per super-step | yes, in `BaseStore` | no — two interfaces | no |
| **Letta / MemGPT** | resumable *stream* | yes, hybrid + RRF | no | no |
| **Retry + idempotency key** | the retry is yours | no | n/a | no |
| **AXIOM** | claim/prepare/dispatch/settle | yes, C-SPANN ANN | **yes** | versioned policy |

"Authority to act" is the column nobody else has a box in, and it is not a gap in their
products — it is not what they are for. It matters here because AXIOM's thesis is that
memory exists to make *action* safe, and an action needs someone to have authorized it.

---

## Temporal

**What it genuinely solves, and solves better than this repo does.** Temporal is a durable
execution engine: your workflow function's progress is a durably persisted event history,
so a process death is invisible to the workflow's logic. It has timers that survive
restarts and run for months, signals, queries, child workflows, cross-language SDKs,
schedules, and a versioning story for changing workflow code while executions are in
flight. AXIOM has none of that. If your problem is "orchestrate a long multi-step process
reliably," Temporal is the answer and AXIOM is not competing.

Temporal is also honest about the boundary AXIOM is honest about. From the Activity
definition docs:

> "For an Activity with a Retry Policy that allows retries, Temporal guarantees that the
> Activity will be observed as completed exactly once. However, the Activity may be
> executed multiple times."

and from their own blog on idempotency:

> "Activity execution is not atomic due to factors such as failures, timeouts, environment
> failure, or other conditions that lead to partial success."

So Temporal tells you to bring an idempotency key, and tells you exactly how to build one:

> "You can use a combination of the Workflow Run ID and the Activity ID as an idempotency
> key since this is guaranteed to be consistent across retry attempts but unique among
> Workflow Executions."

**That recipe works.** `scripts/incumbent_probe.py` implements it — a durable run store
plus a key that is a pure function of durable identity — kills the process in crash window
W4 (the provider committed the refund; nothing recorded it), and gets **one refund and one
idempotent replay**. The naive variant, with the key minted at call time, gets two refunds
and $300 out the door. The difference is entirely the key, and Temporal's docs get it right.

### What AXIOM does that Temporal does not

**1. Execution history is durable but not queryable by meaning.** Event History is
per-execution — "the Temporal Service tracks the progress of each Workflow Execution by
appending information about Events … to the Event History associated with that execution."
It is an append-only log of typed protobuf events retrieved by workflow id. There is no
operation that answers "across all executions, which ones died at this state, and how did
that turn out?"

The thing that *does* search across executions is Visibility, and Temporal is explicit
about its two properties. It is eventually consistent —

> "After a change is recorded, it takes some time to propagate to the index, so a List or
> Count query can briefly return stale results."
>
> "When you need the authoritative, up-to-date state of a specific Workflow Execution, use
> `DescribeWorkflowExecution` instead of a Visibility query."

— and its schema is seven scalar types, none of them a vector:

> "Bool, Datetime, Double, Int, Keyword, KeywordList, Text"
>
> "The default single Search Attribute value size limit is 2 KB. The maximum total Search
> Attribute size is 40 KB. The maximum total characters per Search Attribute value is 255."

With Elasticsearch behind it, a `Text` attribute gives you real full-text search across
workflows. That is genuinely useful and I am not going to pretend otherwise. But it is
keyword matching over fields you decided to project in advance, capped at hundreds of
characters, in an index that is by design not the source of truth — and "what does this
situation *resemble*" is not a question keywords answer.

**2. If you encrypt payloads, the server cannot index them at all.** This is a feature,
and it is the correct default for anyone handling customer data:

> "With encryption enabled, data exists unencrypted only on the Client and the Worker
> process, on hosts that you control."

A Temporal Service holding encrypted payloads is structurally incapable of being the thing
you semantically query. The security posture and the queryability are in direct tension,
and every production deployment I would want to work on picks security.

**3. Temporal's persistence is Temporal's.** The persistence store is "a database used by
the Temporal Server to persist events generated and processed in your Temporal Service and
SDK" — Cassandra, MySQL, PostgreSQL. Your business data is somewhere else. An Activity
cannot enlist Temporal's history write and your `refunds` table write in one transaction;
that is not an omission, it is what it means for Temporal to be a service rather than a
library in your process. So "the refund receipt exists" and "the workflow knows the refund
receipt exists" are two commits, and there is a window between them. Temporal's design
closes that window by *replaying* rather than by *reading* — which is the right call for
orchestration, and which is precisely why the semantic question has nowhere to live.

**4. The obvious workaround is the architecture being criticized.** You can export
histories, embed them, and index them in a vector database — Temporal Cloud supports
export. Now you have a durable execution store and a separate semantic store, updated by
two writers and read by two reads. That is the exact configuration
`scripts/incumbent_probe.py` models, and the four schedules it walks are all reachable in
it. The workaround does not dissolve the seam; it *is* the seam.

### Use Temporal instead of AXIOM when

- The unit of work is a **process**, not a decision: order fulfilment, provisioning, ETL,
  media pipelines, subscription lifecycles, sagas with compensations.
- You need **timers, schedules, signals, child workflows, or human waits measured in days**.
  AXIOM has a lease and a poll loop. That is all it has.
- You need **more than one language**, or more than one team, or an operator on call who
  has run this before.
- Your recovery decision is **"resume"**. If, on restart, the right thing to do is always
  "continue where you left off", you do not have a decision problem and AXIOM's fused
  transaction buys you nothing.

That last bullet is the honest scope of this project. AXIOM is for the case where recovery
is a *judgement* — re-send, escalate, or replan — and the judgement should depend on what
happened the last several times.

---

## Restate

Grouped with Temporal because the shape is the same and the differences do not change the
argument. Restate journals durable steps — "Restate tracks every step of your code
execution in a journal", recording "both the operation and its result" — gives Virtual
Objects a per-key K/V store ("the Restate Server includes an embedded key-value store for
persisting application state in Virtual Objects and Workflows"), and, notably, builds
**request deduplication in at the platform level**: "If you add an idempotency key to your
request headers, Restate will automatically ensure that requests are deduplicated."

That last one is a genuine ergonomic win over both Temporal and AXIOM: the dedup is
infrastructure rather than something each call site has to remember. AXIOM's answer —
`idempotency_key` as a `GENERATED STORED` column no application code path can supply — is
aiming at the same failure mode from the database side.

Restate's state lives in the Restate Server. The docs do not describe content-based or
semantic search over journals or state, and the same two-store consequence follows.

**Use Restate instead** when you want durable execution with minimal ceremony, per-key
serialized state without running a lock service, and platform-level dedup — and your
recovery decision is "resume".

---

## DBOS — the closest thing to AXIOM's central idea, and it got there first

This is the section where the argument gets uncomfortable, so it goes early rather than
buried.

DBOS checkpoints workflow state into **your own Postgres**: "While your application runs,
DBOS checkpoints those workflows and steps to a Postgres database." And it draws exactly
the conclusion AXIOM draws:

> "When workflow metadata and application data live in the same Postgres database, they can
> be updated in the same database transaction. That means partial failures are no longer
> possible."

> "If the workflow engine is a separate system, it can drift out of sync with the database.
> In practice, resolving discrepancies requires additional infrastructure such as
> reconciliation jobs."

Their `@DBOS.transaction` decorator runs a step inside a Postgres transaction that commits
the application's write and DBOS's own checkpoint together, which is what lets them say
"exactly-once" for that class of step. **This is the same insight as AXIOM's fused
transaction, published by people who have been thinking about it longer.** Anyone
evaluating AXIOM should evaluate DBOS first.

Two things remain true after conceding that.

**The exactly-once scope is transactional steps.** It is atomicity between a *database
write* and its checkpoint. An HTTP call to Stripe is not a Postgres write and cannot join
that transaction; for it, DBOS is in the same position as Temporal and as AXIOM — an
at-least-once step that needs a durable key. The `@DBOS.step` tutorial says a workflow
"automatically resumes execution from the last completed step", which is the correct
behaviour and is also crash window W4 restated: if the call landed and the checkpoint did
not, the step is not "completed."

**Nothing in DBOS makes the memory half exist.** It is a durable execution library, not a
memory model. You are on Postgres, so you can add pgvector and build the rest — and you
should, if that is your stack. What AXIOM contributes on top is not the co-location idea;
it is what gets co-located:

- four memory classes separated **by authority** — episodic and semantic *advise*,
  procedural *authorizes*, execution *constrains* — so a recalled memory can push a
  decision toward escalation and structurally cannot push it toward acting
  (`tasks.recover()`, and the `Outcome` enum in `axiom/models.py` that keeps a memory's
  vote inside a vocabulary the state machine already understands);
- an **admissibility gate that is a vector-index prefix column**, so quarantining a
  poisoned memory moves the row to a different index partition *at commit* — no reindex,
  no cache to invalidate, no window in which it is still retrievable;
- **versioned, signable procedural memory** that decides whether the agent may act
  unattended at all, now on a risk axis that is not only money (`axiom/risk.py`,
  `db/004_risk.sql`);
- a written **crash-window spec** (`docs/CRASH_WINDOWS.md`, W1–W7) with a regression test
  per window.

**Use DBOS instead** when you are on Postgres, your steps are mostly database writes, and
you want a maintained library from people who will still be maintaining it next year. If
you want the co-location primitive rather than an opinion about agent memory, DBOS is the
better purchase and AXIOM is the wrong shape.

---

## LangGraph checkpointers

**What it genuinely solves.** LangGraph gives you durable execution for an agent graph
almost for free: "Checkpointers persist a thread's graph state as checkpoints" at every
super-step, which is what makes human-in-the-loop, time travel, and resume-after-crash
work. It also has real long-term memory: `BaseStore` supports semantic search with a
natural-language query, implemented in `InMemoryStore` and `PostgresStore`. On paper it has
both halves.

The docs are unusually clear that the two halves are two things:

> "Checkpointers persist a thread's graph state as checkpoints" — "short-term,
> thread-scoped memory"
>
> "Stores persist application-defined data outside the graph state" — "long-term,
> cross-thread memory"

and they are configured as two independent objects:

```python
graph = builder.compile(checkpointer=checkpointer, store=store)
```

Two interfaces, two backends (which may not even be the same database), two writes. The
public API has no operation that commits a checkpoint and a store write together. If you
point both at one Postgres you have made it *possible* in principle, but LangGraph does not
do it for you, and the semantics you get are whatever the two libraries do independently.

On side effects, LangGraph's durable-execution guide is explicit, and it is asking you to
solve the problem yourself:

> "wrap any non-deterministic operations (e.g., random number generation) and any
> operations with side effects (e.g., file writes, API calls) inside tasks or nodes"
>
> "ensure that side effects (e.g., API calls, file writes) are idempotent … if an operation
> is retried after a failure in the workflow, it will have the same effect as the first time"
>
> "the workflow's resumption will re-run the task, relying on recorded outcomes to maintain
> consistency"
>
> "Use idempotency keys or verify existing results to avoid unintended duplication"

That is correct advice and it is the whole of the guarantee: LangGraph ships no
idempotency key. Where the key comes from, and whether it survives a crash, is yours. The
probe's naive arm is exactly what happens when a key is generated at call time — two
refunds — and it is labelled as *no key*, not as *LangGraph*, because a team that follows
this guidance will not have that bug.

**Use LangGraph instead of AXIOM** for the part AXIOM has no opinion about: how the agent
plans, what the graph looks like, how tools are routed, how a human interrupts a run.
AXIOM is not a graph framework and is not trying to become one. The intended relationship
is composition, not replacement — `axiom/adapter.py` exposes a `@guard` decorator that puts
the five protocols around whatever function actually moves money, so it drops in behind an
existing agent loop rather than asking you to rewrite it.

---

## Letta / MemGPT, and the general "vector DB + framework memory" pattern

**What it genuinely solves.** Letta is the most serious attempt at agent memory as a
product: memory blocks in context, archival memory as "a semantically searchable database
where agents can store facts, knowledge, and information for long-term retrieval," queried
with hybrid semantic-plus-keyword retrieval fused by RRF. If the problem is *recall* — this
user prefers metric units, this account had a billing dispute in March — Letta is built for
it and AXIOM is not.

Letta is also durable in a way worth naming precisely. Background mode "decouples agent
execution from your client connection … allowing you to reconnect and resume from any point
— even if your application crashes or network fails," with `run_id`/`seq_id` tracking and a
`runs.active` discovery API for "application restarts: Resume processing after deployments
or crashes."

Read that carefully: **what resumes is the stream, not the effect.** The documentation on
long-running executions does not address whether a tool call that was in flight when the
process died is re-executed, deduplicated, or treated as idempotent. I looked for a
statement and did not find one; I am reporting silence, not a defect. But silence is the
answer to the question this repo is about — if there is no receipt, there is no way to
answer "did the money already move?" other than asking the provider and hoping the provider
can tell you.

The same holds for the general pattern of a framework plus a vector database. Pinecone
documents the property that makes it a *second system*:

> "Pinecone is eventually consistent, so there can be a slight delay before new or changed
> records are visible to queries."

They also ship the honest mitigation — compare `x-pinecone-request-lsn` from your write
against `x-pinecone-max-indexed-lsn` from your query — which is a read-your-writes protocol
you now implement by hand in the agent's recovery path, for one of your two stores, with no
way to extend it across both.

**Use Letta instead** when the memory you need is memory in the ordinary sense: personalized
recall over long horizons, self-editing context, memory that improves the conversation.
AXIOM's memory is deliberately narrow — it exists to constrain an irreversible act — and
using it for personalization would be using a receipt printer as a notebook.

---

## Doing nothing: a retry and an idempotency key

This is the real incumbent, it is what almost everyone actually ships, and it deserves the
most respect of anything on this page.

If **all** of these hold:

- the action is **one** external call, not a sequence;
- the idempotency key is a **pure function of immutable input** you already have durably
  (an order id, a message id, a row's primary key) — not a UUID, not a timestamp;
- the provider honours idempotency keys;
- your queue redelivers on failure;
- and the correct recovery is always **"retry"**

...then write the key derivation, use it, and go home. You do not need AXIOM, you do not
need Temporal, and a system that tells you otherwise is selling something. This is a real
answer, it is correct, and it costs about fifteen lines.

It stops being sufficient at the points where each assumption breaks:

| When this is true | The bare retry gives you |
| --- | --- |
| The key is minted at call time | A new key per attempt. The provider sees a new request. Two refunds — the probe's naive arm |
| There are several steps with a shared budget | A receipt with no budget debit, or a debit with no receipt. Nothing is all-or-nothing |
| Two workers can hold the same task | No fence. A GC-paused worker wakes up inside an HTTP call and settles over the new owner (crash window W5) |
| A restart re-synthesizes the request with an LLM | The same key on a different body. A real provider rejects it with 409; without a stored `request_fingerprint` you find out from the provider, in production (W7) |
| The right recovery depends on what happened before | Nowhere to put "what happened before" that the retry path can read transactionally |
| Someone must authorize the act | Nothing. There is no policy, no ceiling, no approval, no record of who allowed it |

Every row is a thing AXIOM has a mechanism and a test for, and every row is also a thing you
could build yourself. The claim is not that it is hard. The claim is that the last two rows
have nowhere to live in an architecture whose memory is in a different system from its
execution state.

---

## The experiment

`scripts/incumbent_probe.py` does not install Temporal, LangGraph, or Letta. It models the
one structural property they share and AXIOM does not — **the number of commit points** —
using two real durable stores with independent transactions, plus the same simulated payment
provider (`axiom/provider.py`, own database, own connection, Stripe idempotency semantics)
that every other script in this repo uses.

It reports what it does *not* model in its own docstring, at length. It is not a benchmark
and proves nothing about anyone's throughput or reliability.

Run it:

```bash
export DATABASE_URL='postgresql://root@localhost:26257/axiom?sslmode=disable'
export AXIOM_OFFLINE=1
./.venv/bin/python scripts/incumbent_probe.py
```

### Arms 1 and 2: the incumbent answer works

```
                          SPLIT, key at call time           SPLIT, key from durable id
killed after the refund   yes                               yes
key survives the crash    no — it lived in RAM              yes — recomputed from the store
provider verdict on retry created a second refund           replayed the original
REFUNDS CREATED           2                                 1
DOLLARS OUT               $300.00                           $150.00
```

A durable workflow store plus a deterministically derived key does not double-refund. That
is the end of the "Temporal plus an idempotency key" objection *as an objection about money*,
and it is settled in the incumbent's favour by running code before anything else is argued.

### Arm 3: the seam

Now the recovery has to *decide* rather than just resume. One operator makes one judgement —
"for this merchant, an unattended re-send at this state has produced a duplicate effect
before; revoke the memory that says otherwise" — and that single judgement has to land in
both stores. Four schedules, all legal (output below is reflowed to fit the page; one
`[…]` marks a trimmed line):

```
  S1  ops COMMIT A -> reader reads A,B -> ops COMMIT B
      decision: RESEND     CONTRADICTION
      the audit store already recorded the revocation; the decision cites the
      revoked memory as its evidence anyway

  S2  ops COMMIT B -> reader reads A,B -> ops COMMIT A
      decision: ESCALATE   CONTRADICTION
      the agent escalates a $150 refund on evidence the execution store has no
      record of; the audit trail cannot explain the decision

  S3  ops COMMIT B -> ops process dies -> A never written
      decision: ESCALATE   CONTRADICTION
      permanent divergence. Nothing repairs it; you write and operate a
      reconciliation job. […]

  S4  reader reads A -> ops COMMIT A and COMMIT B -> reader reads B
      decision: ESCALATE   CONTRADICTION
      each store was updated atomically and the reader still assembled a read set
      spanning two timestamps: A as of t0, B as of t2
```

**S4 is the one that matters.** S1–S3 are dual-write problems, and a determined team can
narrow them with an outbox. S4 assumes the writer is already perfect — each store updated
atomically — and the contradiction survives, because it is on the *read* side. Two reads of
two systems return two answers from two timestamps, and no amount of care on the write path
makes a pair of reads a snapshot.

Note that `--semantic-lag` defaults to **0**. None of this depends on assuming a vector
store is slow; Pinecone's documented eventual consistency only widens a window that is
already open at zero lag.

### Arm 4: the same race through `tasks.recover()`

```
  trial  schedule        saw revocation  cited revoked  adverse  decision
  1      recovery first  False           True           0        RESEND
  2      ops first       True            False          1        ESCALATE
  3      recovery first  False           True           0        RESEND
  4      ops first       True            False          1        ESCALATE

  D1  read sets where the journal shows the revocation AND the recall
      still returned the revoked memory  : 0
      orderings observed                 : 2 after the revocation, 2 before it
  D2  operator transaction aborted mid-way; memory left intact and
      still ACTIONABLE                   : True
```

Both orderings are exercised on purpose, and the script prints `INCONCLUSIVE` rather than
`PASS` if only one of them occurs — an arm 4 where the recovery always wins the race would
satisfy the predicate for the boring reason and prove nothing.

Every trial that ran *after* the revocation committed saw the journal entry **and** lost the
revoked memory from the same recall, and escalated. Every trial that ran *before* saw
neither, and re-sent. The middle state — journal says revoked, recall returns it anyway — is
S1, and it is unreachable.

**This is not a probabilistic result and should not be read as one.** The revocation, its
replacement memory, and the journal entry are one commit; a snapshot sees all of them or
none. The trials exist so the claim could have failed, not to establish it.

D2 is S3's counterpart: the operator transaction aborts halfway through. In a two-store
architecture that is permanent divergence plus a reconciliation job. Here it is a rollback,
and divergence is not repaired so much as unrepresentable.

---

## Where AXIOM loses

Consolidated, because a comparison whose losses are scattered through the prose is a
comparison hiding them.

| Use | Use this, not AXIOM | Why |
| --- | --- | --- |
| Long multi-step business processes | **Temporal / Restate** | Timers, signals, child workflows, versioning, schedules. AXIOM has a lease and a poll loop |
| Durable execution on Postgres | **DBOS** | Same co-location insight, mature library, and they published it first |
| Building the agent's reasoning graph | **LangGraph** | AXIOM has no planner and no opinion about one. Compose them via `axiom/adapter.py` |
| Personalized long-horizon recall | **Letta / MemGPT** | Purpose-built for memory-as-recall. AXIOM's memory is for constraining an act |
| Similarity search at scale | **Pinecone / pgvector / etc.** | AXIOM's recall is one C-SPANN index sized for a decision, not a corpus |
| One idempotent call with a natural key | **A retry loop** | Fifteen lines. Anything more is overhead you will regret |
| Anything where recovery is always "resume" | Any of the above | The fused transaction buys nothing if there is no judgement to make |

## What AXIOM has not earned

- **No users.** No production deployment, no adoption, no dollar saved outside this repo's
  own simulator. Every number on this page comes from code in this repo run against a
  database and a provider this repo also wrote.
- **The provider is simulated.** It implements Stripe's idempotency semantics faithfully in
  a separate database over a separate connection, but it is not Stripe and no real money has
  moved.
- **No CI**, single-region, one BASIC cluster. See the README's Limitations, which is longer
  than this list and deliberately so.
- **The systems above are modelled, not installed.** `scripts/incumbent_probe.py` models
  commit-point structure. A stronger version of this document would run the same race against
  an actual Temporal cluster with an actual Pinecone index. That work is not done, and this
  page should be discounted accordingly.
- **Operational maturity is not close.** Temporal has been run in anger by thousands of
  teams. This has been run in anger by one person, for a week, on a laptop and one free-tier
  cluster.

The argument here is structural, and a structural argument is the weakest kind of evidence
short of no evidence at all. It is what a project this age can offer honestly.

---

## Sources

Fetched while writing this, not recalled.

- Temporal — [Visibility](https://docs.temporal.io/visibility) ·
  [Search Attributes](https://docs.temporal.io/search-attribute) ·
  [Event History](https://docs.temporal.io/workflow-execution/event) ·
  [Activity Definition](https://docs.temporal.io/activity-definition) ·
  [Persistence](https://docs.temporal.io/temporal-service/persistence) ·
  [Data encryption](https://docs.temporal.io/production-deployment/data-encryption) ·
  [What is idempotency?](https://temporal.io/blog/idempotency-and-durable-execution)
- Restate — [Key concepts](https://docs.restate.dev/foundations/key-concepts)
- DBOS — [Architecture](https://docs.dbos.dev/architecture) ·
  [Steps](https://docs.dbos.dev/python/tutorials/step-tutorial) ·
  [Transactions & Datasources](https://docs.dbos.dev/typescript/tutorials/transaction-tutorial) ·
  [The Case for Co-Locating Workflow State with Your Data](https://www.dbos.dev/blog/co-locating-workflow-state-with-your-data)
- LangGraph — [Persistence](https://docs.langchain.com/oss/python/langgraph/persistence) ·
  [Durable execution](https://docs.langchain.com/oss/python/langgraph/durable-execution) ·
  [Semantic search for LangGraph memory](https://blog.langchain.com/semantic-search-for-langgraph-memory/)
- Letta — [Overview](https://docs.letta.com/concepts/letta) ·
  [Archival search](https://docs.letta.com/guides/agents/archival-search/) ·
  [Long-running executions](https://docs.letta.com/guides/agents/long-running)
- Pinecone — [Check data freshness](https://docs.pinecone.io/guides/index-data/check-data-freshness)

In this repo: [`scripts/incumbent_probe.py`](../scripts/incumbent_probe.py) (the experiment) ·
[`scripts/counterexample.py`](../scripts/counterexample.py) (transcript memory, same crash) ·
[`docs/CRASH_WINDOWS.md`](CRASH_WINDOWS.md) (W1–W7) ·
[`axiom/tasks.py`](../axiom/tasks.py) (`recover()` is the fused transaction) ·
[`axiom/adapter.py`](../axiom/adapter.py) (the `@guard` decorator, for dropping in behind an
agent loop you already have).
