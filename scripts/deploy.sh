#!/usr/bin/env bash
# =============================================================================
# AXIOM :: build, push, and deploy to ECS Fargate.
#
#   export DATABASE_URL='postgresql://axiom_app:...@...cockroachlabs.cloud:26257/axiom?sslmode=verify-full'
#   ./scripts/deploy.sh
#
#   AXIOM_DRY_RUN=1 ./scripts/deploy.sh          # print the plan, touch nothing
#   AXIOM_SKIP_SECRET=1 ./scripts/deploy.sh      # DATABASE_URL already in SSM
#
# Order matters and the order is not obvious:
#
#   1. create the ECR repository ALONE (terraform -target). The rest of the
#      module needs an image URI that cannot exist until there is somewhere to
#      push to; this is the one legitimate use of -target — bootstrap ordering,
#      not "apply the part I like".
#   2. build for the task definition's architecture and push.
#   3. write DATABASE_URL to SSM Parameter Store as a SecureString. It never
#      enters terraform state, an environment variable in a task definition, or
#      this script's output.
#   4. apply the rest, pinning the image by tag.
#   5. wait for both services to reach steady state, then prove the demo URL
#      answers before claiming success.
# =============================================================================
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
NAME_PREFIX="${AXIOM_NAME_PREFIX:-axiom}"
PARAM_NAME="${AXIOM_DB_PARAM:-/axiom/prod/DATABASE_URL}"
PLATFORM="${AXIOM_PLATFORM:-linux/arm64}"   # must match cpu_architecture in terraform
DRY_RUN="${AXIOM_DRY_RUN:-0}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF_DIR="$REPO_ROOT/deploy/terraform"

if [[ -t 1 ]]; then B=$'\033[1m'; DIM=$'\033[2m'; R=$'\033[0m'; else B=''; DIM=''; R=''; fi
say()  { printf '%s==>%s %s\n' "$B" "$R" "$*"; }
die()  { printf '%sxx%s  %s\n' "$B" "$R" "$*" >&2; exit 1; }
run()  {
  if [[ "$DRY_RUN" == "1" ]]; then printf '%s   + %s%s\n' "$DIM" "$*" "$R"; else "$@"; fi
}

need() { command -v "$1" >/dev/null 2>&1 || die "missing '$1'."; }
if [[ "$DRY_RUN" != "1" ]]; then
  need aws; need docker; need terraform
fi

# ------------------------------------------------------------------- identity --
if [[ "$DRY_RUN" == "1" ]]; then
  ACCOUNT_ID="000000000000"
else
  ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
fi
say "account $ACCOUNT_ID, region $REGION"

# A dirty tree gets a distinct tag. The ECR repository is created with
# IMMUTABLE tags, so pushing "the same" sha twice with different bits is a hard
# error rather than a silent overwrite — which is the whole point, but it means
# uncommitted work needs a name of its own.
GIT_SHA="$(git -C "$REPO_ROOT" rev-parse --short=10 HEAD 2>/dev/null || echo nogit)"
if ! git -C "$REPO_ROOT" diff --quiet HEAD 2>/dev/null; then
  TAG="${GIT_SHA}-dirty-$(date +%s)"
  say "working tree is dirty — tagging $TAG"
else
  TAG="$GIT_SHA"
fi

# ------------------------------------------------------------ 1. ECR bootstrap --
say "ensuring the ECR repository exists"
run terraform -chdir="$TF_DIR" init -input=false
run terraform -chdir="$TF_DIR" apply -input=false -auto-approve \
    -target=aws_ecr_repository.axiom -var "image_uri=bootstrap"

if [[ "$DRY_RUN" == "1" ]]; then
  ECR_REPO="$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/$NAME_PREFIX"
else
  ECR_REPO="$(terraform -chdir="$TF_DIR" output -raw ecr_repository_url 2>/dev/null || true)"
  # create_ecr = false: the repository is managed elsewhere, so derive the URI.
  [[ -n "$ECR_REPO" && "$ECR_REPO" != "null" ]] || \
    ECR_REPO="${AXIOM_ECR_REPO:-$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/$NAME_PREFIX}"
fi
IMAGE_URI="$ECR_REPO:$TAG"

# ------------------------------------------------------------ 2. build & push --
say "building $IMAGE_URI for $PLATFORM"
if [[ "$DRY_RUN" == "1" ]]; then
  run docker login --username AWS --password '<ecr-token>' "$ECR_REPO"
else
  aws ecr get-login-password --region "$REGION" \
    | docker login --username AWS --password-stdin "${ECR_REPO%%/*}"
fi

