# What AXIOM costs to keep alive

The submission requires a functional demo URL from **Aug 19 to Sep 15 2026** —
28 days, 672 hours, unattended. That is an operational commitment with a bill
attached, and the bill is worth reading before the deploy rather than after.

Every rate below was read from the **AWS Price List API on 2026-08-10** against
`us-east-1`, not from memory. The two exceptions are marked. Reproduce any of
them:

```bash
aws pricing get-products --region us-east-1 --service-code AmazonECS \
  --filters Type=TERM_MATCH,Field=usagetype,Value=USE1-Fargate-ARM-vCPU-Hours:perCPU \
  --max-results 1 --output json | jq -r '.PriceList[0]' | jq '.terms.OnDemand'
```

## The rates

| Line item | Rate | Confirmed |
| --- | --- | --- |
| Application Load Balancer, per hour | $0.0225 | Price List API (`AWSELB`, `LoadBalancerUsage`, `LoadBalancing:Application`) |
| ALB capacity unit, per LCU-hour | $0.008 | Price List API (`LCUUsage`) |
| **Public IPv4 address in use, per hour** | **$0.005** | aws.amazon.com/vpc/pricing — *not present in the Price List API* |
| Fargate **ARM** vCPU-hour | $0.03238 | Price List API (`USE1-Fargate-ARM-vCPU-Hours:perCPU`) |
| Fargate **ARM** GB-hour | $0.00356 | Price List API (`USE1-Fargate-ARM-GB-Hours`) |
| Fargate x86 vCPU-hour | $0.04048 | Price List API |
| Fargate x86 GB-hour | $0.004445 | Price List API |
| CloudWatch Logs ingest, per GB | $0.50 | Price List API (`DataProcessing-Bytes`) |
| CloudWatch Logs storage, per GB-month | $0.03 | Price List API (`TimedStorage-ByteHrs`) |
| ECR storage, per GB-month | $0.10 | Price List API |
| SSM Parameter Store, standard tier | $0.00 | free below 10,000 parameters |
| Bedrock `amazon.titan-embed-text-v2:0`, per 1K input tokens | $0.00002 | Price List API (`USE1-TitanEmbeddingV2-Text-input-tokens`) |
| Bedrock `anthropic.claude-sonnet-4-5-...`, per 1M tokens | ~$3 in / ~$15 out | **not confirmed** — see below |
| EC2 `t4g.nano`, per hour | $0.0042 | Price List API |

**The Claude Sonnet 4.5 rate is the one number here that is not verified.** It is
absent from `aws pricing get-attribute-values --service-code AmazonBedrock`
(which stops at `Claude3Sonnet`) and from the rendered Bedrock pricing page,
whose newest Sonnet entry is 3.5 at $6/$30 per million. The arithmetic below is
shown at $3/$15 and the worst case at $6/$30 is double it — which, at this
workload, is the difference between seven cents and fourteen.

**The line item people miss is public IPv4.** Since Feb 2024 every in-use public
IPv4 address bills $0.005/hour — $3.65/month each — and an ALB takes one *per
enabled subnet* while every Fargate task with `assignPublicIp: ENABLED` takes
one of its own. Left on the default VPC's six availability zones, the load
balancer's addresses alone are $21.90/month. `deploy/terraform/network.tf`
therefore pins the ALB to two subnets, which is the minimum it accepts.

## The bill for the judging window

Baseline: the module's defaults — ARM64 Fargate, 0.25 vCPU / 0.5 GB per task,
one API task, two workers, two subnets, 7-day log retention, no NAT gateway, no
Container Insights, CockroachDB Basic.

| | Quantity | Per month (730 h) | Aug 19 – Sep 15 (672 h) |
| --- | --- | ---: | ---: |
| ALB hours | 1 × $0.0225/h | $16.43 | $15.12 |
| ALB LCUs | ~0.1 LCU avg | $0.58 | $0.54 |
| ALB public IPv4 | 2 × $0.005/h | $7.30 | $6.72 |
| Fargate API task | 0.25 vCPU + 0.5 GB ARM | $7.21 | $6.64 |
| Fargate workers | 2 × the same | $14.42 | $13.27 |
| Task public IPv4 | 3 × $0.005/h | $10.95 | $10.08 |
| CloudWatch Logs | see below | $0.01 | $0.01 |
| ECR storage | 0.085 GB image | $0.01 | $0.01 |
| SSM SecureString | 1 standard parameter | $0.00 | $0.00 |
| Bedrock | ~16 full demo runs | $1.04 | $1.04 |
| CockroachDB Basic | free monthly allowance | $0.00 | $0.00 |
| **Total** | | **$57.95** | **$53.43** |

**CloudWatch is effectively free here, and that is measured, not assumed.** A
worker with a drained queue emitted **0 bytes in 60 seconds** — it logs on state
transitions, not on polls. A complete 30-task chaos run produced **14.7 KB** from
one worker container. One gigabyte of ingest is roughly 70,000 demo runs.

