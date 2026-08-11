"""AXIOM :: the Audit Agent.

Natural-language questions, answered in SQL, against the same live database the
agents write to. No ETL, no warehouse, no second copy that can disagree with the
first — which is the whole argument for putting execution state and semantic memory
in one transactional store to begin with.

    python -m axiom.audit_mcp "was any order ever refunded twice?"

Two transports, one agent
-------------------------
**MCP mode** talks to CockroachDB Cloud's Managed MCP Server at
https://cockroachlabs.cloud/mcp over streamable HTTP, authenticating with a scoped
read-only service-account API key as a bearer token, optionally pinned to one
cluster with the `mcp-cluster-id` header. That is the transport the hackathon asks
for and the one the deployed demo uses.

**LOCAL mode** answers the same questions over a plain read-only psycopg connection.
It exists for two reasons, and only one of them is convenience: the Cloud cluster's
service-account key is not present in every environment (CI, a fresh clone, an
offline judge), and a demo feature that cannot run without a secret is a demo
feature that eventually does not run at all. The agent, the tool definitions, the
guard, and the answers are identical in both modes — only the thing that executes
the SELECT changes.

Why the tool arguments are discovered, not hard-coded
----------------------------------------------------
The Managed MCP Server's docs name its tools (`list_tables`, `get_table_schema`,
`select_query`, `explain_query`, …) but do not publish their argument names. Rather
than guess `sql=` vs `statement=` vs `query=` and ship something that 400s on first
contact, McpBackend calls `tools/list` at connect time and reads each tool's
`inputSchema` to decide what to send. Guessing would have been shorter and would
have been wrong somewhere.

Containment
-----------
The agent writes SQL. An LLM that can be argued into `UPDATE axiom_action_attempt
SET attempt_state = 'SUCCEEDED'` would falsify the exact audit trail it exists to
read, so three independent layers stop it, and each holds if the other two are wrong:

  1. db/002_audit_role.sql — a role with SELECT and nothing else. Database-enforced.
  2. `guard_select()` below — one statement, SELECT/WITH only, no DML keyword
     anywhere in the text, no comment syntax, LIMIT injected.
  3. `default_transaction_read_only` on the login, plus an explicit
     `SET TRANSACTION READ ONLY` on every statement this module runs.

Layer 2 scans the WHOLE statement rather than just its first token, and that is not
belt-and-braces: CockroachDB accepts `WITH x AS (INSERT … RETURNING …) SELECT * FROM x`,
which begins with WITH, ends as a SELECT, and writes. A leading-keyword check would
pass it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import textwrap
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg.rows import dict_row

from .config import settings

# The audit agent is a different model call from the worker's triage, and gets its
# own knob: it is invoked by a human at demo time, not in the hot loop, so it can
# afford the better model. NOTE the `us.` inference-profile prefix — Sonnet 4.5 is
# not available for on-demand invocation under its bare model id in this account,
# and the bare id fails with a ValidationException at call time.
AUDIT_MODEL = os.environ.get(
    'AXIOM_AUDIT_MODEL', 'us.anthropic.claude-sonnet-4-5-20250929-v1:0')

MCP_URL = os.environ.get('CC_MCP_URL', 'https://cockroachlabs.cloud/mcp')

DEFAULT_MAX_ROWS = 50


# ============================================================== the SQL guard

class UnsafeSQL(RuntimeError):
    """A statement the audit agent proposed that this module refuses to execute."""


# Whole-word match. `updated_at` must not trip the UPDATE rule, and `created_at`
# must not trip CREATE — hence the \b anchors rather than a substring scan.
_FORBIDDEN = re.compile(
    r'\b('
    r'insert|update|delete|upsert|merge|truncate|drop|alter|create|rename|'
    r'grant|revoke|comment|copy|import|export|backup|restore|'
    r'begin|commit|rollback|savepoint|prepare|execute|deallocate|discard|'
    r'set|reset|call|do|refresh|cancel|pause|resume|split|unsplit|scatter|relocate'
    r')\b', re.IGNORECASE)

_LEADING = re.compile(r'^\s*(select|with)\b', re.IGNORECASE)
# The OFFSET tail is not decoration. Bedrock writes `... LIMIT 20 OFFSET 40` as a
# matter of course, and an end-anchored `LIMIT \d+` does not match it — so the cap
# below appended a SECOND limit and the statement died with `syntax error at or near
# "limit"`. Still anchored to the tail rather than searched anywhere in the text: a
# LIMIT inside a subquery must not suppress the cap on the outer result set.
_HAS_LIMIT = re.compile(r'\blimit\s+\d+(\s+offset\s+\d+)?\s*$', re.IGNORECASE)


def guard_select(sql: str, *, max_rows: int = DEFAULT_MAX_ROWS) -> str:
    """Return `sql` if it is provably a single read, else raise UnsafeSQL.

    Rejecting is always safe here: the worst case is the agent has to rephrase,
    and the cost of a false negative is a corrupted audit trail.
    """
    text = sql.strip().rstrip(';').strip()
    if not text:
        raise UnsafeSQL('empty statement')

    # Comment syntax is banned outright rather than stripped. Stripping means
    # writing a SQL lexer, and a lexer that disagrees with CockroachDB's by one
    # edge case is exactly the hole this guard exists to close.
    if '--' in text or '/*' in text:
        raise UnsafeSQL('comments are not allowed in audit queries')
    if ';' in text:
        raise UnsafeSQL('only one statement per query')
    if not _LEADING.match(text):
        raise UnsafeSQL('audit queries must start with SELECT or WITH')

    bad = _FORBIDDEN.search(text)
    if bad:
        raise UnsafeSQL(
            f'`{bad.group(1).upper()}` is not permitted in an audit query — '
            f'the audit agent reads, it never writes')

    if not _HAS_LIMIT.search(text):
        text = f'{text} LIMIT {int(max_rows)}'
    return text


# =========================================================== the query catalog

# Curated answers to the questions the demo actually asks. They serve three jobs:
# they are what LOCAL-offline mode executes when there is no model available, they
# are few-shot grounding in the system prompt so the model writes SQL in the same
# shape, and they are a regression surface — if the schema moves, these break loudly
# instead of the agent quietly inventing a column.
@dataclass(frozen=True)
class NamedQuery:
    key: str
    question: str
    sql: str
    keywords: tuple[str, ...]


CATALOG: tuple[NamedQuery, ...] = (
    NamedQuery(
        key='duplicate_refunds',
        question='Was any order ever refunded twice?',
        sql="""
            SELECT order_ref,
                   count(*)          AS refund_count,
                   sum(amount_cents) AS total_cents
            FROM provider.public.provider_refund
            GROUP BY order_ref
            HAVING count(*) > 1
            ORDER BY refund_count DESC
        """,
        keywords=('refund', 'twice', 'duplicate', 'double', 'again', 'two'),
    ),
    NamedQuery(
        key='effects_licensed',
        question='Which real-world effects did a given memory license?',
        sql="""
            SELECT a.licensed_by_memory_id,
                   a.id AS attempt_id, a.task_id, a.step_name, a.provider,
                   a.operation, a.amount_cents, a.attempt_state, a.provider_ref,
                   a.settled_at
            FROM axiom.public.axiom_action_attempt a
            WHERE a.licensed_by_memory_id IS NOT NULL
            ORDER BY a.prepared_at DESC
        """,
        keywords=('memory', 'license', 'licensed', 'effect', 'effects',
                  'authorized', 'blast'),
    ),
    NamedQuery(
        key='recovered_tasks',
        question='Show me every task that recovered from a crash.',
        sql="""
            SELECT e.task_id,
                   t.dedupe_key,
                   t.state             AS final_state,
                   e.detail->>'action' AS recovery_action,
                   e.lease_epoch,
                   e.occurred_at
            FROM axiom.public.axiom_event e
            JOIN axiom.public.axiom_task t ON t.id = e.task_id
            WHERE e.event_type = 'task.recovered'
            ORDER BY e.occurred_at DESC
        """,
        keywords=('recover', 'recovered', 'crash', 'crashed', 'died', 'resume',
                  'restart', 'kill'),
    ),
    NamedQuery(
        key='replayed_refunds',
        question='Which refunds were re-sent under the same idempotency key?',
        sql="""
            SELECT order_ref, provider_ref, amount_cents, replay_count,
                   idempotency_key, created_at, last_seen_at
            FROM provider.public.provider_refund
            WHERE replay_count > 0
            ORDER BY replay_count DESC
        """,
        keywords=('replay', 'replayed', 'resent', 're-sent', 'idempotency',
                  'idempotent', 'same key'),
    ),
    NamedQuery(
        key='unsettled',
        question='What external calls might still be in flight?',
        sql="""
            SELECT id AS attempt_id, task_id, step_name, provider, operation,
                   amount_cents, attempt_state, idempotency_key, prepared_at
            FROM axiom.public.axiom_action_attempt
            WHERE attempt_state IN ('PREPARED', 'DISPATCHED')
            ORDER BY prepared_at
        """,
        keywords=('flight', 'unsettled', 'pending', 'unsure', 'reconcile',
                  'outstanding'),
    ),
    NamedQuery(
        key='ledger_vs_belief',
        question="Does the agent's record agree with the provider's ledger?",
        sql="""
            SELECT a.provider_ref,
                   a.amount_cents AS axiom_cents,
                   r.amount_cents AS provider_cents,
                   a.attempt_state,
                   r.replay_count
            FROM axiom.public.axiom_action_attempt a
            FULL OUTER JOIN provider.public.provider_refund r
              ON r.provider_ref = a.provider_ref
            WHERE a.provider_ref IS NULL
               OR r.provider_ref IS NULL
               OR a.amount_cents != r.amount_cents
        """,
        keywords=('agree', 'disagree', 'mismatch', 'ledger', 'reconciliation',
                  'discrepancy', 'drift'),
    ),
)

_UUID_RE = re.compile(
    r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b', re.I)


def match_query(question: str) -> NamedQuery | None:
    """Cheapest possible intent match: keyword overlap, highest score wins.

    Deliberately not a model call. This is the path that runs when there is no
    model, so making it depend on one would defeat it.
    """
    q = question.lower()
    best, best_score = None, 0
    for nq in CATALOG:
        score = sum(1 for k in nq.keywords if k in q)
        if score > best_score:
            best, best_score = nq, score
    return best if best_score else None


# ================================================================== backends

@dataclass
class QueryResult:
    sql: str
    rows: list[dict]
    truncated: bool = False

    def as_text(self, limit: int = 40) -> str:
        if not self.rows:
            return '(0 rows)'
        head = self.rows[:limit]
        return json.dumps(head, indent=None, default=str)


class Backend(Protocol):
    mode: str

    def list_tables(self) -> list[dict]: ...
    def describe_table(self, table: str) -> list[dict]: ...
    def run_sql(self, sql: str, *, max_rows: int = DEFAULT_MAX_ROWS) -> QueryResult: ...
    def close(self) -> None: ...


# ------------------------------------------------------------------ local

def audit_url() -> str:
    """DSN for the read-only audit login.

    Defaults to the same cluster and database as the engine but as `axiom_audit`,
    the SELECT-only user created by db/002_audit_role.sql. Swapping only the
    userinfo means the audit agent cannot accidentally be pointed at a different
    cluster than the one being audited, which would make its answers true about
    the wrong system.
    """
    explicit = os.environ.get('AUDIT_DATABASE_URL')
    if explicit:
        return explicit
    parts = urlsplit(settings.database_url)
    host = parts.hostname or 'localhost'
    netloc = f'axiom_audit@{host}' + (f':{parts.port}' if parts.port else '')
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


class LocalBackend:
    """Read-only psycopg connection. Same questions, no Cloud dependency."""

    mode = 'LOCAL'

    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or audit_url()
        self._conn = psycopg.connect(self.dsn, row_factory=dict_row,
                                     application_name='axiom-audit')
        self._conn.autocommit = False

    def _read(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._conn.cursor() as cur:
            # Layer 3. Redundant with the role's default_transaction_read_only,
            # and stated anyway so the guarantee survives being run as a user
            # somebody forgot to configure.
            cur.execute('SET TRANSACTION READ ONLY')
            cur.execute(sql, params)
            rows = cur.fetchall()
        self._conn.rollback()          # a read transaction is never worth committing
        return rows

    def list_tables(self) -> list[dict]:
        return self._read("""
            SELECT 'axiom' AS database, table_name
            FROM axiom.information_schema.tables
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
            UNION ALL
            SELECT 'provider' AS database, table_name
            FROM provider.information_schema.tables
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
            ORDER BY 1, 2
        """)

    def describe_table(self, table: str) -> list[dict]:
        db, _, name = table.rpartition('.')
        db = (db or 'axiom').split('.')[0]
        if db not in ('axiom', 'provider'):
            raise UnsafeSQL(f'unknown database {db!r}')
        return self._read(f"""
            SELECT column_name, data_type, is_nullable, column_default
            FROM {db}.information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            ORDER BY ordinal_position
        """, (name,))

    def run_sql(self, sql: str, *, max_rows: int = DEFAULT_MAX_ROWS) -> QueryResult:
        safe = guard_select(sql, max_rows=max_rows)
        rows = self._read(safe)
        return QueryResult(sql=safe, rows=rows, truncated=len(rows) >= max_rows)

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:                        # noqa: BLE001 — teardown only
            pass


# -------------------------------------------------------------------- MCP

class McpError(RuntimeError):
    pass


class McpBackend:
    """CockroachDB Cloud Managed MCP Server over streamable HTTP.

    JSON-RPC 2.0 in the body; the server may answer with `application/json` or an
    SSE frame, so both are parsed. The session id the server hands back on
    `initialize` is echoed on every later call — omitting it is the usual reason a
    working handshake is followed by a stream of 400s.
    """

    mode = 'MCP'

    def __init__(self, url: str | None = None, api_key: str | None = None,
                 cluster_id: str | None = None, timeout: float = 30.0):
        self.url = url or MCP_URL
        self.api_key = api_key or os.environ.get('CC_API_KEY') or ''
        self.cluster_id = cluster_id or os.environ.get('CC_CLUSTER_ID') or ''
        self.timeout = timeout
        self.session_id: str | None = None
        self._id = 0
        self._tools: dict[str, dict] = {}
        if not self.api_key:
            raise McpError(
                'CC_API_KEY is not set. Create a service account and an API key in '
                'the CockroachDB Cloud Console, grant it the read-only role from '
                'db/002_audit_role.sql, and export CC_API_KEY. Run with '
                '--mode local to audit the local cluster instead.')

    # ---------------------------------------------------------------- wire

    def _headers(self) -> dict[str, str]:
        h = {
            'Content-Type': 'application/json',
            # The server chooses; we must be able to read either.
            'Accept': 'application/json, text/event-stream',
            'Authorization': f'Bearer {self.api_key}',
        }
        if self.cluster_id:
            # Scopes every tool call to one cluster server-side. Cheap defence in
            # depth: a tool call naming a different cluster_id is rejected rather
            # than quietly answered about the wrong database.
            h['mcp-cluster-id'] = self.cluster_id
        if self.session_id:
            h['Mcp-Session-Id'] = self.session_id
        return h

    @staticmethod
    def _parse(body: bytes, content_type: str) -> dict:
        if 'text/event-stream' in content_type:
            for line in body.decode('utf-8', 'replace').splitlines():
                if line.startswith('data:'):
                    return json.loads(line[5:].strip())
            raise McpError('SSE response carried no data frame')
        return json.loads(body.decode('utf-8', 'replace') or '{}')

    def _send(self, payload: dict, *, expect_reply: bool = True) -> dict | None:
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode(), headers=self._headers(),
            method='POST')
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                sid = resp.headers.get('Mcp-Session-Id')
                if sid:
                    self.session_id = sid
                body = resp.read()
                if not expect_reply or not body:
                    return None
                msg = self._parse(body, resp.headers.get('Content-Type', ''))
        except urllib.error.HTTPError as e:
            detail = e.read().decode('utf-8', 'replace')[:400]
            raise McpError(f'{self.url} -> HTTP {e.code}: {detail}') from e
        except urllib.error.URLError as e:
            raise McpError(f'cannot reach {self.url}: {e.reason}') from e

        if 'error' in msg:
            raise McpError(f'MCP error {msg["error"].get("code")}: '
                           f'{msg["error"].get("message")}')
        return msg.get('result', {})

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        return self._send({'jsonrpc': '2.0', 'id': self._id, 'method': method,
                           'params': params or {}}) or {}

    def connect(self) -> McpBackend:
        self._rpc('initialize', {
            'protocolVersion': '2025-06-18',
            'capabilities': {},
            'clientInfo': {'name': 'axiom-audit-agent', 'version': '0.1.0'},
        })
        self._send({'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                   expect_reply=False)
        listed = self._rpc('tools/list')
        self._tools = {t['name']: t for t in listed.get('tools', [])}
        if not self._tools:
            raise McpError('the MCP server advertised no tools')
        return self

    # -------------------------------------------------------- tool mapping

    def _arg_name(self, tool: str, candidates: tuple[str, ...]) -> str | None:
        """Pick the argument this server actually calls the thing we want to send.

        The docs name the tools but not their parameters, so the schema the server
        publishes is the authority — not a guess baked into this file at the time
        it was written.
        """
        schema = (self._tools.get(tool) or {}).get('inputSchema') or {}
        props = schema.get('properties') or {}
        for c in candidates:
            if c in props:
                return c
        return next(iter(props), None) if len(props) == 1 else None

    def _call(self, tool: str, args: dict) -> Any:
        if tool not in self._tools:
            raise McpError(f'server does not expose a {tool!r} tool; it has '
                           f'{sorted(self._tools)}')
        if self.cluster_id:
            cid = self._arg_name(tool, ('cluster_id',))
            if cid and cid not in args:
                args[cid] = self.cluster_id
        result = self._rpc('tools/call', {'name': tool, 'arguments': args})
        if result.get('isError'):
            raise McpError(f'{tool} failed: {result.get("content")}')
        return result.get('structuredContent') or result.get('content') or []

    @staticmethod
    def _rows(payload: Any) -> list[dict]:
        """Normalize an MCP tool result into rows.

        Servers return either structured JSON or a list of text blocks holding
        JSON; both shapes are handled rather than assumed.
        """
        if isinstance(payload, dict):
            for k in ('rows', 'results', 'data'):
                if isinstance(payload.get(k), list):
                    return payload[k]
            return [payload]
        out: list[dict] = []
        for block in payload if isinstance(payload, list) else []:
            if isinstance(block, dict) and block.get('type') == 'text':
                try:
                    parsed = json.loads(block.get('text', ''))
                except json.JSONDecodeError:
                    out.append({'text': block.get('text', '')})
                    continue
                out.extend(parsed if isinstance(parsed, list) else [parsed])
            elif isinstance(block, dict):
                out.append(block)
        return out

    def list_tables(self) -> list[dict]:
        arg = self._arg_name('list_tables', ('database', 'database_name', 'db'))
        return self._rows(self._call('list_tables', {arg: 'axiom'} if arg else {}))

    def describe_table(self, table: str) -> list[dict]:
        db, _, name = table.rpartition('.')
        args: dict[str, Any] = {}
        t = self._arg_name('get_table_schema', ('table', 'table_name'))
        d = self._arg_name('get_table_schema', ('database', 'database_name', 'db'))
        if t:
            args[t] = name
        if d:
            args[d] = (db or 'axiom').split('.')[0]
        return self._rows(self._call('get_table_schema', args))

    def run_sql(self, sql: str, *, max_rows: int = DEFAULT_MAX_ROWS) -> QueryResult:
        # Guarded before it leaves the process. The server enforces its own
        # SELECT-only policy on select_query; this runs first so a rejected
        # statement never becomes a network request in the first place.
        safe = guard_select(sql, max_rows=max_rows)
        arg = self._arg_name('select_query', ('statement', 'sql', 'query'))
        if not arg:
            raise McpError('cannot determine select_query\'s argument name')
        rows = self._rows(self._call('select_query', {arg: safe}))
        return QueryResult(sql=safe, rows=rows, truncated=len(rows) >= max_rows)

    def close(self) -> None:
        return None


def open_backend(mode: str = 'auto') -> Backend:
    """`auto` prefers MCP when a key exists and falls back to LOCAL loudly."""
    if mode == 'local':
        return LocalBackend()
    if mode == 'mcp':
        return McpBackend().connect()
    if os.environ.get('CC_API_KEY'):
        try:
            return McpBackend().connect()
        except McpError as e:
            print(f'[audit] MCP unavailable ({e}); falling back to LOCAL',
                  file=sys.stderr)
    return LocalBackend()


# ==================================================================== agent

_SCHEMA_BRIEF = """\
Two databases on one CockroachDB cluster.

