#!/usr/bin/env python3
"""The same guarantee, under a LangGraph tool node.

    pip install -r examples/requirements.txt
    python examples/02_langgraph_tool.py

LangGraph checkpoints graph STATE, on node completion. Die *inside* the tool node, after
the refund has left for the provider, and the checkpoint holds nothing about it: the
resume re-enters the node and the tool runs again. Correct for a pure node; a second
refund for this one. The checkpointer is not wrong, it is answering a different question.
AXIOM answers "did the money already move?" from a durable receipt, so the re-run carries
the same idempotency key and the provider replays.

Honest about the demo: the interruption is an exception, not a SIGKILL, because an
in-memory checkpointer cannot outlive the process. Example 01 does the real os._exit(9).
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault('DATABASE_URL', 'postgresql://root@localhost:26257/axiom?sslmode=disable')
os.environ.setdefault('AXIOM_OFFLINE', '1')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage    # noqa: E402
from langchain_core.tools import tool                                       # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver                       # noqa: E402
from langgraph.graph import END, START, MessagesState, StateGraph           # noqa: E402
from langgraph.prebuilt import ToolNode                                     # noqa: E402

from _setup import setup                                                    # noqa: E402
from axiom import provider                                                  # noqa: E402
from axiom.adapter import guard, shutdown                                   # noqa: E402

ORDER = 'ORD-LG-' + os.urandom(3).hex().upper()
INTERRUPT = {'now': True}                          # switched off before the resume

# ------------------------------------------- AXIOM sits UNDER the framework's tool
@guard(action='refund', key='order_id', amount='amount_cents',
       provider='payments', operation='refunds.create')
def _refund(order_id: str, amount_cents: int, idempotency_key: str) -> dict:
    r = provider.create_refund(idempotency_key=idempotency_key, order_ref=order_id,
                               amount_cents=amount_cents, latency_ms=0)
    if INTERRUPT['now']:
        raise RuntimeError(f'process dies here — {r.provider_ref} already exists')
    return {'id': r.provider_ref, 'replayed': r.replayed}

@tool
def issue_refund(order_id: str, amount_cents: int) -> str:
    """Refund a customer for an order. (The tool the model sees — unchanged by AXIOM.)"""
    r = _refund(order_id=order_id, amount_cents=amount_cents)
    return f'refund {r["id"]} issued for {order_id} (provider replayed={r["replayed"]})'

def agent(state: MessagesState) -> dict:
    """Stand-in for a model node bound to the tools."""
    if any(isinstance(m, ToolMessage) for m in state['messages']):
        return {'messages': [AIMessage(content=f'{ORDER} is refunded.')]}
    return {'messages': [AIMessage(content='', tool_calls=[
        {'name': 'issue_refund', 'id': 'call_1',
         'args': {'order_id': ORDER, 'amount_cents': 4200}}])]}

graph = StateGraph(MessagesState)
graph.add_node('agent', agent)
# handle_tool_errors=False: let the failure escape the graph. The default turns it into a
# ToolMessage, which would hide the very moment this example is about.
graph.add_node('tools', ToolNode([issue_refund], handle_tool_errors=False))
graph.add_edge(START, 'agent')
graph.add_conditional_edges(
    'agent', lambda s: 'tools' if getattr(s['messages'][-1], 'tool_calls', None) else END,
    ['tools', END])
graph.add_edge('tools', 'agent')
app = graph.compile(checkpointer=InMemorySaver())

def main() -> int:
    setup('langgraph tool', f'refund {ORDER} exactly once')
    cfg = {'configurable': {'thread_id': ORDER}}
    print(f'\n== run 1: the tool node is interrupted after the refund lands ({ORDER}) ==')
    try:
        app.invoke({'messages': [HumanMessage(f'{ORDER} was charged twice')]}, cfg)
    except RuntimeError as e:
        print(f'   {e}')
    print(f'   graph is parked before {app.get_state(cfg).next} — the node never '
          f'completed, so no checkpoint knows a refund happened')

    print('\n== run 2: resume the same thread from the checkpoint ==')
    INTERRUPT['now'] = False
    out = app.invoke(None, cfg)                    # None = resume where it left off
    print(f'   {[m.content for m in out["messages"] if isinstance(m, ToolMessage)][0]}')
    print(f'   final: {out["messages"][-1].content}')

    led = provider.ledger(ORDER)
    print(f'\n== the provider ledger ==\n   {led[0]["provider_ref"]}  '
          f'{led[0]["amount_cents"]}c  replays={led[0]["replay_count"]}\n'
          f'   orders refunded more than once: {provider.duplicate_check([ORDER]) or "NONE"}')
    shutdown()
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
