from hypothesis import given
from hypothesis import strategies as st

from api.integrations.paypal.money import cents_to_paypal, paypal_to_cents


@given(st.integers(min_value=0, max_value=10**13))
def test_round_trip_is_exact(cents):
    assert paypal_to_cents(cents_to_paypal(cents)) == cents


@given(st.integers(min_value=0, max_value=10**13))
def test_format_always_two_decimals(cents):
    whole, frac = cents_to_paypal(cents)["value"].split(".")
    assert len(frac) == 2
    assert whole.isdigit()
