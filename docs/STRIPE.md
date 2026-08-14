# Stripe: what is real, and what AXIOM actually adds

Every other demonstration in this repository proves the guarantee against a payment
provider that this repository also wrote. That is a fair way to build the thing and an
unconvincing way to finish arguing about it — a reader is entitled to suspect the
simulator was written to agree with the system.

So the same crash runs against Stripe, and then **Stripe is asked what happened**, rather
than AXIOM being asked.

```
POST /api/proof/stripe        on the live demo
./.venv/bin/python scripts/stripe_proof.py     locally
```

## A real run

```
1  charge created            ch_3U491RAwRnm0fQgO0lgLyyP7
2  policy stopped it, a human approved
3  receipt committed         axm_a04c69c00fab80196b9fabe0d9cfcb33e13c97e4c46e31f4
4  refund sent to Stripe, worker A KILLED before recording it
5  worker B recovered        fence e2 -> e3 · RESEND
6  re-sent under the SAME key → re_3U491RAwRnm0fQgO0HX6b1w5   REPLAYED by Stripe

   refunds for this order 1 · Stripe reported a replay: true · duplicates 0
```

Verified independently, by querying Stripe directly rather than through AXIOM:

```
$ curl https://api.stripe.com/v1/refunds/re_3U491RAwRnm0fQgO0HX6b1w5 -u sk_test_…:
  re_3U491RAwRnm0fQgO0HX6b1w5 · succeeded · $300.00 · charge ch_3U491RAwRnm0fQgO0lgLyyP7
  metadata: axiom_idempotency_key, axiom_order_ref, axiom_request_fingerprint

$ curl 'https://api.stripe.com/v1/refunds?charge=ch_3U491RAwRnm0fQgO0lgLyyP7' -u sk_test_…:
  count: 1
```

Every refund carries `metadata[axiom_order_ref]` and `metadata[axiom_idempotency_key]`, so
any row in Stripe's dashboard can be traced back to the AXIOM receipt that authorized it.
The demo prints a `dashboard.stripe.com/test/payments/ch_…` link for exactly that.

## Test mode, and why that is not a dodge

This is a Stripe **sandbox**. `livemode: false`. No real money moves, and
`axiom/stripe_provider.py` refuses any key not prefixed `sk_test_` — the module issues
refunds, so pointing it at live money is prevented by construction rather than by
remembering.

What test mode does **not** change is the thing under examination. Idempotency-key
handling is the same code path Stripe runs in production; it is not a sandbox emulation.
The behaviour below was probed live against the sandbox *before* a line of the integration
was written.

## Stripe already prevents double charges. So what is AXIOM for?

This is the fair question, and a judge should ask it.

Probed directly:

| request | result |
| --- | --- |
| first call, key `K` | `re_…` created |
| same key `K`, same parameters | **the same `re_…` returned**, response header `idempotent-replayed: true` |
| same key `K`, **different** parameters | **400** — *"Keys for idempotent requests can only be used with the same parameters they were first used with"* |

Those are, line for line, the three cases in `db/003_provider.sql` — including the third,
which is AXIOM's crash-window **W7** defence: a recovered agent that re-synthesizes a
subtly different request is a *new intent wearing an old key*. Stripe enforces it too.

That correspondence is worth stating plainly rather than hiding: **AXIOM did not invent
this model, it mirrors what a real provider actually does.** Pointing at the real one
proves that rather than asserting it.

So the honest scope of AXIOM's contribution is one sentence:

> **Stripe can only honour a key it is handed, and the key has to survive the crash.**

An agent that regenerates its idempotency key on restart gets a second refund from a
provider that was willing to prevent one. The key is the whole guarantee, and nothing in
Stripe can help you keep it — it is your side of the contract.

AXIOM keeps it by making the key a **generated column** in the database, derived from
`(tenant_id, task_id, step_name, step_seq)` — all immutable — and committing it *before*
the call goes out. There is no code path that can mint a key at call time, because there
is no code path that mints keys at all. After any crash, the recovering worker reads the
same key off the same durable receipt and hands Stripe exactly what it was handed before.

Stripe supplies the enforcement. AXIOM supplies the memory.

## Running it yourself

```bash
export AXIOM_STRIPE_KEY=sk_test_…          # dashboard.stripe.com/test/apikeys
export DATABASE_URL='postgresql://root@localhost:26257/axiom?sslmode=disable'
export AXIOM_OFFLINE=1
./.venv/bin/python scripts/stripe_proof.py
```

It exits non-zero and prints **INCONCLUSIVE** rather than PASS if the crash did not fire,
if Stripe did not report a replay, or if more than one refund exists — a run that did not
demonstrate its claim must not be allowed to look like one that did.

Each run works in a tenant of its own and deletes it afterwards, so neither the script nor
the endpoint leaves scratch missions in the tenant Mission Control is showing. The refund
itself stays in the sandbox on purpose: it is the evidence.

## Limitations, stated

- **Test mode.** Real API, real idempotency semantics, sandbox money.
- **Stripe's idempotency keys expire after 24 hours.** Far longer than any recovery window
  here, but a replay attempted a day later would create a second refund. AXIOM's receipt is
  durable indefinitely; Stripe's memory of the key is not.
- **The live endpoint is rate-limited** and creates a real charge and refund per run, so a
  judge pressing it repeatedly gets a `429` with an honest message rather than a queue of
  charges.
- **If Stripe is unreachable**, the panel shows a recorded past run, labelled as recorded.
  It never fabricates a live result.
