"""Supplier-side economics, discharged-obligation metrics and the constructed timing case."""

from __future__ import annotations

from dataclasses import replace

from api.domain.money import USD, Money
from api.planning.baselines import run_baselines
from api.planning.plan import build_plan
from api.planning.solver import SolveConfig, SolverStatus, solve
from api.planning.verifier import verify
from fixtures import planning as fx

CHAIN = (
    fx.leg("food_001:r2:offer", 0),
    fx.leg("freight_001:r1", 28),
    fx.leg("repair_001:r1", 29),
)


def legs(r):
    return [(x.option_id, x.slot) for x in r.schedule]


# --- canonical economics are reported honestly, not as a win-win ---------------------
def test_canonical_plan_flags_the_supplier_modeled_loss():
    p = build_plan(fx.network_two_candidates())
    assert p["merchant_benefits"]["buyer"]["net_benefit"] == 370
    assert p["merchant_benefits"]["food_supplier"]["net_benefit"] == -1000
    assert p["economics"]["all_participants_non_negative"] is False
    assert p["economics"]["modeled_losses"] == ["food_supplier"]
    assert "MODELED_NET_LOSS:food_supplier" in p["warnings"]
    assert "Not evidence" in p["economics"]["note"]


def test_canonical_results_are_unchanged_by_the_new_supplier_fields():
    r = solve(fx.network_two_candidates(), SolveConfig())
    assert legs(r) == [("food_001:r2:offer", 0), ("freight_001:r1", 28), ("repair_001:r1", 29)]
    res = {
        b.baseline_id: b.expected_on_time_face for b in run_baselines(fx.network_two_candidates())
    }
    assert res == {"B0": 0, "B1": 270000, "B2": 270000, "B3": 185000, "B4": 270000, "B5": 270000}


# --- the supplier's declared restrictions bind the optimizer ---------------------------
def test_supplier_minimum_benefit_cannot_be_overridden():
    pol = fx.supplier_policy(min_receipt_net_benefit_cents=0)  # no stated receipt value
    net = fx.network_two_candidates(supplier_policy=pol)
    r = solve(net, SolveConfig())
    assert legs(r) == [("pack_001:r2:offer", 0)]  # the network-best chain is refused
    rep = verify(net, CHAIN)
    assert [v.code for v in rep.violations] == ["PAYEE_REJECTS"]
    assert "below its declared minimum" in rep.violations[0].detail
    rejected = {x["leg_id"]: x["code"] for x in build_plan(net)["rejected_legs"]}
    assert rejected["food_001:r2:offer"] == "PAYEE_REJECTS"


def test_supplier_threshold_counts_fees_and_declared_costs():
    base = fx.network_fee_cascade()  # USD 29.01 deducted from the supplier's proceeds
    pol = fx.supplier_policy(early_receipt_value_bps=2000, min_receipt_net_benefit_cents=0)
    net = replace(
        base,
        policies=tuple(pol if p.merchant_id == "food_supplier" else p for p in base.policies),
    )
    # value floor(96099*2000*29/3650000) = 1527; 1527 - 1000 - 2901 < 0
    rep = verify(net, (fx.leg("food_001:r2:offer", 0),))
    assert [v.code for v in rep.violations] == ["PAYEE_REJECTS"]
    costly = fx.supplier_policy(
        early_receipt_value_bps=2000, receipt_cost_cents=600, min_receipt_net_benefit_cents=0
    )
    net2 = fx.network_two_candidates(supplier_policy=costly)  # 1573 - 1000 - 600 < 0
    assert "PAYEE_REJECTS" in [v.code for v in verify(net2, CHAIN).violations]


def test_supplier_concession_cap_fee_ceiling_and_prohibition_bind_the_solver():
    for pol in (
        fx.supplier_policy(max_concession_given=Money(500, USD)),
        fx.supplier_policy(prohibited_counterparties=frozenset({"buyer"})),
    ):
        r = solve(fx.network_two_candidates(supplier_policy=pol), SolveConfig())
        assert all(not x.option_id.startswith("food_001") for x in r.schedule), pol
    fee = fx.network_fee_cascade()
    pol = fx.supplier_policy(max_fee_per_leg=Money(2000, USD))
    fee = replace(
        fee, policies=tuple(pol if p.merchant_id == "food_supplier" else p for p in fee.policies)
    )
    assert solve(fee, SolveConfig()).schedule == ()


