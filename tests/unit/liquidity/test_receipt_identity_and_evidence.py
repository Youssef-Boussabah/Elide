"""Review remediation (P1 review, findings 1-3): one receipt counts once, availability
needs separate evidence, and an older balance snapshot cannot bypass a newer one."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from api.domain.invoice import Provenance, SourceKind
from api.domain.money import CAD, USD, Money
from api.integrations.paypal.settlement import load_settlement
from api.liquidity.capacity import (
    AvailabilityBasis,
    AvailabilityEvidence,
    CapacityError,
    CapacityEvidence,
    Commitment,
    EvidenceConflictError,
    EvidenceKind,
    IncomingReceipt,
    InsufficientCapacityError,
    ReceiptConflictError,
    ReceiptStatus,
    SupersededEvidenceError,
    check_not_superseded,
    commit_capacity,
    compute_capacity,
    mark_available,
    select_current_evidence,
)
from api.liquidity.provider_receipts import receipt_from_settlement

T0 = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
NOW = T0 + timedelta(minutes=30)
ATTESTED = Provenance(SourceKind.MERCHANT_ATTESTED, "unit-test attestation", T0)
SYN = Provenance(SourceKind.SYNTHETIC_FIXTURE, "unit-test fixture", T0)
RUNS = Path(__file__).resolve().parents[3] / "evidence" / "paypal-sandbox" / "runs"


def usd(c):
    return Money(c, USD)


def ev(spendable=0, reserve=0, *, eid="ev1", merchant="supplier", observed=T0, **kw):
    return CapacityEvidence(
        eid,
        merchant,
        usd(spendable),
        usd(reserve),
        kw.pop("kind", EvidenceKind.MERCHANT_ATTESTED),
        observed,
        kw.pop("valid_until", observed + timedelta(hours=4)),
        kw.pop("provenance", ATTESTED),
        **kw,
    )


def attest(merchant="supplier", at=T0, ref="attestation:supplier:1"):
    return AvailabilityEvidence(AvailabilityBasis.MERCHANT_ATTESTED, ref, at, merchant)


def verified(rid="r990", cents=99000, merchant="supplier", cur=USD, at=T0):
    return IncomingReceipt(
        rid,
        merchant,
        Money(cents, cur),
        ReceiptStatus.AVAILABLE_VERIFIED,
        ATTESTED,
        available_at=at,
        availability=attest(merchant, at),
    )


# --- finding 1: one economic receipt contributes at most once ------------------------


def test_990_receipt_supplied_twice_never_becomes_1980():
    r = verified()
    s = compute_capacity(ev(), receipts=[r, r], at=NOW)
    assert s.verified_incoming == usd(99000)
    assert s.deployable == usd(99000)
    # ...and a plan cannot reserve the phantom second $990.
    first = commit_capacity(
        ev(), [], Commitment("c1", "supplier", "p1", usd(99000)), at=NOW, receipts=[r, r]
    )
    with pytest.raises(InsufficientCapacityError):
        commit_capacity(
            ev(), first, Commitment("c2", "supplier", "p2", usd(1)), at=NOW, receipts=[r, r]
        )


@pytest.mark.parametrize(
    "other",
    [
        verified(cents=99001),  # different amount
        verified(cur=CAD),  # different currency
        verified(at=T0 + timedelta(minutes=1)),  # different availability time
        IncomingReceipt("r990", "supplier", usd(99000), ReceiptStatus.HELD, ATTESTED),
    ],
)
def test_conflicting_records_for_one_receipt_raise(other):
    with pytest.raises(ReceiptConflictError):
        compute_capacity(ev(), receipts=[verified(), other], at=NOW)


def test_held_to_verified_transition_replaces_the_record_not_adds_one():
    held = IncomingReceipt("r990", "supplier", usd(99000), ReceiptStatus.HELD, ATTESTED)
    now_available = mark_available(held, attest(at=T0 + timedelta(minutes=5)), T0)
    assert now_available.receipt_id == held.receipt_id
    assert compute_capacity(ev(), receipts=[now_available], at=NOW).verified_incoming == usd(99000)
    with pytest.raises(ReceiptConflictError):  # old and new state together: refused
        compute_capacity(ev(), receipts=[held, now_available], at=NOW)
    with pytest.raises(CapacityError):
        mark_available(now_available, attest(), T0)


def test_receipt_identity_is_scoped_by_merchant():
    mine, theirs = verified(merchant="supplier"), verified(merchant="carrier")
    s = compute_capacity(ev(), receipts=[mine, theirs, theirs], at=NOW)
    assert s.verified_incoming == usd(99000)


def test_included_receipt_exclusion_still_holds_with_duplicates():
    e = ev(99000, included_receipt_ids=frozenset({"r990"}))
    s = compute_capacity(e, receipts=[verified(), verified()], at=NOW)
    assert s.verified_incoming == usd(0)
    assert s.deployable == usd(99000)  # the balance already contains it


# --- finding 3: AVAILABLE_VERIFIED needs a traceable evidence boundary ---------------


def test_status_alone_is_not_enough():
    with pytest.raises(CapacityError):
        IncomingReceipt("r1", "supplier", usd(1), ReceiptStatus.AVAILABLE_VERIFIED, ATTESTED, T0)
    with pytest.raises(CapacityError):  # unverified receipts cannot carry availability
        IncomingReceipt(
            "r1", "supplier", usd(1), ReceiptStatus.HELD, ATTESTED, T0, availability=attest()
        )


def test_merchant_can_attest_only_its_own_funds():
    with pytest.raises(CapacityError):
        IncomingReceipt(
            "r1",
            "supplier",
            usd(1),
            ReceiptStatus.AVAILABLE_VERIFIED,
            ATTESTED,
            T0,
            attest(merchant="buyer"),
        )


def test_synthetic_and_real_availability_never_mix():
    syn_basis = AvailabilityEvidence(AvailabilityBasis.SYNTHETIC, "fixture", T0)
    with pytest.raises(CapacityError):  # real receipt cannot cite synthetic availability
        IncomingReceipt(
            "r1", "supplier", usd(1), ReceiptStatus.AVAILABLE_VERIFIED, ATTESTED, T0, syn_basis
        )
    with pytest.raises(CapacityError):  # synthetic receipt cannot claim a real attestation
        IncomingReceipt(
            "r1", "supplier", usd(1), ReceiptStatus.AVAILABLE_VERIFIED, SYN, T0, attest()
        )


def test_synthetic_availability_counts_only_in_simulation():
    r = IncomingReceipt(
        "r1",
        "supplier",
        usd(99000),
        ReceiptStatus.AVAILABLE_VERIFIED,
        SYN,
        T0,
        AvailabilityEvidence(AvailabilityBasis.SYNTHETIC, "fixture", T0),
    )
    syn_ev = ev(kind=EvidenceKind.SYNTHETIC, provenance=SYN)
    real = compute_capacity(syn_ev, receipts=[r], at=NOW, allow_synthetic=True)
    assert real.verified_incoming == usd(99000)
    assert any("SYNTHETIC receipts" in x for x in real.reasons)
    s = compute_capacity(ev(), receipts=[r], at=NOW)
    assert s.verified_incoming == usd(0)
    assert "explicit simulation" in s.projected[0].reason


def test_availability_cannot_predate_the_funds():
    with pytest.raises(CapacityError):
        IncomingReceipt(
            "r1",
            "supplier",
            usd(1),
            ReceiptStatus.AVAILABLE_VERIFIED,
            ATTESTED,
            T0,
            attest(at=T0 - timedelta(seconds=1)),
        )


def test_receipt_counts_only_after_it_is_verified_not_merely_available():
    r = IncomingReceipt(
        "r1",
        "supplier",
        usd(99000),
        ReceiptStatus.AVAILABLE_VERIFIED,
        ATTESTED,
        T0,
        attest(at=NOW),
    )
    assert compute_capacity(ev(), receipts=[r], at=NOW).verified_incoming == usd(0)
    later = compute_capacity(ev(), receipts=[r], at=NOW + timedelta(seconds=1))
    assert later.verified_incoming == usd(99000)
    assert later.funds_available_from == NOW


@pytest.fixture
def paypal_receipt():
    s = load_settlement(RUNS / "20261110T221823Z.json", RUNS / "20261110T221827Z.json")
    return receipt_from_settlement(s, "supplier"), s


def test_paypal_payment_confirmation_cannot_be_promoted_by_citing_itself(paypal_receipt):
    rcpt, s = paypal_receipt
    assert rcpt.status is ReceiptStatus.PROVIDER_CONFIRMED
    at = s.ordering_time()
    for self_ref in [s.provenance.reference, *s.provenance.evidence_ref.split("; ")]:
        with pytest.raises(CapacityError, match="separate from the payment confirmation"):
            mark_available(
                rcpt, AvailabilityEvidence(AvailabilityBasis.PROVIDER_BALANCE, self_ref, at), at
            )


def test_even_separately_verified_paypal_credit_stays_out_of_usd_capacity(paypal_receipt):
    rcpt, s = paypal_receipt
    at = s.ordering_time()
    balance_check = AvailabilityEvidence(
        AvailabilityBasis.PROVIDER_BALANCE, "hypothetical:balance-check", at
    )
    r = mark_available(rcpt, balance_check, at)
    snap = compute_capacity(
        ev(merchant="supplier", observed=at), receipts=[r], at=at + timedelta(hours=1)
    )
    assert snap.verified_incoming == usd(0)  # CAD 1,306.79 never funds USD (D008)


# --- evidence kind must match where it came from -------------------------------------


@pytest.mark.parametrize(
    ("kind", "source"),
    [
        (EvidenceKind.MERCHANT_ATTESTED, SourceKind.SYNTHETIC_FIXTURE),
        (EvidenceKind.MERCHANT_ATTESTED, SourceKind.PAYPAL_SANDBOX),
        (EvidenceKind.MERCHANT_ATTESTED, SourceKind.MERCHANT_ENTERED),
        (EvidenceKind.PROVIDER_VERIFIED, SourceKind.MERCHANT_ATTESTED),
        (EvidenceKind.PROVIDER_VERIFIED, SourceKind.SYNTHETIC_FIXTURE),
        (EvidenceKind.SYNTHETIC, SourceKind.MERCHANT_ATTESTED),
        (EvidenceKind.UNKNOWN, SourceKind.SYNTHETIC_FIXTURE),
    ],
)
def test_evidence_kind_must_match_its_provenance(kind, source):
    with pytest.raises(CapacityError):
        ev(kind=kind, provenance=Provenance(source, "x", T0))


# --- finding 2 (pure rule): newer observation supersedes older ones ------------------


def test_newer_lower_balance_supersedes_an_older_still_valid_one():
    old = ev(149000, 50000, eid="ev_old", valid_until=T0 + timedelta(hours=8))
    new = ev(1000, 0, eid="ev_new", observed=T0 + timedelta(hours=1))
    at = T0 + timedelta(hours=2)
    assert compute_capacity(old, at=at).deployable == usd(99000)  # still "valid" on its own
    assert select_current_evidence([old, new], merchant_id="supplier", currency=USD, at=at) == new
    with pytest.raises(SupersededEvidenceError):
        check_not_superseded(old, [new], at=at)
    check_not_superseded(new, [old], at=at)  # the current one passes
    # Before the newer observation existed, the older one was current.
    check_not_superseded(old, [new], at=T0 + timedelta(minutes=30))


def test_expired_newest_observation_does_not_fall_back_to_an_older_one():
    old = ev(149000, eid="ev_old", valid_until=T0 + timedelta(hours=8))
    new = ev(
        149000,
        eid="ev_new",
        observed=T0 + timedelta(hours=1),
        valid_until=T0 + timedelta(hours=1, minutes=1),
    )
    at = T0 + timedelta(hours=3)
    assert select_current_evidence([old, new], merchant_id="supplier", currency=USD, at=at) == new
    with pytest.raises(SupersededEvidenceError):
        check_not_superseded(old, [new], at=at)


def test_supersession_is_scoped_by_merchant_and_currency():
    usd_ev = ev(99000, eid="ev_usd")
    cad_ev = CapacityEvidence(
        "ev_cad",
        "supplier",
        Money(130679, CAD),
        Money(0, CAD),
        EvidenceKind.MERCHANT_ATTESTED,
        T0 + timedelta(hours=1),
        T0 + timedelta(hours=4),
        ATTESTED,
    )
    other = ev(0, eid="ev_other", merchant="carrier", observed=T0 + timedelta(hours=1))
    check_not_superseded(usd_ev, [cad_ev, other], at=T0 + timedelta(hours=2))


def test_two_observations_at_the_same_instant_conflict():
    a, b = ev(1, eid="ev_a"), ev(2, eid="ev_b")
    with pytest.raises(EvidenceConflictError):
        select_current_evidence([a, b], merchant_id="supplier", currency=USD, at=NOW)
