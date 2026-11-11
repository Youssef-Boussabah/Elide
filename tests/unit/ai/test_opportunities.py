"""Proactive opportunity analysis (deterministic, read-only, SYNTHETIC)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from api.ai.mock import ScriptedLLMAdapter
from api.app.demo_ai import get_adapter
from api.app.main import create_app
from api.opportunities.analysis import OPPORTUNITY_SCHEMA, analyze, facts, merchant_view
from api.planning.builder import InvoiceInput, build_network, synthetic_position
from api.planning.network import SYNTHETIC_ZERO_LAG, MerchantPolicy
from fixtures import planning as fx
from fixtures.canonical import invoice


def codes(report):
    return {(f["code"], f["merchant_id"]) for f in report["flags"]}


def test_two_candidates_needs_economic_review_and_reports_honest_comparisons():
    r = analyze(fx.network_two_candidates())
    assert r["schema_version"] == OPPORTUNITY_SCHEMA and r["status"] == "SIMULATED"
    # feasible and +$2,700 vs doing nothing, but the supplier's -$10 is an undeclared loss
    assert r["recommendation"]["action"] == "REVIEW_ECONOMICS"
    assert "not a network-aware improvement" in r["recommendation"]["reason"]  # B1 == B4
    d = r["decision"]
    assert d["feasible"] is True
    assert d["advantage"]["network_awareness_needed"] is False
    assert d["buyer_net_benefit_cents"] == 370
    assert d["participant_net_benefit_cents"]["food_supplier"] == -1000
    assert d["unjustified_modeled_losses"] == ["food_supplier"]
    assert d["consent"] == {"approvals_required": 4, "approvals_recorded": 0}
    assert any("no approval is recorded" in c for c in r["conditions"])
    assert any("no downstream funds are verified spendable" in c for c in r["conditions"])
    c = r["comparison"]
    assert (c["incremental_vs_B0"], c["incremental_vs_B1"], c["incremental_vs_B3"]) == (
        "270000",
        "0",
        "85000",
    )
    assert c["simple_rule_matches_plan"] is True  # earliest-due-first gets the same here
    reasons = {b["obligation_id"]: b["reason_code"] for b in r["bottlenecks"]}
    assert reasons == {
        "food_001": "UNLOCKED_BY_PLAN",
        "freight_001": "UNLOCKED_BY_PLAN",
        "repair_001": "UNLOCKED_BY_PLAN",
        "pack_001": "COMPETES_WITH_PLAN",
    }
    assert {
        ("PARTICIPANT_MODELED_LOSS", "food_supplier"),
        ("APPROVALS_OUTSTANDING", None),
        ("DOWNSTREAM_SIMULATED", None),
    } <= codes(r)
    assert r["negotiation_hints"] == []
    assert r["execution"]["may_execute"] is False


def test_negotiation_case_recommends_a_bounded_counteroffer():
    r = analyze(fx.network_negotiation())
    assert r["recommendation"]["action"] == "NEGOTIATE"
    (hint,) = [h for h in r["negotiation_hints"] if h["kind"] == "DISCOUNT_COUNTEROFFER"]
    assert (hint["current_offer_cents"], hint["minimum_needed_cents"]) == (500, 1000)
    assert any(h["kind"] == "PENDING_PROPOSAL" for h in r["negotiation_hints"])


@pytest.mark.parametrize(
    ("network", "reason", "flag"),
    [
        (fx.network_held_receipt, "FUNDS_UNAVAILABLE", ("FUNDS_UNAVAILABLE", "food_supplier")),
        (fx.network_cad_settlement, "FUNDS_UNAVAILABLE", ("FUNDS_UNAVAILABLE", "food_supplier")),
        (fx.network_reserve_shortfall, "CAPACITY", None),
    ],
)
def test_blocked_networks_say_no_action_with_the_reason(network, reason, flag):
    r = analyze(network())
    assert r["recommendation"]["action"] == "NO_ACTION"
    freight = next(b for b in r["bottlenecks"] if b["obligation_id"] == "freight_001")
    assert freight["reason_code"] == reason
    if flag:
        assert flag in codes(r)


def test_shortfall_reason_is_the_exact_deterministic_gap():
    r = analyze(fx.network_reserve_shortfall())
    freight = next(b for b in r["bottlenecks"] if b["obligation_id"] == "freight_001")
    assert "short by USD 60.00" in freight["reason"]


def test_no_action_when_everything_is_already_paid_on_time():
    c = fx.clock(slots=6)
    net = build_network(
        "synthetic_nothing_to_do",
        as_of=fx.PLAN_AS_OF,
        clock=c,
        availability=SYNTHETIC_ZERO_LAG,
        focal_merchant="buyer",
        positions=[synthetic_position("buyer", 500000, 50000), synthetic_position("food_supplier")],
        policies=[MerchantPolicy("buyer"), MerchantPolicy("food_supplier")],
        invoices=[InvoiceInput(invoice("food_001", "buyer", "food_supplier", 100000, due_days=4))],
        labels=fx.BASE_LABELS,
    )
    r = analyze(net)
    assert r["recommendation"]["action"] == "NO_ACTION"
    assert "already paid on time" in r["recommendation"]["reason"]
    assert r["bottlenecks"] == []


def test_timing_case_beats_earliest_due_first():
    r = analyze(fx.network_timing_matters())
    assert r["recommendation"]["action"] == "RECOMMEND_PLAN"
    assert r["comparison"]["incremental_vs_B1"] == "90000"
    assert r["comparison"]["simple_rule_matches_plan"] is False


def test_fee_and_loss_flags():
    assert ("FEE_UNCERTAIN", "food_supplier") in codes(analyze(fx.network_fee_cascade()))
    assert not any(
        c == "PARTICIPANT_MODELED_LOSS" for c, _ in codes(analyze(fx.network_positive_sum()))
    )


def test_analysis_is_read_only_and_deterministic():
    net = fx.network_two_candidates()
    before = net.digest()
    a, b = analyze(net), analyze(net)
    assert net.digest() == before
    assert json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


def test_merchant_view_is_privacy_scoped():
    r = analyze(fx.network_two_candidates())
    carrier = merchant_view(r, "carrier")
    text = json.dumps(carrier)
    assert "buyer" not in text and "pack_001" not in text and "50000" not in text
    assert "comparison" not in carrier and "decision" not in carrier
    assert carrier["recommendation"]["action"] == "REVIEW_AND_DECIDE"
    assert {a["action"] for a in carrier["my_actions"]} == {"pay", "receive"}
    assert "other_bottlenecks_count" not in carrier and "2,700" not in text
    buyer = merchant_view(r, "buyer")
    assert "mechanic" not in json.dumps(buyer) and "carrier" not in json.dumps(buyer)
    assert buyer["comparison"]["incremental_vs_B3"] == "85000"


def test_facts_for_the_explanation_layer():
    fs = facts(merchant_view(analyze(fx.network_negotiation()), "buyer"))
    text = " ".join(f for _, f in fs)
    assert "SIMULATION" in text and "NEGOTIATE" in text
    assert "at least USD 10.00 would fit your rules" in text
    assert "food_supplier" not in text.replace("food_001", "")


def test_api_opportunities_and_local_only_model_routes():
    app = create_app()
    app.dependency_overrides[get_adapter] = lambda: ScriptedLLMAdapter(
        lambda *a: {
            "headline": "Opportunity",
            "explanation": "Simulation only: negotiate a USD 10.00 discount.",
            "cited_facts": ["F1"],
        }
    )
    local = TestClient(app)
    r = local.post("/v1/demo/opportunities", json={"scenario": "negotiation"}).json()
    assert r["model_call"] is None and r["deterministic"]["recommendation"]["action"] == "NEGOTIATE"
    e = local.post("/v1/demo/opportunities/explain", json={"scenario": "negotiation"}).json()
    assert e["status"] == "EXPLAINED" and e["model_call"]["mocked"] is True
    assert (
        local.post(
            "/v1/demo/opportunities", json={"scenario": "held_receipt", "viewer": "buyer"}
        ).status_code
        == 404
    )
    remote = TestClient(app, client=("203.0.113.9", 50000))
    for path, body in (
        ("/v1/demo/opportunities/explain", {}),
        ("/v1/demo/policies/draft", {"merchant_id": "buyer", "text": "Keep $600."}),
        ("/v1/demo/negotiation/counteroffer", {}),
        ("/v1/demo/plans/explain", {}),
    ):
        assert remote.post(path, json=body).status_code == 403, path
    assert remote.post("/v1/demo/opportunities", json={}).status_code == 200  # no model, no cost
    assert (
        remote.post(
            "/v1/demo/policies/validate",
            json={
                "merchant_id": "buyer",
                "text": "x",
                "extraction": {
                    "reserve_floor": None,
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
                    "summary": "",
                },
            },
        ).status_code
        == 200
    )


def test_positive_sum_and_timing_remain_recommended_and_conditional():
    for net in (fx.network_positive_sum(), fx.network_timing_matters()):
        r = analyze(net)
        assert r["recommendation"]["action"] == "RECOMMEND_PLAN"
        assert r["decision"]["unjustified_modeled_losses"] == []
        assert "approval" in r["recommendation"]["reason"]
        assert r["execution"]["may_execute"] is False
    assert analyze(fx.network_timing_matters())["decision"]["advantage"]["network_awareness_needed"]


def test_declared_acceptable_loss_is_not_flagged_for_review():
    pol = fx.supplier_policy(min_receipt_net_benefit_cents=-1000)  # accepts up to $10 loss
    r = analyze(fx.network_two_candidates(supplier_policy=pol))
    assert r["recommendation"]["action"] == "RECOMMEND_PLAN"
    assert r["decision"]["unjustified_modeled_losses"] == []
