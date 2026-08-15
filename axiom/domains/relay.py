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

THE REAL PATH: AMAZON SES, WHICH OFFERS NO IDEMPOTENCY AT ALL
--------------------------------------------------------------
`send(recipients=[...])` with AXIOM_SES=1 stops simulating and puts real messages on the
wire through `axiom/ses.py`. Everything above still holds, and one thing sharpens: SES has
no idempotency key. Call SendEmail twice and two emails arrive. There is no header to
hand it, no replay flag to read back, no dedupe window. So on this path the four lines at
the top of this file are not a model of what an ESP enforces — they are the ONLY place the
guarantee exists anywhere in the system.

Which forces the write order, and the write order is the whole design:

    tx1   claim the idempotency key      COMMIT   <- durable, before anything irreversible
    ---   no transaction is open here
          SendEmail                               <- the act. Cannot be undone or asked about
    ---   one small transaction per accepted message, carrying its MessageId
    tx2   mark the send complete         COMMIT

A key that already exists is answered from this store and SES is never called, which is
what makes a second delivery impossible. A process that dies between the reservation and
the record leaves a row that says `reserved` with no MessageId, and a later re-send under
the same key is REFUSED rather than retried — the relay would rather lose a message than
send one twice, because "we do not know whether that email went out" and "it did not go
out" are not the same claim, and only one of them is safe to act on.

That is at-most-once, stated plainly. Exactly-once across two systems with no shared
transaction is not available from anybody, and this file does not pretend otherwise.
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

