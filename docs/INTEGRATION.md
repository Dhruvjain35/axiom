# You already have an agent. Here is the diff.

AXIOM is normally a system you adopt: its worker claims from its queue and calls its
provider. That is the wrong shape for most people, because most people already have an
agent. This document is the other shape — one decorator, on the one function that does
something you cannot take back.

Everything below is implemented in [`axiom/adapter.py`](../axiom/adapter.py), exercised
by [`tests/test_adapter.py`](../tests/test_adapter.py) (24 tests), and demonstrated by
three runnable examples in [`examples/`](../examples/).

---

## The diff

Here is a tool as a team would actually have written it. It is fine. It is also one
container eviction away from refunding a customer twice.

```python
def issue_refund(order_id: str, amount_cents: int) -> dict:
    r = stripe.Refund.create(charge=order_id, amount=amount_cents)
    db.execute("UPDATE orders SET refunded = true WHERE id = %s", [order_id])
    return {"id": r.id}
```

The guarded version:

```python
from axiom.adapter import guard

@guard(action="refund", key="order_id", amount="amount_cents",
       provider="stripe", operation="refunds.create")
def issue_refund(order_id: str, amount_cents: int, idempotency_key: str) -> dict:
    r = stripe.Refund.create(charge=order_id, amount=amount_cents,
                             idempotency_key=idempotency_key)
    db.execute("UPDATE orders SET refunded = true WHERE id = %s", [order_id])
    return {"id": r.id}
```

Three lines changed. `issue_refund(order_id="ord_1", amount_cents=2999)` still returns a
dict, still has the same name, still reads like a function. Once, at startup:

```python
from axiom.adapter import bind
bind(tenant_id=TENANT, mission_id=RUN, policy_id="refund_authority")
```

That is the whole integration.

---

## What actually happens when you call it

```
  your call ──▶ derive identity from the arguments      (nothing written yet)
             ──▶ CLAIM    one durable row for this act, fence bumped        1 txn
             ──▶ PREPARE  receipt committed, budget debited, policy consulted  1 txn
             ──▶ DISPATCH your function body                                NO txn
             ──▶ SETTLE   outcome + the memory of it, together              1 txn
```

Crash anywhere in that column and the next call to `issue_refund` with the same arguments
enters at RECOVER instead of PREPARE: it reads the receipt, recalls what comparable
recoveries did, and decides — in one commit — whether to re-send under the same key or
stop and ask a human.

The ordering is the guarantee, not a hope about timing. The receipt commits *before* the
call goes out, so a crash before the receipt provably caused no effect, and a crash after
it leaves evidence that one might exist.

---

## The one thing you can get wrong: `key=`

The idempotency key AXIOM sends is a database-generated column —
`sha256(tenant_id, task_id, step_name, step_seq)` — so no code path can mint one at call
time. But `task_id` is only stable across a restart because the task is unique on
`(tenant, dedupe_key)`, and in an adapter the dedupe key comes from **your arguments**.

So `key=` names parameters. It does not compute anything, and it is checked twice:

| when | what is checked | what happens |
|---|---|---|
| decoration (import) | every name in `key=` is a real parameter | `UnstableKey` — your process will not start |
| call | every key value is `str` / `int` / `bool` / `UUID`, non-empty | `UnstableKey` **before any row is written** |

`None`, `""`, floats, dicts, lists and objects are refused with a message that says why.
A float is refused even though `repr()` is stable, because a float in an identity key is
almost always money (use integer cents) or a clock (which is the bug).

Pick the thing the act is *about*: an order id, an invoice id, a workspace id, the
`(campaign_id, segment_id)` pair. Never a request id, a message id, a retry counter, a
`uuid4()`, or a timestamp — those are all different on the retry, which is precisely when
you need them to be the same.

You can print what a call will resolve to without doing anything:

```python
>>> issue_refund.axiom_key(order_id="ord_1", amount_cents=2999)
'refund|order_id=ord_1'
```

That one line belongs in the code review of every guarded function.

---

## What you get

**Effectively-once external effects.** Not exactly-once — see the honesty section. Your
function may run twice; the *effect* is deduped by the provider, because both runs carry
the same key.

**A completed act answers itself.** Call it again after it succeeded and you get the
recorded return value without touching the provider. This is the boring case that happens
constantly: retries, duplicate webhooks, a user double-clicking.

