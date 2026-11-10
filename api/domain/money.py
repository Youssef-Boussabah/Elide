"""Exact money value types (D008).

- `Money`: a nonnegative amount in integer minor units (cents) with an explicit currency.
  Used for invoice faces, payments, fees, balances and reserves.
- `SignedAmount`: a nonzero signed movement used only by journal postings. It is a
  different type on purpose, so a debit/credit can never be used as a payable amount.
- `ExchangeRate`: a provider-reported rate (Decimal), used only to check or describe an
  FX settlement fact. The optimizer never converts currencies.

Spendability boundary: USD is the only currency that can count as deployable capacity.
Other known currencies (CAD) may be recorded as facts, e.g. a PayPal settlement credit.
No floats anywhere: amounts are `int`, rates are `Decimal`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

USD: Final = "USD"
CAD: Final = "CAD"

# Currencies that may appear as recorded facts (all have 2 minor-unit digits).
KNOWN_CURRENCIES: Final = frozenset({USD, CAD})
# Currencies that may count as spendable / deployable capacity (D008).
SPENDABLE_CURRENCIES: Final = frozenset({USD})

_DECIMAL_RE = re.compile(r"^(0|[1-9]\d*)(\.\d{1,2})?$")


class MoneyError(ValueError):
    """Base class for all money validation errors."""


class InvalidAmountError(MoneyError):
    pass


class NegativeAmountError(MoneyError):
    pass


class UnsupportedCurrencyError(MoneyError):
    pass


class CurrencyMismatchError(MoneyError):
    pass


class CurrencyNotSpendableError(MoneyError):
    """A real amount in a currency the USD-only optimizer may not spend (e.g. CAD)."""


def _check_currency(currency: object) -> str:
    if not isinstance(currency, str) or currency not in KNOWN_CURRENCIES:
        raise UnsupportedCurrencyError(f"unsupported currency {currency!r}")
    return currency


def _check_int(cents: object) -> int:
    # bool is a subclass of int; reject it explicitly.
    if isinstance(cents, bool) or not isinstance(cents, int):
        raise InvalidAmountError(f"amount must be an int number of minor units, got {cents!r}")
    return cents


@dataclass(frozen=True, slots=True)
class Money:
    cents: int
    currency: str

    def __post_init__(self) -> None:
        _check_int(self.cents)
        _check_currency(self.currency)
        if self.cents < 0:
            raise NegativeAmountError(f"money cannot be negative: {self.cents}")

    # -- construction -------------------------------------------------------------------
    @classmethod
    def zero(cls, currency: str) -> Money:
        return cls(0, currency)

    @classmethod
    def parse(cls, value: str, currency: str) -> Money:
        """Parse an exact decimal string such as "990.00". Rejects anything inexact."""
        if not isinstance(value, str) or not _DECIMAL_RE.match(value):
            raise InvalidAmountError(f"not an exact nonnegative 2-decimal amount: {value!r}")
        return cls(int(Decimal(value) * 100), currency)

    @classmethod
    def total(cls, items: Iterable[Money], currency: str) -> Money:
        acc = cls.zero(currency)
        for m in items:
            acc = acc + m
        return acc

    # -- arithmetic ---------------------------------------------------------------------
    def _same(self, other: object) -> Money:
        if not isinstance(other, Money):
            raise TypeError(f"cannot combine Money with {type(other).__name__}")
        if other.currency != self.currency:
            raise CurrencyMismatchError(f"{self.currency} vs {other.currency}")
        return other

    def __add__(self, other: object) -> Money:
        o = self._same(other)
        return Money(self.cents + o.cents, self.currency)

    def __sub__(self, other: object) -> Money:
        o = self._same(other)
        if o.cents > self.cents:
            raise NegativeAmountError(f"{self} - {o} would be negative")
        return Money(self.cents - o.cents, self.currency)

    def __mul__(self, other: object) -> Money:
        raise TypeError("Money cannot be multiplied; use an explicit rule")

    __rmul__ = __mul__
    __radd__ = __add__

    def __lt__(self, other: object) -> bool:
        return self.cents < self._same(other).cents

    def __le__(self, other: object) -> bool:
        return self.cents <= self._same(other).cents

    def __gt__(self, other: object) -> bool:
        return self.cents > self._same(other).cents

    def __ge__(self, other: object) -> bool:
        return self.cents >= self._same(other).cents

    def to_decimal_string(self) -> str:
        return f"{self.cents // 100}.{self.cents % 100:02d}"

    def __str__(self) -> str:
        return f"{self.currency} {self.to_decimal_string()}"


@dataclass(frozen=True, slots=True)
class SignedAmount:
    """A nonzero signed journal movement. Positive = debit, negative = credit."""

    cents: int
    currency: str

    def __post_init__(self) -> None:
        _check_int(self.cents)
        _check_currency(self.currency)
        if self.cents == 0:
            raise InvalidAmountError("a journal movement cannot be zero")

    @classmethod
    def debit(cls, m: Money) -> SignedAmount:
        return cls(m.cents, m.currency)

    @classmethod
    def credit(cls, m: Money) -> SignedAmount:
        return cls(-m.cents, m.currency)

    def __neg__(self) -> SignedAmount:
        return SignedAmount(-self.cents, self.currency)

    def __add__(self, other: object) -> SignedAmount:
        raise TypeError("sum journal movements through the journal, not ad hoc")

    __radd__ = __add__


def require_spendable(amount: object) -> Money:
    """Return `amount` if it may count as deployable capacity; raise otherwise."""
    if not isinstance(amount, Money):
        raise InvalidAmountError(f"spendable capacity must be Money, got {type(amount).__name__}")
    if amount.currency not in SPENDABLE_CURRENCIES:
        raise CurrencyNotSpendableError(
            f"{amount.currency} cannot count as spendable capacity (USD-only optimizer, D008)"
        )
    return amount


@dataclass(frozen=True, slots=True)
class ExchangeRate:
    """A provider-reported conversion rate `1 source = rate target` (Decimal only)."""

    source: str
    target: str
    rate: Decimal

    def __post_init__(self) -> None:
        _check_currency(self.source)
        _check_currency(self.target)
        if self.source == self.target:
            raise UnsupportedCurrencyError("exchange rate needs two different currencies")
        if not isinstance(self.rate, Decimal) or not self.rate.is_finite() or self.rate <= 0:
            raise InvalidAmountError(f"rate must be a positive finite Decimal, got {self.rate!r}")

    def convert(self, amount: Money) -> Money:
        """Convert with half-up rounding to the minor unit (matches observed PayPal value)."""
        if amount.currency != self.source:
            raise CurrencyMismatchError(f"rate is {self.source}->{self.target}, got {amount}")
        converted = (Decimal(amount.cents) * self.rate).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
        return Money(int(converted), self.target)