from .. import ses
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
    # --- the real-SES columns, added rather than baked in --------------------------
    # ADD COLUMN IF NOT EXISTS, not a rewritten CREATE TABLE: the relay already exists on
    # the live cluster with rows in it, and a stand-in for a third-party system that
    # silently changes shape under a running demo is worse than one that migrates.
    #
    # channel is what stops the two paths lying about each other. A simulated delivery and
    # a real SES delivery are both one row in relay_delivery, and only this column can
    # answer "was there actually an email?" — an audit that cannot tell them apart would
    # let the demo count 1,700 imaginary sends as evidence about Amazon SES.
    #
    # NULLABLE AND WITHOUT A DEFAULT, which is not a style preference. `NOT NULL DEFAULT
    # 'simulated'` has to write the new value into every existing row, and this table holds
    # 230,110 of them on the development cluster alone: the declarative schema changer
    # enqueued the backfill and the node refused it outright — "store 1 has insufficient
    # remaining capacity to ingest data (remaining: 2.9 GiB / 1.3%, min required: 5.0%)" —
    # leaving a PAUSED schema-change job that had to be cancelled by hand. A migration that
    # can strand a live demo's external system in a half-applied state, for a value every
    # reader can already infer, is a bad trade. NULL means exactly "written before this
    # column existed", which is precisely the simulated path, and the selects below say so
    # with COALESCE.
    'ALTER TABLE relay_send ADD COLUMN IF NOT EXISTS channel STRING',
    'ALTER TABLE relay_delivery ADD COLUMN IF NOT EXISTS channel STRING',
    # NULL for a simulated delivery, and the SES MessageId for a real one. It is the only
    # handle anybody has on a message that has already left, so it is the evidence the
    # crash proof reads: two dispatches, one MessageId.
    'ALTER TABLE relay_delivery ADD COLUMN IF NOT EXISTS message_id STRING',
    # How long SES took to accept this one message. Recorded on the delivery row rather
    # than returned to the caller because the caller may not survive to receive it: at
    # crash window W4 the response — MessageId, latency and all — is thrown away with the
    # process. Anything worth measuring about the send has to be written down by the
    # system that made it, before the system that asked for it dies.
    'ALTER TABLE relay_delivery ADD COLUMN IF NOT EXISTS latency_ms INT8',
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
         status: int) -> Any:
    """One row per REQUEST, and the row id, so a request whose verdict is not yet known
    can be settled later instead of logged twice. See _dispatch_live()."""
    cur.execute("""
        INSERT INTO relay_request_log
            (idempotency_key, request_fingerprint, campaign_ref, recipient_count,
             verdict, http_status)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (key, fp, campaign_ref, n, verdict, status))
    return cur.fetchone()['id']


def _reject_or_replay(cur, existing: dict, *, idempotency_key: str, fp: str,
                      campaign_ref: str, recipient_count: int) -> dict:
    """The two answers a known key can get. Shared by both dispatch paths verbatim,
    because a guarantee that is spelled differently in two places is a guarantee that
    will eventually disagree with itself.

    Raises ProviderError(409) when the same key arrives with a different body — crash
    window W7, a NEW INTENT wearing an OLD key.
    """
    if existing['request_fingerprint'] != fp:
        _log(cur, idempotency_key, fp, campaign_ref, recipient_count,
             'rejected_fingerprint', 409)
        return {}                    # caller commits, then raises; see below
    cur.execute("""
        UPDATE relay_send
        SET replay_count = replay_count + 1, last_seen_at = now()
        WHERE idempotency_key = %s
        RETURNING replay_count
    """, (idempotency_key,))
    replays = cur.fetchone()['replay_count']
    _log(cur, idempotency_key, fp, campaign_ref, recipient_count, 'replayed', 200)
    return {'id': existing['send_ref'], 'campaign_ref': existing['campaign_ref'],
            'segment': existing['segment'],
            'recipient_count': existing['recipient_count'],
            'status': existing['status'], 'idempotent_replay': True,
            'replay_count': replays, 'http_status': 200, 'replayed': True,
            'channel': existing.get('channel', 'simulated')}


_SELECT_SEND = """
    SELECT send_ref, request_fingerprint, campaign_ref, segment,
           recipient_count, status, replay_count,
           COALESCE(channel, 'simulated') AS channel
    FROM relay_send WHERE idempotency_key = %s
"""


def send(*, idempotency_key: str, campaign_ref: str, segment: str, recipient_count: int,
         request_body: dict[str, Any] | None = None,
         recipients: Sequence[str] | None = None,
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

    `recipients` is the real-gateway shape — an explicit address list rather than a count.
    With AXIOM_SES=1 and a list present, those addresses receive REAL email through Amazon
    SES and every guarantee above is enforced by this store alone, because SES has no
    idempotency of its own. With the flag off, or with no list, this is byte-for-byte the
    simulated path it has always been: the demo a judge presses does not change behaviour
    unless somebody arms it.
    """
    import random

    body = request_body or {'campaign_ref': campaign_ref, 'segment': segment,
                            'recipient_count': recipient_count}
    fp = fingerprint(body)

    # --- crash window PRE: receipt is durable, nothing has been sent -----------------
    pre = settings.chaos_crash_before_dispatch if chaos_pre is None else chaos_pre
    if pre and random.random() < pre:
        raise RelayCrash('CHAOS: died after PREPARE, before the send left (W2)')

    if recipients and ses.enabled():
        out = _dispatch_live(idempotency_key=idempotency_key, campaign_ref=campaign_ref,
                             segment=segment, recipient_count=recipient_count,
                             recipients=list(recipients), fp=fp)
    else:
        out = _dispatch_simulated(
            idempotency_key=idempotency_key, campaign_ref=campaign_ref, segment=segment,
            recipient_count=recipient_count, fp=fp,
            latency_ms=latency_ms)

    # --- crash window POST: every message is in an inbox, AXIOM has not recorded it ---
    post = settings.chaos_crash_after_dispatch if chaos_post is None else chaos_post
    if post and random.random() < post:
        raise RelayCrash('CHAOS: died after the campaign went out, before settle (W4)')

    return out


def _dispatch_simulated(*, idempotency_key: str, campaign_ref: str, segment: str,
                        recipient_count: int, fp: str,
                        latency_ms: int | None) -> dict:
    """The stand-in ESP. Unchanged behaviour, moved into a function so the real path can
    sit beside it and be read against it."""
    time.sleep((settings.provider_latency_ms if latency_ms is None else latency_ms) / 1000.0)

    with pool().connection() as conn:
        # The relay's OWN transaction, so a send row and its deliveries land together.
        # That is the relay being a competent external system; it says nothing about
        # AXIOM, which still cannot see inside it.
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute(_SELECT_SEND, (idempotency_key,))
            existing = cur.fetchone()

            if existing:
                out = _reject_or_replay(cur, existing, idempotency_key=idempotency_key,
                                        fp=fp, campaign_ref=campaign_ref,
                                        recipient_count=recipient_count)
                if not out:
                    conn.commit()
                    raise ProviderError(
                        'idempotency key reused with a different request body',
                        status=409, retryable=False)
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
                       'idempotent_replay': False, 'http_status': 201, 'replayed': False,
                       'channel': 'simulated'}
        conn.commit()

    return out


