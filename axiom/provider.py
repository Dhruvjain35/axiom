"""AXIOM :: the external world.

A stand-in payment provider with Stripe's idempotency semantics, reached over its own
connection to its own database, always outside any AXIOM transaction. See
db/003_provider.sql for why that separation is the whole experiment.

This module also owns CHAOS, because the failures we need are not random network
failures — they are failures at three specific instants relative to the external call:

    ... receipt committed ...
        [PRE]   <- die here: the effect definitely did NOT happen (window W2)
    ... provider mutates its ledger ...
        [MID]   <- die here: the effect MAY have happened (W3)
    ... response received ...
        [POST]  <- die here: the effect DEFINITELY happened, we just never recorded it (W4)
    ... settle ...

`ProviderCrash` is raised to simulate an in-process death for tests; the demo instead
sends a real SIGKILL to a worker, which is strictly more convincing and exercises the
same code path because neither one gets to run a finally block that matters.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import random
import time
import uuid
from dataclasses import dataclass
from typing import Any, Sequence
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import settings

_pool: ConnectionPool | None = None


def provider_url() -> str:
    """The provider's DSN. Defaults to the `provider` database on the same cluster.

    Same cluster, different database, different connection, no shared transaction —
    which is exactly the relationship a real payments API has with your application,
    minus the network.
    """
    explicit = os.environ.get('PROVIDER_DATABASE_URL')
    if explicit:
        return explicit

    # Swap ONLY the database component, using a real URL parser.
    #
    # This was `base.rpartition('/')`, which works right up until the connection string
    # carries a query parameter containing a slash — and exactly one does:
    #
    #   ...:26257/axiom?sslmode=verify-full&sslrootcert=certs/root.crt
    #
    # rpartition finds the slash in `certs/root.crt`, so the "database name" it replaced
    # was `root.crt` and the provider pool was handed a URL pointing at a database called
    # `provider` inside a mangled query string. It failed with "no connection after 6s",
    # which reads like a network or connection-limit problem and is neither — the first
    # Vercel deploy spent a debugging round on that.
    #
    # CockroachDB Cloud requires the cluster CA on disk (its CA is not in the system trust
    # store), so a path-valued parameter is not an edge case here, it is the normal case.
    parts = urlsplit(settings.database_url)
    return urlunsplit((parts.scheme, parts.netloc, '/provider',
                       parts.query, parts.fragment))


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        # Sized from settings, not hardcoded, because "the right pool" depends entirely on
        # the deployment shape and this process runs in three of them:
        #
        #   one long-lived server   concurrency arrives as threads -> ONE pool must be wide
        #                           (max_size 6 was exhausted by six simultaneous viewers,
        #                           and every Mission Control poll hits provider/stats, so
        #                           a handful of judges produced 503s on the one number
        #                           the demo exists to show)
        #   serverless              concurrency arrives as INSTANCES, each with its own
        #                           pool -> every instance must be narrow, because the
        #                           number that matters is instances x max_size against
        #                           CockroachDB Basic's connection cap. A wide pool here
        #                           is how a free-tier cluster runs out of connections and
        #                           the demo dies with "no connection after 6s" — which is
        #                           exactly what the first Vercel deploy did.
        #
        # min_size follows too: opening two connections during a cold start costs the
        # first request two TLS handshakes it did not need.
        _pool = ConnectionPool(provider_url(),
                               min_size=settings.pool_min, max_size=settings.pool_max,
                               timeout=10.0, max_idle=300.0,
                               check=ConnectionPool.check_connection,
                               kwargs={'row_factory': dict_row,
                                       'application_name': 'axiom-provider',
                                       'connect_timeout': 10,
                                       'options': '-c statement_timeout=15000'},
                               open=True)
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


atexit.register(close_pool)


class ProviderCrash(BaseException):
    """Simulated process death at a chosen instant.

    Inherits from BaseException, not Exception, on purpose: an `except Exception`
    somewhere in the worker must not be able to swallow a simulated crash and turn a
    correctness test into a false pass.
    """


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, status: int, retryable: bool):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True)
class ProviderResult:
    provider_ref: str
    status: int
    body: dict[str, Any]
    replayed: bool          # True => the provider recognized the key and did NOT re-act


def fingerprint(body: dict[str, Any]) -> str:
    """SHA-256 over a canonicalized request body.

    Canonical = sorted keys, no insignificant whitespace. Two requests that differ only
    in key order must hash identically or every retry would look like a new intent.
    """
    canonical = json.dumps(body, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _log(cur, key: str, fp: str, order_ref: str, amount: int, verdict: str, status: int) -> None:
    cur.execute("""
        INSERT INTO provider_request_log
            (idempotency_key, request_fingerprint, order_ref, amount_cents, verdict, http_status)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (key, fp, order_ref, amount, verdict, status))


