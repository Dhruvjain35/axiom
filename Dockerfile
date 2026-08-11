# syntax=docker/dockerfile:1.7
# =============================================================================
# AXIOM — one image, two entrypoints.
#
# The API and the worker share every line of correctness-critical code
# (axiom/tasks.py, axiom/db.py, axiom/memory.py). Two images would mean two
# things to build, two to push, two to keep in sync, and the possibility of an
# API pod and a worker pod disagreeing about the state machine. So: one image,
# and the role is chosen by CMD.
#
#   docker run ... axiom                                 # -> API on :8000
#   docker run ... axiom python -m axiom.worker          # -> worker agent
#
# Build:
#   docker build -t axiom:local .
# =============================================================================

ARG PYTHON_VERSION=3.14

# ------------------------------------------------------------------- builder --
# Wheels are resolved and installed into a standalone venv here so the final
# stage never sees pip, its cache, or any build toolchain. Every dependency in
# requirements.txt ships a manylinux wheel (psycopg[binary] is specifically
# chosen so there is no libpq/gcc layer), so this stage needs no apt packages.
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copied alone so a code change does not invalidate the dependency layer — the
# dependency install is ~30s, the code copy is ~0s, and they must not share a
# cache key.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --require-virtualenv -r /tmp/requirements.txt


# -------------------------------------------------------------------- runtime --
FROM python:${PYTHON_VERSION}-slim AS runtime

# WHY these three: unbuffered is not a preference, it is the difference between
# seeing worker logs in CloudWatch in real time and seeing them 4 KB at a time
# (which, in a demo where the worker is about to be SIGKILLed, means losing the
# last thing it said). RANDOM_SEED off keeps hash ordering non-deterministic;
# we rely on the database for ordering, never on dict order.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    AXIOM_PORT=8000

# 10001 rather than the first free uid: a fixed, high, non-overlapping uid means
# the same numeric owner on a bind mount here and on Fargate.
RUN set -eux; \
    groupadd --system --gid 10001 axiom; \
    useradd --system --uid 10001 --gid 10001 --home-dir /app --shell /usr/sbin/nologin axiom

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# Ownership is set at COPY time rather than with a later `chown -R`, which would
# duplicate the whole tree into a second layer.
COPY --chown=10001:10001 axiom/    /app/axiom/
COPY --chown=10001:10001 scripts/  /app/scripts/
COPY --chown=10001:10001 db/       /app/db/
COPY --chown=10001:10001 LICENSE README.md /app/

# Mission Control's static assets — OPTIONAL, because this image must also build from a
# checkout where the UI has not been generated yet (axiom/api.py serves a placeholder
# page in that case).
#
# `we[b]` is the optional-source idiom: a character class on a character of the NAME.
# It matches the directory `web` when it exists and matches nothing when it does not.
# Two traps, both hit while building this:
#   * the obvious-looking `web[/]` matches NOTHING EVER — build-context entries are
#     named `web`, not `web/`, so the class never has a character to match, and the
#     result is a silently empty /app/web and a UI that 404s;
#   * a COPY whose every pattern matches nothing is a hard build error, which is why
#     LICENSE is in the list. Apache-2.0 answering at /LICENSE is not a side effect
#     worth engineering around.
COPY --chown=10001:10001 LICENSE we[b] /app/web/

# Stamped by scripts/deploy.sh (--build-arg GIT_SHA=$(git rev-parse HEAD)).
# axiom/worker.py records it on the agent row, so `SELECT build_sha FROM
# axiom_agent` answers "which build issued this refund?" months later.
ARG GIT_SHA=unknown
ENV AXIOM_BUILD_SHA=${GIT_SHA}
LABEL org.opencontainers.image.title="AXIOM" \
      org.opencontainers.image.description="Durable execution memory for agents that take real actions" \
      org.opencontainers.image.source="https://github.com/Dhruvjain35/axiom" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.revision="${GIT_SHA}"

USER 10001:10001

EXPOSE 8000

# Uses the interpreter that is already here instead of adding curl(1) and its
# transitive apt closure for one HTTP GET. Fails closed: any exception is a
# non-zero exit, which is what an unhealthy container should be.
#
# This checks the API. The worker has no socket to probe — it is overridden to
# a database round-trip in docker-compose.yml and in the ECS task definition,
# because a worker whose only symptom is "cannot reach CockroachDB" should be
# replaced, not left polling.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('AXIOM_PORT','8000')+'/api/health',timeout=4).read()"]

# One uvicorn worker on purpose. Every request is a short serializable
# transaction against the pool; a second process would double the pool against
# the same cluster to serve a demo that is nowhere near CPU-bound.
CMD ["sh", "-c", "exec uvicorn axiom.api:app --host 0.0.0.0 --port ${AXIOM_PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
