"""Regressions for the P4 architectural review (written before the fixes).

Finding 1: a payment authorization must never outlive the conditions it was given under
(confirmed policy, effective reserve, participation, evidence basis, fee terms, invoice
revision, payee, due date, open balance), and every intent re-runs the independent
verifier against current data. Revocation racing a financial write must win.
Finding 2: aggregate disclosure is an explicit server-side decision applied to allowlisted
views, HTTP responses and model prompts alike.
Finding 3: viewing recommendations is read-only; durable plans are created explicitly.
"""

from __future__ import annotations

import re
import threading
import time
from datetime import timedelta

import pytest
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import DBAPIError

from api.ai.mock import ScriptedLLMAdapter
from api.domain.invoice import InvoiceRecord, Obligation, ObligationRevision, RevisionStatus
from api.domain.money import USD, Money
from api.liquidity.store import active_commitment_total, save_evidence
from api.workflow.plans import existing_authorization
from api.workflow.store import save_record
from api.workflow.tables import (
    network_participation,
    outbox_message,
    payment_authorization,
    payment_intent,
    plan_snapshot,
)
from fixtures.workspace_seed import NETWORK_ID, TZ, _prov, evidence
from tests.integration.test_workspace_flow import BUYER, SUPPLIER, _agree, _plan

pytestmark = pytest.mark.integration

POLICY_TEXT = "Keep $700 protected at all times. Only pay early for at least $10 off."


def _extraction(reserve="700"):
    return {
        "reserve_floor": {
            "amount": reserve,
            "currency": "$",
            "quote": f"Keep ${reserve} protected",
        },
        "max_early_commitment": None,
        "min_early_discount": {"amount": "10", "currency": "$", "quote": "at least $10 off"},
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
        "summary": "Reserve and minimum discount.",
    }


def _confirm_reserve(actor, reserve="700"):
    text_ = POLICY_TEXT.replace("700", reserve)
    d = actor.post("/v1/m/buyer/policy/drafts", {"text": text_, "extraction": _extraction(reserve)})
    assert d.status_code == 200 and d.json()["status"] == "READY_FOR_CONFIRMATION", d.text
    c = actor.post(
        f"/v1/m/buyer/policy/drafts/{d.json()['draft_id']}/confirm", {"digest": d.json()["digest"]}
    )
    assert c.status_code == 200, c.text


def _approve_food(ws, actor, key="rx-auth-1"):
    plan = _plan(actor, "buyer")["plan"]
    leg = next(x for x in plan["my_legs"] if x["obligation_id"] == "food_001")
    r = actor.post(
        f"/v1/m/buyer/plans/{plan['plan_id']}/authorizations",
        {
            "leg_id": leg["leg_id"],
            "decision": "approve",
            "binding_digest": leg["authorize"]["binding_digest"],
        },
        key=key,
    )
    assert r.status_code == 200, r.text
    return plan, leg, r.json()["authorization_id"]


def _intents(ws):
    with ws.engine.connect() as c:
        return c.execute(select(func.count()).select_from(payment_intent)).scalar_one()


# --- Finding 1 -----------------------------------------------------------------------------
def test_newer_reserve_policy_invalidates_an_earlier_authorization(workspace):
    ws = workspace
    buyer, _s, _ = _agree(ws)
    plan, leg, auth_id = _approve_food(ws, buyer)
    _confirm_reserve(buyer, "700")  # 1,490 - 700 = 790 < 990
    view = buyer.get(f"/v1/m/buyer/plans/{plan['plan_id']}").json()
    status = next(x for x in view["my_legs"] if x["leg_id"] == leg["leg_id"])
    assert status["payment_authorization"]["status"] == "needs_reapproval"
    pi = buyer.post("/v1/m/buyer/payment-intents", {"authorization_id": auth_id}, key="rx-pi-1")
    assert pi.status_code == 409 and pi.json()["detail"]["code"] == "CONDITIONS_CHANGED"
    assert _intents(ws) == 0 and active_commitment_total(ws.engine, "buyer") == 0


def test_capacity_and_verifier_are_rechecked_under_unchanged_conditions(workspace):
    ws = workspace
    buyer, _s, _ = _agree(ws)
    _p, _l, auth_id = _approve_food(ws, buyer)
    ws.advance(minutes=1)
    save_evidence(ws.engine, evidence("buyer", 140000, 50000, ws.now))  # 900 deployable
    pi = buyer.post("/v1/m/buyer/payment-intents", {"authorization_id": auth_id}, key="rx-pi-2")
    assert pi.status_code == 409
    assert pi.json()["detail"]["code"] in ("VERIFIER_REJECTED", "INSUFFICIENT_CAPACITY")
    assert _intents(ws) == 0


