#!/usr/bin/env bash
# AXIOM :: deploy the demo to one EC2 instance, for as close to $0 as AWS allows.
#
#   ./deploy/free-tier/deploy_ec2.sh
#
# Creates: a security group, a key pair, and one instance running the API and three
# workers under Docker Compose against CockroachDB Cloud. No load balancer, no ECS, no
# NAT gateway — those are where the money goes and none of them is visible to a judge.
#
# Deliberately plain AWS CLI rather than Terraform. This provisions five things once; a
# Terraform module would add state to lose and would not make it more reproducible. The
# ECS/Fargate module in deploy/terraform/ still exists for the production-shaped story.
#
# Required environment:
#   AWS_PROFILE           an admin-ish profile for the target account
#   DATABASE_URL          CockroachDB Cloud URL (sslmode=verify-full, NO sslrootcert)
#   CRDB_CLUSTER_ID       cluster UUID, used to fetch the CA cert on the instance
# Optional:
#   AWS_REGION            default us-east-2
#   INSTANCE_TYPE         default t3.micro
#   REPO_URL / REPO_BRANCH
#   AXIOM_OFFLINE         "1" to run without Bedrock (default "0")
set -euo pipefail

REGION="${AWS_REGION:-us-east-2}"
INSTANCE_TYPE="${INSTANCE_TYPE:-t3.micro}"
REPO_URL="${REPO_URL:-https://github.com/Dhruvjain35/axiom.git}"
REPO_BRANCH="${REPO_BRANCH:-master}"
AXIOM_OFFLINE="${AXIOM_OFFLINE:-0}"
NAME="${NAME:-axiom-demo}"
KEY_NAME="${NAME}-key"
SG_NAME="${NAME}-sg"

: "${DATABASE_URL:?set DATABASE_URL to the CockroachDB Cloud connection string}"
: "${CRDB_CLUSTER_ID:?set CRDB_CLUSTER_ID to the cluster UUID}"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

say "account / region"
aws sts get-caller-identity --query 'Account' --output text
echo "region: ${REGION}   type: ${INSTANCE_TYPE}"

# --- free-tier reality check -------------------------------------------------------
# Stated out loud rather than assumed. AWS replaced the old always-12-months free tier
# for accounts created from mid-2025 onward; a new account may instead be on a credit
# plan. Either way this footprint is the cheapest shape available, but the operator
# deserves to know which regime they are in BEFORE an instance starts billing.
say "free tier status (informational — read it)"
aws freetier get-free-tier-usage --region us-east-1 \
  --query 'freeTierUsages[?contains(service,`Elastic Compute`)].[service,usageType,actualUsageAmount,limit,unit]' \
  --output table 2>/dev/null || echo "  (free tier API unavailable for this account; check the Billing console)"

# --- security group ----------------------------------------------------------------
say "security group"
VPC_ID=$(aws ec2 describe-vpcs --region "$REGION" \
  --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)

