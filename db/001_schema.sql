-- =====================================================================================
-- AXIOM :: core schema
-- Target: CockroachDB v25.4+ (vector indexing GA in v25.4).
-- Applied clean and exercised end-to-end on v26.2.3; DDL also accepted on Cloud v26.2.5.
--
-- Every non-obvious choice below is commented with WHY, because the people reading
-- this wrote the database. The recurring themes:
--
--   H1. NO MONOTONIC PRIMARY KEYS. Cockroach's own performance guide: "Traditional
--       approaches using monotonically increasing INT or SERIAL data types will create
--       hotspots for both reads and writes." Every PK here is gen_random_uuid().
--       There is not one SERIAL, unique_rowid(), or ordered INT PK in this file.
--
--   H2. THE QUEUEING HOTSPOT IS A DOCUMENTED ANTI-PATTERN. Cockroach's "Understand
--       hotspots" page names queues specifically: they "require data to be ordered by
--       write, which necessitates indexing in a way that is likely to create a
--       hotspot", and deleting rows as they are read "tends to accumulate an ordered
--       set of garbage data behind the live data." AXIOM's answer is three-part:
--         (a) the claim index is PARTIAL on non-terminal states, so finished work
--             LEAVES the index and the index stays permanently small;
--         (b) we NEVER DELETE a task, so we never accumulate MVCC tombstones;
--         (c) the claim index is prefixed by an application-assigned `shard`, so the
--             head of the queue is N ranges instead of 1.
--
--   H3. HASH SHARDING GOES WHERE THE KEY IS GENUINELY MONOTONIC. That is the append-
--       only event journal's time index, and nowhere else. The task claim index uses
--       an EXPLICIT shard column instead of USING HASH, because workers must be able
--       to deliberately target a shard subset (static work partitioning, mirroring a
--       Kafka consumer group). An opaque crdb_internal_..._shard_N column cannot be
--       targeted by the application.
--
--   H4. MULTI-TENANT SAFE. Every table carries tenant_id UUID NOT NULL. tenant_id is
--       never the leading PK column on a hot table (that would concentrate all writes
--       for the busiest tenant into one range); it leads secondary indexes, where it
--       is a filter, not a write-ordering key. Shared infrastructure rows (agents) use
--       the reserved SYSTEM tenant, so the NOT NULL constraint is uniform and no code
--       path can forget the predicate.
--
--   H5. SINGLE COLUMN FAMILY on axiom_task, deliberately and permanently. Splitting hot
--       lease columns from the cold JSONB payload is a normal Cockroach optimization
--       that a reviewer might suggest, and it silently breaks every SELECT ... FOR
--       UPDATE SKIP LOCKED query ("SKIP LOCKED cannot be used for tables with multiple
--       column families"). We keep the optional SKIP LOCKED claim path viable by never
--       declaring families here. Verify with SHOW CREATE TABLE after any migration.
--
-- Run: cockroach sql --url "$DATABASE_URL" -f db/001_schema.sql
-- =====================================================================================

-- Vector indexing defaults to true on v25.4+, where it went GA (it was Preview and
-- default-OFF in v25.2/v25.3). We assert rather than assume: on an older cluster the
-- CREATE VECTOR INDEX statements below fail loudly instead of silently degrading.
SET CLUSTER SETTING feature.vector_index.enabled = true;

CREATE DATABASE IF NOT EXISTS axiom;
SET database = axiom;

-- =====================================================================================
-- ENUM TYPES
-- Enums rather than STRING+CHECK: 1-byte storage in every index, and the optimizer can
-- prove a partial-index predicate like `state IN ('READY', ...)` is implied, which keeps
-- the claim scan index-only. Cost: adding a state is a schema change. That is the right
-- trade for a state machine whose whole value is that it cannot drift.
-- =====================================================================================

CREATE TYPE mission_state AS ENUM (
    'PLANNING', 'RUNNING', 'PAUSED', 'SUCCEEDED', 'FAILED', 'CANCELLED'
);

CREATE TYPE task_state AS ENUM (
    'PENDING',            -- created, not runnable yet (blocked on a dependency)
    'READY',              -- runnable now, claimable
    'LEASED',             -- owned by a worker; NO external effect is authorized
    'AWAITING_APPROVAL',  -- parked on a human decision; lease intentionally released
    'ACTION_PREPARED',    -- receipt committed; EXTERNAL EFFECT AUTHORIZED / possibly in flight
    'SUCCEEDED',          -- terminal
    'FAILED',             -- terminal, business failure
    'CANCELLED',          -- terminal, operator/mission cancellation
    'DEAD_LETTER'         -- terminal, attempts exhausted or unsafe to continue
);

CREATE TYPE attempt_state AS ENUM (
    'PREPARED',          -- receipt durable, key minted; a call MAY now go out
    'DISPATCHED',        -- best-effort observability marker; SAFETY-EQUIVALENT to PREPARED
    'SUCCEEDED',         -- provider confirmed
    'FAILED_RETRYABLE',  -- provider said "try again" (5xx, timeout, rate limit)
    'FAILED_TERMINAL',   -- provider definitively rejected THIS request
    'ABANDONED',         -- proven no effect; step freed for a new step_seq
    'COMPENSATED'        -- a settled effect was later reversed by a compensating task
);

CREATE TYPE approval_state AS ENUM (
    'PENDING', 'APPROVED', 'REJECTED', 'EXPIRED', 'CANCELLED'
);

CREATE TYPE agent_status AS ENUM (
    'STARTING', 'ALIVE', 'DRAINING', 'DEAD'
);

CREATE TYPE policy_status AS ENUM (
    'DRAFT', 'ACTIVE', 'RETIRED'
);

-- =====================================================================================
-- TENANTS
-- =====================================================================================

CREATE TABLE axiom_tenant (
    id          UUID        NOT NULL DEFAULT gen_random_uuid(),
    slug        STRING      NOT NULL,
    display_name STRING     NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT axiom_tenant_pkey PRIMARY KEY (id),
    CONSTRAINT axiom_tenant_slug_uniq UNIQUE (slug)
);

-- The reserved SYSTEM tenant. Shared infrastructure (the worker pool) belongs to it, so
-- that tenant_id can be NOT NULL on EVERY table without inventing a nullable exception.
-- A nullable tenant_id is how cross-tenant leaks happen: a forgotten IS NULL branch.
INSERT INTO axiom_tenant (id, slug, display_name)
VALUES ('00000000-0000-0000-0000-000000000000', 'system', 'AXIOM system tenant')
ON CONFLICT (id) DO NOTHING;

-- =====================================================================================
-- MISSIONS  — the unit of intent ("resolve today's 30 order exceptions")
-- =====================================================================================

CREATE TABLE axiom_mission (
    id              UUID          NOT NULL DEFAULT gen_random_uuid(),
    tenant_id       UUID          NOT NULL REFERENCES axiom_tenant(id),

    title           STRING        NOT NULL,
    goal            STRING        NOT NULL,
    state           mission_state NOT NULL DEFAULT 'PLANNING',

    -- HARD SPEND CAP. This is deliberately a shared, contended row: every refund
    -- PREPARE increments spent_cents in the SAME transaction that mints the
    -- idempotency key, and the CHECK below cannot be evaded by any interleaving.
    -- Under SERIALIZABLE, two workers racing to spend the last $50 cannot both win —
    -- one gets a 40001 and re-reads. We accept the contention consciously: a budget IS
    -- a shared resource, and serializing on it is the point, not a bug. If this ever
    -- becomes the bottleneck the standard fix is a sharded counter (N sub-budget rows
    -- summed), which trades exactness at the boundary for throughput. At 30 tasks it
    -- is free, and "we chose to serialize here" is a better answer than an
    -- eventually-consistent budget that lets you overspend.
    budget_cents    INT8          NOT NULL DEFAULT 0,
    spent_cents     INT8          NOT NULL DEFAULT 0,

    created_by      STRING        NOT NULL,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),

    -- Random UUID PK, not (tenant_id, ...): a leading tenant_id would funnel every
    -- write for the busiest tenant into a single range. See H1/H4.
    CONSTRAINT axiom_mission_pkey PRIMARY KEY (id),
    CONSTRAINT axiom_mission_budget_ck CHECK (spent_cents >= 0 AND spent_cents <= budget_cents)
);

