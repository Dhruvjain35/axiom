#!/usr/bin/env bash
# AXIOM :: deploy the demo to AWS Lambda for $0.00/month. Idempotent — re-run freely.
#
#   export AWS_PROFILE=axiom
#   export DATABASE_URL='postgresql://axiom_app:...@...cockroachlabs.cloud:26257/axiom?sslmode=verify-full&connect_timeout=5'
#   ./deploy/lambda/deploy.sh
#
# Creates or updates these, none of which bills at rest:
#
#   IAM role          axiom-lambda-role   logs + invoke-the-worker, nothing else
#   Lambda            axiom-api           the FastAPI app and the Mission Control UI
#   Lambda            axiom-worker        the queue-draining worker, invoked async
#   Function URL      on axiom-api        CORS open; its auth type is DECIDED BY PROBE,
#                                         see "the public front" below
#   CloudFront        only if the public function URL is refused — always-free tier
#   Log groups        /aws/lambda/axiom-* 7-day retention so nothing accretes
#
# What it deliberately does NOT create, and why
# ---------------------------------------------
#   API Gateway    $1.00 per million requests. A Function URL is free and does the same
#                  job for a single-origin demo.
#   ALB            ~$16.40/month whether or not a judge ever loads the page.
#   ECR            its 500 MB is a 12-month offer, not always-free — and a ZIP under
#                  50 MB needs no registry at all.
#   S3             same 12-month problem. web/ is inside the ZIP; there is nothing to
#                  put in a bucket.
#   NAT / VPC      a Lambda outside a VPC has internet access for free. Putting it in
#                  one to reach CockroachDB Cloud would require a $32/month NAT gateway
#                  to reach a public endpoint it can already reach.
#   Bedrock        models ARE enabled here and both answer, but the on-demand quota for
#                  Titan V2 is 0.0 req/min and not adjustable, so the functions run with
#                  AXIOM_OFFLINE=1 and the role is granted no bedrock:* at all.
#
# Lambda's 1M requests + 400,000 GB-seconds per month is an ALWAYS-free allowance, not a
# 12-month introductory one, which is the entire reason this path exists: the demo has to
# stay up from Aug 19 to Sep 15 on an account with $0.00 of credits.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ZIP="$HERE/build/axiom-lambda.zip"

REGION="${AWS_REGION:-us-east-2}"
ROLE_NAME="${ROLE_NAME:-axiom-lambda-role}"
API_FN="${API_FN:-axiom-api}"
WORKER_FN="${WORKER_FN:-axiom-worker}"
RUNTIME="${RUNTIME:-python3.13}"          # must match build.sh's PY_VERSION
ARCHS="${ARCHS:-arm64}"                   # must match build.sh's ARCH (aarch64)

# See the sizing arithmetic in handler_api.py. Short version: the warm path is I/O-bound
# on a cross-region CockroachDB round trip, so more memory buys no speed and costs
# strictly more GB-seconds.
API_MEMORY="${API_MEMORY:-512}"
API_TIMEOUT="${API_TIMEOUT:-30}"
# The worker is a different shape: it drains a queue for up to 300 s per invocation, and
# POST /api/demo/run-worker accepts seconds<=300, so the timeout has to cover the longest
# run the API is allowed to ask for. It is invoked asynchronously, so nothing waits.
WORKER_MEMORY="${WORKER_MEMORY:-512}"
WORKER_TIMEOUT="${WORKER_TIMEOUT:-300}"

# The worker's entry point is read out of the file rather than assumed. The two handlers
# are written by different people; a deployment that silently creates a function whose
# handler string does not exist fails only at invoke time, in a log nobody is watching,
# on the one function that is invoked asynchronously and therefore never returns its
# error to a caller. `handler` and `lambda_handler` are both conventional names.
if grep -q '^def lambda_handler' "$HERE/handler_worker.py" 2>/dev/null; then
  WORKER_HANDLER="${WORKER_HANDLER:-handler_worker.lambda_handler}"
elif grep -q '^def handler' "$HERE/handler_worker.py" 2>/dev/null; then
  WORKER_HANDLER="${WORKER_HANDLER:-handler_worker.handler}"
