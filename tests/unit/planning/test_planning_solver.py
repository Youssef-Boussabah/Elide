"""Exact solver mechanics: status honesty, search limits, exclusions, objectives."""

from __future__ import annotations

from dataclasses import replace

from api.domain.invoice import DisputeStatus
from api.planning.network import Authorization, ConsentBasis, FeeStatus, FeeTerms
from api.planning.objective import objective_value
from api.planning.reference import brute_force
from api.planning.solver import SOLVER_NAME, Objective, SolveConfig, SolverStatus, solve
from api.planning.verifier import verify
from fixtures import planning as fx


def test_complete_search_is_optimal_and_matches_brute_force():
    net = fx.network_small_two_slot()
    r = solve(net, SolveConfig())
    ref = brute_force(net, SolveConfig())
    assert r.status is SolverStatus.OPTIMAL and r.complete
    assert ref is not None
    assert r.value == ref.value
    assert r.value == objective_value(verify(net, r.schedule).metrics, Objective.NETWORK)


def test_search_limit_never_reports_optimal():
    net = fx.network_ten_legs()
    r = solve(net, SolveConfig(max_nodes=50))
    assert not r.complete
    assert r.status in (SolverStatus.FEASIBLE, SolverStatus.UNKNOWN)
    assert r.status is not SolverStatus.OPTIMAL
    if r.status is SolverStatus.FEASIBLE:
        assert verify(net, r.schedule).ok
    assert "search limit" in r.note


def test_ten_candidate_legs_solve_exactly_within_limits():
    net = fx.network_ten_legs()
    assert len(net.options) == 10
    r = solve(net, SolveConfig())
    assert r.status is SolverStatus.OPTIMAL and r.complete
    assert verify(net, r.schedule).ok
    assert r.nodes < SolveConfig().max_nodes


def test_infeasible_required_target_is_reported_not_hidden():
    net = fx.network_reserve_shortfall()
    r = solve(net, SolveConfig(required=frozenset({"freight_001"})))
    assert r.status is SolverStatus.INFEASIBLE and r.complete
    assert r.schedule == ()


def test_limit_with_unmet_requirement_is_unknown_not_infeasible():
    net = fx.network_ten_legs()
    target = frozenset(o.obligation_id for o in net.obligations)
    r = solve(net, SolveConfig(required=target, max_nodes=5))
    assert r.status in (SolverStatus.UNKNOWN, SolverStatus.FEASIBLE)
    assert r.status is not SolverStatus.INFEASIBLE


def test_solver_skips_refused_disputed_and_unknown_fee_legs():
    net = fx.network_two_candidates()
    refused = replace(
        net,
        options=tuple(
            replace(
                o,
                authorizations=(
                    Authorization("food_supplier", "authorize_payment", ConsentBasis.REFUSED),
                ),
            )
            if o.option_id == "freight_001:r1"
            else o
            for o in net.options
        ),
    )
    assert all(leg.option_id != "freight_001:r1" for leg in solve(refused, SolveConfig()).schedule)

    e = replace(net.obligation("food_001"), dispute=DisputeStatus.OPEN)
    disputed = replace(
        net, obligations=tuple(e if o.obligation_id == "food_001" else o for o in net.obligations)
    )
    r = solve(disputed, SolveConfig())
    assert all(not leg.option_id.startswith("food_001") for leg in r.schedule)
    assert verify(disputed, r.schedule).ok

    unknown = replace(
        net,
        options=tuple(
            replace(o, fee=FeeTerms(status=FeeStatus.UNKNOWN))
            if o.obligation_id == "food_001"
            else o
            for o in net.options
        ),
    )
    assert all(
        not leg.option_id.startswith("food_001") for leg in solve(unknown, SolveConfig()).schedule
    )


def test_focal_objective_maximizes_the_buyer_own_return_first():
    net = fx.network_buyer_tradeoff()
    network_plan = solve(net, SolveConfig(objective=Objective.NETWORK))
    buyer_plan = solve(net, SolveConfig(objective=Objective.FOCAL))
    mn = verify(net, network_plan.schedule).metrics
    mb = verify(net, buyer_plan.schedule).metrics
    assert mn.on_time_face > mb.on_time_face  # network plan unlocks more downstream
    assert mb.focal_net > mn.focal_net  # but the buyer earns more on its own pick


def test_solver_result_is_deterministic():
    a = solve(fx.network_two_candidates(), SolveConfig())
    b = solve(fx.network_two_candidates(), SolveConfig())
    assert a == b
    assert SOLVER_NAME == "exact_slot_dp"


def test_existing_commitment_blocks_double_allocation_across_plans():
    """Plan 1 reserves the buyer's $990; plan 2 computed afterwards cannot reuse it."""
    from api.planning.plan import build_plan, plan_commitments

    net = fx.network_two_candidates()
    plan1 = build_plan(net)
    commitments = plan_commitments(plan1)
    assert [(c.merchant_id, c.amount.cents) for c in commitments] == [("buyer", 99000)]
    pos = replace(net.position("buyer"), committed=commitments[0].amount)
    after = replace(
        net, positions=tuple(pos if p.merchant_id == "buyer" else p for p in net.positions)
    )
    assert solve(after, SolveConfig()).schedule == ()
