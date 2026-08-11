# What the Lambda deployment costs

**Target: $0.00.** Not "cheap" — zero. The operator has no credits (`aws freetier
get-account-plan-state` reports `accountPlanType: PAID` with $0.00 remaining, and the
Credits console says "You don't have any redeemable credits"), and the demo URL has to
stay up unattended from **Aug 19 to Sep 15 2026** — 28 days, 672 hours. A design that
costs a dollar is the wrong design.

That is only achievable because **Lambda's free tier is *always* free**, not a 12-month
introductory offer. So is CloudWatch's. Almost nothing else on AWS is, which is what
makes most of this document a list of things not to do.

Every rate below was read from the **AWS Price List API on 2026-08-11** against
`us-east-2` (Ohio), the region this deploys to. Free-tier *limits* are not in the pricing
API — those are cited from the published Free Tier page and marked as such. Reproduce a
rate:

```bash
aws pricing get-products --region us-east-1 --service-code AWSLambda \
  --filters Type=TERM_MATCH,Field=usagetype,Value=USE2-Lambda-GB-Second-ARM \
  --max-results 3 --output json | jq -r '.PriceList[]' | jq '.terms.OnDemand'
```

## The rates

| Line item | Rate | Where it came from |
| --- | --- | --- |
| Lambda requests | $0.0000002 / request ($0.20 per 1M) | Price List API, `USE2-Request` |
| Lambda duration, **arm64** | $0.0000133334 / GB-second | Price List API, `USE2-Lambda-GB-Second-ARM` |
| Lambda duration, x86 | $0.0000166667 / GB-second | Price List API, `USE2-Lambda-GB-Second` |
| Lambda Function URL | $0.00 | no charge; you pay only for the invoke |
| CloudWatch Logs ingestion | $0.50 / GB | Price List API, `USE2-DataProcessing-Bytes` |
| CloudWatch Logs storage | $0.03 / GB-month | Price List API, `USE2-TimedStorage-ByteHrs` |
| CloudFront HTTPS requests | $0.01 per 10,000 | Price List API, `US-Requests-Tier2-HTTPS` |
| AWS Budgets | first 2 free, then $0.02/day each | Budgets pricing page |
| Cost Explorer API | **$0.01 per request** | Cost Explorer pricing page |

| Always-free allowance | Amount | Source |
| --- | --- | --- |
| Lambda requests | 1,000,000 / month | Free Tier page — marked *Always free* |
| Lambda compute | 400,000 GB-seconds / month | Free Tier page — marked *Always free* |
| CloudWatch Logs ingestion | 5 GB / month | Free Tier page — *Always free* |
| CloudWatch Logs storage | 5 GB | Free Tier page — *Always free* |
| CloudWatch alarms | 10 alarms | Free Tier page — *Always free* |
| CloudFront | 1 TB out + 10,000,000 requests / month | Free Tier page — *Always free* |

The Lambda free tier is **not** architecture-dependent: 400,000 GB-seconds is 400,000
GB-seconds whether the function is arm64 or x86. Architecture only changes what the
*overage* costs, and arm64 is 20% cheaper, which is why `deploy.sh` pins `ARCHS=arm64`.

## What is actually deployed

Read back from the live account with `aws lambda list-functions --region us-east-2`:

| Function | Arch | Memory | Timeout |
| --- | --- | ---: | ---: |
| `axiom-api` | arm64 | 512 MB | 30 s |
| `axiom-worker` | arm64 | 512 MB | 300 s |

512 MB is not a performance choice. Every request is dominated by a cross-region round
trip to CockroachDB Cloud in `aws-us-east-1`, so more memory buys no measurable speed —
it only multiplies the GB-second burn rate. Doubling to 1024 MB would halve every
GB-second headroom number below.

Measured duration, from CloudWatch `AWS/Lambda` over the last 7 days. **Small sample —
34 and 1 invocations respectively** — so treat these as the right order of magnitude
rather than a forecast:

| Function | n | avg | min | max |
| --- | ---: | ---: | ---: | ---: |
| `axiom-api` | 34 | 171.6 ms | 2.5 ms | 1129.3 ms (cold start) |
| `axiom-worker` | 1 | 2479 ms | — | — |

## Which ceiling binds first

Two independent allowances, and it matters enormously which one you hit first, because
they are defended by completely different things.

At 512 MB the compute allowance converts to wall-clock like this:

```
400,000 GB-s ÷ 0.5 GB = 800,000 function-seconds/month = 222.2 hours
```