else
  WORKER_HANDLER="${WORKER_HANDLER:-handler_worker.lambda_handler}"
fi

LOG_RETENTION_DAYS="${LOG_RETENTION_DAYS:-7}"

: "${DATABASE_URL:?set DATABASE_URL to the CockroachDB Cloud connection string (with sslmode=verify-full, no sslrootcert — the cert ships in the ZIP)}"
[ -f "$ZIP" ] || { echo "no $ZIP — run ./deploy/lambda/build.sh first" >&2; exit 1; }

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
aws() { command aws --region "$REGION" "$@"; }

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
say "account $ACCOUNT / region $REGION"
ZIP_BYTES=$(wc -c < "$ZIP" | tr -d ' ')
printf '  zip: %s (%s MB)\n' "$ZIP" \
  "$(awk -v b="$ZIP_BYTES" 'BEGIN{printf "%.1f", b/1048576}')"

# The password lives in this process's environment and must not reach a file that
# outlives it. umask 077 + trap covers the window where it has to be on disk for the CLI.
umask 077
TMPDIR_RUN="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_RUN"' EXIT

# ------------------------------------------------------------------------- IAM role
say "IAM role $ROLE_NAME"
cat > "$TMPDIR_RUN/trust.json" <<'JSON'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
 "Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}
JSON

if ROLE_ARN=$(aws iam get-role --role-name "$ROLE_NAME" --query 'Role.Arn' --output text 2>/dev/null); then
  echo "  exists: $ROLE_ARN"
else
  ROLE_ARN=$(aws iam create-role --role-name "$ROLE_NAME" \
    --assume-role-policy-document "file://$TMPDIR_RUN/trust.json" \
    --description 'AXIOM Lambda execution role: logs, and API->worker invoke' \
    --query 'Role.Arn' --output text)
  echo "  created: $ROLE_ARN"
fi

# CloudWatch Logs only. Not AWSLambdaRole, not a wildcard — the managed basic-execution
# policy is exactly CreateLogGroup/CreateLogStream/PutLogEvents.
aws iam attach-role-policy --role-name "$ROLE_NAME" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
echo "  attached: AWSLambdaBasicExecutionRole (CloudWatch Logs)"

# The one extra permission, scoped to one function ARN: POST /api/demo/run-worker
# invokes the worker asynchronously rather than forking a subprocess, because a
# subprocess inside a Lambda dies the moment the response is returned and the container
# is frozen. Resource-scoped so a compromised API cannot invoke anything else in the
# account, and put-role-policy overwrites, which is what makes this re-runnable.
cat > "$TMPDIR_RUN/invoke.json" <<JSON
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"lambda:InvokeFunction",
 "Resource":"arn:aws:lambda:${REGION}:${ACCOUNT}:function:${WORKER_FN}"}]}
JSON
aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name axiom-invoke-worker \
  --policy-document "file://$TMPDIR_RUN/invoke.json"
echo "  inline: axiom-invoke-worker -> lambda:InvokeFunction on $WORKER_FN"

# ------------------------------------------------------------------------ environment
# AWS_REGION is NOT set here: it is a reserved Lambda environment variable, and passing
# it makes UpdateFunctionConfiguration fail outright. The runtime already exports it,
# and axiom.config reads it from there.
export WORKER_FN
python3 - "$TMPDIR_RUN/env.json" <<PY
import json, os, sys
env = {
    'DATABASE_URL': os.environ['DATABASE_URL'],
    # Bedrock's on-demand quota on this account is 0.0 requests/minute and AWS marks it
    # non-adjustable, so it cannot serve a request loop even though the models answer.
    # Offline swaps the embedder and the LLM for deterministic local stand-ins; the engine
    # cannot tell the difference, which is the point of the provider interfaces.
    'AXIOM_OFFLINE': '1',
    # CockroachDB Cloud BASIC is signed by a CA that is in no system trust store, and
    # sslrootcert=system does not work either. build.sh puts the cluster cert in the ZIP;
    # /var/task is where Lambda extracts it.
    'PGSSLROOTCERT': '/var/task/root.crt',
    # Tells axiom.api's /api/demo/run-worker to invoke a Lambda instead of forking.
    'AXIOM_WORKER_LAMBDA': os.environ['WORKER_FN'],
    # One invocation per container at a time, so a big pool would only multiply idle
    # CockroachDB connections across warm containers. 1 open, 2 ceiling.
    'AXIOM_POOL_MIN': '1',
    'AXIOM_POOL_MAX': '2',
    'PYTHONUNBUFFERED': '1',
}
json.dump({'Variables': env}, open(sys.argv[1], 'w'))
PY

