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

Measured 2026-08-13 against the live deployment.

```
1  charge created            ch_3U4A9yAwRnm0fQgO0yMnQJJz
2  policy stopped it, a human approved
3  receipt committed         axm_bb4799e8afaed374341e70936556850c6d221e63b52d599f
4  refund sent to Stripe, worker A KILLED before recording it
5  worker B recovered        fence e2 -> e3 · RESEND
6  re-sent under the SAME key → re_3U4A9yAwRnm0fQgO0kOsC6Id   REPLAYED by Stripe

   refunds for this order 1 · Stripe reported a replay: true · duplicates 0
```

Verified independently, by querying Stripe directly rather than through AXIOM:

```
$ curl https://api.stripe.com/v1/refunds/re_3U4A9yAwRnm0fQgO0kOsC6Id -u sk_test_…:
  re_3U4A9yAwRnm0fQgO0kOsC6Id · succeeded · $300.00 · charge ch_3U4A9yAwRnm0fQgO0yMnQJJz
  metadata: axiom_idempotency_key, axiom_order_ref, axiom_request_fingerprint

$ curl 'https://api.stripe.com/v1/refunds?charge=ch_3U4A9yAwRnm0fQgO0yMnQJJz' -u sk_test_…:
  count: 1
```

Every refund carries `metadata[axiom_order_ref]` and `metadata[axiom_idempotency_key]`, so
any row in Stripe's dashboard can be traced back to the AXIOM receipt that authorized it.

## Checking it without a Stripe account

The `dashboard.stripe.com/test/payments/ch_…` link the proof also returns is only useful to
whoever owns the sandbox. To everyone else it is a login screen, which makes it worthless as
evidence for exactly the reader who has the least reason to take our word for anything.

Stripe hosts a second page per charge that needs no login — its own receipt, rendered by
Stripe, on a `stripe.com` origin, showing the refund. Every run now returns it as
`receipt_url`, and the deployment redirects to the recorded one:

```
https://axiom-one-sage.vercel.app/stripe-receipt
```

That is a 302 to Stripe. AXIOM does not proxy or re-render the page, deliberately: a page
this deployment fetched and served back would be a page this deployment could have written.

Receipt **#3048-6646**, `$300.00`, refunded.

## Reproducing the replay from a plain terminal

Nothing in the paragraph above requires AXIOM to be running, and that is the point worth
testing. Anyone holding the same test key can send the refund again from any machine, with
no AXIOM process involved, and the only thing carried over is the idempotency key AXIOM had
already committed to CockroachDB **before** the crash:

```
$ curl https://api.stripe.com/v1/refunds -u sk_test_...: \
    -H 'Idempotency-Key: axm_bb4799e8afaed374341e70936556850c6d221e63b52d599f' \
    -d charge=ch_3U4A9yAwRnm0fQgO0yMnQJJz -d amount=30000 \
    -d 'metadata[axiom_order_ref]=AXM-PROOF-4030f815' ...

HTTP/2 200
idempotency-key: axm_bb4799e8afaed374341e70936556850c6d221e63b52d599f
idempotent-replayed: true
original-request: req_8j6Q6lmQ5Y3ccx
request-id: req_6YcdVgQok3Aw3R
stripe-version: 2026-07-29.dahlia

-> re_3U4A9yAwRnm0fQgO0kOsC6Id  succeeded  $300.00     refunds on that charge: 1
```

Two request ids, one refund. `request-id` is this call; `original-request` is Stripe
pointing back at the call made *before* the crash, and saying that this one did not do
anything new. That header is Stripe's answer, not AXIOM's — the ledger still reads one
refund on the charge.

The scope of what this shows is narrow and worth stating exactly:

> The key AXIOM committed to CockroachDB before the crash is sufficient, **on its own,
> from any machine**, to make Stripe return the original refund instead of creating a
> second one.

It does not show that Stripe's idempotency is impressive — Stripe's idempotency is
table stakes, and the section below concedes it. It shows that the key is the entire
input to that mechanism, and that keeping it across a crash is a storage problem the
provider cannot solve for you.

Two caveats on the command itself. It is a **replay**, so it is not idempotent in the
colloquial sense of "safe to run whenever": run it more than 24 hours after the original,
once Stripe has expired the key, and it creates a *second* refund rather than replaying the
first. And it only replays if every parameter matches the original byte for byte — a
changed amount returns `400`, which is the W7 case in the table below.

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
- **The public receipt link is a bearer URL.** Anyone holding it can open the page, which
  is the property that makes it useful here and would be the wrong default for a live
  charge. Stripe also mints a fresh token each time the charge is retrieved, so the
  recorded link is one of several valid ones rather than the canonical one; it was checked
  to still return `200` unauthenticated on 2026-08-13.
- **`original-request` is shown for the recorded run only.** `create_refund` does not yet
  keep Stripe's `request-id` and `original-request` response headers, so a live run in the
  UI reports the replay flag without the two ids behind it. The panel omits the row rather
  than filling it in from the recorded run.