So the crossover — the average duration at which compute stops being the loose
constraint and starts being the tight one — is:

```
400,000 GB-s ÷ (0.5 GB × 1,000,000 requests) = 0.8 s per request
```

**Any average under 800 ms means the 1,000,000-request limit binds first.** At the
measured 171.6 ms average, a full 1M requests consumes

```
1,000,000 × 0.1716 s × 0.5 GB = 85,800 GB-s  →  21.4% of the 400,000 allowance
```

which leaves 4.7× headroom on compute. **The number to defend is therefore the request
count, and the only thing that generates request count at scale is the browser.**

The one exception is the worker, which is the mirror image: few requests, long duration.
222 hours of compute against a 672-hour judging window means **a permanently-running
worker is not affordable — it is 3× over the allowance on its own.** That is why the
worker is invoked on demand by `POST /api/demo/run-worker` and exits, and is not on a
schedule and not always on.

## Where this deployment could accidentally start costing money

This is the section that matters. Everything above is arithmetic; this is the list of
ways the arithmetic gets away from you.

### 1. A browser tab left open — by far the largest risk

A month is 2,592,000 seconds. **One request per second, from one tab, for one month, is
2,592,000 requests — 2.59× the entire always-free allowance, on its own.** Nobody has to
visit the demo for this to happen. One tab, forgotten, on one laptop.

Mission Control is worse than one request per second, because a poll cycle is not one
request. Each cycle issues:

- **3 core GETs** — `/api/mission`, `/api/tasks?limit=300`, `/api/events?limit=60`
- **5 auxiliary GETs** — `/api/health`, `/api/agents`, `/api/provider/stats`,
  `/api/receipts/unsettled`, `/api/approvals` — rate-limited to at most once per 4 s

At a **fixed 1-second poll**, which is what the pre-Lambda build did:

```
3 req/s + 5 req per 4 s          = 4.25 req/s
4.25 × 2,592,000                 = 11,016,000 requests/month
                                 = 11.0× the free allowance
```

Priced out, one forgotten tab costs:

```
requests : (11,016,000 − 1,000,000) × $0.0000002              = $2.00
compute  : 11,016,000 × 0.1716 s × 0.5 GB = 945,173 GB-s
           (945,173 − 400,000) × $0.0000133334                = $7.27
                                                        total ≈ $9.27 / month
```

Nine dollars is not a catastrophe in absolute terms. It is a total failure against a
$0.00 budget, and it also means the free tier is gone for everything else in the account
for the rest of the month, including the worker the demo actually needs.

**What `web/app.js` does about it.** The poll is a ladder — 1s → 2s → 5s → 15s → 30s →
60s — that steps out when nothing changes, snaps back to 1s when anything does, and
**schedules no timer at all while the tab is hidden** (Page Visibility API). Steady state
for an abandoned but *visible* tab is the 60 s rung:

```
8 requests per 60 s × 60 × 24 × 30 = 345,600 requests/month = 34.6% of the allowance
```

**Measured, not modelled.** Headless Chrome at 1280×720 against a local API, with every
request counted twice — once at the CDP layer (`Network.requestWillBeSent`) and once in
the server's own access log. The two agreed exactly on every sample below.

| Condition | /api requests in 60 s | Extrapolated /month |
| --- | ---: | ---: |
| Tab **visible**, board with a live `ACTION_PREPARED` task | **195** | 8,424,000 |
| Tab **hidden** | **0** | 0 |

And the ladder climbing down from a freshly seeded 30-`READY` board — the resting state
the hosted demo actually sits in between visitors:

| Minute | rung shown in header | requests |
| ---: | ---: | ---: |
| 1 | 15 s | 62 |
| 2 | 30 s | 16 |
| 3 | 60 s | 16 |
| 4 | 60 s | **8** |
| 5 | 60 s | **8** |
| 6 | 60 s | **8** |
| 7 | 60 s | **8** |

It reaches the bottom rung in **under 3 minutes** and then sits at **exactly 8 requests
per minute**, which is the number the 345,600/month figure above is built on. Minutes 8
and 9 of that same run jumped back to 99 and 157 requests — because another engineer
started a worker against the same cluster and the board genuinely changed. That is the
ladder doing its job, not failing: real work appeared, so the page paid for the fast rung
until it stopped.

**A hidden tab issues zero requests, with one bounded exception:** hiding the tab does
not abort a cycle already in flight. The server logged exactly 8 more requests — one full
cycle, 3 core + 5 aux — after the tab went to the background, and then nothing at all.
The cost of backgrounding a tab is therefore capped at one cycle, forever.