# --------------------------------------------------------------------------- functions
# The 11 MB ZIP goes up over the operator's uplink, and the CLI's 60-second read timeout
# is measured from the END of the request body — so on a slow connection the upload
# completes and then the CLI gives up waiting for the response, reporting "Connection was
# closed before we received a valid response". Measured here: 6 m 41 s for 11.2 MB. So
# code uploads get no read timeout and a generous retry budget. Every other call in this
# script keeps the default, because a hung DESCRIBE should fail fast.
UPLOAD_OPTS=(--cli-read-timeout 0 --cli-connect-timeout 60)
export AWS_MAX_ATTEMPTS="${AWS_MAX_ATTEMPTS:-6}"

deploy_fn() {
  local name="$1" handler="$2" memory="$3" timeout="$4"
  say "function $name ($handler)"

  if aws lambda get-function --function-name "$name" >/dev/null 2>&1; then
    # Lambda's CodeSha256 is exactly base64(sha256(zip)), so the artifact already on AWS
    # can be compared to the one on disk without uploading anything. Worth the two lines:
    # a config-only re-run drops from ~14 minutes of upload to seconds, and re-running is
    # supposed to be cheap enough that nobody hesitates.
    if [ "$(openssl dgst -sha256 -binary "$ZIP" | base64)" = \
         "$(aws lambda get-function-configuration --function-name "$name" \
              --query CodeSha256 --output text)" ]; then
      echo "  code unchanged, upload skipped"
    else
      echo "  uploading $(awk -v b="$ZIP_BYTES" 'BEGIN{printf "%.1f", b/1048576}') MB (slow uplinks: minutes, not seconds)"
      aws lambda update-function-code --function-name "$name" "${UPLOAD_OPTS[@]}" \
        --zip-file "fileb://$ZIP" --output text \
        --query 'to_string(CodeSize)' | xargs printf '  code updated: %s bytes\n'
      aws lambda wait function-updated-v2 --function-name "$name"
    fi
    aws lambda update-function-configuration --function-name "$name" \
      --role "$ROLE_ARN" --handler "$handler" --runtime "$RUNTIME" \
      --memory-size "$memory" --timeout "$timeout" \
      --environment "file://$TMPDIR_RUN/env.json" \
      --output text --query 'to_string(MemorySize)' >/dev/null
    aws lambda wait function-updated-v2 --function-name "$name"
    echo "  config updated: ${memory} MB / ${timeout}s / $RUNTIME / $ARCHS"
  else
    # IAM is eventually consistent: a role created seconds ago is frequently not yet
    # assumable, and Lambda reports that as "The role defined for the function cannot be
    # assumed by Lambda" rather than as a retryable error. Retrying the create is the
    # documented workaround.
    local tries=0
    until aws lambda create-function --function-name "$name" "${UPLOAD_OPTS[@]}" \
        --runtime "$RUNTIME" --architectures "$ARCHS" --role "$ROLE_ARN" \
        --handler "$handler" --zip-file "fileb://$ZIP" \
        --memory-size "$memory" --timeout "$timeout" \
        --environment "file://$TMPDIR_RUN/env.json" \
        --description "AXIOM $name — crash-safe agent execution on CockroachDB" \
        --output text --query 'to_string(CodeSize)' >/dev/null 2>&1; do
      tries=$((tries + 1))
      [ "$tries" -lt 12 ] || { echo "  create-function failed 12x" >&2
        aws lambda create-function --function-name "$name" --runtime "$RUNTIME" \
          --architectures "$ARCHS" --role "$ROLE_ARN" --handler "$handler" \
          --zip-file "fileb://$ZIP" --memory-size "$memory" --timeout "$timeout" \
          --environment "file://$TMPDIR_RUN/env.json" >&2; exit 1; }
      echo "  waiting for the role to become assumable (${tries}/12)"
      sleep 5
    done
    aws lambda wait function-active-v2 --function-name "$name"
    echo "  created: ${memory} MB / ${timeout}s / $RUNTIME / $ARCHS"
  fi

  # Logs are the only thing here that can grow without bound. 7 days is longer than the
  # judging window's memory and keeps storage inside the CloudWatch free allowance.
  aws logs create-log-group --log-group-name "/aws/lambda/$name" 2>/dev/null || true
  aws logs put-retention-policy --log-group-name "/aws/lambda/$name" \
    --retention-in-days "$LOG_RETENTION_DAYS"
  echo "  logs: /aws/lambda/$name (${LOG_RETENTION_DAYS}d retention)"
}

