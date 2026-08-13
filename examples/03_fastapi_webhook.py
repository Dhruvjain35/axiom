#!/usr/bin/env python3
"""A FastAPI webhook handler, where crash-mid-request is Tuesday.

    python examples/03_fastapi_webhook.py

Webhooks retry — that is the contract. If the sender does not get a 2xx it delivers
again, and it cannot know whether your handler died before or after it moved money. Below
a real uvicorn server is killed with os._exit(9) inside the request, after the refund has
landed; the redelivery returns 200 carrying the ORIGINAL refund id, because the guard
derives the identity of the act from the order rather than from the delivery.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

os.environ.setdefault('DATABASE_URL', 'postgresql://root@localhost:26257/axiom?sslmode=disable')
os.environ.setdefault('AXIOM_OFFLINE', '1')
os.environ.setdefault('AXIOM_LEASE_SECONDS', '2')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI                                          # noqa: E402
from _setup import setup                                             # noqa: E402
from axiom import provider                                           # noqa: E402
from axiom.adapter import ApprovalRequired, guard, shutdown          # noqa: E402

CRASH = os.environ.get('CRASH_AFTER_EFFECT') == '1'
PORT = int(os.environ.get('AXIOM_EXAMPLE_PORT', '8077'))
ORDER = os.environ.get('AXIOM_EXAMPLE_ORDER', 'ORD-WH-' + os.urandom(3).hex().upper())
app = FastAPI()

@guard(action='refund', key='order_id', amount='amount_cents',
       provider='payments', operation='refunds.create')
def issue_refund(order_id: str, amount_cents: int, idempotency_key: str) -> dict:
    r = provider.create_refund(idempotency_key=idempotency_key, order_ref=order_id,
                               amount_cents=amount_cents, latency_ms=0)
    if CRASH:
        print(f'   !! {r.provider_ref} exists; the SERVER dies mid-request', flush=True)
        os._exit(9)
    return {'refund_id': r.provider_ref, 'replayed': r.replayed}

@app.post('/webhooks/dispute')
def dispute(event: dict) -> dict:
    """Redelivery-safe by construction: nothing here dedupes, and it does not have to."""
    try:
        return {'status': 'refunded',
                **issue_refund(order_id=event['order_id'],
                               amount_cents=event['amount_cents'])}
    except ApprovalRequired as e:            # over policy: parked, nothing sent
        return {'status': 'awaiting_human', 'approval_id': str(e.approval_id)}

def _serve(crash: bool, mission) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), '--serve'],
        env={**os.environ, 'CRASH_AFTER_EFFECT': '1' if crash else '0',
             'PYTHONUNBUFFERED': '1', 'AXIOM_EXAMPLE_MISSION': str(mission),
             'AXIOM_EXAMPLE_ORDER': ORDER})

def _deliver() -> dict:
    """Retry a REFUSED connection (the server is still booting); never retry a DROPPED
    one — that is the crash this example is about and the caller must see it."""
    req = urllib.request.Request(
        f'http://127.0.0.1:{PORT}/webhooks/dispute',
        json.dumps({'order_id': ORDER, 'amount_cents': 7800}).encode(),
        {'Content-Type': 'application/json'})
    for _ in range(150):
        try:
            return json.load(urllib.request.urlopen(req, timeout=15))
        except urllib.error.URLError as e:
            if not isinstance(e.reason, ConnectionRefusedError):
                raise
            time.sleep(0.15)
    raise SystemExit('server never came up')

def main() -> int:
    _, mission = setup('fastapi webhook', f'refund {ORDER} exactly once')
    if '--serve' in sys.argv:
        import uvicorn
        uvicorn.run(app, host='127.0.0.1', port=PORT, log_level='error')
        return 0

    print(f'\n== delivery 1: the server dies inside the handler ({ORDER}) ==')
    srv = _serve(crash=True, mission=mission)
    try:
        print(f'   sender got: {_deliver()}')
    except Exception as e:
        print(f'   sender got: {type(e).__name__} — no 2xx, so it will redeliver')
    srv.wait()

    print('\n== delivery 2: the same event, against a restarted server ==')
    time.sleep(float(os.environ['AXIOM_LEASE_SECONDS']) + 0.4)   # the dead lease lapses
    srv = _serve(crash=False, mission=mission)
    print(f'   sender got: {_deliver()}')
    srv.terminate()

    led = provider.ledger(ORDER)
    print(f'\n== the provider ledger ==\n   {led[0]["provider_ref"]}  '
          f'{led[0]["amount_cents"]}c  replays={led[0]["replay_count"]}\n'
          f'   orders refunded more than once: {provider.duplicate_check([ORDER]) or "NONE"}')
    shutdown()
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
