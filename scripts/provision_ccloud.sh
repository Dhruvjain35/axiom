#!/usr/bin/env bash
# =============================================================================
# AXIOM :: provision the CockroachDB Cloud cluster with the ccloud CLI.
#
#   ./scripts/provision_ccloud.sh
#   AXIOM_CLUSTER=axiom-demo AXIOM_CCLOUD_REGION=us-east-1 ./scripts/provision_ccloud.sh
#   AXIOM_DRY_RUN=1 ./scripts/provision_ccloud.sh        # print the plan, touch nothing
#
# What it does, in order:
#   1. authenticates ccloud
#   2. creates (or selects) a CockroachDB Basic cluster on AWS
#   3. creates the application SQL user
#   4. applies db/001_schema.sql and db/003_provider.sql
#   5. creates the read-only audit role the Managed MCP server connects as
#   6. prints the DATABASE_URL to export
#
# Steps 4-6 use `cockroach sql --url`, not `ccloud`. `ccloud cluster sql` opens an
# interactive shell; the documented way to script against the cluster is to ask
# ccloud for the connection URL and hand it to the normal SQL client. One
# migration path, the same one docker-compose.yml uses locally.
#
# Install ccloud:  brew install cockroachdb/tap/ccloud
# Reference:       https://www.cockroachlabs.com/docs/cockroachcloud/ccloud-get-started
#                  https://www.cockroachlabs.com/docs/cockroachcloud/ccloud-reference
# =============================================================================
set -euo pipefail

CLUSTER="${AXIOM_CLUSTER:-axiom}"
REGION="${AXIOM_CCLOUD_REGION:-us-east-1}"
CLOUD="${AXIOM_CCLOUD_PROVIDER:-AWS}"
SQL_USER="${AXIOM_SQL_USER:-axiom_app}"
# db/002_audit_role.sql defines both: `axiom_auditor` is the privilege bundle,
# `axiom_audit` is the login that holds it. Only the login needs a password here.
AUDIT_USER="${AXIOM_AUDIT_USER:-axiom_audit}"
DB="${AXIOM_DB:-axiom}"
DRY_RUN="${AXIOM_DRY_RUN:-0}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# --------------------------------------------------------------------- output --
if [[ -t 1 ]]; then
  B=$'\033[1m'; DIM=$'\033[2m'; R=$'\033[0m'
else
  B=''; DIM=''; R=''
fi
say()  { printf '%s==>%s %s\n' "$B" "$R" "$*"; }
warn() { printf '%s!!%s  %s\n' "$B" "$R" "$*" >&2; }
die()  { printf '%sxx%s  %s\n' "$B" "$R" "$*" >&2; exit 1; }

# Every mutating command goes through run(). AXIOM_DRY_RUN=1 turns the script
# into a printout of exactly what it would do, which is how you review it before
# letting it near an account.
run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    printf '%s   + %s%s\n' "$DIM" "$*" "$R"
  else
    "$@"
  fi
}

# ------------------------------------------------------------- preconditions --
need() { command -v "$1" >/dev/null 2>&1 || die "missing '$1'. $2"; }

if [[ "$DRY_RUN" != "1" ]]; then
  need ccloud    "Install it: brew install cockroachdb/tap/ccloud"
  need cockroach "Install the CockroachDB binary: https://www.cockroachlabs.com/docs/releases"
  need python3   "Needed to assemble the DSN without string surgery."
fi

for f in db/001_schema.sql db/002_audit_role.sql db/003_provider.sql; do
  [[ -f "$REPO_ROOT/$f" ]] || die "cannot find $f — run this from the AXIOM repo."
done

# ----------------------------------------------------------------------- auth --
say "authenticating"
if [[ "$DRY_RUN" == "1" ]]; then
  run ccloud auth whoami
elif ccloud auth whoami >/dev/null 2>&1; then
  echo "    already logged in as $(ccloud auth whoami 2>/dev/null | head -1)"
else
  # Opens a browser. On a headless box use --no-redirect, which prints a URL to
  # paste into a browser elsewhere.
  run ccloud auth login ${AXIOM_CCLOUD_ORG:+--org "$AXIOM_CCLOUD_ORG"}
fi

# -------------------------------------------------------------------- cluster --
say "cluster '$CLUSTER' ($CLOUD / $REGION)"
if [[ "$DRY_RUN" == "1" ]]; then
  run ccloud cluster list
  run ccloud cluster create basic "$CLUSTER" "$REGION" --cloud "$CLOUD" --spend-limit 0
elif ccloud cluster list 2>/dev/null | grep -qw "$CLUSTER"; then
  echo "    exists, reusing it"
else
  # Basic is the serverless plan: it has a free monthly allowance, and
  # --spend-limit 0 makes the cluster throttle at the end of that allowance
  # instead of billing. Flag names on this CLI do move between releases — if
  # this fails, check `ccloud cluster create basic --help` rather than guessing.
  run ccloud cluster create basic "$CLUSTER" "$REGION" --cloud "$CLOUD" --spend-limit 0
fi

# ------------------------------------------------------------------- SQL user --
# `ccloud cluster user create` prompts for the password on stdin. There is no
# documented --password flag, so this step is interactive on purpose: generating
# the password here and telling the operator exactly what to paste beats
# inventing a flag that may not exist.
say "SQL user '$SQL_USER'"
if [[ -z "${AXIOM_SQL_PASSWORD:-}" ]]; then
  if [[ "$DRY_RUN" == "1" ]]; then
    AXIOM_SQL_PASSWORD='<generated-at-runtime>'
  else
    AXIOM_SQL_PASSWORD="$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')"
  fi
  cat <<MSG

    Paste this at the "Password:" prompt (twice, if asked). It is also what the
    DSN at the end of this script will contain — save it in your password
    manager now; CockroachDB Cloud will not show it again.

        ${B}${AXIOM_SQL_PASSWORD}${R}

