"""AXIOM :: the adapter — the guarantee, behind an agent someone already wrote.

Everything else in this repo is a system you adopt wholesale: AXIOM's worker claims from
AXIOM's queue and calls AXIOM's provider. That is the wrong shape for the question a
reader actually has — *I already have an agent; what do I do?* — so this module is the
smallest surface that answers it. One decorator, on the function that moves money:

    @guard(action='refund', key='order_id', amount='amount_cents')
    def issue_refund(order_id, amount_cents, idempotency_key):
        return stripe.Refund.create(charge=order_id, amount=amount_cents,
                                    idempotency_key=idempotency_key)

`issue_refund` stays an ordinary callable — same name, same signature, same return value,
readable by someone who has never heard of AXIOM. What changes is what happens *around*
the call: the five protocols of axiom/tasks.py, unmodified, in order.

    CLAIM     one durable row per real-world act, fenced             1 txn
    PREPARE   the receipt commits BEFORE the call goes out           1 txn
    DISPATCH  your function body                                     NO txn
    SETTLE    the outcome AND the memory of it, co-committed         1 txn, fused
    RECOVER   a later call reads the receipt, recalls what happened
              the last time an agent died here, and decides          1 txn

THE ONE THING THAT MATTERS: WHERE THE KEY COMES FROM
====================================================
The whole guarantee rests on one property: **the idempotency key must survive the crash**.
A process that dies mid-call and restarts has to arrive at the SAME key, or the provider
sees a brand-new request and the $300 goes out twice.

AXIOM's key is a GENERATED column, sha256 over (tenant_id, task_id, step_name, step_seq) —
no code path can mint one at call time. But that only moves the question one hop: what
makes `task_id` stable across a restart? The unique index on (tenant_id, dedupe_key). So
the entire chain bottoms out on the dedupe key, and in an adapter the dedupe key has to be
derived from the CALLER'S ARGUMENTS. That is the one thing an integration can get wrong,
and getting it wrong is silent — a uuid4() or a timestamp in the key produces a system
that passes every test, demos beautifully, and double-refunds exactly once, in production,
on the day a container is OOM-killed.

So `key=` is REQUIRED, it names parameters rather than computing anything, and it is
checked twice:

  * at DECORATION time — every named parameter must exist in the signature, so a typo is
    an exception at import, not a duplicate charge at 3am;
  * at CALL time — every key value must be a stable scalar. A dict, a list, an object, a
    float, a None, an empty string: all refused, loudly, BEFORE any row is written and
    long before anything leaves the process.

Non-key arguments are fingerprinted too. If the same key arrives with a different
`amount`, that is not a retry — it is a new intent wearing an old identity, and it is a
hard stop (the adapter-level cousin of crash window W7). Other arguments drifting only
warn, because a changed `timeout=` is not a changed act.

WHAT THIS DOES NOT DO, STATED HERE SO NOBODY HAS TO INFER IT
===========================================================
1. It does not stop your function from running twice. It cannot: your function is where
   the network call lives. What it guarantees is that the second run carries the SAME
   idempotency key, so the provider replays instead of re-acting, and that you can prove
   afterwards which happened. "Effectively-once via idempotency receipts", never
   "exactly-once".
2. It requires your provider to honour idempotency keys. Against a provider that ignores
   them, AXIOM narrows the window to "we know exactly what we sent and when" and no
   further. That is real, but it is not the same promise.
3. It does not heartbeat. A guarded call that runs longer than AXIOM_LEASE_SECONDS can
   have its task claimed by another worker; your settle is then refused with LeaseLost
   (the fence working correctly) and the receipt stays live for that other worker to
   recover. Safe, but surprising if you have not read this paragraph.
4. It is synchronous. `db.tx()` is psycopg and psycopg is blocking, so decorating an
   `async def` is refused at decoration time rather than quietly blocking an event loop.
"""

from __future__ import annotations

import atexit
import functools
import hashlib
import inspect
import json
import os
import socket
import threading
import uuid
import warnings
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from . import db, embeddings, policy as policy_mod, tasks
from .models import AttemptState, Outcome, TaskState
from .tasks import BudgetExceeded, LeaseLost

try:
    # The general authority model (db/004_risk.sql): measurements, grants, reversibility.
    # Imported optionally so this module still works against a build that predates it —
    # a guard whose only authority axis is money is degraded, not broken.
    from . import risk as risk_mod
except ImportError:                                            # pragma: no cover
    risk_mod = None

__all__ = [
    'guard', 'bind', 'binding', 'open_mission', 'idempotency_key', 'receipt', 'shutdown',
    'GuardedCall', 'Binding',
    'AdapterError', 'NotBound', 'UnstableKey', 'IntentChanged', 'ApprovalRequired',
    'ActionInFlight', 'ActionRefused',
    'KeyUnusedWarning', 'IntentDriftWarning',
]


# ========================================================================== ERRORS

class AdapterError(RuntimeError):
    """Base for everything this module refuses to do."""


class NotBound(AdapterError):
    """No tenant/mission/policy is bound on this thread. See bind()."""


class UnstableKey(AdapterError, ValueError):
    """The identity of this act cannot be derived from the arguments given.

    Raised at DECORATION time for a name that is not in the signature, and at CALL time
    for a value that would not survive a restart. Both are hard errors on purpose: an
    unstable key does not fail, it double-charges, and it does so only under a crash —
    which is to say, only in production.
    """


class IntentChanged(AdapterError):
    """The same identity key arrived with a different amount.

    Same key + different intent is not a retry. The engine catches this at the request
    body (tasks.verify_fingerprint, crash window W7); this catches it one layer earlier,
    at the caller's arguments, where the mistake is actually made.
    """


