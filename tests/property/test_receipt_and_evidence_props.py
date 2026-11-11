"""Property tests (P1 review findings 1-2): duplicated receipts never add liquidity, and the
newest balance observation is always the one that governs."""

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from api.domain.invoice import Provenance, SourceKind
from api.domain.money import USD, Money
from api.liquidity.capacity import (
    AvailabilityBasis,
    AvailabilityEvidence,
    CapacityEvidence,
    EvidenceKind,
    IncomingReceipt,
    ReceiptConflictError,
    ReceiptStatus,
    SupersededEvidenceError,
    check_not_superseded,
    compute_capacity,
    select_current_evidence,
)

T0 = datetime(2026, 11, 10, tzinfo=UTC)
NOW = T0 + timedelta(hours=1)
PROV = Provenance(SourceKind.MERCHANT_ATTESTED, "property-test attestation", T0)


def evidence(eid="ev", spendable=0, observed=T0, included=frozenset()):
    return CapacityEvidence(
        eid,
        "m",
        Money(spendable, USD),
        Money(0, USD),
        EvidenceKind.MERCHANT_ATTESTED,
        observed,
        observed + timedelta(days=1),
        PROV,
        included_receipt_ids=included,
    )


def receipt(i, cents, status, merchant="m"):
    verified = status is ReceiptStatus.AVAILABLE_VERIFIED
    return IncomingReceipt(
        f"r{i}",
        merchant,
        Money(cents, USD),
        status,
        PROV,
        T0 if verified else None,
        AvailabilityEvidence(AvailabilityBasis.MERCHANT_ATTESTED, f"att:{i}", T0, merchant)
        if verified
        else None,
    )


receipts_st = st.lists(
    st.tuples(st.integers(1, 10**9), st.sampled_from(list(ReceiptStatus))), max_size=6
)


@settings(max_examples=300)
@given(
    receipts_st,
    st.lists(st.integers(0, 5), max_size=12),
    st.randoms(use_true_random=False),
    st.sets(st.integers(0, 5), max_size=3),
)
def test_duplicates_never_add_liquidity(specs, dup_idx, rnd, included):
    unique = [receipt(i, c, s) for i, (c, s) in enumerate(specs)]
    supplied = list(unique)
    for j in dup_idx:
        if unique:
            supplied.append(unique[j % len(unique)])  # identical copies of real receipts
    rnd.shuffle(supplied)
    e = evidence(included=frozenset(f"r{i}" for i in included))
    once = compute_capacity(e, receipts=unique, at=NOW)
    dup = compute_capacity(e, receipts=supplied, at=NOW)
    assert dup.verified_incoming == once.verified_incoming
    assert dup.raw_capacity_cents == once.raw_capacity_cents
    counted = sum(
        c
        for i, (c, s) in enumerate(specs)
        if s is ReceiptStatus.AVAILABLE_VERIFIED and i not in included
    )
    assert dup.verified_incoming.cents == counted


@given(st.integers(1, 10**9), st.integers(1, 10**9), st.sampled_from(list(ReceiptStatus)))
def test_any_differing_record_for_the_same_receipt_conflicts(a, b, status):
    first = receipt(0, a, ReceiptStatus.AVAILABLE_VERIFIED)
    second = receipt(0, b, status)
    if first == second:
        return
    with pytest.raises(ReceiptConflictError):
        compute_capacity(evidence(), receipts=[first, second], at=NOW)


@settings(max_examples=300)
@given(
    st.lists(
        st.tuples(st.integers(0, 600), st.integers(0, 10**9)),
        min_size=1,
        max_size=8,
        unique_by=lambda x: x[0],
    ),
    st.integers(0, 700),
)
def test_only_the_newest_observation_at_or_before_at_can_be_used(obs, at_min):
    evs = [evidence(f"ev{i}", s, T0 + timedelta(minutes=m)) for i, (m, s) in enumerate(obs)]
    at = T0 + timedelta(minutes=at_min)
    current = select_current_evidence(evs, merchant_id="m", currency=USD, at=at)
    known = [e for e in evs if e.observed_at <= at]
    if not known:
        assert current is None
        return
    assert current is not None
    assert current.observed_at == max(e.observed_at for e in known)
    for e in evs:
        others = [x for x in evs if x is not e]
        if e is current or e.observed_at > at:
            check_not_superseded(e, others, at=at)
        else:
            with pytest.raises(SupersededEvidenceError):
                check_not_superseded(e, others, at=at)