CREATE INDEX axiom_mission_by_tenant
    ON axiom_mission (tenant_id, created_at DESC);

-- =====================================================================================
-- TASKS  — the EXECUTION memory class: the durable state machine
-- =====================================================================================

CREATE TABLE axiom_task (
    id              UUID          NOT NULL DEFAULT gen_random_uuid(),
    tenant_id       UUID          NOT NULL REFERENCES axiom_tenant(id),
    mission_id      UUID          NOT NULL REFERENCES axiom_mission(id),

    task_type       STRING        NOT NULL,   -- 'refund' | 'reship' | 'escalate' | ...

    -- Business identity of the work. UNIQUE per tenant (index below), so the planner
    -- physically cannot enqueue the same real-world exception twice. This is the first
    -- line of defence against double refunds and it costs nothing.
    dedupe_key      STRING        NOT NULL,   -- e.g. 'order:1042:refund'

    -- CLAIM SHARD. Computed, not application-supplied, so it cannot drift or be
    -- forgotten; stable for the life of the row because both inputs are immutable.
    -- Explicit column rather than USING HASH (see H3) so a worker can be pinned to a
    -- shard subset. fnv32 + datums_to_bytes is exactly what Cockroach's own
    -- hash-sharded indexes use internally.
    shard           INT2          NOT NULL AS (
        mod(fnv32(crdb_internal.datums_to_bytes(tenant_id::STRING || ':' || dedupe_key)), 16)::INT2
    ) STORED,

    state           task_state    NOT NULL DEFAULT 'READY',

    -- ONE COLUMN, TWO JOBS: earliest-run-time AND lease expiry.
    --   claim      -> available_at = now() + lease_duration
    --   heartbeat  -> pushes it forward
    --   backoff    -> now() + backoff_interval on a retryable failure
    --   park       -> approval expiry while AWAITING_APPROVAL
    -- The claim predicate `available_at <= now()` therefore means "ready to run OR the
    -- owner is dead", with no reaper process. That matters: a reaper is a periodic
    -- LARGE multi-row transaction that competes with claims for exactly the rows
    -- claims want. No reaper, no second index, self-healing.
    available_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),

    -- FENCING TOKEN. Monotonic PER ROW, so it is not a global sequence and creates no
    -- hotspot. Every write after the claim re-checks it. This — not the lease — is the
    -- correctness guarantee: a lease expiring does not stop a GC-paused worker that is
    -- already inside a refund HTTP call. The lease is an optimization; the fence is the
    -- invariant.
    lease_epoch     INT8          NOT NULL DEFAULT 0,
    lease_owner     UUID,                     -- axiom_agent.id; soft ref, see note below

    attempt         INT4          NOT NULL DEFAULT 0,
    max_attempts    INT4          NOT NULL DEFAULT 5,

    -- Governing procedural memory, pinned at claim time so the whole attempt is judged
    -- against ONE policy version even if a new one is published mid-flight.
    policy_id       STRING,
    policy_version  INT4,

    payload         JSONB         NOT NULL DEFAULT '{}'::JSONB,
    result          JSONB,
    last_error      STRING,

    -- Saga link: a compensating task points at the task whose effect it reverses.
    compensates_task_id UUID,

    created_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),

    CONSTRAINT axiom_task_pkey PRIMARY KEY (id),
    CONSTRAINT axiom_task_attempt_ck CHECK (attempt >= 0 AND max_attempts > 0),
    -- Structural guarantee: a task can only be in a lease-holding state if it actually
    -- holds a lease. Makes "orphaned with no owner" unrepresentable.
    CONSTRAINT axiom_task_lease_ck CHECK (
        (state IN ('LEASED', 'ACTION_PREPARED')) = (lease_owner IS NOT NULL)
    )
    -- NOTE: no FAMILY declarations, ever. See H5.
);

