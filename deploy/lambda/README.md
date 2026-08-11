# AXIOM on Lambda — the $0.00 deployment

Two functions, one ZIP, one CockroachDB Cloud cluster. Nothing in this path bills at
rest, and nothing in it is a 12-month introductory offer.

```
./deploy/lambda/build.sh                      # -> build/axiom-lambda.zip (11.2 MB)
export AWS_PROFILE=axiom
export DATABASE_URL='postgresql://axiom_app:...@...cockroachlabs.cloud:26257/axiom?sslmode=verify-full&connect_timeout=5'
./deploy/lambda/deploy.sh                     # creates/updates everything, prints the URL
```

| File | What it is |
| --- | --- |
| `handler_api.py` | `axiom.api:app` behind Mangum. Also the four Lambda-specific adjustments, each argued in the docstring. |
| `handler_worker.py` | the worker entry point (owned by the worker build) |
| `requirements-lambda.txt` | what goes in the ZIP — smaller than `requirements.txt`, and the comments say why |
| `build.sh` | cross-platform wheel build, ELF verification, trim, precompile, zip |
| `deploy.sh` | IAM role, both functions, the Function URL, the public front door, smoke test |
| `signed_curl.py` | curl the deployed URL with a SigV4 signature |

## What it costs

| Thing | Price | Free allowance | Verdict |
| --- | --- | --- | --- |
| Lambda requests | $0.20 / million | **1M/month, always free** | ~1,000 requests per judging session. 0.1% of it. |
| Lambda compute (arm64) | $0.0000133 / GB-s | **400,000 GB-s/month, always free** | 0.0845 GB-s per warm request → ~4.7M requests inside the allowance |
| Function URL | $0.00 | — | free, always |
| CloudFront (fallback front door) | $0.085/GB | **1 TB + 10M requests/month, always free** | free at any demo volume |
| CloudWatch Logs | $0.50/GB ingest | 5 GB/month | 7-day retention, a few MB |
| ECR / S3 / API Gateway / ALB / NAT | — | — | **not used.** The ZIP is 11.2 MB, under the 50 MB direct-upload limit, so there is nothing to put in a bucket. |

The request count is the binding limit, not compute — by about 4.7x. See the sizing
table in `handler_api.py`, which is measured, not estimated.

## Measured on the real deployment

us-east-2, arm64, python3.13, 512 MB, CockroachDB Cloud in us-east-1:

```
cold start   INIT 1447-2258 ms, first request billed 1635-2344 ms, 109 MB of 512 MB
warm         /api/health 169 ms   (two cross-region queries)
             /api/crash-windows 2.7 ms  (no database)
after idle   311 ms — one extra round trip revalidating the pooled connections
peak memory  149 MB, on POST /api/demo/run-worker (it imports boto3 to invoke the worker)
```

The freeze/thaw handling was tested rather than reasoned about — invoke, wait, invoke:

| Gap since the previous request | What happened |
| --- | --- |
| < 15 s | no check, 169 ms, 200 |
| 17 s / 30 s / 73 s / 220 s | `thawed after Ns idle; revalidating pooled connections`, then 200 |
| 14 min | container reclaimed, `INIT_START`, 2144 ms cold start, 200 |

No request in any of those states returned a 500.

`/api/health`, `/api/mission`, `/api/crash-windows`, `POST /api/memories/recall`,
`/` (10,318 B of HTML) and `/styles.css` (37,189 B, `text/css`) all answer 200 from the
deployed function. The UI is served out of `/var/task/web` by the same `StaticFiles`
mount the container uses, which is why this deployment needs no bucket and no CDN
origin of its own.

## The one thing that is not working, and it is not the code

**This AWS account refuses anonymous access to Lambda Function URLs, and the refusal is
account-level.** Deployed, the API answers every request correctly to a signed caller and
403s an unsigned one.

The controlled experiment — one IAM role, one function, one unchanged resource-policy
statement granting it `lambda:InvokeFunctionUrl`:

| Setup | Result |
| --- | --- |
| role **with** an identity policy allowing `lambda:InvokeFunctionUrl` | **200** |
| same role, identity policy removed, resource policy unchanged | **403** |

So resource-based policy grants on a Function URL are not honored here. Both free public
paths are exactly that kind of grant, which is why both fail:

* **auth `NONE`** is a resource policy granting `Principal: "*"` — 403, tested in
  `us-east-2` and `us-east-1`, on this function and on a throwaway hello-world function,
  over a 15-minute window.
