# AXIOM on Lambda — the $0.00 deployment

Two functions, one ZIP, one CockroachDB Cloud cluster. Nothing in this path bills at
rest, and nothing in it is a 12-month introductory offer.

```
./deploy/lambda/build.sh                      # -> build/axiom-lambda.zip (11.2 MB)
export AWS_PROFILE=axiom
export DATABASE_URL='postgresql://axiom_app:...@...cockroachlabs.cloud:26257/axiom?sslmode=verify-full&connect_timeout=5'
./deploy/lambda/deploy.sh                     # creates/updates both functions
./deploy/lambda/apigateway.sh                 # the public URL, and prints it
```

| File | What it is |
| --- | --- |
| `handler_api.py` | `axiom.api:app` behind Mangum. Also the four Lambda-specific adjustments, each argued in the docstring. |
| `handler_worker.py` | the worker entry point (owned by the worker build) |
| `requirements-lambda.txt` | what goes in the ZIP — smaller than `requirements.txt`, and the comments say why |
| `build.sh` | cross-platform wheel build, ELF verification, trim, precompile, zip |
| `deploy.sh` | IAM role, both functions, the Function URL, the public front door, smoke test |
| `apigateway.sh` | **the public front door that actually works on this account.** HTTP API, `$default` route, `$default` stage, throttle, invoke permission, smoke test. Idempotent; `--destroy` removes it. |
| `signed_curl.py` | curl the deployed URL with a SigV4 signature |

## The public URL

```
https://nq0i2ob395.execute-api.us-east-2.amazonaws.com/
```

```console
$ curl https://nq0i2ob395.execute-api.us-east-2.amazonaws.com/api/health
{"ok":true,"db":true,"provider":true,"version":"0.1.0","offline":true,"errors":{}}
```

Anonymous, no signature, no credentials in the environment. `/`, `/styles.css`,
`/api/mission`, `/api/crash-windows`, `/api/docs` and `POST /api/memories/recall` all
answer 200 through it.

### ⚠ The deployed ZIP is stale — rebuild before submitting

The gateway is current; **the code behind it is not.** The function is still running the
ZIP built 2026-08-11, and `axiom/api.py` has roughly doubled since:

| | `axiom/api.py` |
| --- | --- |
| inside `build/axiom-lambda.zip` | 40,945 bytes, 2026-08-11 |
| in the repo | 79,657 bytes, 2026-08-13 |

The visible symptom is the health payload. The current code returns `status`, `checks{}`,
`booted_at` and `uptime_seconds`; the deployed function returns only the old
`{"ok":true,"db":true,"provider":true,...}`. That is enough to fail this repo's own
usability check, which asserts `status == "ok"`:

```console
$ bash scripts/uptime_check.sh https://nq0i2ob395.execute-api.us-east-2.amazonaws.com
  health                             status=          <- fails
  database                           reachable
  provider                           reachable
  mission present                    Resolve today's order exceptions
  duplicate effects                  0
  vector index                       used (not a scan)
  FAILING — intervene before a judge sees it.
```

Everything below that line passes, so this is a stale build rather than a broken
deployment — but a judge comparing the AWS URL to the Vercel one would find them running
different versions of the app. The fix is the normal redeploy, and it does not touch the
gateway or change the URL:

```bash
./deploy/lambda/build.sh
DATABASE_URL='…' ./deploy/lambda/deploy.sh
bash scripts/uptime_check.sh https://nq0i2ob395.execute-api.us-east-2.amazonaws.com
```

Budget real time for it: the upload is ~11 MB and took 6 m 41 s on a home uplink.

## What it costs

| Thing | Price | Free allowance | Verdict |
| --- | --- | --- | --- |
| Lambda requests | $0.20 / million | **1M/month, always free** | ~1,000 requests per judging session. 0.1% of it. |
| Lambda compute (arm64) | $0.0000133 / GB-s | **400,000 GB-s/month, always free** | 0.0845 GB-s per warm request → ~4.7M requests inside the allowance |
| Function URL | $0.00 | — | free, always |
| CloudFront (fallback front door) | $0.085/GB | **1 TB + 10M requests/month, always free** | free at any demo volume |
| CloudWatch Logs | $0.50/GB ingest | 5 GB/month | 7-day retention, a few MB |
| API Gateway (HTTP API) | $1.00 / million | **1M requests/month, free for 12 months** | the public front door. See below for why it is here after all. Throttled to 20 req/s. |
| ECR / S3 / ALB / NAT | — | — | **not used.** The ZIP is 11.2 MB, under the 50 MB direct-upload limit, so there is nothing to put in a bucket. |