if ! SG_ID=$(aws ec2 describe-security-groups --region "$REGION" \
      --filters "Name=group-name,Values=${SG_NAME}" "Name=vpc-id,Values=${VPC_ID}" \
      --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null) || [ "$SG_ID" = "None" ]; then
  SG_ID=$(aws ec2 create-security-group --region "$REGION" --group-name "$SG_NAME" \
    --description "AXIOM demo: HTTP in, SSH from this machine only" --vpc-id "$VPC_ID" \
    --query 'GroupId' --output text)
  aws ec2 authorize-security-group-ingress --region "$REGION" --group-id "$SG_ID" \
    --protocol tcp --port 80 --cidr 0.0.0.0/0 >/dev/null
  # SSH is scoped to the deploying machine's current address rather than 0.0.0.0/0. A
  # public demo box with the world able to reach sshd is how a hackathon project becomes
  # somebody's crypto miner while the judges are still evaluating it.
  MYIP=$(curl -fsS https://checkip.amazonaws.com | tr -d '\n')
  aws ec2 authorize-security-group-ingress --region "$REGION" --group-id "$SG_ID" \
    --protocol tcp --port 22 --cidr "${MYIP}/32" >/dev/null
  echo "created ${SG_ID} (http from anywhere, ssh from ${MYIP}/32)"
else
  echo "reusing ${SG_ID}"
fi

# --- key pair ----------------------------------------------------------------------
say "key pair"
KEY_PATH="${HOME}/.ssh/${KEY_NAME}.pem"
if ! aws ec2 describe-key-pairs --region "$REGION" --key-names "$KEY_NAME" >/dev/null 2>&1; then
  mkdir -p "${HOME}/.ssh"
  aws ec2 create-key-pair --region "$REGION" --key-name "$KEY_NAME" \
    --query 'KeyMaterial' --output text > "$KEY_PATH"
  chmod 400 "$KEY_PATH"
  echo "created ${KEY_PATH}"
else
  echo "reusing ${KEY_NAME} (private key must already be at ${KEY_PATH})"
fi

# --- AMI ---------------------------------------------------------------------------
# Resolved from SSM rather than pinned: a hard-coded AMI id is region-specific and goes
# stale, and "the demo will not start" is a bad thing to discover during judging.
say "AMI"
ARCH_SUFFIX="x86_64"
case "$INSTANCE_TYPE" in t4g.*|c6g.*|m6g.*) ARCH_SUFFIX="arm64" ;; esac
AMI_ID=$(aws ssm get-parameters --region "$REGION" \
  --names "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-${ARCH_SUFFIX}" \
  --query 'Parameters[0].Value' --output text)
echo "${AMI_ID} (al2023, ${ARCH_SUFFIX})"

# --- user data ---------------------------------------------------------------------
USER_DATA=$(mktemp)
trap 'rm -f "$USER_DATA"' EXIT
{
  echo '#!/bin/bash'
  echo "export REPO_URL='${REPO_URL}'"
  echo "export REPO_BRANCH='${REPO_BRANCH}'"
  echo "export DATABASE_URL='${DATABASE_URL}'"
  echo "export CRDB_CLUSTER_ID='${CRDB_CLUSTER_ID}'"
  echo "export AWS_REGION='${REGION}'"
  echo "export AXIOM_OFFLINE='${AXIOM_OFFLINE}'"
  tail -n +2 "$(dirname "$0")/user-data.sh"
} > "$USER_DATA"

# --- instance ----------------------------------------------------------------------
say "launching ${INSTANCE_TYPE}"
INSTANCE_ID=$(aws ec2 run-instances --region "$REGION" \
  --image-id "$AMI_ID" --instance-type "$INSTANCE_TYPE" \
  --key-name "$KEY_NAME" --security-group-ids "$SG_ID" \
  --user-data "file://${USER_DATA}" \
  --metadata-options 'HttpTokens=required,HttpEndpoint=enabled' \
  --block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=20,VolumeType=gp3,DeleteOnTermination=true}' \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=${NAME}},{Key=Project,Value=axiom}]" \
  --query 'Instances[0].InstanceId' --output text)
echo "$INSTANCE_ID"

aws ec2 wait instance-running --region "$REGION" --instance-ids "$INSTANCE_ID"
IP=$(aws ec2 describe-instances --region "$REGION" --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)

say "waiting for the API (first boot builds the image; allow ~4 minutes)"
for i in $(seq 1 90); do
  if curl -fsS --max-time 4 "http://${IP}/api/health" >/dev/null 2>&1; then
    echo
    echo "  DEMO URL   http://${IP}/"
    echo "  health     http://${IP}/api/health"
    echo "  ssh        ssh -i ${KEY_PATH} ec2-user@${IP}"
    echo "  kill one   ssh -i ${KEY_PATH} ec2-user@${IP} 'docker kill axiom-worker-2'"
    echo "  bootstrap  ssh ... 'sudo cat /var/log/axiom-bootstrap.log'"
    echo
    echo "  seed it:   curl -XPOST http://${IP}/api/demo/seed -H 'content-type: application/json' -d '{\"tasks\":30,\"reset\":true}'"
    exit 0
  fi
  sleep 10
done

echo "API did not answer within 15 minutes." >&2
echo "  ssh -i ${KEY_PATH} ec2-user@${IP} 'sudo cat /var/log/axiom-bootstrap.log'" >&2
exit 1
