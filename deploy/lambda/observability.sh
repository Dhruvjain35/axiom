#!/usr/bin/env bash
# AXIOM :: keep the demo alive, and tell somebody when it is not.
#
#   ./deploy/lambda/observability.sh              # create or update everything
#   ./deploy/lambda/observability.sh --status     # read out what exists, change nothing
#   ./deploy/lambda/observability.sh --pause      # stop the sweep (see THE TEST SUITE)
#   ./deploy/lambda/observability.sh --resume     # start it again
#   ./deploy/lambda/observability.sh --fire-test-alarm
#   ./deploy/lambda/observability.sh --destroy    # remove exactly what this file made
#
# Run ./deploy/lambda/deploy.sh and ./deploy/lambda/apigateway.sh first. This script
# watches those two functions and that one gateway; it does not create them, and it
# refuses to run if they are not there.
#
# THE PROBLEM
# -----------
# Judging runs Aug 19 - Sep 15 with nobody watching. Everything else in deploy/ is about
# getting the demo UP. This file is about the four weeks after that, where exactly two
# things can go wrong and neither of them announces itself:
#
#   (a) it degrades quietly. A judge starts a chaos worker, watches it die inside crash
#       window W4 exactly as designed, and closes the tab. Three tasks are left holding
#       an expired lease. AXIOM recovers those the moment ANY worker runs — that is the
#       whole product — but nothing runs one. Eleven days later the next judge opens a
#       board frozen mid-recovery. The system is correct and looks broken.
#   (b) it breaks and nobody finds out for a week. The cluster's password rotates, the
#       function starts 500ing, and the first person to notice is the judge.
#
# (a) is answered by a schedule that drains the queue every five minutes. (b) is answered
# by alarms with an email on the end of them. Both have to fit in the free tier, because
# an unexpected charge on this account during judging suspends it and takes the demo down.
#
# WHAT IT CREATES  (all in us-east-2 except where noted)
# ------------------------------------------------------
#   SNS topic       axiom-ops-alerts        + an EMAIL subscription that starts PENDING
#   IAM role        axiom-scheduler-role    scheduler.amazonaws.com -> InvokeFunction
#   Schedule        axiom-worker-sweep      rate(5 minutes) -> axiom-worker
#   Alarm           axiom-api-errors        Lambda Errors on axiom-api
#   Alarm           axiom-api-throttles     Lambda Throttles on axiom-api
#   Alarm           axiom-http-5xx          API Gateway 5xx on the HTTP API
#   Alarm           axiom-worker-errors     Lambda Errors on axiom-worker (storm only)
#   Alarm           axiom-worker-silent     Invocations FLOOR on axiom-worker
#   Dashboard       axiom-ops               invocations / errors / p95 / 4xx / 5xx
#
# It also VERIFIES, and never modifies, the AWS Budget that billing_guard.sh owns.
#
# WHAT IT COSTS
# -------------
# Under a cent a month, and here is the arithmetic rather than the assertion. This block
# said "$0.00/month" when it was written; the correction is small here and large elsewhere,
# because THIS file's services are the ones that really are free on this account. Verified
# against the account rather than a pricing page: `aws freetier get-free-tier-usage`
# returns 12 entries, all "Always Free", none "12 Months Free" — and CloudWatch, SNS and
# Lambda are all among them. (API Gateway and X-Ray, created by the other two scripts,
# are NOT, and are billed. See deploy/lambda/COST.md.)
#
#   EventBridge Scheduler  8,640 invocations/month (every 5 min). Scheduler is not among
#                          this account's twelve free-tier entries, so price it rather
#                          than assume it: 8,640 at $1.00/million is $0.0086/month.
#                          Published free-tier allowances are irrelevant when the account
#                          does not have them; that is the mistake this whole pass fixes.
#   Lambda                 MEASURED on a real scheduled sweep against an empty queue,
#                          including its cold start: 0 tasks, 1,978 ms wall, 2,387 ms
#                          billed, 83 MB of 512 used. So 8,640 x 2.387 s x 0.5 GB =
#                          ~10,300 GB-s/month against an ALWAYS-free 400,000 = 2.6%,
#                          and that is the pessimistic figure because a warm sweep costs
#                          less. Requests: 8,640 of 1,000,000 = 0.9%.
#   CloudWatch alarms      5 standard-resolution alarms. Always-free tier is 10 alarm
#                          metrics per account — genuinely always-free HERE, confirmed in
#                          get-free-tier-usage — and billing_guard.sh already holds 1
#                          (axiom-estimated-charges, us-east-1). 6 of 10.
#   CloudWatch dashboard   1. First 3 per account are free. NOT shared publicly —
#                          dashboard sharing is a paid feature and this file does not
#                          touch it. See A JUDGE CANNOT SEE THE DASHBOARD below.
#   SNS                    free tier is 1,000 email notifications/month, and SNS IS one of
#                          this account's Always Free entries. These alarms are designed to
#                          send single digits of emails per month. Note the cost of an
#                          UNCONFIRMED subscription is also $0.00, which is exactly the
#                          problem — free and silent look identical on a bill.
#   CloudWatch Logs        the sweep's own log lines, and this is the only line item here
#                          priced per GB. Measured on that same sweep: 7 events, 807
#                          bytes, so 8,640 x 807 B = 7.0 MB/month against a 5 GB/month
#                          free ingest allowance = 0.139%. Retention on
#                          /aws/lambda/axiom-worker is already 7 days, so stored volume
#                          is bounded too.
#   IAM, schedule groups   free.
#
# Nothing here bills per hour. There is no NAT gateway, no ALB, no ECS task, no
# provisioned concurrency, no OpenSearch, no Kinesis shard, no custom schedule group.
#
# Total for this file: ~$0.0086/month, all of it EventBridge Scheduler. The whole-account
# measured month-to-date, every service, was $0.0001021066 on 2026-08-14, and the
# axiom-zero-spend budget emails the owner at one cent.
#
# WHY FIVE MINUTES
# ----------------
# The interval is a three-way constraint and 5 minutes is the only value that satisfies
# all three comfortably:
#
#   * It has to be fast enough that a judge never lands on a frozen board. The worst
#     case a judge can see is (interval - epsilon) of staleness, and five minutes of
#     staleness is invisible: the UI's own auto-heal (axiom/api.py `_start_worker`, the
#     `should_autoheal` path) fires on their first poll anyway. The schedule's job is to
#     make that heal already-finished when they arrive rather than racing their eyeballs.
#   * It has to be slow enough to stay inside the allowance that IS free here. See the
#     arithmetic above: 2.6% of Lambda's always-free 400,000 GB-s, measured. One minute
#     would be 13% and still inside it, but it buys nothing — see the point above — and it
#     multiplies both the Scheduler line item and the test-suite interaction below by five.
#   * It has to be much longer than one invocation, so sweeps can never pile up. A sweep
#     claims for at most SWEEP_SECONDS (45) and then finishes the task in flight within
#     lambda_worker.DEFAULT_MARGIN_MS (6 s), so its ceiling is ~51 s: 17% of the period,
#     ~5.8x headroom. The function's own 300 s timeout is the hard backstop underneath
#     that, and FlexibleTimeWindow is capped at 1 minute so two ticks can never land
#     closer together than 4 minutes.
#
# THE TEST SUITE  (read this before changing the interval)
# --------------------------------------------------------
# tests/conftest.py::_exclusive_queue refuses to start a session while any row in
# axiom_agent has status IN ('STARTING','ALIVE') and heartbeat_at inside 30 seconds. That
# guard is CORRECT — claim() is deliberately not tenant-scoped, so a live worker would
# claim the suite's tasks and settle them out from under a crash-window assertion — and
# nothing here should weaken it.
#
# The good news is measured, not hoped for: a sweep that finishes normally does NOT trip
# it. lambda_worker.drain()'s finally calls Worker.stop(), which calls tasks.stop_agent(),
# which sets status='DEAD' — and 'DEAD' is not in the guard's list. So the window in which
# the suite is blocked is the invocation itself, not invocation + 30 s:
#
#   queue empty (the steady state)   ~2.0 s of every 300 s   = 0.7% of the time
#   queue full (right after a reset) ~51 s of every 300 s    = 17% of the time
#
# The 30-second tail only appears after an invocation that died without running its
# finally — i.e. `{"mode":"chaos"}`, which this schedule never sends.
#
# So the honest instruction for running the suite against the SHARED Cloud cluster is:
# if it exits 2 saying another worker is alive, wait 40 seconds and run it again. If you
# are about to do a long run, or record a demo take, turn the sweep off explicitly:
#
#   ./deploy/lambda/observability.sh --pause      # sets the schedule state to DISABLED
#   ./.venv/bin/python -m pytest -q
#   ./deploy/lambda/observability.sh --resume
#
# --pause is a state flip on the schedule, not a delete: the target, the role and the
# retry policy survive it, so --resume cannot reconstruct them wrongly. Note that pausing
# for more than two hours will trip axiom-worker-silent, which is the alarm doing its job.
#
# scripts/uptime_check.sh is unaffected either way. It only reads HTTP endpoints; it never
# claims a task, so it has no exclusivity requirement to violate.
#
# WHY THE WORKER'S ERROR ALARM IS SET SO HIGH
# -------------------------------------------
# Because on THIS system a Lambda error on axiom-worker is frequently the demo SUCCEEDING.
# `POST /api/demo/run-worker {"mode":"chaos"}` invokes axiom-worker asynchronously with
# chaos_post=1.0, the worker answers the armed window with os._exit(9), and Lambda records
# that as an Error — then retries the async event twice more by default, so ONE judge
# pressing ONE button can post three Errors. An alarm at "> 0 errors" would email on every
# single demonstration of the headline feature and be muted inside a day.
#
# So axiom-worker-errors is deliberately a STORM detector (>30 in each of two consecutive
# 15-minute windows) and it is not the load-bearing worker alarm. The load-bearing one is
# axiom-worker-silent, which watches the Invocations FLOOR: the sweep should produce 12
# invocations an hour, and fewer than 6 for two consecutive hours means the schedule, the
# role, or the function is broken. That is failure mode (a) from the top of this file,
# and an error-count alarm structurally cannot see it — a queue that has stopped draining
# emits no errors at all, which is exactly why it goes unnoticed for a week.
#
# The clean fix for the ambiguity is a CloudWatch Logs metric filter that separates
# "Runtime exited with error: exit status 9" (deliberate) from a Python traceback (real).
# That is reported, not done: it is another resource type for a signal the floor alarm
# already covers.
#
# THE HARDENING STEP THAT SILENTLY BREAKS THE SCHEDULE
# ----------------------------------------------------
# The recommended confused-deputy guard on a Scheduler execution role is an aws:SourceArn
# condition pinning it to the schedule. Adding it here is accepted by IAM, accepted by
# UpdateSchedule, leaves the schedule reporting State=ENABLED — and drops every single
# invocation, with no Lambda log and no error visible anywhere except the AWS/Scheduler
# metric namespace. Measured on this account across three consecutive ticks. The full
# write-up, both failure modes and the numbers are at section 2; the short version is
# that the condition is deliberately absent and must stay absent.
#
# THE METRIC NAME THAT LOOKS RIGHT AND IS NOT
# -------------------------------------------
# HTTP APIs (apigatewayv2) publish `4xx` and `5xx`. REST APIs publish `4XXError` and
# `5XXError`. Every example you will find is written for REST, and an alarm on 5XXError
# against an HTTP API is not an error — it is a valid alarm on a metric that will never
# have a datapoint, sitting in INSUFFICIENT_DATA forever, looking installed. Verified on
# this account before the alarm was written:
#
#   $ aws cloudwatch list-metrics --namespace AWS/ApiGateway --region us-east-2
#     5xx  [('ApiId','nq0i2ob395')]        <- exists
#     5XXError                              <- does not
#
# A JUDGE CANNOT SEE THE DASHBOARD
# --------------------------------
# The dashboard is for whoever owns this account, not for a judge: viewing it needs a
# console login. CloudWatch can share a dashboard publicly and that is a PAID feature
# ($0.50 per shared dashboard per month) — roughly 5,000x the account's entire measured
# month-to-date spend, and the one line item here that would be a deliberate purchase
# rather than a rounding error. So this file does not enable it, and the README should not
# describe the dashboard as something a judge follows a link to. The telemetry a judge actually sees is /api/health, /api/mission and the proofs
# panel, which the application serves itself.
#
# THE EMAIL IS NOT WORKING UNTIL SOMEBODY CLICKS IT
# -------------------------------------------------
# `aws sns subscribe` for an EMAIL endpoint creates the subscription in state
# PendingConfirmation and sends an opt-in link to the address. Until that link is clicked
# the topic has zero confirmed subscribers and every alarm on it fires into nothing. AWS
# deletes an unconfirmed subscription after 3 days, and there is evidence on this very
# account that this has already happened once: billing_guard.sh subscribes
# adamkoners@gmail.com to axiom-billing-alerts in us-east-1, and that topic currently has
# zero subscriptions of any kind — the pending one lapsed unclicked.
#
# This script therefore prints the subscription's real state on every run and refuses to
# describe the alerting path as working while it is pending.
set -euo pipefail

