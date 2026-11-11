"""Liquidity capacity engine: deployable, deficit, evidence validity, receipts, commitments."""

from datetime import UTC, datetime, timedelta

import pytest

from api.domain.invoice import Provenance, SourceKind
from api.domain.money import CAD, USD, CurrencyMismatchError, Money
from api.liquidity.capacity import (
    AvailabilityBasis,
    AvailabilityEvidence,
    CapacityError,
    CapacityEvidence,
    CapacityStatus,
    Commitment,
    DuplicateCommitmentError,
    EvidenceKind,
    IncomingReceipt,
    InsufficientCapacityError,
    ReceiptStatus,
    assess_outflow,
    commit_capacity,
    compute_capacity,
)

T0 = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
NOW = T0 + timedelta(minutes=30)
PROV = Provenance(SourceKind.MERCHANT_ATTESTED, "unit-test", T0)
SYN = Provenance(SourceKind.SYNTHETIC_FIXTURE, "unit-test fixture", T0)


def usd(c):
    return Money(c, USD)


def ev(spendable=149000, reserve=50000, kind=EvidenceKind.MERCHANT_ATTESTED, cur=USD, **kw):
    return CapacityEvidence(
        evidence_id=kw.pop("evidence_id", "ev1"),
        merchant_id=kw.pop("merchant_id", "buyer"),
        verified_spendable=Money(spendable, cur),
        protected_reserve=Money(reserve, cur),
        kind=kind,
        observed_at=kw.pop("observed_at", T0),
        valid_until=kw.pop("valid_until", T0 + timedelta(hours=1)),
        provenance=kw.pop("provenance", PROV),
        **kw,
    )


def commit(cid, cents, plan="plan_a", merchant="buyer"):
    return Commitment(cid, merchant, plan, usd(cents))


def receipt(rid, cents, status, available_at=None, cur=USD):
    availability = None
    if status is ReceiptStatus.AVAILABLE_VERIFIED:  # merchant's own attestation (test value)
        availability = AvailabilityEvidence(
            AvailabilityBasis.MERCHANT_ATTESTED, f"attestation:{rid}", available_at, "buyer"
        )
    return IncomingReceipt(
        rid, "buyer", Money(cents, cur), status, PROV, available_at, availability
    )


# --- core relationship -----------------------------------------------------------------


def test_deployable_is_spendable_minus_reserve_minus_committed():
    s = compute_capacity(ev(), [commit("c1", 20000)], at=NOW)
    assert s.status is CapacityStatus.AVAILABLE
    assert s.raw_capacity_cents == 149000 - 50000 - 20000
    assert s.deployable == usd(79000)
    assert s.deficit == usd(0)
    assert s.funds_available_from == T0


def test_negative_capacity_is_reported_as_deficit_not_hidden():
    s = compute_capacity(ev(spendable=40000, reserve=50000), [commit("c1", 5000)], at=NOW)
    assert s.status is CapacityStatus.DEFICIT
    assert s.deployable == usd(0)
    assert s.deficit == usd(15000)
    assert s.raw_capacity_cents == -15000


def test_reserve_exactly_binding_gives_zero_not_negative():
    s = compute_capacity(ev(spendable=50000, reserve=50000), at=NOW)
    assert (s.status, s.deployable, s.deficit) == (CapacityStatus.AVAILABLE, usd(0), usd(0))


# --- evidence validity -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("at", "status"),
    [
        (T0 - timedelta(seconds=1), CapacityStatus.NOT_YET_OBSERVED),
        (T0 + timedelta(hours=1), CapacityStatus.STALE),  # valid_until is exclusive
        (T0 + timedelta(days=3), CapacityStatus.STALE),
    ],
)
def test_evidence_outside_its_window_deploys_zero(at, status):
    s = compute_capacity(ev(), at=at)
    assert s.status is status
    assert s.deployable == usd(0)
    assert not s.usable


def test_unknown_evidence_deploys_zero():
    assert compute_capacity(ev(kind=EvidenceKind.UNKNOWN), at=NOW).deployable == usd(0)


def test_synthetic_evidence_only_in_explicit_simulation():
    assert compute_capacity(ev(kind=EvidenceKind.SYNTHETIC, provenance=SYN), at=NOW).status is (
        CapacityStatus.UNVERIFIED
    )
    sim = compute_capacity(
        ev(kind=EvidenceKind.SYNTHETIC, provenance=SYN), at=NOW, allow_synthetic=True
    )
    assert sim.deployable == usd(99000)
    assert any("SYNTHETIC" in r for r in sim.reasons)


def test_cad_balance_is_currency_blocked_and_deploys_zero_usd():
    s = compute_capacity(ev(spendable=130679, reserve=0, cur=CAD), at=NOW)
    assert s.status is CapacityStatus.CURRENCY_BLOCKED
    assert s.deployable == usd(0)
    assert s.verified_spendable == Money(130679, CAD)  # the fact is preserved


