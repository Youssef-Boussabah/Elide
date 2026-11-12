"""P5 pilot: a held intent and the false "CONDITIONS CHANGED" label (owner-approved fixes).

The original defect (intent pin_7452d6099eaf9caf on the isolated pilot database) is kept
as evidence: `evidence/phase5/pilot_intent_held_and_label_2026-11-12.md` and
`evidence/phase5/pilot_intent_diagnosis_20261112T171911Z.json`. The plan view recomputed
conditions on a network that excludes invoices in execution, so it reported
`needs_reapproval` and a stale plan. Held dispatch messages had no release path.

Here:
- A. the plan view reports `in_execution` (or `in_execution_conditions_changed` when the
  execution-aware revalidation fails), offers no re-approval, and does not call the plan stale;
- B. the payer's release re-runs the worker's revalidation and queues the SAME intent once.

Mock provider only; no PayPal call.
"""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import func, select, update

from api.app.auth import service as auth
from api.execution import bindings
from api.execution.tables import payment_state_event, provider_account
from api.liquidity.store import release_commitment_in
from api.liquidity.tables import capacity_commitment
from api.workflow import conditions
from api.workflow.networks import load_network
from api.workflow.plans import load_snapshot, verify_leg_now
from api.workflow.tables import outbox_message, payment_authorization, payment_intent

pytestmark = pytest.mark.integration


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


def _leg_view(actor, merchant, plan_id, obligation="pilot_001"):
    r = actor.get(f"/v1/m/{merchant}/plans/{plan_id}")
    assert r.status_code == 200, r.text
    return next(x for x in r.json()["my_legs"] if x["obligation_id"] == obligation)


def _release(p, actor, iid, key):
    actor.reauth()
    return actor.post(f"/v1/m/pilot_buyer/payment-intents/{iid}/release", {}, key=key)


def _enable(p):
    p.app.state.payments_execution_enabled = True
    p.flags["execution"] = True


def _confirm_reserve(actor, reserve: str) -> None:
    from tests.integration.test_p5_failures import _confirm_reserve as confirm

    confirm(actor, reserve)


# --- characterization (unchanged by the fixes) -----------------------------------------------
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
    _enable(p)
    p.work()
    assert _outbox(p, intent["intent_id"]) == [("held", "create_payment_session")]
    assert p.mock.calls == []


# --- A. plan view ------------------------------------------------------------------------------
def test_reproduction_now_corrected_the_view_shows_in_execution_not_a_conditions_change(pilot):
    """The original defect (needs_reapproval + stale plan; evidence above) is fixed."""
    p = pilot
    buyer, intent = _held(p)
    leg = _leg_view(buyer, "pilot_buyer", intent["plan_id"])
    assert leg["payment_authorization"]["status"] == "in_execution"
    assert leg["payment_authorization"]["status"] != "needs_reapproval"


def test_plan_view_does_not_ask_to_reapprove_an_intent_in_execution(pilot):
    p = pilot
    buyer, intent = _held(p)
    leg = _leg_view(buyer, "pilot_buyer", intent["plan_id"])
    auth_ = leg["payment_authorization"]
    assert auth_["status"] == "in_execution"
    assert auth_["intent_id"] == intent["intent_id"]
    assert auth_["authorization_id"] == intent["authorization_id"]
    assert auth_["intent_status"] == "approved"
    assert "authorize" not in leg  # no re-approval offered for an invoice in execution


def test_a_real_change_is_reported_as_in_execution_conditions_changed(pilot):
    p = pilot
    buyer, intent = _held(p)
    buyer.reauth()
    _confirm_reserve(buyer, "70")  # a newer policy: the approval no longer holds
    leg = _leg_view(buyer, "pilot_buyer", intent["plan_id"])
    auth_ = leg["payment_authorization"]
    assert auth_["status"] == "in_execution_conditions_changed"
    assert auth_["problems"] == ["CONDITIONS_CHANGED"]
    assert "authorize" not in leg and "expires_at" not in auth_