**Bedrock is per-run, not per-hour.** One 30-task mission is about 320 input and
80 output tokens of triage per task (`_SYSTEM` in `axiom/llm.py` plus a one-line
order description, `max_tokens: 300`), so ~9,600 in / ~2,400 out per run:
**$0.065 per run** at $3/$15, $0.13 at $6/$30. Embeddings are noise — ~5,400
tokens per run is $0.0001. An idle deployment calls Bedrock zero times.

## The cheapest configuration that still demos well

Three changes take it to **$43.43 for the window** ($47.09/month):

1. **`worker_count = 1`** — saves $6.64 of compute and $3.36 of IPv4. You lose
   the ability to show one worker recovering *another* worker's orphaned lease
   live; the crash-and-recover story still works with a single worker because
   ECS restarts it, but the concurrent-recovery narrative gets harder to film.
   If the video is already shot, drop to one.
2. **Keep ARM64.** Graviton is 20% off the same vCPU. All of AXIOM's
   dependencies ship `manylinux` aarch64 wheels; there is no downside.
3. **Keep the two-subnet ALB and 7-day retention.** Both are already defaults;
   the point is not to "fix" them later.

What is **not** worth cutting:

- **The ALB.** It is $23.73/month with its two addresses and it is the only thing
  giving you a DNS name that survives a task restart — in a demo whose entire
  premise is that tasks restart. A bare Fargate public IP changes on every
  replacement, which breaks the submitted URL the first time the thing does what
  it is built to do.
- **`enable_execute_command`.** It is free and it is the demo: `aws ecs
  execute-command` into a running worker and `kill -9` it on camera. It is also
  the reason this is Fargate and not Lambda.
- **Fargate task size.** 0.25 vCPU / 0.5 GB is already the floor Fargate sells.

## If the budget is $0

The honest answer first: **there is no $0 AWS configuration that satisfies
"functional demo URL, continuously, through Sep 15."** Anything reachable over
IPv4 costs at least $3.65/month for the address, before compute. Do not plan
around a free tier that does not exist; plan around one of these:

1. **Credits.** Hackathon sponsors routinely issue AWS credits, and AWS Activate
   grants apply to a project like this. $53 of demo is one credit code. This is
   the intended answer and the only one that keeps the submission fully honest.

2. **Collapse onto one EC2 instance — ~$17.51/month.** `t4g.small` at
   $0.0168/h ($12.26/mo) + one public IPv4 ($3.65/mo) + 20 GB gp3
   ($1.60/mo), running the repo's own `docker compose up -d` with CockroachDB in
   a container and Caddy terminating TLS. One third the cost. What you give up:
   ECS Fargate and the ALB disappear from the architecture, and with them the
   `aws ecs execute-command` kill demo (you SSH and `docker kill -s KILL` instead
   — same effect, weaker story) and any claim of a managed, autoscaling
   deployment.

3. **IPv6-only, ~$13.86/month.** IPv6 addresses are free; the $0.005/hour is
   IPv4-only. An IPv6-only EC2 instance behind a free Cloudflare proxy (which is
   dual-stack, so IPv4 clients still reach it) removes the address charge
   entirely. Cheapest genuinely-reachable option. It fails closed if Cloudflare's
   free tier changes, and a judge on an IPv4-only corporate network hitting the
   origin directly sees nothing.

4. **What $0 actually buys, stated plainly.** Scale the services to zero, delete
   the ALB, and point the submission URL at a static page. You then have a
   README, a video, and a `docker compose up` that reproduces everything in 18
   seconds — which is a *reproducible* project but not a *deployed* one, and the
   rules ask for deployed. Choose this only if the alternative is not submitting.

## Guardrails

Set these before the deploy, not after the invoice:

```bash
# a budget with an alert at 80% — AWS Budgets is free for the first two budgets
aws budgets create-budget --account-id "$(aws sts get-caller-identity --query Account --output text)" \
  --budget '{"BudgetName":"axiom-demo","BudgetLimit":{"Amount":"75","Unit":"USD"},
             "TimeUnit":"MONTHLY","BudgetType":"COST"}'
```

- Log retention is set at creation (`log_retention_days = 7`). A log group
  created without it retains forever, and that is the most common way a finished
  demo keeps billing.
- ECR lifecycle expires untagged layers after a day; without it every rebuild
  leaves ~85 MB behind.

## Teardown, Sep 16

```bash
cd deploy/terraform && terraform destroy
```

Then confirm nothing survived — an orphaned load balancer is $23.73/month of
silence:

```bash
aws elbv2 describe-load-balancers --query 'LoadBalancers[].LoadBalancerName'
aws ec2 describe-addresses --query 'Addresses[].PublicIp'
aws ecs list-clusters
```

And delete the CockroachDB Cloud cluster (`ccloud cluster delete axiom`) — a
Basic cluster inside the free allowance bills nothing, but it holds data that
should not outlive the demo.
