# deploy/ecs — the same deployment without Terraform

`deploy/terraform/` is the supported path. These JSON documents are the raw
`aws ecs` inputs it produces, kept in the repo for two reasons: a reviewer can
read one file and see exactly what runs on Fargate without learning HCL, and
anyone who cannot install Terraform can still stand the demo up.

Every value that depends on your account appears as a literal placeholder:
`ACCOUNT_ID`, `REGION`, `IMAGE_TAG`, `SUBNET_A`, `SUBNET_B`, `SG_SERVICE`,
`TG_ID`. Substitute them before use — `sed` is enough:

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION=us-east-1
IMAGE_TAG=$(git rev-parse --short HEAD)

for f in taskdef-api.json taskdef-worker.json service-api.json service-worker.json \
         iam-task-role-policy.json iam-execution-role-policy.json iam-trust-policy.json; do
  sed -e "s/ACCOUNT_ID/$ACCOUNT_ID/g" \
      -e "s/REGION/$REGION/g" \
      -e "s/IMAGE_TAG/$IMAGE_TAG/g" "$f" > "/tmp/axiom-$f"
done
```

That loop only resolves the three placeholders that can be derived from your
shell. `SUBNET_A`, `SUBNET_B`, `SG_SERVICE` and `TG_ID` in `service-api.json`
and `service-worker.json` name the subnets, the task security group and the
target group — all of which this directory does NOT create (see the note on the
ALB below). Fill those four in by hand after you have created the networking,
or `aws ecs create-service` in step 4 fails on a literal `SUBNET_A`.

## Order of operations

```bash
# 1. roles
aws iam create-role --role-name axiom-ecs-execution \
    --assume-role-policy-document file:///tmp/axiom-iam-trust-policy.json
aws iam attach-role-policy --role-name axiom-ecs-execution \
    --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
aws iam put-role-policy --role-name axiom-ecs-execution --policy-name secrets \
    --policy-document file:///tmp/axiom-iam-execution-role-policy.json

aws iam create-role --role-name axiom-ecs-task \
    --assume-role-policy-document file:///tmp/axiom-iam-trust-policy.json
aws iam put-role-policy --role-name axiom-ecs-task --policy-name axiom \
    --policy-document file:///tmp/axiom-iam-task-role-policy.json

# 2. the secret (SecureString, standard tier — free)
aws ssm put-parameter --name /axiom/prod/DATABASE_URL --type SecureString \
    --value "$DATABASE_URL" --overwrite

# 3. log groups, with retention set at creation
aws logs create-log-group --log-group-name /axiom/api
aws logs create-log-group --log-group-name /axiom/worker
aws logs put-retention-policy --log-group-name /axiom/api    --retention-in-days 7
aws logs put-retention-policy --log-group-name /axiom/worker --retention-in-days 7

# 4. cluster, task definitions, services
aws ecs create-cluster --cluster-name axiom
aws ecs register-task-definition --cli-input-json file:///tmp/axiom-taskdef-api.json
aws ecs register-task-definition --cli-input-json file:///tmp/axiom-taskdef-worker.json
aws ecs create-service --cli-input-json file:///tmp/axiom-service-api.json
aws ecs create-service --cli-input-json file:///tmp/axiom-service-worker.json
```

The ALB, its target group, its listener and the two security groups are not
represented here — they are eight `aws elbv2` and `aws ec2` calls whose
arguments are all derived from each other, which is precisely the kind of thing
Terraform exists for. Use `deploy/terraform/` unless you have a reason not to.

## The one thing to check in these files

`healthCheck` is restated in both task definitions even though the Dockerfile
already declares one. That is not redundancy: **ECS ignores a Dockerfile's
`HEALTHCHECK` instruction entirely.** If you delete these blocks, the containers
run with no task-level health check at all and a wedged worker stays registered
forever.
