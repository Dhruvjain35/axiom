"""AXIOM :: configuration.

One module, read once at import, no hidden globals mutated later. Every value that
differs between a laptop, CI, and ECS is here and nowhere else.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field

# The reserved SYSTEM tenant from db/001_schema.sql. Shared infrastructure rows (the
# worker pool) belong to it so tenant_id can be NOT NULL on every table with no
# nullable exception — a nullable tenant_id is how cross-tenant leaks happen.
SYSTEM_TENANT = uuid.UUID('00000000-0000-0000-0000-000000000000')

# Titan Text Embeddings V2. The schema pins VECTOR(1024) and the vector indexes are
# built with vector_cosine_ops, which is correct precisely because Titan V2 output is
# normalized. Changing either of these three things means changing all three.
EMBED_DIMS = 1024

# Must match the modulus in axiom_task.shard's generated expression. The value is
# duplicated between SQL and Python on purpose — the SQL is authoritative, and
# scripts/verify_invariants.py asserts the two agree rather than trusting a comment.
SHARD_COUNT = 16


def _b(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ('1', 'true', 'yes', 'on')


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: os.environ.get(
        'DATABASE_URL', 'postgresql://root@localhost:26257/axiom?sslmode=disable'))

    # --- pool -----------------------------------------------------------------
    pool_min: int = field(default_factory=lambda: _i('AXIOM_POOL_MIN', 1))
    pool_max: int = field(default_factory=lambda: _i('AXIOM_POOL_MAX', 8))

    # --- retry ----------------------------------------------------------------
    # 40001 is not an error condition in this system, it is SERIALIZABLE working.
    # Every transaction goes through db.tx(), which retries with exponential backoff
    # and full jitter. A demo that surfaces stack traces under contention reads as
    # broken even when it is behaving correctly.
    max_retries: int = field(default_factory=lambda: _i('AXIOM_MAX_RETRIES', 10))
    retry_base_ms: int = field(default_factory=lambda: _i('AXIOM_RETRY_BASE_MS', 8))
    retry_cap_ms: int = field(default_factory=lambda: _i('AXIOM_RETRY_CAP_MS', 750))

    # --- lease ----------------------------------------------------------------
    # Deliberately short. The lease is a LIVENESS optimization — how fast a dead
    # worker's task becomes claimable again — never a correctness mechanism. The
    # fencing token (axiom_task.lease_epoch) is what makes concurrent writes safe,
    # so a too-short lease costs duplicated *effort*, never a duplicated *effect*.
    lease_seconds: int = field(default_factory=lambda: _i('AXIOM_LEASE_SECONDS', 20))
    heartbeat_seconds: int = field(default_factory=lambda: _i('AXIOM_HEARTBEAT_SECONDS', 5))
    poll_idle_ms: int = field(default_factory=lambda: _i('AXIOM_POLL_IDLE_MS', 400))

    # --- recall ---------------------------------------------------------------
    recall_k: int = field(default_factory=lambda: _i('AXIOM_RECALL_K', 5))
    # Over-fetch multiplier. valid_from/valid_until cannot be folded into the
    # retrieval_class prefix column (a computed column cannot call now()), so they
    # remain post-ANN filters — and post-filtering an ANN result silently returns
    # fewer than LIMIT rows. Over-fetching by 4x and then filtering is the fix.
    recall_overfetch: int = field(default_factory=lambda: _i('AXIOM_RECALL_OVERFETCH', 4))
    beam_size: int = field(default_factory=lambda: _i('AXIOM_BEAM_SIZE', 64))

    # --- AWS ------------------------------------------------------------------
    aws_region: str = field(default_factory=lambda: os.environ.get('AWS_REGION', 'us-east-1'))
    embed_model: str = field(default_factory=lambda: os.environ.get(
        'AXIOM_EMBED_MODEL', 'amazon.titan-embed-text-v2:0'))
    llm_model: str = field(default_factory=lambda: os.environ.get(
        'AXIOM_LLM_MODEL', 'anthropic.claude-sonnet-4-5-20250929-v1:0'))

    # Offline mode swaps Bedrock for deterministic local stand-ins. Tests run offline
    # so the invariant suite is hermetic and free; the demo runs online. The engine
    # cannot tell the difference — that is the point of the provider interfaces.
    offline: bool = field(default_factory=lambda: _b('AXIOM_OFFLINE', False))

    # --- demo controls --------------------------------------------------------
    # Chaos is a first-class product feature here, not a test-only hack: the demo
    # kills workers on camera. Rates are per-dispatch probabilities.
    chaos_crash_before_dispatch: float = field(
        default_factory=lambda: float(os.environ.get('AXIOM_CHAOS_PRE', '0')))
    chaos_crash_after_dispatch: float = field(
        default_factory=lambda: float(os.environ.get('AXIOM_CHAOS_POST', '0')))
    provider_latency_ms: int = field(default_factory=lambda: _i('AXIOM_PROVIDER_LATENCY_MS', 120))

    # How a chaos-injected crash ends the process.
    #
    # True (default): os._exit(9) — no finally, no atexit, exactly what SIGKILL does.
    # False: raise, for deployments where the worker shares a process with the HTTP
    # request that started it. Killing the process there kills the caller's request, so
    # the browser sees a dead socket instead of a recovery. The task is abandoned in the
    # same durable state either way — ACTION_PREPARED with a live receipt — and that
    # state is all recovery reads.
    crash_exits: bool = field(default_factory=lambda: _b('AXIOM_CRASH_EXIT', True))


settings = Settings()