REGION="${AWS_REGION:-us-east-2}"

# Billing is published only in us-east-1 whatever region you deploy to, and the Budgets
# API is global-behind-us-east-1. billing_guard.sh says the same thing for the same
# reason; this file only READS through it.
BILLING_REGION=us-east-1

API_FN="${API_FN:-axiom-api}"
WORKER_FN="${WORKER_FN:-axiom-worker}"
API_NAME="${API_NAME:-axiom-api-http}"

TOPIC_NAME="${TOPIC_NAME:-axiom-ops-alerts}"
ALERT_EMAIL="${ALERT_EMAIL:-adamkoners@gmail.com}"
ROLE_NAME="${ROLE_NAME:-axiom-scheduler-role}"
ROLE_POLICY="${ROLE_POLICY:-axiom-invoke-worker}"
SCHEDULE_NAME="${SCHEDULE_NAME:-axiom-worker-sweep}"
DASHBOARD_NAME="${DASHBOARD_NAME:-axiom-ops}"
BUDGET_NAME="${BUDGET_NAME:-axiom-zero-spend}"

# See WHY FIVE MINUTES. Both of these are load-bearing together: the rate sets the cost
# and the staleness ceiling, SWEEP_SECONDS sets the pile-up headroom.
SCHEDULE_RATE="${SCHEDULE_RATE:-rate(5 minutes)}"
SWEEP_SECONDS="${SWEEP_SECONDS:-45}"

