"""Plan-record tampering: every financially important field must be re-derivable.

Each mutation is checked twice: as-is (the stale plan id should already expose it) and
with the plan id recomputed by an attacker (only the independent re-derivation from the
immutable network can catch it). A matching hash is NOT an authorization.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timedelta

import pytest

from api.planning.plan import build_plan, verify_plan_record
from fixtures import planning as fx


def rehash(rec: dict) -> dict:
    body = {k: v for k, v in rec.items() if k != "plan_id"}
    blob = json.dumps(body, sort_keys=True, separators=(",", ":"))
    rec["plan_id"] = "plan_" + hashlib.sha256(blob.encode()).hexdigest()[:16]
    return rec


def _leg(rec, leg_id):
    return next(x for x in rec["selected_legs"] if x["leg_id"] == leg_id)


def _later(iso: str) -> str:
    return (datetime.fromisoformat(iso) + timedelta(days=1)).isoformat()


TAMPERS: dict[str, Callable[[dict], None]] = {
    "plan_id": lambda r: r.__setitem__("plan_id", "plan_0000000000000000"),
    "buyer_benefit": lambda r: r["merchant_benefits"]["buyer"].__setitem__("net_benefit", 5000),
    "hide_supplier_loss": lambda r: r["merchant_benefits"]["food_supplier"].__setitem__(
        "net_benefit", 0
    ),
    "drop_required_approval": lambda r: r.__setitem__(
        "required_approvals",
        [a for a in r["required_approvals"] if a["merchant_id"] != "food_supplier"],
    ),
    "forge_recorded_decision": lambda r: r["required_approvals"][0].__setitem__(
        "basis", "recorded_decision"
    ),
    "drop_leg_approval": lambda r: _leg(r, "freight_001:r1").__setitem__("approvals", []),
    "extend_expiry": lambda r: r.__setitem__("expires_at", _later(r["expires_at"])),
    "fees": lambda r: r.__setitem__("fees_cents", 1),
    "baseline_b3": lambda r: next(
        b for b in r["baselines"] if b["baseline_id"] == "B3"
    ).__setitem__("expected_on_time_face_cents", "0"),
    "incremental_vs_b3": lambda r: r["incremental_on_time_face_cents"].__setitem__(
        "vs_B3", "270000"
    ),
    "leg_amount": lambda r: _leg(r, "food_001:r2:offer").__setitem__("amount_cents", 98000),
    "leg_proceeds": lambda r: _leg(r, "food_001:r2:offer").__setitem__("proceeds_cents", 99900),
    "feasible_slot_move": lambda r: _leg(r, "freight_001:r1").__setitem__("slot", 27),
    "slot_start": lambda r: _leg(r, "freight_001:r1").__setitem__(
        "slot_start", _later(_leg(r, "freight_001:r1")["slot_start"])
    ),
    "drop_dependency": lambda r: _leg(r, "freight_001:r1").__setitem__("depends_on", []),
    "leg_status": lambda r: _leg(r, "freight_001:r1").__setitem__("status", "proposed"),
    "on_time_flag": lambda r: _leg(r, "repair_001:r1").__setitem__("accelerated", True),
    "gross_flow": lambda r: r.__setitem__("gross_flow_cents", 170000),
    "downstream_face": lambda r: r.__setitem__("downstream_face_cents", 270000),
    "dollar_days": lambda r: r.__setitem__("liquidity_dollar_days", "0"),
    "may_execute": lambda r: r["execution"].__setitem__("may_execute", True),
    "drop_warning": lambda r: r["warnings"].remove("SIMULATED_NOT_EXECUTABLE"),
    "solver_value": lambda r: r["solver"]["value"].__setitem__(2, 999999),
    "rejected_leg_code": lambda r: r["rejected_legs"][0].__setitem__("code", "NOT_SELECTED"),
    "alternative": lambda r: r["alternatives"][1].__setitem__("focal_net_cents", 0),
    "binding_constraints": lambda r: r.__setitem__("binding_constraints", []),
    "discharged": lambda r: r["discharged"].__setitem__("cash_cents", 1),
}


@pytest.fixture(scope="module")
def plan_and_net():
    net = fx.network_two_candidates()
    return net, build_plan(net)


def test_untampered_plan_verifies(plan_and_net):
    net, p = plan_and_net
    assert verify_plan_record(net, p) == []


@pytest.mark.parametrize("name", sorted(TAMPERS))
def test_tampering_is_detected(plan_and_net, name):
    net, p = plan_and_net
    t = copy.deepcopy(p)
    TAMPERS[name](t)
    assert verify_plan_record(net, t), f"{name} escaped detection"


@pytest.mark.parametrize("name", sorted(set(TAMPERS) - {"plan_id"}))
def test_tampering_is_detected_even_with_a_recomputed_plan_id(plan_and_net, name):
    net, p = plan_and_net
    t = copy.deepcopy(p)
    TAMPERS[name](t)
    rehash(t)
    problems = verify_plan_record(net, t)
    assert problems, f"{name} escaped detection after re-hashing"
    assert not any("plan_id" in x for x in problems)  # the hash itself now matches


def test_a_matching_hash_is_not_an_authorization(plan_and_net):
    _net, p = plan_and_net
    assert p["execution"]["may_execute"] is False
    assert all(a["basis"] == "synthetic_assumption" for a in p["required_approvals"])
    assert "not an authorization" in p["integrity"]["note"]


def test_record_for_a_different_network_is_rejected(plan_and_net):
    _, p = plan_and_net
    assert verify_plan_record(fx.network_buyer_tradeoff(), p)
