"""Exact money value types: integer minor units, explicit currency, no floats."""

from decimal import Decimal

import pytest

from api.domain.money import (
    CAD,
    USD,
    CurrencyMismatchError,
    CurrencyNotSpendableError,
    ExchangeRate,
    InvalidAmountError,
    Money,
    NegativeAmountError,
    SignedAmount,
    UnsupportedCurrencyError,
    require_spendable,
)

# --- construction -----------------------------------------------------------------------


def test_money_holds_integer_cents_and_currency():
    m = Money(99000, USD)
    assert (m.cents, m.currency) == (99000, USD)
    assert str(m) == "USD 990.00"


@pytest.mark.parametrize("bad", [1.0, 990.5, Decimal("1"), "100", None, True, False])
def test_money_rejects_non_int_cents(bad):
    with pytest.raises(InvalidAmountError):
        Money(bad, USD)


def test_money_rejects_negative():
    with pytest.raises(NegativeAmountError):
        Money(-1, USD)


@pytest.mark.parametrize("bad", ["usd", "EUR", "", "US", None, 840])
def test_money_rejects_unknown_currency(bad):
    with pytest.raises(UnsupportedCurrencyError):
        Money(1, bad)


def test_money_parses_exact_decimal_strings_only():
    assert Money.parse("990.00", USD) == Money(99000, USD)
    assert Money.parse("1306.79", CAD) == Money(130679, CAD)
    assert Money.parse("0", USD) == Money(0, USD)
    for bad in ["990.001", "-1.00", "1e3", " 1.00", "1,000.00", "", "01.00", "NaN"]:
        with pytest.raises(InvalidAmountError):
            Money.parse(bad, USD)


def test_money_to_decimal_string_is_exact():
    assert Money(5, USD).to_decimal_string() == "0.05"
    assert Money(130679, CAD).to_decimal_string() == "1306.79"


# --- arithmetic -------------------------------------------------------------------------


def test_add_and_subtract_same_currency():
    assert Money(99000, USD) + Money(1000, USD) == Money(100000, USD)
    assert Money(100000, USD) - Money(1000, USD) == Money(99000, USD)


def test_subtraction_below_zero_is_an_error_not_a_negative_money():
    with pytest.raises(NegativeAmountError):
        Money(90000, USD) - Money(99000, USD)


def test_cross_currency_arithmetic_and_comparison_rejected():
    with pytest.raises(CurrencyMismatchError):
        Money(1, USD) + Money(1, CAD)
    with pytest.raises(CurrencyMismatchError):
        Money(1, USD) - Money(1, CAD)
    with pytest.raises(CurrencyMismatchError):
        _ = Money(1, USD) < Money(2, CAD)


def test_equality_requires_same_currency():
    assert Money(100, USD) != Money(100, CAD)


@pytest.mark.parametrize("other", [1, 1.0, Decimal("1")])
def test_money_cannot_be_mixed_with_bare_numbers(other):
    with pytest.raises(TypeError):
        Money(100, USD) + other
    with pytest.raises(TypeError):
        Money(100, USD) * other


def test_sum_requires_explicit_currency_for_empty_and_rejects_mixes():
    assert Money.total([], USD) == Money(0, USD)
    assert Money.total([Money(1, USD), Money(2, USD)], USD) == Money(3, USD)
    with pytest.raises(CurrencyMismatchError):
        Money.total([Money(1, USD), Money(2, CAD)], USD)


# --- signed journal movements ---------------------------------------------------------


def test_signed_amount_allows_sign_and_negation_but_not_zero_by_default():
    d = SignedAmount(99000, USD)
    assert (-d).cents == -99000
    assert SignedAmount.debit(Money(5, USD)).cents == 5
    assert SignedAmount.credit(Money(5, USD)).cents == -5
    with pytest.raises(InvalidAmountError):
        SignedAmount(0, USD)
    with pytest.raises(InvalidAmountError):
        SignedAmount(1.5, USD)


def test_signed_amount_is_not_money():
    """A journal movement can never be used where a nonnegative amount is expected."""
    with pytest.raises(TypeError):
        Money(1, USD) + SignedAmount(1, USD)
    with pytest.raises(InvalidAmountError):
        require_spendable(SignedAmount(1, USD))


# --- spendability boundary ------------------------------------------------------------


def test_usd_is_spendable():
    assert require_spendable(Money(99000, USD)) == Money(99000, USD)


def test_cad_is_a_valid_fact_but_not_spendable_usd():
    cad = Money(130679, CAD)  # constructing a CAD fact is allowed
    assert cad.currency == CAD
    with pytest.raises(CurrencyNotSpendableError):
        require_spendable(cad)


# --- exchange rates -------------------------------------------------------------------


def test_observed_paypal_conversion_reproduced_exactly():
    rate = ExchangeRate(USD, CAD, Decimal("1.35984225"))
    # 960.99 x 1.35984225 = 1306.7948038275 -> 1306.79 (half-up to the cent)
    assert rate.convert(Money(96099, USD)) == Money(130679, CAD)


def test_rate_rounding_is_half_up_at_the_cent():
    rate = ExchangeRate(USD, CAD, Decimal("1.5"))
    assert rate.convert(Money(1, USD)) == Money(2, CAD)  # 1.5 -> 2
    assert ExchangeRate(USD, CAD, Decimal("1.49")).convert(Money(1, USD)) == Money(1, CAD)


@pytest.mark.parametrize("bad", [Decimal("0"), Decimal("-1.2"), 1.35, "1.35", Decimal("NaN")])
def test_rate_rejects_non_positive_or_non_decimal(bad):
    with pytest.raises(InvalidAmountError):
        ExchangeRate(USD, CAD, bad)


def test_rate_rejects_same_currency_and_wrong_source():
    with pytest.raises(UnsupportedCurrencyError):
        ExchangeRate(USD, USD, Decimal("1"))
    rate = ExchangeRate(USD, CAD, Decimal("1.3"))
    with pytest.raises(CurrencyMismatchError):
        rate.convert(Money(1, CAD))