def test_capacity_reservation_enforces_the_confirmed_policy_floor(fresh_db):
    """Unit-level: the reservation itself honours a reserve floor above the evidence."""
    from api.liquidity.capacity import Commitment, InsufficientCapacityError
    from api.liquidity.store import reserve_capacity_in
    from fixtures.workspace_seed import seed

    engine, _ = fresh_db
    from datetime import UTC, datetime

    now = datetime(2026, 11, 11, 18, 0, tzinfo=UTC)
    seed(
        engine,
        now=now,
        passwords={
            k: "x" * 16 for k in __import__("fixtures.workspace_seed", fromlist=["USERS"]).USERS
        },
    )
    ev_id = f"ev_buyer_{int((now - timedelta(minutes=20)).timestamp())}"
    with engine.begin() as c, pytest.raises(InsufficientCapacityError):
        reserve_capacity_in(
            c,
            evidence_id=ev_id,
            at=now,
            allow_synthetic=True,
            commitment=Commitment("t1", "buyer", "p", Money(99000, USD)),
            min_protected_reserve_cents=70000,
        )
    with engine.begin() as c:
        reserve_capacity_in(
            c,
            evidence_id=ev_id,
            at=now,
            allow_synthetic=True,
            commitment=Commitment("t2", "buyer", "p", Money(79000, USD)),
            min_protected_reserve_cents=70000,
        )


def test_expired_authorization_is_not_shown_or_used_as_active(workspace):
    ws = workspace
    buyer, _s, _ = _agree(ws)
    plan, leg, auth_id = _approve_food(ws, buyer)
    ws.advance(hours=24, seconds=1)
    buyer = ws.actor(BUYER)
    view = buyer.get(f"/v1/m/buyer/plans/{plan['plan_id']}").json()
    st = next(x for x in view["my_legs"] if x["leg_id"] == leg["leg_id"])
    assert st["payment_authorization"]["status"] == "expired"
    pi = buyer.post("/v1/m/buyer/payment-intents", {"authorization_id": auth_id}, key="rx-pi-3")
    assert pi.status_code == 409 and pi.json()["detail"]["code"] == "AUTHORIZATION_EXPIRED"


def test_changed_payee_fee_or_amount_never_inherits_an_approval(workspace):
    ws = workspace
    buyer, _s, _ = _agree(ws)
    plan, leg, _auth = _approve_food(ws, buyer)
    with ws.engine.connect() as c:
        snap = c.execute(
            select(plan_snapshot.c.record).where(plan_snapshot.c.plan_id == plan["plan_id"])
        ).scalar_one()
        full = next(x for x in snap["selected_legs"] if x["leg_id"] == leg["leg_id"])
        row = c.execute(select(payment_authorization)).mappings().one()
        same = existing_authorization(c, full, row["conditions_digest"], ws.now)
        assert same is not None and same.authorization_id == row["authorization_id"]
        for change in (
            {"payee": "carrier"},
            {"payer_fee_cents": 100},
            {"amount_cents": 98000},
            {"revision_no": 3},
        ):
            assert (
                existing_authorization(c, {**full, **change}, row["conditions_digest"], ws.now)
                is None
            ), change
        assert existing_authorization(c, full, "0" * 64, ws.now) is None  # conditions moved


def _hold_revocation(ws, user_id, merchant):
    conn = ws.engine.connect()
    tx = conn.begin()
    conn.execute(
        text(
            "UPDATE membership SET revoked_at = :t, revoke_reason = 'race' "
            "WHERE user_id = :u AND merchant_id = :m AND revoked_at IS NULL"
        ),
        {"t": ws.now, "u": user_id, "m": merchant},
    )
    return conn, tx


def _run_blocked(fn):
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", fn()))
    t.start()
    time.sleep(1.0)  # let the request reach the membership lock
    return t, out