-- No two tasks for the same real-world exception. Enforced, not hoped for.
CREATE UNIQUE INDEX axiom_task_dedupe
    ON axiom_task (tenant_id, dedupe_key);

-- ===== THE ONLY INDEX THE CLAIM LOOP TOUCHES =====
-- PARTIAL: terminal tasks fall OUT of it. This is the direct, literal answer to the
-- hotspot doc's warning about "an ordered set of garbage data behind the live data" —
-- our claim index shrinks as work completes instead of growing forever, and because we
-- transition state instead of DELETEing, there are no tombstones either.
-- STORING: keeps the inner SELECT index-only, so `attempt < max_attempts` can be
-- evaluated without an index join back to the primary index. That matters beyond
-- latency: the optimizer refuses to push LIMIT into an index join when SKIP LOCKED is
-- in play, so a covering index is what makes the optional SKIP LOCKED path viable too.
-- payload is deliberately NOT stored: a fat JSONB in the claim index doubles storage,
-- and returning it would push the claim result toward the 16 KiB results-buffer
-- ceiling past which CockroachDB can no longer auto-retry the statement server-side.
CREATE INDEX axiom_task_claimable
    ON axiom_task (shard ASC, available_at ASC)
    STORING (state, tenant_id, mission_id, lease_epoch, lease_owner, attempt, max_attempts, task_type)
    WHERE state IN ('READY', 'LEASED', 'ACTION_PREPARED', 'AWAITING_APPROVAL');

-- Operator/dashboard access path. Also partial, for the same reason.
CREATE INDEX axiom_task_by_mission
    ON axiom_task (tenant_id, mission_id, state);

-- =====================================================================================
-- AGENTS  — worker registry, heartbeat, shard assignment
-- Shared pool rows live under the SYSTEM tenant (H4).
-- =====================================================================================

CREATE TABLE axiom_agent (
    id              UUID          NOT NULL DEFAULT gen_random_uuid(),
    tenant_id       UUID          NOT NULL REFERENCES axiom_tenant(id),

    worker_ref      STRING        NOT NULL,   -- ECS task ARN, or 'local-<hex>' in dev
    kind            STRING        NOT NULL DEFAULT 'worker',  -- worker|planner|auditor
    status          agent_status  NOT NULL DEFAULT 'STARTING',

    -- Static work partitioning across the pool. Empty array = "steal from all shards".
    shards          INT2[]        NOT NULL DEFAULT ARRAY[]::INT2[],

    -- Each agent updates ONLY its own row, so heartbeats generate exactly zero
    -- cross-worker contention. There is no shared "workers" row to hammer, which is
    -- the usual way a heartbeat design ends up being the hotspot it was meant to avoid.
    heartbeat_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),

    build_sha       STRING,
    region          STRING,
    capabilities    JSONB         NOT NULL DEFAULT '{}'::JSONB,
    started_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),
    stopped_at      TIMESTAMPTZ,

    CONSTRAINT axiom_agent_pkey PRIMARY KEY (id),
    CONSTRAINT axiom_agent_ref_uniq UNIQUE (tenant_id, worker_ref)
);

