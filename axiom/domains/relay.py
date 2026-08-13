"""AXIOM :: the external world, second instance — a bulk message relay.

The same honest properties as axiom/provider.py, and for the same reason: if the
external system could be enlisted in AXIOM's transaction, AXIOM would be solving a
problem nobody has. So this is

    * its own DATABASE (`relay`, alongside `provider`)
    * reached over its own CONNECTION POOL
    * never inside an AXIOM transaction
    * with real idempotency semantics:
        unknown key                      -> deliver, 201
        known key + same fingerprint     -> return the original send, 200, replayed
        known key + different fingerprint-> 409, terminal. Not a retry, a new intent.

WHAT IS DIFFERENT FROM THE PAYMENT PROVIDER, AND WHY IT MATTERS
---------------------------------------------------------------
A refund ledger holds one row per refund, so "did we act twice?" is a GROUP BY on
order_ref. A message relay holds one row per PERSON WHO RECEIVED SOMETHING, and that is
a much less forgiving record: a double send is not a duplicated number in a database, it
is forty thousand human beings receiving a second copy of an email that cannot be
recalled. So relay_delivery materializes one row per recipient per send, and the audit
query is `GROUP BY campaign_ref, recipient HAVING count(*) > 1`.

There is deliberately NO unique constraint on (campaign_ref, recipient).

That absence is the whole experiment. A real ESP will happily deliver the same campaign
to the same address twice — it is not its job to know that you meant to send once. If
this table refused duplicates, the relay would be doing AXIOM's job and the demo would
prove nothing. The ONLY thing standing between a crashed agent and a second delivery to
every recipient is the idempotency key, which is exactly the claim under test.

Recipients are derived deterministically from the campaign ref rather than passed as a
40,000-element list, so a second send of the same campaign reaches the SAME addresses
and the duplicate query can see it. A real gateway takes the list; nothing about the
guarantee changes.
"""

from __future__ import annotations

import atexit
import threading
import time
import uuid
from typing import Any, Sequence
from urllib.parse import urlsplit, urlunsplit

import os

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from ..config import settings
from ..provider import ProviderCrash, ProviderError, fingerprint

_pool: ConnectionPool | None = None
_schema_lock = threading.Lock()
_schema_ready = False


class RelayCrash(ProviderCrash):
    """Simulated process death at a chosen instant inside a send.

    Subclasses ProviderCrash — which is a BaseException, so `except Exception` cannot
    swallow it and turn a correctness test into a false pass — so that any handler
    already written for "the worker died mid-dispatch" keeps working unchanged. The
    class name that survives into the log line is still RelayCrash, which is what tells
    a reader WHICH outside world the process died talking to.
    """


def relay_url() -> str:
    """The relay's DSN: the `relay` database on the same cluster.

    Same URL-parser care as provider_url(). rpartition('/') on a connection string
    carrying `sslrootcert=certs/root.crt` replaces the wrong path component and fails
    with a connection timeout that reads like a network problem; CockroachDB Cloud
    requires that path-valued parameter, so it is the normal case, not an edge case.
    """
    explicit = os.environ.get('RELAY_DATABASE_URL')
    if explicit:
        return explicit
    parts = urlsplit(settings.database_url)
    return urlunsplit((parts.scheme, parts.netloc, '/relay', parts.query, parts.fragment))


# --------------------------------------------------------------------- provisioning

