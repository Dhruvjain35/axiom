#!/usr/bin/env bash
# AXIOM :: make it impossible for this deployment to spend money quietly.
#
#   ./deploy/lambda/billing_guard.sh [alert-email]
#
# The Lambda deployment is designed to sit inside the always-free tier (1M requests and
# 400,000 GB-seconds per month, which are NOT 12-month offers). Designed-to is not the
# same as verified-to, and the failure mode is the expensive one: nobody looks at an AWS
# account for three weeks, and the first signal is an invoice on Sep 15 during judging.
#
# So this installs two INDEPENDENT tripwires and one read-out:
#
#   1. An AWS Budget of $1/month, alerting at 1% ($0.01), 50% and 100% of ACTUAL spend.
#      1% is the important one — it fires on the first cent, i.e. on the first thing that
#      is not free, days before it is a real number. AWS Budgets bills $0.02/day per
#      budget after the first two, so this script creates EXACTLY ONE and refuses to be
#      the third.
#   2. A CloudWatch alarm on AWS/Billing EstimatedCharges > $1. Independent of Budgets:
#      different pipeline, different failure mode, and it lands in the same inbox. The
#      first 10 alarms are always-free.
#   3. Month-to-date spend printed to the terminal, read from the Budgets API
#      (free) rather than Cost Explorer (which bills $0.01 per request — the one AWS API
#      whose use would itself violate the constraint this script exists to enforce).
#
# Then it scans the account for the resources that actually cost money — an ALB, a NAT
# gateway, an EC2 instance, provisioned concurrency — and says plainly whether any exist.
#
# Idempotent. Run it before the deploy, after the deploy, and any time you are nervous.
#
# Environment:
#   AWS_PROFILE     default axiom
#   AWS_REGION      the region the demo runs in, default us-east-2
#   BUDGET_EMAIL    alert address; $1 overrides it. Defaults to the account email.
# Flags:
#   --cost-explorer  additionally query Cost Explorer for a per-service MTD breakdown.
#                    Off by default because it costs $0.01 per call. Yes, really.
set -euo pipefail

export AWS_PROFILE="${AWS_PROFILE:-axiom}"
REGION="${AWS_REGION:-us-east-2}"

# adamkoners@gmail.com is the root email on account 034971967323. Budgets and SNS both
# need somewhere to shout; if you change it, change it in both by re-running this script.
EMAIL="${BUDGET_EMAIL:-adamkoners@gmail.com}"
USE_CE=0
for arg in "$@"; do
  case "$arg" in
    --cost-explorer) USE_CE=1 ;;
    -*)              echo "unknown flag: $arg" >&2; exit 2 ;;
    *)               EMAIL="$arg" ;;
  esac
done

BUDGET_NAME="${BUDGET_NAME:-axiom-zero-spend}"
ALARM_NAME="${ALARM_NAME:-axiom-estimated-charges}"
TOPIC_NAME="${TOPIC_NAME:-axiom-billing-alerts}"
LIMIT_USD="${LIMIT_USD:-1}"

# Billing lives in us-east-1 whatever region you deploy to: the AWS/Billing namespace is
# published only there, and the Budgets API endpoint is global-behind-us-east-1. Getting
# this wrong produces an alarm stuck in INSUFFICIENT_DATA that looks like it works.
BILLING_REGION=us-east-1

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
say "account"
note "account   ${ACCOUNT}"
note "region    ${REGION} (billing signals always come from ${BILLING_REGION})"
note "alerts to ${EMAIL}"

# ── 1. the budget ──────────────────────────────────────────────────────────────────
# One budget, $1, monthly. Credits and refunds are EXCLUDED from the calculation on
# purpose: if a credit ever lands on this account it must not mask real usage, because
# the credit expires and the usage does not.
say "AWS Budget: \$${LIMIT_USD}/month, alert at 1% / 50% / 100% actual"

EXISTING=$(aws budgets describe-budgets --account-id "$ACCOUNT" --region "$BILLING_REGION" \
  --query 'length(Budgets)' --output text 2>/dev/null || echo 0)
[ "$EXISTING" = "None" ] && EXISTING=0
note "budgets that already exist: ${EXISTING} (first 2 are free, \$0.02/day each after)"

HAVE_OURS=0
aws budgets describe-budget --account-id "$ACCOUNT" --budget-name "$BUDGET_NAME" \
  --region "$BILLING_REGION" >/dev/null 2>&1 && HAVE_OURS=1

if [ "$HAVE_OURS" = 0 ] && [ "$EXISTING" -ge 2 ]; then
  echo "REFUSING: ${EXISTING} budgets already exist; a third would bill \$0.02/day." >&2
  echo "Delete one, or set BUDGET_NAME to reuse an existing budget." >&2
  exit 1
fi

