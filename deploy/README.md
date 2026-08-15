# deploy/ — getting AXIOM onto AWS

**What is actually deployed is `deploy/lambda/`, not this.** The ECS path below was written
first and never applied — no cluster, service or task definition exists — and Bedrock turned
out to be unusable on the deployment account (on-demand quota 0.0 req/min, not adjustable).
The live demo is two Lambda functions behind an HTTP API, with an EventBridge sweep and five
CloudWatch alarms keeping it alive through judging: see
[`lambda/README.md`](lambda/README.md). This directory remains the production-shaped story
and is priced out honestly in [`COST.md`](COST.md); every hour-billed line in it is a reason
it stayed unapplied.

Two paths, and they are not alternatives to each other.

| Path | What it proves | Time |
| --- | --- | --- |
| `docker compose up --build` from the repo root | the whole system works, reproducibly, on any machine with Docker and no cloud account at all | 18 s |
| `./scripts/provision_ccloud.sh` then `./scripts/deploy.sh` | the production shape: CockroachDB Cloud + ECS Fargate. **Never applied** — the demo URL that survives to Sep 15 is the Lambda one. | ~15 min |

Do the first one before the second. If compose does not come up green, the
deployment will not either, and finding that out locally costs nothing.

## Contents

```
deploy/
  COST.md              what a month of uptime actually costs, with the rates
                       read from the AWS Price List API, and what to do at $0
  terraform/           the module. This is the supported path.
  ecs/                 the same task definitions and IAM policies as raw JSON,
                       for reading, and for anyone who cannot install terraform
```

The two scripts that drive all of it live in `scripts/`, not here, because they
are things you run rather than things you deploy:

```
scripts/provision_ccloud.sh    ccloud: cluster, SQL user, schema, audit role
scripts/deploy.sh              ECR build+push, SSM secret, terraform, smoke test
```

Both take `AXIOM_DRY_RUN=1`, which prints every command they would run and
executes none of them. Read the plan before you hand either one an account.

## The full sequence

```bash
# 0. prove it locally first
docker compose up --build -d
curl -s localhost:8000/api/health
docker compose stop worker
docker compose --profile chaos run --rm chaos     # must end in DUPLICATE REFUNDS 0
docker compose down -v

# 1. the database. Prints the DSN to export at the end.
./scripts/provision_ccloud.sh

# 2. the 17 correctness gates, against the real Cloud cluster this time
export DATABASE_URL='...'          # from step 1
./.venv/bin/python scripts/preflight.py

# 3. AWS
cp deploy/terraform/terraform.tfvars.example deploy/terraform/terraform.tfvars
$EDITOR deploy/terraform/terraform.tfvars
./scripts/deploy.sh
```

`deploy.sh` ends by curling `/api/health` through the load balancer and refuses
to claim success until it gets an answer.

## Why ECS Fargate and not Lambda

The demo's claim is about what happens when a process dies **between** the
provider accepting a $300 refund and AXIOM recording that it did. Demonstrating
that requires killing the process at that moment, on camera:

```bash
aws ecs execute-command --cluster axiom --task <ARN> \
    --container worker --interactive --command /bin/sh
kill -9 1
```

You cannot SIGKILL a Lambda invocation. That is the entire architectural
argument, and `enable_execute_command = true` is what makes it possible — it
costs nothing and it is the most demo-critical line in the module.

## What is deliberately not here

- **No NAT gateway.** $33/month, more than everything else combined. Tasks run
  in public subnets with a security group that only admits the load balancer.
- **No Secrets Manager.** $0.40/secret/month for something SSM Parameter Store
  does for free at this scale. `DATABASE_URL` is a SecureString parameter,
  referenced by ARN so its value never enters terraform state.
- **No remote terraform backend.** One environment, one operator, four weeks.
  Add `backend "s3"` in `versions.tf` the moment a second person deploys.
- **No autoscaling.** Fixed at one API task and two workers. A demo that scales
  under judging load is a demo with a variable bill and no witness.
- **No `latest` tag.** ECR is created with immutable tags and `deploy.sh` pins
  the git sha, so the API and the workers cannot end up running different builds
  of `axiom/tasks.py` after a partial rollout.
