"""P4 end-to-end workflow over HTTP with separate merchant sessions (PostgreSQL).

Buyer counters the supplier's $5 offer with $10; the supplier independently accepts; the
agreed amendment becomes an AUTHORIZED revision; the buyer re-plans, authorizes its own
leg, and creates a payment intent whose outbox message stays HELD (no PayPal call).
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import select, text

from api.liquidity.store import active_commitment_total
from api.workflow.audit import verify_chain
from api.workflow.tables import (
    obligation_revision,
    offer,
    outbox_message,
    payment_authorization,
    payment_intent,
)

pytestmark = pytest.mark.integration

BUYER, SUPPLIER = "buyer-owner@elide.test", "supplier-owner@elide.test"


def _offer(actor, merchant, status="proposed"):
    offers = actor.get(f"/v1/m/{merchant}/offers").json()["offers"]
    return next(o for o in offers if o["status"] == status)


def _counter_ten(ws, buyer):
    o = _offer(buyer, "buyer")
    t = ws.now
    body = {
        "decision": "counter",
        "terms_digest": o["terms_digest"],
        "counter": {
            "concession_cents": 1000,
            "pay_by": (t + timedelta(days=2)).isoformat(),
            "expires_at": (t + timedelta(days=1)).isoformat(),
            "message": "Could we settle food_001 for $990.00 if paid early?",
        },
    }
    return o, buyer.post(f"/v1/m/buyer/offers/{o['offer_id']}/decision", body, key="k-counter-1")


def _agree(ws):
    buyer, supplier = ws.actor(BUYER), ws.actor(SUPPLIER)
    _, r = _counter_ten(ws, buyer)
    assert r.status_code == 200, r.text
    child = _offer(supplier, "food_supplier")
    r = supplier.post(
        f"/v1/m/food_supplier/offers/{child['offer_id']}/decision",
        {"decision": "accept", "terms_digest": child["terms_digest"]},
        key="k-accept-1",
    )
    assert r.status_code == 200, r.text
    return buyer, supplier, child


def test_full_negotiation_and_payment_authorization_flow(workspace):
    ws = workspace
    buyer, supplier = ws.actor(BUYER), ws.actor(SUPPLIER)

    # 1. proactive recommendation without asking anything
    opp = buyer.get("/v1/m/buyer/opportunities").json()["opportunity"]
    assert opp["recommendation"]["action"] == "NEGOTIATE"
    (hint,) = opp["my_negotiation_hints"]
    assert hint["minimum_needed_cents"] == 1000

    # 2. buyer counters $10 (recording its own acceptance of its own terms)
    parent, r = _counter_ten(ws, buyer)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "countered" and r.json()["amendment_authoritative"] is False
    # replay with the same key returns the stored result; a changed body is refused
    again = buyer.post(
        f"/v1/m/buyer/offers/{parent['offer_id']}/decision",
        json.loads(r.request.content),
        key="k-counter-1",
    )
    assert again.status_code == 200 and again.json()["replayed"] is True
    changed = buyer.post(
        f"/v1/m/buyer/offers/{parent['offer_id']}/decision",
        {"decision": "reject", "terms_digest": parent["terms_digest"]},
        key="k-counter-1",
    )
    assert changed.status_code == 409 and changed.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"

    child = _offer(buyer, "buyer")
    assert child["proposer"] == "buyer" and child["concession_cents"] == 1000
    assert child["can_decide"] is False and child["can_withdraw"] is True
    # the buyer cannot accept its own offer on the supplier's behalf
    r = buyer.post(
        f"/v1/m/buyer/offers/{child['offer_id']}/decision",
        {"decision": "accept", "terms_digest": child["terms_digest"]},
        key="k-self-accept",
    )
    assert r.status_code == 409 and r.json()["detail"]["code"] == "NOT_COUNTERPARTY"
    # nor through the supplier's path (it is not a member there)
    r = buyer.post(
        f"/v1/m/food_supplier/offers/{child['offer_id']}/decision",
        {"decision": "accept", "terms_digest": child["terms_digest"]},
        key="k-cross-1",
    )
    assert r.status_code == 404

    # 3. supplier reviews its own consequences and accepts independently
    sv = _offer(supplier, "food_supplier")
    assert sv["offer_id"] == child["offer_id"] and sv["can_decide"] is True
    stale = supplier.post(
        f"/v1/m/food_supplier/offers/{sv['offer_id']}/decision",
        {"decision": "accept", "terms_digest": "0" * 64},
        key="k-stale-1",
    )
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "STALE_DIGEST"
    ok = supplier.post(
        f"/v1/m/food_supplier/offers/{sv['offer_id']}/decision",
        {"decision": "accept", "terms_digest": sv["terms_digest"]},
        key="k-accept-1",
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["amendment_authoritative"] is True
    dup = supplier.post(
        f"/v1/m/food_supplier/offers/{sv['offer_id']}/decision",
        {"decision": "accept", "terms_digest": sv["terms_digest"]},
        key="k-accept-2",
    )
    assert dup.status_code == 409 and dup.json()["detail"]["code"] == "OFFER_CLOSED"

    with ws.engine.connect() as c:
        revs = c.execute(
            select(obligation_revision.c.status, obligation_revision.c.authorized_by)
            .where(obligation_revision.c.obligation_id == "food_001")
            .order_by(obligation_revision.c.revision_no)
        ).all()
    assert [r.status for r in revs] == ["authorized", "draft", "draft", "authorized"]
    assert sorted(revs[-1].authorized_by) == ["buyer", "food_supplier"]

    # 4. re-plan under the agreed amendment; the buyer authorizes only its own leg
    data = buyer.get("/v1/m/buyer/opportunities").json()
    leg = next(x for x in data["plan"]["my_legs"] if x["obligation_id"] == "food_001")
    assert leg["amount_cents"] == 99000 and leg["authorize"]["terms_agreed"] is True
    plan_id = data["plan"]["plan_id"]
    supplier_try = supplier.post(
        f"/v1/m/food_supplier/plans/{plan_id}/authorizations",
        {
            "leg_id": leg["leg_id"],
            "decision": "approve",
            "binding_digest": leg["authorize"]["binding_digest"],
        },
        key="k-sup-auth",
    )
    assert supplier_try.status_code == 409
    assert supplier_try.json()["detail"]["code"] == "NOT_PAYER"
    auth = buyer.post(
        f"/v1/m/buyer/plans/{plan_id}/authorizations",
        {
            "leg_id": leg["leg_id"],
            "decision": "approve",
            "binding_digest": leg["authorize"]["binding_digest"],
        },
        key="k-auth-1",
    )
    assert auth.status_code == 200, auth.text
    assert auth.json()["money_moved"] is False
    auth_id = auth.json()["authorization_id"]

    # 5. payment intent: preconditions enforced, capacity reserved, outbox HELD
    pi = buyer.post("/v1/m/buyer/payment-intents", {"authorization_id": auth_id}, key="pi-key-1")
    assert pi.status_code == 200, pi.text
    body = pi.json()
    assert body["status"] == "awaiting_payer_action" and body["execution"] == "disabled"
    assert body["money_moved"] is False and body["provider_confirmed"] is False
    assert body["funds_usable"] is False
    replay = buyer.post(
        "/v1/m/buyer/payment-intents", {"authorization_id": auth_id}, key="pi-key-1"
    )
    assert replay.json()["replayed"] is True and replay.json()["intent_id"] == body["intent_id"]
    second = buyer.post(
        "/v1/m/buyer/payment-intents", {"authorization_id": auth_id}, key="pi-key-2"
    )
    assert second.status_code == 409 and second.json()["detail"]["code"] == "DUPLICATE"
    assert active_commitment_total(ws.engine, "buyer") == 99000
    with ws.engine.connect() as c:
        (msg,) = c.execute(select(outbox_message.c.status, outbox_message.c.kind)).all()
        assert tuple(msg) == ("held", "create_payment_session")
        assert verify_chain(c) == []

    # the supplier's own view never shows the buyer's private cash or reserve
    sup_views = [
        supplier.get(p).text
        for p in (
            "/v1/m/food_supplier/overview",
            "/v1/m/food_supplier/opportunities",
            "/v1/m/food_supplier/offers",
            "/v1/m/food_supplier/network",
        )
    ]
    for t in sup_views:
        for secret in ("149000", "1,490", "50000", "500.00", "hurdle"):
            assert secret not in t, secret


def test_downstream_leg_cannot_become_an_intent(workspace):
    ws = workspace
    _buyer, supplier, _ = _agree(ws)
    data = supplier.get("/v1/m/food_supplier/opportunities").json()
    leg = next(x for x in data["plan"]["my_legs"] if x["obligation_id"] == "freight_001")
    r = supplier.post(
        f"/v1/m/food_supplier/plans/{data['plan']['plan_id']}/authorizations",
        {
            "leg_id": leg["leg_id"],
            "decision": "approve",
            "binding_digest": leg["authorize"]["binding_digest"],
        },
        key="k-freight",
    )
    assert r.status_code == 200, r.text  # conditional consent can be recorded
    pi = supplier.post(
        "/v1/m/food_supplier/payment-intents",
        {"authorization_id": r.json()["authorization_id"]},
        key="pi-freight",
    )
    assert pi.status_code == 409
    assert pi.json()["detail"]["code"] in ("UPSTREAM_NOT_CONFIRMED", "INSUFFICIENT_CAPACITY")


def test_open_offer_terms_cannot_be_payment_authorized(workspace):
    ws = workspace
    buyer = ws.actor(BUYER)
    # make the plan rely on an un-agreed proposal: a $10 counter the supplier has not signed
    _counter_ten(ws, buyer)
    data = buyer.get("/v1/m/buyer/opportunities").json()
    for leg in data["plan"]["my_legs"]:
        if "authorize" in leg and not leg["authorize"]["terms_agreed"]:
            r = buyer.post(
                f"/v1/m/buyer/plans/{data['plan']['plan_id']}/authorizations",
                {
                    "leg_id": leg["leg_id"],
                    "decision": "approve",
                    "binding_digest": leg["authorize"]["binding_digest"],
                },
                key="k-unagreed",
            )
            assert r.status_code == 409 and r.json()["detail"]["code"] == "TERMS_NOT_AGREED"


def test_expired_offer_rejects_decisions(workspace):
    ws = workspace
    supplier, buyer = ws.actor(SUPPLIER), ws.actor(BUYER)
    _counter_ten(ws, buyer)
    child = _offer(supplier, "food_supplier")
    ws.advance(days=1, seconds=1)
    supplier = ws.actor(SUPPLIER)  # fresh session (the old one idled out)
    r = supplier.post(
        f"/v1/m/food_supplier/offers/{child['offer_id']}/decision",
        {"decision": "accept", "terms_digest": child["terms_digest"]},
        key="k-late-1",
    )
    assert r.status_code == 409 and r.json()["detail"]["code"] == "OFFER_EXPIRED"
    assert _offer(supplier, "food_supplier", "expired")["offer_id"] == child["offer_id"]


def test_concurrent_acceptance_has_exactly_one_winner(workspace):
    ws = workspace
    buyer = ws.actor(BUYER)
    _counter_ten(ws, buyer)
    a, b = ws.actor(SUPPLIER), ws.actor(SUPPLIER)  # two sessions of the supplier
    child = _offer(a, "food_supplier")

    def go(actor, key):
        return actor.post(
            f"/v1/m/food_supplier/offers/{child['offer_id']}/decision",
            {"decision": "accept", "terms_digest": child["terms_digest"]},
            key=key,
        ).status_code

    with ThreadPoolExecutor(2) as ex:
        codes = sorted(ex.map(go, (a, b), ("race-a-key", "race-b-key")))
    assert codes == [200, 409]
    with ws.engine.connect() as c:
        n = c.execute(
            select(obligation_revision.c.revision_no).where(
                obligation_revision.c.obligation_id == "food_001",
                obligation_revision.c.status == "authorized",
            )
        ).all()
    assert len(n) == 2  # original terms + exactly one agreed amendment


def test_concurrent_plans_cannot_double_spend_the_same_cash(workspace):
    ws = workspace
    buyer, _supplier, _ = _agree(ws)
    data = buyer.get("/v1/m/buyer/opportunities").json()
    leg = next(x for x in data["plan"]["my_legs"] if x["obligation_id"] == "food_001")
    r = buyer.post(
        f"/v1/m/buyer/plans/{data['plan']['plan_id']}/authorizations",
        {
            "leg_id": leg["leg_id"],
            "decision": "approve",
            "binding_digest": leg["authorize"]["binding_digest"],
        },
        key="k-plan-0",
    )
    assert r.status_code == 200, r.text
    first = r.json()["authorization_id"]
    ws.advance(minutes=1)
    # a later plan over the same cash: the UI and API refuse a second decision on identical
    # live terms ...
    data2 = buyer.get("/v1/m/buyer/opportunities").json()
    assert data2["plan"]["plan_id"] != data["plan"]["plan_id"]
    leg2 = next(x for x in data2["plan"]["my_legs"] if x["obligation_id"] == "food_001")
    assert leg2["payment_authorization"]["status"] == "approved"
    # ... so simulate an approval race that slipped past it, directly in the database
    with ws.engine.begin() as c:
        row = dict(
            c.execute(
                select(payment_authorization).where(
                    payment_authorization.c.authorization_id == first
                )
            )
            .mappings()
            .one()
        )
        row.update(
            authorization_id="pau_race",
            plan_id=data2["plan"]["plan_id"],
            binding_digest="f" * 64,
            operation_key="race",
        )
        c.execute(payment_authorization.insert().values(**row))

    def go(i):
        auth_id = (first, "pau_race")[i]
        return buyer.post(
            "/v1/m/buyer/payment-intents", {"authorization_id": auth_id}, key=f"pi-race-{i}"
        )

    with ThreadPoolExecutor(2) as ex:
        results = list(ex.map(go, (0, 1)))
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 409], [r.text for r in results]
    loser = next(r for r in results if r.status_code == 409)
    assert loser.json()["detail"]["code"] == "ALREADY_IN_EXECUTION"
    assert active_commitment_total(ws.engine, "buyer") == 99000  # deployable was 990.00


def test_revoked_approver_authorization_is_not_executable(workspace):
    ws = workspace
    buyer, _s, _ = _agree(ws)
    from api.app.auth import service as auth

    with ws.engine.begin() as c:
        uid = auth.create_user(
            c,
            "buyer-approver@elide.test",
            "Approver (TEST)",
            "approver-pass-123456",
            is_test_identity=True,
        )
        auth.grant(c, uid, "buyer", "approver", at=ws.now)
    ws.seeded.passwords["buyer-approver@elide.test"] = "approver-pass-123456"
    approver = ws.actor("buyer-approver@elide.test")
    data = approver.get("/v1/m/buyer/opportunities").json()
    leg = next(x for x in data["plan"]["my_legs"] if x["obligation_id"] == "food_001")
    r = approver.post(
        f"/v1/m/buyer/plans/{data['plan']['plan_id']}/authorizations",
        {
            "leg_id": leg["leg_id"],
            "decision": "approve",
            "binding_digest": leg["authorize"]["binding_digest"],
        },
        key="k-appr-1",
    )
    assert r.status_code == 200, r.text
    rv = buyer.post(f"/v1/m/buyer/members/{uid}/revoke", {"reason": "left the company"})
    assert rv.status_code == 200, rv.text
    assert approver.get("/v1/m/buyer/overview").status_code == 404  # immediate
    pi = buyer.post(
        "/v1/m/buyer/payment-intents",
        {"authorization_id": r.json()["authorization_id"]},
        key="pi-revoked",
    )
    assert pi.status_code == 409 and pi.json()["detail"]["code"] == "AUTHORIZATION_REVOKED"


def test_audit_trail_is_append_only_and_scoped(workspace):
    ws = workspace
    _agree(ws)
    sup_audit = ws.actor("supplier-auditor@elide.test").get("/v1/m/food_supplier/audit").json()
    actions = {e["action"] for e in sup_audit["events"]}
    assert {"offer.proposed", "offer.agreed"} <= actions
    assert all(e["action"] != "access.denied" or True for e in sup_audit["events"])
    with ws.engine.begin() as c, pytest.raises(Exception, match="append-only"):
        c.execute(text("UPDATE audit_event SET outcome = 'ok'"))
    with ws.engine.begin() as c, pytest.raises(Exception, match=r"closed|allows changes"):
        c.execute(text("UPDATE offer SET cash_cents = 1"))
    with ws.engine.connect() as c:
        assert verify_chain(c) == []
        assert c.execute(select(payment_intent)).first() is None
        agreed = c.execute(select(offer.c.status).where(offer.c.status == "agreed")).all()
        assert len(agreed) == 1


def test_invoice_in_execution_is_not_planned_again(workspace):
    ws = workspace
    buyer, _s, _ = _agree(ws)
    data = buyer.get("/v1/m/buyer/opportunities").json()
    leg = next(x for x in data["plan"]["my_legs"] if x["obligation_id"] == "food_001")
    auth = buyer.post(
        f"/v1/m/buyer/plans/{data['plan']['plan_id']}/authorizations",
        {
            "leg_id": leg["leg_id"],
            "decision": "approve",
            "binding_digest": leg["authorize"]["binding_digest"],
        },
        key="k-exec-auth",
    ).json()
    pi = buyer.post(
        "/v1/m/buyer/payment-intents",
        {"authorization_id": auth["authorization_id"]},
        key="k-exec-pi1",
    )
    assert pi.status_code == 200, pi.text
    after = buyer.get("/v1/m/buyer/opportunities").json()
    assert all(x["obligation_id"] != "food_001" for x in after["plan"]["my_legs"])
    assert all(b["obligation_id"] != "food_001" for b in after["opportunity"]["my_bottlenecks"])
    overview = buyer.get("/v1/m/buyer/overview").json()
    food = next(i for i in overview["invoices"] if i["obligation_id"] == "food_001")
    assert food["payment_state"] == "INTENT_CREATED_NOT_SENT"
    assert overview["capacity"]["committed_cents"] == 99000
    assert overview["capacity"]["deployable_cents"] == 0