BUDGET_JSON=$(cat <<JSON
{
  "BudgetName": "${BUDGET_NAME}",
  "BudgetLimit": { "Amount": "${LIMIT_USD}", "Unit": "USD" },
  "TimeUnit": "MONTHLY",
  "BudgetType": "COST",
  "CostTypes": {
    "IncludeCredit": false,
    "IncludeRefund": false,
    "IncludeDiscount": true,
    "IncludeOtherSubscription": true,
    "IncludeRecurring": true,
    "IncludeSubscription": true,
    "IncludeSupport": true,
    "IncludeTax": true,
    "IncludeUpfront": true,
    "UseAmortized": false,
    "UseBlended": false
  }
}
JSON
)

# Thresholds are percentages of the $1 limit, so 1 = one cent. GREATER_THAN rather than
# GREATER_THAN_OR_EQUAL_TO because AWS only evaluates these once a day anyway and the
# difference is a rounding error against "did anything at all start billing".
notif() {  # $1 = threshold percent
  printf '{"NotificationType":"ACTUAL","ComparisonOperator":"GREATER_THAN","Threshold":%s,"ThresholdType":"PERCENTAGE"}' "$1"
}
subs() {   # $1 = threshold percent — the create-budget shape, notification + subscribers
  printf '{"Notification":%s,"Subscribers":[{"SubscriptionType":"EMAIL","Address":"%s"}]}' \
    "$(notif "$1")" "$EMAIL"
}

if [ "$HAVE_OURS" = 1 ]; then
  note "budget ${BUDGET_NAME} exists — updating limit and re-asserting notifications"
  aws budgets update-budget --account-id "$ACCOUNT" --region "$BILLING_REGION" \
    --new-budget "$BUDGET_JSON" >/dev/null
  for T in 1 50 100; do
    # DuplicateRecordException is the success case on a re-run; it means the notification
    # is already installed. Anything else is a real failure and should be loud.
    if OUT=$(aws budgets create-notification --account-id "$ACCOUNT" --region "$BILLING_REGION" \
          --budget-name "$BUDGET_NAME" --notification "$(notif "$T")" \
          --subscribers "[{\"SubscriptionType\":\"EMAIL\",\"Address\":\"${EMAIL}\"}]" 2>&1); then
      note "notification @${T}% created"
    elif grep -q DuplicateRecordException <<<"$OUT"; then
      note "notification @${T}% already present"
    else
      echo "$OUT" >&2; exit 1
    fi
  done
else
  NOTIFS="[ $(subs 1), $(subs 50), $(subs 100) ]"
  aws budgets create-budget --account-id "$ACCOUNT" --region "$BILLING_REGION" \
    --budget "$BUDGET_JSON" --notifications-with-subscribers "$NOTIFS" >/dev/null
  note "budget ${BUDGET_NAME} created with 3 notifications"
fi

# ── 2. the CloudWatch billing alarm ────────────────────────────────────────────────
say "CloudWatch billing alarm (second, independent signal)"

TOPIC_ARN=$(aws sns create-topic --name "$TOPIC_NAME" --region "$BILLING_REGION" \
  --query TopicArn --output text)                 # create-topic is idempotent
note "topic ${TOPIC_ARN}"

# The default topic policy already permits this account, but CloudWatch publishes as the
# service principal and an explicit grant is one less thing to debug at 2am when the
# alarm fires into a void. SourceOwner pins it to this account so the topic cannot be
# used as a relay by anyone else's alarms.
POLICY=$(cat <<JSON
{ "Version": "2012-10-17", "Statement": [
  { "Sid": "AllowCloudWatchAlarms", "Effect": "Allow",
    "Principal": { "Service": "cloudwatch.amazonaws.com" },
    "Action": "SNS:Publish", "Resource": "${TOPIC_ARN}",
    "Condition": { "StringEquals": { "AWS:SourceOwner": "${ACCOUNT}" } } },
  { "Sid": "AllowBudgets", "Effect": "Allow",
    "Principal": { "Service": "budgets.amazonaws.com" },
    "Action": "SNS:Publish", "Resource": "${TOPIC_ARN}",
    "Condition": { "StringEquals": { "AWS:SourceOwner": "${ACCOUNT}" } } },
  { "Sid": "Owner", "Effect": "Allow", "Principal": { "AWS": "*" },
    "Action": ["SNS:Subscribe","SNS:SetTopicAttributes","SNS:GetTopicAttributes",
               "SNS:DeleteTopic","SNS:Publish","SNS:ListSubscriptionsByTopic"],
    "Resource": "${TOPIC_ARN}",
    "Condition": { "StringEquals": { "AWS:SourceOwner": "${ACCOUNT}" } } } ] }
JSON
)
aws sns set-topic-attributes --region "$BILLING_REGION" --topic-arn "$TOPIC_ARN" \
  --attribute-name Policy --attribute-value "$POLICY" >/dev/null

