"""The boring part, factored out so the examples stay about the integration.

In your application these three things already exist in some form: a customer (tenant),
a run with a budget (mission), and a written-down rule about what the agent may do
unattended (policy). Here they are created on demand so every example is one command.

Nothing in this file is part of the adapter's surface — it is scaffolding.
"""

from __future__ import annotations

import os
import uuid

from axiom import db, policy as policy_mod
from axiom.adapter import bind, open_mission

# A fixed id so repeated runs share one tenant instead of littering the cluster.
TENANT = uuid.UUID('22222222-2222-2222-2222-222222222222')
POLICY = 'action_authority'


def _ensure_tenant_and_policy(cur) -> None:
    cur.execute("""
        INSERT INTO axiom_tenant (id, slug, display_name)
        VALUES (%s, 'axiom-examples', 'AXIOM examples') ON CONFLICT (id) DO NOTHING
    """, (str(TENANT),))
    cur.execute("SELECT 1 FROM axiom_policy WHERE tenant_id = %s AND policy_id = %s",
                (str(TENANT), POLICY))
    if cur.fetchone():
        return
    policy_mod.publish(
        cur, tenant_id=TENANT, policy_id=POLICY, version=1,
        body={'description': 'What the example agent may do without asking a human',
              'max_auto_action_cents': 20000,
              # Read by axiom.adapter's risk gate. Money has a ceiling in a column;
              # everything else that is irreversible is named here, by label.
              'escalate_risks': ['data_deletion', 'bulk_external_comms'],
              'rationale': 'Above $200, or anything that cannot be undone with money, '
                           'is a business decision and gets a person.'},
        max_auto_action_cents=20000, requires_approval=False,
        created_by='human:ops@example.invalid', activate=True,
        signature='example-signature', signed_by='human:cfo@example.invalid')


def setup(title: str, goal: str, budget_cents: int = 500_00) -> tuple[uuid.UUID, uuid.UUID]:
    """Tenant + policy + mission, then bind the guards to them. Returns (tenant, mission).

    AXIOM_EXAMPLE_MISSION lets a child process join the mission its parent opened, which
    is what the crash demos need: the same run, across a process that died.
    """
    db.tx(_ensure_tenant_and_policy)

    existing = os.environ.get('AXIOM_EXAMPLE_MISSION')
    if existing:
        bind(tenant_id=TENANT, mission_id=uuid.UUID(existing), policy_id=POLICY,
             actor='system:example')
        return TENANT, uuid.UUID(existing)

    mission_id = open_mission(
        tenant_id=TENANT, title=title, goal=goal, budget_cents=budget_cents,
        policy_id=POLICY, created_by='human:ops@example.invalid')
    return TENANT, mission_id
