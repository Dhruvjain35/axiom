# Deploying AXIOM for ~$0

The demo URL has to answer from Aug 19 to Sep 15. This is the cheapest shape that does
that honestly, with the numbers stated rather than hand-waved.

## What the money actually is

| Thing | Cost | Verdict |
| --- | --- | --- |
| **Application Load Balancer** | **~$0.0225/hr ≈ $16.40/month**, no free tier, billed whether or not anyone visits | **Cut.** This was ~half the original estimate and buys nothing a judge can see. |
| **ECS Fargate** | ~$0.04/vCPU-hr + $0.004/GB-hr, **no free tier** | **Cut.** ~$9/month for 0.25 vCPU running continuously. |
| **NAT Gateway** | ~$32/month | **Never needed.** A public subnet with a public IP is free. |
| **EC2 `t4g.micro`** | **$0.00840/hr = $6.13/month** | **Used.** 1 GB, ARM. Runs the API and three workers. |
| **EC2 `t3.micro`** (x86 alternative) | $0.01040/hr = $7.59/month | Only if an ARM image is a problem. |
| **EC2 `t4g.nano`** | $0.00420/hr = $3.07/month | **Rejected: 0.5 GB.** The image build alone will not fit. |
| **EBS gp3, 8 GB** | $0.08/GB-month = $0.64/month | Used. 20 GB would be $1.60. |
| **Public IPv4 address** | ~$3.60/month since Feb 2024 — **charged even on free-tier instances** | Unavoidable for a public URL. |
| **Data transfer out** | 100 GB/month free | Free at demo volume. |
| **CockroachDB Cloud BASIC** | free tier | Free. |
| **Amazon Bedrock** | per token | Pennies. A 30-task mission is ~$0.0001; an idle deployment calls it zero times. |

**Measured total on this account: ~$10.40/month** — `t4g.micro` $6.13 + IPv4 ~$3.65 +
8 GB gp3 $0.64 — which is **~$14 from Aug 11 through the Sep 21 winners announcement.**

Those instance prices are not estimates: they were pulled from the AWS Pricing API for
`us-east-2` on 2026-08-11. Re-check them for another region before quoting them.

⚠️ **This account gets no *twelve-month* free tier.** `aws freetier get-account-plan-state`
reports `accountPlanType: PAID` with `accountPlanRemainingCredits: $0.00`. This paragraph
said `get-free-tier-usage` "returns zero rows", which was what it did on 2026-08-11; re-run
on 2026-08-14 it returns **twelve rows, every one `Always Free` and not one
`12 Months Free`**. The correction sharpens the warning rather than softening it: the
always-free allowances (Lambda, CloudWatch, SNS, SQS, KMS, Glue, SES) are real and
permanent here, and **every twelve-month offer — including the 750 EC2 hours this whole
directory would need — is unavailable.** So the ~$10.40/month above is the real number for
this path, not a number you might dodge. AWS advertises up to $200 in credits for new
accounts ($100 immediately), so if that is missing it is worth chasing in **Billing →
Credits** before launching anything — with credits the entire deployment, Bedrock
included, is free. There is no public API for promotional credits; the console is the
only authority.

> **Check which free tier you are on before launching.** AWS changed the deal in mid-2025:
> accounts created before then get the old 12-months-of-750-hours; newer accounts get a
> credit-based plan instead. `deploy_ec2.sh` prints your free-tier usage before it starts
> anything, and the Billing console is authoritative. Do not assume — a `t3.micro` outside
> the free tier is ~$7.50/month, which is survivable but is not the $0 you were promised.

## Why one instance beats ECS here — and not only on price

The demo's whole point is killing a worker on camera and watching another recover the task
without double-refunding. On this deployment that is:

```bash
ssh -i ~/.ssh/axiom-demo-key.pem ec2-user@<IP> 'docker kill axiom-worker-2'
```

A real `SIGKILL` of a real process, visible in Mission Control within a second as the
fence (`lease_epoch`) advances and another worker re-dispatches under the same idempotency
key. On Fargate you would stop a task and wait for the scheduler. The cheaper option is
also the more convincing one.

`deploy/terraform/` still holds the ECS + ALB module. It is the production-shaped answer
and it is worth showing a judge as the architecture you would run at scale; it is simply
not what should be burning money for a month of judging.

## Deploy

```bash
export AWS_PROFILE=axiom                 # a profile for the demo account
export AWS_REGION=us-east-2
export DATABASE_URL='postgresql://axiom_app:<PW>@axiom-memory-31580.j77.aws-us-east-1.cockroachlabs.cloud:26257/axiom?sslmode=verify-full'
export CRDB_CLUSTER_ID=b8325d1b-96ec-428f-b295-021f77f417a9

./deploy/free-tier/deploy_ec2.sh
```

It creates a security group (HTTP from anywhere, **SSH from your current IP only**), a key
pair, and one instance; then it waits for `/api/health` to answer and prints the URL. First
boot builds the image, so allow about four minutes.

Then seed a mission:

```bash
curl -XPOST http://<IP>/api/demo/seed -H 'content-type: application/json' \
  -d '{"tasks":30,"reset":true}'
```

## Note on regions

The instance runs in **us-east-2** while the CockroachDB cluster is in **aws-us-east-1**.
That cross-region hop adds roughly 10–20 ms per statement — irrelevant for a demo, and the
chaos run already tolerates real Cloud latency. Moving the instance to `us-east-1` would
remove it if you care. Cross-region data transfer at this volume is cents.

**Bedrock model access is per-account and per-region.** A brand-new account has no models
enabled: open Bedrock → Model access in the deployment region and enable
`amazon.titan-embed-text-v2:0` and a Claude model, or run with `AXIOM_OFFLINE=1`, which
uses the deterministic local stand-ins and needs no Bedrock at all. Offline mode is what
every measured number in the README was produced with, so the demo is fully functional
without it — but the submission is stronger with a real Bedrock call in the loop.

## Keeping it alive through Sep 15

- `restart: unless-stopped` on every container, so a reboot recovers with no human.
- Put an external uptime check on `http://<IP>/api/health` (any free monitor) that emails
  on failure. **Do this.** The demo URL going quiet in week three of judging is the exact
  failure that has bitten this operator before, and nothing in AWS will tell you.
- Do **not** stop/start the instance casually: a stop releases the public IP and the URL
  changes. Allocate an Elastic IP if you need the address to be stable (free while
  attached to a running instance, ~$3.60/month if left unattached).

## Tearing it down

```bash
aws ec2 terminate-instances --region us-east-2 --instance-ids <ID>
aws ec2 delete-security-group --region us-east-2 --group-id <SG>
aws ec2 delete-key-pair --region us-east-2 --key-name axiom-demo-key
```

Do this **after Sep 21** (winners announced), not before.