# Subscribe only if this address is not already on the topic. Re-subscribing an address
# that is Pending sends ANOTHER confirmation email, and an operator who has to sort three
# identical confirmation emails confirms none of them.
SUB=$(aws sns list-subscriptions-by-topic --region "$BILLING_REGION" --topic-arn "$TOPIC_ARN" \
  --query "Subscriptions[?Endpoint=='${EMAIL}'].SubscriptionArn" --output text)
if [ -z "$SUB" ] || [ "$SUB" = "None" ]; then
  aws sns subscribe --region "$BILLING_REGION" --topic-arn "$TOPIC_ARN" \
    --protocol email --notification-endpoint "$EMAIL" >/dev/null
  note "subscribed ${EMAIL} — CONFIRM THE EMAIL or this signal is dead"
elif [ "$SUB" = "PendingConfirmation" ]; then
  note "subscription still PendingConfirmation — click the link in ${EMAIL}"
else
  note "subscription confirmed: ${SUB}"
fi

# treat-missing-data notBreaching: AWS/Billing publishes roughly every 6 hours and only
# once the account has charges, so a genuinely-free account has NO datapoints. Without
# this the alarm sits in INSUFFICIENT_DATA forever, which trains you to ignore it.
aws cloudwatch put-metric-alarm --region "$BILLING_REGION" \
  --alarm-name "$ALARM_NAME" \
  --alarm-description "AXIOM: estimated charges exceeded \$${LIMIT_USD}. The Lambda demo is supposed to cost \$0." \
  --namespace AWS/Billing --metric-name EstimatedCharges \
  --dimensions Name=Currency,Value=USD \
  --statistic Maximum --period 21600 --evaluation-periods 1 \
  --threshold "$LIMIT_USD" --comparison-operator GreaterThanThreshold \
  --treat-missing-data notBreaching \
  --alarm-actions "$TOPIC_ARN" --ok-actions "$TOPIC_ARN"
note "alarm ${ALARM_NAME}: AWS/Billing EstimatedCharges > \$${LIMIT_USD} (Maximum/6h)"

STATE=$(aws cloudwatch describe-alarms --region "$BILLING_REGION" --alarm-names "$ALARM_NAME" \
  --query 'MetricAlarms[0].StateValue' --output text)
note "alarm state: ${STATE}"

HAVE_METRIC=$(aws cloudwatch list-metrics --region "$BILLING_REGION" --namespace AWS/Billing \
  --metric-name EstimatedCharges --query 'length(Metrics)' --output text 2>/dev/null || echo 0)
if [ "$HAVE_METRIC" = "0" ]; then
  note "NOTE: AWS/Billing has published no datapoints yet. That is expected on an account"
  note "      with no charges, but it ALSO happens when 'Receive billing alerts' is off."
  note "      Turn it on once: Billing console -> Billing preferences -> Alert preferences."
  note "      Console-only setting; there is no API for it, so this script cannot do it."
fi

# ── 3. what has actually been spent ────────────────────────────────────────────────
say "month-to-date spend"
aws budgets describe-budget --account-id "$ACCOUNT" --region "$BILLING_REGION" \
  --budget-name "$BUDGET_NAME" \
  --query 'Budget.{limit:BudgetLimit.Amount,actual:CalculatedSpend.ActualSpend.Amount,forecast:CalculatedSpend.ForecastedSpend.Amount}' \
  --output table
note "(a budget created minutes ago reports null until AWS's first daily refresh)"

CHARGES=$(aws cloudwatch get-metric-statistics --region "$BILLING_REGION" \
  --namespace AWS/Billing --metric-name EstimatedCharges --dimensions Name=Currency,Value=USD \
  --start-time "$(date -u -v-2d '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u -d '2 days ago' '+%Y-%m-%dT%H:%M:%SZ')" \
  --end-time "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
  --period 21600 --statistics Maximum \
  --query 'sort_by(Datapoints,&Timestamp)[-1].Maximum' --output text 2>/dev/null || echo None)
note "EstimatedCharges (CloudWatch, last 48h): ${CHARGES}"

aws freetier get-free-tier-usage --region "$BILLING_REGION" \
  --query 'freeTierUsages[].[service,usageType,actualUsageAmount,forecastedUsageAmount,limit,unit]' \
  --output table 2>/dev/null || note "(free-tier usage API returned nothing — no usage recorded yet)"

if [ "$USE_CE" = 1 ]; then
  say "Cost Explorer breakdown (this call cost \$0.01)"
  aws ce get-cost-and-usage --region "$BILLING_REGION" \
    --time-period "Start=$(date -u '+%Y-%m-01'),End=$(date -u '+%Y-%m-%d')" \
    --granularity MONTHLY --metrics UnblendedCost --group-by Type=DIMENSION,Key=SERVICE \
    --query 'ResultsByTime[0].Groups[?Metrics.UnblendedCost.Amount!=`0`].[Keys[0],Metrics.UnblendedCost.Amount]' \
    --output table