-- Liveness view for the dashboard. Partial so dead agents age out of the hot index.
CREATE INDEX axiom_agent_live
    ON axiom_agent (heartbeat_at DESC)
    WHERE status IN ('STARTING', 'ALIVE', 'DRAINING');

-- axiom_task.lease_owner is intentionally NOT a foreign key to axiom_agent.
-- WHY: an FK would force the lease path to touch a second table's range on every claim,
-- and — worse — it would make agent-row cleanup a cascading operation over the hot task
-- table. Task ownership is proven by lease_epoch, which is self-contained in the task
-- row. Cockroach's own recommended queue design (ajwerner) uses
-- `items.claim REFERENCES sessions(id) ON DELETE SET NULL` so a single
-- `DELETE FROM sessions WHERE heartbeated_at < ...` un-claims everything atomically;
-- we evaluated that and chose self-expiring leases instead, because the FK cascade is a
-- large multi-row transaction that lands on exactly the rows the claim loop is scanning.

-- =====================================================================================
-- ACTION ATTEMPTS  — the idempotency receipt / outbox.
-- One row per (task, step, step_seq). This table is the reason a crash cannot refund
-- twice, and the reason we can say what happened in every crash window.
-- =====================================================================================

CREATE TABLE axiom_action_attempt (
    id              UUID          NOT NULL DEFAULT gen_random_uuid(),
    tenant_id       UUID          NOT NULL REFERENCES axiom_tenant(id),
    task_id         UUID          NOT NULL REFERENCES axiom_task(id),

    step_name       STRING        NOT NULL,           -- 'refund' | 'notify' | ...
    -- step_seq bumps ONLY on an explicit, journalled decision that a genuinely NEW
    -- external call is required (e.g. the provider terminally rejected the previous
    -- request body). It never bumps on a retry of the SAME logical call. Every bump is
    -- an event in axiom_event, so "we deliberately called the provider a second time"
    -- is always auditable.
    step_seq        INT4          NOT NULL DEFAULT 1,

    -- ===== THE IDEMPOTENCY KEY, MADE UNFORGEABLE BY THE SCHEMA =====
    -- A generated STORED column, not an application-supplied value. The single most
    -- lethal bug in this class of system is a key derived at call time
    -- (gen_random_uuid(), a timestamp, worker_id, attempt, or lease_epoch): the
    -- recovering worker then mints a DIFFERENT key, the provider sees a brand-new
    -- request, and the $300 goes out twice — the exact failure this project exists to
    -- prevent. Making it computed removes the possibility from the codebase rather
    -- than from the code review. Inputs are all immutable columns of this row.
    idempotency_key STRING        NOT NULL AS (
        'axm_' || substring(
            sha256(tenant_id::STRING || ':' || task_id::STRING || ':' || step_name || ':' || step_seq::STRING)
            FROM 1 FOR 48)
    ) STORED,

    attempt_state   attempt_state NOT NULL DEFAULT 'PREPARED',

    provider        STRING        NOT NULL,           -- 'stripe' | 'shipping' | ...
    operation       STRING        NOT NULL,           -- 'refunds.create'
    amount_cents    INT8,
    currency        STRING(3),

    -- SHA-256 of the canonicalized request body. Guards the "semantic rollback attack"
    -- (ACRFence, arXiv:2603.20625): after a restart an LLM re-synthesizes a subtly
    -- DIFFERENT request. Same key + different fingerprint is not a retry, it is a new
    -- intent wearing an old key. Detecting it is a hard stop, not a warning.
    request_fingerprint STRING    NOT NULL,
    request_body    JSONB         NOT NULL,

    response_body   JSONB,
    provider_ref    STRING,                            -- e.g. Stripe re_...
    http_status     INT2,

    -- The fence in force when this receipt was minted. A settle written under a stale
    -- epoch is rejected, which is what stops a zombie worker overwriting the result of
    -- the worker that legitimately took over.
    lease_epoch     INT8          NOT NULL,
    prepared_by     UUID          NOT NULL,            -- axiom_agent.id

    -- Governance / rollback traceability: which memory and which policy version
    -- authorized this irreversible act. If a memory is later found to be poisoned, this
    -- column is how we enumerate every real-world effect it licensed.
    licensed_by_memory_id UUID,
    policy_id       STRING,
    policy_version  INT4,

    prepared_at     TIMESTAMPTZ   NOT NULL DEFAULT now(),
    dispatched_at   TIMESTAMPTZ,                       -- observability ONLY, see below
    settled_at      TIMESTAMPTZ,

    CONSTRAINT axiom_action_attempt_pkey PRIMARY KEY (id),
    -- One receipt per logical step invocation.
    CONSTRAINT axiom_attempt_step_uniq UNIQUE (tenant_id, task_id, step_name, step_seq),
    -- Global dedupe. Redundant with the above given a correct derivation — which is
    -- precisely why it is here: it is the assertion that the derivation IS correct.
    CONSTRAINT axiom_attempt_key_uniq  UNIQUE (tenant_id, idempotency_key),
    CONSTRAINT axiom_attempt_settled_ck CHECK (
        (attempt_state IN ('PREPARED', 'DISPATCHED')) = (settled_at IS NULL)
    )
);

