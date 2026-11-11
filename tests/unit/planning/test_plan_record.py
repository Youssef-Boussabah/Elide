"""Structured, versioned, deterministic plan record; per-merchant views; tamper checks."""

from __future__ import annotations

import copy
import json

from api.planning.plan import (
    PLAN_SCHEMA_VERSION,
    build_plan,
    merchant_view,
    verify_plan_record,
)
from fixtures import planning as fx


def test_plan_record_is_deterministic_and_versioned():
    a = build_plan(fx.network_two_candidates())
    b = build_plan(fx.network_two_candidates())
    ja = json.dumps(a, sort_keys=True)
    assert ja == json.dumps(b, sort_keys=True)
    assert a["schema_version"] == PLAN_SCHEMA_VERSION
    assert a["plan_id"].startswith("plan_")
    assert len(a["input_digest"]) == 64


def test_plan_record_contents():
    p = build_plan(fx.network_two_candidates())
    assert p["status"] == "SIMULATED"
    assert p["execution"]["may_execute"] is False
    assert p["solver"]["status"] == "OPTIMAL"
    assert p["solver"]["name"] == "exact_slot_dp"
    assert p["currency"] == "USD"
    assert [leg["leg_id"] for leg in p["selected_legs"]] == [
        "food_001:r2:offer",
        "freight_001:r1",
        "repair_001:r1",
    ]
    food, freight, repair = p["selected_legs"]
    assert food["status"] == "proposed" and food["depends_on"] == []
    assert freight["status"] == "conditional" and freight["depends_on"] == ["food_001:r2:offer"]
    assert repair["depends_on"] == ["freight_001:r1"]
    assert p["required_initial_capacity"] == [
        {"merchant_id": "buyer", "cents": 99000, "evidence_id": "ev_buyer_a"}
    ]
    assert p["gross_flow_cents"] == 269000
    assert p["downstream_face_cents"] == 170000
    inc = p["incremental_on_time_face_cents"]
    assert inc["vs_B0"] == "270000" and inc["vs_B3"] == "85000"
    assert {b["baseline_id"] for b in p["baselines"]} == {"B0", "B1", "B2", "B3", "B4", "B5"}
    rejected = {r["leg_id"]: r["code"] for r in p["rejected_legs"]}
    assert rejected["pack_001:r2:offer"] == "CAPACITY"
    assert "SYNTHETIC_NETWORK" in p["warnings"]
    assert "DOWNSTREAM_NOT_PROVIDER_VERIFIED" in p["warnings"]
    approvals = {(a["merchant_id"], a["action"]) for a in p["required_approvals"]}
    assert ("buyer", "accept_offer") in approvals
    assert ("food_supplier", "authorize_payment") in approvals
    assert all(a["basis"] == "synthetic_assumption" for a in p["required_approvals"])
    assert p["expires_at"] <= "2026-11-10T16:05:00+00:00"  # offer deadline binds the plan
    assert any(b["kind"] == "OFFER_EXPIRY" for b in p["binding_constraints"])
    names = {alt["name"] for alt in p["alternatives"]}
    assert {"network", "focal_merchant", "no_action"} <= names


def test_infeasible_target_plan_lists_reasons():
    p = build_plan(fx.network_reserve_shortfall(), required=frozenset({"freight_001"}))
    assert p["solver"]["status"] == "INFEASIBLE"
    assert p["selected_legs"] == []
    assert any("short by USD 60.00" in r for r in p["infeasibility_reasons"])
    assert p["minimum_liquidity"]["hypothetical_cents"] == {"food_supplier": 6000}
    assert "HYPOTHETICAL" in p["minimum_liquidity"]["label"]


def test_merchant_view_hides_other_merchants_private_data():
    p = build_plan(fx.network_two_candidates())
    carrier = merchant_view(p, "carrier")
    text = json.dumps(carrier)
    assert "buyer" not in text  # not a counterparty of the carrier
    assert "50000" not in text  # buyer reserve
    assert [leg["leg_id"] for leg in carrier["my_legs"]] == ["freight_001:r1", "repair_001:r1"]
    assert set(carrier["my_benefit"]) >= {"net_benefit", "fees_paid"}
    buyer = merchant_view(p, "buyer")
    btext = json.dumps(buyer)
    assert "mechanic" not in btext and "carrier" not in btext
    assert buyer["downstream_legs_count"] == 2


def test_record_verifier_accepts_the_plan_and_rejects_tampering():
    net = fx.network_two_candidates()
    p = build_plan(net)
    assert verify_plan_record(net, p) == []
    t = copy.deepcopy(p)
    t["selected_legs"][1]["slot"] = 1
    t["selected_legs"][2]["slot"] = 1  # repair in the same slot as its funding receipt
    assert verify_plan_record(net, t)
    t = copy.deepcopy(p)
    t["selected_legs"][0]["amount_cents"] = 98000
    assert verify_plan_record(net, t)
    t = copy.deepcopy(p)
    t["required_initial_capacity"][0]["cents"] = 50000
    assert verify_plan_record(net, t)
    t = copy.deepcopy(p)
    t["input_digest"] = "0" * 64
    assert verify_plan_record(net, t)


def test_negotiated_plan_lists_both_signatures():
    p = build_plan(fx.network_negotiation(), allow_proposals=True)
    approvals = {(a["merchant_id"], a["action"]) for a in p["required_approvals"]}
    assert {("buyer", "sign_terms"), ("food_supplier", "sign_terms")} <= approvals
    assert "NEGOTIATED_CHANGE_ASSUMED_APPROVED" in p["warnings"]
    assert p["incremental_on_time_face_cents"]["vs_B4"] == "270000"


def test_infeasibility_reasons_say_why_funds_count_for_zero():
    held = build_plan(fx.network_held_receipt(), required=frozenset({"freight_001"}))
    assert any("counts for zero" in r and "held" in r for r in held["infeasibility_reasons"])
    cad = build_plan(fx.network_cad_settlement(), required=frozenset({"freight_001"}))
    assert any("CAD 1306.79 is currency-blocked" in r for r in cad["infeasibility_reasons"])