```python
call = issue_refund.axiom(order_id="ord_1", amount_cents=2999)
call.value             # what your function returned
call.already_settled   # True => answered from the durable record, nothing was called
call.recovered         # True => a previous attempt may have already acted
call.idempotency_key
```

**A policy decides whether the machine may act alone.** `axiom_policy` is versioned,
content-hashed and signable, and the version is pinned for the whole attempt. Over the
ceiling, you get `ApprovalRequired` with an `approval_id`; nothing was sent and no receipt
exists. After a human approves, calling the function again completes the same act — and
the approval token is consumed, so it authorizes exactly one action.

**A hard spend ceiling.** `PREPARE` debits `axiom_mission.spent_cents` in the same
transaction that mints the key, under a `CHECK` constraint. Your agent's blast radius in
dollars is a number you wrote down, not a hope.

**An audit trail you did not write.** Every transition appends to a gap-free per-subject
journal, in the same transaction as the transition.

**Memory of the act, co-committed.** `SETTLE` writes the outcome and an embedded episodic
memory in one transaction, so memory can never disagree with execution state. The next
recovery of a similar situation recalls it and can vote — in one direction only, toward
escalation.

**A reconciliation worklist.** `tasks.unsettled_receipts()` is the honest answer to "what
is this system currently unsure about?", which is the question you actually want during an
incident.

---

## What you must give up

1. **Your tool's return value must be JSON-serializable**, or you lose it on the replay
   path. The guard never fails the settle over this — an unserializable value is stored as
   `{"unserializable": true, "repr": ...}` — but `already_settled` calls can then only
   give you that. Return a dict.

2. **Arguments must be bindable to a real signature.** `*args`/`**kwargs`-only tools
   cannot name a key.

3. **The tool must be synchronous.** `db.tx()` is psycopg and psycopg blocks, so
   decorating an `async def` is refused at decoration time. Wrap it:
   `await asyncio.to_thread(issue_refund, order_id=..., amount_cents=...)`.

4. **Three extra transactions per guarded call** (claim, prepare, settle), plus one point
   read. They are small and indexed, and two of them are on the row you already own — but
   this is not free, and it is not for a tool you call in a loop a thousand times a
   second. It is for the call that moves money.

5. **A tenant, a mission and a policy must exist.** `bind()` will not invent them, and
   `policy_id` has no default — inheriting the engine's `refund_authority` would quietly
   make every integration a refund integration.

6. **Changing the amount under an existing key is a hard error** (`IntentChanged`), not a
   new call. If that is what you meant, your key does not distinguish the two acts.

---

## What AXIOM does NOT do for you

**It does not stop your function from running twice.** It cannot — your function is where
the network call lives. What it guarantees is that the second run carries the *same*
idempotency key, and that afterwards you can prove which run caused the effect. Everything
downstream of that rests on the next point.

**It cannot make a non-idempotent provider idempotent.** Against an API that ignores
idempotency keys, AXIOM narrows the guarantee to "we know exactly what we sent, when, and
under which authority, and we will not mint a second key for the same act". That is real
and it is worth having. It is not the same promise, and the code says so rather than
letting you assume otherwise. If your tool never uses the key, the guard emits a
`KeyUnusedWarning` naming the function — that warning is the bug, not the noise.

**It does not roll anything back.** There is no compensating transaction here. A refund
that happened, happened; AXIOM records it and refuses to do it again.

**It does not heartbeat.** A guarded call that runs longer than `AXIOM_LEASE_SECONDS`
(default 20) can have its task claimed by another worker. Your settle is then refused with
`LeaseLost` — the fence working correctly — and the receipt stays live for the new owner
to recover. Nothing is lost and no effect can double, but if your provider calls take
minutes, raise the lease.

**It does not make your LLM sensible.** The model proposes; the policy and the receipt
dispose. A model that asks for the same refund twice gets one refund and no error. A model
that asks for a $4,000,000 refund gets an approval request. Neither is intelligence.

**It is not a workflow engine.** No DAGs, no retries-with-backoff-policies, no schedules,
no fan-out. One guarded call is one irreversible act. If you need orchestration, keep the
orchestrator and put AXIOM under the step that touches the world — which is exactly what
`examples/02_langgraph_tool.py` does.

---

## Errors, and what to do about each