The 195 is a *transient*, and it is the honest worst case. While a task is holding a
lease the ladder tolerates 25 still cycles per rung instead of 3, because a worker
mid-refund goes quiet for a second or two and the next second is when the crash lands.
Riding the full ladder down from a live board takes 25×(1+2+5+15+30) = 1,325 s ≈ 22
minutes and costs roughly 840 requests, after which it settles at 8/min. Every path
through the backoff terminates — there is no board state that pins the fast rung open
forever, which is the property that keeps a forgotten tab inside the tier.

**The remaining exposure, stated plainly:** at 345,600 requests/month per permanently
open tab, this design affords **two** such tabs (691,200) and not **three**
(1,036,800 — over). That is the real limit, and it is why the poll interval and the
session request count are printed in the header where an operator can see them.

### 2. Putting the worker on a schedule

Do not. At 512 MB, one worker invocation per minute costs:

```
60 s runs : 43,200 runs × 30 GB-s   = 1,296,000 GB-s → overage $11.95/month
300 s runs: 43,200 runs × 150 GB-s  = 6,480,000 GB-s → overage $80.99/month
```

The worker is invoked on demand and exits. An EventBridge rule pointed at it is the
single easiest way to turn this deployment into a bill.

### 3. Async invoke retries

`POST /api/demo/run-worker` uses `InvocationType='Event'`. **Asynchronous Lambda
invocations retry twice on failure by default**, so a worker that reliably throws costs
3× its duration every time it is pressed, silently. A worker that throws at the 300 s
timeout is 450 GB-s per button press instead of 150. Set the function's
`MaximumRetryAttempts` to 0 if the worker ever starts failing.

### 4. CloudWatch Logs

5 GB/month of ingestion is always free. Real measured volume from the deployed
functions (`AWS/Logs` `IncomingBytes` over 7 days, divided by invocations):

| Function | bytes / invocation |
| --- | ---: |
| `axiom-api` | 691 B |
| `axiom-worker` | 1,070 B |

At the UI's steady-state 345,600 invocations/month:

```
345,600 × 691 B = 238.8 MB/month  →  4.7% of the 5 GB allowance
5 GB ÷ 691 B    = 7,240,000 invocations of headroom
```

So at our request volume **logs are not the binding constraint** — the 1M request limit
is hit roughly 7× before log ingestion is. Storage is bounded separately: `deploy.sh`
sets `LOG_RETENTION_DAYS=7` on every log group, so stored volume tops out near 56 MB
against the 5 GB free storage.

**Where a chatty worker breaks it.** The useful threshold is a sustained rate:

```
5 GB ÷ 2,592,000 s = 1,929 bytes/second, sustained, for a month
```

**Anything logging more than ~1.9 KB/s continuously exceeds the free tier**, and
overage is $0.50/GB. A worker that logs one line per claim attempt in a tight loop
against an empty queue — say 20 attempts/second at 120 bytes — is 2.4 KB/s and is
already over. The worker must log per *task*, not per *poll*. This is the one place
where a debugging `print()` left in a loop has a dollar cost.

### 5. CloudFront, if the public function URL is ever refused

`deploy.sh` puts a CloudFront distribution in front of the function URL only as a
fallback (`FRONT=cloudfront`), and none exists today — `aws cloudfront list-distributions`
returns `None`. CloudFront's free tier is **always free**: 1 TB out and 10,000,000
requests per month, plus the TLS certificate.

Response sizes, measured with `curl` against a 30-task board:

| Endpoint | bytes |
| --- | ---: |
| `/api/events?limit=60` | 26,801 |
| `/api/tasks?limit=300` | 16,891 |
| `/api/approvals` | 1,906 |
| the other five combined | 1,595 |
| **one full 8-request cycle** | **47,193 (46 KB)** |

At the steady-state one cycle per minute:

```
43,200 cycles/month × 47,193 B = 2.04 GB/month  →  0.2% of the 1 TB allowance
```

Data transfer is therefore never the binding constraint — CloudFront's 10M request
allowance is 10× Lambda's, so if the requests fit in Lambda's tier they fit in
CloudFront's by definition. (These are uncompressed figures; `curl` sent no
`Accept-Encoding`, and a real browser gets these JSON bodies gzipped at roughly 10:1,
so 2.04 GB is a conservative ceiling.)

`billing_guard.sh` reports distributions but deliberately does **not** flag them red — a
distribution inside the free tier costs $0, and a guard that cries wolf gets ignored.