# A fixed worker_ref, which overrides handler_worker._worker_ref for this caller only.
# That handler keys on the execution ENVIRONMENT and its reasoning is right for an
# invocation whose identity is ephemeral. A scheduled sweep's identity is not ephemeral:
# it is one recurring job for the whole judging window. Letting it derive a fresh ref
# per cold start would mint up to 8,640 rows in axiom_agent over four weeks; pinning it
# means register_agent's ON CONFLICT reuses ONE row forever, and `SELECT * FROM
# axiom_agent WHERE worker_ref = 'lambda-sweep'` tells you at a glance when the schedule
# last ran. Sweeps cannot overlap (51 s ceiling, 4-minute floor between ticks), so
# nothing else contends for that row.
SWEEP_WORKER_REF="${SWEEP_WORKER_REF:-lambda-sweep}"

ALARM_NAMES=(axiom-api-errors axiom-api-throttles axiom-http-5xx
             axiom-worker-errors axiom-worker-silent)

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }
warn() { printf '    \033[33m%s\033[0m\n' "$*"; }
bad()  { printf '    \033[31m%s\033[0m\n' "$*"; }

aws() { command aws --region "$REGION" "$@"; }

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
TOPIC_ARN="arn:aws:sns:${REGION}:${ACCOUNT}:${TOPIC_NAME}"
ROLE_ARN="arn:aws:iam::${ACCOUNT}:role/${ROLE_NAME}"
WORKER_ARN="arn:aws:lambda:${REGION}:${ACCOUNT}:function:${WORKER_FN}"
SCHEDULE_ARN="arn:aws:scheduler:${REGION}:${ACCOUNT}:schedule/default/${SCHEDULE_NAME}"

# ================================================================== small read helpers

api_id() {
  aws apigatewayv2 get-apis --query "Items[?Name=='${API_NAME}'].ApiId | [0]" --output text
}

schedule_state() {
  aws scheduler get-schedule --name "$SCHEDULE_NAME" --group-name default \
    --query State --output text 2>/dev/null || echo ABSENT
}

# The one fact about the alerting path that matters. Prints CONFIRMED / PENDING / NONE.
subscription_state() {
  local arn
  arn=$(aws sns list-subscriptions-by-topic --topic-arn "$TOPIC_ARN" \
        --query "Subscriptions[?Endpoint=='${ALERT_EMAIL}'].SubscriptionArn | [0]" \
        --output text 2>/dev/null || echo None)
  case "$arn" in
    arn:aws:sns:*)        echo CONFIRMED ;;
    PendingConfirmation)  echo PENDING ;;
    *)                    echo NONE ;;
  esac
}

# ============================================================================= teardown
# Exactly what this file made, and nothing else. The two Lambdas, the HTTP API, their log
# groups, the AWS Budget and the us-east-1 billing topic/alarm all belong to deploy.sh,
# apigateway.sh and billing_guard.sh. Deleting the schedule does not touch the function
# it targeted; deleting the role does not touch the function's own execution role.
if [ "${1:-}" = "--destroy" ]; then
  say "destroying what observability.sh created (account $ACCOUNT / $REGION)"

  aws scheduler delete-schedule --name "$SCHEDULE_NAME" --group-name default \
    >/dev/null 2>&1 && note "deleted schedule $SCHEDULE_NAME" \
    || note "no schedule $SCHEDULE_NAME"

  # The inline policy has to go before the role will delete.
  command aws iam delete-role-policy --role-name "$ROLE_NAME" --policy-name "$ROLE_POLICY" \
    >/dev/null 2>&1 && note "deleted inline policy $ROLE_POLICY" || true
  command aws iam delete-role --role-name "$ROLE_NAME" \
    >/dev/null 2>&1 && note "deleted role $ROLE_NAME" || note "no role $ROLE_NAME"

  aws cloudwatch delete-alarms --alarm-names "${ALARM_NAMES[@]}" >/dev/null 2>&1 \
    && note "deleted alarms: ${ALARM_NAMES[*]}" || note "no alarms to delete"

  aws cloudwatch delete-dashboards --dashboard-names "$DASHBOARD_NAME" >/dev/null 2>&1 \
    && note "deleted dashboard $DASHBOARD_NAME" || true

  # Subscriptions first. A PendingConfirmation subscription has no ARN and cannot be
  # unsubscribed — delete-topic takes it with the topic, which is why this order works
  # for both the confirmed and the unconfirmed case.
  for sub in $(aws sns list-subscriptions-by-topic --topic-arn "$TOPIC_ARN" \
                 --query 'Subscriptions[?starts_with(SubscriptionArn, `arn:`)].SubscriptionArn' \
                 --output text 2>/dev/null || true); do
    aws sns unsubscribe --subscription-arn "$sub" >/dev/null 2>&1 \
      && note "unsubscribed $sub" || true
  done
  aws sns delete-topic --topic-arn "$TOPIC_ARN" >/dev/null 2>&1 \
    && note "deleted topic $TOPIC_NAME" || note "no topic $TOPIC_NAME"

  note ""
  note "UNTOUCHED, deliberately: $API_FN, $WORKER_FN, the HTTP API, their log groups,"
  note "the AWS Budget $BUDGET_NAME, and the us-east-1 billing topic and alarm."
  exit 0
fi

# ======================================================================= pause / resume
# See THE TEST SUITE. A state flip, not a delete: the target, the input payload, the
# role and the retry policy all survive, so --resume restores the exact schedule rather
# than reconstructing one from these variables.
if [ "${1:-}" = "--pause" ] || [ "${1:-}" = "--resume" ]; then
  WANT=ENABLED; [ "${1}" = "--pause" ] && WANT=DISABLED
  CUR=$(schedule_state)
  if [ "$CUR" = "ABSENT" ]; then
    bad "no schedule named $SCHEDULE_NAME — run this script with no flags first"
    exit 1
  fi
  # update-schedule is a full replace, so every field has to be restated or it is
  # dropped. Read the live schedule, change one key, put it back — the only form of this
  # that cannot silently lose the retry policy.
  TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
  aws scheduler get-schedule --name "$SCHEDULE_NAME" --group-name default \
    --output json > "$TMP/cur.json"
  python3 - "$TMP/cur.json" "$WANT" > "$TMP/put.json" <<'PY'
import json, sys
cur = json.load(open(sys.argv[1]))
cur['State'] = sys.argv[2]
# Server-assigned fields. update-schedule rejects any key that is not part of its input
# shape, so these three have to come back out before the document is handed back.
for k in ('Arn', 'CreationDate', 'LastModificationDate'):
    cur.pop(k, None)
