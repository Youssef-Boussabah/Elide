"""P2 payment network model: identities, time slots, consent, fees and cash categories."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from api.domain.invoice import DisputeStatus
from api.domain.money import CAD, USD, Money
from api.planning.network import (
    AvailabilityModel,
    ConsentBasis,
    DuplicateLegError,
    DuplicateObligationError,
    FeeStatus,
    FeeTerms,
    InvalidLegError,
    NetworkError,
    SameSlotReuseError,
    SlotClock,
    TermsKind,
)
from fixtures import planning as fx


def usd(c: int) -> Money:
    return Money(c, USD)


# --- time slots ---------------------------------------------------------------------
def test_slot_clock_maps_due_and_offer_windows():
    clock = SlotClock.uniform(fx.PLAN_AS_OF, timedelta(days=1), 31)
    assert len(clock) == 31
    assert clock.start(0) == fx.PLAN_AS_OF
    due = fx.AS_OF + timedelta(days=30)  # fixture due date, 15:00 UTC
    # slot 29 starts at AS_OF + 29d + 30min <= due; slot 30 starts after due
    assert clock.last_slot_at_or_before(due) == 29
    assert clock.last_slot_before(fx.PLAN_AS_OF + timedelta(minutes=35)) == 0
    assert clock.last_slot_before(fx.PLAN_AS_OF) is None
    assert clock.first_slot_after(fx.PLAN_AS_OF) == 1


def test_slot_clock_rejects_unordered_or_naive_starts():
    with pytest.raises(NetworkError):
        SlotClock((fx.PLAN_AS_OF, fx.PLAN_AS_OF))
    with pytest.raises(NetworkError):
        SlotClock((fx.PLAN_AS_OF.replace(tzinfo=None),))


@pytest.mark.parametrize("lag", [0, -1])
def test_same_slot_reuse_is_unrepresentable(lag):
    with pytest.raises(SameSlotReuseError):
        AvailabilityModel("bad", default_lag=lag)
    with pytest.raises(SameSlotReuseError):
        AvailabilityModel("bad", lags=(("carrier", lag),))


def test_availability_model_per_merchant_delay():
    m = AvailabilityModel("synthetic_delay", default_lag=1, lags=(("carrier", 3),))
    assert m.lag("carrier") == 3
    assert m.lag("buyer") == 1


# --- obligations and legs ---------------------------------------------------------
def test_parallel_invoices_between_the_same_pair_stay_separate():
    net = fx.network_parallel_invoices()
    ids = [o.obligation_id for o in net.obligations if o.debtor == "buyer"]
    assert ids == ["food_001", "food_002"]
    assert {o.creditor for o in net.obligations if o.debtor == "buyer"} == {"food_supplier"}
    assert len(net.options_for("food_001")) >= 1
    assert len(net.options_for("food_002")) >= 1


def test_duplicate_obligation_is_rejected():
    net = fx.network_two_candidates()
    with pytest.raises(DuplicateObligationError):
        replace(net, obligations=(*net.obligations, net.obligations[0]))


def test_duplicate_provider_reference_is_rejected():
    net = fx.network_two_candidates()
    a, b = net.obligations[0], net.obligations[1]
    with pytest.raises(DuplicateObligationError):
        replace(
            net,
            obligations=(
                replace(a, provider_ref="paypal:sandbox:inv-1"),
                replace(b, provider_ref="paypal:sandbox:inv-1"),
                *net.obligations[2:],
            ),
        )


def test_duplicate_leg_is_rejected():
    net = fx.network_two_candidates()
    with pytest.raises(DuplicateLegError):
        replace(net, options=(*net.options, net.options[0]))


def test_leg_must_discharge_the_invoice_exactly():
    net = fx.network_two_candidates()
    o = net.options_for("food_001")[0]
    bad = replace(o, cash=usd(o.cash.cents - 1))
    with pytest.raises(InvalidLegError):
        replace(net, options=tuple(bad if x is o else x for x in net.options))


def test_leg_payer_and_payee_must_match_the_invoice():
    net = fx.network_two_candidates()
    o = net.options_for("freight_001")[0]
    bad = replace(o, payer="buyer")
    with pytest.raises(InvalidLegError):
        replace(net, options=tuple(bad if x is o else x for x in net.options))


def test_options_come_from_invoice_revisions():
    net = fx.network_negotiation()
    kinds = {o.option_id: o.terms for o in net.options_for("food_001")}
    assert kinds == {
        "food_001:r1": TermsKind.IN_FORCE,
        "food_001:r2:offer": TermsKind.OPEN_OFFER,
        "food_001:r3:proposal": TermsKind.NEGOTIATED_PROPOSAL,
    }
    cash = {o.option_id: o.cash.cents for o in net.options_for("food_001")}
    assert cash == {
        "food_001:r1": 100000,
        "food_001:r2:offer": 99500,
        "food_001:r3:proposal": 99000,
    }


def test_offer_needs_payer_acceptance_and_proposal_needs_both_parties():
    net = fx.network_negotiation()
    offer = net.option("food_001:r2:offer")
    prop = net.option("food_001:r3:proposal")
    assert {(a.merchant_id, a.action) for a in offer.authorizations} == {
        ("buyer", "accept_offer"),
        ("buyer", "authorize_payment"),
    }
    assert {(a.merchant_id, a.action) for a in prop.authorizations} == {
        ("buyer", "sign_terms"),
        ("food_supplier", "sign_terms"),
        ("buyer", "authorize_payment"),
    }
    assert all(a.basis is ConsentBasis.SYNTHETIC_ASSUMPTION for a in prop.authorizations)


def test_recorded_decision_requires_evidence_and_synthetic_has_none():
    from api.planning.network import Authorization

    with pytest.raises(NetworkError):
        Authorization("buyer", "authorize_payment", ConsentBasis.RECORDED_DECISION)
    with pytest.raises(NetworkError):
        Authorization(
            "buyer", "authorize_payment", ConsentBasis.SYNTHETIC_ASSUMPTION, evidence_ref="x"
        )
    ok = Authorization("buyer", "authorize_payment", ConsentBasis.RECORDED_DECISION, "dec_1")
    assert ok.evidence_ref == "dec_1"


def test_expired_offer_is_excluded_with_reason():
    net = fx.network_negotiation(as_of_minutes=90)  # both drafts expired by then
    assert [o.option_id for o in net.options_for("food_001")] == ["food_001:r1"]
    codes = {(e.subject_id, e.code) for e in net.exclusions}
    assert ("food_001:r2:offer", "OFFER_EXPIRED") in codes
    assert ("food_001:r3:proposal", "OFFER_EXPIRED") in codes


# --- fees ---------------------------------------------------------------------------
def test_fee_terms_distinguish_payer_debit_payee_deduction_and_unknown():
    known = FeeTerms(payer_fee=usd(100), payee_fee=usd(2901))
    assert known.status is FeeStatus.KNOWN
    with pytest.raises(NetworkError):
        FeeTerms(status=FeeStatus.UNKNOWN, payee_fee=usd(1))  # unknown has no amount
    with pytest.raises(NetworkError):
        FeeTerms(payer_fee=Money(1, CAD))


def test_sandbox_fee_is_labelled_an_example_not_a_fee_model():
    net = fx.network_fee_cascade()
    leg = net.option("food_001:r2:offer")
    assert leg.fee.payee_fee == usd(2901)
    assert leg.fee.source == "paypal_sandbox_observed_example"
    assert any("not a fee model" in n for n in net.labels)


# --- cash categories ------------------------------------------------------------------
def test_held_receipt_is_reported_but_never_scheduled():
    net = fx.network_held_receipt()
    pos = net.position("food_supplier")
    assert pos.available == usd(0)
    assert pos.inflows == ()
    assert [u.amount for u in pos.unverified] == [usd(99000)]
    assert "held" in pos.unverified[0].reason


def test_cad_settlement_credit_is_currency_blocked():
    net = fx.network_cad_settlement()
    pos = net.position("food_supplier")
    assert pos.available == usd(0)
    assert pos.blocked == (Money(130679, CAD),)
    assert any("D008" in n for n in pos.notes)


def test_positions_separate_available_pending_held_and_unverified():
    net = fx.network_two_candidates()
    buyer = net.position("buyer")
    assert (buyer.available, buyer.protected_reserve, buyer.committed) == (
        usd(149000),
        usd(50000),
        usd(0),
    )
    assert buyer.base_slack_cents == 99000
    assert buyer.evidence_kind == "synthetic"


def test_network_digest_is_stable_and_sensitive():
    a, b = fx.network_two_candidates(), fx.network_two_candidates()
    assert a.digest() == b.digest()
    o = a.options_for("food_001")[0]
    pol = replace(a.policy("buyer"), hurdle_bps=900)
    changed = replace(a, policies=tuple(pol if p.merchant_id == "buyer" else p for p in a.policies))
    assert changed.digest() != a.digest()
    assert o.option_id in str(a.to_canonical())


def test_disputed_invoice_stays_in_the_graph_but_is_flagged():
    net = fx.network_two_candidates()
    e = replace(net.obligation("pack_001"), dispute=DisputeStatus.OPEN)
    disputed = replace(
        net, obligations=tuple(e if o.obligation_id == "pack_001" else o for o in net.obligations)
    )
    assert disputed.obligation("pack_001").dispute is DisputeStatus.OPEN