# ------------------------------------------------------------- the real path: SES

def _deliveries(cur, send_ref: str) -> list[dict]:
    cur.execute("""
        SELECT recipient, message_id, latency_ms,
               COALESCE(channel, 'simulated') AS channel
        FROM relay_delivery WHERE send_ref = %s ORDER BY delivered_at
    """, (send_ref,))
    return [dict(r) for r in cur.fetchall()]


def _dispatch_live(*, idempotency_key: str, campaign_ref: str, segment: str,
                   recipient_count: int, recipients: list[str], fp: str) -> dict:
    """Real email, through Amazon SES, deduplicated by this store and nothing else.

    Three transactions and one irreversible act between them, in this order and no other:

      1. CLAIM THE KEY, COMMIT. After this line a second call under the same key can be
         answered without asking SES anything, which is the only reason a second email
         cannot happen. Committed BEFORE the send because a reservation written after the
         act protects nothing.
      2. SEND, outside any transaction, one message at a time, recording each MessageId in
         its own small transaction the instant SES returns it. Per-message rather than a
         single write at the end so the unrecorded window is one message wide instead of
         the whole campaign.
      3. MARK IT COMPLETE, and settle the request-log row that step 1 opened.

    A failure between 1 and 3 leaves `status='reserved'`. The next call under that key is
    answered as a replay and sends NOTHING — see the module docstring: losing a message is
    a bad afternoon, and sending one twice is a compliance incident, so the ambiguous case
    resolves toward silence. The one exception is a failure the sender can prove happened
    before SES accepted anything, which releases the reservation so the campaign is not
    permanently burned by a typo in an address.
    """
    # The audience the policy authorized may be smaller than the list (triage is allowed
    # to trim and never to widen), so the effective fanout is the smaller of the two.
    fanout = recipients[:min(len(recipients), recipient_count)]
    if not fanout:
        raise ProviderError('no deliverable recipients for this campaign',
                            status=400, retryable=False)
    if len(fanout) > ses.max_per_send():
        # REFUSE rather than truncate. Silently mailing the first 5 of 1,000 addresses
        # would put a number in the ledger that means nothing and would read, to anyone
        # checking, as a campaign that succeeded. Raising AXIOM_SES_MAX_PER_SEND is a
        # decision somebody makes on purpose against a 200/day sandbox.
        raise ProviderError(
            f'{len(fanout)} recipients exceeds AXIOM_SES_MAX_PER_SEND='
            f'{ses.max_per_send()}; refusing to mail part of a campaign and call it sent',
            status=400, retryable=False)
    for addr in fanout:
        ses.guard(addr)              # refuse the whole campaign before sending any of it

    # ---------------------------------------------------------- 1. claim the key
    with pool().connection() as conn:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute(_SELECT_SEND, (idempotency_key,))
            existing = cur.fetchone()
            if existing:
                out = _reject_or_replay(cur, existing, idempotency_key=idempotency_key,
                                        fp=fp, campaign_ref=campaign_ref,
                                        recipient_count=recipient_count)
                if not out:
                    conn.commit()
                    raise ProviderError(
                        'idempotency key reused with a different request body',
                        status=409, retryable=False)
                rows = _deliveries(cur, existing['send_ref'])
                conn.commit()
                out['message_ids'] = [r['message_id'] for r in rows if r['message_id']]
                out['recipients'] = [r['recipient'] for r in rows]
                # The latencies of the ORIGINAL send, read back out of the store. The
                # process that measured them may be dead; the numbers are not.
                out['ses_latency_ms'] = [r['latency_ms'] for r in rows
                                         if r['latency_ms'] is not None]
                # THE NUMBER THE PROOF READS. Messages SES accepted DURING THIS CALL —
                # zero, always, on a replay. Two dispatches, one MessageId.
                out['ses_accepted'] = 0
                if existing['status'] == 'reserved':
                    out['warning'] = (
                        'the original send was reserved and never confirmed; refusing to '
                        're-send under this key. At most once, never twice.')
                return out

            ref = 'msg_' + uuid.uuid4().hex[:20]
            cur.execute("""
                INSERT INTO relay_send
                    (send_ref, idempotency_key, request_fingerprint, campaign_ref,
                     segment, recipient_count, status, channel)
                VALUES (%s, %s, %s, %s, %s, %s, 'reserved', 'ses')
            """, (ref, idempotency_key, fp, campaign_ref, segment, recipient_count))
            # 202: the relay has ACCEPTED the request and claimed the key, and does not yet
            # know what SES will say. One row per request, settled in step 3 rather than
            # written twice — a request log that logs one request twice cannot be counted.
            log_id = _log(cur, idempotency_key, fp, campaign_ref, len(fanout),
                          'reserved', 202)
        conn.commit()

    # ------------------------------------------------------- 2. the irreversible act
    accepted: list[dict] = []
    subject = f'[AXIOM] {campaign_ref} · {segment}'
    text = (f'AXIOM crash-safe execution demo.\n\n'
            f'campaign        {campaign_ref}\n'
            f'segment         {segment}\n'
            f'idempotency key {idempotency_key}\n\n'
            f'Amazon SES has no idempotency key. This message exists once because the key '
            f'above was committed to a durable store before it was sent, and the second '
            f'dispatch under that key never reached SES at all.\n')
    try:
        for addr in fanout:
            a = ses.send_one(recipient=addr, subject=subject, body_text=text,
                             campaign_ref=campaign_ref, idempotency_key=idempotency_key)
            # Recorded IMMEDIATELY, in its own transaction. Any later failure therefore
            # loses at most the message currently in flight.
            with pool().connection() as conn:
                conn.autocommit = True
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO relay_delivery
                            (send_ref, campaign_ref, recipient, message_id, latency_ms,
                             channel)
                        VALUES (%s, %s, %s, %s, %s, 'ses')
                    """, (ref, campaign_ref, a.recipient, a.message_id,
                          int(a.latency_ms)))
            accepted.append({'recipient': a.recipient, 'message_id': a.message_id,
                             'latency_ms': round(a.latency_ms, 1)})
    except ProviderError as e:
        if not accepted and getattr(e, 'sent_uncertain', True) is False:
            # Provably nothing was sent — SES named the refusal before accepting anything.
            # Releasing the reservation is safe here and ONLY here, and it matters: a
            # campaign whose key is burned by a rejected address can never be sent again
            # under the receipt AXIOM already committed.
            _release(idempotency_key, ref, log_id, fp, campaign_ref, str(e))
        raise

    # ------------------------------------------------------------ 3. mark it complete
    with pool().connection() as conn:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("UPDATE relay_send SET status = 'sent', last_seen_at = now() "
                        'WHERE send_ref = %s', (ref,))
            cur.execute("UPDATE relay_request_log SET verdict = 'created', "
                        'http_status = 201 WHERE id = %s', (log_id,))
        conn.commit()

    return {'id': ref, 'campaign_ref': campaign_ref, 'segment': segment,
            'recipient_count': recipient_count, 'status': 'sent',
            'idempotent_replay': False, 'http_status': 201, 'replayed': False,
            'channel': 'ses',
            'recipients': [a['recipient'] for a in accepted],
            'message_ids': [a['message_id'] for a in accepted],
            'ses_accepted': len(accepted),
            'ses_latency_ms': [a['latency_ms'] for a in accepted],
            'ses_region': ses.region(),
            'requested_recipients': len(recipients)}


def _release(idempotency_key: str, send_ref: str, log_id: Any, fp: str,
             campaign_ref: str, reason: str) -> None:
    """Un-claim a key for a send that provably never happened. Never raises.

    Deliberately narrow. This is the only path that deletes a reservation, it runs only
    when zero messages were accepted AND the error was one SES raised before accepting
    anything, and if it fails the key simply stays claimed — which is the safe direction.
    """
    try:
        with pool().connection() as conn:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute('DELETE FROM relay_send WHERE send_ref = %s AND '
                            "status = 'reserved'", (send_ref,))
                cur.execute("UPDATE relay_request_log SET verdict = 'rejected_upstream', "
                            'http_status = 400 WHERE id = %s', (log_id,))
            conn.commit()
    except Exception:                                # noqa: BLE001 — see the docstring
        pass


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