class ApprovalRequired(AdapterError):
    """Procedural memory refused to let the machine act unattended.

    Carries the approval id so a caller can route it to whatever their humans use. The
    task is parked in AWAITING_APPROVAL with its lease released — nothing is in flight,
    nothing is held, and calling the guarded function again after a human approves
    resumes the same act under the same identity.
    """

    def __init__(self, message: str, *, approval_id: uuid.UUID, task_id: uuid.UUID):
        super().__init__(message)
        self.approval_id = approval_id
        self.task_id = task_id


class ActionInFlight(AdapterError):
    """Another holder has the fence on this exact act right now.

    Not a failure — it is the system declining to run the same irreversible act twice
    concurrently. Retry after the lease lapses, or leave it to whoever holds it.
    """


class ActionRefused(AdapterError):
    """This act reached a terminal state that is not success: dead-lettered, cancelled,
    or escalated by recovery because comparable recoveries ended badly."""

    def __init__(self, message: str, *, task_id: uuid.UUID, state: str,
                 reason: str | None = None):
        super().__init__(message)
        self.task_id = task_id
        self.state = state
        self.reason = reason


class KeyUnusedWarning(UserWarning):
    """AXIOM prepared an idempotency key and the guarded function never touched it.

    A warning rather than an error because a legitimately non-idempotent tool exists
    (a read, a log write). But if the function moves money, this warning IS the bug:
    a provider cannot dedupe a call that does not carry the key.
    """


class IntentDriftWarning(UserWarning):
    """Same identity key, same amount, different other arguments."""


# ========================================================================= BINDING

@dataclass(frozen=True)
class Binding:
    """Which tenant, which mission's budget, which procedural memory governs.

    A frozen dataclass in a ContextVar rather than module globals: agent processes are
    routinely multi-tenant (one server, many customers) and a per-thread/per-task binding
    is the only version of this that is not a cross-tenant leak waiting to happen.
    """
    tenant_id: uuid.UUID
    mission_id: uuid.UUID
    policy_id: str
    actor: str = 'system:adapter'


_bound: ContextVar[Binding | None] = ContextVar('axiom_binding', default=None)

# The last bind() anywhere in this process, used ONLY when the calling context has none.
#
# A plain threading.Thread starts with an EMPTY context — it does not inherit the one that
# created it (asyncio tasks and anyio's threadpool do; raw threads do not). Without this
# fallback, an agent that binds at startup and then fans work out over a ThreadPoolExecutor
# would get NotBound from every worker, which is a baffling error for a correct program.
#
# The cost, stated so nobody discovers it the hard way: in a MULTI-TENANT process that
# binds per request, a thread you spawn yourself sees whichever tenant bound most recently
# — which may not be yours. If you spawn threads and serve more than one tenant, read
# binding() in the parent and bind() it again inside the thread.
_default_binding: Binding | None = None


class _BindingScope:
    """What bind() hands back.

    bind() takes effect IMMEDIATELY — it is a statement, not a promise — and the object
    it returns exists so the binding can also be scoped:

        bind(...)                       # from here on, in this context
        with bind(...):  ...            # ... and undone at the end of the block
    """

    def __init__(self, token, previous_default: 'Binding | None') -> None:
        self._token = token
        self._previous_default = previous_default

    def __enter__(self) -> Binding:
        return _bound.get()

    def __exit__(self, *exc) -> bool:
        global _default_binding
        _bound.reset(self._token)
        _default_binding = self._previous_default
        return False


def bind(*, tenant_id: uuid.UUID | str, mission_id: uuid.UUID | str,
         policy_id: str, actor: str = 'system:adapter') -> _BindingScope:
    """Point the guards at a tenant, a mission budget, and a policy.

    `policy_id` has no default. The engine's own default is 'refund_authority', and
    inheriting it here would quietly make every integration a refund integration — the
    exact coupling this module exists to disprove.
    """
    if not policy_id:
        raise NotBound('policy_id is required: an act with no procedural memory '
                       'governing it is an unauthorized act')
    global _default_binding
    b = Binding(tenant_id=uuid.UUID(str(tenant_id)), mission_id=uuid.UUID(str(mission_id)),
                policy_id=policy_id, actor=actor)
    previous, _default_binding = _default_binding, b
    return _BindingScope(_bound.set(b), previous)


def binding() -> Binding:
    b = _bound.get() or _default_binding
    if b is None:
        raise NotBound(
            'no AXIOM binding on this context. Call '
            'axiom.adapter.bind(tenant_id=..., mission_id=..., policy_id=...) first.')
    return b


def open_mission(*, tenant_id: uuid.UUID | str, title: str, goal: str,
                 budget_cents: int, policy_id: str, created_by: str) -> uuid.UUID:
    """Create a mission and bind to it. Convenience for a process that owns its run.

    The budget is not decoration: PREPARE debits it in the same transaction that mints
    the key, under a CHECK constraint, so it is a hard ceiling on how much money this
    agent can move before a human raises it — the blast radius, in dollars, written down.
    """
    mid = db.tx(lambda cur: tasks.create_mission(
        cur, tenant_id=uuid.UUID(str(tenant_id)), title=title, goal=goal,
        budget_cents=budget_cents, created_by=created_by))
    bind(tenant_id=tenant_id, mission_id=mid, policy_id=policy_id, actor=created_by)
    return mid


# =========================================================================== AGENT

_agent_lock = threading.Lock()
_agent_id: uuid.UUID | None = None
_agent_ref: str | None = None


