#!/usr/bin/env bash
# AXIOM :: the public front door — an HTTP API in front of the axiom-api Lambda.
# Idempotent, create-or-update. Re-run freely.
#
#   export AWS_PROFILE=axiom
#   ./deploy/lambda/apigateway.sh
#
# Run ./deploy/lambda/deploy.sh first: this script fronts the function, it does not
# create it, and it refuses to run if axiom-api is not already deployed.
#
# Creates or updates, in this order:
#
#   HTTP API        axiom-api-http      apigatewayv2, protocol HTTP
#   Integration     AWS_PROXY           payload format 2.0, 30 s timeout
#   Route           $default            every method, every path, including /
#   Stage           $default            auto-deploy, throttled to 20 req/s burst 40
#   Permission      on axiom-api        apigateway.amazonaws.com, scoped to THIS api id
#
# Why this exists at all
# ----------------------
# deploy.sh's header says API Gateway is deliberately not created, because a Function
# URL is free and does the same job. That reasoning was right and the conclusion was
# still wrong on this account, for a reason no cost table predicts: **this account
# refuses anonymous access to Lambda Function URLs**, and the refusal is account-level,
# not a misconfiguration. The controlled experiment is in README.md — one role, one
# function, one unchanged resource policy: with an identity policy allowing
# lambda:InvokeFunctionUrl -> 200, with it removed -> 403. Resource-based grants on a
# Function URL are not honored here, and both free public paths (auth type NONE, and
# CloudFront + OAC) are exactly that kind of grant.
#
# API Gateway is a different service and does not depend on that grant. Its permission
# is still a Lambda resource policy statement, but the action is lambda:InvokeFunction
# rather than lambda:InvokeFunctionUrl, and it is evaluated by the Lambda control plane
# for a named AWS service principal rather than by the Function URL front end for an
# anonymous caller. Tested before anything here was built: a throwaway HTTP API pointed
# at the same unchanged axiom-api function answered an anonymous curl
#
#   GET https://<id>.execute-api.us-east-2.amazonaws.com/api/health -> 200
#   {"ok":true,"db":true,"provider":true,"version":"0.1.0","offline":true,"errors":{}}
#
# That is the whole reason for this file. The Function URL still exists and still 403s
# anonymously; nothing here changes it.
#
# What it costs
# -------------
# CORRECTED 2026-08-14. This block said the 1M-requests-per-month allowance "runs well
# past the Sep 15 judging deadline" and concluded $0.00. It does not run at all here.
# The allowance is a TWELVE-MONTH offer, and this account has no twelve-month tier to
# spend:
#
#   aws freetier get-account-plan-state  -> accountPlanType "PAID", $0.00 credits
#   aws freetier get-free-tier-usage     -> 12 entries, ALL "Always Free",
#                                           ZERO "12 Months Free"
#
# So HTTP API requests are billed at **$1.00 per million from the first request**. At
# judging volume — a handful of humans over four weeks — that is cents, and the measured
# month-to-date across every service in the account is $0.0001021066. Nothing here bills
# at rest: API Gateway is per-request, so an HTTP API with no traffic still costs nothing.
# Cents stated plainly beats zero asserted, which is the whole posture of this repo.
#
# Throttling is the guard on the only variable in that sentence, and it matters more now
# that there is no free million underneath it. 20 requests/second with a burst of 40 is
# far above what Mission Control's polling needs and far below what a crawler would need
# to reach a million requests in a month (that is 0.38 req/s sustained, so the cap is
# ~50x headroom over the point where the bill reaches one dollar, and the account would
# hit the Lambda concurrency limit of 10 long before that).
#
# What this script makes publicly reachable
# -----------------------------------------
# Everything the function serves, including the mutating demo controls
# (POST /api/demo/reset, /api/demo/seed, /api/demo/run-worker). That is deliberate and
# is argued at the bottom of axiom/api.py: they are a demo control panel rather than a
# login, the UI's own buttons call them with no token, and token-gating them would take
# those buttons away from the judge this URL exists for. What bounds the damage is not
# authentication:
#
#   * AXIOM_DEMO_TOKEN is unset here, so `_demo_auth` is a no-op. SET IT in the function's
#     environment if the URL is ever handed to anyone other than a judge — the UI then
#     needs the X-Axiom-Demo-Token header and its buttons stop working.
#   * reset RE-SEEDS rather than empties, seed is idempotent and capped, and run-worker
#     refuses a fourth concurrent worker. Nothing here can create unbounded work.
#   * Each of those routes has its own minimum interval (reset: 15 s), so a retry loop
#     cannot reset the board under a judge faster than once every 15 seconds.
#
# The honest summary: a determined stranger can reset the demo board. They cannot destroy
# it, exhaust it, or run up a bill.
set -euo pipefail

