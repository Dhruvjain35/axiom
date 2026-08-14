"""AXIOM :: Stripe, for real.

`axiom/provider.py` is a stand-in with Stripe's semantics. This is Stripe.

Why it matters more than it looks
---------------------------------
The obvious value is that the demo stops being a simulator arguing with itself. The
deeper one is that AXIOM did not invent its model — it mirrors what a real payment
provider actually enforces, and pointing at the real one proves that rather than asserting
it. Probed live against a Stripe sandbox before this file was written:

    first call, key K                 -> re_3U3oFc... created
    same key K, same parameters       -> re_3U3oFc... AGAIN, and the response carries
                                         the header `idempotent-replayed: true`
    same key K, DIFFERENT parameters  -> 400, "Keys for idempotent requests can only be
                                         used with the same parameters they were first
                                         used with"

Those are, line for line, the three cases in db/003_provider.sql — including the third,
which is AXIOM's crash-window W7 defence (a recovered agent that re-synthesizes a subtly
different request is a NEW INTENT wearing an OLD key). Stripe enforces it too.

So what does AXIOM add, if Stripe already does idempotency? Exactly one thing, and it is
the whole project: **Stripe can only honour a key it is given, and the key has to survive
the crash.** An agent that regenerates its key on restart gets a second refund from a
provider that was willing to prevent one. AXIOM derives the key in the database from
immutable columns, commits it BEFORE the call, and hands the same key back after any
crash. Stripe supplies the enforcement; AXIOM supplies the memory.

Idempotency keys expire at Stripe after 24 hours, which is far longer than any recovery
window here and is noted so nobody is surprised by a much later replay creating a second
refund.

Set AXIOM_STRIPE_KEY (a `sk_test_...` secret key). Never a live key: this module issues
refunds, and there is no reason to point it at real money.
"""

from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import settings
from .provider import ProviderCrash, ProviderError, ProviderResult, fingerprint

API = 'https://api.stripe.com/v1'


class StripeNotConfigured(RuntimeError):
    """AXIOM_STRIPE_KEY is unset. Callers fall back to the simulated provider."""


def _key() -> str:
    k = os.environ.get('AXIOM_STRIPE_KEY', '')
    if not k:
        raise StripeNotConfigured(
            'set AXIOM_STRIPE_KEY to a Stripe TEST secret key (sk_test_...)')
    if not k.startswith('sk_test_'):
        # A guard, not a nicety. This module's whole job is to issue refunds; pointed at a
        # live key it would issue real ones. Refusing anything but a test key makes that
        # impossible by construction rather than by remembering.
        raise StripeNotConfigured(
            'refusing a non-test Stripe key — this module issues refunds, so it accepts '
            'sk_test_ only')
    return k


def _call(method: str, path: str, params: dict[str, Any] | None = None,
          idempotency_key: str | None = None) -> tuple[dict, dict[str, str]]:
    """One Stripe call. Returns (body, headers).

    The headers are returned because that is where Stripe reports a replay:
    `idempotent-replayed: true`. It is the single most useful fact in the whole
    integration — the provider itself, not AXIOM, confirming it did not act twice — and
    it is available nowhere in the response body.
    """
    data = urllib.parse.urlencode(params or {}, doseq=True).encode()
    req = urllib.request.Request(
        f'{API}{path}', data=data if method == 'POST' else None, method=method)
    req.add_header('Authorization', f'Bearer {_key()}')
    req.add_header('Content-Type', 'application/x-www-form-urlencoded')
    if idempotency_key:
        req.add_header('Idempotency-Key', idempotency_key)

    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return json.loads(r.read()), {k.lower(): v for k, v in r.headers.items()}
    except urllib.error.HTTPError as e:
        body = json.loads(e.read() or b'{}')
        err = body.get('error', {})
        msg = err.get('message', str(e))
        # Stripe says whether a retry could possibly help. Trusting its judgement beats
        # inferring retryability from a status code: `stripe-should-retry` accounts for
        # things a 4xx/5xx split does not, and a wrong guess here either loses money or
        # duplicates it.
        should_retry = (e.headers or {}).get('stripe-should-retry', '').lower() == 'true'
        raise ProviderError(msg, status=e.code, retryable=should_retry or e.code >= 500)