| exception | meaning | the right response |
|---|---|---|
| `UnstableKey` | the identity would not survive a restart | fix the call site; nothing was written |
| `ApprovalRequired` | policy will not authorize this unattended | route `.approval_id` to a human; call again after |
| `ActionInFlight` | another holder has the fence right now | back off and retry, or leave it to them |
| `ActionRefused` | dead-lettered, cancelled, or escalated by recovery | a human looks; `.reason` says why |
| `IntentChanged` | same key, different amount | your key does not distinguish these acts |
| `BudgetExceeded` | the mission's hard ceiling refused | raise the budget, then call again |
| `NotBound` | no tenant/mission/policy on this context | `bind()` at startup |

Anything your tool raises propagates unchanged. AXIOM's only involvement is that it hands
the act back for recovery **without touching the receipt** — because an exception cannot
tell you whether the effect landed, and discarding the receipt there is exactly how you
get the second refund.

---

## Acts that are not denominated in money

Deleting records, sending 40,000 emails, revoking access: dollars are the wrong axis and
`amount_cents` is `NULL`. `risk=` is how the guard describes those, and it takes three
forms, strongest first.

**A `risk.Risk` descriptor.** The guard decides nothing — it hands the descriptor to
`Policy.decide()`, the same general authority model
([`axiom/risk.py`](../axiom/risk.py), `db/004_risk.sql`) that `tasks.prepare()` uses. An
**ungoverned unit is a refusal**, not a default.

**A callable**, because magnitude usually depends on the arguments. It receives the bound
arguments and nothing else, so the measurement is reproducible from the audit trail
instead of being something the agent asserted about itself:

```python
from axiom.risk import Reversibility, Risk

@guard(action="purge_records", key="workspace_id",
       risk=lambda a: Risk.of("data.subjects", a["record_count"],
                              reversibility=Reversibility.IRREVERSIBLE))
def purge(workspace_id: str, record_count: int, idempotency_key: str) -> dict:
    ...
```

With a policy granting 100 `data.subjects` at IRREVERSIBLE, `record_count=50` proceeds
unattended and `record_count=40_000` raises `ApprovalRequired` with a reason naming the
axis that failed. Not one cent moved in either case.

**A plain label**, for an act nobody has written a measurement for yet. It is matched
against a vocabulary in the policy *body* — versioned, content-hashed, signable, so still
procedural memory:

```json
{"escalate_risks": ["data_deletion"]}   // deny-list: these need a human
{"auto_risks": ["money_movement"]}      // allow-list: anything unnamed needs one
```

Stated plainly, because the difference matters: a `Risk` descriptor is decided by the
engine and is deny-by-default; a label is checked by Python in `axiom/adapter.py` and a
policy declaring neither list authorizes every label. Use `auto_risks` if you want the
closed form without writing a measurement.

---

## The examples

All three run against a local CockroachDB with no credentials
(`AXIOM_OFFLINE=1` swaps Bedrock for a deterministic local embedder).

| file | what it argues |
|---|---|
| `examples/01_tool_calling_agent.py` | a model-proposes-we-execute loop; the process is `os._exit(9)`-killed after the money moves and restarted |
| `examples/02_langgraph_tool.py` | LangGraph checkpoints *state*, on node completion — die inside the tool node and the resume re-runs it. AXIOM makes the re-run a replay. Needs `examples/requirements.txt` |
| `examples/03_fastapi_webhook.py` | a real uvicorn server killed mid-request; the sender redelivers and gets 200 with the original refund id |

Real output from `examples/01_tool_calling_agent.py`:

```
== run 1: this process will be killed mid-refund (ORD-F25EC37D) ==
   model -> issue_refund({'order_id': 'ORD-F25EC37D', 'amount_cents': 2999})
   !! the money moved (re_ce053f17217f49bca5a0) and the process dies HERE
   process exited 9 — receipt committed, outcome never recorded

== run 2: an ordinary restart, once the dead lease lapses ==
   model -> issue_refund({'order_id': 'ORD-F25EC37D', 'amount_cents': 2999})
   tool  -> {'id': 're_ce053f17217f49bca5a0', 'amount_cents': 2999, 'replayed': True}
   model: Refunded ORD-F25EC37D. Told the customer.

== the provider ledger, which AXIOM cannot edit ==
   re_ce053f17217f49bca5a0  2999c  replays=1  key=axm_3b3df4e2bf5552f4d146...
   orders refunded more than once: NONE
```

Same refund id, one row, one replay. The second process never saw the first one's memory,
its variables, or its uuid — only the order number.
