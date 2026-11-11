"""Property tests for the capacity engine and the checked transfer simulator."""

import contextlib
from datetime import UTC, datetime, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from api.domain.invoice import Provenance, SourceKind
from api.domain.money import USD, Money
from api.liquidity.capacity import (
    AvailabilityBasis,
    AvailabilityEvidence,
    CapacityEvidence,
    CapacityStatus,
    Commitment,
    EvidenceKind,
    IncomingReceipt,
    InsufficientCapacityError,
    ReceiptStatus,
    commit_capacity,
    compute_capacity,
)
from api.liquidity.simulation import ReserveViolationError, Transfer, simulate

T0 = datetime(2026, 11, 10, tzinfo=UTC)
NOW = T0 + timedelta(minutes=10)
PROV = Provenance(SourceKind.MERCHANT_ATTESTED, "property-test attestation", T0)
amt = st.integers(0, 10**9)


def ev(spendable, reserve):
    return CapacityEvidence(
        "ev",
        "m",
        Money(spendable, USD),
        Money(reserve, USD),
        EvidenceKind.MERCHANT_ATTESTED,
        T0,
        T0 + timedelta(hours=1),
        PROV,
    )


@given(amt, amt, st.lists(st.integers(1, 10**8), max_size=8))
def test_deployable_and_deficit_partition_raw_capacity(spendable, reserve, commits):
    cs = [Commitment(f"c{i}", "m", "p", Money(c, USD)) for i, c in enumerate(commits)]
    s = compute_capacity(ev(spendable, reserve), cs, at=NOW)
    raw = spendable - reserve - sum(commits)
    assert s.raw_capacity_cents == raw
    assert s.deployable.cents >= 0 and s.deficit.cents >= 0
    assert s.deployable.cents - s.deficit.cents == raw
    assert (s.status is CapacityStatus.DEFICIT) == (raw < 0)


@given(
    amt,
    amt,
    st.lists(st.tuples(st.integers(0, 10**9), st.sampled_from(list(ReceiptStatus))), max_size=6),
)
def test_only_verified_available_receipts_ever_add_capacity(spendable, reserve, receipts):
    rs = [
        IncomingReceipt(
            f"r{i}",
            "m",
            Money(a, USD),
            status,
            PROV,
            T0 if status is ReceiptStatus.AVAILABLE_VERIFIED else None,
            AvailabilityEvidence(AvailabilityBasis.MERCHANT_ATTESTED, f"att:{i}", T0, "m")
            if status is ReceiptStatus.AVAILABLE_VERIFIED
            else None,
        )
        for i, (a, status) in enumerate(receipts)
    ]
    base = compute_capacity(ev(spendable, reserve), at=NOW)
    s = compute_capacity(ev(spendable, reserve), receipts=rs, at=NOW)
    verified = sum(a for a, st_ in receipts if st_ is ReceiptStatus.AVAILABLE_VERIFIED)
    assert s.raw_capacity_cents == base.raw_capacity_cents + verified


@settings(max_examples=200)
@given(amt, amt, st.lists(st.integers(1, 10**9), max_size=12))
def test_sequential_commitments_never_exceed_initial_deployable(spendable, reserve, requests):
    e = ev(spendable, reserve)
    initial = compute_capacity(e, at=NOW).deployable.cents
    held: tuple[Commitment, ...] = ()
    for i, r in enumerate(requests):
        with contextlib.suppress(InsufficientCapacityError):
            held = commit_capacity(
                e, held, Commitment(f"c{i}", "m", f"plan{i}", Money(r, USD)), at=NOW
            )
        total = sum(c.amount.cents for c in held)
        assert total <= initial
        assert compute_capacity(e, held, at=NOW).deployable.cents == initial - total


merchants = ["a", "b", "c", "d"]


@settings(max_examples=200)
@given(
    opening=st.lists(st.integers(0, 10**7), min_size=4, max_size=4),
    reserves=st.lists(st.integers(0, 10**6), min_size=4, max_size=4),
    legs=st.lists(
        st.tuples(
            st.sampled_from(merchants),
            st.sampled_from(merchants),
            st.integers(1, 10**7),
            st.integers(0, 10**4),
        ),
        max_size=10,
    ),
)
def test_transfers_conserve_cash_and_never_breach_reserves(opening, reserves, legs):
    op = {m: Money(c, USD) for m, c in zip(merchants, opening, strict=True)}
    rs = {m: Money(c, USD) for m, c in zip(merchants, reserves, strict=True)}
    transfers = [
        Transfer(f"l{i}", p, q, Money(a, USD), Money(f, USD))
        for i, (p, q, a, f) in enumerate(legs)
        if p != q
    ]
    # Run the longest prefix that the simulator accepts, then check invariants on it.
    accepted = []
    for t in transfers:
        try:
            simulate(op, rs, [*accepted, t])
        except ReserveViolationError:
            continue
        accepted.append(t)
    r = simulate(op, rs, accepted)
    assert Money.total(r.closing.values(), USD) + r.provider_fees == Money.total(op.values(), USD)
    for t in accepted:
        assert r.closing[t.payer] >= Money(0, USD)
    # After every accepted leg, each payer that spent is still at or above its reserve.
    cash = dict(op)
    for t in accepted:
        cash[t.payer] = cash[t.payer] - t.amount - (t.fee or Money(0, USD))
        cash[t.payee] = cash[t.payee] + t.amount
        assert cash[t.payer] >= rs[t.payer]
