import pytest

from api.integrations.paypal.money import AmountError, cents_to_paypal, paypal_to_cents


@pytest.mark.parametrize(
    ("cents", "value"),
    [(0, "0.00"), (5, "0.05"), (99000, "990.00"), (100000, "1000.00"), (123456789, "1234567.89")],
)
def test_cents_to_paypal(cents, value):
    assert cents_to_paypal(cents) == {"currency_code": "USD", "value": value}


@pytest.mark.parametrize("bad", [-1, 1.0, True, "100"])
def test_cents_to_paypal_rejects_non_int_or_negative(bad):
    with pytest.raises(AmountError):
        cents_to_paypal(bad)


@pytest.mark.parametrize("value", ["990", "990.0", "990.00"])
def test_paypal_to_cents_accepts_exact(value):
    assert paypal_to_cents({"currency_code": "USD", "value": value}) == 99000


@pytest.mark.parametrize("value", ["990.001", "-1.00", "1e3", " 1.00", "01.00", "", "NaN"])
def test_paypal_to_cents_rejects_inexact(value):
    with pytest.raises(AmountError):
        paypal_to_cents({"currency_code": "USD", "value": value})


def test_currency_mismatch_rejected():
    with pytest.raises(AmountError):
        paypal_to_cents({"currency_code": "EUR", "value": "1.00"})
    with pytest.raises(AmountError):
        cents_to_paypal(100, "EUR")