_DDL = (
    """
    CREATE TABLE IF NOT EXISTS relay_send (
        id                  UUID        NOT NULL DEFAULT gen_random_uuid(),
        send_ref            STRING      NOT NULL,
        idempotency_key     STRING      NOT NULL,
        request_fingerprint STRING      NOT NULL,
        campaign_ref        STRING      NOT NULL,
        segment             STRING      NOT NULL,
        recipient_count     INT8        NOT NULL,
        status              STRING      NOT NULL DEFAULT 'sent',
        replay_count        INT8        NOT NULL DEFAULT 0,
        created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT relay_send_pkey PRIMARY KEY (id),
        CONSTRAINT relay_send_key_uniq UNIQUE (idempotency_key),
        CONSTRAINT relay_send_ref_uniq UNIQUE (send_ref)
    )
    """,
    'CREATE INDEX IF NOT EXISTS relay_send_by_campaign ON relay_send (campaign_ref, created_at)',
    """
    CREATE TABLE IF NOT EXISTS relay_delivery (
        id           UUID        NOT NULL DEFAULT gen_random_uuid(),
        send_ref     STRING      NOT NULL,
        campaign_ref STRING      NOT NULL,
        recipient    STRING      NOT NULL,
        delivered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT relay_delivery_pkey PRIMARY KEY (id)
    )
    """,
    # NO UNIQUE (campaign_ref, recipient). See the module docstring — that omission is
    # the experiment, not an oversight, and this index exists to make the duplicate
    # query fast rather than to prevent the duplicate.
    'CREATE INDEX IF NOT EXISTS relay_delivery_by_recipient ON relay_delivery (campaign_ref, recipient)',
    """
    CREATE TABLE IF NOT EXISTS relay_request_log (
        id                  UUID        NOT NULL DEFAULT gen_random_uuid(),
        idempotency_key     STRING      NOT NULL,
        request_fingerprint STRING      NOT NULL,
        campaign_ref        STRING      NOT NULL,
        recipient_count     INT8        NOT NULL,
        verdict             STRING      NOT NULL,
        http_status         INT2        NOT NULL,
        received_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT relay_request_log_pkey PRIMARY KEY (id)
    )
    """,
    'CREATE INDEX IF NOT EXISTS relay_request_log_by_key ON relay_request_log (idempotency_key, received_at)',
)