# --- a separate, disclosed positive-sum example --------------------------------------
def test_positive_sum_example_with_disclosed_assumptions():
    net = fx.network_positive_sum()
    assert "CONSTRUCTED_EXAMPLE" in net.labels
    assert any(label.startswith("SYNTHETIC_ASSUMPTIONS (disclosed)") for label in net.labels)
    p = build_plan(net)
    assert p["selected_legs"][0]["leg_id"] == "food_001:r2:offer"
    sup = p["merchant_benefits"]["food_supplier"]
    # value floor(99000*2000*29/3650000)=1573; -1000 concession; -200 declared cost
    assert (sup["early_receipt_value_cents"], sup["other_costs_cents"]) == (1573, 200)
    assert sup["net_benefit"] == 373
    assert p["merchant_benefits"]["buyer"]["net_benefit"] == 370
    assert p["economics"]["all_participants_non_negative"] is True
    assert p["economics"]["modeled_losses"] == []


# --- discharged obligations vs original face vs gross flow ----------------------------
def test_partially_paid_invoice_does_not_count_its_face_as_new_cash():
    net = fx.network_partially_paid()
    edge = net.obligation("food_001")
    assert (edge.face.cents, edge.cash_applied.cents, edge.open.cents) == (100000, 40000, 60000)
    offer = net.option("food_001:r2:offer")
    assert offer.cash.cents == 59000  # $1,000 - $400 already applied - $10 concession
    r = solve(net, SolveConfig())
    assert r.status is SolverStatus.OPTIMAL
    m = verify(net, r.schedule).metrics
    assert m.on_time_face == 100000  # published metric: original face completed on time
    assert m.gross_flow == 59000  # cash actually moved by the plan
    assert m.discharged_open == 60000  # outstanding value discharged
    assert (m.discharged_cash, m.discharged_new_concession) == (59000, 1000)
    assert m.prior_cash_excluded == 40000  # applied before planning, not new movement
    p = build_plan(net)
    assert p["discharged"]["open_discharged_cents"] == 60000
    assert p["discharged"]["prior_cash_applied_excluded_cents"] == 40000
    assert p["selected_legs"][0]["prior_cash_applied_cents"] == 40000
    assert "original invoice face" in p["discharged"]["note"]


def test_discharged_metric_matches_face_when_nothing_was_paid_before():
    m = verify(fx.network_two_candidates(), CHAIN).metrics
    assert m.discharged_open == m.on_time_face == 270000
    assert m.gross_flow == 269000  # $10 concession is discharged without cash


# --- constructed case: network awareness beats earliest-due-first ---------------------
def test_constructed_timing_case_beats_earliest_due_first():
    net = fx.network_timing_matters()
    res = {b.baseline_id: b for b in run_baselines(net)}
    b1 = [(x.option_id, x.slot) for x in res["B1"].schedule]
    # B1 pays packaging (due first) with the only cash; food waits for the slot-5 receipt;
    # the supplier can then pay the carrier only at slot 6, after its due slot 1.
    assert b1 == [("pack_001:r1", 0), ("food_001:r1", 5), ("freight_001:r1", 6)]
    assert res["B1"].expected_on_time_face == 200000
    b4 = [(x.option_id, x.slot) for x in res["B4"].schedule]
    assert b4[:2] == [("food_001:r1", 0), ("freight_001:r1", 1)]
    assert ("pack_001:r1", 7) in b4  # still on time (due slot 7), funded by the receipt
    assert res["B4"].expected_on_time_face == 290000
    assert res["B4"].expected_on_time_face - res["B1"].expected_on_time_face == 90000
    assert res["B3"].expected_on_time_face <= res["B4"].expected_on_time_face
    m = res["B4"].metrics
    assert all(o.net_benefit == 0 for o in m.merchants)  # nobody is made worse off here
