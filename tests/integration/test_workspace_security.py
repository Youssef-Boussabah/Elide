"""P4 security contracts over HTTP (PostgreSQL): sessions, CSRF, rate limits, tenant
isolation, roles, step-up, webhooks, policy confirmation and multi-instance metering."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from api.accounting.tables import provider_event_inbox
from api.ai.spend import (
    BudgetExceededError,
    ReservationConflictError,
    SpendCapExceededError,
    UnknownReservationError,
)
from api.ai.spend_pg import PgSpendGuard
from api.workflow.tables import audit_event, outbox_message

pytestmark = pytest.mark.integration

BUYER, SUPPLIER = "buyer-owner@elide.test", "supplier-owner@elide.test"
MERCHANTS = ("buyer", "food_supplier", "carrier", "mechanic")


# --- sessions ----------------------------------------------------------------------------
def test_login_sets_a_hardened_cookie_and_labels_test_identities(workspace):
    c = TestClient(workspace.app)
    pw = workspace.seeded.passwords[BUYER]
    r = c.post("/v1/auth/login", json={"login": BUYER, "password": pw})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
    body = r.json()
    assert body["user"]["identity_label"] == "TEST IDENTITY"
    assert r.headers["cache-control"] == "no-store"
    raw = r.text
    assert pw not in raw and "argon2" not in raw and "password_hash" not in raw
    token = c.cookies.get("elide_session")
    assert token and token not in raw


def test_bad_credentials_are_generic_and_rate_limited(workspace):
    c = TestClient(workspace.app)
    wrong = c.post("/v1/auth/login", json={"login": BUYER, "password": "x" * 14})
    unknown = c.post("/v1/auth/login", json={"login": "nobody@elide.test", "password": "x" * 14})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    for _ in range(4):
        c.post("/v1/auth/login", json={"login": BUYER, "password": "y" * 14})
    blocked = c.post(
        "/v1/auth/login", json={"login": BUYER, "password": workspace.seeded.passwords[BUYER]}
    )
    assert blocked.status_code == 429  # even the right password, until the window passes
    workspace.advance(minutes=16)
    assert workspace.actor(BUYER).session["user"]["login"] == BUYER


def test_csrf_and_origin_are_enforced(workspace):
    b = workspace.actor(BUYER)
    no_token = b.post("/v1/auth/reauth", {"password": b.password}, csrf=False)
    assert no_token.status_code == 403 and no_token.json()["detail"]["code"] == "CSRF_FAILED"
    bad = b.post(
        "/v1/auth/reauth", {"password": b.password}, headers={"X-CSRF-Token": "f" * 64}, csrf=False
    )
    assert bad.status_code == 403
    evil = b.post(
        "/v1/auth/reauth", {"password": b.password}, headers={"Origin": "https://evil.example"}
    )
    assert evil.status_code == 403 and evil.json()["detail"]["code"] == "ORIGIN_REJECTED"
    login_csrf = TestClient(workspace.app).post(
        "/v1/auth/login",
        json={"login": BUYER, "password": b.password},
        headers={"Origin": "https://evil.example"},
    )
    assert login_csrf.status_code == 403
    same_tab = b.get("/v1/auth/session").json()["csrf_token"]
    assert same_tab == b.csrf  # every tab of one session derives the same token


def test_idle_and_absolute_expiry_and_logout(workspace):
    b = workspace.actor(BUYER)
    workspace.advance(minutes=31)
    assert b.get("/v1/m/buyer/overview").status_code == 401
    b2 = workspace.actor(BUYER)
    for _ in range(16):  # keep it active: idle window keeps moving, absolute does not
        workspace.advance(minutes=29)
        assert b2.get("/v1/auth/session").status_code == 200
    workspace.advance(minutes=29)
    assert b2.get("/v1/auth/session").status_code == 401  # past 8 h absolute
    b3 = workspace.actor(BUYER)
    assert b3.post("/v1/auth/logout").status_code == 200
    assert b3.get("/v1/auth/session").status_code == 401


def test_a_new_login_replaces_the_old_session(workspace):
    b = workspace.actor(BUYER)
    old = b.client.cookies.get("elide_session")
    r = b.client.post("/v1/auth/login", json={"login": BUYER, "password": b.password})
    assert r.status_code == 200
    stale = TestClient(workspace.app, cookies={"elide_session": old})
    assert stale.get("/v1/auth/session").status_code == 401


# --- tenant isolation and roles --------------------------------------------------------------
ROUTES = ("overview", "opportunities", "network", "offers", "policy", "payment-intents")


@pytest.mark.parametrize("login,own", [(BUYER, "buyer"), ("carrier-owner@elide.test", "carrier")])
def test_cross_tenant_reads_look_nonexistent(workspace, login, own):
    a = workspace.actor(login)
    missing = a.get("/v1/m/no_such_merchant/overview")
    for m in MERCHANTS:
        for route in ROUTES:
            r = a.get(f"/v1/m/{m}/{route}")
            if m == own:
                assert r.status_code == 200, (m, route, r.text)
            else:
                assert r.status_code == 404, (m, route)
                assert r.json() == missing.json()
    with workspace.engine.connect() as c:
        denied = c.execute(
            select(func.count())
            .select_from(audit_event)
            .where(audit_event.c.action == "access.denied", audit_event.c.merchant_id.is_(None))
        ).scalar_one()
    assert denied >= 3 * len(ROUTES)  # recorded in the security log, not in a merchant's


def test_invoice_and_plan_objects_are_party_scoped(workspace):
    buyer, mech = workspace.actor(BUYER), workspace.actor("mechanic-owner@elide.test")
    assert buyer.get("/v1/m/buyer/invoices/food_001").status_code == 200
    assert buyer.get("/v1/m/buyer/invoices/repair_001").status_code == 404  # not a party
    assert buyer.get("/v1/m/buyer/invoices/does_not_exist").status_code == 404
    plan_id = buyer.get("/v1/m/buyer/opportunities").json()["plan"]["plan_id"]
    view = mech.get(f"/v1/m/mechanic/plans/{plan_id}")
    assert view.status_code in (200, 404)
    if view.status_code == 200:  # mechanic is downstream: sees only its own legs
        assert all("mechanic" in (x["payer"], x["payee"]) for x in view.json()["my_legs"])
        assert "food_001" not in view.text and "149000" not in view.text


def test_role_permissions(workspace):
    viewer = workspace.actor("buyer-viewer@elide.test")
    auditor = workspace.actor("supplier-auditor@elide.test")
    assert viewer.get("/v1/m/buyer/overview").status_code == 200
    assert viewer.get("/v1/m/buyer/audit").status_code == 403
    r = viewer.post(
        "/v1/m/buyer/offers",
        {
            "obligation_id": "food_001",
            "concession_cents": 1,
            "pay_by": "2026-11-13T00:00:00Z",
            "expires_at": "2026-11-12T00:00:00Z",
        },
        key="viewer-offer",
    )
    assert r.status_code == 403
    assert auditor.get("/v1/m/food_supplier/audit").status_code == 200
    offers = auditor.get("/v1/m/food_supplier/offers").json()["offers"]
    r = auditor.post(
        f"/v1/m/food_supplier/offers/{offers[0]['offer_id']}/decision",
        {"decision": "withdraw", "terms_digest": offers[0]["terms_digest"]},
        key="auditor-wd",
    )
    assert r.status_code == 403
    assert auditor.get("/v1/m/food_supplier/members").status_code == 403


def test_step_up_is_required_for_decisions(workspace):
    s = workspace.actor(SUPPLIER)
    workspace.advance(minutes=11)
    assert s.get("/v1/auth/session").json()["session"]["step_up_valid"] is False
    o = s.get("/v1/m/food_supplier/offers").json()["offers"][0]
    body = {"decision": "withdraw", "terms_digest": o["terms_digest"]}
    r = s.post(f"/v1/m/food_supplier/offers/{o['offer_id']}/decision", body, key="stepup-1")
    assert r.status_code == 401 and r.json()["detail"]["code"] == "REAUTH_REQUIRED"
    bad = s.post("/v1/auth/reauth", {"password": "wrong-password-123"})
    assert bad.status_code == 401
    s.reauth()
    r = s.post(f"/v1/m/food_supplier/offers/{o['offer_id']}/decision", body, key="stepup-1")
    assert r.status_code == 200 and r.json()["status"] == "withdrawn"


def test_mutations_need_an_idempotency_key(workspace):
    s = workspace.actor(SUPPLIER)
    o = s.get("/v1/m/food_supplier/offers").json()["offers"][0]
    r = s.post(
        f"/v1/m/food_supplier/offers/{o['offer_id']}/decision",
        {"decision": "withdraw", "terms_digest": o["terms_digest"]},
    )
    assert r.status_code == 422 and r.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_offer_validation_refuses_credit_extension_and_stray_amounts(workspace):
    b = workspace.actor(BUYER)
    o = b.get("/v1/m/buyer/offers").json()["offers"][0]
    now = workspace.now

    def counter(**kw):
        terms = {
            "concession_cents": 1000,
            "pay_by": (now + timedelta(days=2)).isoformat(),
            "expires_at": (now + timedelta(days=1)).isoformat(),
            **kw,
        }
        return b.post(
            f"/v1/m/buyer/offers/{o['offer_id']}/decision",
            {"decision": "counter", "terms_digest": o["terms_digest"], "counter": terms},
            key=f"val-{abs(hash(json.dumps(kw, default=str)))}",
        )

    late = counter(pay_by=(now + timedelta(days=60)).isoformat())
    assert late.status_code == 422 and late.json()["detail"]["code"] == "PAY_BY_OUT_OF_RANGE"
    stray = counter(message="We usually keep $500.00 aside, so $10.00 off please")
    assert stray.status_code == 422
    assert stray.json()["detail"]["code"] == "MESSAGE_AMOUNT_MISMATCH"
    assert counter(concession_cents=100000).status_code == 422


# --- policy ------------------------------------------------------------------------------
def test_policy_draft_and_confirmation(workspace):
    b = workspace.actor(BUYER)
    text = "Keep $600 protected at all times."
    model = b.post("/v1/m/buyer/policy/drafts", {"text": text})
    assert model.status_code == 200 and model.json()["status"] == "MODEL_UNAVAILABLE"
    extraction = {
        "reserve_floor": {"amount": "600", "currency": "$", "quote": "Keep $600 protected"},
        "max_early_commitment": None,
        "min_early_discount": None,
        "min_net_benefit": None,
        "fee_ceiling": None,
        "allowed_counterparties": [],
        "prohibited_counterparties": [],
        "policy_valid_until": None,
        "offer_response_deadline": None,
        "participation": "unspecified",
        "approval_preference": "unspecified",
        "ambiguities": [],
        "refused_requests": [],
        "summary": "Keep a larger reserve.",
    }
    d = b.post("/v1/m/buyer/policy/drafts", {"text": text, "extraction": extraction}).json()
    assert d["status"] == "READY_FOR_CONFIRMATION" and d["active"] is False
    wrong = b.post(f"/v1/m/buyer/policy/drafts/{d['draft_id']}/confirm", {"digest": "0" * 64})
    assert wrong.status_code == 409 and wrong.json()["detail"]["code"] == "STALE_DIGEST"
    other = workspace.actor(SUPPLIER).post(
        f"/v1/m/food_supplier/policy/drafts/{d['draft_id']}/confirm", {"digest": d["digest"]}
    )
    assert other.status_code == 404  # another merchant's draft looks nonexistent
    ok = b.post(f"/v1/m/buyer/policy/drafts/{d['draft_id']}/confirm", {"digest": d["digest"]})
    assert ok.status_code == 200, ok.text
    conf = ok.json()["confirmed"]
    assert conf["revision_no"] == 2 and conf["rules"]["reserve_floor_cents"] == 60000
    again = b.post(f"/v1/m/buyer/policy/drafts/{d['draft_id']}/confirm", {"digest": d["digest"]})
    assert again.status_code == 409
    cap = b.get("/v1/m/buyer/overview").json()["capacity"]
    assert cap["basis"] == "SYNTHETIC" and cap["observed_at"] and cap["valid_until"]


# --- webhooks ----------------------------------------------------------------------------
HEADERS = {
    "paypal-transmission-id": "t-1",
    "paypal-transmission-time": "2026-11-11T18:00:00Z",
    "paypal-transmission-sig": "sig",
    "paypal-cert-url": "https://api.sandbox.paypal.com/cert",
    "paypal-auth-algo": "SHA256withRSA",
}


class _Verifier:
    configured = True

    def verify(self, headers, body):
        return headers.get("paypal-transmission-sig") == "sig"


def _event(eid="WH-TEST-0001", status="PAID"):
    return json.dumps(
        {"id": eid, "event_type": "INVOICING.INVOICE.PAID", "resource": {"status": status}}
    )


def test_webhooks_need_a_verifier_and_are_deduplicated(workspace):
    c = TestClient(workspace.app)
    url = "/v1/provider/paypal/webhook"
    assert c.post(url, content=_event(), headers=HEADERS).status_code == 503
    assert c.post(url, content="{not json", headers=HEADERS).status_code == 422
    assert c.post(url, content=_event(), headers={}).status_code == 422
    workspace.app.state.webhook_verifier = _Verifier()
    forged = c.post(url, content=_event(), headers={**HEADERS, "paypal-transmission-sig": "x"})
    assert forged.status_code == 400
    first = c.post(url, content=_event(), headers=HEADERS)
    assert first.status_code == 202 and first.json()["status"] == "accepted"
    dup = c.post(url, content=_event(), headers=HEADERS)
    assert dup.status_code == 200 and dup.json()["status"] == "duplicate"
    changed = c.post(url, content=_event(status="REFUNDED"), headers=HEADERS)
    assert changed.status_code == 409
    with workspace.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(provider_event_inbox)).scalar() == 1
        kinds = conn.execute(select(outbox_message.c.kind, outbox_message.c.status)).all()
    assert [tuple(k) for k in kinds] == [("provider_fetch", "held")]


# --- LLM metering across instances ---------------------------------------------------------
def test_pg_spend_guard_is_atomic_idempotent_and_flags_overage(workspace):
    engine = workspace.engine
    PgSpendGuard(engine, "p4-test", Decimal("0.010"))
    guards = [PgSpendGuard(engine, "p4-test") for _ in range(16)]  # 16 "instances"

    def take(g):
        try:
            return g.reserve(Decimal("0.004"))
        except SpendCapExceededError:
            return None

    with ThreadPoolExecutor(16) as ex:
        got = [r for r in ex.map(take, guards) if r is not None]
    assert len(got) == 2  # 2 x 0.004 <= 0.010 < 3 x 0.004
    g = guards[0]
    g.commit(got[0], cost=Decimal("0.001"), model="m", prompt_version="v", tokens=(1, 1))
    g.commit(got[0], cost=Decimal("0.001"), model="m", prompt_version="v", tokens=(1, 1))
    with pytest.raises(ReservationConflictError):
        g.commit(got[0], cost=Decimal("0.002"), model="m", prompt_version="v", tokens=(1, 1))
    with pytest.raises(ReservationConflictError):
        g.release(got[0])
    from api.ai.spend import Reservation

    with pytest.raises(UnknownReservationError):
        g.release(Reservation("00000000-0000-0000-0000-000000000000", Decimal("0.001")))
    # actual usage above the estimate pushes committed over the cap: recorded, then blocked
    g.commit(got[1], cost=Decimal("0.0125"), model="m", prompt_version="v", tokens=(9, 9))
    st = g.state()
    assert st["exceeded"] and Decimal(st["overage_usd"]) == Decimal("0.0035")
    with pytest.raises(BudgetExceededError):
        g.reserve(Decimal("0.000001"))
    g.acknowledge_overage("provider usage export reconciled; cap raised separately")
    assert g.state()["acknowledged"] is True