def test_revocation_committed_during_intent_creation_wins(workspace):
    ws = workspace
    buyer, _s, _ = _agree(ws)
    _p, _l, auth_id = _approve_food(ws, buyer)
    conn, tx = _hold_revocation(ws, ws.seeded.users[BUYER], "buyer")
    t, out = _run_blocked(
        lambda: buyer.post(
            "/v1/m/buyer/payment-intents", {"authorization_id": auth_id}, key="rx-race-pi"
        )
    )
    assert t.is_alive(), "intent creation must wait for the pending revocation"
    tx.commit()
    conn.close()
    t.join(30)
    assert out["r"].status_code in (404, 409), out["r"].text
    assert _intents(ws) == 0 and active_commitment_total(ws.engine, "buyer") == 0


def test_revocation_committed_during_approval_wins(workspace):
    ws = workspace
    buyer, _s, _ = _agree(ws)
    plan = _plan(buyer, "buyer")["plan"]
    leg = next(x for x in plan["my_legs"] if x["obligation_id"] == "food_001")
    conn, tx = _hold_revocation(ws, ws.seeded.users[BUYER], "buyer")
    t, out = _run_blocked(
        lambda: buyer.post(
            f"/v1/m/buyer/plans/{plan['plan_id']}/authorizations",
            {
                "leg_id": leg["leg_id"],
                "decision": "approve",
                "binding_digest": leg["authorize"]["binding_digest"],
            },
            key="rx-race-auth",
        )
    )
    assert t.is_alive()
    tx.commit()
    conn.close()
    t.join(30)
    assert out["r"].status_code in (404, 409), out["r"].text
    with ws.engine.connect() as c:
        assert c.execute(select(func.count()).select_from(payment_authorization)).scalar() == 0


def test_synthetic_evidence_never_gains_payment_authority(workspace):
    ws = workspace
    buyer, _s, _ = _agree(ws)
    _p, _l, auth_id = _approve_food(ws, buyer)
    ws.app.state.payments_execution_enabled = True  # even if execution were switched on
    pi = buyer.post("/v1/m/buyer/payment-intents", {"authorization_id": auth_id}, key="rx-syn")
    assert pi.status_code == 200, pi.text
    assert pi.json()["evidence_kind"] == "synthetic"
    with ws.engine.connect() as c:
        assert c.execute(select(outbox_message.c.status)).scalar_one() == "held"
    with ws.engine.begin() as c, pytest.raises(DBAPIError):
        c.execute(update(payment_intent).values(status="provider_pending"))
    with ws.engine.begin() as c, pytest.raises(DBAPIError):
        c.execute(update(outbox_message).values(status="pending"))


def test_production_refuses_synthetic_evidence(workspace, monkeypatch):
    ws = workspace
    buyer, _s, _ = _agree(ws)
    _p, _l, auth_id = _approve_food(ws, buyer)
    monkeypatch.setattr("api.app.workspace.synthetic_authority_allowed", lambda: False)
    pi = buyer.post("/v1/m/buyer/payment-intents", {"authorization_id": auth_id}, key="rx-prod")
    assert pi.status_code == 409 and pi.json()["detail"]["code"] == "SYNTHETIC_EVIDENCE"
    assert _intents(ws) == 0


# --- Finding 2 -----------------------------------------------------------------------------
HIDDEN = (
    "hidden_inv_9931",
    "hidden_vendor",
    "777777",
    "7,777.77",
    "43210",
    "432.10",
    "9876",
    "98.76",
)
NETWORK = (
    "1,700",
    "170000",
    "approvals_required",
    "downstream_legs_count",
    "other_legs_count",
    "other_bottlenecks_count",
    "more invoice face on time",
    "repair_001",
    "mechanic",
)
BUYER_PRIVATE = ("149000", "1,490", "50000", "hurdle")


def _adversarial(ws):
    """Distinctive private data plus a nonparticipating merchant (never in any view)."""
    from api.app.auth import service as auth

    with ws.engine.begin() as c:
        auth.create_merchant(c, "hidden_vendor", "Hidden Vendor (synthetic)", TZ, is_synthetic=True)
        t0 = ws.now - timedelta(minutes=30)
        ob = Obligation(
            "hidden_inv_9931",
            "carrier",
            "hidden_vendor",
            Money(777777, USD),
            t0,
            _prov(t0, "hidden"),
        )
        rev = ObligationRevision(
            obligation_id="hidden_inv_9931",
            revision_no=1,
            status=RevisionStatus.AUTHORIZED,
            due_at=t0 + timedelta(days=2),
            business_tz=TZ,
            concession=Money(0, USD),
            created_at=t0,
            provenance=_prov(t0, "hidden"),
        )
        save_record(c, NETWORK_ID, InvoiceRecord.open(ob, rev))
    save_evidence(ws.engine, evidence("carrier", 43210, 9876, ws.now - timedelta(minutes=1)))