def test_an_expired_approval_is_never_shown_as_valid(pilot):
    p = pilot
    _, intent = _held(p)
    p.advance(hours=25)
    buyer = p.actor(p.buyer_login)
    auth_ = _leg_view(buyer, "pilot_buyer", intent["plan_id"])["payment_authorization"]
    assert auth_["status"] == "in_execution_conditions_changed"
    assert auth_["problems"] == ["AUTHORIZATION_EXPIRED"]


def test_supplier_sees_in_execution_only_and_outsiders_see_nothing(pilot):
    p = pilot
    _, intent = _held(p)
    seller = p.actor(p.seller_login)
    auth_ = _leg_view(seller, "pilot_seller", intent["plan_id"])["payment_authorization"]
    assert auth_ == {
        "status": "in_execution",
        "intent_id": intent["intent_id"],
        "intent_status": "approved",
    }
    with p.engine.begin() as c:
        auth.create_merchant(
            c, "outsider", "Outsider (TEST)", "America/Toronto", is_synthetic=False
        )
        uid = auth.create_user(
            c, "outsider@elide.test", "Outsider", "test-only-outsider-pw-9", is_test_identity=True
        )
        auth.grant(c, uid, "outsider", "owner", at=p.now)
    from tests.integration.conftest import Actor

    out = Actor(p.app, "outsider@elide.test", "test-only-outsider-pw-9")
    for merchant in ("outsider", "pilot_buyer"):
        assert out.get(f"/v1/m/{merchant}/plans/{intent['plan_id']}").status_code == 404


def test_other_legs_are_not_called_stale_because_one_invoice_is_in_execution(pilot_two):
    p = pilot_two
    p.bind()
    p.app.state.payments_execution_enabled = False
    buyer = p.actor(p.buyer_login)
    for o in buyer.get("/v1/m/pilot_buyer/offers").json()["offers"]:
        r = buyer.post(
            f"/v1/m/pilot_buyer/offers/{o['offer_id']}/decision",
            {"decision": "accept", "terms_digest": o["terms_digest"]},
            key=f"accept-{o['obligation_id']}",
        )
        assert r.status_code == 200, r.text
    plan = buyer.post("/v1/m/pilot_buyer/plans", {}, key="plan-two-legs").json()["plan"]
    leg1 = next(x for x in plan["my_legs"] if x["obligation_id"] == "pilot_001")
    a = buyer.post(
        f"/v1/m/pilot_buyer/plans/{plan['plan_id']}/authorizations",
        {
            "leg_id": leg1["leg_id"],
            "decision": "approve",
            "binding_digest": leg1["authorize"]["binding_digest"],
        },
        key="auth-two-1",
    )
    assert a.status_code == 200, a.text
    pi = buyer.post(
        "/v1/m/pilot_buyer/payment-intents",
        {"authorization_id": a.json()["authorization_id"]},
        key="pi-two-1",
    )
    assert pi.status_code == 200, pi.text
    other = _leg_view(buyer, "pilot_buyer", plan["plan_id"], "pilot_002")
    assert other["payment_authorization"]["status"] == "pending"
    assert other["authorize"]["plan_current"] is True  # not stale: pilot_001 is in execution


