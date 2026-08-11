#!/usr/bin/env bash
# AXIOM :: start the API.
#
#   scripts/run_api.sh                 # localhost:8000, offline embeddings/LLM
#   AXIOM_OFFLINE=0 scripts/run_api.sh # use Bedrock for real
#   PORT=9000 scripts/run_api.sh --reload
#
# Anything after the script name is passed straight through to uvicorn.
#
# WHY the defaults are what they are
# ----------------------------------
# * AXIOM_OFFLINE defaults to 1. The API embeds on every /api/memories/recall, and a
#   demo that silently spends Bedrock tokens on page load is a demo nobody leaves
#   running. Set it to 0 for the recorded run — the engine cannot tell the difference,
#   which is the whole point of the provider seam.
# * The pool is sized above uvicorn's default threadpool pressure. Handlers are blocking
#   `def` routes on Starlette's threadpool, so concurrent requests each want a
#   connection; a pool of 1 turns a dashboard poll into a queue.
# * No --reload by default. Reload restarts the process on file changes, and the demo
#   runs beside agents that write files.

set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"

export DATABASE_URL="${DATABASE_URL:-postgresql://root@localhost:26257/axiom?sslmode=disable}"
export AXIOM_OFFLINE="${AXIOM_OFFLINE:-1}"
export AXIOM_POOL_MIN="${AXIOM_POOL_MIN:-2}"
export AXIOM_POOL_MAX="${AXIOM_POOL_MAX:-12}"
export AWS_REGION="${AWS_REGION:-us-east-1}"
export PYTHONPATH="${ROOT}${PYTHONPATH:+:$PYTHONPATH}"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"

PY="${ROOT}/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

echo "axiom api  ->  http://localhost:${PORT}"
echo "  DATABASE_URL   ${DATABASE_URL%%\?*}"
echo "  PROVIDER       $("$PY" -c 'from axiom import provider; print(provider.provider_url().split("?")[0])')"
echo "  AXIOM_OFFLINE  ${AXIOM_OFFLINE}"
echo "  web/           $([ -d "${ROOT}/web" ] && echo present || echo 'absent (placeholder page will be served)')"

exec "$PY" -m uvicorn axiom.api:app \
    --host "$HOST" --port "$PORT" \
    --log-level info --no-access-log \
    "$@"
