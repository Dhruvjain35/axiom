#!/usr/bin/env python3
"""A plain tool-calling agent loop — the model proposes, we execute — with AXIOM behind
the one tool that moves money.

    python examples/01_tool_calling_agent.py

The loop runs TWICE against the same complaint. Run 1 is killed with os._exit(9) at the
worst possible instant: the refund has landed at the provider and nothing has recorded
it. Run 2 is an ordinary restart. The ledger at the end is the argument.

The only line here that AXIOM required is `idempotency_key=` inside the tool.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid

os.environ.setdefault('DATABASE_URL', 'postgresql://root@localhost:26257/axiom?sslmode=disable')
os.environ.setdefault('AXIOM_OFFLINE', '1')        # no Bedrock credentials needed
os.environ.setdefault('AXIOM_LEASE_SECONDS', '2')  # a dead worker's task frees up fast
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _setup import setup                                            # noqa: E402
from axiom import provider                                          # noqa: E402
from axiom.adapter import guard, shutdown                           # noqa: E402

CRASH = os.environ.get('CRASH_AFTER_EFFECT') == '1'
ORDER = os.environ.get('AXIOM_EXAMPLE_ORDER', f'ORD-{uuid.uuid4().hex[:8].upper()}')

# ------------------------------------------------------------------ the tool, guarded
# `key='order_id'` is the entire safety argument. The idempotency key is derived from the
# order — not from a uuid, not from a clock — so the process that restarts after the crash
# below arrives at the SAME key, and the provider replays instead of refunding twice.
@guard(action='refund', key='order_id', amount='amount_cents',
       provider='payments', operation='refunds.create')
def issue_refund(order_id: str, amount_cents: int, idempotency_key: str) -> dict:
    """Pay a customer back. An ordinary function; AXIOM fills in `idempotency_key`."""
    r = provider.create_refund(idempotency_key=idempotency_key, order_ref=order_id,
                               amount_cents=amount_cents, latency_ms=0)
    if CRASH:
        print(f'   !! the money moved ({r.provider_ref}) and the process dies HERE',
              flush=True)
        os._exit(9)                     # a real SIGKILL: no finally, no atexit, nothing
    return {'id': r.provider_ref, 'amount_cents': amount_cents, 'replayed': r.replayed}

TOOLS = {'issue_refund': issue_refund}

def model(transcript: list[dict]) -> dict:
    """Stand-in for `client.messages.create(..., tools=TOOLS)`. Deterministic, so the
    example needs no API key; the block shapes and the loop below are the real ones."""
    if any(m['role'] == 'tool' for m in transcript):
        return {'type': 'text', 'text': f'Refunded {ORDER}. Told the customer.'}
    return {'type': 'tool_use', 'name': 'issue_refund',
            'input': {'order_id': ORDER, 'amount_cents': 2999}}

def agent_loop() -> None:
    transcript = [{'role': 'user', 'content': f'{ORDER} was charged twice.'}]
    while True:
        step = model(transcript)
        if step['type'] == 'text':
            return print(f'   model: {step["text"]}', flush=True)
        print(f'   model -> {step["name"]}({step["input"]})', flush=True)
        out = TOOLS[step['name']](**step['input'])          # <- the guarded call
        print(f'   tool  -> {out}', flush=True)
        transcript.append({'role': 'tool', 'content': out})

def main() -> int:
    _, mission = setup('tool-calling agent', f'refund {ORDER} exactly once')
    if CRASH:
        return agent_loop() or 0                            # never returns

    print(f'\n== run 1: this process will be killed mid-refund ({ORDER}) ==', flush=True)
    rc = subprocess.run(
        [sys.executable, os.path.abspath(__file__)],
        env={**os.environ, 'CRASH_AFTER_EFFECT': '1', 'PYTHONUNBUFFERED': '1',
             'AXIOM_EXAMPLE_ORDER': ORDER, 'AXIOM_EXAMPLE_MISSION': str(mission)},
    ).returncode
    print(f'   process exited {rc} — receipt committed, outcome never recorded')

    print('\n== run 2: an ordinary restart, once the dead lease lapses ==')
    time.sleep(float(os.environ['AXIOM_LEASE_SECONDS']) + 0.4)
    agent_loop()

    print('\n== the provider ledger, which AXIOM cannot edit ==')
    for row in provider.ledger(ORDER):
        print(f'   {row["provider_ref"]}  {row["amount_cents"]}c  '
              f'replays={row["replay_count"]}  key={row["idempotency_key"]}')
    print(f'   orders refunded more than once: {provider.duplicate_check([ORDER]) or "NONE"}')
    shutdown()
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
