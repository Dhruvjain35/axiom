-- =====================================================================================
-- THE EXTERNAL WORLD.
--
-- A stand-in payment provider, modelled on Stripe's idempotency semantics. It lives in
-- its OWN DATABASE and is reached over its OWN CONNECTION with autocommit, never inside
-- an AXIOM transaction.
--
-- That separation is not fastidiousness, it is the entire experiment. If the provider
-- ledger could be written in the same transaction as the receipt, AXIOM would be
-- solving a problem nobody has: the hard part of agent safety is precisely that the
-- irreversible act happens in a system you cannot enlist in your transaction. Putting
-- the fake provider inside our transaction would make the demo pass and prove nothing.
--
-- Semantics implemented (all three matter for the crash-window table):
--   * same key + same request fingerprint      -> REPLAY the original response
--   * same key + different request fingerprint -> reject; this is not a retry
--   * new key                                  -> create a new, real effect
-- =====================================================================================

CREATE DATABASE IF NOT EXISTS provider;
SET database = provider;

CREATE TABLE IF NOT EXISTS provider_refund (
    id                  UUID        NOT NULL DEFAULT gen_random_uuid(),

    -- The provider's own reference, the thing a customer would see on a statement.
    provider_ref        STRING      NOT NULL,

    -- What the caller sent as Idempotency-Key. UNIQUE — this is the provider's
    -- promise, and the reason a correctly-derived key makes a double refund impossible
    -- even when AXIOM re-sends after a crash.
    idempotency_key     STRING      NOT NULL,

    -- SHA-256 of the canonicalized request body, retained so a replayed key carrying a
    -- DIFFERENT body can be rejected rather than silently honoured. A recovering LLM
    -- that re-synthesizes a subtly different request is a new intent wearing an old
    -- key (the semantic-rollback attack class, ACRFence arXiv:2603.20625).
    request_fingerprint STRING      NOT NULL,

    order_ref           STRING      NOT NULL,
    amount_cents        INT8        NOT NULL,
    currency            STRING(3)   NOT NULL DEFAULT 'USD',
    status              STRING      NOT NULL DEFAULT 'succeeded',

    -- How many times this key was presented. >1 proves AXIOM re-sent after a crash AND
    -- that the provider absorbed it. The demo shows this column: replays > 0 with
    -- exactly one refund row is the whole thesis in one line of SQL.
    replay_count        INT8        NOT NULL DEFAULT 0,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT provider_refund_pkey PRIMARY KEY (id),
    CONSTRAINT provider_refund_key_uniq UNIQUE (idempotency_key),
    CONSTRAINT provider_refund_ref_uniq UNIQUE (provider_ref)
);

-- The audit query the demo runs on camera: refunds per order. Any count > 1 is a
-- double refund and a failure of the entire premise.
CREATE INDEX IF NOT EXISTS provider_refund_by_order ON provider_refund (order_ref, created_at);

-- Every request the provider ever saw, including the ones it deduped and the ones it
-- rejected. AXIOM never writes here; only the provider does. It is the independent
-- record used to verify AXIOM's own claims rather than taking them on trust.
CREATE TABLE IF NOT EXISTS provider_request_log (
    id                  UUID        NOT NULL DEFAULT gen_random_uuid(),
    idempotency_key     STRING      NOT NULL,
    request_fingerprint STRING      NOT NULL,
    order_ref           STRING      NOT NULL,
    amount_cents        INT8        NOT NULL,
    verdict             STRING      NOT NULL,   -- created | replayed | rejected_fingerprint | failed
    http_status         INT2        NOT NULL,
    received_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT provider_request_log_pkey PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS provider_request_log_by_key
    ON provider_request_log (idempotency_key, received_at);
