"""P5 pilot: an intent created with execution off is held, and its plan shows a false
"CONDITIONS CHANGED" (owner report 2026-11-12, intent pin_7452d6099eaf9caf).

Reproduced read-only on the pilot database (`scripts/diagnose_pilot_intent.py`,
`evidence/phase5/pilot_intent_diagnosis_20261112T171911Z.json`). These tests pin:
- the material conditions are in fact unchanged when computed the way execution does
  (the intent's own invoice included, its own reservation excluded): passing;
- enabling execution never releases a held message by itself: passing (safety property);
- the plan view must not tell the payer to re-approve an intent that is merely in
  execution: strict xfail until the proposed fix is approved;
- an explicit, revalidated, idempotent release of the SAME intent and commitment: strict
  xfail specification of the proposed mechanism (not implemented).

Mock provider only; no PayPal call.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from api.liquidity.tables import capacity_commitment
from api.workflow import conditions
from api.workflow.networks import load_network
from api.workflow.plans import load_snapshot, verify_leg_now
from api.workflow.tables import outbox_message, payment_authorization

pytestmark = pytest.mark.integration

PROPOSED = "proposed fix (pilot intent report); not implemented until the owner reviews it"


def _held(p):
    p.bind()
    p.app.state.payments_execution_enabled = False  # as when the owner prepared the intent
    buyer, intent = p.agree_and_approve()
    assert intent["status"] == "approved" and intent["status_reason"] == "EXECUTION_DISABLED"
    return buyer, intent


def _outbox(p, iid):
    with p.engine.connect() as c:
        return c.execute(
            select(outbox_message.c.status, outbox_message.c.kind).where(
                outbox_message.c.intent_id == iid
            )
        ).all()


def test_material_conditions_are_unchanged_when_computed_as_execution_does(pilot):
    p = pilot
    _, intent = _held(p)
    with p.engine.connect() as c:
        a = (
            c.execute(
                select(payment_authorization).where(
                    payment_authorization.c.authorization_id == intent["authorization_id"]
                )
            )
            .mappings()
            .one()
        )
        snap = load_snapshot(c, intent["plan_id"], "pilot_buyer")
        leg = next(x for x in snap["record"]["selected_legs"] if x["leg_id"] == a["option_id"])
        view = load_network(c, snap["network_id"], initiator="pilot_buyer", as_of=p.now)
        execution = load_network(
            c,
            snap["network_id"],
            initiator="pilot_buyer",
            as_of=p.now,
            executing_intent=intent["intent_id"],
        )
        assert conditions.payment_conditions(c, view, leg, p.now) is None  # invoice excluded
        cond = conditions.payment_conditions(c, execution, leg, p.now)
        assert conditions.digest(cond) == a["conditions_digest"]
        assert verify_leg_now(execution, leg, p.now) == []
        c.rollback()


def test_enabling_execution_never_releases_a_held_message_by_itself(pilot):
    p = pilot
    _, intent = _held(p)
    p.app.state.payments_execution_enabled = True
    p.flags["execution"] = True
    p.work()
    assert _outbox(p, intent["intent_id"]) == [("held", "create_payment_session")]
    assert p.mock.calls == []


def test_reproduction_plan_view_reports_a_false_conditions_change(pilot):
    """The defect as observed: the view asks to re-approve and calls the plan stale."""
    p = pilot
    buyer, intent = _held(p)
    view = buyer.get(f"/v1/m/pilot_buyer/plans/{intent['plan_id']}").json()
    leg = next(x for x in view["my_legs"] if x["obligation_id"] == "pilot_001")
    assert leg["payment_authorization"]["status"] == "needs_reapproval"
    assert leg["authorize"]["plan_current"] is False


@pytest.mark.xfail(strict=True, reason=PROPOSED)
def test_plan_view_does_not_ask_to_reapprove_an_intent_in_execution(pilot):
    p = pilot
    buyer, intent = _held(p)
    view = buyer.get(f"/v1/m/pilot_buyer/plans/{intent['plan_id']}").json()
    leg = next(x for x in view["my_legs"] if x["obligation_id"] == "pilot_001")
    assert leg["payment_authorization"]["status"] == "in_execution"
    assert leg["payment_authorization"]["intent_id"] == intent["intent_id"]
    assert "authorize" not in leg  # no re-approval offered for an invoice in execution


@pytest.mark.xfail(strict=True, reason=PROPOSED)
def test_release_queues_the_same_intent_once_after_full_revalidation(pilot):
    p = pilot
    buyer, intent = _held(p)
    iid = intent["intent_id"]
    p.app.state.payments_execution_enabled = True
    buyer.reauth()
    r = buyer.post(f"/v1/m/pilot_buyer/payment-intents/{iid}/release", {}, key="release-key-1")
    assert r.status_code == 200, r.text
    again = buyer.post(f"/v1/m/pilot_buyer/payment-intents/{iid}/release", {}, key="release-key-1")
    assert again.json()["replayed"] is True
    assert _outbox(p, iid) == [("pending", "create_payment_session")]
    with p.engine.connect() as c:  # the original reservation is reused, never duplicated
        assert c.execute(select(func.count()).select_from(capacity_commitment)).scalar() == 1
    p.work()
    assert p.mock.created_count() == 1 and p.intent(iid)["status"] == "payer_action_pending"


@pytest.mark.xfail(strict=True, reason=PROPOSED)
def test_release_is_refused_when_execution_is_still_off(pilot):
    p = pilot
    buyer, intent = _held(p)
    buyer.reauth()
    r = buyer.post(
        f"/v1/m/pilot_buyer/payment-intents/{intent['intent_id']}/release", {}, key="release-key-2"
    )
    assert r.status_code == 409 and r.json()["detail"]["code"] == "EXECUTION_DISABLED"
    assert _outbox(p, intent["intent_id"]) == [("held", "create_payment_session")]