def create_refund(*, idempotency_key: str, order_ref: str, amount_cents: int,
                  currency: str = 'usd', request_body: dict[str, Any] | None = None,
                  charge_id: str | None = None,
                  chaos_pre: float | None = None, chaos_post: float | None = None,
                  latency_ms: int | None = None) -> ProviderResult:
    """Issue a REAL refund against a Stripe test charge. Signature-compatible with
    provider.create_refund, so the engine cannot tell which one it is talking to.

    `charge_id` is Stripe's — AXIOM's own `order_ref` means nothing to Stripe, so it is
    attached as metadata and the charge is what actually gets refunded.
    """
    body = request_body or {}
    charge = charge_id or body.get('charge_id')
    if not charge:
        raise ProviderError('no Stripe charge to refund against', status=400,
                            retryable=False)

    # Crash window W2: the receipt is durable, nothing has been sent. Raised BEFORE the
    # request so no money can have moved.
    pre = settings.chaos_crash_before_dispatch if chaos_pre is None else chaos_pre
    if pre and random.random() < pre:
        raise ProviderCrash('CHAOS: died after PREPARE, before dispatch (W2)')

    if latency_ms:
        time.sleep(latency_ms / 1000.0)

    params = {
        'charge': charge,
        'amount': int(amount_cents),
        'metadata[axiom_order_ref]': order_ref,
        'metadata[axiom_idempotency_key]': idempotency_key,
        'metadata[axiom_request_fingerprint]': fingerprint(body) if body else '',
    }
    refund, headers = _call('POST', '/refunds', params, idempotency_key=idempotency_key)

    replayed = headers.get('idempotent-replayed', '').lower() == 'true'

    # Crash window W4: the refund is REAL and AXIOM has not recorded it. This is the
    # instant the whole project is about, and against Stripe it is no longer a
    # simulation — the money has genuinely moved in the sandbox.
    post = settings.chaos_crash_after_dispatch if chaos_post is None else chaos_post
    if post and random.random() < post:
        raise ProviderCrash('CHAOS: died after the refund landed, before settle (W4)')

    return ProviderResult(
        provider_ref=refund['id'],
        status=200 if replayed else 201,
        body={'id': refund['id'], 'amount_cents': refund['amount'],
              'currency': refund['currency'], 'status': refund['status'],
              'charge': refund.get('charge'), 'order_ref': order_ref,
              'idempotent_replay': replayed,
              'stripe_request_replayed_header': replayed},
        replayed=replayed,
    )


# --------------------------------------------------------------- charges to refund

def create_test_charge(amount_cents: int, order_ref: str) -> str:
    """A succeeded test charge, so there is something to refund.

    Stripe will not refund what was never paid, so the demo has to create the payment
    first. `pm_card_visa` is Stripe's documented always-succeeds test payment method;
    redirects are disabled because nothing here can complete a browser flow.
    """
    pi, _ = _call('POST', '/payment_intents', {
        'amount': int(amount_cents),
        'currency': 'usd',
        'payment_method': 'pm_card_visa',
        'confirm': 'true',
        'automatic_payment_methods[enabled]': 'true',
        'automatic_payment_methods[allow_redirects]': 'never',
        'metadata[axiom_order_ref]': order_ref,
    })
    charge = pi.get('latest_charge')
    if not charge:
        raise ProviderError(f'payment intent {pi.get("id")} produced no charge',
                            status=502, retryable=False)
    return charge


def receipt_url(charge_id: str) -> str | None:
    """Stripe's own PUBLIC receipt page for a charge — no Stripe login required.

    This exists because the obvious link is the wrong one. `dashboard.stripe.com/test/
    payments/ch_…` is useful to whoever owns the sandbox and is a dead end for everybody
    else: a stranger following it gets a login screen, not evidence. Stripe also hosts a
    tokenised receipt per charge, rendered by Stripe, showing the refund — and it opens
    for anyone holding the link. For a reviewer with no account, that is the difference
    between checking the claim and taking it on faith.

    Returns None rather than raising when the field is absent. `receipt_url` is null until
    the charge settles, so a freshly created charge legitimately has no receipt yet, and
    that is a "not yet", not a failure. Transport and API errors still raise ProviderError
    out of `_call` like every other call in this module.
    """
    charge, _ = _call('GET', f'/charges/{urllib.parse.quote(charge_id, safe="")}')
    return charge.get('receipt_url') or None


# ------------------------------------------------------------------------- audit

def ledger(limit: int = 100) -> list[dict]:
    """Refunds as STRIPE sees them. Read back from the API, not from our own records —
    an audit that reads your own books proves nothing about the other party's."""
    out, _ = _call('GET', f'/refunds?limit={min(limit, 100)}')
    return [{
        'provider_ref': r['id'],
        'order_ref': (r.get('metadata') or {}).get('axiom_order_ref', ''),
        'amount_cents': r['amount'],
        'currency': r['currency'],
        'status': r['status'],
        'charge': r.get('charge'),
        'created': r['created'],
    } for r in out.get('data', [])]


def duplicate_check(order_refs: list[str] | None = None) -> list[dict]:
    """Any order Stripe refunded more than once. The headline query, asked of Stripe.

    Counting DISTINCT refund objects per order is the honest test: a replayed request
    returns the same refund id, so a genuine double-refund is the only thing that can
    produce two.
    """
    by_order: dict[str, list[dict]] = {}
    for r in ledger(100):
        ref = r['order_ref']
        if not ref or (order_refs is not None and ref not in order_refs):
            continue
        by_order.setdefault(ref, []).append(r)
    return [{'order_ref': k, 'refund_count': len(v),
             'total_cents': sum(x['amount_cents'] for x in v),
             'refund_ids': [x['provider_ref'] for x in v]}
            for k, v in by_order.items() if len(v) > 1]


def available() -> bool:
    try:
        _key()
        return True
    except StripeNotConfigured:
        return False
