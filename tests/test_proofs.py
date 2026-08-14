"""AXIOM :: the proofs are only worth anything if they can fail.

`axiom/proofs.py` exists so a judge can press a button and watch the strongest claims in
this project happen live. That creates a specific hazard the rest of the suite does not
cover: a demonstration that always prints PASS is not evidence, it is a picture of the
word PASS. So these tests assert three different things, and the second and third are the
ones that matter most.

  1. The proofs prove what they say. Memory flips the recovery decision in BOTH
     directions and the quarantine lands inside the transaction that asks; a second
     workload measures its authority in people rather than dollars.

  2. The proofs LEAVE NOTHING BEHIND. Judging runs unattended for four weeks and these
     endpoints are public. A proof that accumulated a tenant, a mission, two quarantined
     memories and an agent row per press would degrade the demo it sits next to — slowly,
     invisibly, and exactly during the month nobody is watching.

  3. The endpoints degrade instead of exploding. A proof that fails has still learned
     something; the honest report of that is a 200 carrying INCONCLUSIVE and a reason.
     A 500 on the page a judge is looking at is worth less than no button at all.
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi import HTTPException
from starlette.responses import Response

from axiom import api, db, demo_state, proofs
from axiom.config import SYSTEM_TENANT
from axiom.seed import DEMO_TENANT

from conftest import query


def route(fn, **kw):
    """Call a route function directly and normalise (status, body).

    Same reasoning as tests/test_resilience.py: httpx is not a dependency of this project,
    the handlers are ordinary `def`s, and calling one is calling exactly what the server
    calls.
    """
    out = fn(**kw)
    if isinstance(out, Response):
        return out.status_code, json.loads(out.body)
    return 200, out


@pytest.fixture(autouse=True)
def _no_gates():
    """The rate gates are per-process and per-name; a test must not be throttled by the
    test before it. The one test that asserts the gate resets it itself."""
    demo_state.reset_gates()
    yield
    demo_state.reset_gates()


def _proof_rows() -> dict[str, int]:
    """Everything a proof run could leave behind, counted."""
    # The LIKE patterns are BOUND, not inlined: conftest.query always hands psycopg a
    # parameter tuple, and a literal % in the statement is then a malformed placeholder.
    return {
        'tenants': query('SELECT count(*) AS n FROM axiom_tenant WHERE slug LIKE %s',
                         ('axiom-proof-%',))[0]['n'],
        'agents': query('SELECT count(*) AS n FROM axiom_agent WHERE worker_ref LIKE %s',
                        ('proof-%',))[0]['n'],
    }


# ============================================================ 1. memory decides

@pytest.fixture(scope='module')
def memory_proof() -> dict:
    """One run of the memory proof, shared by the assertions about it.

    Module-scoped because it is the same run every assertion is about — re-running it per
    test would prove the same thing four times and cost four times the wall clock.
    """
    return proofs.memory_decides()


def test_memory_moves_the_decision_in_both_directions(memory_proof):
    """RESEND -> ESCALATE -> RESEND, with nothing but memory changing between them.

    This is the assertion the whole project is named after. The execution half (a crash
    cannot cause a second refund) is proved everywhere else in this suite; this is the
    half that is easy to fake, because a system can retrieve memories, print them, and
    ignore them, and the recovery would look identical.
    """
    assert [s['action'] for s in memory_proof['steps']] == ['RESEND', 'ESCALATE', 'RESEND']
    assert memory_proof['verdict'] == 'PASS'


def test_the_receipt_never_changed_between_the_three_recoveries(memory_proof):
    """One key across all three. A different key would be a different act, and the proof
    would be comparing two situations rather than one situation under two memories."""
    assert memory_proof['key_unchanged'] is True
    assert memory_proof['idempotency_key'].startswith('axm_')


def test_the_quarantine_took_effect_inside_the_transaction_that_asked(memory_proof):
    """Step 3 quarantines two memories and re-asks in ONE transaction, and the recall does
    not see them — because `quarantined` feeds the computed `retrieval_class`, which is a
    vector index PREFIX column, so the rows move partition at COMMIT with no reindex."""
    assert memory_proof['quarantined'] == 2
    step2, step3 = memory_proof['steps'][1], memory_proof['steps'][2]
    planted = {r['id'] for r in step2['recalled'] if r['planted_by_this_proof']}
    assert len(planted) == 2, 'the planted memories did not come back at all in step 2'
    assert not (planted & {r['id'] for r in step3['recalled']}), (
        'a quarantined memory was still in the candidate set in the same transaction')


def test_the_recall_used_the_vector_index_and_says_so(memory_proof):
    """Identical rows come back when the plan degrades to a full scan, so only the plan
    can catch the regression. `None` would mean "could not check", which is a different
    claim and must not be reported as True."""
    assert memory_proof['plan_uses_vector_index'] is True
    assert 'vector search' in memory_proof['plan']


def test_every_recalled_memory_is_renderable(memory_proof):
    """The API contract the UI consumes: content, outcome and a similarity per memory.

    A judge should be able to SEE which memories moved the decision, which means the
    similarity has to be in the payload — not merely used to produce it.
    """
    for step in memory_proof['steps']:
        assert step['recalled'], f'step {step["n"]} recalled nothing'
        for r in step['recalled']:
            assert r['content'] and r['outcome']
            assert 0.0 <= r['similarity'] <= 1.0
            assert isinstance(r['adverse'], bool)
        # The count the recovery decision is actually made on, so a viewer can check the
        # arithmetic instead of trusting the rationale sentence.
        assert step['adverse_recalled'] == sum(1 for r in step['recalled'] if r['adverse'])


# ================================================ 2. it leaves nothing behind

def test_the_memory_proof_deletes_its_own_world(memory_proof):
    before = _proof_rows()
    out = proofs.memory_decides()
    assert out['cleaned_up'] is True
    after = _proof_rows()
    assert after == before, f'the proof left rows behind: {before} -> {after}'
    assert not query('SELECT id FROM axiom_tenant WHERE id = %s', (out['tenant_id'],))


def test_pressing_it_twice_does_not_degrade_the_demo_tenant():
    """The failure this exists to prevent, stated plainly: forty presses leaving forty
    quarantined memories in the tenant Mission Control is showing."""
    def demo_state_counts():
        return query("""SELECT count(*) AS memories,
                               count(*) FILTER (WHERE quarantined) AS quarantined
                        FROM axiom_memory WHERE tenant_id = %s""", (str(DEMO_TENANT),))[0]

    before = demo_state_counts()
    first, second = proofs.memory_decides(), proofs.memory_decides()
    assert first['verdict'] == second['verdict'] == 'PASS'
    assert first['tenant_id'] != second['tenant_id'], 'two runs shared a tenant'
    assert demo_state_counts() == before


def test_the_reaper_collects_a_tenant_an_earlier_run_abandoned():
    """The one failure the `finally` cannot cover: an instance frozen mid-proof.

    Over four weeks that is the difference between a database that stays the size of the
    demo and one that quietly does not.
    """
    orphan = uuid.uuid4()
    db.tx(lambda cur: cur.execute(
        """INSERT INTO axiom_tenant (id, slug, display_name, created_at)
           VALUES (%s, %s, 'abandoned proof run', now() - INTERVAL '2 hours')""",
        (str(orphan), f'axiom-proof-orphan-{orphan.hex[:8]}')))
    assert query('SELECT id FROM axiom_tenant WHERE id = %s', (str(orphan),))

    assert proofs.reap_stale_tenants(max_age_minutes=30) >= 1
    assert not query('SELECT id FROM axiom_tenant WHERE id = %s', (str(orphan),))


def test_the_reaper_leaves_a_run_that_is_still_going():
    """It must not collect the proof that is running right now, which is what a naive
    'delete every proof tenant' would do to a judge mid-press."""
    live = uuid.uuid4()
    db.tx(lambda cur: cur.execute(
        """INSERT INTO axiom_tenant (id, slug, display_name)
           VALUES (%s, %s, 'live proof run')""",
        (str(live), f'axiom-proof-live-{live.hex[:8]}')))
    try:
        proofs.reap_stale_tenants(max_age_minutes=30)
        assert query('SELECT id FROM axiom_tenant WHERE id = %s', (str(live),))
    finally:
        db.tx(lambda cur: cur.execute('DELETE FROM axiom_tenant WHERE id = %s', (str(live),)))


# =========================================================== 3. the second domain

def test_the_second_domain_measures_authority_in_people():
    """Three campaigns, one crash at W4, and the audit query that matters: one row per
    human being who received the same campaign twice.

    `replays >= 1` is as load-bearing as `messaged_twice == 0`. A run in which no crash
    landed in the dangerous window proved nothing, and a proof that cannot come back
    INCONCLUSIVE is not a proof.
    """
    out = proofs.broadcast_proof()
    if not out.get('available', True):
        pytest.skip(f'relay unavailable: {out.get("reason")}')
    assert out['risk_unit'] == 'comms.recipients'
    assert out['campaigns'] == 3
    assert out['recipients'] > 0
    assert out['replays'] >= 1, 'no re-send was absorbed, so nothing was demonstrated'
    assert out['messaged_twice'] == 0
    assert out['verdict'] == 'PASS'
    assert out['crashed_campaign'] in out['campaign_refs']
    assert _proof_rows()['tenants'] == 0


def test_the_domain_index_shows_two_different_risk_axes():
    rows = {d['task_type']: d for d in proofs.domains()}
    assert rows['refund']['risk_unit'] == 'money.usd_cents'
    assert rows['broadcast']['risk_unit'] == 'comms.recipients'
    assert rows['broadcast']['noun'] == 'recipients'
    # A ceiling of zero would mean the authority model is not actually configured, which
    # is worse than an absent row: it reads as "authorized for nothing" on screen.
    assert all(d['ceiling'] > 0 for d in rows.values())
    assert rows['refund']['ceiling_rendered'].startswith('$')
    assert rows['broadcast']['ceiling_rendered'].endswith('recipients')


def test_a_recipients_ceiling_never_comes_from_the_cents_column(monkeypatch):
    """The one number on this endpoint that could quietly become a lie.

    `max_auto_action_cents` is denominated in cents. A broadcast policy carries its real
    authority in `risk_grants` and keeps that column only because the schema predates the
    unit model. Reading the column for a domain measured in PEOPLE would render a dollar
    figure as a recipient count — on the exact endpoint whose whole argument is that
    authority is denominated in the action's own units.

    Today the demo tenant has no broadcast policy, so the fallback answers and it happens
    to be right. This pins the behaviour for the day someone seeds one.
    """
    seeded = {'broadcast_authority': {
        'policy_id': 'broadcast_authority',
        'max_auto_action_cents': 999999,          # a dollar number, deliberately absurd
        'risk_grants': [{'unit': 'comms.recipients', 'max_magnitude': 750,
                         'max_reversibility': 'IRREVERSIBLE'}],
    }}
    monkeypatch.setattr(demo_state, 'tx', lambda *a, **k: seeded)
    row = {d['task_type']: d for d in proofs.domains()}['broadcast']
    assert row['ceiling'] == 750, 'the cents column leaked into a recipient count'
    assert row['ceiling_rendered'] == '750 recipients'
    assert row['ceiling_source'] == 'live policy'


def test_a_domain_with_no_grant_for_its_own_unit_falls_back_rather_than_lying():
    """A policy row that says nothing about this domain's unit has not authorized it.

    Silently substituting the cents column would manufacture an authority nobody granted,
    so the honest answer is the published proof default, labelled as one.
    """
    rows = {d['task_type']: d for d in proofs.domains()}
    assert rows['broadcast']['ceiling_source'] == 'proof default'
    assert rows['broadcast']['ceiling'] > 0


# ================================================== 4. Stripe degrades honestly

def test_stripe_proof_without_a_key_is_a_200_and_not_an_error(monkeypatch):
    """A missing credential is a fact about the deployment, not a failed proof.

    It must not raise, must not claim PASS, and must still answer in the shape the UI
    consumes, so the page can show the recorded result instead of an error state.
    """
    monkeypatch.delenv('AXIOM_STRIPE_KEY', raising=False)
    out = proofs.stripe_proof()
    assert out['available'] is False
    assert out['verdict'] == 'INCONCLUSIVE'
    assert out['reason']
    for k in ('steps', 'charge_id', 'refund_id', 'replayed', 'refunds_for_order',
              'duplicates', 'dashboard_url'):
        assert k in out


# ================================================== 5. the receipts index

def test_every_recorded_measurement_carries_the_command_that_produced_it():
    """No illustrative figures. If a number is on the page it was measured, and the thing
    that measured it is named beside it — otherwise a laboratory number and a live one
    look identical in JSON."""
    m = proofs.measurements()
    assert isinstance(m['tests'], int) and m['tests'] > 0
    for block in ('chaos', 'scale', 'counterexample'):
        assert m[block]['command'], f'{block} has no command'
        assert m[block]['measured_at'], f'{block} has no date'
        assert m[block]['recorded'] is True
    assert m['sources']['tests']['command']
    for row in m['cockroach_tools'] + m['aws_services']:
        assert row['name'] and row['status'] and row['detail']


def test_the_proofs_index_reports_the_crash_windows_it_actually_serves():
    """Computed from the table this API serves rather than typed into JSON, because a
    hardcoded 7 would survive somebody deleting a window."""
    status, body = route(api.proofs_index)
    assert status == 200
    assert body['crash_windows'] == len(api.CRASH_WINDOWS) == 7
    assert body['live']['vector_index_in_use'] in (True, False, None)
    assert body['tests'] == proofs.measurements()['tests']


def test_the_vector_index_claim_is_verified_live_in_the_index():
    status, body = route(api.proofs_index)
    vector_row = next(r for r in body['cockroach_tools'] if 'Vector' in r['name'])
    assert 'verified_live' in vector_row


# ================================================== 6. the endpoints themselves

def test_the_memory_endpoint_returns_the_contract_the_ui_consumes():
    status, body = route(api.proof_memory)
    assert status == 200
    assert body['verdict'] in ('PASS', 'INCONCLUSIVE')
    assert isinstance(body['quarantined'], int)
    assert body['plan_uses_vector_index'] in (True, False, None)
    for step in body['steps']:
        assert {'n', 'label', 'action', 'rationale', 'recalled'} <= set(step)


def test_a_second_press_is_rate_limited_rather_than_run_twice():
    """One judge's double-click, or one crawler's retry loop, must not become two runs."""
    demo_state.reset_gates()
    route(api.proof_memory)
    with pytest.raises(HTTPException) as e:
        api.proof_memory()
    assert e.value.status_code == 429


def test_a_proof_that_blows_up_degrades_instead_of_500ing(monkeypatch):
    """The blanket guard, tested by breaking the proof under it.

    A 500 on the page a judge is looking at is worth less than no button at all, so the
    contract is: a proof that fails reports INCONCLUSIVE and why.
    """
    def _boom(**kw):
        raise RuntimeError('the database ate the receipt')

    monkeypatch.setattr(proofs, 'memory_decides', _boom)
    demo_state.reset_gates()
    status, body = route(api.proof_memory)
    assert status == 200
    assert body['verdict'] == 'INCONCLUSIVE'
    assert 'the database ate the receipt' in body['error']


def test_the_domains_endpoint_is_a_list_of_workloads():
    status, body = route(api.domains)
    assert status == 200
    assert {d['task_type'] for d in body} >= {'refund', 'broadcast'}
    for d in body:
        assert {'task_type', 'name', 'risk_unit', 'noun', 'ceiling', 'description'} <= set(d)


def test_no_proof_leaves_an_agent_row_behind():
    """Agent rows are the quietest leak in this system: one per worker start, no tenant
    scoping, and the UI renders every one it is given. tests/test_resilience.py already
    fought this battle for the demo controls; the proofs must not reopen it."""
    before = query("SELECT count(*) AS n FROM axiom_agent WHERE tenant_id = %s",
                   (str(SYSTEM_TENANT),))[0]['n']
    demo_state.reset_gates()
    route(api.proof_memory)
    after = query("SELECT count(*) AS n FROM axiom_agent WHERE tenant_id = %s",
                  (str(SYSTEM_TENANT),))[0]['n']
    assert after == before