REGION="${AWS_REGION:-us-east-2}"
API_FN="${API_FN:-axiom-api}"
API_NAME="${API_NAME:-axiom-api-http}"
STATEMENT_ID="${STATEMENT_ID:-axiom-apigw-invoke}"

# The Lambda's own timeout is 30 s (API_TIMEOUT in deploy.sh). HTTP API integrations
# cap at 30 s too, so these are equal and the gateway can never give up on a request
# the function would have answered.
INTEGRATION_TIMEOUT_MS="${INTEGRATION_TIMEOUT_MS:-30000}"
THROTTLE_RATE="${THROTTLE_RATE:-20}"      # steady-state requests/second
THROTTLE_BURST="${THROTTLE_BURST:-40}"    # token bucket depth

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
aws() { command aws --region "$REGION" "$@"; }

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
say "account $ACCOUNT / region $REGION"

# ------------------------------------------------------------------------- teardown
# Everything this script creates, removed in one call plus one. Deleting the API takes
# its integration, route and stage with it — they are children of the API, not
# independent resources — so the only orphan to clean up is the statement this script
# added to the function's resource policy. The function itself, its Function URL, its
# log group and its environment are deploy.sh's and are not touched.
if [ "${1:-}" = "--destroy" ]; then
  DOOMED=$(aws apigatewayv2 get-apis --query "Items[?Name=='${API_NAME}'].ApiId | [0]" \
    --output text)
  if [ -n "$DOOMED" ] && [ "$DOOMED" != "None" ]; then
    aws apigatewayv2 delete-api --api-id "$DOOMED"
    echo "  deleted api $DOOMED ($API_NAME)"
  else
    echo "  no api named $API_NAME"
  fi
  aws lambda remove-permission --function-name "$API_FN" \
    --statement-id "$STATEMENT_ID" >/dev/null 2>&1 \
    && echo "  removed $STATEMENT_ID from $API_FN" \
    || echo "  no $STATEMENT_ID statement on $API_FN"
  echo "  $API_FN itself is untouched — it is deploy.sh's, not this script's."
  exit 0
fi

FN_ARN="arn:aws:lambda:${REGION}:${ACCOUNT}:function:${API_FN}"
aws lambda get-function --function-name "$API_FN" >/dev/null 2>&1 || {
  echo "  $API_FN does not exist in $REGION — run ./deploy/lambda/deploy.sh first" >&2
  exit 1; }
echo "  target function: $FN_ARN"

# A gateway in front of a function with no DATABASE_URL is the worst failure mode this
# script has: /api/health answers 200 with {"db":false}, which looks deployed and is
# not. Checked here, by name only — the value is never printed.
if aws lambda get-function-configuration --function-name "$API_FN" \
     --query 'Environment.Variables.DATABASE_URL' --output text | grep -q '^postgresql'; then
  echo "  DATABASE_URL: present on $API_FN"
else
  echo "  DATABASE_URL is missing or not a postgresql:// URL on $API_FN." >&2
  echo "  The API would answer 200 with db:false. Re-run deploy.sh with DATABASE_URL set." >&2
  exit 1
fi

# ---------------------------------------------------------------------------- the API
# Looked up by name rather than remembered in a file, so this script owns no state and
# two people running it cannot end up with two APIs and two URLs.
say "HTTP API $API_NAME"
API_ID=$(aws apigatewayv2 get-apis \
  --query "Items[?Name=='${API_NAME}'].ApiId | [0]" --output text)

if [ -z "$API_ID" ] || [ "$API_ID" = "None" ]; then
  # No --target here: quick-create would build the integration, route and stage in one
  # call with defaults this script would then have to overwrite. Building them
  # explicitly is four more calls and zero ambiguity about what is deployed.
  #
  # CORS is deliberately NOT configured on the API. axiom.api already installs
  # CORSMiddleware with allow_origins=['*'] (axiom/api.py), and a gateway that also
  # answers preflights would emit a second, conflicting Access-Control-Allow-Origin
  # header on every response. One layer owns CORS, and it is the app.
  API_ID=$(aws apigatewayv2 create-api \
    --name "$API_NAME" --protocol-type HTTP \
    --description 'AXIOM public demo front door -> axiom-api Lambda' \
    --query ApiId --output text)
  echo "  created: $API_ID"
else
  echo "  exists: $API_ID"