print(json.dumps(cur))
PY
  aws scheduler update-schedule --cli-input-json "file://$TMP/put.json" \
    --query ScheduleArn --output text >/dev/null
  say "schedule $SCHEDULE_NAME: $CUR -> $(schedule_state)"
  [ "$WANT" = DISABLED ] && note "the queue is no longer being swept. --resume when done."
  [ "$WANT" = ENABLED ]  && note "sweeping every 5 minutes again."
  exit 0
fi

# =========================================================================== fire a test
# Puts one alarm into ALARM by hand so the alarm -> SNS wiring is proven by execution
# rather than by reading the config. set-alarm-state is a control-plane call, costs
# nothing, runs the alarm's actions for real, and is temporary: CloudWatch overwrites the
# state on its next evaluation. It is set back to OK here anyway rather than left for the
# next evaluation to tidy up.
#
# What this proves and what it does not: it proves the alarm holds a valid topic ARN and
# that SNS accepted the publish. It cannot prove delivery while the email subscription is
# PENDING — with no confirmed subscriber SNS accepts the message and drops it. That is
# stated in the output rather than glossed.
if [ "${1:-}" = "--fire-test-alarm" ]; then
  TARGET="${2:-axiom-api-errors}"
  say "firing $TARGET into ALARM deliberately"
  BEFORE=$(aws cloudwatch get-metric-statistics --namespace AWS/SNS \
    --metric-name NumberOfMessagesPublished --dimensions Name=TopicName,Value="$TOPIC_NAME" \
    --start-time "$(date -u -v-20M '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u -d '20 min ago' '+%Y-%m-%dT%H:%M:%SZ')" \
    --end-time "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" --period 60 --statistics Sum \
    --query 'sum(Datapoints[].Sum) || `0`' --output text)
  note "SNS publishes on $TOPIC_NAME in the last 20 min, before: $BEFORE"

  aws cloudwatch set-alarm-state --alarm-name "$TARGET" --state-value ALARM \
    --state-reason 'deliberate test from observability.sh --fire-test-alarm'
  note "state now: $(aws cloudwatch describe-alarms --alarm-names "$TARGET" \
        --query 'MetricAlarms[0].StateValue' --output text)"

  note "waiting 60s for the SNS publish metric to land"
  sleep 60
  AFTER=$(aws cloudwatch get-metric-statistics --namespace AWS/SNS \
    --metric-name NumberOfMessagesPublished --dimensions Name=TopicName,Value="$TOPIC_NAME" \
    --start-time "$(date -u -v-20M '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u -d '20 min ago' '+%Y-%m-%dT%H:%M:%SZ')" \
    --end-time "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" --period 60 --statistics Sum \
    --query 'sum(Datapoints[].Sum) || `0`' --output text)
  note "after: $AFTER"

  aws cloudwatch set-alarm-state --alarm-name "$TARGET" --state-value OK \
    --state-reason 'test complete, restored by observability.sh'
  note "restored to $(aws cloudwatch describe-alarms --alarm-names "$TARGET" \
        --query 'MetricAlarms[0].StateValue' --output text)"
  note "subscription is $(subscription_state) — a PENDING subscription receives nothing."
  exit 0
fi

# ================================================================================ status
if [ "${1:-}" = "--status" ]; then
  say "observability status (account $ACCOUNT / $REGION)"
  note "schedule $SCHEDULE_NAME: $(schedule_state)"
  note "topic    $TOPIC_NAME: subscription for $ALERT_EMAIL is $(subscription_state)"
  aws cloudwatch describe-alarms --alarm-names "${ALARM_NAMES[@]}" \
    --query 'MetricAlarms[].[AlarmName,StateValue]' --output text \
    | while read -r n s; do note "alarm    $n: $s"; done
  note "dashboard $DASHBOARD_NAME: $(aws cloudwatch list-dashboards \
       --query "length(DashboardEntries[?DashboardName=='${DASHBOARD_NAME}'])" --output text)"
  exit 0
fi

# ======================================================================== preconditions
say "account $ACCOUNT / region $REGION"

for fn in "$API_FN" "$WORKER_FN"; do
  aws lambda get-function --function-name "$fn" >/dev/null 2>&1 || {
    bad "$fn does not exist in $REGION — run ./deploy/lambda/deploy.sh first"; exit 1; }
done
note "functions: $API_FN, $WORKER_FN"

API_ID=$(api_id)
[ -n "$API_ID" ] && [ "$API_ID" != "None" ] || {
  bad "no HTTP API named $API_NAME — run ./deploy/lambda/apigateway.sh first"; exit 1; }
note "http api: $API_ID"

# A schedule pointed at a worker with no DATABASE_URL would run 8,640 times a month and
# fail 8,640 times, which is a worse outcome than not scheduling it at all. Checked by
# key name; the value is never printed.
aws lambda get-function-configuration --function-name "$WORKER_FN" \
  --query 'Environment.Variables.DATABASE_URL' --output text | grep -q '^postgresql' || {
  bad "$WORKER_FN has no postgresql:// DATABASE_URL. Re-run deploy.sh before scheduling it."
  exit 1; }
note "DATABASE_URL: present on $WORKER_FN"

# ============================================================================ 1. the topic
# Created before the alarms, because an alarm needs a topic ARN to point at. A CloudWatch
# alarm can only publish to an SNS topic in ITS OWN region, which is why this one is in
# us-east-2 with the alarms rather than joining billing_guard.sh's us-east-1 topic.
say "SNS topic $TOPIC_NAME"
aws sns create-topic --name "$TOPIC_NAME" --query TopicArn --output text >/dev/null
note "topic $TOPIC_ARN"

# Scoped to cloudwatch.amazonaws.com and to alarms in THIS account, rather than the
# default policy's account-wide publish. Nothing else needs to publish here.
POLICY=$(python3 - "$TOPIC_ARN" "$ACCOUNT" <<'PY'
import json, sys
topic, account = sys.argv[1], sys.argv[2]
print(json.dumps({
    "Version": "2012-10-17",
    "Statement": [{
        "Sid": "AllowCloudWatchAlarms",
        "Effect": "Allow",
        "Principal": {"Service": "cloudwatch.amazonaws.com"},
        "Action": "SNS:Publish",
        "Resource": topic,
        "Condition": {"StringEquals": {"AWS:SourceAccount": account}},
    }],
}))
PY
)
aws sns set-topic-attributes --topic-arn "$TOPIC_ARN" \
  --attribute-name Policy --attribute-value "$POLICY"
note "policy: cloudwatch.amazonaws.com may SNS:Publish, source account $ACCOUNT"

SUB_STATE=$(subscription_state)
if [ "$SUB_STATE" = "NONE" ]; then
  aws sns subscribe --topic-arn "$TOPIC_ARN" --protocol email \
    --notification-endpoint "$ALERT_EMAIL" --query SubscriptionArn --output text >/dev/null
  SUB_STATE=$(subscription_state)
  note "subscribed $ALERT_EMAIL"
