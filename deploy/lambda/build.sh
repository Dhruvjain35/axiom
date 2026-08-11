#!/usr/bin/env bash
# AXIOM :: build the Lambda deployment ZIP.
#
#   ./deploy/lambda/build.sh
#
# Produces deploy/lambda/build/axiom-lambda.zip containing:
#   handler_api.py, handler_worker.py   the two entry points
#   axiom/                              the engine, unmodified
#   web/                                Mission Control, served from /var/task by
#                                       StaticFiles — which is why this deployment needs
#                                       no S3 bucket and no CloudFront distribution
#   root.crt                            the CockroachDB Cloud cluster CA
#   <deps>                              from requirements-lambda.txt, as linux wheels
#
# Two hard constraints shape everything below.
#
# 1. **The wheels must be linux-aarch64, not macOS arm64.** This is built on a Mac. A
#    plain `pip install --target` would happily install psycopg_binary's macOS .dylib
#    build, which imports fine here and dies at runtime on Lambda with
#    "No module named 'psycopg_binary._psycopg'" — a failure that only appears after a
#    deploy. `--platform` + `--only-binary=:all:` forces cross-platform resolution, and
#    the verification step below then reads the ELF headers rather than trusting it.
#
# 2. **The ZIP must stay under 50 MB.** That is the hard limit for
#    `aws lambda update-function-code --zip-file fileb://...`; above it AWS requires the
#    object to come from S3, and S3's free tier is a 12-month offer rather than an
#    always-free one. Staying under the line is what keeps this deployment at $0, so the
#    size is printed against the limit on every build instead of being assumed.
#
# arm64/Graviton, not x86_64: identical free-tier allowance, ~20% cheaper per GB-second
# past it, and the wheels exist for everything we need.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
BUILD="$HERE/build"
STAGE="$BUILD/stage"
ZIP="$BUILD/axiom-lambda.zip"

# Must match the runtime deploy.sh creates. python3.13 is the newest runtime on Amazon
# Linux 2023 with cp313 wheels for every compiled dependency we need (psycopg_binary,
# pydantic_core). Changing this means changing deploy.sh's --runtime in the same commit.
PY_VERSION="${PY_VERSION:-3.13}"
PY_TAG="cp${PY_VERSION/./}"
PYC_TAG="cpython-${PY_VERSION/./}"
ARCH="${ARCH:-aarch64}"
CLUSTER_ID="${CRDB_CLUSTER_ID:-b8325d1b-96ec-428f-b295-021f77f417a9}"

# The size the tooling actually enforces, in bytes. Not a round number by accident:
# `aws lambda get-account-settings` reports CodeSizeZipped = 52428800.
ZIP_LIMIT=52428800

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

# pip needs to run under SOME interpreter, but --platform/--python-version mean the
# building interpreter never has to match the target one. The repo's venv is 3.14 and
# builds a 3.13 package here without complaint.
PIP_PY="${PIP_PY:-$ROOT/.venv/bin/python}"
[ -x "$PIP_PY" ] || PIP_PY="$(command -v python3)"

say "clean"
rm -rf "$STAGE" "$ZIP"
mkdir -p "$STAGE"
echo "stage: $STAGE"

# ------------------------------------------------------------------ dependencies
say "dependencies (linux/$ARCH, $PY_TAG)"
# Three --platform values, all accepted for the same wheel set: pip matches a wheel whose
# tag appears in ANY of them. psycopg-binary 3.3.x publishes manylinux_2_28 only, while
# pydantic-core still publishes manylinux2014 (= manylinux_2_17). Passing one tag alone
# fails to resolve one of the two, with an error that reads like the version does not
# exist rather than like a tag mismatch — which is exactly how this line was arrived at.
"$PIP_PY" -m pip install --quiet --disable-pip-version-check \
  --target "$STAGE" \
  --implementation cp \
  --python-version "$PY_VERSION" \
  --platform "manylinux_2_28_${ARCH}" \
  --platform "manylinux_2_17_${ARCH}" \
  --platform "manylinux2014_${ARCH}" \
  --only-binary=:all: \
  --upgrade \
  -r "$HERE/requirements-lambda.txt"

# boto3 is provided by the runtime and is NOT in requirements-lambda.txt. If a transitive
# dependency ever drags it in anyway it would add ~50 MB unzipped, so fail loudly rather
# than silently shipping a second copy of the SDK.
if [ -d "$STAGE/boto3" ] || [ -d "$STAGE/botocore" ]; then
  echo "ERROR: boto3/botocore landed in the package; the runtime already provides them" >&2
  exit 1
fi

say "verify the binaries are actually linux/$ARCH"
# The failure this catches is silent at build time and fatal at runtime, so it is
# checked rather than assumed. On a Mac, a wheel that slipped through as macOS arm64
# would report "Mach-O 64-bit dynamically linked shared library arm64" here.
BAD=0
while IFS= read -r so; do
  desc="$(file -b "$so")"
  case "$desc" in
    *"ELF 64-bit LSB"*"ARM aarch64"*) : ;;
    *) echo "  NOT LINUX: $so -> $desc" >&2; BAD=1 ;;
  esac
done < <(find "$STAGE" -name '*.so' -o -name '*.so.*' | sort)
[ "$BAD" -eq 0 ] || { echo "ERROR: non-linux binaries in the package" >&2; exit 1; }
find "$STAGE" -name '*.so' | wc -l | xargs printf '  %s ELF aarch64 objects, all linux\n'
file -b "$(find "$STAGE" -path '*psycopg_binary*' -name '_psycopg*.so' | head -1)" \
  | sed 's/^/  psycopg_binary: /'