-- ===== AT MOST ONE IN-FLIGHT EXTERNAL CALL PER STEP, ENFORCED BY THE DATABASE =====
-- Two workers cannot both hold a live receipt for the same (task, step). The loser of
-- the race gets 23505, not a second refund. Terminal rows fall out of the index, so a
-- legitimate new step_seq after a terminal rejection is still allowed.
CREATE UNIQUE INDEX axiom_attempt_one_live
    ON axiom_action_attempt (tenant_id, task_id, step_name)
    WHERE attempt_state IN ('PREPARED', 'DISPATCHED');

-- Recovery's receipt lookup: point-read by task. Partial index over unsettled receipts
-- doubles as the reconciliation worklist ("what might be in flight right now").
CREATE INDEX axiom_attempt_unsettled
    ON axiom_action_attempt (tenant_id, prepared_at ASC)
    STORING (task_id, step_name, step_seq, provider, provider_ref, amount_cents)
    WHERE attempt_state IN ('PREPARED', 'DISPATCHED');

CREATE INDEX axiom_attempt_by_task
    ON axiom_action_attempt (tenant_id, task_id, step_name, step_seq);

-- Enumerate every effect a given memory licensed — the query you run the moment you
-- discover a memory was poisoned.
CREATE INDEX axiom_attempt_by_license
    ON axiom_action_attempt (tenant_id, licensed_by_memory_id)
    WHERE licensed_by_memory_id IS NOT NULL;

-- =====================================================================================
-- EVENT LOG  — append-only journal. Never updated, never deleted.
-- =====================================================================================

CREATE TABLE axiom_event (
    id              UUID          NOT NULL DEFAULT gen_random_uuid(),
    tenant_id       UUID          NOT NULL REFERENCES axiom_tenant(id),

    -- Uniform subject addressing keeps one journal for the whole system instead of
    -- five per-entity logs that have to be merged to answer any real question.
    subject_type    STRING        NOT NULL,   -- mission|task|attempt|memory|policy|approval|agent
    subject_id      UUID          NOT NULL,

    -- Gap-free per-subject sequence. Computed in the same transaction as the write:
    --   coalesce((SELECT max(seq) FROM axiom_event WHERE subject_id = $1), 0) + 1
    -- Not a global sequence (that would be a hotspot) and not unique_rowid() (gaps make
    -- "did we lose an event?" unanswerable). Contention is per-subject only, and the
    -- fencing token already serializes writers to a subject.
    seq             INT8          NOT NULL,

    event_type      STRING        NOT NULL,
    from_state      STRING,
    to_state        STRING,

    actor           STRING        NOT NULL,   -- 'agent:<uuid>' | 'human:<email>' | 'system'
    lease_epoch     INT8,

    mission_id      UUID,                     -- denormalized for cheap filtering
    task_id         UUID,
    attempt_id      UUID,

    detail          JSONB         NOT NULL DEFAULT '{}'::JSONB,
    occurred_at     TIMESTAMPTZ   NOT NULL DEFAULT now(),

    CONSTRAINT axiom_event_pkey PRIMARY KEY (id),
    CONSTRAINT axiom_event_seq_uniq UNIQUE (tenant_id, subject_type, subject_id, seq)
);

-- Per-subject replay, in order. No hotspot: subject_id is a random UUID, so writes
-- scatter across the keyspace even though seq is ordered within a subject.
CREATE INDEX axiom_event_replay
    ON axiom_event (tenant_id, subject_type, subject_id, seq ASC);

-- THE GLOBAL AUDIT TIMELINE — the one genuinely monotonic index in this schema, and
-- therefore the one place USING HASH belongs (H3). Without it, every event in the
-- cluster appends to a single range. bucket_count is kept at or below the node count;
-- above that the docs note diminishing returns, and the cost is a range scan that must
-- hit and merge every bucket.
CREATE INDEX axiom_event_timeline
    ON axiom_event (occurred_at DESC)
    USING HASH WITH (bucket_count = 8);

-- =====================================================================================
-- POLICIES  — PROCEDURAL memory, versioned and signable.
--
-- Terminology note, stated deliberately: CoALA (arXiv:2309.02427) defines procedural
-- memory as LLM weights plus agent source code. AXIOM departs from that and uses
-- "procedural" in the MemP / Voyager skill-library / Agent-Workflow-Memory sense —
-- explicit, versioned, deprecable operating procedure. Flagging the departure rather
-- than quietly redefining a cited term.
-- =====================================================================================