# --platform is not optional: the deploy host is very likely arm64 while the task
# definition pins X86_64 or the reverse, and a Fargate task started from a
# mismatched image fails with "exec format error" several minutes later, in a
# place that does not name the cause.
#
# buildx when it is installed (it cross-builds and pushes in one step); the
# classic builder plus an explicit push when it is not. Both honour --platform
# given qemu/binfmt on the host — the amd64 image was cross-built and run this
# way from an arm64 laptop.
if [[ "$DRY_RUN" != "1" ]] && ! docker buildx version >/dev/null 2>&1; then
  say "docker buildx is not installed — falling back to the classic builder"
  run docker build --platform "$PLATFORM" --build-arg "GIT_SHA=$GIT_SHA" \
      -t "$IMAGE_URI" "$REPO_ROOT"
  run docker push "$IMAGE_URI"
else
  run docker buildx build \
      --platform "$PLATFORM" \
      --build-arg "GIT_SHA=$GIT_SHA" \
      -t "$IMAGE_URI" \
      --push \
      "$REPO_ROOT"
fi

# ---------------------------------------------------------------- 3. secrets --
if [[ "${AXIOM_SKIP_SECRET:-0}" == "1" ]]; then
  say "skipping the SSM parameter (AXIOM_SKIP_SECRET=1)"
else
  [[ -n "${DATABASE_URL:-}" ]] || die \
    "DATABASE_URL is not set. Run ./scripts/provision_ccloud.sh first, or set AXIOM_SKIP_SECRET=1."
  say "writing $PARAM_NAME to SSM Parameter Store (SecureString, standard tier — free)"
  # --value on the command line puts the DSN in this process's argv, where it is
  # visible to `ps` for the life of the call. file:// reads it from a
  # short-lived 0600 file instead.
  if [[ "$DRY_RUN" == "1" ]]; then
    run aws ssm put-parameter --name "$PARAM_NAME" --type SecureString --value '<from-file>' --overwrite
  else
    tmp="$(mktemp)"; chmod 600 "$tmp"
    trap 'rm -f "$tmp"' EXIT
    printf '%s' "$DATABASE_URL" > "$tmp"
    aws ssm put-parameter --region "$REGION" --name "$PARAM_NAME" \
        --type SecureString --value "file://$tmp" --overwrite >/dev/null
    rm -f "$tmp"; trap - EXIT
  fi
fi

# ------------------------------------------------------------------ 4. apply --
say "terraform apply"
run terraform -chdir="$TF_DIR" apply -input=false -auto-approve \
    -var "image_uri=$IMAGE_URI" \
    -var "region=$REGION" \
    -var "name_prefix=$NAME_PREFIX" \
    -var "database_url_parameter_name=$PARAM_NAME"

# ------------------------------------------------------------------- 5. wait --
CLUSTER="$NAME_PREFIX"
API_SVC="$NAME_PREFIX-api"
WORKER_SVC="$NAME_PREFIX-worker"

say "waiting for both services to reach steady state (this takes 2-4 minutes)"
# `aws ecs wait services-stable` polls for 40 tries at 15s = 10 minutes, then
# fails. The deployment circuit breaker in ecs.tf will have rolled back before
# that, so a failure here means the NEW task never became healthy — read
# /axiom/api and /axiom/worker in CloudWatch, in that order.
run aws ecs wait services-stable --region "$REGION" \
    --cluster "$CLUSTER" --services "$API_SVC" "$WORKER_SVC"

if [[ "$DRY_RUN" == "1" ]]; then
  DEMO_URL="http://axiom-alb-000000.us-east-1.elb.amazonaws.com"
else
  DEMO_URL="$(terraform -chdir="$TF_DIR" output -raw demo_url)"
fi

say "smoke test: $DEMO_URL/api/health"
if [[ "$DRY_RUN" != "1" ]]; then
  ok=0
  for _ in $(seq 1 30); do
    body="$(curl -fsS --max-time 5 "$DEMO_URL/api/health" 2>/dev/null || true)"
    if [[ -n "$body" ]]; then echo "    $body"; ok=1; break; fi
    sleep 4
  done
  # An ALB target that has not passed two health checks yet returns 503; 120
  # seconds of patience is the difference between a real failure and an
  # impatient one.
  [[ "$ok" == "1" ]] || die "the demo URL did not answer within 120s. Check target group health."
fi

cat <<OUT

${B}Deployed.${R}  image ${IMAGE_URI}

  Demo URL      ${DEMO_URL}
  Health        ${DEMO_URL}/api/health
  API logs      aws logs tail /${NAME_PREFIX}/api --follow --region ${REGION}
  Worker logs   aws logs tail /${NAME_PREFIX}/worker --follow --region ${REGION}

  Kill a worker on camera (this is why it is Fargate and not Lambda):

      aws ecs list-tasks --cluster ${CLUSTER} --service-name ${WORKER_SVC} --region ${REGION}
      aws ecs execute-command --cluster ${CLUSTER} --task <TASK_ARN> \\
          --container worker --interactive --command /bin/sh --region ${REGION}
      # then, inside the container:  kill -9 1

  Teardown when judging closes (see deploy/COST.md):

      terraform -chdir=deploy/terraform destroy

OUT
