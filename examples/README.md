# examples/

Three runnable arguments that AXIOM drops in behind an agent someone already wrote. The
integration itself is documented in [`docs/INTEGRATION.md`](../docs/INTEGRATION.md); this
directory is the proof.

Each one issues a refund, is interrupted at the worst possible instant — after the money
has moved and before anything has recorded it — and then resumes. Each one ends by
printing the external provider's ledger, which AXIOM has no ability to edit.

```bash
export DATABASE_URL='postgresql://root@localhost:26257/axiom?sslmode=disable'

python examples/01_tool_calling_agent.py     # no extra dependencies
python examples/03_fastapi_webhook.py        # no extra dependencies

pip install -r examples/requirements.txt     # langgraph
python examples/02_langgraph_tool.py
```

`AXIOM_OFFLINE=1` is set by each script, so none of them needs AWS credentials: the
embedder is a deterministic local one and the engine cannot tell the difference.

| file | the failure it stages | the argument |
|---|---|---|
| `01_tool_calling_agent.py` | `os._exit(9)` inside the tool, in a child process | a plain model-proposes/we-execute loop needs one decorator and one `idempotency_key=` |
| `02_langgraph_tool.py` | an exception inside the tool node | LangGraph checkpoints graph **state**, on node completion. Nothing about a half-finished side effect is in the checkpoint, so the resume re-runs the node. That is correct for a pure node and a second refund for this one — AXIOM makes the re-run a replay. Honest caveat: the interruption is an exception rather than a SIGKILL, because an in-memory checkpointer cannot outlive its process; example 01 does the real kill |
| `03_fastapi_webhook.py` | a real uvicorn server `os._exit(9)`-ed mid-request | webhooks retry — that *is* the contract — and the sender cannot know whether you died before or after moving money. The redelivery returns 200 with the original refund id |

`_setup.py` is scaffolding, not surface: it creates the tenant, publishes the policy and
opens a mission, because in your application those already exist.

## Reading the output

`replayed=True` is the whole point. It means the provider recognized the idempotency key
from the attempt that died and returned the ORIGINAL refund instead of making a new one.
`replays=1` on the ledger row is the same fact from the provider's side, and
`orders refunded more than once: NONE` is the claim in the form a customer would check.

## Re-running

Each script generates a fresh order reference per run, so runs do not collide. If you call
a guarded function twice with the SAME arguments after it succeeded, you get the recorded
result back without the provider being touched at all — which is a different (and much
more common) property, covered in `tests/test_adapter.py`.