axiom.public — what the agent believes and what it did
  axiom_task            id, tenant_id, mission_id, task_type, dedupe_key, state,
                        shard, attempt, max_attempts, lease_epoch, lease_owner,
                        available_at, payload JSONB, result JSONB, last_error
  axiom_action_attempt  THE RECEIPT. id, task_id, step_name, step_seq,
                        idempotency_key (GENERATED), attempt_state, provider,
                        operation, amount_cents, request_fingerprint,
                        provider_ref, http_status, lease_epoch,
                        licensed_by_memory_id, prepared_at, settled_at
  axiom_event           append-only journal: subject_type, subject_id, seq,
                        event_type, from_state, to_state, actor, lease_epoch,
                        task_id, attempt_id, detail JSONB, occurred_at
  axiom_memory          episodic/semantic memory: memory_class, context_key,
                        content, outcome, trust_level, retrieval_class,
                        quarantined, superseded_by
  axiom_mission, axiom_approval, axiom_policy, axiom_agent, axiom_tenant

provider.public — the EXTERNAL world, a separate database with no shared txn
  provider_refund       provider_ref, idempotency_key, request_fingerprint,
                        order_ref, amount_cents, status, replay_count
  provider_request_log  idempotency_key, verdict, http_status, received_at

Facts that matter when answering:
  * Cross-database queries need fully qualified names, e.g.
    provider.public.provider_refund.
  * A duplicate refund means two ROWS in provider_refund for one order_ref.
    replay_count > 0 is NOT a duplicate — it is the same refund returned again
    under the same idempotency key, which is the system working correctly.
  * A task that recovered from a crash has a 'task.recovered' row in axiom_event.
  * attempt_state IN ('PREPARED','DISPATCHED') means an effect MAY exist and has
    not been settled.