deploy_fn "$API_FN" handler_api.lambda_handler "$API_MEMORY" "$API_TIMEOUT"
# The worker ships in the SAME ZIP with a different entry point: one artifact, one
# upload, two functions, and no chance of the API and the worker disagreeing about what
# axiom.tasks does. Its handler is built in parallel — if handler_worker.py was missing
# when build.sh ran, this function exists but cannot import, which is visible instantly
# in its log group and fixed by re-running build.sh and this script.
deploy_fn "$WORKER_FN" "$WORKER_HANDLER" "$WORKER_MEMORY" "$WORKER_TIMEOUT"

# ------------------------------------------------------------------------ Function URL
# Every deployment gets a Function URL — it is free, and it is the origin CloudFront uses
# if the public path below turns out to be unavailable. Only its auth type varies.
say "function URL on $API_FN"
CORS='{"AllowOrigins":["*"],"AllowMethods":["*"],"AllowHeaders":["*"],"MaxAge":86400}'
if URL=$(aws lambda get-function-url-config --function-name "$API_FN" \
         --query FunctionUrl --output text 2>/dev/null); then
  echo "  exists: $URL"
else
  URL=$(aws lambda create-function-url-config --function-name "$API_FN" \
    --auth-type AWS_IAM --cors "$CORS" --query FunctionUrl --output text)
  echo "  created: $URL"
fi
URL="${URL%/}"
ORIGIN_HOST="${URL#https://}"

# ------------------------------------------------------------------- the public front
# THREE ways to reach this function, in descending order of how much a judge will enjoy
# them, and the script uses the first one the account actually permits:
#
#   A. Function URL, auth NONE. No extra service, no extra hop, $0.
#   B. CloudFront -> Function URL (auth AWS_IAM, signed by an Origin Access Control).
#      CloudFront's free tier is ALWAYS free — 1 TB out, 10M requests, free TLS cert,
#      per month, permanently — so this is also $0. It is simply more moving parts.
#   C. Function URL, auth AWS_IAM, reached with a SigV4 signature. Not a public demo,
#      but it proves the deployment and it is what `signed_curl.py` speaks.
#
# A and B are both refused on THIS account, and the cause is worth recording because it
# looks exactly like a misconfiguration and is not one. Controlled experiment, one role,
# one function, one unchanged resource-policy statement granting it InvokeFunctionUrl:
#
#   role WITH an identity policy allowing lambda:InvokeFunctionUrl  -> 200
#   same role, identity policy removed, resource policy unchanged   -> 403
#
# i.e. **resource-based policy grants on a Function URL are not honored on this account**,
# after 90 s of propagation, with `iam simulate-principal-policy` reporting "allowed".
# Anonymous access (A) is a resource-policy grant to `*`, and OAC (B) is a resource-policy
# grant to cloudfront.amazonaws.com — so both fail for one reason, in two regions, on a
# throwaway hello-world function as well as on this one. There is no API to inspect or
# change it: no PublicAccessBlock operation exists anywhere in the Lambda service model,
# and this account is in no organization, so it is not an SCP either. It is an
# account-level restriction, and the fix is an AWS Support case (free on Basic support),
# not a change to this script. So the script PROBES rather than assumes: A, then B, then
# C, using the first that answers 200 to an anonymous GET.
#
# The verdict is REMEMBERED, in a tag on the function, and that is not a cache for its own
# sake. Probing means flipping the URL's auth type to NONE and back, and an auth-type
# change takes a minute or two to settle across the URL fleet — during which a live demo
# answers 403 to perfectly good signed requests (observed: 5 of 6 endpoints 403 immediately
# after a redeploy, all fine a minute later). Re-deploying a working demo must not do that.
# `FRONT=reprobe` re-tests deliberately; that is the switch to throw the day the support
# case is resolved, and it needs no edit to this file.
FRONT="${FRONT:-auto}"                 # auto | reprobe | url | cloudfront | iam
DEMO_URL=""
FRONT_KIND=""
API_ARN="arn:aws:lambda:${REGION}:${ACCOUNT}:function:${API_FN}"

