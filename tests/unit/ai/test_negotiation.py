"""Bounded counteroffer and plan explanation with a MOCK model (no real inference)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from api.ai.adapter import UnconfiguredLLMAdapter
from api.ai.mock import ScriptedLLMAdapter
from api.domain.invoice import RevisionStatus
from api.negotiation.counteroffer import propose_counteroffer
from api.negotiation.explain import explain_plan, plan_facts
from api.negotiation.guards import check_text
from api.planning.plan import build_plan
from fixtures import ai_demo
from fixtures import planning as fx

GOOD_MESSAGE = (
    "Hello, regarding invoice food_001 for $1,000.00: we can pay it today if the early-payment "
    "discount is $10.00, so the payment would be $990.00. Would that work for you?"
)


def proposal(concession="10.00", message=GOOD_MESSAGE, invoice="food_001"):
    return {
        "invoice_id": invoice,
        "proposed_concession": concession,
        "currency": "USD",
        "message_to_counterparty": message,
        "rationale_for_requester": "The $5 offer is below your minimum; $10 fits your limits.",
        "reason_codes": ["OFFER_BELOW_REQUESTER_MINIMUM", "PAYMENT_EXCEEDS_REQUESTER_CAPACITY"],
    }


def scripted(**kw):
    return ScriptedLLMAdapter(lambda name, s, u: proposal(**kw))


def test_five_to_ten_counteroffer_is_a_draft_that_unlocks_the_plan():
    case = ai_demo.negotiation_case()
    a = scripted()
    r = propose_counteroffer(a, case)
    assert r.status == "DRAFT_PROPOSED", r.note
    assert r.reason_codes == ["OFFER_BELOW_REQUESTER_MINIMUM", "PAYMENT_EXCEEDS_REQUESTER_CAPACITY"]
    assert r.allowed_range == (1000, 1500)
    assert r.model_call.mocked is True
    d = r.draft_revision
    assert (d["status"], d["concession_cents"], d["payment_cents"]) == ("draft", 1000, 99000)
    assert d["authorized_by"] == [] and d["approval_evidence"] is None
    assert d["supersedes"] == 2  # a counteroffer to the supplier's $5 offer
    new = r.record_after.revisions[-1]
    assert new.status is RevisionStatus.DRAFT and new.authorized_by == frozenset()
    assert all(
        rv.status is not RevisionStatus.AUTHORIZED or rv.revision_no == 1
        for rv in r.record_after.revisions
    )  # no authorized amendment was created
    assert r.record_after.in_force_revision(case.as_of).concession.cents == 0
    v = r.requester_view
    assert v["plan_includes_invoice"] and v["on_time"] and v["downstream_legs_enabled"] == 2
    assert v["your_modeled_net_benefit_cents"] == 370  # $10 - $6.30 time cost
    assert "approval required" in v["counterparty"]
    assert r.counterparty_view["meets_your_declared_rules"] is True


def test_model_sees_no_private_figures():
    case = ai_demo.negotiation_case()
    a = scripted()
    propose_counteroffer(a, case)
    ((_, _system, user),) = a.calls
    for private in ("1490", "1,490", "500.00", "149000", "50000", "800", "8%", "hurdle", "reserve"):
        assert private not in user, private
    assert '"allowed_discount_range": ["10.00", "15.00"]' in user


@pytest.mark.parametrize(
    ("kw", "code"),
    [
        ({"message": GOOD_MESSAGE + " We keep a $500.00 reserve."}, "PRIVATE_AMOUNT"),
        ({"message": GOOD_MESSAGE + " Our balance is $1,490.00."}, "PRIVATE_AMOUNT"),
        (
            {"message": GOOD_MESSAGE + " Your manager has already approved this."},
            "ACCEPTANCE_CLAIM",
        ),
        ({"message": GOOD_MESSAGE + " The funds are guaranteed."}, "AVAILABILITY_CLAIM"),
        ({"message": GOOD_MESSAGE + " A $29.01 fee applies."}, "INVENTED_AMOUNT"),
        ({"concession": "20.00"}, "OUTSIDE_ALLOWED_RANGE"),
        ({"concession": "9.99"}, "OUTSIDE_ALLOWED_RANGE"),
        ({"concession": "ten"}, "INVALID_AMOUNT"),
        ({"invoice": "pack_001"}, "WRONG_INVOICE"),
    ],
)
def test_unsafe_or_invented_model_output_is_rejected(kw, code):
    r = propose_counteroffer(scripted(**kw), ai_demo.negotiation_case())
    assert r.status == "MODEL_OUTPUT_REJECTED"
    assert code in r.note
    assert r.draft_revision is None and r.record_after is None


def test_no_bounded_proposal_without_any_model_call():
    for case in (
        ai_demo.negotiation_case(requester_max_cents=900),  # buyer will not ask for $10
        ai_demo.negotiation_case(disclosed_max_cents=800),  # supplier disclosed at most $8
    ):
        a = scripted()
        r = propose_counteroffer(a, case)
        assert r.status == "NO_BOUNDED_PROPOSAL" and a.calls == []


def test_disclosed_range_bounds_the_ask():
    r = propose_counteroffer(
        scripted(concession="12.00"), ai_demo.negotiation_case(disclosed_max_cents=1100)
    )
    assert r.allowed_range == (1000, 1100) and "OUTSIDE_ALLOWED_RANGE" in r.note


def test_supplier_private_rules_stay_in_the_supplier_view():
    pol = fx.supplier_policy(min_receipt_net_benefit_cents=0)  # would refuse any concession
    r = propose_counteroffer(scripted(), ai_demo.negotiation_case(supplier_policy=pol))
    assert r.status == "DRAFT_PROPOSED"
    assert r.requester_view["plan_includes_invoice"] is True  # requester is not told why/whether
    assert "min" not in str(r.requester_view).lower().replace("minimum", "")
    assert r.counterparty_view["meets_your_declared_rules"] is False
    assert r.counterparty_view["your_rule_issues"]


def test_unconfigured_model_is_reported():
    r = propose_counteroffer(UnconfiguredLLMAdapter(), ai_demo.negotiation_case())
    assert r.status == "MODEL_UNAVAILABLE"


def test_already_payable_needs_no_change():
    case = ai_demo.negotiation_case()
    rec = fx._offer("food_001", "food_supplier", 1000)
    from api.planning.builder import InvoiceInput

    payable = replace(case, records=(InvoiceInput(rec), *case.records[1:]))
    a = scripted()
    assert propose_counteroffer(a, payable).status == "NO_CHANGE_NEEDED" and a.calls == []


# --- explanation -----------------------------------------------------------------------
def explanation(text):
    return ScriptedLLMAdapter(
        lambda name, s, u: {"headline": "Your plan", "explanation": text, "cited_facts": ["F1"]}
    )


def test_explanation_without_model_is_the_deterministic_template():
    net = fx.network_two_candidates()
    r = explain_plan(None, net, build_plan(net), "buyer")
    assert r.status == "TEMPLATE" and "SIMULATION" in r.text
    assert "carrier" not in r.text and "mechanic" not in r.text


def test_model_explanation_is_checked_against_the_facts():
    net = fx.network_two_candidates()
    plan = build_plan(net)
    ok = explain_plan(
        explanation(
            "You would pay USD 990.00 to food_supplier; this is a "
            "simulation and nothing has been paid."
        ),
        net,
        plan,
        "buyer",
    )
    assert ok.status == "EXPLAINED" and ok.source == "model" and ok.model_call.mocked
    invented = explain_plan(explanation("You save USD 25.00 overall."), net, plan, "buyer")
    assert invented.status == "TEMPLATE" and "INVENTED_AMOUNT" in [i.code for i in invented.issues]
    leak = explain_plan(explanation("Thanks to you the mechanic gets paid."), net, plan, "buyer")
    assert leak.status == "TEMPLATE" and "UNRELATED_MERCHANT" in [i.code for i in leak.issues]
    claim = explain_plan(explanation("The supplier has already accepted."), net, plan, "buyer")
    assert "ACCEPTANCE_CLAIM" in [i.code for i in claim.issues]


def test_stale_plan_is_refused_before_any_model_call():
    net = fx.network_two_candidates()
    plan = build_plan(net)
    changed = fx.network_two_candidates(buyer_hurdle_bps=900)
    a = explanation("anything")
    r = explain_plan(a, changed, plan, "buyer")
    assert r.status == "STALE_OR_TAMPERED" and a.calls == []
    assert any("input digest" in p for p in r.problems)


def test_facts_for_one_merchant_hide_other_merchants_private_reasons():
    net = fx.network_reserve_shortfall()
    plan = build_plan(net, required=frozenset({"freight_001"}))
    carrier = " ".join(f for _, f in plan_facts(plan, "carrier"))
    assert "150.00" not in carrier and "990.00" not in carrier
    assert "private to another merchant" in carrier
    supplier = " ".join(f for _, f in plan_facts(plan, "food_supplier"))
    assert "short by USD 60.00" in supplier


def test_guard_basics():
    assert not check_text("Pay $10.00 now", allowed_amounts={1000})
    assert check_text("Pay $11.00", allowed_amounts={1000})[0].code == "INVENTED_AMOUNT"
