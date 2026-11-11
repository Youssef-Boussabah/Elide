"""Canonical P2 scenarios (all SYNTHETIC unless a fact is from the Sandbox evidence).

Expected numbers are derived by hand in the comments, then checked against the exact
solver AND the independent verifier.
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from api.planning.baselines import run_baselines
from api.planning.solver import (
    Funding,
    Objective,
    SolveConfig,
    SolverStatus,
    minimum_liquidity,
    solve,
)
from api.planning.verifier import verify
from fixtures import planning as fx


def legs(result) -> list[tuple[str, int]]:
    return [(leg.option_id, leg.slot) for leg in result.schedule]


# --- A: two candidate $990 payments, one unlocks $1,700 downstream -------------------
def test_two_candidates_network_plan():
    net = fx.network_two_candidates()
    r = solve(net, SolveConfig())
    assert r.status is SolverStatus.OPTIMAL and r.complete
    # buyer: only slot 0 (offer and evidence windows); supplier pays as late as keeps the
    # mechanic on time (freight 28 -> carrier usable 29 -> repair 29 = due slot).
    assert legs(r) == [("food_001:r2:offer", 0), ("freight_001:r1", 28), ("repair_001:r1", 29)]
    rep = verify(net, r.schedule)
    assert rep.ok, rep.violations
    m = rep.metrics
    assert m.on_time_face == 270000
    assert m.gross_flow == 269000  # $990 + $900 + $800 distinct transfers
    assert m.downstream_face == 170000
    closing = {o.merchant_id: o.closing_cents for o in m.merchants}
    assert closing == {
        "buyer": 50000,
        "food_supplier": 9000,
        "packaging_supplier": 0,
        "carrier": 10000,
        "mechanic": 80000,
    }
    assert sum(closing.values()) == 149000  # conserved, fee-free


def test_buyer_net_benefit_counts_the_time_cost_of_paying_early():
    net = fx.network_two_candidates()
    m = verify(net, solve(net, SolveConfig()).schedule).metrics
    buyer = next(o for o in m.merchants if o.merchant_id == "buyer")
    # $990 paid 29 days before the due slot at an 8%/yr hurdle:
    # ceil(99000 * 800 * 29 / (10000 * 365)) = ceil(629.26) = 630 cents
    assert buyer.discount_gained == 1000
    assert buyer.opportunity_cost == 630
    assert buyer.net_benefit == 370
    supplier = next(o for o in m.merchants if o.merchant_id == "food_supplier")
    assert supplier.discount_given == 1000 and supplier.net_benefit == -1000


def test_two_candidates_baselines_report_the_tie_trap_honestly():
    res = {b.baseline_id: b for b in run_baselines(fx.network_two_candidates())}
    assert res["B0"].expected_on_time_face == 0  # $1,000 at due exceeds $990 deployable
    assert res["B1"].expected_on_time_face == 270000  # lucky: id order picks food first
    assert res["B2"].expected_on_time_face == 270000  # deterministic tie-break, same luck
    assert res["B3"].expected_on_time_face == Fraction(185000)  # (2700 + 1000) / 2
    assert res["B3"].outcomes_considered == 2
    assert res["B4"].expected_on_time_face == 270000
    assert res["B5"].expected_on_time_face == 270000
    assert any("no negotiated change" in n for n in res["B5"].notes)
    # Elide's honest increment over a random tie-break is $850, not $1,700.
    assert res["B4"].expected_on_time_face - res["B3"].expected_on_time_face == 85000
    assert res["B4"].expected_on_time_face - res["B1"].expected_on_time_face == 0


# --- B: negotiation $5 -> $10 -----------------------------------------------------
def test_negotiation_without_the_change_is_infeasible_for_the_buyer():
    net = fx.network_negotiation()
    r = solve(net, SolveConfig())
    assert r.status is SolverStatus.OPTIMAL
    assert r.schedule == ()  # $1,000 and $995 both exceed $990; $5 < buyer's $10 minimum


def test_negotiation_with_the_change_unlocks_the_chain():
    net = fx.network_negotiation()
    r = solve(net, SolveConfig(allow_proposals=True))
    assert legs(r)[0] == ("food_001:r3:proposal", 0)
    rep = verify(net, r.schedule, allow_proposals=True)
    assert rep.ok and rep.metrics.on_time_face == 270000
    # proposals are never usable without explicitly enabling B5
    assert not verify(net, r.schedule).ok


def test_negotiation_baselines():
    res = {b.baseline_id: b for b in run_baselines(fx.network_negotiation())}
    assert [res[k].expected_on_time_face for k in ("B0", "B1", "B2", "B3", "B4")] == [0] * 5
    assert res["B5"].expected_on_time_face == 270000
    assert any("different feasible set" in n for n in res["B5"].notes)


# --- C: held receipt ---------------------------------------------------------------
def test_held_receipt_cannot_fund_the_freight_payment():
    net = fx.network_held_receipt()
    r = solve(net, SolveConfig(required=frozenset({"freight_001"})))
    assert r.status is SolverStatus.INFEASIBLE
    assert solve(net, SolveConfig()).schedule == ()
    ml = minimum_liquidity(
        net, frozenset({"freight_001"}), (Funding("food_supplier", hypothetical_allowed=True),)
    )
    assert ml.status is SolverStatus.OPTIMAL
    assert ml.hypothetical == {"food_supplier": 105000}  # $900 + $150 reserve; held adds 0


# --- D: reserve shortfall ----------------------------------------------------------
def test_reserve_shortfall_explained_exactly():
    net = fx.network_reserve_shortfall()
    r = solve(net, SolveConfig(required=frozenset({"freight_001"})))
    assert r.status is SolverStatus.INFEASIBLE
    rep = verify(net, ((fx.leg("freight_001:r1", 0)),))
    assert not rep.ok
    assert rep.violations[0].detail == (
        "food_supplier needs USD 900.00 + USD 150.00 reserve, has USD 990.00 available "
        "at slot 0: short by USD 60.00"
    )
    ml = minimum_liquidity(
        net, frozenset({"freight_001"}), (Funding("food_supplier", hypothetical_allowed=True),)
    )
    assert ml.hypothetical == {"food_supplier": 6000}
    assert ml.existing == {"food_supplier": 84000}


def test_hypothetical_usd_net_after_the_sandbox_fee_is_short_by_89_01():
    net = fx.network_reserve_shortfall(hypothetical_usd_net=True)
    rep = verify(net, (fx.leg("freight_001:r1", 0),))
    assert rep.violations[0].detail.endswith("short by USD 89.01")


def test_payee_fee_deducted_from_proceeds_changes_the_gap_to_89_01():
    net = fx.network_fee_cascade()
    r = solve(net, SolveConfig(required=frozenset({"food_001", "freight_001"})))
    assert r.status is SolverStatus.INFEASIBLE
    rep = verify(net, (fx.leg("food_001:r2:offer", 0), fx.leg("freight_001:r1", 1)))
    assert rep.violations[0].detail == (
        "food_supplier needs USD 900.00 + USD 150.00 reserve, has USD 960.99 available "
        "at slot 1: short by USD 89.01"
    )
    ml = minimum_liquidity(
        net,
        frozenset({"food_001", "freight_001"}),
        (Funding("buyer"), Funding("food_supplier", hypothetical_allowed=True)),
    )
    assert ml.hypothetical == {"food_supplier": 8901}


def test_payer_fee_is_added_to_the_payer_debit():
    net = fx.network_fee_cascade(payer_pays_fee=True)
    rep = verify(net, (fx.leg("food_001:r2:offer", 0),))
    assert rep.violations[0].detail == (
        "buyer needs USD 1019.01 + USD 500.00 reserve, has USD 1490.00 available "
        "at slot 0: short by USD 29.01"
    )


# --- E: CAD settlement credit (Sandbox fact) -----------------------------------------
def test_cad_credit_cannot_fund_a_usd_obligation():
    net = fx.network_cad_settlement()
    r = solve(net, SolveConfig(required=frozenset({"freight_001"})))
    assert r.status is SolverStatus.INFEASIBLE
    rep = verify(net, (fx.leg("freight_001:r1", 0),))
    assert not rep.ok
    ml = minimum_liquidity(
        net, frozenset({"freight_001"}), (Funding("food_supplier", hypothetical_allowed=True),)
    )
    # the CAD 1,306.79 counts for nothing; a separately authorized conversion would be needed
    assert ml.hypothetical == {"food_supplier": 90000}
    assert any("PAYPAL_SANDBOX_FACT" in label for label in net.labels)


# --- minimum liquidity ---------------------------------------------------------------
def test_minimum_existing_seed_for_the_canonical_chain_is_990():
    net = fx.network_two_candidates()
    ml = minimum_liquidity(
        net, frozenset({"food_001", "freight_001", "repair_001"}), (Funding("buyer"),)
    )
    assert ml.status is SolverStatus.OPTIMAL
    assert ml.existing == {"buyer": 99000}
    assert ml.hypothetical == {}


def test_target_unreachable_without_hypothetical_funding_is_infeasible():
    net = fx.network_reserve_shortfall()
    ml = minimum_liquidity(net, frozenset({"freight_001"}), (Funding("food_supplier"),))
    assert ml.status is SolverStatus.INFEASIBLE


def test_three_party_loop_needs_one_hypothetical_seed_not_zero():
    net = fx.network_loop()
    target = frozenset({"ab_001", "bc_001", "ca_001"})
    ml = minimum_liquidity(
        net, target, tuple(Funding(m, hypothetical_allowed=True) for m in ("a", "b", "c"))
    )
    assert ml.status is SolverStatus.OPTIMAL
    assert sum(ml.hypothetical.values()) == 10000  # $100 seed circulates A->B->C->A
    assert ml.existing == {}
    assert any("D011" in n for n in ml.notes)  # noncash clearing is a separate mode


def test_minimum_liquidity_result_is_verified_and_tight():
    net = fx.network_reserve_shortfall()
    funding = (Funding("food_supplier", hypothetical_allowed=True),)
    ml = minimum_liquidity(net, frozenset({"freight_001"}), funding)
    assert verify(net, ml.schedule, injections=ml.hypothetical).ok
    less = {"food_supplier": ml.hypothetical["food_supplier"] - 1}
    assert not verify(net, ml.schedule, injections=less).ok


@pytest.mark.parametrize("objective", [Objective.NETWORK, Objective.FOCAL])
def test_objectives_never_break_buyer_acceptance(objective):
    net = fx.network_two_candidates(buyer_hurdle_bps=2000)  # 20%/yr makes $10 not worth it
    r = solve(net, SolveConfig(objective=objective))
    # ceil(99000*2000*29/3650000) = 1574 > 1000 discount: buyer would lose, so no plan
    assert r.schedule == ()


# --- late receipts ------------------------------------------------------------------------
def _supplier_with_late_receipt(days: int, duplicate: bool = False):
    from datetime import timedelta

    from api.liquidity.capacity import (
        AvailabilityBasis,
        AvailabilityEvidence,
        IncomingReceipt,
        ReceiptStatus,
    )
    from api.planning.builder import position_from_evidence
    from fixtures import canonical

    when = fx.PLAN_AS_OF + timedelta(days=days, hours=1)
    receipt = IncomingReceipt(
        "rcpt_late",
        "food_supplier",
        canonical.usd(99000),
        ReceiptStatus.AVAILABLE_VERIFIED,
        canonical.SYN,
        available_at=when,
        availability=AvailabilityEvidence(AvailabilityBasis.SYNTHETIC, "syn-availability", when),
    )
    ev = canonical.synthetic_evidence("ev_food_late", "food_supplier", 0, 0, hours_valid=24 * 40)
    net = fx.network_reserve_shortfall()
    receipts = [receipt, receipt] if duplicate else [receipt]
    pos = position_from_evidence(ev, as_of=fx.PLAN_AS_OF, clock=net.clock, receipts=receipts)
    return replace_position(net, pos)


def replace_position(net, pos):
    from dataclasses import replace

    return replace(
        net,
        positions=tuple(pos if p.merchant_id == pos.merchant_id else p for p in net.positions),
    )


def test_a_verified_receipt_funds_nothing_before_its_usable_slot():
    net = _supplier_with_late_receipt(days=2)  # usable 2 days + 1 h later -> slot 3
    (inflow,) = net.position("food_supplier").inflows
    assert inflow.available_slot == 3 and inflow.amount.cents == 99000
    assert not verify(net, (fx.leg("freight_001:r1", 2),)).ok
    assert verify(net, (fx.leg("freight_001:r1", 3),)).ok
    r = solve(net, SolveConfig(required=frozenset({"freight_001"})))
    assert r.status is SolverStatus.OPTIMAL and r.schedule[0].slot >= 3


def test_a_receipt_after_the_due_slot_cannot_make_the_payment_on_time():
    net = _supplier_with_late_receipt(days=29)  # usable after the freight due slot
    assert net.position("food_supplier").inflows[0].available_slot == 30
    r = solve(net, SolveConfig(required=frozenset({"freight_001"})))
    assert r.status is SolverStatus.INFEASIBLE


def test_a_duplicated_receipt_record_still_counts_once():
    net = _supplier_with_late_receipt(days=2, duplicate=True)
    assert [i.amount.cents for i in net.position("food_supplier").inflows] == [99000]
