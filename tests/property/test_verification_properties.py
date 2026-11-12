"""Property: the independent checker accepts exactly the honest provider figures
(ADR-0012 §17.1). Synthetic bodies; no database."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from hypothesis import given
from hypothesis import strategies as st

from api.accounting import reconciliation_check as rc


def _v(c: int) -> str:
    return f"{Decimal(c) / 100:.2f}"


def _bodies(face, disc, fee, rate, credit=None):
    total, net = face - disc, face - disc - fee
    honest = int((Decimal(net) * rate).quantize(Decimal(1), ROUND_HALF_UP))
    m = lambda c, cur="USD": {"currency_code": cur, "value": _v(c)}  # noqa: E731
    inv = {
        "id": "I",
        "status": "PAID",
        "amount": {
            **m(total),
            "breakdown": {
                "item_total": m(face),
                "discount": {
                    "invoice_discount": {
                        "amount": {"currency_code": "USD", "value": "-" + _v(disc)}
                    }
                },
            },
        },
        "due_amount": m(0),
        "payments": {
            "paid_amount": m(total),
            "transactions": [{"payment_id": "C", "transaction_status": "SUCCESS"}],
        },
    }
    cap = {
        "id": "C",
        "status": "COMPLETED",
        "final_capture": True,
        "invoice_id": "I",
        "amount": m(total),
        "seller_receivable_breakdown": {
            "gross_amount": m(total),
            "paypal_fee": m(fee),
            "net_amount": m(net),
            "receivable_amount": m(honest if credit is None else credit, "CAD"),
            "exchange_rate": {
                "source_currency": "USD",
                "target_currency": "CAD",
                "value": str(rate),
            },
        },
        "payee": {"merchant_id": "P"},
        "create_time": "2026-11-12T18:24:35Z",
    }
    return inv, cap, honest


AMOUNTS = st.integers(min_value=200, max_value=10**8)
RATES = st.decimals(min_value=Decimal("0.5"), max_value=Decimal("2"), places=8)


@given(face=AMOUNTS, disc_pct=st.integers(0, 20), fee_pct=st.integers(0, 10), rate=RATES)
def test_honest_figures_pass(face, disc_pct, fee_pct, rate):
    disc = face * disc_pct // 100
    fee = (face - disc) * fee_pct // 100
    inv, cap, _ = _bodies(face, disc, fee, rate)
    assert rc.facts_problems(rc.extract_facts(inv, cap)) == []


@given(face=AMOUNTS, fee_pct=st.integers(0, 10), rate=RATES, off=st.integers(1, 10**6))
def test_any_wrong_credit_is_reported(face, fee_pct, rate, off):
    fee = face * fee_pct // 100
    _, _, honest = _bodies(face, 0, fee, rate)
    exact = Decimal(face - fee) * rate
    for wrong in (honest + off, honest - off):
        # A nearest rounding of net x rate is accepted; at an exact half-cent tie either
        # neighbour is (PayPal does not document its tie rule). Anything else is reported.
        if wrong <= 0 or abs(exact - wrong) <= Decimal("0.5"):
            continue
        inv, cap, _ = _bodies(face, 0, fee, rate, credit=wrong)
        assert rc.facts_problems(rc.extract_facts(inv, cap))