fi
case "$SUB_STATE" in
  CONFIRMED) note "subscription: CONFIRMED — alarms will reach $ALERT_EMAIL" ;;
  PENDING)   warn "subscription: PENDING. AWS emailed $ALERT_EMAIL an opt-in link."
             warn "UNTIL THAT LINK IS CLICKED THE ALARMS NOTIFY NOBODY, and AWS deletes"
             warn "an unconfirmed subscription after 3 days. Re-run this script to"
             warn "recreate it if it lapses." ;;
  *)         bad  "subscription: could not be read" ;;
esac

# ============================================================================ 2. the role
# EventBridge Scheduler assumes a role to invoke the target; it does not use a Lambda
# resource policy the way API Gateway does. A bare `Service: scheduler.amazonaws.com`
# principal is a confused-deputy hole — any schedule in any account that could name this
# role could invoke the worker — so the trust policy carries two conditions:
# aws:SourceAccount, and aws:SourceArn pinned to THIS schedule.
#
# THE aws:SourceArn CONDITION IS DELIBERATELY ABSENT, AND THAT COST TWO HOURS.
#
# Every guide, including AWS's own, tells you to add it. On this account it breaks the
# schedule twice, in two different ways, and the second way is silent:
#
#   1. CreateSchedule REJECTS a role that requires it. Scheduler validates assumability at
#      create time, the schedule does not exist yet, so the validation carries no
#      aws:SourceArn and a policy demanding one denies it. The error blames the principal
#      ("The execution role you provide must allow AWS EventBridge Scheduler to assume the
#      role"), and the principal is fine.
#   2. Applying it AFTER the schedule exists is accepted by UpdateSchedule and by IAM, and
#      then every delivery fails silently forever. No Lambda invocation, no Lambda log, no
#      error anywhere a person would look. The only place it is visible is the AWS/Scheduler
#      namespace, which nothing points you at.
#
# Both measured on this account rather than reasoned about. Same role, propagated, same
# target, same minute:
#
#   CREATE  trust = SourceAccount + SourceArn  -> ValidationException
#   CREATE  trust = SourceAccount              -> schedule/default/axiom-worker-sweep
#   UPDATE  trust = SourceAccount + SourceArn  -> ACCEPTED (this is the trap)
#
# and then, with that accepted policy in place, three consecutive ticks:
#
#   AWS/Scheduler InvocationAttemptCount   1, 1, 1
#   AWS/Scheduler TargetErrorCount         1, 1, 1
#   AWS/Scheduler InvocationDroppedCount   1, 1, 1
#   AWS/Lambda    Invocations              no datapoint
#   /aws/lambda/axiom-worker               no log stream
#
# A schedule that reports State=ENABLED, whose last modification succeeded, which fires on
# time, and which delivers nothing. That is precisely failure mode (a) from the top of this
# file, and it is why axiom-worker-silent watches the invocation FLOOR: it is the only
# alarm on this page that would have caught it.
#
# What is left is aws:SourceAccount, which still blocks every principal outside this
# account. The residual exposure is a different schedule INSIDE this account naming this
# role — and this account has exactly one schedule, created by this file.
say "IAM role $ROLE_NAME"

TRUST=$(python3 - "$ACCOUNT" <<'PY'
import json, sys
print(json.dumps({
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "scheduler.amazonaws.com"},
        "Action": "sts:AssumeRole",
        # See the block above before adding aws:SourceArn here. It is accepted and it
        # silently stops every delivery.
        "Condition": {"StringEquals": {"aws:SourceAccount": sys.argv[1]}},
    }],
}))
PY
)
SCHEDULE_WAS=$(schedule_state)

if command aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  command aws iam update-assume-role-policy --role-name "$ROLE_NAME" --policy-document "$TRUST"
  note "exists: $ROLE_ARN (trust policy refreshed)"
else
  command aws iam create-role --role-name "$ROLE_NAME" \
    --assume-role-policy-document "$TRUST" \
    --description 'EventBridge Scheduler -> axiom-worker. Created by observability.sh' \
    --query 'Role.Arn' --output text >/dev/null
  note "created: $ROLE_ARN"
fi

# Both ARNs: the unqualified one, and :* for any future version or alias. Not a wildcard
# on the function name — this role must never be able to invoke axiom-api.
PERM=$(python3 - "$WORKER_ARN" <<'PY'
import json, sys
arn = sys.argv[1]
print(json.dumps({
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow", "Action": "lambda:InvokeFunction",
                   "Resource": [arn, arn + ":*"]}],
}))
PY
)
command aws iam put-role-policy --role-name "$ROLE_NAME" \
  --policy-name "$ROLE_POLICY" --policy-document "$PERM"
note "inline $ROLE_POLICY -> lambda:InvokeFunction on $WORKER_FN only"

# ======================================================================== 3. the schedule
say "EventBridge Scheduler $SCHEDULE_NAME"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

# RetryPolicy is the anti-stampede setting and the default is the stampede: Scheduler
# retries a failed delivery up to 185 times over 24 hours. That default is written for a
# job that must eventually run. This one must NOT — the next tick is five minutes away and
# does the identical, idempotent work, so a delivery that fails should be forgotten rather
# than accumulated. One retry inside 60 seconds absorbs a transient throttle; anything
# beyond that would still be firing yesterday's sweeps into tomorrow's queue.
#
# MaximumEventAgeInSeconds has a floor of 60 in the API, so 60 is the tightest expression
# of "give up before the next tick".
#
# The Input is what bounds the invocation. `seconds` caps how long the drain may keep
# CLAIMING; lambda_worker arms a threading.Timer for it and Worker.run() checks the stop
# event between tasks, so the ceiling is seconds + the in-flight task. `idle_exit` is what
# makes the common case cheap: an empty queue returns in ~2.0 s instead of polling for 45.
python3 - "$WORKER_ARN" "$ROLE_ARN" "$SWEEP_SECONDS" "$SWEEP_WORKER_REF" \
  > "$TMP/target.json" <<'PY'
import json, sys
arn, role, secs, ref = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
print(json.dumps({
    "Arn": arn,
    "RoleArn": role,
    "Input": json.dumps({"mode": "drain", "seconds": secs,
                         "idle_exit": True, "worker_ref": ref}),
    "RetryPolicy": {"MaximumRetryAttempts": 1, "MaximumEventAgeInSeconds": 60},
}))
PY

# FlexibleTimeWindow lets AWS place the invocation anywhere in a 60-second window instead
# of firing on the exact tick. One minute, not the 15 the console suggests: it is enough
# to keep this off the top-of-minute pile-up that every other cron in every account is
# also sitting on, and small enough that two consecutive ticks can never converge closer
# than 4 minutes — which is what keeps the no-overlap guarantee true.
FTW='Mode=FLEXIBLE,MaximumWindowInMinutes=1'
DESC='AXIOM demo self-heal: drain the queue so an unattended judge never lands on a frozen board'

