"""AXIOM :: embeddings.

Amazon Bedrock Titan Text Embeddings V2 in production; a deterministic local stand-in
when AXIOM_OFFLINE=1.

The offline path is not a mock in the testing sense — it is a real, deterministic
embedding function. That matters because the invariant suite (tests/) exercises the
same recovery code the demo runs, including the ANN recall, and a suite that cannot run
without network access and AWS credentials is a suite that stops being run. The engine
cannot tell the two apart; both return 1024 normalized floats.

Normalization is not cosmetic. Both vector indexes are built with vector_cosine_ops,
which is the right metric precisely because Titan V2 returns unit vectors. If a future
embedding provider returns unnormalized output, either normalize it here or change the
opclass — silently mixing the two gives plausible-looking, subtly wrong neighbours.

TWO EMBEDDERS MEANS TWO VECTOR SPACES, AND THAT HAS TO BE VISIBLE
-----------------------------------------------------------------
The two paths above produce vectors that are not comparable. A cosine query against a
table holding a mixture returns rows — it returns the wrong rows, ranked confidently,
with no error anywhere. It is the worst failure mode in the system: silent, plausible,
and invisible to every test that only asserts "some memories came back".

So which space a row belongs to is recorded on the row (`axiom_memory.embedding_model`),
written explicitly from MODEL_ID rather than left to a column default, and asserted by
preflight gate 17. Before this module named its spaces, the column defaulted to
'amazon.titan-embed-text-v2:0' and every row on the demo cluster claimed to be a Titan
embedding while in fact being a blake2b sketch or a sine test fixture. Nothing was broken
by it — both sides of every comparison were the stand-in — but the database was stating
something untrue about itself, which in this project is its own kind of bug.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from functools import lru_cache

from .config import EMBED_DIMS, settings

_client_lock = threading.Lock()
_client = None

#: The identifier of the vector space these functions actually produce.
#:
#: Not a label — the value written to axiom_memory.embedding_model and the value
#: preflight compares the stored corpus against. Changing the offline algorithm without
#: changing this string is how a corpus silently becomes a mixture.
OFFLINE_MODEL_ID = 'offline-blake2b-sketch-v1'

MODEL_ID: str = OFFLINE_MODEL_ID if settings.offline else settings.embed_model


def _bedrock():
    global _client
    with _client_lock:
        if _client is None:
            import boto3  # imported lazily so offline runs need no AWS SDK config
            from botocore.config import Config
            # Bedrock is on the request path of every recall, and this deployment is
            # judged unattended for four weeks. Two retries with adaptive backoff cover
            # the throttles and transient 5xx that a long-lived demo will eventually see;
            # the short timeouts mean a hung socket surfaces in seconds rather than
            # holding a serverless invocation open until the platform kills it.
            _client = boto3.client(
                'bedrock-runtime',
                region_name=settings.aws_region,
                config=Config(retries={'max_attempts': 3, 'mode': 'adaptive'},
                              connect_timeout=4, read_timeout=12))
    return _client


def _normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    if n == 0.0:
        # A zero vector has no direction; cosine distance to it is undefined and
        # CockroachDB would happily index it. Nudge deterministically instead.
        v = [1.0] + [0.0] * (len(v) - 1)
        n = 1.0
    return [x / n for x in v]


_WORD = re.compile(r'[a-z0-9]+')


def _offline_embed(text: str) -> list[float]:
    """Deterministic bag-of-features embedding.

    Hashes each token (plus each bigram) into the vector with a sign drawn from the
    hash, then normalizes — a signed random-projection sketch. Two texts sharing tokens
    land close together, which is enough for the recall tests to assert real ranking
    behaviour rather than merely "some rows came back". Identical input always gives an
    identical vector, so a test can be exact.
    """
    toks = _WORD.findall(text.lower())
    feats = toks + [f'{a}_{b}' for a, b in zip(toks, toks[1:])]
    vec = [0.0] * EMBED_DIMS
    for f in feats:
        h = hashlib.blake2b(f.encode(), digest_size=8).digest()
        idx = int.from_bytes(h[:4], 'big') % EMBED_DIMS
        sign = 1.0 if h[4] & 1 else -1.0
        vec[idx] += sign
    if not feats:
        vec[0] = 1.0
    return _normalize(vec)


def _titan_embed(text: str) -> list[float]:
    body = json.dumps({'inputText': text, 'dimensions': EMBED_DIMS, 'normalize': True})
    resp = _bedrock().invoke_model(modelId=settings.embed_model, body=body)
    emb = json.loads(resp['body'].read())['embedding']
    if len(emb) != EMBED_DIMS:
        raise RuntimeError(
            f'{settings.embed_model} returned {len(emb)} dims, schema pins {EMBED_DIMS}')
    # Titan honours normalize=true, but asserting costs nothing and the failure mode of
    # a silently unnormalized vector under a cosine index is bad neighbours, not an error.
    return _normalize(emb)


# --------------------------------------------------------------- the durable cache

def _cache_get(sha: str) -> list[float] | None:
    """Look the vector up in axiom_embedding_cache. Best-effort, never raises.

    Why a DURABLE cache and not just the lru_cache below. Serverless instances are cold
    more often than they are warm, so the in-process cache is empty for most of the
    requests that matter — and the demo's query texts are a small fixed set that gets
    embedded over and over across four weeks of judging. More importantly it is the only
    thing standing between a Bedrock outage and a judge seeing a broken recall panel: the
    seeded vectors are already in the table, so the demonstration path keeps answering
    from CockroachDB even when Bedrock does not answer at all.

    Swallows everything. A cache that can fail the request it was added to protect is
    worse than no cache.
    """
    try:
        from . import db
        with db.pool().connection() as c, c.cursor() as cur:
            cur.execute("""SELECT embedding::STRING AS v FROM axiom_embedding_cache
                           WHERE model = %s AND text_sha256 = %s""", (MODEL_ID, sha))
            row = cur.fetchone()
        if not row:
            return None
        return [float(x) for x in row['v'].strip('[]').split(',')]
    except Exception:
        return None


def _cache_put(sha: str, text: str, vec: list[float]) -> None:
    """Store a vector for next time. Best-effort, never raises."""
    try:
        from . import db
        with db.pool().connection() as c, c.cursor() as cur:
            cur.execute("""
                INSERT INTO axiom_embedding_cache (model, text_sha256, chars, embedding)
                VALUES (%s, %s, %s, %s::VECTOR(1024))
                ON CONFLICT (model, text_sha256) DO NOTHING
            """, (MODEL_ID, sha, len(text), '[' + ','.join(repr(x) for x in vec) + ']'))
    except Exception:
        pass


@lru_cache(maxsize=4096)
def embed(text: str) -> tuple[float, ...]:
    """Embed one string. Cached twice: in process, and in CockroachDB.

    The recovery path embeds the same handful of situation descriptions repeatedly, and
    Bedrock charges per call. Returns a tuple so the lru_cache value is immutable — a
    caller that mutated a cached list would poison every later recall in the process.
    """
    text = text.strip()
    if not text:
        raise ValueError('refusing to embed empty text')

    if settings.offline:
        # Deterministic and free. Caching it durably would be pure overhead, and worse,
        # it would put stand-in vectors in a table the online path also reads.
        return tuple(_offline_embed(text))

    sha = content_sha256(text)
    hit = _cache_get(sha)
    if hit is not None and len(hit) == EMBED_DIMS:
        return tuple(hit)

    # One retry beyond botocore's own, for the case botocore does not treat as
    # retryable. Bedrock model access can also be revoked mid-run, and that failure
    # deserves to surface rather than be papered over — see the note below.
    last: Exception | None = None
    for attempt in range(2):
        try:
            v = _titan_embed(text)
            _cache_put(sha, text, v)
            return tuple(v)
        except Exception as e:  # noqa: BLE001 — re-raised below
            last = e
            if attempt == 0:
                time.sleep(0.4)

    # DELIBERATELY NOT falling back to _offline_embed. The stand-in returns a vector from
    # a different space, so the fallback would not degrade the answer — it would return
    # confident nonsense, ranked, with no error. An honest failure is the correct
    # behaviour for a system whose entire argument is that it tells the truth about what
    # it did.
    raise RuntimeError(
        f'Bedrock embedding failed for {MODEL_ID}: {last}. Refusing to substitute the '
        f'offline stand-in — it is a different vector space and would return plausible '
        f'wrong neighbours.') from last


def embed_list(text: str) -> list[float]:
    return list(embed(text))


def content_sha256(text: str) -> str:
    """Stable content hash stored alongside every memory.

    Used to prove a memory's text was not edited after the fact, and to dedupe
    identical observations without a vector comparison.
    """
    return hashlib.sha256(text.encode('utf-8')).hexdigest()