def create_refund(*, idempotency_key: str, order_ref: str, amount_cents: int,
                  currency: str = 'USD', request_body: dict[str, Any] | None = None,
                  chaos_pre: float | None = None, chaos_post: float | None = None,
                  latency_ms: int | None = None) -> ProviderResult:
    """Issue a refund. THE irreversible act.

    Behaviour, matching a real provider:
      * unknown key                      -> create the refund, return 201
      * known key, same fingerprint      -> return the ORIGINAL refund, 200, replayed=True
      * known key, different fingerprint -> 409, ProviderError(retryable=False)

    The middle case is what makes a crash between dispatch and settle harmless: AXIOM
    re-sends under the same derived key and the provider hands back the refund it
    already made instead of making a second one.
    """
    body = request_body or {'order_ref': order_ref, 'amount_cents': amount_cents,
                            'currency': currency}
    fp = fingerprint(body)

    # --- crash window PRE: after the receipt is durable, before anything is sent ------
    pre = settings.chaos_crash_before_dispatch if chaos_pre is None else chaos_pre
    if pre and random.random() < pre:
        raise ProviderCrash('CHAOS: died after PREPARE, before dispatch (W2)')

    time.sleep((settings.provider_latency_ms if latency_ms is None else latency_ms) / 1000.0)

    with pool().connection() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, provider_ref, request_fingerprint, amount_cents, currency,
                       order_ref, status, replay_count
                FROM provider_refund WHERE idempotency_key = %s
            """, (idempotency_key,))
            existing = cur.fetchone()

            if existing:
                if existing['request_fingerprint'] != fp:
                    # Same key, different intent. Not a retry. A real provider rejects
                    # this and so do we — it is the defence against a recovered agent
                    # re-synthesizing a subtly different request under an old key.
                    _log(cur, idempotency_key, fp, order_ref, amount_cents,
                         'rejected_fingerprint', 409)
                    raise ProviderError(
                        'idempotency key reused with a different request body',
                        status=409, retryable=False)

                cur.execute("""
                    UPDATE provider_refund
                    SET replay_count = replay_count + 1, last_seen_at = now()
                    WHERE idempotency_key = %s
                    RETURNING replay_count
                """, (idempotency_key,))
                replays = cur.fetchone()['replay_count']
                _log(cur, idempotency_key, fp, order_ref, amount_cents, 'replayed', 200)
                result = ProviderResult(
                    provider_ref=existing['provider_ref'], status=200,
                    body={'id': existing['provider_ref'], 'order_ref': existing['order_ref'],
                          'amount_cents': existing['amount_cents'],
                          'currency': existing['currency'], 'status': existing['status'],
                          'idempotent_replay': True, 'replay_count': replays},
                    replayed=True)
            else:
                ref = 're_' + uuid.uuid4().hex[:20]
                cur.execute("""
                    INSERT INTO provider_refund
                        (provider_ref, idempotency_key, request_fingerprint, order_ref,
                         amount_cents, currency)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (ref, idempotency_key, fp, order_ref, amount_cents, currency))
                _log(cur, idempotency_key, fp, order_ref, amount_cents, 'created', 201)
                result = ProviderResult(
                    provider_ref=ref, status=201,
                    body={'id': ref, 'order_ref': order_ref, 'amount_cents': amount_cents,
                          'currency': currency, 'status': 'succeeded',
                          'idempotent_replay': False},
                    replayed=False)

    # --- crash window POST: the effect is real, we have not recorded it yet ----------
    post = settings.chaos_crash_after_dispatch if chaos_post is None else chaos_post
    if post and random.random() < post:
        raise ProviderCrash('CHAOS: died after the refund landed, before settle (W4)')

    return result


# ------------------------------------------------------------------ audit / the demo

def ledger(order_ref: str | None = None, limit: int = 200) -> list[dict]:
    with pool().connection() as conn:
        with conn.cursor() as cur:
            if order_ref:
                cur.execute("""
                    SELECT provider_ref, order_ref, amount_cents, currency, status,
                           replay_count, idempotency_key, created_at, last_seen_at
                    FROM provider_refund WHERE order_ref = %s ORDER BY created_at
                """, (order_ref,))
            else:
                cur.execute("""
                    SELECT provider_ref, order_ref, amount_cents, currency, status,
                           replay_count, idempotency_key, created_at, last_seen_at
                    FROM provider_refund ORDER BY created_at DESC LIMIT %s
                """, (limit,))
            return cur.fetchall()


def duplicate_check(order_refs: Sequence[str] | None = None) -> list[dict]:
    """Any order refunded more than once. The demo's headline query.

    An empty result is the claim: N crashes, M re-sends, zero duplicate effects.

    `order_refs` scopes the check to one run's orders. That is not cosmetic: the ledger
    is append-only and shared, and `scripts/counterexample.py` deliberately double-refunds
    a baseline order to prove a point. Left unscoped, those intentional duplicates make
    every later chaos-demo run report a failure it did not cause.
    """
    with pool().connection() as conn:
        with conn.cursor() as cur:
            if order_refs:
                cur.execute("""
                    SELECT order_ref, count(*) AS refund_count, sum(amount_cents) AS total_cents
                    FROM provider_refund WHERE order_ref = ANY(%s)
                    GROUP BY order_ref HAVING count(*) > 1
                    ORDER BY refund_count DESC
                """, (list(order_refs),))
            else:
                cur.execute("""
                    SELECT order_ref, count(*) AS refund_count, sum(amount_cents) AS total_cents
                    FROM provider_refund GROUP BY order_ref HAVING count(*) > 1
                    ORDER BY refund_count DESC
                """)
            return cur.fetchall()


def stats(order_refs: Sequence[str] | None = None) -> dict:
    """Ledger totals. `order_refs` scopes them to one run — see duplicate_check()."""
    where = 'WHERE order_ref = ANY(%s)' if order_refs else ''
    args = (list(order_refs),) if order_refs else ()
    with pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT count(*) AS refunds,
                       coalesce(sum(amount_cents), 0) AS total_cents,
                       coalesce(sum(replay_count), 0) AS replays
                FROM provider_refund {where}
            """, args)
            row = dict(cur.fetchone())
            cur.execute(f"""
                SELECT verdict, count(*) AS n FROM provider_request_log {where}
                GROUP BY verdict
            """, args)
            row['verdicts'] = {r['verdict']: r['n'] for r in cur.fetchall()}
            row['duplicate_orders'] = len(duplicate_check(order_refs))
            return row


def reset() -> None:
    """Wipe the external world. Tests and demo resets only."""
    with pool().connection() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute('DELETE FROM provider_refund WHERE true')
            cur.execute('DELETE FROM provider_request_log WHERE true')
