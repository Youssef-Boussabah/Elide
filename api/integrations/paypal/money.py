"""Exact conversion between integer USD cents and PayPal's decimal-string amount format.

Never uses floats. Rejects anything that is not an exact, non-negative, 2-decimal amount.
"""

import re
from decimal import Decimal

SUPPORTED_CURRENCIES = frozenset({"USD"})  # Decision D008: one currency (USD cents).
_VALUE_RE = re.compile(r"^(0|[1-9]\d*)(\.\d{1,2})?$")


class AmountError(ValueError):
    pass


def _check_currency(currency: str) -> None:
    if currency not in SUPPORTED_CURRENCIES:
        raise AmountError(f"unsupported currency {currency!r}")


def cents_to_paypal(cents: int, currency: str = "USD") -> dict[str, str]:
    """`99000` -> `{"currency_code": "USD", "value": "990.00"}`."""
    _check_currency(currency)
    if isinstance(cents, bool) or not isinstance(cents, int):
        raise AmountError("amount must be an int number of cents")
    if cents < 0:
        raise AmountError("amount must be non-negative")
    return {"currency_code": currency, "value": f"{cents // 100}.{cents % 100:02d}"}


def paypal_to_cents(amount: dict[str, str], expected_currency: str = "USD") -> int:
    """Parse a PayPal money object. Raises on currency mismatch or inexact values."""
    _check_currency(expected_currency)
    currency = amount.get("currency_code")
    if currency != expected_currency:
        raise AmountError(f"currency mismatch: expected {expected_currency}, got {currency!r}")
    value = amount.get("value")
    if not isinstance(value, str) or not _VALUE_RE.match(value):
        raise AmountError(f"not an exact 2-decimal amount: {value!r}")
    return int(Decimal(value) * 100)