def _capture(ws):
    adapter = ScriptedLLMAdapter(
        lambda *a: {"headline": "h", "explanation": "A simulation.", "cited_facts": ["F1"]}
    )
    ws.app.state.workspace_llm = adapter
    return adapter


def _surfaces(actor, merchant):
    texts = [actor.get(f"/v1/m/{merchant}/opportunities").text]
    created = actor.post(f"/v1/m/{merchant}/plans", {}, key=f"adv-{merchant}-{time.time_ns()}")
    if created.status_code == 200:
        texts.append(created.text)
        texts.append(actor.get(f"/v1/m/{merchant}/plans/{created.json()['plan']['plan_id']}").text)
    for route in ("overview", "network", "offers", "plans"):
        texts.append(actor.get(f"/v1/m/{merchant}/{route}").text)
    return texts


def _found(token, text_):
    """Numeric tokens must stand alone (not inside a hex digest or a longer number)."""
    if token.replace(",", "").replace(".", "").isdigit():
        return re.search(rf"(?<![0-9A-Za-z]){re.escape(token)}(?![0-9A-Za-z])", text_) is not None
    return token in text_


def _assert_clean(texts, forbidden, where):
    for t in texts:
        leaked = [s for s in forbidden if _found(s, t)]
        assert not leaked, f"{where} leaks {leaked}"


def _set_share(ws, merchant, value):
    with ws.engine.begin() as c:
        c.execute(
            update(network_participation)
            .where(network_participation.c.merchant_id == merchant)
            .values(share_aggregates=value)
        )


@pytest.mark.parametrize("case", ["consent_denied", "small_group"])
def test_withheld_aggregates_leak_nowhere(workspace, case):
    ws = workspace
    buyer, _s, _ = _agree(ws)
    _adversarial(ws)
    if case == "consent_denied":
        _set_share(ws, "carrier", False)
    else:  # mechanic leaves: two contributors remain, below the minimum group of three
        with ws.engine.begin() as c:
            c.execute(
                update(network_participation)
                .where(network_participation.c.merchant_id == "mechanic")
                .values(left_at=ws.now)
            )
    adapter = _capture(ws)
    texts = _surfaces(buyer, "buyer")
    opp = buyer.get("/v1/m/buyer/opportunities").json()
    assert opp["opportunity"]["aggregates"]["status"].startswith("WITHHELD")
    forbidden = HIDDEN + NETWORK + (("90000", "900.00") if case == "small_group" else ())
    _assert_clean(texts, forbidden, f"buyer HTTP ({case})")
    prompts = [c[1] + c[2] for c in adapter.calls]
    assert prompts, "the captured model context must be exercised"
    _assert_clean(prompts, forbidden, f"buyer model prompt ({case})")


def test_aggregates_are_shown_only_with_consent_and_group(workspace):
    ws = workspace
    buyer, _s, _ = _agree(ws)
    opp = buyer.get("/v1/m/buyer/opportunities").json()["opportunity"]
    assert opp["aggregates"]["status"] == "SHOWN"
    assert opp["comparison"]["incremental_vs_B0"].startswith("170000")


@pytest.mark.parametrize(
    "login,merchant",
    [
        (SUPPLIER, "food_supplier"),
        ("carrier-owner@elide.test", "carrier"),
        ("mechanic-owner@elide.test", "mechanic"),
    ],
)
def test_non_initiators_never_receive_network_data(workspace, login, merchant):
    """Another merchant's durable plan is always non-initiator; and with the buyer's
    aggregate consent withdrawn, each merchant's own analysis withholds aggregates too."""
    ws = workspace
    buyer, _s, _ = _agree(ws)
    _adversarial(ws)
    buyer_plan = _plan(buyer, "buyer")["plan"]["plan_id"]
    _set_share(ws, "buyer", False)
    adapter = _capture(ws)
    actor = ws.actor(login)
    texts = _surfaces(actor, merchant)
    other = actor.get(f"/v1/m/{merchant}/plans/{buyer_plan}")
    assert other.status_code == 200  # every merchant here is a participant of that plan
    texts.append(other.text)
    assert "other_legs_in_plan" not in other.text
    hidden = HIDDEN if merchant != "carrier" else ("hidden_vendor_never",)
    forbidden = hidden + NETWORK[:7] + BUYER_PRIVATE
    _assert_clean(texts, forbidden, merchant)
    _assert_clean([c[1] + c[2] for c in adapter.calls], forbidden, f"{merchant} prompt")