* **CloudFront + Origin Access Control** is a resource policy granting
  `cloudfront.amazonaws.com` — 403 from the origin, with the distribution `Deployed`,
  the OAC signing `always` with origin type `lambda`, and
  `AllViewerExceptHostHeader` forwarding.

Ruled out: it is not propagation (90 s, and the public statement stood for 15 minutes);
not policy syntax (`aws lambda add-permission` refuses to write a `*` statement without
the `lambda:FunctionUrlAuthType` condition, so the statement is the one AWS dictates, and
`iam simulate-principal-policy` returns `allowed`); not an SCP (this account is in no
organization); and not a setting anyone can read, because **no `PublicAccessBlock`
operation exists anywhere in the Lambda service model** — checked against botocore
1.43.69, the newest published.

### What to do about it

1. **Open an AWS Support case** (free on Basic support): Account and billing →
   "Lambda function URL public access is denied on account 034971967323 despite a correct
   resource-based policy; anonymous requests return 403 AccessDeniedException while SigV4
   requests succeed." When it is resolved, run `FRONT=reprobe ./deploy/lambda/deploy.sh`.
   It re-tests and switches to the public URL on its own, with no edit to any file.
2. **Meanwhile the deployment is real and testable over HTTP:**
   ```
   ./.venv/bin/python deploy/lambda/signed_curl.py /api/health
   ./.venv/bin/python deploy/lambda/signed_curl.py -X POST /api/memories/recall \
       -d '{"query":"refund policy for delayed orders","k":3}'
   ```
3. **`deploy/free-tier/`** (one EC2 instance, ~$10.40/month) uses no Lambda resource
   policy and is unaffected by any of this. It is the fallback that does not depend on
   AWS changing its mind before Aug 18.

`deploy.sh` prints all of this at the end of a run rather than reporting success and
leaving a judge to discover the 403.

## Operational notes

* **Re-running is safe and fast.** Every step is create-or-update. `deploy.sh` compares
  `base64(sha256(zip))` against the function's `CodeSha256` and skips the upload when
  they match, so a config-only re-run takes ~26 s instead of ~14 minutes of uploading.
  `build.sh` then `deploy.sh` is the whole redeploy; one ZIP, both functions.
* **The front-door verdict is remembered** in the tag `axiom:front` on `axiom-api`.
  Probing means flipping the URL's auth type to `NONE` and back, and that change takes a
  minute or two to settle — during which a live demo 403s perfectly good signed requests.
  A re-deploy must not do that to a working demo, so it only probes once. `FRONT=reprobe`
  re-tests deliberately: **that is the command to run after AWS lifts the restriction.**
* **The password never lands in a tracked file.** `deploy.sh` reads `DATABASE_URL` from
  the environment, writes it to a `mktemp` file under `umask 077` because the CLI needs
  `--environment file://`, and deletes it on exit.
* **`FRONT=url|cloudfront|iam`** skips the probe and forces one front door.
* **Logs:** `aws logs tail /aws/lambda/axiom-api --follow --region us-east-2`. Retention
  is 7 days on both functions so nothing accretes.
* **Concurrency:** this account's Lambda limit is 10, which is also the blast-radius cap
  on a public URL — at 512 MB the worst possible burn is 5 GB-s per wall-clock second.
  No reserved concurrency is set, because reserving any of 10 would drop unreserved
  concurrency below the minimum of 10 that AWS enforces.
* **Bedrock is not used and not permitted.** No model is enabled on this account, so both
  functions run `AXIOM_OFFLINE=1` and the execution role is granted no `bedrock:*`.
* **A CloudFront distribution exists** (`E16IJKGYV79WU6`, `d3rlxycj556sia.cloudfront.net`)
  from the fallback attempt. It costs $0 and `deploy.sh` reuses and re-probes it, so it
  starts working the moment the account restriction is lifted. To remove it instead:
  `aws cloudfront get-distribution-config`, set `Enabled: false`, `update-distribution`,
  wait for `Deployed`, then `delete-distribution` — CloudFront requires the disable step
  first, and it takes about 15 minutes.
* **Uploads are slow on a home uplink** — 11.2 MB took 6 m 41 s here, which is longer than
  the CLI's default 60-second read timeout. `deploy.sh` passes `--cli-read-timeout 0` on
  the two code uploads and nothing else; if a run dies with "Connection was closed before
  we received a valid response", that is the network, and re-running is safe.
