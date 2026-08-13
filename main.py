"""AXIOM :: the Vercel entrypoint.

Vercel's Python runtime looks for a top-level `app` in one of a handful of filenames at
the project root; this is that file and nothing more. The application itself is
`axiom.api`, unchanged — the same module that runs under uvicorn on a laptop, inside the
Lambda ZIP behind Mangum, and in the Docker image. A deployment target that required its
own fork of the app would be a deployment target that drifts.

Why this file exists at all rather than pointing Vercel at axiom/api.py: the entrypoint
must be importable at the project root, and `axiom` must stay a package so the workers,
the tests and the Lambda handler keep importing it the same way.

## Serverless notes

**Connection pools.** Every Vercel Function instance gets its own pool, and CockroachDB
Basic caps concurrent connections. AXIOM_POOL_MIN/AXIOM_POOL_MAX are therefore set small
in the project's environment (1 and 3): the arithmetic that matters is
instances x pool_max, not pool_max alone. `db.py` already enables check_connection, which
is what makes a resumed-from-frozen instance reconnect instead of serving a 500 off a dead
socket — the same property that mattered on Lambda.

**Workers.** A serverless function cannot spawn a background process that outlives the
request, so `POST /api/demo/run-worker` runs the worker INLINE here (AXIOM_WORKER_INLINE).
It drains for a bounded number of seconds inside the request and returns a summary. That
is a demo affordance, not a change to the engine: it is the same `Worker.run()` loop the
ECS and Lambda deployments call, given a deadline instead of a lifetime.
"""

from __future__ import annotations

from axiom.api import app

__all__ = ['app']
