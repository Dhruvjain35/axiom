"""AXIOM :: the guarantee where the provider supplies none of it.

tests/test_crash_windows.py proves the engine cannot double-refund against a provider with
idempotency keys. tests/test_domain2.py proves it cannot double-send against a relay that
models one. This file removes the last piece of external help: Amazon SES has NO idempotent
send. Call SendEmail twice and two emails arrive, each with its own MessageId, and there is
no header to hand it and no replay flag to read back.

So every assertion here is about AXIOM's own machinery, and the load-bearing test is the
COUNTEREXAMPLE — test_two_different_keys_really_do_send_two_emails. It proves the fake SES
in this file behaves like the real one: it deduplicates nothing. Without it, every other
test in the file could pass against a stub that quietly refused the second send, and the
suite would be proving a property of the test double.

HERMETIC BY DEFAULT, AND THAT IS NOT A COMPROMISE
-------------------------------------------------
The relay's SES path is exercised through a fake sender, so the reservation protocol, the
dedupe, the refusal to re-send after an ambiguous failure and the 409 on a changed body are
all under test with no AWS credentials, no network, and no cost. tests/conftest.py sets
AXIOM_OFFLINE before axiom.config is imported, which forces ses.enabled() to False no matter
what is exported in the developer's shell; the tests that need the path armed monkeypatch
`ses.enabled` inside their own process rather than touching the environment.

ONE test reaches the real Amazon SES, and it is skipped unless BOTH credentials and
AXIOM_SES_LIVE=1 are present. Ambient credentials are not consent to send email.
"""

from __future__ import annotations

import os
import uuid

import pytest

from axiom import ses
from axiom.domains import relay
from axiom.provider import ProviderError

SIM = ses.SUCCESS_MAILBOX
SIM2 = 'success+axiom@simulator.amazonses.com'


# ============================================================================ the rails

def test_ses_is_off_unless_it_is_armed():
    """The demo a judge presses must not change behaviour because a module was added."""
    assert ses.enabled() is False


def test_the_suite_disarms_the_flag_rather_than_trusting_the_shell():
    """Hermetic BY CONSTRUCTION, not by remembering.

    AXIOM_OFFLINE cannot be the guard here — axiom/ses.py explains why: Bedrock's quota is
    structurally zero on this account, so offline is the permanent mode of every real run,
    and "offline disables SES" would mean SES never runs. tests/conftest.py therefore SETS
    AXIOM_SES=0 before axiom.config is imported, the way it already does for
    AXIOM_COMPREHEND. This test is that guarantee, asserted rather than assumed.
    """
    assert os.environ.get('AXIOM_SES') == '0'
    assert ses.enabled() is False


@pytest.mark.parametrize('addr', [
    'someone@gmail.com',                     # a real stranger. The whole point.
    'nobody@example.com',
    'evil@simulator.amazonses.comx',         # domain that only looks like the simulator
    'notamailbox@simulator.amazonses.com',   # simulator domain, invented local part
    'garbage',
    '',
])
def test_guard_refuses_every_address_that_is_not_the_simulator(addr):
    """A hackathon demo that emails strangers is a hackathon demo that gets its account
    suspended, and a bounce from a stranger's mailbox damages the sending reputation of the
    account being judged. The rail is in this process, before SES is asked."""
    with pytest.raises(ses.SESRefused):
        ses.guard(addr)


@pytest.mark.parametrize('addr', [
    'success@simulator.amazonses.com',
    'SUCCESS@Simulator.AmazonSES.com',       # normalized, not rejected
    'success+axiom@simulator.amazonses.com',  # a label, so a campaign can have distinct
    'bounce@simulator.amazonses.com',         # recipients that are all still simulated
    'complaint@simulator.amazonses.com',
])
def test_guard_allows_the_mailbox_simulator(addr):
    assert ses.guard(addr) == addr.lower()


def test_the_allowlist_is_the_only_escape_hatch(monkeypatch):
    monkeypatch.setenv('AXIOM_SES_ALLOW', 'ops@axiom.demo, other@axiom.demo')
    assert ses.guard('ops@axiom.demo') == 'ops@axiom.demo'
    with pytest.raises(ses.SESRefused):
        ses.guard('someone-else@axiom.demo')