def _agent() -> uuid.UUID:
    """This process's row in the worker pool, registered once, lazily.

    A guarded call needs an agent id because that is what the fence is checked against —
    `lease_owner` is an agent, and every write after the claim re-reads it. Registering
    per process rather than per call keeps the pool table meaningful: one row per thing
    that can die, which is what an operator wants to look at during an incident.
    """
    global _agent_id, _agent_ref
    with _agent_lock:
        if _agent_id is None:
            _agent_ref = os.environ.get('AXIOM_ADAPTER_REF') or (
                f'adapter-{socket.gethostname().split(".")[0]}-{os.getpid()}')
            _agent_id = db.tx(lambda cur: tasks.register_agent(
                cur, worker_ref=_agent_ref, shards=(), kind='adapter',
                build_sha=os.environ.get('AXIOM_BUILD_SHA')))
        return _agent_id


def shutdown() -> None:
    """Mark this process's agent DEAD. Idempotent; also runs at exit.

    Not correctness — the fence is what makes concurrent writes safe and it does not care
    whether we said goodbye. It is hygiene: an ALIVE agent row with a fresh heartbeat is
    how an operator (and tests/conftest.py) decide whether someone is holding the queue.
    """
    global _agent_id
    with _agent_lock:
        if _agent_id is None:
            return
        aid, _agent_id = _agent_id, None
    try:
        db.tx(lambda cur: tasks.stop_agent(cur, agent_id=aid))
    except Exception:                       # a shutdown that raises is worse than useless
        pass


atexit.register(shutdown)


# =============================================================== KEY DERIVATION

def _render(name: str, value: Any) -> str:
    """One key component, as text. Raises rather than guesses.

    str / int / bool / UUID and nothing else. Everything admitted here has one obvious
    textual form that is identical in every process, on every machine, in every Python
    version — which is the entire requirement, because the value has to reproduce
    byte-for-byte after a restart.

    float is deliberately absent. Not because repr() drifts (it has not since 3.1) but
    because a float in an identity key is almost always money or a clock, and both are
    wrong: money belongs in integer cents, and a clock is precisely the kind of key that
    looks stable right up until the process restarts one millisecond later.
    """
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        if not value.strip():
            raise UnstableKey(
                f'key parameter {name!r} is empty. An empty identity is shared by every '
                f'call that forgot to pass one, which would give them all one receipt.')
        return value
    if value is None:
        raise UnstableKey(
            f'key parameter {name!r} is None. None is not an identity: two unrelated '
            f'calls that both omitted it would collapse onto the same idempotency key '
            f'and the second act would be silently suppressed.')
    raise UnstableKey(
        f'key parameter {name!r} is a {type(value).__name__}, which has no stable text '
        f'form across processes. The idempotency key has to be reproducible after a '
        f'crash; derive one from {type(value).__name__} yourself and pass that instead.')


def _stable_repr(value: Any, depth: int = 0) -> str:
    """A best-effort stable rendering for the ARGUMENT FINGERPRINT (not the key).

    Weaker than _render on purpose: this one never raises, because it has to cope with
    whatever a real tool signature carries — an http client, a logger, a model handle.
    An unrenderable object contributes only its type name, which is stable, so the
    fingerprint stays useful for detecting "same key, different call" without inventing
    false differences every time a client object is reconstructed.
    """
    if isinstance(value, bool) or value is None:
        return repr(value)
    if isinstance(value, (str, int, float, uuid.UUID)):
        return f'{type(value).__name__}:{value}'
    if depth < 3 and isinstance(value, (list, tuple)):
        return '[' + ','.join(_stable_repr(v, depth + 1) for v in value) + ']'
    if depth < 3 and isinstance(value, dict):
        return '{' + ','.join(f'{k}={_stable_repr(value[k], depth + 1)}'
                              for k in sorted(map(str, value))) + '}'
    return f'<{type(value).__name__}>'


def _dedupe_key(action: str, parts: dict[str, Any]) -> str:
    """'refund|order_id=ORD-1042' — readable first, hashed only when it has to be.

    Readable matters more than it sounds: this string is the business identity of the act
    in Mission Control, in the journal, and in every incident conversation about it. A
    bare sha256 would be correct and useless. The hash fallback exists only so a caller
    with a long composite key cannot blow past what is comfortable in a UI.
    """
    rendered = '|'.join(f'{n}={_render(n, v)}' for n, v in parts.items())
    raw = f'{action}|{rendered}'
    if len(raw) <= 180 and '\n' not in raw:
        return raw
    return f'{action}|sha256:{hashlib.sha256(raw.encode()).hexdigest()[:40]}'


# ============================================================== THE CALL CONTEXT

@dataclass
class _Live:
    """The receipt that authorizes the call currently on this stack."""
    receipt: tasks.Receipt
    used: bool = False


_live: ContextVar[_Live | None] = ContextVar('axiom_live', default=None)


def idempotency_key() -> str:
    """The key this call is authorized under. Callable from inside a guarded function.

    The alternative to reading it here is accepting an `idempotency_key` parameter, which
    the guard injects automatically when the signature has one. Both are fine; what is not
    fine is neither, and the guard warns when the call finishes without either happening.
    """
    live = _live.get()
    if live is None:
        raise AdapterError('idempotency_key() is only meaningful inside a @guard-ed call')
    live.used = True
    return live.receipt.idempotency_key


def receipt() -> tasks.Receipt:
    """The whole receipt, for a tool that wants the amount or the step it was minted for."""
    live = _live.get()
    if live is None:
        raise AdapterError('receipt() is only meaningful inside a @guard-ed call')
    live.used = True
    return live.receipt


# ================================================================== THE RESULT