if [ "$SCHEDULE_WAS" = "ABSENT" ]; then
  # A role created seconds ago is not yet assumable everywhere, and Scheduler validates
  # the role at create time — so a brand-new role really does need a few seconds. That is
  # a stopwatch, not a failure, so retry it. Exhausting the retries IS a failure and must
  # exit non-zero: the first version of this loop fell through silently and reported a
  # complete install with no schedule in it, which is the exact species of overclaim this
  # project exists to argue against.
  CREATED=0
  for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if aws scheduler create-schedule --name "$SCHEDULE_NAME" --group-name default \
         --schedule-expression "$SCHEDULE_RATE" \
         --flexible-time-window "$FTW" --state ENABLED --description "$DESC" \
         --target "file://$TMP/target.json" \
         --query ScheduleArn --output text >/dev/null 2>"$TMP/err"; then
      note "created: $SCHEDULE_RATE -> $WORKER_FN"
      CREATED=1
      break
    fi
    if ! grep -qi 'role\|assume\|validation' "$TMP/err"; then
      cat "$TMP/err" >&2; exit 1
    fi
    note "waiting for the IAM role to propagate (${attempt}/10)"
    sleep 5
  done
  if [ "$CREATED" = 0 ]; then
    bad "could not create $SCHEDULE_NAME after 10 attempts. Last error:"
    sed 's/^/      /' "$TMP/err" >&2
    bad "If it says the execution role must allow Scheduler to assume it, check that"
    bad "${ROLE_NAME}'s trust policy has NO aws:SourceArn condition — see section 2."
    exit 1
  fi
else
  # Preserve the current ENABLED/DISABLED state: re-running this script must not silently
  # un-pause a schedule somebody paused to run the test suite.
  KEEP=$(schedule_state)
  aws scheduler update-schedule --name "$SCHEDULE_NAME" --group-name default \
    --schedule-expression "$SCHEDULE_RATE" \
    --flexible-time-window "$FTW" --state "$KEEP" --description "$DESC" \
    --target "file://$TMP/target.json" \
    --query ScheduleArn --output text >/dev/null
  note "updated: $SCHEDULE_RATE -> $WORKER_FN (state kept at $KEEP)"
fi
note "input: $(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["Input"])' "$TMP/target.json")"
note "retry: 1 attempt, give up after 60s — the next tick redoes the same idempotent work"

# ------------------------------------------------------- 3b. did the delivery actually work
# The one question State=ENABLED does not answer. Scheduler reports delivery outcomes in
# the AWS/Scheduler namespace and nowhere else — not in the Lambda log group, not on the
# schedule, not in the CLI's output — so a schedule can fire on time and drop every
# invocation while looking perfectly healthy. Read it here, on every run, because this
# already happened once (see section 2).
say "delivery outcomes (AWS/Scheduler, last 30 min)"
sched_metric() {
  aws cloudwatch get-metric-statistics --namespace AWS/Scheduler --metric-name "$1" \
    --dimensions Name=ScheduleGroup,Value=default \
    --start-time "$(date -u -v-30M '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u -d '30 min ago' '+%Y-%m-%dT%H:%M:%SZ')" \
    --end-time "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" --period 1800 --statistics Sum \
    --query 'sum(Datapoints[].Sum) || `0`' --output text
}
ATTEMPTS=$(sched_metric InvocationAttemptCount)
DROPPED=$(sched_metric InvocationDroppedCount)
note "attempts $ATTEMPTS / dropped $DROPPED / target errors $(sched_metric TargetErrorCount)"
if awk -v d="$DROPPED" 'BEGIN{exit !(d+0 > 0)}'; then
  bad "SCHEDULER IS DROPPING INVOCATIONS. The schedule fires and the worker never runs."
  bad "First thing to check is ${ROLE_NAME}'s trust policy — see section 2. Recent"
  bad "drops are expected only if you just fixed it; re-run in 10 minutes to confirm."
fi

# ========================================================================== 4. the alarms
# Every alarm here obeys three rules learned from alarms that got muted:
#
#   1. No alarm fires on a single datapoint. DatapointsToAlarm=2 of EvaluationPeriods=2
#      means the condition has to hold across two consecutive windows, so one cold start,
#      one 40001 retry storm or one judge hammering a button cannot page anybody.
#   2. TreatMissingData=notBreaching on every error-count alarm. These metrics have NO
#      datapoints when there is no traffic, and "nobody visited the demo for six hours" is
#      the normal state of an unattended demo, not an outage. The one exception is the
#      floor alarm, where missing data IS the outage — see below.
#   3. OKActions as well as AlarmActions, so the all-clear arrives in the same inbox. An
#      alert with no matching recovery is how a person learns to ignore the alert.
say "CloudWatch alarms -> $TOPIC_NAME"

alarm() {   # name, description, then the metric-specific flags
  local name="$1" desc="$2"; shift 2
  aws cloudwatch put-metric-alarm --alarm-name "$name" --alarm-description "$desc" \
    --alarm-actions "$TOPIC_ARN" --ok-actions "$TOPIC_ARN" "$@"
  note "$name"
}

# 4+ errors in each of two consecutive 5-minute windows. A cold start does not produce an
# Error (INIT is 1.4-2.3 s against a 30 s timeout), so this is not tuned for blips; it is
# tuned for the case where CockroachDB Cloud is unreachable and every request 500s.
alarm axiom-api-errors \
  'axiom-api is failing requests: >3 Lambda errors in each of two consecutive 5-min windows' \
  --namespace AWS/Lambda --metric-name Errors \
  --dimensions Name=FunctionName,Value="$API_FN" \
  --statistic Sum --period 300 --evaluation-periods 2 --datapoints-to-alarm 2 \
  --threshold 3 --comparison-operator GreaterThanThreshold \
  --treat-missing-data notBreaching

# This account's concurrent-execution limit is 10 for ALL functions together and neither
# function reserves any of it, so a sweep, an auto-heal drain and a judge loading the page
# genuinely can collide. A handful of throttles is that collision resolving itself; a
# sustained 6+ per 5 minutes twice running is the demo refusing to load.
alarm axiom-api-throttles \
  'axiom-api is being throttled: >5 throttles in each of two consecutive 5-min windows' \
  --namespace AWS/Lambda --metric-name Throttles \
  --dimensions Name=FunctionName,Value="$API_FN" \
  --statistic Sum --period 300 --evaluation-periods 2 --datapoints-to-alarm 2 \
  --threshold 5 --comparison-operator GreaterThanThreshold \
  --treat-missing-data notBreaching

# `5xx`, lowercase, no "Error" suffix — see THE METRIC NAME THAT LOOKS RIGHT AND IS NOT.
# Dimensioned on ApiId alone rather than ApiId+Stage: both series exist, there is exactly
# one stage, and the ApiId rollup keeps working if a stage is ever added.
alarm axiom-http-5xx \
  'the public demo URL is returning 5xx: >3 in each of two consecutive 5-min windows' \
  --namespace AWS/ApiGateway --metric-name 5xx \
  --dimensions Name=ApiId,Value="$API_ID" \
  --statistic Sum --period 300 --evaluation-periods 2 --datapoints-to-alarm 2 \
  --threshold 3 --comparison-operator GreaterThanThreshold \
  --treat-missing-data notBreaching