def test_malformed_evidence_rejected():
    with pytest.raises(CapacityError):
        ev(valid_until=T0)
    with pytest.raises(CurrencyMismatchError):
        CapacityEvidence(
            "ev1",
            "buyer",
            usd(1),
            Money(1, CAD),
            EvidenceKind.MERCHANT_ATTESTED,
            T0,
            T0 + timedelta(hours=1),
            PROV,
        )
    with pytest.raises(Exception):  # noqa: B017 - Money rejects negatives
        ev(spendable=-1)


# --- incoming receipts ---------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [ReceiptStatus.PROMISED, ReceiptStatus.PROVIDER_CONFIRMED, ReceiptStatus.HELD],
)
def test_unverified_receipts_add_zero_deployable(status):
    base = compute_capacity(ev(), at=NOW)
    with_rcpt = compute_capacity(ev(), receipts=[receipt("r1", 99000, status)], at=NOW)
    assert with_rcpt.deployable == base.deployable
    assert with_rcpt.verified_incoming == usd(0)
    assert [p.receipt_id for p in with_rcpt.projected] == ["r1"]


def test_paypal_paid_invoice_is_not_spendable():
    paid = receipt("paypal_capture:x", 99000, ReceiptStatus.PROVIDER_CONFIRMED)
    s = compute_capacity(ev(spendable=0, reserve=0), receipts=[paid], at=NOW)
    assert s.deployable == usd(0)
    assert "adds zero" in s.projected[0].reason


def test_verified_receipt_counts_only_after_it_is_available_no_same_slot_reuse():
    r = receipt("r1", 99000, ReceiptStatus.AVAILABLE_VERIFIED, available_at=NOW)
    same_instant = compute_capacity(ev(), receipts=[r], at=NOW)
    later = compute_capacity(ev(), receipts=[r], at=NOW + timedelta(seconds=1))
    assert same_instant.verified_incoming == usd(0)
    assert later.verified_incoming == usd(99000)
    assert later.deployable == usd(99000 + 99000)
    assert later.funds_available_from == NOW


def test_receipt_already_in_observed_balance_is_not_counted_twice():
    r = receipt(
        "r1", 99000, ReceiptStatus.AVAILABLE_VERIFIED, available_at=T0 - timedelta(minutes=1)
    )
    e = ev(included_receipt_ids=frozenset({"r1"}))
    assert compute_capacity(e, receipts=[r], at=NOW).verified_incoming == usd(0)


def test_cad_receipt_never_funds_usd_capacity():
    r = receipt("r1", 130679, ReceiptStatus.AVAILABLE_VERIFIED, T0, cur=CAD)
    s = compute_capacity(ev(), receipts=[r], at=NOW)
    assert s.verified_incoming == usd(0)
    assert "CAD receipt cannot fund USD" in s.projected[0].reason


# --- commitments / concurrent plans --------------------------------------------------


def test_same_commitment_cannot_be_counted_twice():
    with pytest.raises(DuplicateCommitmentError):
        compute_capacity(ev(), [commit("c1", 100), commit("c1", 100)], at=NOW)


def test_two_plans_cannot_both_claim_the_same_990():
    e = ev()  # deployable 99000
    first = commit_capacity(e, [], commit("c_food", 99000, "plan_food"), at=NOW)
    with pytest.raises(InsufficientCapacityError):
        commit_capacity(e, first, commit("c_pack", 99000, "plan_pack"), at=NOW)
    # A smaller amount that still fits is fine; the remaining capacity is exact.
    assert compute_capacity(e, first, at=NOW).deployable == usd(0)


def test_commitment_rejected_on_stale_or_blocked_evidence():
    with pytest.raises(InsufficientCapacityError):
        commit_capacity(ev(), [], commit("c1", 1), at=T0 + timedelta(hours=2))
    with pytest.raises(InsufficientCapacityError):
        commit_capacity(ev(cur=CAD), [], commit("c1", 1), at=NOW)


def test_commitments_of_other_merchants_are_ignored():
    other = Commitment("c9", "carrier", "plan_x", usd(99000))
    assert compute_capacity(ev(), [other], at=NOW).deployable == usd(99000)


# --- payment assessment / liquidity gap ----------------------------------------------


def test_assessment_explains_shortfall():
    s = compute_capacity(ev(spendable=99000, reserve=15000), at=NOW)
    a = assess_outflow(s, usd(90000))
    assert not a.feasible
    assert a.shortfall == usd(6000)
    assert "short by USD 60.00" in a.explanation


def test_assessment_includes_fees_and_existing_deficit():
    s = compute_capacity(ev(spendable=40000, reserve=50000), at=NOW)  # deficit 10000
    a = assess_outflow(s, usd(5000), fee=usd(100))
    assert a.shortfall == usd(15100)


def test_assessment_on_unusable_evidence_is_never_feasible():
    s = compute_capacity(ev(), at=T0 + timedelta(hours=2))
    a = assess_outflow(s, usd(1))
    assert not a.feasible
    assert a.explanation.startswith("stale")