def test_the_process_cap_refuses_rather_than_burning_the_daily_quota(monkeypatch):
    """A loop that sends is how a 200/day sandbox dies at 09:14. The cap is a rail against
    a bug, so it refuses instead of trimming."""
    monkeypatch.setenv('AXIOM_SES_MAX_TOTAL', '2')
    ses.reset_counters()
    try:
        ses._reserve_quota()
        ses._reserve_quota()
        with pytest.raises(ses.SESRefused):
            ses._reserve_quota()
    finally:
        ses.reset_counters()


def test_cost_is_stated_in_the_units_amazon_bills(monkeypatch):
    # $0.10 per 1,000. Two emails is $0.0002, which is small and is not zero.
    assert ses.cost_usd(1000) == pytest.approx(0.10)
    assert ses.cost_usd(2) == pytest.approx(0.0002)


# ====================================================================== a fake Amazon SES

class FakeSES:
    """SES, faithfully: it sends whatever it is asked to send, every time.

    No key, no memory, no dedupe — a fresh MessageId per call, exactly like the real API.
    If this class ever grows a dedupe, every test below stops testing AXIOM.
    """

    def __init__(self):
        self.calls: list[tuple[str, str]] = []       # (recipient, idempotency_key)
        self.fail_after: int | None = None
        self.fail_with: Exception | None = None

    def __call__(self, *, recipient, subject, body_text, campaign_ref, idempotency_key):
        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            raise self.fail_with
        self.calls.append((recipient, idempotency_key))
        return ses.Accepted(recipient=recipient,
                            message_id=f'0100{uuid.uuid4().hex[:16]}-fake-000000',
                            latency_ms=12.5)


@pytest.fixture
def fake_ses(monkeypatch):
    f = FakeSES()
    monkeypatch.setattr(ses, 'enabled', lambda: True)
    monkeypatch.setattr(ses, 'send_one', f)
    relay.ensure_schema()
    return f


def _campaign() -> str:
    return f'SES-T-{uuid.uuid4().hex[:12].upper()}'


def _key() -> str:
    return f'test-ses-{uuid.uuid4().hex}'


def _send(key, ref, recipients, *, n=None, body=None):
    return relay.send(idempotency_key=key, campaign_ref=ref, segment='sim',
                      recipient_count=n if n is not None else len(recipients),
                      recipients=recipients, request_body=body)


# ========================================================== the counterexample, first

def test_two_different_keys_really_do_send_two_emails(fake_ses):
    """SES DEDUPLICATES NOTHING, and this test is what makes the rest of the file mean
    something. Same recipient, same body, two keys: two calls, two MessageIds, and the
    relay's audit query finds a human being who received the same campaign twice.

    That is the failure this system exists to prevent, reproduced deliberately.
    """
    ref = _campaign()
    a = _send(_key(), ref, [SIM])
    b = _send(_key(), ref, [SIM])

    assert len(fake_ses.calls) == 2
    assert a['message_ids'] != b['message_ids']
    assert a['ses_accepted'] == b['ses_accepted'] == 1
    dupes = relay.duplicate_recipients([ref])
    assert [d['recipient'] for d in dupes] == [SIM]
    assert dupes[0]['deliveries'] == 2


# ============================================================== the guarantee itself

def test_the_same_key_sends_exactly_one_email(fake_ses):
    """Two dispatches, ONE MessageId. The second call never reaches SES at all — there is
    no request for SES to recognize, because AXIOM did not make one."""
    ref, key = _campaign(), _key()
    first = _send(key, ref, [SIM])
    second = _send(key, ref, [SIM])

    assert len(fake_ses.calls) == 1                    # SES was asked exactly once
    assert first['replayed'] is False and first['ses_accepted'] == 1
    assert second['replayed'] is True and second['ses_accepted'] == 0
    assert second['message_ids'] == first['message_ids']
    assert second['id'] == first['id']
    assert relay.duplicate_recipients([ref]) == []