# Deliberately a storm detector. See WHY THE WORKER'S ERROR ALARM IS SET SO HIGH: a chaos
# demonstration produces up to 3 Errors per button press by design, so anything under
# "30 in each of two consecutive quarter-hours" would be an alarm on the feature working.
alarm axiom-worker-errors \
  'axiom-worker error STORM: >30 errors in each of two consecutive 15-min windows. Ordinary chaos-demo crashes are expected and are below this.' \
  --namespace AWS/Lambda --metric-name Errors \
  --dimensions Name=FunctionName,Value="$WORKER_FN" \
  --statistic Sum --period 900 --evaluation-periods 2 --datapoints-to-alarm 2 \
  --threshold 30 --comparison-operator GreaterThanThreshold \
  --treat-missing-data notBreaching

# The one that catches failure mode (a). The sweep should produce 12 invocations an hour;
# fewer than 6 for two consecutive hours means the schedule, the role or the function is
# broken, and NOTHING ELSE ON THIS PAGE CAN SEE THAT — a queue that has stopped draining
# emits no errors, no throttles and no 5xx. It just quietly stops being a demo.
#
# TreatMissingData=breaching here and only here: no datapoints means no invocations means
# the sweep is dead. That inverts rule 2 above, on purpose.
#
# Two evaluation periods rather than one, so a single quiet hour cannot alert. Note that
# this does NOT stop the alarm going ALARM the moment it is created — measured, not
# predicted: a new alarm is evaluated against the metric's EXISTING history, and the two
# hours before the schedule existed contain no sweeps, so both periods breach and the
# alarm is correct and useless. See the set-alarm-state call below.
alarm axiom-worker-silent \
  'the queue is not being swept: <6 axiom-worker invocations in each of two consecutive hours (12/hr expected). This is the alarm for "the demo silently stopped healing itself".' \
  --namespace AWS/Lambda --metric-name Invocations \
  --dimensions Name=FunctionName,Value="$WORKER_FN" \
  --statistic Sum --period 3600 --evaluation-periods 2 --datapoints-to-alarm 2 \
  --threshold 6 --comparison-operator LessThanThreshold \
  --treat-missing-data breaching

# Seed it OK on a FRESH INSTALL only, because otherwise the first thing this whole system
# ever does is email "the queue is not being swept" about the two hours before the sweep
# was installed. That statement is true and worthless, and an alerting channel whose first
# message is worthless is an alerting channel somebody mutes in week one.
#
# This hides nothing. set-alarm-state is a manual override that CloudWatch discards at its
# next evaluation, so if the schedule really is broken the alarm flips back within the
# hour and emails then. Detection is unchanged; only the retroactive complaint is dropped.
# On a re-run the alarm's state is left exactly as found — never reset a real alarm.
if [ "$SCHEDULE_WAS" = "ABSENT" ]; then
  aws cloudwatch set-alarm-state --alarm-name axiom-worker-silent --state-value OK \
    --state-reason 'fresh install: the sweep did not exist during the evaluated history'
  note "axiom-worker-silent seeded OK (fresh install; CloudWatch re-evaluates within the hour)"
fi

TOTAL_ALARMS=$(( $(aws cloudwatch describe-alarms --query 'length(MetricAlarms)' --output text) \
               + $(command aws cloudwatch describe-alarms --region "$BILLING_REGION" \
                     --query 'length(MetricAlarms)' --output text) ))
note "alarms in this account, all regions: $TOTAL_ALARMS of 10 always-free"

# ========================================================================== 5. the budget
# READ ONLY. billing_guard.sh owns this budget; duplicating it here would create a second
# one, and the third budget on an account bills $0.02/day. What this section exists to
# answer is one question: would anybody find out before real money was spent?
say "AWS Budget $BUDGET_NAME (verified, never modified)"
if ! command aws budgets describe-budget --account-id "$ACCOUNT" \
       --budget-name "$BUDGET_NAME" --region "$BILLING_REGION" \
       --output json > "$TMP/budget.json" 2>/dev/null; then
  bad "no budget named $BUDGET_NAME — run ./deploy/lambda/billing_guard.sh"
else
  python3 - "$TMP/budget.json" <<'PY'
import json, sys
b = json.load(open(sys.argv[1]))['Budget']
lim = b['BudgetLimit']
spend = b.get('CalculatedSpend', {}).get('ActualSpend', {})
print(f"    limit     {lim['Amount']} {lim['Unit']} / {b['TimeUnit'].lower()}")
print(f"    spent MTD {spend.get('Amount', '?')} {spend.get('Unit', '')}")
PY
  # A budget with only a 100% threshold tells you after the money is gone. The one that
  # matters is the lowest one: on a $1 budget, 1% is $0.01, which fires on the first cent
  # that is not free-tier — days before it is a number anyone would notice on a statement.
  LOWEST=$(command aws budgets describe-notifications-for-budget --account-id "$ACCOUNT" \
    --budget-name "$BUDGET_NAME" --region "$BILLING_REGION" \
    --query 'min(Notifications[?NotificationType==`ACTUAL`].Threshold)' --output text)
  note "lowest ACTUAL threshold: ${LOWEST}% of the limit"
  # $0.05 on a $1 budget is still "before real money". awk rather than [ ] because the
  # threshold comes back as a float and test(1) only compares integers.
  if awk -v l="$LOWEST" 'BEGIN{exit !(l+0 > 0 && l+0 <= 5)}' 2>/dev/null; then
    note "that is a near-zero tripwire — it fires before real money is spent"
  else
    warn "that is NOT near zero. Re-run billing_guard.sh to install a 1% threshold."
  fi
  SUBS=$(command aws budgets describe-subscribers-for-notification --account-id "$ACCOUNT" \
    --budget-name "$BUDGET_NAME" --region "$BILLING_REGION" \
    --notification "NotificationType=ACTUAL,ComparisonOperator=GREATER_THAN,Threshold=$LOWEST,ThresholdType=PERCENTAGE" \
    --query 'Subscribers[].Address' --output text 2>/dev/null || echo '')
  [ -n "$SUBS" ] && note "notifies: $SUBS (a Budgets email subscriber needs no opt-in click)" \
                 || bad  "the ${LOWEST}% threshold has NO subscribers — it alerts nobody"
fi

# The independent billing tripwire billing_guard.sh installed, checked rather than
# assumed. It is a different pipeline from Budgets and lands in a different place, which
# is the point of having both — but only if somebody is on the end of it.
BILL_SUBS=$(command aws sns list-subscriptions-by-topic --region "$BILLING_REGION" \
  --topic-arn "arn:aws:sns:${BILLING_REGION}:${ACCOUNT}:axiom-billing-alerts" \
  --query 'length(Subscriptions)' --output text 2>/dev/null || echo 0)
if [ "$BILL_SUBS" = "0" ]; then
  warn "axiom-billing-alerts (us-east-1) has ZERO subscriptions, so the"
  warn "axiom-estimated-charges alarm currently notifies nobody. The Budget above still"
  warn "does. That topic is billing_guard.sh's, not this file's — fix it with:"
  warn "  aws sns subscribe --region $BILLING_REGION \\"
  warn "    --topic-arn arn:aws:sns:${BILLING_REGION}:${ACCOUNT}:axiom-billing-alerts \\"
  warn "    --protocol email --notification-endpoint $ALERT_EMAIL"