CREATE TABLE axiom_policy (
    tenant_id       UUID          NOT NULL REFERENCES axiom_tenant(id),
    policy_id       STRING        NOT NULL,          -- 'refund_authority'
    version         INT4          NOT NULL,

    status          policy_status NOT NULL DEFAULT 'DRAFT',
    body            JSONB         NOT NULL,

    -- Materialized out of body because the state machine consults it on the hot path
    -- and JSONB extraction in a CHECK/predicate is neither cheap nor indexable.
    max_auto_action_cents INT8    NOT NULL DEFAULT 0,
    requires_approval     BOOL    NOT NULL DEFAULT false,

    -- A signed policy is the highest trust tier. Verifying the signature is what lets a
    -- policy outrank a memory: recency does not win, provenance does.
    content_sha256  STRING        NOT NULL,
    signature       STRING,
    signed_by       STRING,

    effective_from  TIMESTAMPTZ   NOT NULL DEFAULT now(),
    effective_until TIMESTAMPTZ,

    created_by      STRING        NOT NULL,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),

    -- Leading tenant_id in the PK is CORRECT here and wrong on axiom_task. Policies are
    -- a tiny, cold, read-mostly table written by humans a handful of times; there is no
    -- write rate to spread, and co-locating a tenant's policies gives a single-range
    -- point read on the hot lookup path. The anti-pattern is a leading low-cardinality
    -- column on a WRITE-HEAVY table, not on any table.
    CONSTRAINT axiom_policy_pkey PRIMARY KEY (tenant_id, policy_id, version),
    CONSTRAINT axiom_policy_version_ck CHECK (version > 0)
);

-- EXACTLY ONE ACTIVE VERSION per (tenant, policy). Activating v3 without retiring v2 is
-- a 23505, not a silently ambiguous authority model. This is an invariant a test can
-- assert by trying to violate it.
CREATE UNIQUE INDEX axiom_policy_one_active
    ON axiom_policy (tenant_id, policy_id)
    WHERE status = 'ACTIVE';

-- =====================================================================================
-- MEMORIES  — EPISODIC + SEMANTIC, vector indexed, with full provenance.
-- =====================================================================================