### 6. Cost Explorer

`aws ce get-cost-and-usage` bills **$0.01 per request**. It is genuinely absurd that the
API for checking whether you are spending money costs money, but it does, and a script
that polls it hourly spends $7.30/month to tell you that you are spending money.
`billing_guard.sh` therefore reads month-to-date spend from the **Budgets** API, which is
free, and puts Cost Explorer behind an explicit `--cost-explorer` flag that announces the
charge.

### 7. Anything with an hour hand

The failure mode that actually empties accounts is not per-request overage — it is a
resource that bills whether or not anyone visits. Nothing in this deployment has one, and
`billing_guard.sh` asserts that by enumerating them and expecting zero.

## What is not free, and must never be added

| Resource | Cost | Why it is disqualified |
| --- | --- | --- |
| **Application Load Balancer** | $0.0225/h = **$16.43/mo** + LCUs | No free tier at all. Plus $0.005/h per public IPv4 *per enabled subnet* — $3.65/mo each. |
| **NAT Gateway** | $0.045/h = **$32.85/mo** + $0.045/GB processed | Never free. A Lambda in a VPC that needs egress needs one; so this deployment keeps the functions **out** of any VPC. |
| **ECR** | 500 MB storage | **12-month offer, not always-free.** This is the whole reason the deployment is ZIP-based rather than container-image. |
| **S3** | 5 GB standard | **12-month offer.** Assets ship inside the ZIP instead. |
| **API Gateway** | 1M calls (REST and HTTP) | **12-month offer.** Function URLs are free forever and do the same job here. |
| **Provisioned concurrency** | per GB-s of *idle* time | Bills for doing nothing and is **excluded from the free tier**. The single most expensive checkbox on the Lambda console. |
| **EC2** | `t4g.nano` $0.0042/h = $3.07/mo | The 750 h/month `t2/t3.micro` allowance is a **12-month offer**, and this account is past it. |
| **Elastic IP** | $0.005/h = $3.65/mo | Billed whether attached or not, since Feb 2024. |
| **RDS / Aurora** | — | The database is CockroachDB Cloud Basic, which is free and outside AWS billing entirely. |
| **AWS Budgets, 3rd onward** | $0.02/day = $0.61/mo | `billing_guard.sh` creates exactly one and refuses to create a third. |

`deploy/terraform/` (ECS + ALB) and `deploy/free-tier/` (EC2) remain in the repo as the
production-shaped story. **Neither is deployed.** Applying either one starts an hourly
meter; `deploy/COST.md` prices them out in full.

## The guardrails

`deploy/lambda/billing_guard.sh` installs two independent tripwires and a read-out:

1. **One AWS Budget**, $1/month, email at **1% / 50% / 100% of actual** spend. The 1%
   threshold is the important one: it fires on the first cent, days before there is a
   real number to react to. Credits and refunds are excluded from the calculation so a
   credit can never mask live usage.
2. **A CloudWatch alarm** on `AWS/Billing` `EstimatedCharges > $1` — a different
   pipeline with a different failure mode, landing in the same inbox. Within the 10
   always-free alarms.
3. **Month-to-date spend**, printed, read from the free Budgets API.

It then enumerates every hourly-billed resource type in the account and prints them in
red if any exist.

```bash
export AWS_PROFILE=axiom
./deploy/lambda/billing_guard.sh                      # defaults to the account email
./deploy/lambda/billing_guard.sh you@example.com      # or name one
```

**The alert address is `adamkoners@gmail.com`**, the root email on account
`034971967323`. Override it with the first positional argument or `BUDGET_EMAIL`.

**Two things the script cannot do for you, and both are load-bearing:**

- **Confirm the SNS email subscription.** AWS sends a confirmation link and the alarm
  cannot deliver until a human clicks it. Until then `aws sns
  list-subscriptions-by-topic` reports `PendingConfirmation` and the CloudWatch alarm is
  a signal into a void. The script prints this state on every run.
- **Enable "Receive billing alerts."** `AWS/Billing` publishes `EstimatedCharges` only
  when that preference is on, and it is a console-only setting with no API —
  Billing → Billing preferences → Alert preferences. Without it the alarm sits in
  `INSUFFICIENT_DATA` forever, which trains you to ignore it. The script detects the
  absence of any `AWS/Billing` datapoint and says so, but cannot fix it.

Run it before the deploy, after the deploy, and any time you are nervous. It is
idempotent.