else
  note "axiom-billing-alerts (us-east-1): $BILL_SUBS subscription(s)"
fi

# ======================================================================= 6. the dashboard
# Optional, and last, because items 1-5 are the ones that matter. Operator-facing: see
# A JUDGE CANNOT SEE THE DASHBOARD. p95 rather than Average on duration, because the mean
# of a serverless function is the warm path and hides exactly the cold-start and
# cross-region-query tail that makes a demo feel broken.
say "CloudWatch dashboard $DASHBOARD_NAME"
python3 - "$REGION" "$API_FN" "$WORKER_FN" "$API_ID" > "$TMP/dash.json" <<'PY'
import json, sys
region, api_fn, worker_fn, api_id = sys.argv[1:5]

def metric(title, metrics, stat='Sum', y=None, height=6, width=12, x=0, yy=0):
    p = {'type': 'metric', 'x': x, 'y': yy, 'width': width, 'height': height,
         'properties': {'title': title, 'region': region, 'view': 'timeSeries',
                        'stacked': False, 'stat': stat, 'period': 300,
                        'metrics': metrics}}
    if y:
        p['properties']['yAxis'] = y
    return p

L = 'AWS/Lambda'
G = 'AWS/ApiGateway'
widgets = [
    {'type': 'text', 'x': 0, 'y': 0, 'width': 24, 'height': 3, 'properties': {'markdown': (
        '# AXIOM — operational telemetry\n'
        f'`{api_fn}` serves the public demo through HTTP API `{api_id}`. '
        f'`{worker_fn}` drains the queue: every 5 minutes on a schedule '
        '(`axiom-worker-sweep`), plus whenever a judge presses RUN MISSION.\n\n'
        '**Worker errors are not all failures.** `{"mode":"chaos"}` makes the worker '
        '`os._exit(9)` inside crash window W4 on purpose — that is the demo working. '
        'The alarm that means something is `axiom-worker-silent`: invocations falling '
        'to a floor, i.e. the queue no longer being swept.'
    )}},
    metric('Invocations', [
        [L, 'Invocations', 'FunctionName', api_fn],
        ['...', worker_fn],
    ], yy=3, x=0),
    metric('Errors and throttles', [
        [L, 'Errors', 'FunctionName', api_fn],
        ['...', worker_fn],
        [L, 'Throttles', 'FunctionName', api_fn],
        ['...', worker_fn],
    ], yy=3, x=12),
    metric('Duration p95 (ms)', [
        [L, 'Duration', 'FunctionName', api_fn],
        ['...', worker_fn],
    ], stat='p95', yy=9, x=0),
    metric('HTTP API: requests, 4xx, 5xx', [
        [G, 'Count', 'ApiId', api_id],
        [G, '4xx', 'ApiId', api_id],
        [G, '5xx', 'ApiId', api_id],
    ], yy=9, x=12),
    metric('HTTP API latency p95 (ms)', [
        [G, 'Latency', 'ApiId', api_id],
        [G, 'IntegrationLatency', 'ApiId', api_id],
    ], stat='p95', yy=15, x=0, width=24),
]
print(json.dumps({'widgets': widgets}))
PY
aws cloudwatch put-dashboard --dashboard-name "$DASHBOARD_NAME" \
  --dashboard-body "file://$TMP/dash.json" --output text >/dev/null
note "https://${REGION}.console.aws.amazon.com/cloudwatch/home?region=${REGION}#dashboards:name=${DASHBOARD_NAME}"
note "console login required — this is for the operator, not for a judge."

# ============================================================================ 7. prove it
# Nothing above is described as working until it has been run. This invokes the schedule's
# EXACT payload synchronously and shows the worker's own log lines, so the thing proven is
# the payload and the function, not the wiring alone. The schedule's own delivery is
# proven separately, by the invocation count in the readout below rising on its own.
say "proving the sweep payload actually runs"
INPUT=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["Input"])' "$TMP/target.json")
START_MS=$(python3 -c 'import time; print(int(time.time()*1000) - 5000)')
aws lambda invoke --function-name "$WORKER_FN" --payload "$INPUT" \
  --cli-binary-format raw-in-base64-out "$TMP/out.json" \
  --query '{status:StatusCode,error:FunctionError}' --output text
note "response: $(cat "$TMP/out.json")"

note "worker log lines from this invocation:"
sleep 8    # CloudWatch Logs is a few seconds behind the invocation that wrote it
aws logs filter-log-events --log-group-name "/aws/lambda/${WORKER_FN}" \
  --start-time "$START_MS" --query 'events[].message' --output text 2>/dev/null \
  | sed 's/^/      /' | head -20 || warn "no log events yet — check again in a few seconds"

# ============================================================================== read-out
say "state"
note "schedule      $SCHEDULE_NAME: $(schedule_state), $SCHEDULE_RATE, flexible window 1 min"
note "              $SCHEDULE_ARN"
note "role          $ROLE_ARN"
note "topic         $TOPIC_ARN"
note "subscription  $ALERT_EMAIL: $(subscription_state)"
aws cloudwatch describe-alarms --alarm-names "${ALARM_NAMES[@]}" \
  --query 'MetricAlarms[].[AlarmName,StateValue]' --output text \
  | while read -r n s; do printf '    alarm         %-22s %s\n' "$n" "$s"; done
note "dashboard     $DASHBOARD_NAME"

say "what is NOT working yet"
if [ "$(subscription_state)" = "PENDING" ]; then
  warn "The email subscription is PENDING. $ALERT_EMAIL has an opt-in link from AWS."
  warn "Every alarm above is correctly configured and currently notifies NOBODY."
  warn "Click the link, then confirm with:"
  warn "  ./deploy/lambda/observability.sh --status"
else
  note "nothing — $ALERT_EMAIL is confirmed on $TOPIC_NAME."
fi

say "operating it"
note "pause the sweep      ./deploy/lambda/observability.sh --pause"
note "resume it            ./deploy/lambda/observability.sh --resume"
note "read the state       ./deploy/lambda/observability.sh --status"
note "prove an alarm       ./deploy/lambda/observability.sh --fire-test-alarm"
note "watch the sweeps     aws logs tail /aws/lambda/${WORKER_FN} --follow --region ${REGION}"
note "tear it all down     ./deploy/lambda/observability.sh --destroy"
note ""
note "Standing cost of THIS file: ~\$0.0086/month, all of it EventBridge Scheduler — 8,640"
note "scheduled invocations at \$1.00/million, priced rather than assumed because Scheduler"
note "is not one of this account's twelve Always Free entries. Everything else here IS free"
note "on this account: ~10,300 GB-s against an always-free 400,000, 7 MB of logs against"
note "5 GB, 5 alarms of 10, 1 dashboard of 3, and an SNS topic designed to send single-digit"
note "emails per month. Whole account, measured month-to-date: \$0.0001021066."
