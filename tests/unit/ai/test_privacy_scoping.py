"""Adversarial privacy checks with distinctive private values (fixture privacy-probe-v1).

For every merchant, its opportunity view, the opportunity-explanation prompt and the
plan-explanation prompt actually sent to the (mock) model must contain none of the facts
it is not entitled to: other merchants' cash, reserves, hidden counterparties, hidden
invoice ids, private payer reasons, and network totals (initiating merchant only).
"""

from __future__ import annotations

import json

import pytest

from api.ai.mock import ScriptedLLMAdapter
from api.negotiation.counteroffer import propose_counteroffer
from api.negotiation.explain import explain_plan, narrate
from api.opportunities.analysis import analyze, facts, merchant_view
from api.planning.plan import build_plan
from fixtures import ai_demo
from fixtures import planning as fx
from fixtures.privacy_probe import network_privacy_probe

BUYER_CASH = ("1,777.77", "1777.77", "177777", "512.34", "51234")
SUPPLIER_RESERVE = ("123.45", "12345")
HIDDEN_VENDOR = ("secret_vendor", "secret_inv_9", "4,321.09", "4321.09", "432109")
NETWORK_TOTALS = ("2,650", "265000")
BUYER_RULE = ("extra discount",)  # the buyer's private minimum-discount reason text

FORBIDDEN = {
    "buyer": (*SUPPLIER_RESERVE, "repair_77", "carrier", "mechanic"),
    "food_supplier": BUYER_CASH + HIDDEN_VENDOR + NETWORK_TOTALS + ("repair_77", "mechanic"),
    "carrier": BUYER_CASH + SUPPLIER_RESERVE + HIDDEN_VENDOR + NETWORK_TOTALS + ("food_001",),
    "mechanic": BUYER_CASH
    + SUPPLIER_RESERVE
    + HIDDEN_VENDOR
    + NETWORK_TOTALS
    + ("food_001", "freight_001", "food_supplier", "buyer"),
    "secret_vendor": BUYER_CASH
    + SUPPLIER_RESERVE
    + NETWORK_TOTALS
    + BUYER_RULE
    + ("food_001", "freight_001", "repair_77", "food_supplier", "carrier", "mechanic"),
}


def _capture():
    return ScriptedLLMAdapter(
        lambda *a: {"headline": "h", "explanation": "A simulation.", "cited_facts": ["F1"]}
    )


def _assert_clean(text: str, viewer: str, where: str):
    leaked = [s for s in FORBIDDEN[viewer] if s in text]
    assert not leaked, f"{where} for {viewer} leaks {leaked}"


@pytest.fixture(scope="module")
def probe():
    net = network_privacy_probe()
    return net, analyze(net), build_plan(net)


@pytest.mark.parametrize("viewer", sorted(FORBIDDEN))
def test_opportunity_view_and_its_model_prompt_are_scoped(probe, viewer):
    _net, report, _plan = probe
    view = merchant_view(report, viewer)
    _assert_clean(json.dumps(view), viewer, "opportunity view")
    a = _capture()
    narrate(a, facts(view), viewer, ())
    _assert_clean(a.calls[0][2] + a.calls[0][1], viewer, "opportunity prompt")


@pytest.mark.parametrize("viewer", sorted(FORBIDDEN))
def test_plan_explanation_prompt_is_scoped(probe, viewer):
    net, _report, plan = probe
    a = _capture()
    r = explain_plan(a, net, plan, viewer)
    assert r.status in ("EXPLAINED", "TEMPLATE")
    _assert_clean(a.calls[0][2], viewer, "plan explanation prompt")
    _assert_clean(r.text, viewer, "plan explanation text")


def test_only_the_initiating_merchant_gets_network_comparisons(probe):
    _net, report, _plan = probe
    buyer = merchant_view(report, "buyer")
    assert buyer["comparison"]["incremental_vs_B0"] == "265000"
    assert buyer["decision"]["participants_with_undeclared_losses"] == 1
    assert "participant_net_benefit_cents" not in buyer["decision"]  # others' nets are private
    for other in ("food_supplier", "carrier", "mechanic", "secret_vendor"):
        v = merchant_view(report, other)
        assert "comparison" not in v and "decision" not in v and "other_legs_count" not in v


def test_creditor_never_sees_the_payers_private_reason(probe):
    _net, report, _plan = probe
    vendor = merchant_view(report, "secret_vendor")
    (b,) = vendor["my_bottlenecks"]
    assert b["reason_code"] == "PAYER_SIDE" and "private to the payer" in b["reason"]
    assert vendor["recommendation"]["action"] == "NOTHING_FOR_YOU"
    buyer_b = next(
        x
        for x in merchant_view(report, "buyer")["my_bottlenecks"]
        if x["obligation_id"] == "secret_inv_9"
    )
    assert buyer_b["reason_code"] != "PAYER_SIDE"  # the payer sees its own reason


def test_counteroffer_prompt_never_contains_supplier_private_rules():
    pol = fx.supplier_policy(
        max_concession_given=fx.usd(3333),
        min_receipt_net_benefit_cents=-777,
        receipt_cost_cents=4444,
    )
    a = ScriptedLLMAdapter(
        lambda *x: {
            "invoice_id": "food_001",
            "proposed_concession": "10.00",
            "currency": "USD",
            "message_to_counterparty": "Could we settle food_001 ($1,000.00) today for $990.00?",
            "rationale_for_requester": "fits",
            "reason_codes": [],
        }
    )
    r = propose_counteroffer(a, ai_demo.negotiation_case(supplier_policy=pol))
    prompt = a.calls[0][1] + a.calls[0][2]
    for s in ("33.33", "3333", "7.77", "777", "44.44", "4444", "1490", "1,490", "500.00"):
        assert s not in prompt, s
    assert "33.33" not in json.dumps(r.requester_view) and "44.44" not in json.dumps(
        r.requester_view
    )