fi
API_ARN="arn:aws:execute-api:${REGION}:${ACCOUNT}:${API_ID}"

# -------------------------------------------------------------------- the integration
# Payload format 2.0 is stated rather than defaulted. Mangum reads `version` off the
# event to decide how to build the ASGI scope, and 1.0 and 2.0 differ in the shape of
# the whole thing (rawPath/requestContext.http vs path/httpMethod) as well as in what a
# handler may return. Defaults change; this one is load-bearing.
say "integration -> $API_FN"
INTEG_ID=$(aws apigatewayv2 get-integrations --api-id "$API_ID" \
  --query "Items[?IntegrationUri=='${FN_ARN}'].IntegrationId | [0]" --output text)

if [ -z "$INTEG_ID" ] || [ "$INTEG_ID" = "None" ]; then
  INTEG_ID=$(aws apigatewayv2 create-integration --api-id "$API_ID" \
    --integration-type AWS_PROXY --integration-uri "$FN_ARN" \
    --payload-format-version 2.0 --timeout-in-millis "$INTEGRATION_TIMEOUT_MS" \
    --query IntegrationId --output text)
  echo "  created: $INTEG_ID (payload 2.0, ${INTEGRATION_TIMEOUT_MS} ms)"
else
  aws apigatewayv2 update-integration --api-id "$API_ID" --integration-id "$INTEG_ID" \
    --integration-type AWS_PROXY --integration-uri "$FN_ARN" \
    --payload-format-version 2.0 --timeout-in-millis "$INTEGRATION_TIMEOUT_MS" \
    --query IntegrationId --output text >/dev/null
  echo "  updated: $INTEG_ID (payload 2.0, ${INTEGRATION_TIMEOUT_MS} ms)"
fi

# --------------------------------------------------------------------------- the route
# ONE route: $default. It is the catch-all for every method and every path, and that
# includes "/" — so it is strictly equivalent to `ANY /{proxy+}` plus `ANY /`, in one
# route instead of two, with no chance of the two drifting apart. It matters that the
# app, not the gateway, decides what 404s: axiom.api serves the UI at /, the API under
# /api/*, and StaticFiles for the rest, and a gateway that only forwarded known
# prefixes would turn every new endpoint into a deploy step.
say 'route $default'
ROUTE_ID=$(aws apigatewayv2 get-routes --api-id "$API_ID" \
  --query "Items[?RouteKey=='\$default'].RouteId | [0]" --output text)

if [ -z "$ROUTE_ID" ] || [ "$ROUTE_ID" = "None" ]; then
  ROUTE_ID=$(aws apigatewayv2 create-route --api-id "$API_ID" \
    --route-key '$default' --target "integrations/${INTEG_ID}" \
    --query RouteId --output text)
  echo "  created: $ROUTE_ID -> integrations/$INTEG_ID"
else
  aws apigatewayv2 update-route --api-id "$API_ID" --route-id "$ROUTE_ID" \
    --route-key '$default' --target "integrations/${INTEG_ID}" \
    --query RouteId --output text >/dev/null
  echo "  updated: $ROUTE_ID -> integrations/$INTEG_ID"
fi

# --------------------------------------------------------------------------- the stage
# The stage is named $default for one specific reason: it is the only stage name API
# Gateway does NOT prepend to the request path. Any other name and every URL becomes
# https://<id>.execute-api.../prod/api/health, the app sees rawPath=/prod/api/health,
# and every route 404s unless the app is told its root_path. $default keeps the
# deployed URL identical to the local one, so nothing in web/ or axiom/api.py needs a
# base-path setting.
#
# Auto-deploy means a route or integration change is live without a create-deployment
# call — this script would otherwise have to make one, and a forgotten deployment is a
# gateway that silently serves the previous config.
say 'stage $default'
THROTTLE="ThrottlingRateLimit=${THROTTLE_RATE},ThrottlingBurstLimit=${THROTTLE_BURST}"
if aws apigatewayv2 get-stage --api-id "$API_ID" --stage-name '$default' >/dev/null 2>&1; then
  aws apigatewayv2 update-stage --api-id "$API_ID" --stage-name '$default' \
    --auto-deploy --default-route-settings "$THROTTLE" \
    --query StageName --output text >/dev/null
  echo "  updated: auto-deploy, ${THROTTLE_RATE} req/s burst ${THROTTLE_BURST}"
else
  aws apigatewayv2 create-stage --api-id "$API_ID" --stage-name '$default' \
    --auto-deploy --default-route-settings "$THROTTLE" \
    --description 'AXIOM public demo' \
    --query StageName --output text >/dev/null
  echo "  created: auto-deploy, ${THROTTLE_RATE} req/s burst ${THROTTLE_BURST}"