def ensure_schema() -> None:
    """Provision the relay, idempotently, over its own one-shot connections.

    Deliberately NOT a numbered file in db/. Those migrations describe AXIOM's own
    tables, which the live cluster already carries and which must never be rewritten;
    this is a stand-in for a third-party system, and a stand-in that provisions itself is
    honest about being one. If the relay ever becomes a real integration, its schema
    stops being ours entirely.
    """
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        # CREATE DATABASE cannot run from inside the database being created, so the
        # bootstrap connects to the base DSN first. One statement, autocommit, gone.
        with psycopg.connect(settings.database_url, autocommit=True,
                             connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute('CREATE DATABASE IF NOT EXISTS relay')
        with psycopg.connect(relay_url(), autocommit=True, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                for stmt in _DDL:
                    cur.execute(stmt)
        _schema_ready = True


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        ensure_schema()
        # Narrow, for the same reason provider.pool() is narrow: in the serverless
        # shape concurrency arrives as INSTANCES, and instances x max_size is what
        # meets CockroachDB Basic's connection cap.
        _pool = ConnectionPool(relay_url(),
                               min_size=settings.pool_min, max_size=settings.pool_max,
                               timeout=10.0, max_idle=300.0,
                               check=ConnectionPool.check_connection,
                               kwargs={'row_factory': dict_row,
                                       'application_name': 'axiom-relay',
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


# ---------------------------------------------------------------------- the send

def _log(cur, key: str, fp: str, campaign_ref: str, n: int, verdict: str,
         status: int) -> None:
    cur.execute("""
        INSERT INTO relay_request_log
            (idempotency_key, request_fingerprint, campaign_ref, recipient_count,
             verdict, http_status)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (key, fp, campaign_ref, n, verdict, status))


def send(*, idempotency_key: str, campaign_ref: str, segment: str, recipient_count: int,
         request_body: dict[str, Any] | None = None,
         chaos_pre: float | None = None, chaos_post: float | None = None,
         latency_ms: int | None = None) -> dict:
    """Deliver a campaign to a segment. THE irreversible act.

    Returns a dict shaped like a gateway response. Raises:
      RelayCrash    — simulated death, at PRE (nothing sent) or POST (everything sent)
      ProviderError — 409 when the key is reused with a different body

    The middle case of the idempotency contract is the one that matters: a crash between
    the send and AXIOM's settle is harmless because the recovered worker re-sends under
    the SAME derived key and this function returns the original send instead of putting
    a second copy in every inbox.
    """
    import random

    body = request_body or {'campaign_ref': campaign_ref, 'segment': segment,
                            'recipient_count': recipient_count}
    fp = fingerprint(body)

    # --- crash window PRE: receipt is durable, nothing has been sent -----------------
    pre = settings.chaos_crash_before_dispatch if chaos_pre is None else chaos_pre
    if pre and random.random() < pre:
        raise RelayCrash('CHAOS: died after PREPARE, before the send left (W2)')

    time.sleep((settings.provider_latency_ms if latency_ms is None else latency_ms) / 1000.0)

    with pool().connection() as conn:
        # The relay's OWN transaction, so a send row and its deliveries land together.
        # That is the relay being a competent external system; it says nothing about
        # AXIOM, which still cannot see inside it.
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("""
                SELECT send_ref, request_fingerprint, campaign_ref, segment,
                       recipient_count, status, replay_count
                FROM relay_send WHERE idempotency_key = %s
            """, (idempotency_key,))
            existing = cur.fetchone()

            if existing:
                if existing['request_fingerprint'] != fp:
                    _log(cur, idempotency_key, fp, campaign_ref, recipient_count,
                         'rejected_fingerprint', 409)
                    conn.commit()
                    raise ProviderError(
                        'idempotency key reused with a different request body',
                        status=409, retryable=False)

                cur.execute("""
                    UPDATE relay_send
                    SET replay_count = replay_count + 1, last_seen_at = now()
                    WHERE idempotency_key = %s
                    RETURNING replay_count
                """, (idempotency_key,))
                replays = cur.fetchone()['replay_count']
                _log(cur, idempotency_key, fp, campaign_ref, recipient_count,
                     'replayed', 200)
                out = {'id': existing['send_ref'], 'campaign_ref': existing['campaign_ref'],
                       'segment': existing['segment'],
                       'recipient_count': existing['recipient_count'],
                       'status': existing['status'], 'idempotent_replay': True,
                       'replay_count': replays, 'http_status': 200, 'replayed': True}
            else:
                ref = 'msg_' + uuid.uuid4().hex[:20]
                cur.execute("""
                    INSERT INTO relay_send
                        (send_ref, idempotency_key, request_fingerprint, campaign_ref,
                         segment, recipient_count)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (ref, idempotency_key, fp, campaign_ref, segment, recipient_count))
                # One row per human being. Generated server-side in a single statement so
                # a five-thousand-recipient campaign is one round trip rather than five
                # thousand — the count has to be real for the audit to mean anything, but
                # it does not have to be slow.
                cur.execute("""
                    INSERT INTO relay_delivery (send_ref, campaign_ref, recipient)
                    SELECT %s, %s, %s || '+' || g::STRING || '@example.invalid'
                    FROM generate_series(1, %s) AS g
                """, (ref, campaign_ref, campaign_ref, recipient_count))
                _log(cur, idempotency_key, fp, campaign_ref, recipient_count,
                     'created', 201)
                out = {'id': ref, 'campaign_ref': campaign_ref, 'segment': segment,
                       'recipient_count': recipient_count, 'status': 'sent',
                       'idempotent_replay': False, 'http_status': 201, 'replayed': False}
        conn.commit()

    # --- crash window POST: every message is in an inbox, AXIOM has not recorded it ---
    post = settings.chaos_crash_after_dispatch if chaos_post is None else chaos_post
    if post and random.random() < post:
        raise RelayCrash('CHAOS: died after the campaign went out, before settle (W4)')

    return out


# ------------------------------------------------------------------ audit / the demo

def duplicate_recipients(campaign_refs: Sequence[str] | None = None) -> list[dict]:
    """Anyone who received the same campaign more than once. The headline query.

    An empty result IS the claim: N crashes, M re-sends, zero people messaged twice.
    Scoped to one run's campaigns by default for the same reason the refund audit is
    scoped to one run's orders — the ledger is append-only and shared, and a test that
    deliberately double-sends must not fail a later demo run.

    `None` means the whole ledger. AN EMPTY LIST MEANS NOTHING, and the difference is not
    pedantry: this was written as `if campaign_refs` and a demo run whose scoping query
    returned no rows therefore audited the ENTIRE ledger instead of auditing nothing. It
    printed "campaigns sent 9" from another run's rows and looked completely normal. An
    audit that silently widens its own scope on empty input is how a headline number stops
    being evidence.
    """
    scoped = campaign_refs is not None
    where = 'WHERE campaign_ref = ANY(%s)' if scoped else ''
    args = (list(campaign_refs),) if scoped else ()
    with pool().connection() as conn:
        with conn.cursor() as cur:
            # Deliberately unbounded. This was written with LIMIT 50 and the domain-2
            # counterexample test caught it immediately: a run that double-messaged 300
            # people reported 50, so the headline number — the ONE number the demo exists
            # to show — would have understated a catastrophic failure by a factor of six.
            # An audit query that truncates is worse than no audit query. Callers that
            # only want a preview slice the result.
            cur.execute(f"""
                SELECT campaign_ref, recipient, count(*) AS deliveries
                FROM relay_delivery {where}
                GROUP BY campaign_ref, recipient HAVING count(*) > 1
                ORDER BY deliveries DESC
            """, args)
            return cur.fetchall()


def stats(campaign_refs: Sequence[str] | None = None) -> dict:
    # None = the whole ledger; [] = nothing. See duplicate_recipients().
    scoped = campaign_refs is not None
    where = 'WHERE campaign_ref = ANY(%s)' if scoped else ''
    args = (list(campaign_refs),) if scoped else ()
    with pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT count(*) AS sends, coalesce(sum(replay_count), 0) AS replays
                FROM relay_send {where}
            """, args)
            row = dict(cur.fetchone())
            cur.execute(f'SELECT count(*) AS deliveries FROM relay_delivery {where}', args)
            row['deliveries'] = cur.fetchone()['deliveries']
            cur.execute(f"""
                SELECT verdict, count(*) AS n FROM relay_request_log {where}
                GROUP BY verdict
            """, args)
            row['verdicts'] = {r['verdict']: r['n'] for r in cur.fetchall()}
            row['duplicate_recipients'] = len(duplicate_recipients(campaign_refs))
            return row


def ledger(campaign_ref: str | None = None, limit: int = 200) -> list[dict]:
    where = 'WHERE campaign_ref = %s' if campaign_ref else ''
    args = (campaign_ref,) if campaign_ref else ()
    with pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT send_ref, campaign_ref, segment, recipient_count, status,
                       replay_count, idempotency_key, created_at, last_seen_at
                FROM relay_send {where}
                ORDER BY created_at DESC LIMIT {int(limit)}
            """, args)
            return cur.fetchall()


def reset() -> None:
    """Wipe the external world. Tests and demo resets only.

    TRUNCATE, not DELETE. This was three unbounded DELETEs, and at 18,540 rows the first
    one blew through the 15s statement_timeout that axiom/db.py sets — the demo died with
    QueryCanceled before it printed anything.

    Which is a small joke at this project's expense, because db/001_schema.sql's own
    header warns about exactly this: CockroachDB's hotspot guidance says deleting rows
    "tends to accumulate an ordered set of garbage data behind the live data", and the
    task table is designed around never doing it. The external stand-in then did it three
    times per reset. TRUNCATE is a schema-level operation rather than a per-row write, so
    it is fast and leaves no tombstones to scan past.
    """
    with pool().connection() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            # One statement: TRUNCATE on tables with FK relationships must name them
            # together, and doing it atomically means a reset cannot half-happen.
            cur.execute('TRUNCATE relay_delivery, relay_send, relay_request_log')