CREATE TABLE axiom_memory (
    id              UUID          NOT NULL DEFAULT gen_random_uuid(),
    tenant_id       UUID          NOT NULL REFERENCES axiom_tenant(id),

    memory_class    STRING        NOT NULL,  -- 'EPISODIC' | 'SEMANTIC'

    -- Namespaced retrieval key, and a VECTOR INDEX PREFIX COLUMN. Convention:
    --   'state:ACTION_PREPARED'      an agent died at this execution state
    --   'exception:duplicate_charge' a class of business situation
    -- A prefix column must be pinned to an exact value for the index to be used at all
    -- ("Index acceleration with filters is only supported if the filters match prefix
    -- columns"), so this is one column with a convention rather than several optional
    -- ones.
    context_key     STRING        NOT NULL,

    content         STRING        NOT NULL,           -- the text that was embedded
    content_sha256  STRING        NOT NULL,

    embedding       VECTOR(1024)  NOT NULL,           -- Titan Text Embeddings V2, 1024-d
    embedding_model STRING        NOT NULL DEFAULT 'amazon.titan-embed-text-v2:0',
    embedding_dims  INT2          NOT NULL DEFAULT 1024,
    embedding_normalized BOOL     NOT NULL DEFAULT true,

    -- What actually happened. The recovery decision aggregates over THIS column, so it
    -- is a constrained vocabulary, not free text — a memory can only vote in ways the
    -- state machine understands.
    outcome         STRING        NOT NULL DEFAULT 'UNKNOWN',
    resolution      JSONB         NOT NULL DEFAULT '{}'::JSONB,

    -- ===== PROVENANCE =====
    source          STRING        NOT NULL,   -- 'system:execution'|'human:operator'|'tool:stripe'|'ingest:email'
    source_ref      STRING,

    -- 3 signed policy / verified operator, 2 first-party execution outcome,
    -- 1 tool output, 0 untrusted third-party text.
    -- Ordered so the tier comparison is a simple >=. Note it is folded into
    -- retrieval_class below rather than used as a range predicate at query time,
    -- because a range predicate on a prefix column disables the vector index entirely.
    trust_level     INT2          NOT NULL DEFAULT 1,

    confidence      FLOAT8        NOT NULL DEFAULT 1.0,

    -- VALID TIME (when the fact is true in the world). Distinct from transaction time,
    -- which MVCC gives us for free via AS OF SYSTEM TIME. AOST is bounded by
    -- gc.ttlseconds and yields a READ-ONLY transaction, so it can never be the durable
    -- audit axis — these columns are.
    valid_from      TIMESTAMPTZ   NOT NULL DEFAULT now(),
    valid_until     TIMESTAMPTZ,

    -- Supersession chain. Nothing is ever deleted; superseding a memory is a write, and
    -- under SERIALIZABLE two writers cannot fork the chain (see the invariant tests).
    supersedes      UUID          REFERENCES axiom_memory(id),
    superseded_by   UUID          REFERENCES axiom_memory(id),
    superseded_at   TIMESTAMPTZ,

    -- Quarantine (memory-poisoning defence). Adopting a known defence — CaMeL
    -- (arXiv:2503.18813), SMSR (arXiv:2606.12703), OWASP Agentic T1 — and moving its
    -- enforcement point from the prompt to the index.
    quarantined     BOOL          NOT NULL DEFAULT false,
    quarantined_at  TIMESTAMPTZ,
    quarantined_by  STRING,
    quarantine_reason STRING,

    -- ===== THE ADMISSIBILITY GATE, ENFORCED BY THE INDEX ITSELF =====
    -- A STORED computed column used as a VECTOR INDEX PREFIX. Because the recovery
    -- query pins retrieval_class = 'ACTIONABLE', a quarantined or superseded or
    -- low-trust memory is not merely filtered out of the results — it is in a different
    -- partition of the index and never enters the ANN candidate set at all.
    --
    -- This matters for CORRECTNESS, not tidiness. Post-filtering an ANN result is a
    -- documented trap: the vector search returns `target count` candidates, your WHERE
    -- then discards some, and you silently get FEWER than LIMIT rows and miss true
    -- nearest neighbours. In a memory system that is a wrong answer, not a slow query.
    --
    -- Consequence worth stating out loud: `UPDATE axiom_memory SET quarantined = true`
    -- changes a prefix column, so the row MOVES in the vector index inside that
    -- transaction. Quarantine takes effect at commit, atomically, for every subsequent
    -- retrieval. There is no reindex, no cache, no eventual anything.
    --
    -- Only valid_from/valid_until remain as post-ANN filters, because they are
    -- time-varying and a computed column cannot call now(). The recovery query
    -- compensates by over-fetching (LIMIT k*4) before applying them.
    retrieval_class STRING        NOT NULL AS (
        CASE
            WHEN quarantined                THEN 'QUARANTINED'
            WHEN superseded_by IS NOT NULL   THEN 'SUPERSEDED'
            WHEN trust_level >= 2            THEN 'ACTIONABLE'
            ELSE                                  'ADVISORY'
        END
    ) STORED,

    -- Where this memory came from in the execution world.
    mission_id      UUID,
    task_id         UUID,
    attempt_id      UUID,
    created_by_agent_id UUID,
    policy_id       STRING,
    policy_version  INT4,

    occurred_at     TIMESTAMPTZ   NOT NULL DEFAULT now(),
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),

    CONSTRAINT axiom_memory_pkey PRIMARY KEY (id),
    CONSTRAINT axiom_memory_class_ck   CHECK (memory_class IN ('EPISODIC', 'SEMANTIC')),
    CONSTRAINT axiom_memory_trust_ck   CHECK (trust_level BETWEEN 0 AND 3),
    CONSTRAINT axiom_memory_conf_ck    CHECK (confidence >= 0.0 AND confidence <= 1.0),
    CONSTRAINT axiom_memory_valid_ck   CHECK (valid_until IS NULL OR valid_until > valid_from),
    CONSTRAINT axiom_memory_super_ck   CHECK ((superseded_by IS NULL) = (superseded_at IS NULL)),
    CONSTRAINT axiom_memory_quar_ck    CHECK ((quarantined = false) = (quarantined_at IS NULL)),
    -- Constrained vocabulary: the recovery decision aggregates over this.
    CONSTRAINT axiom_memory_outcome_ck CHECK (outcome IN (
        'RESOLVED',            -- replay/resume worked cleanly
        'NO_EFFECT',           -- proven the external call never landed
        'DUPLICATE_EFFECT',    -- a double side effect actually occurred
        'PROVIDER_AMBIGUOUS',  -- provider state could not be determined
        'HUMAN_REQUIRED',      -- resolved only by escalation
        'UNKNOWN'
    )),

    -- ===== VECTOR INDEX 1: the recovery path =====
    -- "What happened last time an agent died at THIS state, among memories we are
    -- allowed to act on?" All four prefix columns are pinned to exact values by the
    -- recovery query, which is the only way the index is used.
    VECTOR INDEX axiom_memory_ann_by_context
        (tenant_id, memory_class, context_key, retrieval_class, embedding vector_cosine_ops)
        WITH (min_partition_size = 16, max_partition_size = 128),

    -- ===== VECTOR INDEX 2: broad semantic recall =====
    -- "What past situations resemble this new exception?" — no context_key pin.
    -- Two vector indexes on one column doubles vector write cost; that is an accepted,
    -- measured trade. (Requires v25.3+: a v25.2 bug could drop user-supplied filters
    -- when the same vector column was indexed twice.)
    VECTOR INDEX axiom_memory_ann_by_tenant
        (tenant_id, memory_class, retrieval_class, embedding vector_cosine_ops)
        WITH (min_partition_size = 16, max_partition_size = 128)
);

