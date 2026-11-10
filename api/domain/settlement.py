"""Provider settlement evidence: what PayPal reported about one captured payment.

Keeps every figure separate and cross-checked instead of collapsing them into one
"received" number (ADR-0007):

    invoice_face - authorized_discount = gross
    gross - provider_fee               = net           (same currency as gross)
    rate.convert(net)                  = settlement_credit   (if currencies differ)

There is exactly ONE cash receipt: `settlement_credit`, the amount the provider credited to
the merchant's balance. `net` in the invoice currency is an equivalence used to check the
conversion; it is not a second receipt.

Provider confirmation is not spendability (D006): nothing here produces capacity. The
liquidity layer decides that, and a non-USD credit is always currency-blocked (D008).

Timestamps are kept with their source field and reliability. A DISPUTED timestamp (e.g.
INV-1, the invoice transaction time that disagrees with the capture time) is preserved but
can never be used for ordering.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from api.domain.invoice import Provenance, check_aware
from api.domain.money import CurrencyMismatchError, ExchangeRate, Money, MoneyError


class SettlementEvidenceError(ValueError):
    pass


class DisputedTimestampError(SettlementEvidenceError):
    pass


class TimestampReliability(StrEnum):
    PROVIDER_REPORTED = "provider_reported"
    DISPUTED = "disputed"  # preserved, never used for ordering


@dataclass(frozen=True, slots=True)
class ProviderTimestamp:
    source_field: str  # e.g. "capture.create_time"
    value: datetime
    reliability: TimestampReliability
    evidence_ref: str
    note: str = ""

    def __post_init__(self) -> None:
        check_aware(self.value, self.source_field)
        if not isinstance(self.reliability, TimestampReliability):
            raise SettlementEvidenceError(f"invalid reliability {self.reliability!r}")


@dataclass(frozen=True, slots=True)
class ProviderSettlement:
    provider: str
    environment: str
    capture_id: str
    invoice_id: str
    order_id: str | None
    capture_status: str
    invoice_status: str
    invoice_face: Money
    authorized_discount: Money
    gross: Money
    provider_fee: Money
    net: Money
    settlement_credit: Money
    exchange_rate: ExchangeRate | None
    timestamps: tuple[ProviderTimestamp, ...]
    provenance: Provenance

    def __post_init__(self) -> None:
        if self.environment != "sandbox":
            raise SettlementEvidenceError("only Sandbox settlements exist in this project")
        if not self.capture_id or not self.invoice_id:
            raise SettlementEvidenceError("capture_id and invoice_id are required")
        try:
            if self.invoice_face - self.authorized_discount != self.gross:
                raise SettlementEvidenceError(
                    f"face {self.invoice_face} - discount {self.authorized_discount} "
                    f"!= gross {self.gross}"
                )
            if self.gross - self.provider_fee != self.net:
                raise SettlementEvidenceError(
                    f"gross {self.gross} - fee {self.provider_fee} != net {self.net}"
                )
        except (CurrencyMismatchError, MoneyError) as exc:
            raise SettlementEvidenceError(f"inconsistent settlement amounts: {exc}") from exc
        if self.settlement_credit.currency == self.net.currency:
            if self.exchange_rate is not None:
                raise SettlementEvidenceError("same-currency settlement must not carry a rate")
            if self.settlement_credit != self.net:
                raise SettlementEvidenceError("same-currency credit must equal net")
        else:
            r = self.exchange_rate
            if r is None:
                raise SettlementEvidenceError("cross-currency settlement needs the provider rate")
            if (r.source, r.target) != (self.net.currency, self.settlement_credit.currency):
                raise SettlementEvidenceError("rate direction does not match the settlement")
            if r.convert(self.net) != self.settlement_credit:
                raise SettlementEvidenceError(
                    f"{self.net} x {r.rate} does not round to {self.settlement_credit}"
                )
        if not self.timestamps:
            raise SettlementEvidenceError("at least one provider timestamp is required")

    # -- derived facts ------------------------------------------------------------------
    @property
    def is_fx_settlement(self) -> bool:
        return self.settlement_credit.currency != self.gross.currency

    def cash_receipt(self) -> Money:
        """The single amount credited to the merchant balance (never also `net`)."""
        return self.settlement_credit

    def ordering_time(self) -> datetime:
        """Earliest non-disputed provider timestamp. Raises if only disputed ones exist."""
        trusted = [
            t.value
            for t in self.timestamps
            if t.reliability is TimestampReliability.PROVIDER_REPORTED
        ]
        if not trusted:
            raise DisputedTimestampError("no undisputed provider timestamp for ordering")
        return min(trusted)

    def disputed_timestamps(self) -> tuple[ProviderTimestamp, ...]:
        return tuple(t for t in self.timestamps if t.reliability is TimestampReliability.DISPUTED)
