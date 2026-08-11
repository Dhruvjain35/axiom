"""AXIOM — durable memory for agents that take real actions.

    Memory is not saved chat history. Memory is what makes autonomous action safe.

Module map:
    config       settings, and the three constants the SQL also encodes
    db           the pool, tx() with 40001 retry, and the one vector formatter
    models       the vocabulary, mirroring db/001_schema.sql
    embeddings   Bedrock Titan V2, with a deterministic offline stand-in
    llm          Bedrock Claude triage; proposes, never authorizes
    events       the append-only journal
    memory       episodic + semantic — ADVISES
    policy       procedural — AUTHORIZES
    tasks        execution — CONSTRAINS. The five protocols.
    provider     the external world, and chaos
    worker       the process you are meant to kill
    api          FastAPI + Mission Control
"""

__version__ = '0.1.0'
