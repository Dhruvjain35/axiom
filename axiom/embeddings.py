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
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from functools import lru_cache

from .config import EMBED_DIMS, settings

_client_lock = threading.Lock()
_client = None


def _bedrock():
    global _client
    with _client_lock:
        if _client is None:
            import boto3  # imported lazily so offline runs need no AWS SDK config
            _client = boto3.client('bedrock-runtime', region_name=settings.aws_region)
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


@lru_cache(maxsize=4096)
def embed(text: str) -> tuple[float, ...]:
    """Embed one string. Cached: the recovery path embeds the same handful of situation
    descriptions repeatedly, and Bedrock charges per call.

    Returns a tuple so the lru_cache value is immutable — a caller that mutated a cached
    list would poison every later recall in the process.
    """
    text = text.strip()
    if not text:
        raise ValueError('refusing to embed empty text')
    v = _offline_embed(text) if settings.offline else _titan_embed(text)
    return tuple(v)


def embed_list(text: str) -> list[float]:
    return list(embed(text))


def content_sha256(text: str) -> str:
    """Stable content hash stored alongside every memory.

    Used to prove a memory's text was not edited after the fact, and to dedupe
    identical observations without a vector comparison.
    """
    return hashlib.sha256(text.encode('utf-8')).hexdigest()
