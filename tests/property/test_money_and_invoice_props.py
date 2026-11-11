"""Property tests: money arithmetic and the invoice settlement identity under random
sequences of payments, reversals and concession changes (synthetic values)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from api.domain.invoice import (
    InvoiceError,
    InvoiceRecord,
    Obligation,
    ObligationRevision,
    Provenance,
    RevisionStatus,
    SettlementEvent,
    SettlementKind,
    SettlementStatus,
    SourceKind,
)
from api.domain.money import CAD, USD, ExchangeRate, Money, MoneyError

cents = st.integers(min_value=0, max_value=10**12)
T0 = datetime(2026, 11, 1, tzinfo=UTC)
PROV = Provenance(SourceKind.SYNTHETIC_FIXTURE, "property-test", T0)
PARTIES = frozenset({"buyer", "seller"})


@given(cents, cents)
def test_add_then_subtract_round_trips(a, b):
    assert (Money(a, USD) + Money(b, USD)) - Money(b, USD) == Money(a, USD)


@given(cents, cents)
def test_subtraction_never_yields_negative_money(a, b):
    try:
        r = Money(a, USD) - Money(b, USD)
    except MoneyError:
        assert b > a
    else:
        assert r.cents == a - b >= 0


@given(cents)
def test_parse_and_format_round_trip(c):
    m = Money(c, USD)
    assert Money.parse(m.to_decimal_string(), USD) == m


@given(
    cents,
    st.decimals(min_value=Decimal("0.0001"), max_value=Decimal("1000"), places=8),
)
def test_conversion_error_is_at_most_half_a_cent(c, rate):
    converted = ExchangeRate(USD, CAD, rate).convert(Money(c, USD))
    exact = Decimal(c) * rate
    assert abs(Decimal(converted.cents) - exact) <= Decimal("0.5")


# --- invoice state machine --------------------------------------------------------------

op = st.one_of(
    st.tuples(st.just("pay"), st.integers(1, 120_000)),
    st.tuples(st.just("reverse"), st.integers(1, 120_000)),
    st.tuples(st.just("concede"), st.integers(0, 120_000)),
    st.tuples(st.just("replay"), st.integers(0, 50)),
)


@settings(max_examples=300)
@given(face=st.integers(1, 100_000), ops=st.lists(op, max_size=25))
def test_settlement_identity_survives_any_operation_sequence(face, ops):
    ob = Obligation("inv", "buyer", "seller", Money(face, USD), T0, PROV)
    r1 = ObligationRevision(
        "inv",
        1,
        RevisionStatus.AUTHORIZED,
        T0 + timedelta(days=30),
        "UTC",
        Money(0, USD),
        T0,
        PROV,
        authorized_by=PARTIES,
    )
    record = InvoiceRecord.open(ob, r1)
    applied: dict[str, int] = {}
    reversed_: dict[str, int] = {}
    for i, (kind, amount) in enumerate(ops):
        at = T0 + timedelta(minutes=i + 1)
        before = record
        try:
            if kind == "pay":
                record = record.apply(
                    SettlementEvent(
                        f"p{i}", "inv", SettlementKind.CASH_APPLIED, Money(amount, USD), at, PROV
                    )
                )
                applied[f"p{i}"] = amount
            elif kind == "reverse" and applied:
                target = sorted(applied)[amount % len(applied)]
                record = record.apply(
                    SettlementEvent(
                        f"r{i}",
                        "inv",
                        SettlementKind.CASH_REVERSED,
                        Money(amount, USD),
                        at,
                        PROV,
                        target,
                    )
                )
                reversed_[target] = reversed_.get(target, 0) + amount
            elif kind == "concede":
                record = record.add_revision(
                    ObligationRevision(
                        "inv",
                        len(record.revisions) + 1,
                        RevisionStatus.AUTHORIZED,
                        T0 + timedelta(days=30),
                        "UTC",
                        Money(amount, USD),
                        at,
                        PROV,
                        authorized_by=PARTIES,
                    )
                )
            elif kind == "replay" and record.events:
                ev = record.events[amount % len(record.events)]
                assert record.apply(ev) is record  # identical replay never double-counts
        except InvoiceError:
            assert record is before  # a rejected change leaves no partial state
        except MoneyError:
            assert record is before

        p = record.position()  # constructor re-checks the identity
        assert p.cash_applied + p.authorized_concession + p.remaining_open == p.original_open
        assert p.original_open == Money(face, USD)
        assert p.cash_applied.cents == sum(applied.values()) - sum(reversed_.values())
        for target, amt in reversed_.items():
            assert amt <= applied[target]
        if p.remaining_open.cents == 0:
            assert p.status is SettlementStatus.SETTLED