"""

_SYSTEM = f"""You are AXIOM's audit agent. You answer questions about an autonomous
refund agent by querying its live database directly. You are read-only.

{_SCHEMA_BRIEF}

How to work:
  1. Call run_sql with a SELECT. Use list_tables / describe_table if unsure.
  2. Read the rows. If they do not answer the question, query again.
  3. Answer in at most four sentences, in plain language, stating the numbers you
     actually saw. Then quote the SQL you ran on a final line prefixed "SQL: ".

Rules:
  * SELECT and WITH only. Any write keyword is rejected before it executes.
  * Never state a number you did not read out of a result set.
  * If the result is empty, say so plainly — an empty duplicate-refund result is
    the headline finding, not a failure to answer."""

_TOOLS = [
    {
        'name': 'run_sql',
        'description': ('Run one read-only SQL statement against the live cluster '
                        'and return the rows. SELECT/WITH only.'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'sql': {'type': 'string',
                        'description': 'A single SELECT statement. Fully qualify '
                                       'cross-database tables.'},
            },
            'required': ['sql'],
        },
    },
    {
        'name': 'list_tables',
        'description': 'List base tables in the axiom and provider databases.',
        'input_schema': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'describe_table',
        'description': 'Column names and types for one table, e.g. '
                       '"axiom.axiom_action_attempt" or "provider.provider_refund".',
        'input_schema': {
            'type': 'object',
            'properties': {'table': {'type': 'string'}},
            'required': ['table'],
        },
    },
]


@dataclass
class AuditAnswer:
    question: str
    answer: str
    mode: str
    engine: str                      # 'bedrock' | 'catalog'
    queries: list[QueryResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            'question': self.question,
            'answer': self.answer,
            'mode': self.mode,
            'engine': self.engine,
            'queries': [{'sql': ' '.join(q.sql.split()), 'row_count': len(q.rows),
                         'rows': q.rows} for q in self.queries],
        }


class AuditAgent:
    def __init__(self, backend: Backend, *, offline: bool | None = None,
                 max_steps: int = 6):
        self.backend = backend
        self.offline = settings.offline if offline is None else offline
        self.max_steps = max_steps
        self._bedrock = None

    # ------------------------------------------------------------- tools

    def _dispatch(self, name: str, args: dict) -> tuple[str, QueryResult | None]:
        if name == 'run_sql':
            r = self.backend.run_sql(str(args.get('sql', '')))
            return r.as_text(), r
        if name == 'list_tables':
            return json.dumps(self.backend.list_tables(), default=str), None
        if name == 'describe_table':
            return json.dumps(self.backend.describe_table(
                str(args.get('table', ''))), default=str), None
        return f'unknown tool {name}', None

    # ---------------------------------------------------------- offline

    def _catalog_answer(self, question: str) -> AuditAnswer:
        """Deterministic path. Runs the curated query, states the finding.

        The prose is templated per query rather than generated, because this path
        exists precisely for when there is no model, and a summary that drifted
        run to run would be a worse audit artifact than one that does not.
        """
        nq = match_query(question)
        if nq is None:
            return AuditAnswer(
                question, 'No catalogued audit query matches that question. Run '
                'with AXIOM_OFFLINE=0 to let Bedrock write SQL for it, or ask '
                'about: ' + ', '.join(q.question for q in CATALOG),
                self.backend.mode, 'catalog')

        sql = nq.sql
        mem = _UUID_RE.search(question)
        if nq.key == 'effects_licensed' and mem:
            sql = sql.replace('a.licensed_by_memory_id IS NOT NULL',
                              f"a.licensed_by_memory_id = '{mem.group(0)}'")

        result = self.backend.run_sql(sql)
        n = len(result.rows)

        if nq.key == 'duplicate_refunds':
            answer = ('No. Zero orders appear more than once in the provider ledger.'
                      if n == 0 else
                      f'Yes — {n} order(s) have more than one refund row: ' +
                      ', '.join(f'{r["order_ref"]} x{r["refund_count"]}'
                                for r in result.rows[:5]))
        elif nq.key == 'effects_licensed':
            cents = sum(int(r.get('amount_cents') or 0) for r in result.rows)
            answer = (f'{n} irreversible effect(s), totalling {cents} cents, were '
                      f'licensed by that memory.' if n else
                      'That memory licensed no external effects.')
        elif nq.key == 'recovered_tasks':
            # EVENTS are not TASKS. One task that crashed three times produces three
            # task.recovered rows, and reporting `n` as a task count silently inflates
            # the number. In an audit tool whose entire purpose is that its numbers can
            # be trusted, a plausible-but-wrong count is worse than no answer, so both
            # figures are stated and neither is called the other.
            acts: dict[str, int] = {}
            for r in result.rows:
                acts[r.get('recovery_action')] = acts.get(r.get('recovery_action'), 0) + 1
            distinct = len({r.get('task_id') for r in result.rows})
            answer = (f'{n} recovery event(s) across {distinct} distinct task(s) '
                      f'({", ".join(f"{k}={v}" for k, v in acts.items())}).'
                      if n else 'No task has recovered from a crash yet.')
        elif nq.key == 'replayed_refunds':
            total = sum(int(r.get('replay_count') or 0) for r in result.rows)
            answer = (f'{n} refund(s) were re-sent under their original idempotency '
                      f'key, {total} replay(s) in total — the provider returned the '
                      f'original refund each time.' if n else
                      'No refund was ever re-sent; no crash landed in the dangerous '
                      'window.')
        elif nq.key == 'unsettled':
            answer = (f'{n} external call(s) are still unsettled.' if n else
                      'Nothing is in flight — every authorized effect has a recorded '
                      'outcome.')
        else:
            answer = (f'{n} row(s) disagree between the agent\'s receipts and the '
                      f'provider ledger.' if n else
                      'The agent\'s receipts and the provider ledger agree on every '
                      'row.')

        return AuditAnswer(question, answer, self.backend.mode, 'catalog',
                           queries=[result])

    # ---------------------------------------------------------- bedrock

    def _client(self):
        if self._bedrock is None:
            import boto3
            self._bedrock = boto3.client('bedrock-runtime',
                                         region_name=settings.aws_region)
        return self._bedrock

    def _bedrock_answer(self, question: str) -> AuditAnswer:
        messages: list[dict] = [{'role': 'user', 'content': question}]
        queries: list[QueryResult] = []

        for _ in range(self.max_steps):
            body = json.dumps({
                'anthropic_version': 'bedrock-2023-05-31',
                'max_tokens': 1500,
                'temperature': 0,
                'system': _SYSTEM,
                'tools': _TOOLS,
                'messages': messages,
            })
            resp = self._client().invoke_model(modelId=AUDIT_MODEL, body=body)
            payload = json.loads(resp['body'].read())
            content = payload.get('content', [])
            messages.append({'role': 'assistant', 'content': content})

            if payload.get('stop_reason') != 'tool_use':
                text = '\n'.join(b.get('text', '') for b in content
                                 if b.get('type') == 'text').strip()
                return AuditAnswer(question, text or '(no answer)',
                                   self.backend.mode, 'bedrock', queries)

            # Every tool_use block in the turn must get a tool_result back in ONE
            # user message; splitting them trains the model out of parallel calls.
            results = []
            for block in content:
                if block.get('type') != 'tool_use':
                    continue
                try:
                    out, q = self._dispatch(block['name'], block.get('input') or {})
                    if q is not None:
                        queries.append(q)
                    results.append({'type': 'tool_result',
                                    'tool_use_id': block['id'], 'content': out})
                except (UnsafeSQL, McpError, psycopg.Error) as e:
                    # Returned as an error result rather than raised: the model can
                    # usually repair its own SQL, and a refused statement is a
                    # normal event in an agent that is allowed to propose anything.
                    results.append({'type': 'tool_result',
                                    'tool_use_id': block['id'],
                                    'content': f'{type(e).__name__}: {e}',
                                    'is_error': True})
            messages.append({'role': 'user', 'content': results})

        return AuditAnswer(question, f'gave up after {self.max_steps} tool steps',
                           self.backend.mode, 'bedrock', queries)

    # -------------------------------------------------------------- ask

    def ask(self, question: str) -> AuditAnswer:
        if self.offline:
            return self._catalog_answer(question)
        try:
            return self._bedrock_answer(question)
        except Exception as e:                   # noqa: BLE001
            # A model outage must not take the audit surface down with it — the
            # catalogued queries answer the demo's questions without Bedrock.
            print(f'[audit] Bedrock unavailable ({type(e).__name__}: {e}); '
                  f'using the catalogued query', file=sys.stderr)
            return self._catalog_answer(question)


# ====================================================================== CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog='python -m axiom.audit_mcp',
        description='Ask the AXIOM audit agent a question about the live database.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            examples:
              python -m axiom.audit_mcp "was any order ever refunded twice?"
              python -m axiom.audit_mcp "show me every task that recovered from a crash"
              python -m axiom.audit_mcp --mode mcp "which memories licensed a refund?"

            modes:
              auto   MCP when CC_API_KEY is set, otherwise LOCAL (default)
              mcp    CockroachDB Cloud Managed MCP Server (needs CC_API_KEY)
              local  read-only psycopg connection as the axiom_audit role
        """))
    ap.add_argument('question', nargs='*', help='the question, in plain English')
    ap.add_argument('--mode', choices=('auto', 'mcp', 'local'), default='auto')
    ap.add_argument('--json', action='store_true', help='machine-readable output')
    ap.add_argument('--list', action='store_true',
                    help='list the catalogued audit questions and exit')
    ap.add_argument('--max-rows', type=int, default=DEFAULT_MAX_ROWS)
    args = ap.parse_args(argv)

    if args.list:
        for nq in CATALOG:
            print(f'{nq.key:20} {nq.question}')
        return 0
    if not args.question:
        ap.error('a question is required (or pass --list)')

    question = ' '.join(args.question)
    try:
        backend = open_backend(args.mode)
    except McpError as e:
        print(f'error: {e}', file=sys.stderr)
        return 2

    try:
        answer = AuditAgent(backend).ask(question)
    finally:
        backend.close()

    if args.json:
        print(json.dumps(answer.to_dict(), indent=2, default=str))
        return 0

    print(f'[{answer.mode} / {answer.engine}] {question}')
    print()
    print(answer.answer)
    for q in answer.queries:
        print()
        print(f'  SQL   {" ".join(q.sql.split())}')
        print(f'  rows  {len(q.rows)}')
        for row in q.rows[:8]:
            print(f'        {json.dumps(row, default=str)}')
        if len(q.rows) > 8:
            print(f'        ... {len(q.rows) - 8} more')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