# Tags are free, live with the function, and survive a redeploy — which is exactly the
# lifetime this verdict has.
remembered=$(aws lambda list-tags --resource "$API_ARN" \
  --query 'Tags."axiom:front"' --output text 2>/dev/null || echo None)
if [ "$FRONT" = "auto" ] && [ -n "$remembered" ] && [ "$remembered" != "None" ]; then
  FRONT="$remembered"
  say "front door: reusing the remembered verdict '$FRONT' (FRONT=reprobe to re-test)"
elif [ "$FRONT" = "reprobe" ]; then
  FRONT=auto
fi

find_distribution() {
  aws cloudfront list-distributions \
    --query "DistributionList.Items[?Origins.Items[?DomainName=='${ORIGIN_HOST}']].Id | [0]" \
    --output text 2>/dev/null | grep -v '^None$' || true
}

# One probe used for every candidate, so "it works" always means the same thing:
# an ANONYMOUS request that a judge could make from a browser.
probe() {
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 45 "$1/api/health" || echo 000)
  echo "  anonymous GET $1/api/health -> $code"
  [ "$code" = "200" ]
}

try_public_url() {
  say "public front door: function URL with auth NONE"
  aws lambda update-function-url-config --function-name "$API_FN" \
    --auth-type NONE --cors "$CORS" --output text --query AuthType >/dev/null
  # AuthType NONE is not enough on its own: without this resource policy statement every
  # request is 403. AWS makes you say "public" twice, in two different places.
  aws lambda add-permission --function-name "$API_FN" \
    --statement-id FunctionURLAllowPublicAccess \
    --action lambda:InvokeFunctionUrl --principal '*' \
    --function-url-auth-type NONE --output text --query Statement >/dev/null 2>&1 \
    && echo "  public invoke permission added" || echo "  public invoke permission present"
  sleep 10
  probe "$URL"
}

