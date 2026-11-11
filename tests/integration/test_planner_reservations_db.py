"""P2 plans reserve cash through the P1 store: two concurrent plans cannot both use the
buyer's $990, and a re-plan after the winner's reservation sees no capacity left."""

from __future__ import annotations

import threading
from dataclasses import replace

import pytest

from api.domain.money import USD, Money
from api.liquidity import store as cap_store
from api.liquidity.capacity import Commitment, InsufficientCapacityError
from api.planning.builder import position_from_evidence
from api.planning.plan import build_plan, plan_commitments
from api.planning.solver import Objective, SolveConfig, solve
from fixtures import canonical
from fixtures import planning as fx

pytestmark = pytest.mark.integration


def _two_plans():
    network_plan = build_plan(fx.network_buyer_tradeoff())  # pays food ($990)
    focal = solve(fx.network_buyer_tradeoff(), SolveConfig(objective=Objective.FOCAL))
    assert [x.option_id for x in focal.schedule] == ["pack_001:r2:offer"]  # pays $980
    return network_plan, focal


def test_two_plans_cannot_reserve_the_same_buyer_cash(kernel_db, uid):
    merchant = f"buyer_{uid}"
    ev = replace(
        canonical.synthetic_evidence("ev_buyer_a", "buyer", 149000, 50000),
        evidence_id=f"ev_{uid}",
        merchant_id=merchant,
    )
    cap_store.save_evidence(kernel_db, ev)
    network_plan, _focal = _two_plans()
    (food,) = plan_commitments(network_plan)
    attempts = [
        Commitment(f"c_{uid}_net", merchant, network_plan["plan_id"], food.amount),
        Commitment(f"c_{uid}_focal", merchant, "plan_focal_alt", Money(98000, USD)),
    ]
    barrier = threading.Barrier(2)
    results, errors = [], []

    def worker(c):
        barrier.wait()
        try:
            results.append(
                cap_store.reserve_capacity(
                    kernel_db,
                    evidence_id=ev.evidence_id,
                    commitment=c,
                    at=fx.PLAN_AS_OF,
                    allow_synthetic=True,
                )
            )
        except Exception as exc:  # asserted below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(c,)) for c in attempts]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(results) == 1
    assert len(errors) == 1 and isinstance(errors[0], InsufficientCapacityError)
    assert cap_store.active_commitment_total(kernel_db, merchant) in (99000, 98000)

    # Re-plan with the stored commitment: no second $980-$990 payment fits any more.
    snap = cap_store.capacity_now(
        kernel_db, evidence_id=ev.evidence_id, at=fx.PLAN_AS_OF, allow_synthetic=True
    )
    assert snap.deployable.cents in (0, 1000)
    committed = Money(cap_store.active_commitment_total(kernel_db, merchant), USD)
    net = fx.network_buyer_tradeoff()
    pos = position_from_evidence(
        canonical.synthetic_evidence("ev_buyer_a", "buyer", 149000, 50000),
        as_of=fx.PLAN_AS_OF,
        clock=net.clock,
        commitments=[Commitment("c_existing", "buyer", "plan_other", committed)],
    )
    after = replace(
        net, positions=tuple(pos if p.merchant_id == "buyer" else p for p in net.positions)
    )
    assert solve(after, SolveConfig()).schedule == ()
