"""The independent replay verifier rejects every way a plan can be wrong."""

from __future__ import annotations

from dataclasses import replace

import pytest

from api.domain.invoice import DisputeStatus
from api.domain.money import CAD, USD, Money
from api.planning.network import (
    Authorization,
    AvailabilityModel,
    ConsentBasis,
    FeeStatus,
    FeeTerms,
)
from api.planning.verifier import verify
from fixtures import planning as fx

L = fx.leg
CHAIN = (L("food_001:r2:offer", 0), L("freight_001:r1", 1), L("repair_001:r1", 2))


def codes(rep) -> list[str]:
    return [v.code for v in rep.violations]


def swap_option(net, option_id, **changes):
    return replace(
        net,
        options=tuple(
            replace(o, **changes) if o.option_id == option_id else o for o in net.options
        ),
    )


def test_valid_chain_passes_and_conserves_cash():
    net = fx.network_two_candidates()
    rep = verify(net, CHAIN)
    assert rep.ok, rep.violations
    m = rep.metrics
    assert sum(o.closing_cents for o in m.merchants) + m.fees + m.in_transit == 149000


def test_same_slot_reuse_of_an_incoming_receipt_is_rejected():
    net = fx.network_two_candidates()
    rep = verify(net, (L("food_001:r2:offer", 0), L("freight_001:r1", 0)))
    assert codes(rep) == ["CAPACITY"]
    assert rep.violations[0].leg_id == "freight_001:r1"


def test_wrong_order_is_rejected():
    net = fx.network_two_candidates()
    rep = verify(net, (L("freight_001:r1", 0), L("food_001:r2:offer", 1)))
    assert "CAPACITY" in codes(rep)


def test_explicit_availability_delay_is_enforced():
    net = replace(fx.network_two_candidates(), availability=AvailabilityModel("delay2", 2))
    assert not verify(net, CHAIN).ok
    ok = (L("food_001:r2:offer", 0), L("freight_001:r1", 2), L("repair_001:r1", 4))
    assert verify(net, ok).ok


def test_duplicate_leg_is_rejected():
    net = fx.network_two_candidates()
    rep = verify(net, (*CHAIN, L("food_001:r2:offer", 3)))
    assert "DUPLICATE_LEG" in codes(rep)


def test_paying_one_invoice_twice_under_different_terms_is_rejected():
    net = fx.network_parallel_invoices()
    rep = verify(net, (L("food_001:r1", 0), L("food_001:r2:offer", 0)))
    assert "DOUBLE_SETTLEMENT" in codes(rep)


def test_parallel_invoices_are_two_obligations_not_a_duplicate():
    net = fx.network_parallel_invoices()
    assert verify(net, (L("food_001:r1", 0), L("food_002:r1", 0))).ok


def test_unknown_leg_and_out_of_range_slot():
    net = fx.network_two_candidates()
    assert codes(verify(net, (L("nope:r1", 0),))) == ["UNKNOWN_LEG"]
    assert codes(verify(net, (L("food_001:r2:offer", 31),))) == ["SLOT_OUT_OF_RANGE"]


def test_reserve_must_hold_after_every_leg_including_fees():
    net = fx.network_two_candidates()
    rep = verify(net, (L("food_001:r2:offer", 0), L("pack_001:r2:offer", 0)))
    assert codes(rep) == ["CAPACITY"]
    assert rep.violations[0].leg_id == "pack_001:r2:offer"


def test_existing_commitments_reduce_capacity():
    net = fx.network_two_candidates()
    pos = replace(net.position("buyer"), committed=Money(1, USD))
    net = replace(
        net, positions=tuple(pos if p.merchant_id == "buyer" else p for p in net.positions)
    )
    assert codes(verify(net, CHAIN[:1])) == ["CAPACITY"]


def test_expired_offer_cannot_be_used():
    net = fx.network_two_candidates(evidence_hours=48)
    rep = verify(net, (L("food_001:r2:offer", 1),))  # offer expires 60 min after it is made
    assert "OFFER_EXPIRED" in codes(rep)


def test_stale_capacity_evidence_cannot_fund_a_later_slot():
    net = fx.network_two_candidates()
    rep = verify(net, (L("food_001:r1", 1),))
    assert "EVIDENCE_EXPIRED" in codes(rep)


def test_proposal_requires_explicit_b5():
    net = fx.network_negotiation()
    rep = verify(net, (L("food_001:r3:proposal", 0),))
    assert "PROPOSAL_NOT_ENABLED" in codes(rep)


def test_buyer_minimum_discount_is_a_hard_constraint():
    net = fx.network_negotiation(buyer_deployable_extra=500)  # $995 now affordable
    rep = verify(net, (L("food_001:r2:offer", 0),))
    assert codes(rep) == ["PAYER_REJECTS"]  # $5 < buyer's $10 minimum for paying early