ensure_cloudfront() {
  say "public front door: CloudFront -> function URL (signed)"
  # Back to AWS_IAM: with OAC the origin must REQUIRE a signature, otherwise the URL
  # would still be a second, unsigned way in.
  aws lambda update-function-url-config --function-name "$API_FN" \
    --auth-type AWS_IAM --cors "$CORS" --output text --query AuthType >/dev/null
  aws lambda remove-permission --function-name "$API_FN" \
    --statement-id FunctionURLAllowPublicAccess >/dev/null 2>&1 || true

  local oac
  oac=$(aws cloudfront list-origin-access-controls \
    --query "OriginAccessControlList.Items[?Name=='${API_FN}-oac'].Id | [0]" --output text)
  if [ -z "$oac" ] || [ "$oac" = "None" ]; then
    oac=$(aws cloudfront create-origin-access-control --origin-access-control-config \
      "{\"Name\":\"${API_FN}-oac\",\"Description\":\"AXIOM API Lambda function URL\",\"SigningProtocol\":\"sigv4\",\"SigningBehavior\":\"always\",\"OriginAccessControlOriginType\":\"lambda\"}" \
      --query 'OriginAccessControl.Id' --output text)
    echo "  origin access control created: $oac"
  else
    echo "  origin access control: $oac"
  fi

  local dist; dist=$(find_distribution)
  if [ -n "$dist" ]; then
    echo "  distribution exists: $dist"
  else
    # CachingDisabled, deliberately. Mission Control polls /api/* while a mission runs
    # and a cached task list is a demo that lies. AllViewerExceptHostHeader is mandatory
    # rather than a preference: the Host header must be the origin's or the SigV4
    # signature CloudFront computes will not match what Lambda verifies.
    cat > "$TMPDIR_RUN/dist.json" <<JSON
{
  "CallerReference": "${API_FN}-$(date +%s)",
  "Comment": "AXIOM demo: CloudFront in front of the ${API_FN} Lambda function URL",
  "Enabled": true,
  "PriceClass": "PriceClass_100",
  "HttpVersion": "http2and3",
  "IsIPV6Enabled": true,
  "Origins": {"Quantity": 1, "Items": [{
    "Id": "${API_FN}",
    "DomainName": "${ORIGIN_HOST}",
    "OriginAccessControlId": "${oac}",
    "CustomOriginConfig": {"HTTPPort": 80, "HTTPSPort": 443,
      "OriginProtocolPolicy": "https-only",
      "OriginSslProtocols": {"Quantity": 1, "Items": ["TLSv1.2"]},
      "OriginReadTimeout": ${API_TIMEOUT}, "OriginKeepaliveTimeout": 5}
  }]},
  "DefaultCacheBehavior": {
    "TargetOriginId": "${API_FN}",
    "ViewerProtocolPolicy": "redirect-to-https",
    "AllowedMethods": {"Quantity": 7,
      "Items": ["GET","HEAD","OPTIONS","PUT","POST","PATCH","DELETE"],
      "CachedMethods": {"Quantity": 2, "Items": ["GET","HEAD"]}},
    "CachePolicyId": "4135ea2d-6df8-44a3-9df3-4b5a84be39ad",
    "OriginRequestPolicyId": "b689b0a8-53d0-40ab-baf2-68738e2966ac",
    "Compress": false
  }
}
JSON
    dist=$(aws cloudfront create-distribution \
      --distribution-config "file://$TMPDIR_RUN/dist.json" \
      --query 'Distribution.Id' --output text)
    echo "  distribution created: $dist"
  fi

  # Scoped to this one distribution by SourceArn, so the signed door opens for nothing
  # else in the account. This is strictly tighter than the public URL it replaces.
  aws lambda add-permission --function-name "$API_FN" \
    --statement-id AllowCloudFrontServicePrincipal \
    --action lambda:InvokeFunctionUrl --principal cloudfront.amazonaws.com \
    --source-arn "arn:aws:cloudfront::${ACCOUNT}:distribution/${dist}" \
    --function-url-auth-type AWS_IAM --output text --query Statement >/dev/null 2>&1 \
    && echo "  cloudfront invoke permission added" || echo "  cloudfront invoke permission present"

  if [ "$(aws cloudfront get-distribution --id "$dist" --query 'Distribution.Status' \
          --output text)" != "Deployed" ]; then
    echo "  waiting for the distribution to reach Deployed (first time: 3-8 minutes)"
    aws cloudfront wait distribution-deployed --id "$dist"
  fi
  CF_URL="https://$(aws cloudfront get-distribution --id "$dist" \
    --query 'Distribution.DomainName' --output text)"
  probe "$CF_URL"
}

if [ "$FRONT" != "cloudfront" ] && [ "$FRONT" != "iam" ] && try_public_url; then
  DEMO_URL="$URL"; FRONT_KIND="function URL, public (auth NONE)"; VERDICT=url
elif [ "$FRONT" != "url" ] && [ "$FRONT" != "iam" ] && ensure_cloudfront; then
  DEMO_URL="$CF_URL"; FRONT_KIND="CloudFront -> function URL (OAC, sigv4)"; VERDICT=cloudfront