# ------------------------------------------------------------------ trim
say "trim"
# Everything removed here is dead weight in a read-only /var/task: test suites that will
# never run, C headers nothing will compile against, and type stubs no runtime reads.
# RECORD/WHEEL metadata inside .dist-info stays — importlib.metadata is a real runtime
# API and stripping it to save 200 KB is how a library starts raising
# PackageNotFoundError in production.
before=$(du -sk "$STAGE" | cut -f1)
find "$STAGE" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
find "$STAGE" -type d \( -name 'tests' -o -name 'test' \) -prune -exec rm -rf {} + 2>/dev/null || true
find "$STAGE" -type d -name 'include' -path '*psycopg*' -prune -exec rm -rf {} + 2>/dev/null || true
find "$STAGE" -type f \( -name '*.pyi' -o -name '*.h' -o -name '*.c' -o -name '*.pyx' \) -delete 2>/dev/null || true
find "$STAGE" -type f -name 'py.typed' -delete 2>/dev/null || true
after=$(du -sk "$STAGE" | cut -f1)
echo "  deps ${before} KB -> ${after} KB unzipped"

# ------------------------------------------------------------------ our code
say "application"
# --exclude on the copy, not a delete afterwards: a .pyc built by the 3.14 venv would
# shadow nothing (wrong magic tag) but would still cost ZIP bytes, and a stray .env is
# the kind of thing that must never be able to reach a public artifact.
rsync -a --exclude '__pycache__' --exclude '*.pyc' --exclude '.env*' \
  "$ROOT/axiom" "$STAGE/"
rsync -a --exclude '.DS_Store' "$ROOT/web" "$STAGE/"
cp "$HERE/handler_api.py" "$STAGE/"

# The worker handler is built in parallel; the ZIP is deliberately shared between both
# functions (one artifact, one upload, two entry points), so its absence is a warning
# rather than an error and deploy.sh will simply create a worker that cannot import.
if [ -f "$HERE/handler_worker.py" ]; then
  cp "$HERE/handler_worker.py" "$STAGE/"
  echo "  handler_worker.py included"
else
  echo "  WARNING: handler_worker.py not present yet — axiom-worker will fail to import"
fi

# ------------------------------------------------------------------ the CA
say "CockroachDB Cloud CA"
# CockroachDB Cloud BASIC clusters are signed by Cockroach Labs' own CA, which is not in
# any system trust store — sslmode=verify-full fails without this file, and
# sslrootcert=system fails too. Shipping it inside the ZIP (PGSSLROOTCERT=/var/task/
# root.crt) is what makes a verified TLS connection possible from a function that has no
# writable filesystem and no way to install trust roots at boot.
curl -fsS -o "$STAGE/root.crt" \
  "https://cockroachlabs.cloud/clusters/${CLUSTER_ID}/cert"
head -1 "$STAGE/root.crt" | grep -q 'BEGIN CERTIFICATE' \
  || { echo "ERROR: fetched CA does not look like a PEM certificate" >&2; exit 1; }
printf '  %s bytes, %s certificate(s)\n' "$(wc -c < "$STAGE/root.crt" | tr -d ' ')" \
  "$(grep -c 'BEGIN CERTIFICATE' "$STAGE/root.crt")"

# ------------------------------------------------------------------ bytecode
say "bytecode"
# Precompiling cuts a second or so off every cold start: /var/task is read-only, so a
# container that has to compile fastapi + pydantic + psycopg from source does it in
# memory and then throws the result away — once per container, forever.
#
# --invalidation-mode unchecked-hash is the part that makes it work at all. The default
# .pyc records the source mtime, ZIP timestamps have 2-second granularity, and Lambda's
# extraction does not preserve them exactly — so a normal .pyc would be judged stale and
# recompiled anyway. `unchecked-hash` says: this deployment is immutable, use the .pyc.
TARGET_PY="$(command -v "python${PY_VERSION}" || true)"
if [ -n "$TARGET_PY" ]; then
  "$TARGET_PY" -m compileall -q -j 0 --invalidation-mode unchecked-hash "$STAGE" \
    >/dev/null 2>&1 || echo "  (compileall reported errors; non-fatal, .py still ships)"
  n=$(find "$STAGE" -name "*.${PYC_TAG}.pyc" | wc -l | tr -d ' ')
  echo "  $n ${PY_TAG} .pyc files precompiled with $TARGET_PY"
else
  echo "  SKIPPED: no python${PY_VERSION} on this machine, so any .pyc built here would"
  echo "  carry the wrong magic tag and be ignored. Cold start pays the compile instead."
fi

# ------------------------------------------------------------------ zip
say "zip"
# -X drops the extra file attributes (uid/gid, Finder metadata) that make the archive
# non-reproducible and slightly larger. -9 because upload time is not the constraint,
# the 50 MB limit is.
( cd "$STAGE" && zip -q -r -X -9 "$ZIP" . )

BYTES=$(wc -c < "$ZIP" | tr -d ' ')
MB=$(awk "BEGIN{printf \"%.1f\", $BYTES/1048576}")
LIMIT_MB=$(awk "BEGIN{printf \"%.0f\", $ZIP_LIMIT/1048576}")
PCT=$(awk "BEGIN{printf \"%.0f\", 100*$BYTES/$ZIP_LIMIT}")
UNZIPPED=$(awk "BEGIN{printf \"%.1f\", $(du -sk "$STAGE" | cut -f1)/1024}")

echo
printf '  %s\n' "$ZIP"
printf '  %s MB zipped / %s MB limit (%s%%), %s MB unzipped\n' "$MB" "$LIMIT_MB" "$PCT" "$UNZIPPED"
if [ "$BYTES" -ge "$ZIP_LIMIT" ]; then
  echo "  ERROR: over the direct-upload limit — this would force an S3 bucket" >&2
  exit 1
fi
echo
echo "  next: ./deploy/lambda/deploy.sh"