def test_supplier_concession_cap_is_a_hard_constraint():
    net = fx.network_two_candidates()
    pol = replace(net.policy("food_supplier"), max_concession_given=Money(500, USD))
    net = replace(
        net, policies=tuple(pol if p.merchant_id == "food_supplier" else p for p in net.policies)
    )
    assert "PAYEE_REJECTS" in codes(verify(net, CHAIN))


def test_refused_consent_disputes_currency_and_unknown_fees_are_rejected():
    net = fx.network_two_candidates()
    refused = swap_option(
        net,
        "freight_001:r1",
        authorizations=(Authorization("food_supplier", "authorize_payment", ConsentBasis.REFUSED),),
    )
    assert "CONSENT_REFUSED" in codes(verify(refused, CHAIN))

    e = replace(net.obligation("freight_001"), dispute=DisputeStatus.OPEN)
    disputed = replace(
        net,
        obligations=tuple(e if o.obligation_id == "freight_001" else o for o in net.obligations),
    )
    assert "DISPUTED" in codes(verify(disputed, CHAIN))

    unknown = swap_option(net, "freight_001:r1", fee=FeeTerms(status=FeeStatus.UNKNOWN))
    assert "FEE_UNKNOWN" in codes(verify(unknown, CHAIN))


def test_non_usd_obligation_is_currency_blocked():
    net = fx.network_cad_obligation()
    assert "CURRENCY_BLOCKED" in codes(verify(net, (L("cad_001:r1", 0),)))
    assert net.obligation("cad_001").face.currency == CAD


def test_one_merchant_cannot_authorize_for_another():
    net = fx.network_two_candidates()
    forged = swap_option(
        net,
        "freight_001:r1",
        authorizations=(
            Authorization("buyer", "authorize_payment", ConsentBasis.SYNTHETIC_ASSUMPTION),
        ),
    )
    assert "FOREIGN_AUTHORIZATION" in codes(verify(forged, CHAIN))
    missing = swap_option(net, "freight_001:r1", authorizations=())
    assert "PAYER_AUTHORIZATION_MISSING" in codes(verify(missing, CHAIN))


def test_not_participating_and_prohibited_counterparty():
    net = fx.network_two_candidates()
    pol = replace(net.policy("carrier"), participating=False)
    out = replace(
        net, policies=tuple(pol if p.merchant_id == "carrier" else p for p in net.policies)
    )
    assert "NOT_PARTICIPATING" in codes(verify(out, CHAIN))
    pol = replace(net.policy("buyer"), prohibited_counterparties=frozenset({"food_supplier"}))
    pro = replace(net, policies=tuple(pol if p.merchant_id == "buyer" else p for p in net.policies))
    assert "PROHIBITED_COUNTERPARTY" in codes(verify(pro, CHAIN))


def test_fee_above_ceiling_is_rejected():
    net = fx.network_fee_cascade()
    pol = replace(net.policy("food_supplier"), max_fee_per_leg=Money(2000, USD))
    net = replace(
        net, policies=tuple(pol if p.merchant_id == "food_supplier" else p for p in net.policies)
    )
    assert "FEE_ABOVE_CEILING" in codes(verify(net, (L("food_001:r2:offer", 0),)))


def test_conditional_fee_uses_its_ceiling_as_worst_case():
    net = fx.network_two_candidates()
    cond = swap_option(
        net,
        "food_001:r2:offer",
        fee=FeeTerms(payer_fee=Money(1, USD), status=FeeStatus.CONDITIONAL),
    )
    assert codes(verify(cond, CHAIN[:1])) == ["CAPACITY"]  # $990.01 worst case > $990


def test_required_obligation_paid_late_does_not_count():
    net = fx.network_two_candidates(evidence_hours=24 * 40)
    rep = verify(net, (L("food_001:r1", 30),), required=frozenset({"food_001"}))
    assert "REQUIRED_NOT_MET" in codes(rep)
    assert rep.metrics.on_time_face == 0


def test_hypothetical_injection_is_never_counted_as_existing_cash():
    net = fx.network_reserve_shortfall()
    rep = verify(net, (L("freight_001:r1", 0),), injections={"food_supplier": 6000})
    assert rep.ok
    sup = next(o for o in rep.metrics.merchants if o.merchant_id == "food_supplier")
    assert sup.hypothetical_cents == 6000
    assert sup.existing_draw_cents == 84000


@pytest.mark.parametrize("cents", [0, -5])
def test_injections_must_be_positive(cents):
    net = fx.network_reserve_shortfall()
    with pytest.raises(ValueError):
        verify(net, (), injections={"food_supplier": cents})