fi

# ── 4. does anything expensive exist right now ─────────────────────────────────────
# Every resource listed here is one that bills by the hour whether or not a judge ever
# loads the page. The Lambda deployment creates NONE of them; this is the assertion.
say "resources that would cost money (expect all zero)"
COST_FOUND=0
check() {  # $1 = label, $2..= command
  local label="$1"; shift
  local out
  out=$("$@" 2>/dev/null || true)
  out=$(tr -d '[:space:]' <<<"$out")
  if [ -z "$out" ] || [ "$out" = "None" ] || [ "$out" = "0" ] || [ "$out" = "[]" ]; then
    printf '    %-34s none\n' "$label"
  else
    printf '    \033[31m%-34s %s\033[0m\n' "$label" "$out"
    COST_FOUND=1
  fi
}

for R in "$REGION" "$BILLING_REGION"; do
  check "EC2 instances (${R})" aws ec2 describe-instances --region "$R" \
    --filters Name=instance-state-name,Values=running,pending,stopping,stopped \
    --query 'length(Reservations[].Instances[])' --output text
  check "load balancers (${R})" aws elbv2 describe-load-balancers --region "$R" \
    --query 'length(LoadBalancers)' --output text
  check "NAT gateways (${R})" aws ec2 describe-nat-gateways --region "$R" \
    --filter Name=state,Values=available,pending --query 'length(NatGateways)' --output text
  check "elastic IPs (${R})" aws ec2 describe-addresses --region "$R" \
    --query 'length(Addresses)' --output text
  check "ECR repositories (${R})" aws ecr describe-repositories --region "$R" \
    --query 'length(repositories)' --output text
  check "RDS instances (${R})" aws rds describe-db-instances --region "$R" \
    --query 'length(DBInstances)' --output text
  check "API Gateway v2 APIs (${R})" aws apigatewayv2 get-apis --region "$R" \
    --query 'length(Items)' --output text
done
check "S3 buckets (global)" aws s3api list-buckets --query 'length(Buckets)' --output text

# CloudFront is deliberately NOT a red line. Its free tier is always-free (1 TB out and
# 10,000,000 requests a month), deploy.sh only creates a distribution when the public
# function URL is refused, and a distribution inside those limits bills $0 — so flagging
# it as "money" would be a false alarm, and a guard that cries wolf gets ignored. It is
# reported because it is the one resource here whose cost is invisible until traffic
# arrives: nothing accrues per hour, and then a scraper finds it.
CF=$(aws cloudfront list-distributions --query 'DistributionList.Quantity' --output text 2>/dev/null || echo 0)
[ "$CF" = "None" ] && CF=0
if [ "$CF" = "0" ]; then
  printf '    %-34s none\n' "CloudFront distributions"
else
  printf '    %-34s %s (free to 1TB out / 10M req per month)\n' "CloudFront distributions" "$CF"
fi

# Provisioned concurrency is the one Lambda setting that bills for idle time — it is
# charged per GB-second whether or not anything invokes the function, and it is NOT in
# the free tier. If this ever prints a number, delete it.
PC=0
for FN in $(aws lambda list-functions --region "$REGION" --query 'Functions[].FunctionName' --output text 2>/dev/null); do
  N=$(aws lambda list-provisioned-concurrency-configs --region "$REGION" --function-name "$FN" \
      --query 'length(ProvisionedConcurrencyConfigs)' --output text 2>/dev/null || echo 0)
  [ "$N" != "0" ] && [ "$N" != "None" ] && { printf '    \033[31m%-34s %s\033[0m\n' "provisioned concurrency ($FN)" "$N"; PC=1; }
done
[ "$PC" = 0 ] && printf '    %-34s none\n' "provisioned concurrency"
[ "$PC" = 1 ] && COST_FOUND=1

say "verdict"
if [ "$COST_FOUND" = 0 ]; then
  note "no hourly-billed resource exists in ${REGION} or ${BILLING_REGION}."
  note "the only way this account starts charging is by exceeding an always-free limit —"
  note "which is what the \$1 budget and the billing alarm are there to catch."
else
  note "SOMETHING IN THIS ACCOUNT BILLS BY THE HOUR. Read the red lines above."
fi
note ""
note "teardown: aws budgets delete-budget --account-id ${ACCOUNT} --budget-name ${BUDGET_NAME}"
note "          aws cloudwatch delete-alarms --region ${BILLING_REGION} --alarm-names ${ALARM_NAME}"
note "          aws sns delete-topic --region ${BILLING_REGION} --topic-arn ${TOPIC_ARN}"