MSG
fi
run ccloud cluster user create "$CLUSTER" "$SQL_USER"

# ------------------------------------------------------------ connection URL --
say "resolving the connection URL"
if [[ "$DRY_RUN" == "1" ]]; then
  printf '%s   + ccloud cluster sql %s --connection-url%s\n' "$DIM" "$CLUSTER" "$R"
  BASE_URL="postgresql://placeholder@axiom-0000.j77.aws-us-east-1.cockroachlabs.cloud:26257/defaultdb?sslmode=verify-full"
else
  BASE_URL="$(ccloud cluster sql "$CLUSTER" --connection-url | tr -d '[:space:]')"
  [[ -n "$BASE_URL" ]] || die "ccloud returned an empty connection URL."
fi

# The host and TLS parameters come from ccloud; only the credentials and the
# database are ours. Rewriting the URL with urllib rather than sed is the
# difference between "works" and "works when the password contains a slash".
mkurl() {
  BASE_URL="$BASE_URL" AX_USER="$1" AX_PASS="$2" AX_DB="$3" python3 - <<'PY'
import os, urllib.parse as u
p = u.urlsplit(os.environ['BASE_URL'])
host = p.hostname or ''
port = f':{p.port}' if p.port else ''
netloc = f"{u.quote(os.environ['AX_USER'], safe='')}:{u.quote(os.environ['AX_PASS'], safe='')}@{host}{port}"
print(u.urlunsplit((p.scheme or 'postgresql', netloc, '/' + os.environ['AX_DB'], p.query, '')))
PY
}

BOOTSTRAP_URL="$(mkurl "$SQL_USER" "$AXIOM_SQL_PASSWORD" defaultdb)"
DATABASE_URL="$(mkurl "$SQL_USER" "$AXIOM_SQL_PASSWORD" "$DB")"

# --------------------------------------------------------------------- schema --
# Applied against defaultdb: 001_schema.sql opens with CREATE DATABASE axiom and
# SET database = axiom, so connecting to the database it is about to create would
# fail on the first run.
say "applying db/001_schema.sql"
run cockroach sql --url "$BOOTSTRAP_URL" -f "$REPO_ROOT/db/001_schema.sql"

say "applying db/003_provider.sql (the external provider, its own database)"
run cockroach sql --url "$BOOTSTRAP_URL" -f "$REPO_ROOT/db/003_provider.sql"

# ----------------------------------------------------------------- audit role --
# db/002_audit_role.sql is the single definition of the read-only identity the
# Audit Agent and the Cloud Managed MCP Server connect as. It is applied here
# rather than restated, because a second hand-written copy of a privilege set is
# how a "read-only" account quietly acquires INSERT.
#
# It runs LAST, after 003, because it grants CONNECT on the `provider` database
# that 003 creates.
say "audit role (db/002_audit_role.sql — read-only, for the Audit Agent and Managed MCP)"
run cockroach sql --url "$BOOTSTRAP_URL" -f "$REPO_ROOT/db/002_audit_role.sql"

# 002 creates the login with no password, which is correct for an insecure local
# node and impossible in Cloud — a passwordless SQL user cannot authenticate
# there. Setting it separately keeps the checked-in SQL free of credentials.
if [[ -z "${AXIOM_AUDIT_PASSWORD:-}" ]]; then
  if [[ "$DRY_RUN" == "1" ]]; then
    AXIOM_AUDIT_PASSWORD='<generated-at-runtime>'
  else
    AXIOM_AUDIT_PASSWORD="$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')"
  fi
fi
run cockroach sql --url "$BOOTSTRAP_URL" \
    -e "ALTER USER ${AUDIT_USER} WITH PASSWORD '${AXIOM_AUDIT_PASSWORD}';"

AUDIT_URL="$(mkurl "$AUDIT_USER" "$AXIOM_AUDIT_PASSWORD" "$DB")"

# --------------------------------------------------------------------- verify --
if [[ "$DRY_RUN" != "1" ]]; then
  say "verifying"
  # Expect 9 tables and the two C-SPANN indexes, axiom_memory_ann_by_context and
  # axiom_memory_ann_by_tenant. Matched by name rather than by type on purpose:
  # crdb_internal.table_indexes, which is where index_type lives, is blocked in
  # v26.2 ("Access to crdb_internal and system is restricted") and SHOW INDEXES
  # has no index_type column.
  cockroach sql --url "$DATABASE_URL" -e "
    SELECT count(*) AS axiom_tables FROM [SHOW TABLES FROM ${DB}.public];
    SELECT DISTINCT index_name AS vector_index
      FROM [SHOW INDEXES FROM ${DB}.public.axiom_memory] WHERE index_name LIKE '%ann%';
  "
fi

# --------------------------------------------------------------------- output --
cat <<OUT

${B}Done.${R}

  Application DSN — export this, or store it as the SSM parameter that
  scripts/deploy.sh reads:

      export DATABASE_URL='${DATABASE_URL}'

  Read-only audit DSN — this is what the Cloud Managed MCP Server uses. It can
  SELECT from ${DB} and do nothing else:

      export AXIOM_AUDIT_DATABASE_URL='${AUDIT_URL}'

  Next:
      ./.venv/bin/python scripts/preflight.py   # gates against the live cluster
      ./scripts/deploy.sh                       # build, push, stand up ECS Fargate

OUT
