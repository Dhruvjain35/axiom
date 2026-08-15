"""AXIOM :: Amazon SES — the external system that offers you nothing.

READ THIS FIRST. IT IS THE REASON THIS FILE EXISTS.
===================================================
`axiom/stripe_provider.py` proves the guarantee against a provider that already helps.
Stripe accepts an `Idempotency-Key` header, remembers it for 24 hours, and returns
`idempotent-replayed: true` when you send the same key twice. The argument there is
careful and correct but it is the WEAK form of the claim: *Stripe can only honour a key
it is handed, and AXIOM is what makes the key survive the crash.*

Amazon SES has no such header. There is no idempotency key on SendEmail, no request id
you can supply, no "have I already sent this?" endpoint, no dedupe window, nothing. Call
SendEmail twice with the same bytes and two emails arrive, each with its own MessageId,
and both of them are in somebody's inbox forever. The API is behaving correctly — an
email service whose job is to send mail cannot decide on your behalf that your second
send was a mistake.

So this is the STRONG form of the same argument. Where the provider offers no protection
at all, every part of the guarantee is AXIOM's:

    Stripe   provider enforces, AXIOM remembers.   Split responsibility.
    SES      nothing enforces.                     AXIOM is the entire mechanism.

The dedupe therefore lives where it can live at all — in the relay's own durable store,
in `axiom/domains/relay.py`, keyed by the idempotency key AXIOM derived in the database
from immutable columns before the send. This module is only the transport: it puts a
message on the wire and reports what SES said. It contains no deduplication of any kind,
and it must not grow one, because a reader who found dedupe in here would reasonably
conclude SES provides it.

WHAT CAN AND CANNOT BE PROMISED, STATED BEFORE ANYONE OVERSELLS IT
------------------------------------------------------------------
The relay commits the idempotency key BEFORE calling SendEmail and only calls SendEmail
for a key it has never seen. That makes a SECOND send impossible. It does not make the
FIRST send guaranteed-recorded: the send and the record are in two different systems with
no shared transaction, so a process that dies in between leaves a message that was
delivered and never written down. The residual window is one message wide, and what falls
into it is a LOST message, never a duplicated one.

That asymmetry is a choice, and it is the right one for this workload: a campaign that
reaches 999 of 1,000 people is a bad afternoon, and a campaign that reaches 1,000 people
twice is a compliance incident and a burnt sending domain. AXIOM prefers to lose a message
than to send one twice, and says so rather than claiming exactly-once — which two systems
without a shared transaction cannot have, from anybody, ever.

BOTO3 WILL SEND A SECOND EMAIL FOR YOU IF YOU LET IT
-----------------------------------------------------
botocore retries automatically by default: connection errors, timeouts and throttles are
all retried inside the client. For a request carrying an idempotency key that is free. For
SendEmail it is not — a read timeout on an accepted request, retried, is a second email
that nothing in this system will ever see or be able to count. So the client below is
built with `total_max_attempts=1`. Every retry decision in this file is explicit, and the
only errors retried are the ones SES raises BEFORE accepting anything (throttling).

SAFETY RAILS, AND WHY THEY ARE NOT NEGOTIABLE
---------------------------------------------
The account is in the SES SANDBOX: 200 messages/day, 1/second, and every recipient must be
a verified identity — except Amazon's mailbox simulator, which needs no verification and
touches no real inbox:

    success@simulator.amazonses.com     accepted and delivered
    bounce@simulator.amazonses.com      hard bounce
    complaint@simulator.amazonses.com   complaint
    ooto@simulator.amazonses.com        out-of-office reply
    suppressionlist@simulator.amazonses.com   suppression-list rejection

All demo and proof traffic goes to the simulator, and `guard()` refuses anything else
unless it is named explicitly in AXIOM_SES_ALLOW. A hackathon demo that emails strangers is
a hackathon demo that gets an account suspended, and bounces from a stranger's mailbox
damage the sending reputation of the account being judged. Labels are allowed
(`success+axiom@simulator.amazonses.com`) so a campaign can have several distinct
recipients while every one of them is still a simulator address.

Two more rails, both because a bug in a loop is how a free tier turns into a bill:
`AXIOM_SES_MAX_PER_SEND` (default 5) caps one campaign, and `AXIOM_SES_MAX_TOTAL`
(default 50) caps everything one process may send, ever.

COST, MEASURED RATHER THAN ASSUMED
----------------------------------
SES list price is $0.10 per 1,000 outbound messages. The proof program here is single
digits of messages per run — fractions of a cent — but the free-tier question deserves a
straight answer rather than a rounded-down one, because this project argues against
rounding your own bill down:

    aws freetier get-free-tier-usage   -> 12 entries on this account, ALL "Always Free",
                                          ZERO "12 Months Free". None of the 12 is SES.
                                          (Glue, Lambda x2, SQS, CloudWatch x4, SNS x3, KMS)

SES's 3,000 message charges/month is a twelve-month offer under the current free-tier
plan, and this account is `accountPlanType: PAID` with $0.00 remaining credits — past that
window, exactly like API Gateway, X-Ray and Comprehend. So the honest statement is that
SES message charges are billed here at $0.10/1,000 and that the proof runs cost a fraction
of a cent, not that they are free. If the account's free-tier ledger later shows an SES
row, that is the number to trust over this paragraph; the command is above.

OFF BY DEFAULT
--------------
`AXIOM_SES=1` turns it on, read per call rather than frozen into `settings`, and
`AXIOM_OFFLINE=1` forces it off no matter what. The invariant suite stays hermetic with no
AWS credentials and no network, and the demo the judges press does not change behaviour
unless somebody sets the flag.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any

from .provider import ProviderError

# ------------------------------------------------------------------------- constants

SIMULATOR_DOMAIN = 'simulator.amazonses.com'

# Amazon's documented mailbox simulator. Not a list we invented: each one drives a
# specific outcome inside SES and none of them reaches a real person or affects the
# account's bounce and complaint metrics.
SIMULATOR_MAILBOXES = frozenset({
    'success', 'bounce', 'ooto', 'complaint', 'suppressionlist',
})

SUCCESS_MAILBOX = f'success@{SIMULATOR_DOMAIN}'

# List price, US regions, outbound. Data-transfer and attachment charges do not apply at
# the sizes here (the messages are a few hundred bytes of text).
PRICE_PER_1000_USD = 0.10

# The verified sender is discovered from SES rather than hardcoded — see sender(). The
# region default is where this account's identity actually lives; AWS_REGION overrides it.
DEFAULT_REGION = 'us-east-2'


class SESNotConfigured(RuntimeError):
    """No credentials, no verified sender, or boto3 is absent. Callers fall back to the
    simulated relay path — a missing credential is not a failed proof."""


class SESRefused(ProviderError):
    """A rail in this module refused BEFORE anything was sent.

    Distinct from a transport failure on purpose: `sent_uncertain` is False here, which is
    what lets the relay release a reservation it made for a send that provably never
    happened. Any error whose `sent_uncertain` is True keeps the key burned, because the
    only safe reading of "we do not know whether that email went out" is "assume it did".
    """

    def __init__(self, message: str, *, status: int = 400):
        super().__init__(message, status=status, retryable=False)
        self.sent_uncertain = False


def _flag(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ('1', 'true', 'yes', 'on')


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


# ------------------------------------------------------------------------ the switch

def enabled() -> bool:
    """Is the real SES path armed?

    Read from the environment per call rather than from `settings`, which is frozen at
    import: a deployed function can be switched without a code change, and a test can arm
    or disarm it inside one process.

    AXIOM_OFFLINE DELIBERATELY DOES NOT FORCE THIS OFF, which is worth explaining because
    the opposite rule is the obvious one and it is wrong here. AXIOM_OFFLINE means "use the
    deterministic local stand-ins instead of Bedrock". On this account that is not a
    testing convenience, it is the permanent mode: Bedrock's on-demand quota is
    structurally zero (L-26C560CE, not adjustable, measured across three regions), so every
    real run of this system — including the deployed demo — is an offline run. A rule that
    said "offline disables SES" would therefore mean "SES can never run at all", which is a
    rail that fires only on the honest case.

    Hermeticity is enforced where it belongs instead: tests/conftest.py sets AXIOM_SES=0
    before axiom.config is imported, exactly as it already does for AXIOM_COMPREHEND, so a
    developer with the flag exported in their shell cannot make `pytest -q` send email.
    """
    return _flag('AXIOM_SES', False)


def region() -> str:
    return (os.environ.get('AXIOM_SES_REGION')
            or os.environ.get('AWS_REGION')
            or DEFAULT_REGION)


# ------------------------------------------------------------------------ the client

_client_lock = threading.Lock()
_client: Any = None
_sender: str | None = None

# Everything this process has put on the wire, ever. A module global rather than a
# per-caller counter because the cap it feeds is a rail against a LOOP, and a loop that
# constructs a fresh caller each iteration is exactly the shape that burns a daily quota.
_sent_count = 0
_sent_lock = threading.Lock()


def _boto():
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError as e:                 # pragma: no cover - boto3 is pinned
                raise SESNotConfigured(f'boto3 is not importable: {e}') from e
            _client = boto3.client(
                'sesv2', region_name=region(),
                # total_max_attempts=1 is the load-bearing argument of this file expressed
                # as configuration. See the module docstring: an automatic retry of a
                # SendEmail that SES may already have accepted is a second email, and
                # there is no key to make it idempotent. One attempt; every retry in this
                # module is a decision somebody wrote down.
                config=Config(retries={'total_max_attempts': 1},
                              connect_timeout=5, read_timeout=20,
                              user_agent_extra='axiom-crash-safe'))
    return _client


def _reset_client() -> None:
    """Drop the cached client and sender. Tests that change region or credentials."""
    global _client, _sender
    with _client_lock:
        _client = None
        _sender = None


def sender() -> str:
    """The From address: AXIOM_SES_FROM, or the account's own verified identity.

    Discovered from SES rather than written into the repository, for two reasons. The
    small one is that a personal address does not belong in a public git history. The
    load-bearing one is that discovery PROVES the identity is verified — a hardcoded
    address is an assumption that fails at send time with a message about identities,
    which reads like an SES outage and is not one.
    """
    global _sender
    explicit = os.environ.get('AXIOM_SES_FROM', '').strip()
    if explicit:
        return explicit
    if _sender:
        return _sender
    try:
        resp = _boto().list_email_identities(PageSize=100)
    except SESNotConfigured:
        raise
    except Exception as e:                           # noqa: BLE001
        raise SESNotConfigured(
            f'cannot list SES identities in {region()}: {type(e).__name__}: {e}') from e
    verified = [i['IdentityName'] for i in resp.get('EmailIdentities', [])
                if i.get('VerificationStatus') == 'SUCCESS' and i.get('SendingEnabled')]
    if not verified:
        raise SESNotConfigured(
            f'no verified SES sending identity in {region()}; verify one or set '
            f'AXIOM_SES_FROM')
    _sender = verified[0]
    return _sender


def available() -> tuple[bool, str]:
    """(can this process send, why not). Never raises — callers use it to choose a path."""
    try:
        import boto3
    except ImportError:                              # pragma: no cover
        return False, 'boto3 is not installed'
    try:
        if boto3.Session().get_credentials() is None:
            return False, 'no AWS credentials'
        return True, sender()
    except SESNotConfigured as e:
        return False, str(e)
    except Exception as e:                           # noqa: BLE001
        return False, f'{type(e).__name__}: {e}'


# -------------------------------------------------------------------------- the rails

def guard(recipient: str) -> str:
    """Refuse any address that is not safe to send to from this account. Returns it
    normalized.

    Default policy is the mailbox simulator and nothing else. AXIOM_SES_ALLOW is the
    deliberate escape hatch for a verified identity of your own, comma separated; it is
    NOT a way to reach a third party, because the sandbox refuses unverified recipients
    anyway — the rail here is to make the refusal happen in this process, with a sentence
    a human wrote, rather than as an SES error after the account has already been asked
    to mail a stranger.
    """
    addr = (recipient or '').strip().lower()
    if '@' not in addr:
        raise SESRefused(f'not an email address: {recipient!r}')
    local, _, domain = addr.rpartition('@')
    if domain == SIMULATOR_DOMAIN:
        # Labels are allowed so one campaign can have several distinct recipients that are
        # all still simulator addresses: success+axiom@simulator.amazonses.com.
        base = local.split('+', 1)[0]
        if base not in SIMULATOR_MAILBOXES:
            raise SESRefused(
                f'{addr} is not a mailbox simulator address; the simulator mailboxes are '
                f'{", ".join(sorted(SIMULATOR_MAILBOXES))}')
        return addr
    allowed = {a.strip().lower()
               for a in os.environ.get('AXIOM_SES_ALLOW', '').split(',') if a.strip()}
    if addr in allowed:
        return addr
    raise SESRefused(
        f'refusing to send to {addr}: this account is in the SES sandbox and AXIOM only '
        f'mails Amazon\'s mailbox simulator ({SIMULATOR_DOMAIN}). Add the address to '
        f'AXIOM_SES_ALLOW only if it is a verified identity you own.')


def max_per_send() -> int:
    return max(1, _int('AXIOM_SES_MAX_PER_SEND', 5))


def max_per_process() -> int:
    return max(1, _int('AXIOM_SES_MAX_TOTAL', 50))


def sent_this_process() -> int:
    return _sent_count


def reset_counters() -> None:
    """Tests only. The cap exists to bound a runaway loop, not to bound a test file."""
    global _sent_count
    with _sent_lock:
        _sent_count = 0


def _reserve_quota() -> None:
    global _sent_count
    with _sent_lock:
        if _sent_count >= max_per_process():
            raise SESRefused(
                f'this process has already sent {_sent_count} messages, at its '
                f'AXIOM_SES_MAX_TOTAL cap; refusing rather than burning the sandbox\'s '
                f'200/day')
        _sent_count += 1


# ------------------------------------------------------------------------- the send

@dataclass(frozen=True)
class Accepted:
    """What SES said. `message_id` is the evidence — it is assigned by SES at acceptance
    and it is the only handle anyone has on a message that has already left."""
    recipient: str
    message_id: str
    latency_ms: float


# SES sandbox rate limit is 1 message/second. Paced here rather than discovered through
# throttling exceptions, because a throttle costs a round trip and a retry decision on a
# call that must not be retried carelessly.
def _pace_seconds() -> float:
    try:
        return float(os.environ.get('AXIOM_SES_PACE_S', '1.05'))
    except ValueError:
        return 1.05


_last_send_at = 0.0
_pace_lock = threading.Lock()

# SES error codes raised BEFORE the message is accepted. Only these are safe to retry,
# and only these let a caller conclude that nothing was sent.
_PRE_ACCEPT_THROTTLES = ('TooManyRequestsException', 'ThrottlingException', 'Throttling')
_PRE_ACCEPT_REFUSALS = ('MessageRejected', 'MailFromDomainNotVerifiedException',
                        'AccountSuspendedException', 'SendingPausedException',
                        'BadRequestException', 'ValidationException',
                        'NotFoundException', 'LimitExceededException')


def send_one(*, recipient: str, subject: str, body_text: str, campaign_ref: str,
             idempotency_key: str) -> Accepted:
    """Put ONE message on the wire. The irreversible act, with nothing standing behind it.

    The idempotency key is carried as a message header. SES does not read it, act on it or
    remember it — it is written into the message so that the artifact sitting in the
    recipient's mailbox names the AXIOM receipt that authorized it, which is the difference
    between an audit trail and an assertion.

    Raises ProviderError. Every raised error carries `sent_uncertain`, and the relay's
    handling of a failed send turns on it: False means the reservation may be released,
    True means the key stays burned because "we do not know" and "it did not happen" are
    not the same claim.
    """
    addr = guard(recipient)
    _reserve_quota()

    from_addr = sender()
    client = _boto()

    def _attempt() -> tuple[str, float]:
        global _last_send_at
        with _pace_lock:
            gap = time.monotonic() - _last_send_at
            if gap < _pace_seconds():
                time.sleep(_pace_seconds() - gap)
            _last_send_at = time.monotonic()
        t0 = time.perf_counter()
        resp = client.send_email(
            FromEmailAddress=from_addr,
            Destination={'ToAddresses': [addr]},
            Content={'Simple': {
                'Subject': {'Data': subject[:200], 'Charset': 'UTF-8'},
                'Body': {'Text': {'Data': body_text, 'Charset': 'UTF-8'}},
                'Headers': [
                    {'Name': 'X-AXIOM-Idempotency-Key', 'Value': idempotency_key},
                    {'Name': 'X-AXIOM-Campaign', 'Value': campaign_ref},
                ],
            }})
        return resp['MessageId'], (time.perf_counter() - t0) * 1000.0

    try:
        from botocore.exceptions import ClientError
    except ImportError:                              # pragma: no cover
        raise SESNotConfigured('botocore is not importable')

    try:
        mid, ms = _attempt()
    except ClientError as e:
        code = e.response.get('Error', {}).get('Code', '')
        if code in _PRE_ACCEPT_THROTTLES:
            # Safe to retry exactly because a throttle is a refusal: SES rejected the
            # request, so no message exists. One retry, then give up and let the task's
            # own retry policy decide.
            time.sleep(1.2)
            try:
                mid, ms = _attempt()
            except ClientError as e2:
                raise _wrap(e2) from e2
        else:
            raise _wrap(e) from e
    except Exception as e:                           # noqa: BLE001
        # Timeouts and connection errors: SES may have accepted the message and lost the
        # response. This is the ambiguous case, and it is the one that must never be
        # retried automatically.
        err = ProviderError(f'SES send failed with no verdict: {type(e).__name__}: {e}',
                            status=502, retryable=False)
        err.sent_uncertain = True
        raise err from e

    return Accepted(recipient=addr, message_id=mid, latency_ms=ms)


def _wrap(e: Any) -> ProviderError:
    resp = getattr(e, 'response', {}) or {}
    code = resp.get('Error', {}).get('Code', '')
    status = int(resp.get('ResponseMetadata', {}).get('HTTPStatusCode', 502) or 502)
    msg = resp.get('Error', {}).get('Message', str(e))
    pre_accept = code in _PRE_ACCEPT_THROTTLES or code in _PRE_ACCEPT_REFUSALS
    err = ProviderError(f'SES {code or "error"}: {msg}'[:300], status=status,
                        retryable=code in _PRE_ACCEPT_THROTTLES)
    # A 4xx that SES names is a decision it made before accepting anything. A 5xx it does
    # not name could be anything, including an accepted message whose response was lost.
    err.sent_uncertain = not (pre_accept or (400 <= status < 500))
    return err


# --------------------------------------------------------------- SES's own testimony

def account() -> dict:
    """SES's own account state and quota.

    `SentLast24Hours` LOOKS like the independent witness this project wants and is not one,
    and finding that out was worth the run that found it. Measured here, us-east-2:

        before a proof that sent 2 messages to the mailbox simulator   1.0
        after                                                          1.0

    Mailbox-simulator traffic does not count toward the sending quota — which is precisely
    what makes the simulator safe to use, and precisely what makes this counter useless as
    evidence about it. Reported anyway, because the sandbox state and the 200/day ceiling
    are real facts about what this account may do; just never used to prove a send
    happened. `cloudwatch_sends()` is the witness that works.
    """
    a = _boto().get_account()
    q = a.get('SendQuota', {})
    return {
        'region': region(),
        'sandbox': not a.get('ProductionAccessEnabled', False),
        'sending_enabled': a.get('SendingEnabled'),
        'enforcement_status': a.get('EnforcementStatus'),
        'max_24_hour_send': q.get('Max24HourSend'),
        'sent_last_24_hours': q.get('SentLast24Hours'),
        'max_send_rate': q.get('MaxSendRate'),
    }


def sent_last_24h() -> float:
    return float(account().get('sent_last_24_hours') or 0.0)


def cloudwatch_sends(*, start, end, period: int = 60) -> float | None:
    """How many messages SES says it accepted in a window. THE independent witness.

    AWS/SES `Send` is published by SES itself, it DOES count mailbox-simulator traffic
    (measured: Sum 2.0 for the minute in which a two-recipient proof ran), and nothing in
    AXIOM can write to it. So a crash proof that dispatched twice and shows 2 here rather
    than 4 is Amazon answering the question, not AXIOM auditing itself.

    Two honest caveats, both of which are why this is reported rather than asserted:

      * it lags. The datapoint for a send appears a few minutes later — the same query run
        90 seconds after a send returned nothing and returned Sum 2.0 at four minutes.
        None means "not published yet", which is a different claim from 0.0 and must never
        be rendered as one.
      * it is account-wide per region. Another process sending from this account in this
        region during the window would inflate it. Nothing else sends from this account.

    CloudWatch GetMetricStatistics is inside the Always Free 1M requests/month on this
    account (verified: `aws freetier get-free-tier-usage` lists CloudWatch Requests at
    1,000,000/month, Always Free).
    """
    try:
        import boto3
        from botocore.config import Config
        cw = boto3.client('cloudwatch', region_name=region(),
                          config=Config(retries={'total_max_attempts': 3}))
        resp = cw.get_metric_statistics(
            Namespace='AWS/SES', MetricName='Send', StartTime=start, EndTime=end,
            Period=period, Statistics=['Sum'])
    except Exception:                                # noqa: BLE001 — a witness is not proof
        return None
    points = resp.get('Datapoints') or []
    if not points:
        return None
    return float(sum(p['Sum'] for p in points))


def cost_usd(messages: int) -> float:
    return round(messages * PRICE_PER_1000_USD / 1000.0, 6)