# --- B. release --------------------------------------------------------------------------------
def test_release_queues_the_same_intent_once_after_full_revalidation(pilot):
    p = pilot
    buyer, intent = _held(p)
    iid = intent["intent_id"]
    _enable(p)
    listed = buyer.get("/v1/m/pilot_buyer/payment-intents").json()["intents"][0]
    assert listed["release_available"] is True
    r = _release(p, buyer, iid, "release-key-1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["intent_id"] == iid and body["authorization_id"] == intent["authorization_id"]
    assert body["commitment_id"] == intent["commitment_id"] and body["replayed"] is False
    again = _release(p, buyer, iid, "release-key-1")
    assert again.status_code == 200 and again.json()["replayed"] is True
    assert _outbox(p, iid) == [("pending", "create_payment_session")]
    with p.engine.connect() as c:  # the original reservation is reused, never duplicated
        assert c.execute(select(func.count()).select_from(capacity_commitment)).scalar() == 1
        events = (
            c.execute(
                select(payment_state_event.c.reason).where(payment_state_event.c.intent_id == iid)
            )
            .scalars()
            .all()
        )
    assert events == ["RELEASED_BY_PAYER"]
    p.work()
    assert p.mock.created_count() == 1 and p.intent(iid)["status"] == "payer_action_pending"


def test_release_is_refused_when_execution_is_still_off(pilot):
    p = pilot
    buyer, intent = _held(p)
    r = _release(p, buyer, intent["intent_id"], "release-key-2")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "EXECUTION_DISABLED"
    assert _outbox(p, intent["intent_id"]) == [("held", "create_payment_session")]


def test_a_second_release_with_another_key_changes_nothing(pilot):
    p = pilot
    buyer, intent = _held(p)
    _enable(p)
    assert _release(p, buyer, intent["intent_id"], "release-key-3").status_code == 200
    r = _release(p, buyer, intent["intent_id"], "release-key-4")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "ALREADY_QUEUED"


def test_the_same_key_cannot_release_a_different_request(pilot):
    p = pilot
    buyer, intent = _held(p)
    _enable(p)
    assert _release(p, buyer, intent["intent_id"], "release-key-5").status_code == 200
    buyer.reauth()
    r = buyer.post(
        f"/v1/m/pilot_buyer/payment-intents/{intent['intent_id']}/cancel", {}, key="release-key-5"
    )
    assert r.status_code == 409 and r.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_concurrent_releases_queue_exactly_once(pilot):
    p = pilot
    _, intent = _held(p)
    _enable(p)
    from tests.integration.conftest import Actor

    actors = [Actor(p.app, p.buyer_login, p.seeded.passwords[p.buyer_login]) for _ in range(4)]
    barrier = threading.Barrier(4)
    codes: list[int] = []

    def go(n):
        barrier.wait()
        r = actors[n].post(
            f"/v1/m/pilot_buyer/payment-intents/{intent['intent_id']}/release",
            {},
            key=f"release-concurrent-{n}",
        )
        codes.append(r.status_code)

    ts = [threading.Thread(target=go, args=(n,)) for n in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(codes) == [200, 409, 409, 409]
    with p.engine.connect() as c:
        n = c.execute(
            select(func.count())
            .select_from(payment_state_event)
            .where(payment_state_event.c.reason == "RELEASED_BY_PAYER")
        ).scalar()
    assert n == 1 and _outbox(p, intent["intent_id"]) == [("pending", "create_payment_session")]


def _refused(p, buyer, intent, code, key):
    r = _release(p, buyer, intent["intent_id"], key)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == code
    assert _outbox(p, intent["intent_id"]) == [("held", "create_payment_session")]
    assert p.mock.calls == []


def test_release_refused_after_the_approval_expired(pilot):
    p = pilot
    _, intent = _held(p)
    _enable(p)
    p.advance(hours=25)
    _refused(p, p.actor(p.buyer_login), intent, "AUTHORIZATION_EXPIRED", "release-exp-1")


def test_release_refused_when_the_approver_lost_authority(pilot):
    p = pilot
    _, intent = _held(p)
    _enable(p)
    with p.engine.begin() as c:  # a second approver acts; the original approver is revoked
        uid = auth.create_user(
            c,
            "pilot-approver2@elide.test",
            "Approver 2 (TEST)",
            "test-only-approver2-pw",
            is_test_identity=True,
        )
        auth.grant(c, uid, "pilot_buyer", "approver", at=p.now)
        auth.revoke_membership(
            c,
            p.seeded.users[p.buyer_login],
            "pilot_buyer",
            at=p.now,
            revoked_by=None,
            reason="test: approver revoked",
        )
    from tests.integration.conftest import Actor

    other = Actor(p.app, "pilot-approver2@elide.test", "test-only-approver2-pw")
    _refused(p, other, intent, "AUTHORIZATION_REVOKED", "release-rev-1")


def test_release_refused_when_financial_conditions_changed(pilot):
    p = pilot
    buyer, intent = _held(p)
    _enable(p)
    buyer.reauth()
    _confirm_reserve(buyer, "70")
    _refused(p, buyer, intent, "CONDITIONS_CHANGED", "release-cond-1")


def test_release_refused_when_the_reservation_was_altered(pilot):
    p = pilot
    buyer, intent = _held(p)
    _enable(p)
    with p.engine.begin() as c:
        release_commitment_in(c, intent["commitment_id"], p.now, "test: reservation lost")
    _refused(p, buyer, intent, "COMMITMENT_MISSING", "release-com-1")


def test_release_refused_when_the_provider_binding_changed(pilot):
    p = pilot
    buyer, intent = _held(p)
    _enable(p)
    with p.engine.begin() as c:
        aid = c.execute(
            select(provider_account.c.account_id).where(
                provider_account.c.merchant_id == "pilot_seller"
            )
        ).scalar_one()
        bindings.revoke(c, aid, reason="test: payee binding withdrawn", at=p.now)
    _refused(p, buyer, intent, "PROVIDER_NOT_CONNECTED", "release-bind-1")


def test_release_refused_for_a_job_that_was_attempted_before(pilot):
    p = pilot
    buyer, intent = _held(p)
    _enable(p)
    with p.engine.begin() as c:
        c.execute(
            update(outbox_message)
            .where(outbox_message.c.intent_id == intent["intent_id"])
            .values(attempts=1)
        )
    _refused(p, buyer, intent, "PREVIOUSLY_ATTEMPTED", "release-att-1")


def test_release_refused_when_another_intent_owns_the_invoice(pilot):
    p = pilot
    buyer, intent = _held(p)
    _enable(p)
    with p.engine.begin() as c:  # an irregular second intent (the product cannot create one)
        a = dict(
            c.execute(
                select(payment_authorization).where(
                    payment_authorization.c.authorization_id == intent["authorization_id"]
                )
            )
            .mappings()
            .one()
        )
        a.update(
            authorization_id="pau_competing_01",
            option_id=a["option_id"] + ":copy",
            binding_digest="c" * 64,
            operation_key="competing-authorization",
        )
        c.execute(payment_authorization.insert().values(**a))
        i = dict(
            c.execute(
                select(payment_intent).where(payment_intent.c.intent_id == intent["intent_id"])
            )
            .mappings()
            .one()
        )
        i.update(
            intent_id="pin_competing_01",
            authorization_id="pau_competing_01",
            operation_key="competing-intent",
            commitment_id="pi:pin_competing_01",
        )
        c.execute(payment_intent.insert().values(**i))
    _refused(p, buyer, intent, "COMPETING_INTENT", "release-comp-1")


def test_payee_and_outsiders_cannot_release(pilot):
    p = pilot
    _, intent = _held(p)
    _enable(p)
    seller = p.actor(p.seller_login)
    seller.reauth()
    for merchant in ("pilot_seller", "pilot_buyer"):
        r = seller.post(
            f"/v1/m/{merchant}/payment-intents/{intent['intent_id']}/release",
            {},
            key=f"release-payee-{merchant}",
        )
        assert r.status_code == 404
    assert _outbox(p, intent["intent_id"]) == [("held", "create_payment_session")]


def test_the_worker_revalidates_again_after_release(pilot):
    p = pilot
    buyer, intent = _held(p)
    _enable(p)
    assert _release(p, buyer, intent["intent_id"], "release-key-9").status_code == 200
    buyer.reauth()
    _confirm_reserve(buyer, "70")  # conditions change between release and dispatch
    p.work()
    assert p.intent(intent["intent_id"])["status"] == "cancelled"
    assert p.intent(intent["intent_id"])["status_reason"] == "CONDITIONS_CHANGED"
    assert p.mock.calls == []