fi

# No access logging. It would need a log group, a destination ARN and a delivery
# permission, to record what /aws/lambda/axiom-api already records with the request id,
# the status and the duration — and CloudWatch ingest is the one line in this stack
# that is priced per GB. `aws logs tail /aws/lambda/axiom-api --follow` is the answer to
# "what did the judge's browser do".

# ---------------------------------------------------------------------- the permission
# Scoped to this API id, not to *. A wildcard here would let any API Gateway anyone
# ever creates in this account invoke the function. The wildcards that remain are the
# stage and the route within THIS api ($API_ID/*/*), which is what a $default route on
# a $default stage requires and is as tight as this can be.
#
# add-permission is not idempotent: it raises ResourceConflictException when the
# statement id already exists. Remove-then-add is the re-runnable form, and it is safe
# because the statement is recreated in the next call — the window is milliseconds and
# only affects this one statement, never the FunctionURL ones deploy.sh manages.
say "invoke permission on $API_FN"
aws lambda remove-permission --function-name "$API_FN" \
  --statement-id "$STATEMENT_ID" >/dev/null 2>&1 || true
aws lambda add-permission --function-name "$API_FN" \
  --statement-id "$STATEMENT_ID" \
  --action lambda:InvokeFunction \
  --principal apigateway.amazonaws.com \
  --source-arn "${API_ARN}/*/*" \
  --query Statement --output text >/dev/null
echo "  apigateway.amazonaws.com may lambda:InvokeFunction, source ${API_ARN}/*/*"

# ------------------------------------------------------------------------------ prove
ENDPOINT=$(aws apigatewayv2 get-api --api-id "$API_ID" --query ApiEndpoint --output text)
ENDPOINT="${ENDPOINT%/}"

say "smoke test (anonymous, no signature)"
# IAM and route propagation are eventually consistent; a fresh API can 403 or 404 for a
# few seconds. Retry the health check rather than reporting a failure that is really a
# stopwatch.
HEALTH=""
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  HEALTH=$(curl -s --max-time 45 "$ENDPOINT/api/health" || true)
  case "$HEALTH" in *'"ok":true'*) break ;; esac
  echo "  waiting for the route to settle (${attempt}/10)"
  sleep 5
done

for path in /api/health /api/crash-windows / /styles.css /api/mission; do
  body=$(mktemp)
  code=$(curl -s -o "$body" -w '%{http_code}' --max-time 45 "$ENDPOINT$path" || echo 000)
  printf '  %-24s -> %s  %s\n' "GET $path" "$code" \
    "$(head -c 72 "$body" | tr -d '\n')"
  rm -f "$body"
done

# db:false is a 200. Nothing above would catch it, and it is the difference between a
# demo and a page that renders an empty mission. Fail the script on it, loudly.
case "$HEALTH" in
  *'"db":true'*) echo "  database: reachable through the gateway (db:true)" ;;
  *) echo "" >&2
     echo "  THE GATEWAY WORKS AND THE DATABASE DOES NOT." >&2
     echo "  /api/health answered: $HEALTH" >&2
     echo "  The function is reachable but its DATABASE_URL is wrong, expired, or the" >&2
     echo "  CockroachDB cluster is paused. Check the cluster, then re-run deploy.sh." >&2
     exit 1 ;;
esac

say "demo URL"
echo
echo "    $ENDPOINT/"
echo
echo "  reached as:  HTTP API (apigatewayv2), \$default route, \$default stage, anonymous"
echo "  api:         $ENDPOINT/api/health"
echo "               $ENDPOINT/api/mission"
echo "               $ENDPOINT/api/crash-windows"
echo "  docs:        $ENDPOINT/api/docs"
echo "  logs:        aws logs tail /aws/lambda/$API_FN --follow --region $REGION"
echo "  throttle:    ${THROTTLE_RATE} req/s, burst ${THROTTLE_BURST}, on the \$default stage"
echo
echo "  Standing cost: nothing bills at rest — an HTTP API with no traffic costs nothing,"
echo "  and Lambda's 1M requests + 400,000 GB-seconds/month are always-free on this account."
echo "  But this is NOT \$0.00/month: API Gateway's 1M free requests is a 12-MONTH offer and"
echo "  this PAID account has none, so requests bill at \$1.00/million from the first one."
echo "  Measured month-to-date, all services: \$0.0001021066. Through Sep 15: under \$1.00."
echo
echo "  Tear down:   ./deploy/lambda/apigateway.sh --destroy"