-- vector_cosine_ops is written EXPLICITLY above. Omitting the opclass silently gives you
-- vector_l2_ops (the default); a `<=>` query then ignores the index and full-scans,
-- which looks perfect on 200 demo rows and collapses at scale. Titan V2 output is
-- normalized, so cosine is the right metric. Verify with EXPLAIN: the plan must contain
-- a `vector search` node with `prefix spans`, not a `scan`.

CREATE INDEX axiom_memory_by_task
    ON axiom_memory (tenant_id, task_id, occurred_at DESC)
    WHERE task_id IS NOT NULL;

-- The chain-head query used by the supersession invariant test.
CREATE INDEX axiom_memory_current
    ON axiom_memory (tenant_id, context_key, valid_from DESC)
    WHERE superseded_by IS NULL AND quarantined = false;

-- Back-reference from a receipt to the memory that licensed it. Added after both tables
-- exist because the two tables reference each other.
ALTER TABLE axiom_action_attempt
    ADD CONSTRAINT axiom_attempt_licensed_fk
    FOREIGN KEY (licensed_by_memory_id) REFERENCES axiom_memory(id);

-- =====================================================================================
-- APPROVALS  — human-in-the-loop gate
-- =====================================================================================

CREATE TABLE axiom_approval (
    id              UUID           NOT NULL DEFAULT gen_random_uuid(),
    tenant_id       UUID           NOT NULL REFERENCES axiom_tenant(id),
    task_id         UUID           NOT NULL REFERENCES axiom_task(id),
    mission_id      UUID           NOT NULL REFERENCES axiom_mission(id),

    step_name       STRING         NOT NULL,
    state           approval_state NOT NULL DEFAULT 'PENDING',

    reason          STRING         NOT NULL,   -- why the machine refused to self-authorize
    proposed_action JSONB          NOT NULL,
    proposed_amount_cents INT8,
    risk            JSONB          NOT NULL DEFAULT '{}'::JSONB,

    -- The evidence the human is being asked to rule on. Pinning it means the approval
    -- is auditable even after the underlying memory is superseded or quarantined.
    evidence_memory_ids UUID[]     NOT NULL DEFAULT ARRAY[]::UUID[],
    policy_id       STRING,
    policy_version  INT4,

    requested_by    UUID           NOT NULL,   -- axiom_agent.id
    requested_at    TIMESTAMPTZ    NOT NULL DEFAULT now(),

    -- Self-healing park: the task's available_at is set to this, so an approval nobody
    -- answers is reclaimed by a worker and resolved (escalate again, or fail) instead of
    -- sitting forever. No approval-expiry cron.
    expires_at      TIMESTAMPTZ    NOT NULL,

    -- Single-use capability. The worker must present this exact token to advance past
    -- the gate, so a human decision cannot be replayed to authorize a second action.
    decision_token  UUID           NOT NULL DEFAULT gen_random_uuid(),
    token_consumed_at TIMESTAMPTZ,

    decided_by      STRING,
    decided_at      TIMESTAMPTZ,
    decision_note   STRING,

    CONSTRAINT axiom_approval_pkey PRIMARY KEY (id),
    CONSTRAINT axiom_approval_decided_ck CHECK (
        (state IN ('PENDING', 'EXPIRED', 'CANCELLED')) = (decided_at IS NULL)
    ),
    CONSTRAINT axiom_approval_token_ck CHECK (
        token_consumed_at IS NULL OR state = 'APPROVED'
    )
);

-- At most one open question per (task, step). Two agents cannot both raise the same
-- approval and get two different humans to answer it differently.
CREATE UNIQUE INDEX axiom_approval_one_pending
    ON axiom_approval (tenant_id, task_id, step_name)
    WHERE state = 'PENDING';

-- The human's inbox, and the expiry sweep. Partial: answered approvals leave the index.
CREATE INDEX axiom_approval_queue
    ON axiom_approval (tenant_id, expires_at ASC)
    STORING (task_id, step_name, proposed_amount_cents, reason)
    WHERE state = 'PENDING';

-- =====================================================================================
-- OPTIONAL: row-level security.
-- Left commented ON PURPOSE. RLS is the right long-term tenant boundary, but a
-- misconfigured FORCE RLS returns ZERO ROWS SILENTLY rather than erroring, which is the
-- worst possible failure mode to discover during a live demo. Until there is a test that
-- asserts a cross-tenant read is denied AND an in-tenant read still returns rows, the
-- boundary here is: tenant_id NOT NULL on every table, tenant_id as the leading column
-- of every access-path index, and a mandatory tenant predicate in every query.
--
-- ALTER TABLE axiom_task ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY axiom_task_tenant ON axiom_task
--     USING (tenant_id = current_setting('axiom.tenant_id')::UUID);
-- =====================================================================================