def test_nonparticipant_invoice_is_never_planned_or_shown(workspace):
    ws = workspace
    _agree(ws)
    _adversarial(ws)
    carrier = ws.actor("carrier-owner@elide.test")
    over = carrier.get("/v1/m/carrier/overview").json()
    # the carrier owes hidden_vendor: its own invoice is listed in its own overview only
    assert any(i["obligation_id"] == "hidden_inv_9931" for i in over["invoices"])
    plan = carrier.post("/v1/m/carrier/plans", {}, key="np-plan-1").json()["plan"]
    assert all(x["obligation_id"] != "hidden_inv_9931" for x in plan["my_legs"])


# --- Finding 3 -----------------------------------------------------------------------------
def test_viewing_recommendations_writes_nothing(workspace):
    ws = workspace
    buyer = ws.actor(BUYER)
    for _ in range(5):
        assert buyer.get("/v1/m/buyer/opportunities").status_code == 200
        ws.advance(seconds=30)
    with ws.engine.connect() as c:
        assert c.execute(select(func.count()).select_from(plan_snapshot)).scalar_one() == 0


def test_durable_plans_are_explicit_idempotent_and_bounded(workspace):
    ws = workspace
    buyer = ws.actor(BUYER)
    assert buyer.post("/v1/m/buyer/plans", {}, key="p-plan-1", csrf=False).status_code == 403
    assert buyer.post("/v1/m/buyer/plans", {}).status_code == 422  # Idempotency-Key required
    viewer = ws.actor("buyer-viewer@elide.test")
    assert viewer.post("/v1/m/buyer/plans", {}, key="p-plan-v").status_code == 403
    a = buyer.post("/v1/m/buyer/plans", {}, key="p-plan-1").json()
    assert a["reused"] is False
    replay = buyer.post("/v1/m/buyer/plans", {}, key="p-plan-1").json()
    assert replay["replayed"] is True and replay["plan"]["plan_id"] == a["plan"]["plan_id"]
    for i in range(5):  # time passes, nothing economic changes: no new immutable records
        ws.advance(minutes=1)
        b = buyer.post("/v1/m/buyer/plans", {}, key=f"p-plan-{i + 2}").json()
        assert b["reused"] is True and b["plan"]["plan_id"] == a["plan"]["plan_id"]
    with ws.engine.connect() as c:
        assert c.execute(select(func.count()).select_from(plan_snapshot)).scalar_one() == 1
    listing = buyer.get("/v1/m/buyer/plans").json()
    assert [p["plan_id"] for p in listing["plans"]] == [a["plan"]["plan_id"]]


def test_plan_creation_is_rate_limited(workspace, monkeypatch):
    ws = workspace
    monkeypatch.setattr("api.workflow.plans.NEW_SNAPSHOTS_PER_HOUR", 1)
    buyer = ws.actor(BUYER)
    assert buyer.post("/v1/m/buyer/plans", {}, key="rl-plan-1").status_code == 200
    _confirm_reserve(buyer, "600")  # economic inputs change -> a new snapshot is needed
    r = buyer.post("/v1/m/buyer/plans", {}, key="rl-plan-2")
    assert r.status_code == 429 and r.json()["detail"]["code"] == "RATE_LIMITED"


def test_inserting_a_snapshot_directly_is_still_append_only(workspace):
    ws = workspace
    buyer = ws.actor(BUYER)
    buyer.post("/v1/m/buyer/plans", {}, key="ao-plan-1")
    with ws.engine.begin() as c, pytest.raises(DBAPIError):
        c.execute(update(plan_snapshot).values(solver_status="X"))
    _ = insert


def test_unconfirmed_policy_is_flagged_and_never_assumed_acceptable(workspace):
    ws = workspace
    _agree(ws)
    carrier = ws.actor("carrier-owner@elide.test")  # the seed confirms no carrier policy
    opp = carrier.get("/v1/m/carrier/opportunities").json()["opportunity"]
    assert any(f["code"] == "POLICY_NOT_CONFIRMED" for f in opp["my_flags"])
    assert opp["recommendation"]["action"] not in ("RECOMMEND_PLAN", "REVIEW_AND_DECIDE")