@dataclass(frozen=True)
class GuardedCall:
    """What `fn.axiom(...)` returns: the value, plus what the engine did to get it.

    The plain call returns `.value` and nothing else, because a tool that suddenly
    returns a wrapper object is no longer droppable into an agent someone already wrote.
    """
    value: Any
    task_id: uuid.UUID
    dedupe_key: str
    idempotency_key: str | None
    recovered: bool          # this call came through RECOVER; a prior attempt may have acted
    already_settled: bool    # returned from the durable record; the function was NOT called

    @property
    def called(self) -> bool:
        return not self.already_settled


# ======================================================================== THE GUARD

@dataclass(frozen=True)
class _Spec:
    action: str
    key_names: tuple[str, ...]
    amount_param: str | None
    amount_fixed: int | None
    risk: Any                       # None | str label | risk.Risk | (arguments) -> Risk
    provider: str
    operation: str
    currency: str
    max_attempts: int
    key_param: str
    inject_key: bool
    retry_backoff_seconds: int
    signature: inspect.Signature


def guard(
    *,
    action: str,
    key: str | Sequence[str],
    amount: str | int | None = None,
    risk: Any = None,
    provider: str = 'external',
    operation: str | None = None,
    currency: str = 'USD',
    max_attempts: int = 5,
    key_param: str = 'idempotency_key',
    retry_backoff_seconds: int = 0,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Put AXIOM's guarantee behind one function.

    action    the step name. Becomes axiom_task.task_type and the receipt's step_name,
              so two different acts on the same object get two different receipts.
    key       parameter name(s) whose values identify the ACT IN THE WORLD. Required.
              Read the module docstring before choosing one; it is the only thing here
              that cannot be fixed later.
    amount    parameter name holding integer cents, or a fixed int, or None. This is what
              the policy's authority ceiling is checked against and what the mission
              budget is debited by. None means "not denominated in money" — see `risk`.
    risk      what kind of irreversibility this is, for the acts money cannot describe.
              Three accepted forms, strongest first:
                risk.Risk(...)          a descriptor decided by Policy.decide() — the
                                        engine's general authority model, where an
                                        ungoverned unit is a refusal, not a default
                lambda args: Risk(...)  the same, measured per call, because magnitude
                                        usually depends on the arguments
                'data_deletion'         a label, matched against escalate_risks /
                                        auto_risks in the policy body. The shortcut for
                                        an act nobody has written a measurement for; see
                                        _risk_authorized for exactly how much weaker it is
    provider / operation   recorded on the receipt for the reconciliation worklist.
    key_param when the wrapped signature has a parameter of this name, the guard passes
              the idempotency key into it. Otherwise call idempotency_key() in the body.
    """
    key_names = (key,) if isinstance(key, str) else tuple(key)
    if not key_names:
        raise UnstableKey('key= is required: without it there is nothing to derive a '
                          'crash-stable idempotency key from')

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        if inspect.iscoroutinefunction(fn):
            raise AdapterError(
                f'{fn.__qualname__} is async. AXIOM\'s transactions are psycopg, which is '
                f'blocking, so guarding a coroutine would stall the event loop inside '
                f'every commit. Wrap the sync function with asyncio.to_thread instead.')

        sig = inspect.signature(fn)
        params = sig.parameters

        # DECORATION-TIME check. A key naming a parameter that does not exist is a typo,
        # and a typo here is a double charge under a crash. Fail at import.
        for name in key_names:
            p = params.get(name)
            if p is None:
                raise UnstableKey(
                    f'@guard(key={name!r}) but {fn.__qualname__}{sig} has no such '
                    f'parameter. The key must name real arguments — the guard reads their '
                    f'values, it does not invent them.')
            if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                raise UnstableKey(
                    f'@guard(key={name!r}) names *args/**kwargs on {fn.__qualname__}. '
                    f'A key has to be a named parameter so its presence is checkable.')
        if isinstance(amount, str) and amount not in params:
            raise UnstableKey(
                f'@guard(amount={amount!r}) but {fn.__qualname__}{sig} has no such '
                f'parameter')
        if (amount is not None and not isinstance(amount, (str, int))) or isinstance(amount, bool):
            raise UnstableKey(
                f'@guard(amount={amount!r}) must be a parameter name, integer cents, or '
                f'None — got {type(amount).__name__}')

        spec = _Spec(
            action=action, key_names=key_names,
            amount_param=amount if isinstance(amount, str) else None,
            amount_fixed=amount if isinstance(amount, int) and not isinstance(amount, bool)
            else None,
            risk=risk, provider=provider, operation=operation or fn.__name__,
            currency=currency, max_attempts=max_attempts, key_param=key_param,
            # A tool that takes **kwargs gets the key too — that is the shape most
            # framework tool wrappers end up with, and refusing to fill it there would
            # push every such integration onto the easier-to-forget idempotency_key() call.
            inject_key=(key_param in params
                        or any(p.kind is inspect.Parameter.VAR_KEYWORD
                               for p in params.values())),
            retry_backoff_seconds=retry_backoff_seconds, signature=sig,
        )

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            return _invoke(spec, fn, args, kwargs).value

        def _rich(*args, **kwargs) -> GuardedCall:
            return _invoke(spec, fn, args, kwargs)

        def _key(*args, **kwargs) -> str:
            return _bind_args(spec, args, kwargs)[0]

        # The ordinary callable keeps the ordinary name. The two extras hang off it for
        # the caller who wants the receipt, or wants to see the derived identity without
        # doing anything — `fn.axiom_key(...)` is the line to put in a code review.
        wrapper.axiom = _rich                 # type: ignore[attr-defined]
        wrapper.axiom_key = _key              # type: ignore[attr-defined]
        wrapper.axiom_spec = spec             # type: ignore[attr-defined]
        wrapper.__wrapped__ = fn
        return wrapper

    return decorate


# =================================================================== THE MACHINERY

def _bind_args(spec: _Spec, args: tuple,
               kwargs: dict) -> tuple[str, int | None, str, dict, dict]:
    """(dedupe_key, amount_cents, args_fingerprint, key_values, all_arguments) — and
    nothing has been written yet when this raises, which is the point of doing it first."""
    try:
        bound = spec.signature.bind(*args, **kwargs)
    except TypeError:
        if not spec.inject_key:
            raise
        # The tool declares `idempotency_key` with no default — the most honest signature
        # it can have, since the parameter really is mandatory. The CALLER still must not
        # supply it (that is the guard's job, and a caller-supplied key is the bug this
        # module exists to prevent), so binding it here with a placeholder is what lets
        # `def issue_refund(order_id, amount_cents, idempotency_key)` be a legal tool.
        # The placeholder is excluded from the fingerprint below and replaced with the
        # real key at dispatch.
        bound = spec.signature.bind(*args, **{**kwargs, spec.key_param: ''})
    bound.apply_defaults()

    key_values = {n: bound.arguments.get(n) for n in spec.key_names}
    dedupe = _dedupe_key(spec.action, key_values)

    amount_cents: int | None = spec.amount_fixed
    if spec.amount_param is not None:
        raw = bound.arguments.get(spec.amount_param)
        if raw is not None and (not isinstance(raw, int) or isinstance(raw, bool)):
            raise UnstableKey(
                f'amount parameter {spec.amount_param!r} is a {type(raw).__name__}; the '
                f'policy ceiling and the mission budget are integer cents')
        amount_cents = raw

    # Fingerprint every argument EXCEPT the key AXIOM injects (which differs by
    # construction between the first attempt and a recovery, and would otherwise make
    # every recovery look like a changed intent).
    fp_src = {k: _stable_repr(v) for k, v in bound.arguments.items() if k != spec.key_param}
    fingerprint = hashlib.sha256(
        json.dumps(fp_src, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return dedupe, amount_cents, fingerprint, key_values, dict(bound.arguments)


def _json_safe(value: Any) -> Any:
    """Whatever settle() can store. Never raises — losing the settle because a tool
    returned an SDK object would mean an effect that happened and was not recorded,
    which is strictly the worst outcome this system has."""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return {'unserializable': True, 'repr': repr(value)[:1000],
                'type': type(value).__name__}


def _amount_str(cents: int | None, currency: str) -> str:
    """For the memory text, which is embedded and later recalled — so it is worth being
    readable rather than a raw integer no future recall query will ever match."""
    return 'no monetary amount' if cents is None else f'{cents / 100:.2f} {currency}'


def _provider_ref(value: Any) -> str | None:
    """Pull the provider's own reference out of the return value if it is obvious.

    A Stripe refund dict has `id`; most SDKs land on one of these three. Getting it onto
    the receipt is what makes `re_...` show up in the reconciliation worklist and in the
    memory of the act, rather than only in your logs.
    """
    if isinstance(value, dict):
        for field in ('provider_ref', 'id', 'ref'):
            v = value.get(field)
            if isinstance(v, str) and v:
                return v[:200]
    return None


def _ensure_task(b: Binding, spec: _Spec, dedupe: str, payload: dict) -> dict:
    """One durable row per real-world act. Idempotent by the unique index, not by a read.

    ON CONFLICT DO NOTHING means the second process to arrive gets None and then READS the
    row the first one wrote — so two agents starting the same act at the same instant
    converge on ONE task id and therefore one idempotency key, with no lock and no
    coordination beyond the index.
    """
    def _apply(cur):
        tasks.enqueue(
            cur, tenant_id=b.tenant_id, mission_id=b.mission_id, task_type=spec.action,
            dedupe_key=dedupe, payload=payload, max_attempts=spec.max_attempts,
            actor=b.actor)
        cur.execute("""
            SELECT id, state, result, last_error, payload, attempt, max_attempts
            FROM axiom_task WHERE tenant_id = %s AND dedupe_key = %s
        """, (str(b.tenant_id), dedupe))
        return cur.fetchone()
    return db.tx(_apply)


def _check_intent(spec: _Spec, row: dict, amount_cents: int | None,
                  fingerprint: str) -> None:
    """Same identity, different call. The adapter-level cousin of crash window W7.

    A changed AMOUNT is a hard stop: refunding $30 and then $300 under one identity means
    one of the two will never happen and the caller will not be told which. Anything else
    only warns — a changed `timeout=` is not a changed act, and an adapter that refuses to
    run because a logger was reconstructed is an adapter nobody keeps.
    """
    prior = row.get('payload') or {}
    if prior.get('axiom_adapter') is not True:
        return                                   # not ours; a worker or a test made it
    if prior.get('amount_cents') != amount_cents:
        raise IntentChanged(
            f'{row["dedupe_key"]!r} was created for '
            f'amount_cents={prior.get("amount_cents")} and this call passes '
            f'{amount_cents} under the same key. Same key + different intent is not a '
            f'retry — use a key that distinguishes the two acts.')
    if prior.get('args_fingerprint') != fingerprint:
        warnings.warn(
            f'guarded call {spec.action} reached the same identity with different '
            f'non-key arguments; proceeding under the ORIGINAL intent recorded on task '
            f'{row["id"]}', IntentDriftWarning, stacklevel=4)


def _recorded(row: dict) -> GuardedCall:
    """A completed act, answered from the durable record without calling anything.

    This is the single most useful thing the guard does on an ordinary day: the crash-safe
    path is rare, but "this already happened, here is what it returned" is every retry,
    every duplicate webhook, and every impatient user double-clicking a button.
    """
    result = row.get('result') or {}
    return GuardedCall(
        value=result.get('value'), task_id=row['id'], dedupe_key=row['dedupe_key'],
        idempotency_key=result.get('idempotency_key'), recovered=False,
        already_settled=True)


def _release(task: tasks.Claimed, agent_id: uuid.UUID, error: str, backoff: int) -> None:
    """Hand the act back after an ambiguous failure — WITHOUT touching the receipt.

    `receipt=None` is the entire point of this function and it is not an oversight.
    When your tool raises, AXIOM does not know whether the call landed: a ConnectionError
    on the response is indistinguishable from one on the request. Marking the receipt
    FAILED_RETRYABLE here would destroy the only evidence that an effect might exist, and
    the next attempt would mint a NEW key and cause a second one. So the receipt stays
    LIVE, the task goes back to READY, and the next call recovers into it and re-sends
    under the same key — which the provider dedupes.

    The worker does mark the receipt failed in the equivalent place, and that is correct
    THERE: it holds a structured ProviderError, which is the provider stating that it
    did not act. An arbitrary exception from someone else's tool states nothing.
    """
    try:
        db.tx(lambda cur: tasks.fail_retryable(
            cur, task=task, agent_id=agent_id, receipt=None,
            error=error, backoff_seconds=backoff))
    except LeaseLost:
        # We lost the fence while the tool was running, so someone else already owns the
        # recovery. Nothing to release, and raising here would mask the caller's real
        # exception with a bookkeeping one.
        pass


def _risk_authorized(pol: policy_mod.Policy, risk: Any) -> tuple[bool, str]:
    """Does procedural memory let the machine take THIS KIND of act unattended?

    Two forms, and the first one is the real one:

    A `risk.Risk` DESCRIPTOR — measurements, magnitudes, reversibility — is handed
    straight to `Policy.decide()`. That is the engine's own general authority model
    (db/004_risk.sql): grants are stored per policy version, an ungoverned unit is a
    refusal rather than a default, and the reason comes back naming the axis that failed.
    The adapter adds nothing to it and must not, because the decision belongs to
    procedural memory, not to a decorator.

    A plain STRING is a shortcut for the common case where nobody has written a
    measurement for this act yet. It is checked against a vocabulary in the policy BODY —
    versioned, hashed and signable JSONB, so still procedural memory:

        {"escalate_risks": ["data_deletion"]}   deny-list: named labels need a human
        {"auto_risks": ["money_movement"]}      allow-list: anything unnamed needs one

    Stated plainly: a policy declaring NEITHER list authorizes every label. That
    default-open is the honest weakness of the shortcut, and it is why the shortcut is a
    shortcut — a Risk descriptor is deny-by-default, enforced by the same function
    tasks.prepare() uses. Use `auto_risks` if you want the closed form without writing a
    measurement.
    """
    if risk is None:
        return True, ''

    if risk_mod is not None and isinstance(risk, risk_mod.Risk):
        decide = getattr(pol, 'decide', None)
        if decide is None:                                     # pragma: no cover
            raise AdapterError(
                'this build of axiom.policy has no general authority model; a Risk '
                'descriptor cannot be decided. Apply db/004_risk.sql, or use a label.')
        d = decide(risk)
        return d.authorized, d.reason

    body = pol.body or {}
    escalate = body.get('escalate_risks')
    if isinstance(escalate, (list, tuple)) and risk in escalate:
        return False, (f'policy {pol.policy_id} v{pol.version} escalates {risk!r} '
                       f'to a human')
    allow = body.get('auto_risks')
    if isinstance(allow, (list, tuple)) and risk not in allow:
        return False, (f'policy {pol.policy_id} v{pol.version} authorizes '
                       f'{", ".join(allow) or "nothing"} unattended, not {risk!r}')
    return True, ''


def _risk_gate(b: Binding, spec: _Spec, claimed: tasks.Claimed, agent_id: uuid.UUID,
               amount_cents: int | None, key_values: dict,
               risk: Any) -> uuid.UUID | None:
    """Park on a human when the policy will not authorize this RISK unattended.

    Runs before PREPARE and consumes a human's decision token the same way prepare() does,
    because an approval is a capability rather than a standing permission — otherwise an
    approved task would re-park on every claim forever, since the policy never moves.

    The token is consumed ONLY when this gate is the thing that needs authorizing. If the
    amount also exceeds the money ceiling, prepare() needs its own token; that act
    requires two human decisions, which is the safe direction to be wrong in.
    """
    if risk is None:
        return None

    def _apply(cur):
        pol = (policy_mod.at_version(cur, tenant_id=b.tenant_id, policy_id=b.policy_id,
                                     version=claimed.policy_version)
               if claimed.policy_version is not None
               else policy_mod.active(cur, tenant_id=b.tenant_id, policy_id=b.policy_id))
        authorized, reason = _risk_authorized(pol, risk)
        if authorized:
            return None
        if tasks.consume_approval(cur, tenant_id=b.tenant_id, task_id=claimed.id,
                                  step_name=spec.action) is not None:
            return None                       # a human already ruled on exactly this act
        return tasks.request_approval(
            cur, task=claimed, agent_id=agent_id, step_name=spec.action, reason=reason,
            proposed_action={'action': spec.action, 'risk': str(risk), 'key': key_values,
                             'operation': spec.operation},
            proposed_amount_cents=amount_cents)
    return db.tx(_apply)


def _terminal_or_none(row: dict, spec: _Spec) -> GuardedCall | None:
    """Answer from the record when the act is already over. Raises when it ended badly."""
    state = TaskState(row['state'])
    if state == TaskState.SUCCEEDED:
        return _recorded(row)
    if state in (TaskState.DEAD_LETTER, TaskState.CANCELLED, TaskState.FAILED):
        raise ActionRefused(
            f'{spec.action} on {row["dedupe_key"]} is {state}: {row.get("last_error")}',
            task_id=row['id'], state=str(state), reason=row.get('last_error'))
    return None


def _pending_approval(b: Binding, task_id: uuid.UUID, step: str) -> uuid.UUID | None:
    def _q(cur):
        cur.execute("""
            SELECT id FROM axiom_approval
            WHERE tenant_id = %s AND task_id = %s AND step_name = %s AND state = 'PENDING'
        """, (str(b.tenant_id), str(task_id), step))
        row = cur.fetchone()
        return row['id'] if row else None
    return db.tx(_q, readonly=True)


def _invoke(spec: _Spec, fn: Callable[..., Any], args: tuple, kwargs: dict) -> GuardedCall:
    """CLAIM -> (RECOVER | PREPARE) -> DISPATCH -> SETTLE, around someone else's function."""
    b = binding()
    dedupe, amount_cents, fingerprint, key_values, arguments = _bind_args(spec, args, kwargs)

    # A CALLABLE risk is measured per call, because magnitude usually is: "delete one
    # workspace" and "delete forty thousand records" are the same function. The callable
    # sees the bound arguments and nothing else, so the measurement is reproducible from
    # the audit trail rather than being something the agent got to assert about itself.
    risk = spec.risk(arguments) if callable(spec.risk) else spec.risk

    payload = {
        'axiom_adapter': True, 'action': spec.action, 'key': key_values,
        'amount_cents': amount_cents, 'args_fingerprint': fingerprint,
        'risk': str(risk) if risk is not None else None,
        'operation': spec.operation, 'function': fn.__qualname__,
    }
    row = _ensure_task(b, spec, dedupe, payload)
    row['dedupe_key'] = dedupe
    _check_intent(spec, row, amount_cents, fingerprint)

    done = _terminal_or_none(row, spec)
    if done is not None:
        return done

    agent_id = _agent()
    task_id = row['id']

    claimed = db.tx(lambda cur: tasks.claim(cur, agent_id=agent_id, task_id=task_id))
    if claimed is None:
        # Nothing claimable. Either somebody else holds the fence, or the act finished
        # or was refused between our read and now. Say which, precisely — "busy" and
        # "already done" demand opposite responses from the caller.
        fresh = db.tx(lambda cur: tasks.get_task(
            cur, tenant_id=b.tenant_id, task_id=task_id))
        fresh = dict(fresh or row)
        fresh['dedupe_key'] = dedupe
        done = _terminal_or_none(fresh, spec)
        if done is not None:
            return done
        if TaskState(fresh['state']) == TaskState.AWAITING_APPROVAL:
            raise ApprovalRequired(
                f'{spec.action} on {dedupe} is parked on a human decision',
                approval_id=_pending_approval(b, task_id, spec.action), task_id=task_id)
        raise ActionInFlight(
            f'{spec.action} on {dedupe} is held by another agent (state '
            f'{fresh["state"]}, lease until {fresh.get("available_at")}). AXIOM is '
            f'declining to run the same irreversible act concurrently.')

    # RECOVER, whenever this act has been attempted before.
    #
    # `is_recovery` alone is not enough here, and that is the difference between the
    # adapter and the worker. The worker's process DIES, leaving ACTION_PREPARED; a
    # guarded call whose tool merely RAISED gets its task handed back to READY (the
    # schema forbids ACTION_PREPARED without a lease owner), and the live receipt is the
    # only surviving evidence. Keying recovery off `attempt > 0` catches both. When
    # nothing is outstanding, recover() says REPLAN and this falls through to PREPARE —
    # which is exactly right, because REPLAN means no effect can exist.
    rct: tasks.Receipt | None = None
    recovered = False
    if claimed.is_recovery or claimed.attempt > 0:
        situation = f'{spec.action} on {dedupe} interrupted after the receipt committed'
        plan = db.tx(lambda cur: tasks.recover(
            cur, task=claimed, agent_id=agent_id, step_name=spec.action,
            situation_embedding=embeddings.embed_list(situation)))
        if plan.action == 'ESCALATE':
            # Memory voting against an unattended re-dispatch is the one direction it is
            # allowed to vote. Honour it and stop.
            content = (f'recovery of {spec.action} on {dedupe} refused to re-dispatch '
                       f'unattended: {plan.rationale}')
            db.tx(lambda cur: tasks.dead_letter(
                cur, task=claimed, agent_id=agent_id, reason=plan.rationale,
                memory_content=content, memory_embedding=embeddings.embed_list(content)))
            raise ActionRefused(
                f'{spec.action} on {dedupe} escalated during recovery: {plan.rationale}',
                task_id=task_id, state=str(TaskState.DEAD_LETTER), reason=plan.rationale)
        if plan.action == 'RESEND':
            rct, recovered = plan.receipt, True

    if rct is None:
        # NO SEPARATE RISK GATE HERE.
        #
        # The adapter used to make its own authority decision before calling prepare().
        # Two problems, and the second is the serious one:
        #
        #   1. Two deciders, two vocabularies. The adapter checked a Risk descriptor while
        #      prepare() checked an int, so a @guard(risk=data.subjects=50) could pass one
        #      and fail the other.
        #   2. TWO CONSUMERS OF ONE SINGLE-USE TOKEN. The gate burned the human's approval
        #      and then prepare() tried to burn it again, found it spent, and parked the
        #      act a second time — so an approved action could never proceed. An approval
        #      is a capability; two independent readers of it is a bug by construction.
        #
        # prepare() now takes the risk descriptor, so it is the single place that decides
        # whether an irreversible act may happen unattended — which is where that decision
        # belongs anyway: inside the transaction that mints the receipt.
        #
        # The gate survives for the LABEL form only. A plain string like 'data_deletion'
        # is matched against a vocabulary in the policy body; prepare() knows nothing
        # about that, so something has to. Exactly one of the two fires per call, because
        # a guard declares either a descriptor or a label and never both — which is what
        # keeps the single-use approval token to a single consumer.
        if risk is not None and not (risk_mod is not None
                                     and isinstance(risk, risk_mod.Risk)):
            approval_id = _risk_gate(b, spec, claimed, agent_id, amount_cents,
                                     key_values, risk)
            if approval_id is not None:
                raise ApprovalRequired(
                    f'{spec.action} on {dedupe} needs a human: policy {b.policy_id} does '
                    f'not authorize this unattended ({risk})',
                    approval_id=approval_id, task_id=task_id)
        try:
            prepared = db.tx(lambda cur: tasks.prepare(
                cur, task=claimed, agent_id=agent_id, step_name=spec.action,
                provider_name=spec.provider, operation=spec.operation,
                request_body={'action': spec.action, 'key': key_values,
                              'amount_cents': amount_cents, 'currency': spec.currency},
                # Hand prepare() the risk descriptor the caller declared, so the policy
                # decides in the units the ACTION is measured in. Before prepare() took a
                # Risk, the adapter computed one, checked it itself, and then passed an
                # int — so a @guard(risk=data.subjects=50) still reached the authority
                # transaction as "50 cents", and a policy granting data.subjects refused
                # it. Two places deciding the same question in different vocabularies is
                # how they end up disagreeing.
                risk=risk if (risk_mod is not None
                              and isinstance(risk, risk_mod.Risk)) else None,
                amount_cents=amount_cents, currency=spec.currency,
                policy_id=b.policy_id))
        except BudgetExceeded:
            # The mission's hard ceiling refused. Nothing was minted and nothing was
            # sent; hand the act back so a human who raises the budget can re-run it.
            _release(claimed, agent_id, 'mission budget exhausted',
                     spec.retry_backoff_seconds)
            raise
        if prepared.parked:
            raise ApprovalRequired(
                f'{spec.action} on {dedupe} exceeds the authority of policy '
                f'{b.policy_id}; parked for a human',
                approval_id=prepared.approval_id, task_id=task_id)
        rct = prepared.receipt

    return _dispatch_and_settle(b, spec, fn, args, kwargs, claimed, agent_id, rct,
                                dedupe, recovered)


def _dispatch_and_settle(b: Binding, spec: _Spec, fn: Callable[..., Any], args: tuple,
                         kwargs: dict, claimed: tasks.Claimed, agent_id: uuid.UUID,
                         rct: tasks.Receipt, dedupe: str, recovered: bool) -> GuardedCall:
    """The only place someone else's code runs. Everything before it is reversible."""
    db.tx(lambda cur: tasks.mark_dispatched(cur, receipt=rct))

    call_kwargs = dict(kwargs)
    if spec.inject_key:
        call_kwargs[spec.key_param] = rct.idempotency_key

    live = _Live(receipt=rct, used=spec.inject_key)
    token = _live.set(live)
    try:
        value = fn(*args, **call_kwargs)
    except BaseException as e:                 # ProviderCrash is a BaseException
        # Ambiguous by construction: we cannot tell a failure before the effect from one
        # after it. Release the act, keep the receipt live, let the next call RECOVER.
        # A real SIGKILL skips this entirely and lands in exactly the same durable state,
        # a lease-expiry later — which is why the crash tests and this path agree.
        _release(claimed, agent_id, f'{type(e).__name__}: {e}'[:400],
                 spec.retry_backoff_seconds)
        raise
    finally:
        _live.reset(token)

    if not live.used:
        warnings.warn(
            f'{fn.__qualname__} finished without using the idempotency key AXIOM prepared '
            f'({rct.idempotency_key}). If this call has an external effect, the provider '
            f'cannot dedupe it and a recovery WILL cause a second one. Accept an '
            f'{spec.key_param!r} parameter or call axiom.adapter.idempotency_key().',
            KeyUnusedWarning, stacklevel=3)

    safe = _json_safe(value)
    ref = _provider_ref(value)
    content = (
        f'{spec.action} on {dedupe} for {_amount_str(rct.amount_cents, spec.currency)} '
        f'settled under key {rct.idempotency_key}'
        + ('; recovered after an interrupted attempt' if recovered else '')
        + (f'; provider ref {ref}' if ref else ''))

    db.tx(lambda cur: tasks.settle(
        cur, task=claimed, agent_id=agent_id, receipt=rct,
        outcome_state=AttemptState.SUCCEEDED, task_state=TaskState.SUCCEEDED,
        response_body=safe if isinstance(safe, dict) else {'value': safe},
        provider_ref=ref, http_status=None,
        memory_content=content, memory_embedding=embeddings.embed_list(content),
        memory_outcome=Outcome.RESOLVED,
        result={'value': safe, 'recovered': recovered,
                'idempotency_key': rct.idempotency_key}))

    return GuardedCall(value=value, task_id=claimed.id, dedupe_key=dedupe,
                       idempotency_key=rct.idempotency_key, recovered=recovered,
                       already_settled=False)
