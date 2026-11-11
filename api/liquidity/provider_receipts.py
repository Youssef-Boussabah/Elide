"""How a provider settlement may (not) affect liquidity.

A PayPal capture is provider confirmation, so it becomes an `IncomingReceipt` with status
PROVIDER_CONFIRMED: zero deployable capacity until the merchant's usable balance is
separately verified (D006). If the credit is not USD it can never become USD capacity at
all (D008); `usd_capacity_effect` states which rule blocks it.

There is deliberately no function that turns a settlement into an available receipt.
Becoming AVAILABLE_VERIFIED requires `capacity.mark_available` with `AvailabilityEvidence`
(a merchant attestation of its own funds, or an independently fetched available-balance
check) whose reference differs from the capture/invoice evidence; citing the payment
confirmation itself is refused. No balance check is implemented yet (P5).
"""

from __future__ import annotations

from dataclasses import dataclass

from api.domain.money import SPENDABLE_CURRENCIES, USD, Money
from api.domain.settlement import ProviderSettlement
from api.liquidity.capacity import IncomingReceipt, ReceiptStatus


@dataclass(frozen=True, slots=True)
class CapacityEffect:
    usd_deployable: Money
    blocked_by: tuple[str, ...]


def receipt_from_settlement(settlement: ProviderSettlement, merchant_id: str) -> IncomingReceipt:
    return IncomingReceipt(
        receipt_id=f"paypal_capture:{settlement.capture_id.lower()}",
        merchant_id=merchant_id,
        amount=settlement.cash_receipt(),  # the single credited amount
        status=ReceiptStatus.PROVIDER_CONFIRMED,
        provenance=settlement.provenance,
        available_at=None,
    )


def usd_capacity_effect(settlement: ProviderSettlement) -> CapacityEffect:
    blocked: list[str] = ["D006: provider confirmation is not verified usable funds"]
    credit = settlement.cash_receipt()
    if credit.currency not in SPENDABLE_CURRENCIES:
        rate = settlement.exchange_rate.rate if settlement.exchange_rate else "?"
        blocked.append(
            f"D008: settled as {credit} at {rate} {settlement.gross.currency}->"
            f"{credit.currency}; not USD capacity"
        )
    return CapacityEffect(Money.zero(USD), tuple(blocked))
