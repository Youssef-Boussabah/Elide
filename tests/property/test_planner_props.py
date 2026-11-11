"""Planner properties on seeded SYNTHETIC networks (generator `api.planning.generator`).

- the exact solver equals an independent brute-force enumerator (small networks);
- every solver plan passes the independent verifier, whose replay reproduces the value;
- B4 is never worse than B0-B3 on on-time face, and B5 never worse than B4;
- minimum liquidity equals brute force and is tight (one cent less fails);
- a truncated search is never labelled OPTIMAL.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from api.planning.baselines import run_baselines
from api.planning.generator import FAMILIES, generate
from api.planning.objective import objective_value
from api.planning.reference import brute_force, brute_force_min_liquidity
from api.planning.solver import (
    Funding,
    Objective,
    SolveConfig,
    SolverStatus,
    minimum_liquidity,
    solve,
)
from api.planning.verifier import verify

SETTINGS = settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
small = st.builds(
    lambda family, seed: generate(family, seed, max_obligations=4, slots=3),
    st.sampled_from(FAMILIES),
    st.integers(0, 10_000),
)


@SETTINGS
@given(small, st.sampled_from([Objective.NETWORK, Objective.FOCAL]), st.booleans())
def test_solver_equals_brute_force(net, objective, proposals):
    cfg = SolveConfig(objective=objective, allow_proposals=proposals)
    r = solve(net, cfg)
    ref = brute_force(net, cfg)
    assert r.status is SolverStatus.OPTIMAL
    assert ref is not None
    assert r.value == ref.value
    rep = verify(net, r.schedule, allow_proposals=proposals)
    assert rep.ok, rep.violations
    assert objective_value(rep.metrics, objective) == r.value


@SETTINGS
@given(small)
def test_verified_plans_conserve_cash_and_keep_reserves(net):
    r = solve(net, SolveConfig(allow_proposals=True))
    m = verify(net, r.schedule, allow_proposals=True).metrics
    opening = sum(p.available.cents for p in net.positions)
    external = sum(i.amount.cents for p in net.positions for i in p.inflows)
    closing = sum(o.closing_cents for o in m.merchants)
    assert closing + m.fees + m.in_transit + m.external_pending == opening + external
    for o in m.merchants:
        if any(leg.payer == o.merchant_id for leg in m.legs):
            assert o.min_slack_cents >= 0


@SETTINGS
@given(small)
def test_network_plan_dominates_baselines_on_on_time_face(net):
    res = {b.baseline_id: b for b in run_baselines(net)}
    b4 = res["B4"].expected_on_time_face
    for k in ("B0", "B1", "B2", "B3"):
        assert b4 >= res[k].expected_on_time_face, k
    assert res["B5"].expected_on_time_face >= b4
    for b in res.values():
        if b.baseline_id != "B3":
            assert verify(net, b.schedule, allow_proposals=b.baseline_id == "B5").ok


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.builds(
        lambda family, seed: generate(family, seed, max_obligations=3, slots=3),
        st.sampled_from(FAMILIES),
        st.integers(0, 10_000),
    )
)
def test_minimum_liquidity_equals_brute_force_and_is_tight(net):
    target = frozenset(o.obligation_id for o in net.obligations[:2])
    funding = tuple(Funding(p.merchant_id, hypothetical_allowed=True) for p in net.positions)
    ml = minimum_liquidity(net, target, funding)
    ref = brute_force_min_liquidity(net, target, funding)
    if ref is None:
        assert ml.status is SolverStatus.INFEASIBLE
        return
    assert ml.status is SolverStatus.OPTIMAL
    assert ml.value == ref.value
    assert verify(net, ml.schedule, injections=ml.hypothetical, required=target).ok
    for m, cents in ml.hypothetical.items():
        less = dict(ml.hypothetical, **{m: cents - 1})
        less = {k: v for k, v in less.items() if v > 0}
        assert not verify(net, ml.schedule, injections=less, required=target).ok


@settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.builds(
        lambda family, seed: generate(family, seed, max_obligations=8, slots=4),
        st.sampled_from(FAMILIES),
        st.integers(0, 10_000),
    ),
    st.integers(1, 40),
)
def test_truncated_search_is_never_optimal(net, limit):
    r = solve(net, SolveConfig(max_nodes=limit))
    if not r.complete:
        assert r.status is not SolverStatus.OPTIMAL
    if r.status is SolverStatus.FEASIBLE:
        assert verify(net, r.schedule).ok


@SETTINGS
@given(
    st.builds(
        lambda family, seed: generate(
            family, seed, max_obligations=4, slots=3, supplier_rules=True
        ),
        st.sampled_from(FAMILIES),
        st.integers(0, 10_000),
    ),
    st.booleans(),
)
def test_supplier_rules_bind_solver_and_brute_force_alike(net, proposals):
    cfg = SolveConfig(allow_proposals=proposals)
    r = solve(net, cfg)
    ref = brute_force(net, cfg)
    assert ref is not None and r.value == ref.value and r.schedule == ref.schedule
    rep = verify(net, r.schedule, allow_proposals=proposals)
    assert rep.ok, rep.violations
    for leg in rep.metrics.legs:
        floor = net.policy(leg.payee).min_receipt_net_benefit_cents
        if floor is not None and (leg.extra_concession_cents > 0 or leg.accelerated):
            assert (
                leg.receipt_value_cents
                - leg.extra_concession_cents
                - leg.payee_fee_cents
                - leg.receipt_cost_cents
                >= floor
            )