def test_the_key_is_committed_before_the_send(fake_ses):
    """The reservation is not an implementation detail, it is the mechanism.

    A sender that recorded the key AFTER the call would be protecting nothing: the crash
    this system is about happens in exactly that gap. So the fake asserts on the state of
    the relay's store at the instant SES is called, from inside the call.
    """
    ref, key = _campaign(), _key()
    seen: dict = {}

    real = ses.send_one

    def observe(**kw):
        with relay.pool().connection() as conn, conn.cursor() as cur:
            cur.execute('SELECT status, channel FROM relay_send WHERE idempotency_key = %s',
                        (key,))
            seen['row'] = cur.fetchone()
        return real(**kw)

    import unittest.mock as m
    with m.patch.object(ses, 'send_one', observe):
        _send(key, ref, [SIM])

    assert seen['row'] is not None, 'the key was not durable when the email was sent'
    assert seen['row']['status'] == 'reserved'
    assert seen['row']['channel'] == 'ses'


def test_a_changed_body_under_the_same_key_is_a_409(fake_ses):
    """Crash window W7 on the real path: a recovered agent that re-synthesizes the campaign
    with one address changed is a NEW INTENT wearing an OLD key, and it must not send."""
    ref, key = _campaign(), _key()
    _send(key, ref, [SIM], body={'campaign_ref': ref, 'recipients': [SIM]})
    with pytest.raises(ProviderError) as e:
        _send(key, ref, [SIM2], body={'campaign_ref': ref, 'recipients': [SIM2]})
    assert e.value.status == 409
    assert e.value.retryable is False
    assert len(fake_ses.calls) == 1


def test_an_ambiguous_failure_burns_the_key_and_never_re_sends(fake_ses):
    """The hardest case, and the one that decides whether this design is honest.

    SES accepted message 1. Message 2 failed with no verdict — a read timeout is
    indistinguishable from an accepted message whose response was lost. AXIOM cannot know
    whether that email exists, and "we do not know" is not "it did not happen".

    So the key stays claimed and the next dispatch sends NOTHING. That loses a message and
    it cannot produce a second copy, which is the correct direction for a campaign: a bad
    afternoon rather than a compliance incident.
    """
    ref, key = _campaign(), _key()
    boom = ProviderError('SES send failed with no verdict: ReadTimeoutError',
                         status=502, retryable=False)
    boom.sent_uncertain = True
    fake_ses.fail_after, fake_ses.fail_with = 1, boom

    with pytest.raises(ProviderError):
        _send(key, ref, [SIM, SIM2])

    fake_ses.fail_after = None                       # the transport recovers; AXIOM retries
    again = _send(key, ref, [SIM, SIM2])

    assert len(fake_ses.calls) == 1, 'a message was sent twice after an ambiguous failure'
    assert again['replayed'] is True and again['ses_accepted'] == 0
    assert again['status'] == 'reserved'
    assert 'refusing to re-send' in again['warning']
    assert relay.duplicate_recipients([ref]) == []


def test_a_refusal_that_provably_sent_nothing_releases_the_key(fake_ses):
    """The one case where releasing the reservation is safe, and it matters.

    SES named the refusal before accepting anything, so nothing exists. If the key stayed
    burned, a campaign rejected for a bad address could never be sent under the receipt
    AXIOM already committed — a typo would become permanent.
    """
    ref, key = _campaign(), _key()
    fake_ses.fail_after = 0
    fake_ses.fail_with = ses.SESRefused('MessageRejected: Email address is not verified')

    with pytest.raises(ProviderError):
        _send(key, ref, [SIM])

    with relay.pool().connection() as conn, conn.cursor() as cur:
        cur.execute('SELECT count(*) AS n FROM relay_send WHERE idempotency_key = %s',
                    (key,))
        assert cur.fetchone()['n'] == 0, 'a send that provably never happened burned a key'

    fake_ses.fail_after = None
    out = _send(key, ref, [SIM])
    assert out['ses_accepted'] == 1 and out['replayed'] is False


def test_a_campaign_larger_than_the_cap_is_refused_not_trimmed(fake_ses, monkeypatch):
    """Mailing the first 5 of 1,000 addresses and recording it as a delivered campaign
    would put a number in the ledger that means nothing."""
    monkeypatch.setenv('AXIOM_SES_MAX_PER_SEND', '2')
    with pytest.raises(ProviderError) as e:
        _send(_key(), _campaign(), [SIM, SIM2, 'bounce@simulator.amazonses.com'])
    assert e.value.status == 400
    assert fake_ses.calls == []