API Gateway's allowance is a 12-month offer rather than an always-free one, which is why
`deploy.sh` preferred a Function URL and says so in its header. That reasoning was right
about the cost and wrong about this account. The offer runs to Aug 2027, the demo has to
live until Sep 15 2026, and an HTTP API with no traffic bills $0.00 — so the standing
cost of the whole deployment is still zero.

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

## The account restriction, and the way around it

**This AWS account refuses anonymous access to Lambda Function URLs, and the refusal is
account-level.** That is still true, and it is still observable right now: the Function
URL is configured as publicly as AWS allows — auth type `NONE`, plus a resource policy
granting `Principal: "*"` — and it 403s an anonymous caller, while the *same function,
at the same moment,* answers 200 through the HTTP API. Two front doors, one Lambda:

```console
$ curl -o /dev/null -w '%{http_code}\n' https://a4ozyrv3noyq4ziekjzvdfdeqi0zjcgn.lambda-url.us-east-2.on.aws/api/health
403
$ curl -o /dev/null -w '%{http_code}\n' https://nq0i2ob395.execute-api.us-east-2.amazonaws.com/api/health
200
```

The restriction is specific to Function URLs, and API Gateway is not subject to it. Both
doors need a Lambda resource policy statement, but they are not the same grant: the
Function URL needs `lambda:InvokeFunctionUrl` for an anonymous principal, evaluated by
the Function URL front end, and that is what this account withholds. API Gateway needs
`lambda:InvokeFunction` for the named service principal `apigateway.amazonaws.com`,
evaluated by the Lambda control plane, and that is honored normally. `apigateway.sh`
is nothing more than that observation, written down and made re-runnable.

The rest of this section is the original diagnosis, kept because it is what ruled the
Function URL out and it is how the finding above was reached.

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

1. **`./deploy/lambda/apigateway.sh`.** This is the answer, it costs nothing, and it
   needs no one at AWS to do anything. It is what serves the public URL today.
2. **Optionally, an AWS Support case** (free on Basic support): Account and billing →
   "Lambda function URL public access is denied on account 034971967323 despite a correct
   resource-based policy; anonymous requests return 403 AccessDeniedException while SigV4
   requests succeed." If it is ever resolved, `FRONT=reprobe ./deploy/lambda/deploy.sh`
   re-tests and switches to the Function URL, removing one hop and one service. Nothing
   depends on this happening.
3. **The signed path still works**, and is the way to test the function itself with the
   gateway out of the picture:
   ```
   ./.venv/bin/python deploy/lambda/signed_curl.py /api/health
   ./.venv/bin/python deploy/lambda/signed_curl.py -X POST /api/memories/recall \
       -d '{"query":"refund policy for delayed orders","k":3}'
   ```
4. **`deploy/free-tier/`** (one EC2 instance, ~$10.40/month) uses no Lambda resource
   policy and is unaffected by any of this. It was the fallback for a public URL and is
   no longer needed for that; it remains written and unapplied.

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
* **Bedrock is not used and not permitted.** Model access **is** enabled on this account and
  both models answer a single call — what is missing is throughput: on-demand inference for
  Titan Text Embeddings V2 is 0.0 requests/minute and 0.0 tokens/minute (quota `L-26C560CE`,
  `Adjustable: false`, the same in `us-east-1`, `us-east-2` and `us-west-2`), and a sustained
  probe got 0 of 10 calls through in 87.4 s, all `ThrottlingException`. Single calls do land
  on a burst allowance, which is why one probe is not a capability check. So both functions
  run `AXIOM_OFFLINE=1` and the execution role is granted no `bedrock:*`.
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
