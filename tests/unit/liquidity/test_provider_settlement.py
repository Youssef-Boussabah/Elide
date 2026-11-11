"""Provider settlement evidence, built from the real redacted Sandbox runs.

Evidence class: Sandbox-verified input (runs 20261110T221823Z invoice GET and
20261110T221827Z capture GET). No PayPal call is made by these tests.
"""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from api.domain.invoice import Provenance, SourceKind
from api.domain.money import CAD, USD, ExchangeRate, Money
from api.domain.settlement import (
    DisputedTimestampError,
    ProviderTimestamp,
    SettlementEvidenceError,
    TimestampReliability,
)
from api.integrations.paypal.settlement import load_settlement
from api.liquidity.capacity import (
    CapacityEvidence,
    CapacityStatus,
    EvidenceKind,
    ReceiptStatus,
    compute_capacity,
)
from api.liquidity.provider_receipts import receipt_from_settlement, usd_capacity_effect

RUNS = Path(__file__).resolve().parents[3] / "evidence" / "paypal-sandbox" / "runs"


@pytest.fixture
def settlement():
    return load_settlement(RUNS / "20261110T221823Z.json", RUNS / "20261110T221827Z.json")


def test_every_observed_figure_is_preserved_separately(settlement):
    s = settlement
    assert (s.invoice_id, s.capture_id, s.order_id) == (
        "INV2-4RUV-XPEH-4F3C-T8BW",
        "4J016440MV5285528",
        "7YW741992S8159210",
    )
    assert s.invoice_face == Money(100000, USD)
    assert s.authorized_discount == Money(1000, USD)
    assert s.gross == Money(99000, USD)
    assert s.provider_fee == Money(2901, USD)
    assert s.net == Money(96099, USD)
    assert s.settlement_credit == Money(130679, CAD)
    assert s.exchange_rate == ExchangeRate(USD, CAD, Decimal("1.35984225"))
    assert (s.invoice_status, s.capture_status) == ("PAID", "COMPLETED")
    assert s.is_fx_settlement


def test_exactly_one_cash_receipt_net_usd_is_not_a_second_receipt(settlement):
    assert settlement.cash_receipt() == Money(130679, CAD)
    assert settlement.cash_receipt() != settlement.net


def test_cad_settlement_never_becomes_usd_capacity(settlement):
    effect = usd_capacity_effect(settlement)
    assert effect.usd_deployable == Money(0, USD)
    assert any(b.startswith("D006") for b in effect.blocked_by)
    assert any(b.startswith("D008") for b in effect.blocked_by)

    rcpt = receipt_from_settlement(settlement, "food_supplier")
    assert rcpt.status is ReceiptStatus.PROVIDER_CONFIRMED
    assert rcpt.amount == Money(130679, CAD)
    ev = CapacityEvidence(
        "ev_supplier",
        "food_supplier",
        Money(0, USD),
        Money(15000, USD),
        EvidenceKind.MERCHANT_ATTESTED,
        settlement.ordering_time(),
        datetime(2026, 11, 11, tzinfo=UTC),
        Provenance(
            SourceKind.MERCHANT_ATTESTED, "unit-test attestation", settlement.ordering_time()
        ),
    )
    snap = compute_capacity(ev, receipts=[rcpt], at=datetime(2026, 11, 10, 23, tzinfo=UTC))
    assert snap.deployable == Money(0, USD)
    assert snap.status is CapacityStatus.DEFICIT  # $0 spendable vs $150 reserve


def test_cad_balance_itself_is_currency_blocked(settlement):
    ev = CapacityEvidence(
        "ev_cad",
        "food_supplier",
        settlement.settlement_credit,
        Money(0, CAD),
        EvidenceKind.PROVIDER_VERIFIED,
        settlement.ordering_time(),
        datetime(2026, 11, 11, tzinfo=UTC),
        settlement.provenance,
    )
    snap = compute_capacity(ev, at=datetime(2026, 11, 10, 23, tzinfo=UTC))
    assert snap.status is CapacityStatus.CURRENCY_BLOCKED
    assert snap.deployable == Money(0, USD)


def test_timestamps_keep_provenance_and_disputed_one_never_orders(settlement):
    assert settlement.ordering_time() == datetime(2026, 11, 10, 22, 15, 48, tzinfo=UTC)
    (disputed,) = settlement.disputed_timestamps()
    assert disputed.source_field == "invoice.payments.transactions[0].payment_date_time"
    assert disputed.value == datetime(2026, 11, 10, 14, 15, 49, tzinfo=UTC)  # preserved raw
    assert "INV-1" in disputed.note
    only_disputed = replace(settlement, timestamps=(disputed,))
    with pytest.raises(DisputedTimestampError):
        only_disputed.ordering_time()


@pytest.mark.parametrize(
    "change",
    [
        {"provider_fee": Money(2900, USD)},  # gross - fee != net
        {"authorized_discount": Money(0, USD)},  # face - discount != gross
        {"settlement_credit": Money(130680, CAD)},  # conversion mismatch
        {"exchange_rate": None},  # cross-currency without a rate
        {"exchange_rate": ExchangeRate(CAD, USD, Decimal("0.73"))},  # wrong direction
        {"environment": "live"},
        {"timestamps": ()},
        {"provider_fee": Money(2901, CAD)},  # currency mix
    ],
)
def test_inconsistent_settlement_evidence_rejected(settlement, change):
    with pytest.raises(SettlementEvidenceError):
        replace(settlement, **change)


def test_same_currency_settlement_must_equal_net(settlement):
    usd_settled = replace(settlement, settlement_credit=settlement.net, exchange_rate=None)
    assert usd_settled.cash_receipt() == Money(96099, USD)
    with pytest.raises(SettlementEvidenceError):
        replace(settlement, settlement_credit=Money(99000, USD), exchange_rate=None)


def test_timestamp_requires_timezone():
    with pytest.raises(Exception):  # noqa: B017
        ProviderTimestamp("x", datetime(2026, 11, 10), TimestampReliability.DISPUTED, "ref")