def test_a_stranger_stops_the_whole_campaign_before_anything_is_sent(fake_ses):
    """The guard runs over every address BEFORE the first send, not per message. A campaign
    that is half-sent and then refused would have mailed people under a key that is now
    burned, for a mistake that was visible before anything left."""
    with pytest.raises(ses.SESRefused):
        _send(_key(), _campaign(), [SIM, 'someone@gmail.com'])
    assert fake_ses.calls == []


def test_the_simulated_path_is_untouched_when_no_list_is_given(fake_ses):
    """AXIOM_SES armed, no recipient list: byte-for-byte the old behaviour, and SES is not
    called. POST /api/proof/broadcast passes no list, which is why arming the flag cannot
    turn the demo a judge presses into 1,700 real emails."""
    ref, key = _campaign(), _key()
    out = relay.send(idempotency_key=key, campaign_ref=ref, segment='sim',
                     recipient_count=3, latency_ms=0)
    assert out['channel'] == 'simulated'
    assert 'message_ids' not in out
    assert fake_ses.calls == []
    assert relay.stats([ref])['deliveries'] == 3


def test_the_ledger_can_tell_a_real_email_from_a_simulated_one(fake_ses):
    """An audit that cannot distinguish them would let 230,000 simulated rows be counted as
    evidence about Amazon SES."""
    ref = _campaign()
    _send(_key(), ref, [SIM])
    relay.send(idempotency_key=_key(), campaign_ref=ref, segment='sim',
               recipient_count=2, latency_ms=0)
    with relay.pool().connection() as conn, conn.cursor() as cur:
        cur.execute("""SELECT COALESCE(channel, 'simulated') AS c, count(*) AS n,
                              count(message_id) AS with_id
                       FROM relay_delivery WHERE campaign_ref = %s GROUP BY 1""", (ref,))
        by = {r['c']: r for r in cur.fetchall()}
    assert by['ses']['n'] == 1 and by['ses']['with_id'] == 1
    assert by['simulated']['n'] == 2 and by['simulated']['with_id'] == 0


# ================================================================== the live one

def _live_skip() -> str:
    """Why the live test is skipped, or '' to run it.

    Ambient credentials are NOT enough, for the reason tests/test_comprehend.py gives and
    with more at stake: this one puts a real message on the wire from an account that is
    being judged. `pytest -q` on a laptop with a shared credentials file must not send
    email, so the opt-in is explicit and separate.
    """
    if os.environ.get('AXIOM_SES_LIVE', '').strip().lower() not in ('1', 'true', 'yes'):
        return ('set AXIOM_SES_LIVE=1 to put a real message on the wire; ambient '
                'credentials alone are not consent to send email')
    ok, why = ses.available()
    return '' if ok else f'SES unavailable: {why}'


LIVE_SKIP = _live_skip()


@pytest.mark.skipif(bool(LIVE_SKIP), reason=LIVE_SKIP or 'live')
def test_live_ses_sends_exactly_one_email_under_one_key():
    """The whole claim, against the real Amazon SES, for the price of one email.

    Two dispatches under one key. The evidence is the MessageId: SES mints it at
    acceptance, AXIOM cannot fabricate one, and the second dispatch produces no new one
    because SES never hears about it.

    The quota counter is asserted NOT to move, which is the opposite of what this test
    claimed when it was written. Mailbox-simulator mail does not count against
    Max24HourSend — measured, 1.0 before and after a two-message run — and that exclusion
    is exactly what makes the simulator safe to send to. CloudWatch's AWS/SES Send metric
    is the counter that does include it, and it publishes minutes late, so it belongs in
    scripts/ses_proof.py behind an explicit poll rather than in a test that has to finish.
    """
    import unittest.mock as m

    relay.ensure_schema()
    before = ses.sent_last_24h()
    ref, key = _campaign(), _key()

    with m.patch.object(ses, 'enabled', lambda: True):
        first = _send(key, ref, [SIM])
        second = _send(key, ref, [SIM])

    assert first['ses_accepted'] == 1
    # An SES MessageId, not a string this process invented: 0100<hex>-<uuid>-000000.
    assert first['message_ids'] and first['message_ids'][0].count('-') >= 4
    assert second['ses_accepted'] == 0
    assert second['message_ids'] == first['message_ids']
    assert relay.duplicate_recipients([ref]) == []
    assert ses.sent_last_24h() == before, (
        'the sending quota moved for simulator traffic; the safety argument for using the '
        'simulator rests on it not counting')
