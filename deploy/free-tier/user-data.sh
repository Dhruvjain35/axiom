#!/bin/bash
# AXIOM :: EC2 first-boot provisioning.
#
# Runs once, as root, when the instance first starts. Installs Docker, fetches the repo,
# writes the environment, and brings up the API plus a worker pool under Compose.
#
# Why one box and no load balancer
# --------------------------------
# An ALB is ~$16/month and has no free tier, which for this project is the entire hosting
# bill. A single instance with a public IP costs nothing on a free-tier-eligible type and
# is strictly BETTER for the demo: `docker kill axiom-worker-2` on camera is a real
# process death, which is exactly what the project is about. An ALB would add a health
# check, a target group and a DNS name, and buy nothing a judge can see.
#
# Everything is restart:unless-stopped, so a reboot brings the demo back without a human.
set -euxo pipefail

exec > >(tee /var/log/axiom-bootstrap.log) 2>&1

REPO="${REPO_URL}"
BRANCH="${REPO_BRANCH}"
APP_DIR=/opt/axiom

dnf -y update
dnf -y install docker git
systemctl enable --now docker

# Compose v2 as a docker plugin (the `docker compose` subcommand, not docker-compose).
mkdir -p /usr/libexec/docker/cli-plugins
ARCH="$(uname -m)"
curl -fsSL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-${ARCH}" \
  -o /usr/libexec/docker/cli-plugins/docker-compose
chmod +x /usr/libexec/docker/cli-plugins/docker-compose

git clone --branch "${BRANCH}" --depth 1 "${REPO}" "${APP_DIR}"
cd "${APP_DIR}"

# CockroachDB Cloud BASIC presents its own CA, and the system trust store does NOT
# verify it — `sslmode=verify-full` alone fails, and so does `sslrootcert=system`. The
# cert has to be on disk. Fetching it here rather than baking it into the image keeps
# the cluster id out of the repository.
mkdir -p /opt/axiom/certs
curl -fsSL "https://cockroachlabs.cloud/clusters/${CRDB_CLUSTER_ID}/cert" \
  -o /opt/axiom/certs/root.crt

cat > "${APP_DIR}/.env" <<ENVEOF
DATABASE_URL=${DATABASE_URL}
AWS_REGION=${AWS_REGION}
AXIOM_OFFLINE=${AXIOM_OFFLINE}
AXIOM_LEASE_SECONDS=20
AXIOM_PROVIDER_LATENCY_MS=120
ENVEOF
chmod 600 "${APP_DIR}/.env"

docker compose -f deploy/free-tier/docker-compose.ec2.yml --env-file "${APP_DIR}/.env" \
  up -d --build

# Wait for the API to answer before declaring success, so a failed boot is visible in
# the log rather than as a URL that silently never comes up.
for i in $(seq 1 60); do
  if curl -fsS http://localhost:8000/api/health >/dev/null; then
    echo "AXIOM API is up after ${i}s"
    exit 0
  fi
  sleep 1
done
echo "AXIOM API did not become healthy within 60s" >&2
docker compose -f deploy/free-tier/docker-compose.ec2.yml logs --tail 100 >&2
exit 1