else
  VERDICT=iam
  # Neither public door opened. Say so precisely — an operator who is told "deployed"
  # and then hands a judge a 403 has been failed by their tooling, not by AWS.
  # Leave the URL requiring a signature rather than parked on a NONE that answers 403:
  # an endpoint whose configuration claims "public" and whose behaviour is "denied" is
  # the worst of the three states to hand somebody.
  aws lambda update-function-url-config --function-name "$API_FN" \
    --auth-type AWS_IAM --cors "$CORS" --output text --query AuthType >/dev/null
  aws lambda remove-permission --function-name "$API_FN" \
    --statement-id FunctionURLAllowPublicAccess >/dev/null 2>&1 || true
  DEMO_URL="$URL"; FRONT_KIND="function URL, IAM-signed only (NOT public)"
  # Only when the script CHOSE this outcome. `FRONT=iam` means the operator asked for it
  # and does not need to be told the account is broken.
  if [ "$FRONT" = "auto" ]; then cat >&2 <<'DIAG'

  ------------------------------------------------------------------------------
  NO PUBLIC DOOR. Both free public paths were refused by this ACCOUNT, not by this
  deployment: anonymous invoke (resource policy grants "*") and CloudFront OAC
  (resource policy grants cloudfront.amazonaws.com) both return
  403 AccessDeniedException, while the same URL answers 200 to a SigV4 signature.

  Reproduce the finding in 60 seconds:
    role + identity policy allowing lambda:InvokeFunctionUrl        -> 200
    same role, identity policy removed, resource policy unchanged   -> 403

  Resource-based policy grants on Function URLs are not being honored here. That is
  an account restriction with no API surface (no PublicAccessBlock operation exists
  in the Lambda service model; this account is in no organization, so no SCP).

  Remedy, in order of speed:
    1. AWS Support case (free on Basic support), Account and billing ->
       "Lambda function URL public access is denied on account 034971967323 despite
       a correct resource policy". Then re-run this script; it will pick up path A
       with no edit.
    2. Until then the API is reachable and provable over real HTTP:
         ./.venv/bin/python deploy/lambda/signed_curl.py /api/health
    3. deploy/free-tier/ (one EC2 instance, ~$10.40/month) needs no Lambda resource
       policy at all and is unaffected by this.
  ------------------------------------------------------------------------------

DIAG
  fi
fi

# Remember it, so the next run does not flip a live demo's auth type to re-learn what
# this one just found out.
aws lambda tag-resource --resource "$API_ARN" --tags "axiom:front=${VERDICT}" >/dev/null

# ------------------------------------------------------------------------------ prove
say "smoke test ($FRONT_KIND)"
if [ "$VERDICT" = "iam" ]; then
  # An unsigned curl is the wrong instrument for an IAM-authed URL; it would print four
  # 403s that mean nothing about whether the deployment works. Use the signer.
  PY="$(dirname "$HERE")/../.venv/bin/python"
  [ -x "$PY" ] || PY=python3
  for path in /api/health /api/crash-windows / /styles.css; do
    "$PY" "$HERE/signed_curl.py" --head "$path" --function "$API_FN" --region "$REGION" \
      2>&1 | head -1 | sed 's/^/  /'
  done
else
  for path in /api/health /api/crash-windows / /styles.css; do
    code=$(curl -s -o "$TMPDIR_RUN/body" -w '%{http_code}' --max-time 45 "$DEMO_URL$path" || echo 000)
    printf '  %-22s -> %s  %s\n' "GET $path" "$code" \
      "$(head -c 80 "$TMPDIR_RUN/body" | tr -d '\n')"
  done
fi

say "demo URL"
echo
echo "    $DEMO_URL/"
echo
echo "  reached as:  $FRONT_KIND"
echo "  api:         $DEMO_URL/api/health"
echo "               $DEMO_URL/api/mission"
echo "               $DEMO_URL/api/crash-windows"
echo "  docs:        $DEMO_URL/api/docs"
echo "  logs:        aws logs tail /aws/lambda/$API_FN --follow --region $REGION"
echo "  origin:      $URL/  (auth $(aws lambda get-function-url-config \
                                     --function-name "$API_FN" --query AuthType --output text))"
echo
echo "  Standing cost: \$0.00/month. Lambda's 1M requests and 400,000 GB-seconds per month"
echo "  are always-free (not a 12-month offer), the Function URL is free, CloudFront's"
echo "  1 TB + 10M requests per month are always-free, and nothing here bills at rest."
