"""P5 pilot blocker (2026-11-12): an accepted early-payment offer produced an empty plan.

Reproduced on the isolated pilot database (read-only; `scripts/diagnose_pilot_plan.py`,
`evidence/phase5/pilot_plan_blocker_2026-11-12.md`). These tests pin the planner's behaviour
for the exact accepted-offer scenario on PostgreSQL, through the real persistence path:
- capacity evidence that expires before the agreed pay-by date leaves no feasible slot;
- with evidence covering the pay-by window the existing optimizer plans the payment ON the
  agreed date (not earlier, not later);
- accelerating beyond the agreed date has no incremental benefit and is refused;
- a newer confirmed reserve still binds;
- the payer's minimum early-payment discount is currently applied to the INCREMENTAL
  concession relative to the revised date (a latent P2 defect, recorded as a strict xfail).

No PayPal call; the mock provider is never used here.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from api.domain.money import USD, Money
from api.execution import views
from api.planning.solver import SolveConfig, _Search, solve
from api.workflow.networks import load_network

pytestmark = pytest.mark.integration


def _accept(p):
    buyer = p.actor(p.buyer_login)
    o = next(
        x
        for x in buyer.get("/v1/m/pilot_buyer/offers").json()["offers"]
        if x["status"] == "proposed"
    )
    r = buyer.post(
        f"/v1/m/pilot_buyer/offers/{o['offer_id']}/decision",
        {"decision": "accept", "terms_digest": o["terms_digest"]},
        key="accept-blocker",
    )
    assert r.status_code == 200, r.text


def _attest(p, hours):
    with p.engine.begin() as c:
        views.attest_capacity(
            c,
            merchant_id="pilot_buyer",
            user_id=p.seeded.users[p.buyer_login],
            spendable_cents=20000,
            reserve_cents=5000,
            valid_hours=hours,
            included_receipt_ids=[],
            included_outflow_ids=[],
            at=p.now,
        )


def _net(p):
    with p.engine.connect() as c:
        net = load_network(c, "sandbox_pilot_network", initiator="pilot_buyer", as_of=p.now).net
        c.rollback()
    return net


def _feasible(net):
    s = _Search(net, SolveConfig())
    o = net.option("pilot_001:r3")
    return s.due["pilot_001"], [t for t in range(0, 6) if s._eval(o, t) is not None]


def _scenario(p, hours):
    _accept(p)
    p.advance(minutes=1)
    _attest(p, hours)  # supersedes the seed's 72 h attestation
    return _net(p)


def test_accepted_offer_with_24h_evidence_plans_nothing(pilot):
    net = _scenario(pilot, 24)
    e = net.obligation("pilot_001")
    assert (e.in_force_concession.cents, e.open.cents, e.revision_no) == (100, 2400, 3)
    due, feasible = _feasible(net)
    assert due == 2 and feasible == []
    assert solve(net).schedule == ()


def test_evidence_covering_the_pay_by_window_plans_the_agreed_date(pilot):
    net = _scenario(pilot, 72)
    due, feasible = _feasible(net)
    assert feasible == [due]  # on the agreed date: not earlier (no benefit), not later
    assert [(x.option_id, x.slot) for x in solve(net).schedule] == [("pilot_001:r3", due)]


def test_acceleration_beyond_the_agreed_date_has_no_incremental_benefit(pilot):
    net = _scenario(pilot, 72)
    # Even without the minimum-discount rule, paying before the agreed date costs time value
    # at the payer's hurdle and gains nothing: the optimizer must not recommend it.
    pols = tuple(
        replace(x, min_early_discount=Money(0, USD)) if x.merchant_id == "pilot_buyer" else x
        for x in net.policies
    )
    due, feasible = _feasible(replace(net, policies=pols))
    assert feasible == [due]


def test_a_newer_reserve_still_binds_the_agreed_payment(pilot):
    net = _scenario(pilot, 72)
    pos = tuple(
        replace(x, protected_reserve=Money(19000, USD)) if x.merchant_id == "pilot_buyer" else x
        for x in net.positions
    )
    # Eligibility (timing, policy, evidence) is unchanged; cash is checked by the search.
    assert solve(replace(net, positions=pos)).schedule == ()  # 200.00 - 24.00 < 190.00


@pytest.mark.xfail(
    strict=True,
    reason=(
        "latent P2 defect (pilot blocker report): min_early_discount is compared with the "
        "INCREMENTAL concession vs the revised date; it should be the total concession vs the "
        "original terms. Fix proposed in ADR-0013; not applied without owner approval."
    ),
)
def test_min_early_discount_should_count_the_already_agreed_concession(pilot):
    net = _scenario(pilot, 72)
    # Isolate the discount rule: no time cost, no net-benefit floor. The buyer's rule "pay
    # early only for at least $1 off" is met: it already has $1 off vs its original terms.
    pols = tuple(
        replace(x, hurdle_bps=0, min_net_benefit_cents=0) if x.merchant_id == "pilot_buyer" else x
        for x in net.policies
    )
    _, feasible = _feasible(replace(net, policies=pols))
    assert 0 in feasible
