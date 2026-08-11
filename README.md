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

## The idea

> Memory is not saved chat history. Memory is what makes autonomous **action** safe.

Agent memory is usually treated as recall — remember the user's name, remember the last
ten turns. That framing is why agents are unsafe to automate with. The memory that actually
matters in production is the memory of **what the agent has already done**, and it has to be
durable across the crash, correct under concurrency, and auditable afterward.

AXIOM models four classes of memory:

| Class | Question it answers |
| --- | --- |
| **Episodic** | What happened the last time we saw this? |
| **Semantic** | What past situations resemble this one? |
| **Procedural** | What policy applies here, and which version of it? |
| **Execution** | What has this agent already *done* — irreversibly, in the real world? |

The first three advise. The fourth constrains. Vector memory tells the agent what it
*could* do; transactional execution state decides what it *may* do.

## Why this needs CockroachDB

Because execution state and semantic memory commit in a **single serializable transaction**.

When a worker recovers a task orphaned by a dead peer, it does two things at once: it reads
the durable receipt of what the dead worker had already done, and it semantically retrieves
what happened the *last* time an agent died at this exact point in this exact kind of
operation. Both, then the state transition, in one commit.

Split that across two systems — a workflow engine plus a vector database — and you have a
window where the agent resumes on memory that has already been superseded, with no
transaction to close it. Durable execution engines store history that is opaque and not
semantically queryable. Vector databases have no transactions to join. One store, one commit,
or you are racing.

## Architecture

```
                      User
                        │
                        ▼
                 Mission Control
                        │
                        ▼
               API / Orchestrator
                        │
              ┌─────────┴─────────┐
              ▼                   ▼
        Amazon Bedrock        ECS Fargate
        planner + embeddings  worker agents
              │                   │
              └─────────┬─────────┘
                        ▼
                ┌──────────────┐
                │ CockroachDB  │
                │              │
                │ tasks        │  execution memory
                │ event log    │  append-only journal
                │ policies     │  procedural memory
                │ memories     │  episodic + semantic (C-SPANN)
                └──────┬───────┘
                       │  scoped, read-only service account
                  Managed MCP
                       │
                       ▼
                  Audit Agent
```

## What it does not claim

AXIOM does not provide exactly-once execution of external side effects. That guarantee is
not available to any system that calls a network API it does not control. AXIOM provides
durable, idempotent, effectively-once execution: every external action is issued under a
derived idempotency key against a durable receipt, and every crash window has a defined and
tested outcome.

## Status

Built for the [CockroachDB × AWS Hackathon — Build with Agentic Memory](https://cockroachdb-ai.devpost.com/).
Under active construction.

## License

Apache-2.0
