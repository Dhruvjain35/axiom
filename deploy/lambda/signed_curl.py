#!/usr/bin/env python3
"""AXIOM :: curl the deployed Function URL with a SigV4 signature.

    export AWS_PROFILE=axiom
    ./.venv/bin/python deploy/lambda/signed_curl.py /api/health
    ./.venv/bin/python deploy/lambda/signed_curl.py -X POST /api/demo/seed -d '{"tasks":8}'

Why this exists
---------------
A Function URL with `--auth-type AWS_IAM` is a normal HTTPS endpoint that requires a
SigV4 signature — which `curl` cannot produce and `aws lambda invoke` does not exercise
(invoke goes to the Lambda control plane and never touches the URL's HTTP path). This is
the only way to test the deployment as a browser would see it, minus the browser.

It matters more than it should on this account, because anonymous access to Function
URLs is refused here regardless of the resource policy (see deploy/lambda/README.md).
Until that is lifted, this is how the deployed API gets exercised over real HTTP.

No dependencies beyond botocore, which arrives with boto3 and therefore with the repo's
requirements.txt. It resolves the URL from the function itself, so there is nothing to
copy-paste and nothing to keep in sync.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

import botocore.session
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('path', help='request path, e.g. /api/health')
    ap.add_argument('-X', '--method', default='GET')
    ap.add_argument('-d', '--data', default=None, help='request body (implies POST)')
    ap.add_argument('--function', default='axiom-api')
    ap.add_argument('--region', default=None, help='defaults to the session region')
    ap.add_argument('--head', action='store_true', help='print status and headers only')
    args = ap.parse_args()

    session = botocore.session.get_session()
    region = args.region or session.get_config_variable('region') or 'us-east-2'
    lam = session.create_client('lambda', region_name=region)
    base = lam.get_function_url_config(FunctionName=args.function)['FunctionUrl'].rstrip('/')

    method = 'POST' if (args.data and args.method == 'GET') else args.method
    body = args.data.encode() if args.data else None
    url = base + (args.path if args.path.startswith('/') else '/' + args.path)

    # The body is signed, not just the headers: SigV4 hashes the payload into the
    # signature, so a POST whose body is added after signing fails with a mismatch
    # rather than with a 403 — a distinction worth keeping visible while debugging.
    req = AWSRequest(method=method, url=url, data=body,
                     headers={'content-type': 'application/json'} if body else {})
    creds = session.get_credentials().get_frozen_credentials()
    SigV4Auth(creds, 'lambda', region).add_auth(req)

    try:
        resp = urllib.request.urlopen(
            urllib.request.Request(url, data=body, headers=dict(req.headers), method=method),
            timeout=60)
        status, payload, headers = resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as e:
        status, payload, headers = e.code, e.read(), e.headers

    print(f'{status} {method} {url}  ({len(payload)} bytes, {headers.get("content-type")})')
    if args.head:
        for k, v in headers.items():
            print(f'  {k}: {v}')
        return 0 if status < 400 else 1

    text = payload.decode('utf-8', 'replace')
    try:
        print(json.dumps(json.loads(text), indent=2)[:4000])
    except ValueError:
        print(text[:2000])
    return 0 if status < 400 else 1


if __name__ == '__main__':
    sys.exit(main())
