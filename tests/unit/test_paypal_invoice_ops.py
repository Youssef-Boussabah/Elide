"""Due-date update body, pre-send checks and payer-link extraction.

Synthetic bodies shaped like the observed Sandbox draft; not evidence of PayPal behavior.
"""

import copy
import datetime as dt

import pytest

from api.integrations.paypal.invoice_ops import (
    InvoiceOpError,
    build_due_date_update,
    extract_payer_link,
    presend_problems,
    recipient_matches,
)
from api.integrations.paypal.invoice_terms import compare_terms, invoice_terms

PAYER = "payer@example.com"
DUE = dt.date(2026, 12, 1)


def _usd(v: str) -> dict[str, str]:
    return {"currency_code": "USD", "value": v}


def _draft(**overrides) -> dict:
    body = {
        "id": "INV2-TEST",
        "status": "DRAFT",
        "detail": {
            "currency_code": "USD",
            "note": "Synthetic",
            "invoice_number": "0001",
            "invoice_date": "2026-11-10",
            "payment_term": {"term_type": "NO_DUE_DATE"},
            "metadata": {"recipient_view_url": "https://www.sandbox.paypal.com/invoice/p/#X"},
        },
        "invoicer": {},
        "primary_recipients": [{"billing_info": {"email_address": PAYER}}],
        "items": [
            {
                "id": "ITEM-1",
                "name": "x",
                "quantity": "1",
                "unit_amount": _usd("1000.00"),
                "unit_of_measure": "QUANTITY",
            }
        ],
        "configuration": {"allow_tip": False, "template_id": "TEMP-1"},
        "amount": {
            **_usd("990.00"),
            "breakdown": {
                "item_total": _usd("1000.00"),
                "discount": {"invoice_discount": {"amount": _usd("-10.00")}},
                "tax_total": _usd("0.00"),
            },
        },
        "due_amount": _usd("990.00"),
        "links": [{"rel": "self", "href": "https://api.sandbox.paypal.com/x"}],
    }
    body.update(overrides)
    return body


def test_update_changes_only_payment_term_and_preserves_terms():
    body = _draft()
    original = copy.deepcopy(body)
    p = build_due_date_update(body, DUE)
    assert body == original  # input not mutated
    assert p["detail"]["payment_term"] == {
        "term_type": "DUE_ON_DATE_SPECIFIED",
        "due_date": "2026-12-01",
    }
    for k in ("currency_code", "invoice_number", "invoice_date", "note"):
        assert p["detail"][k] == body["detail"][k]
    assert p["primary_recipients"] == body["primary_recipients"]
    assert p["items"] == [
        {
            "name": "x",
            "quantity": "1",
            "unit_amount": _usd("1000.00"),
            "unit_of_measure": "QUANTITY",
        }
    ]
    # Discount sent back positive, exactly as on creation.
    assert p["amount"] == {
        "breakdown": {"discount": {"invoice_discount": {"amount": _usd("10.00")}}}
    }
    # Provider-managed fields are not echoed back.
    assert "id" not in p and "status" not in p and "links" not in p
    assert "metadata" not in p["detail"]


def test_update_refuses_non_draft():
    with pytest.raises(InvoiceOpError):
        build_due_date_update(_draft(status="SENT"), DUE)


def test_update_refuses_due_date_not_after_invoice_date():
    with pytest.raises(InvoiceOpError):
        build_due_date_update(_draft(), dt.date(2026, 11, 10))


def _with_due(body: dict) -> dict:
    body["detail"]["payment_term"] = {
        "term_type": "DUE_ON_DATE_SPECIFIED",
        "due_date": "2026-12-01",
    }
    return body


def test_presend_passes_for_expected_draft():
    assert (
        presend_problems(
            _with_due(_draft()),
            payer_email="PAYER@example.com ",
            face_cents=100000,
            discount_cents=1000,
            expected_due_date=DUE,
        )
        == []
    )


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda b: b.update(status="SENT"), "status"),
        (lambda b: b["amount"].update(value="1000.00"), "total_cents"),
        (lambda b: b["primary_recipients"].append(b["primary_recipients"][0]), "recipient"),
        (
            lambda b: b["primary_recipients"][0]["billing_info"].update(email_address="o@x.com"),
            "recipient",
        ),
        (lambda b: b["detail"]["payment_term"].update(due_date="2026-12-02"), "due_date"),
    ],
)
def test_presend_blocks_on_any_deviation(mutate, needle):
    body = _with_due(_draft())
    mutate(body)
    problems = presend_problems(
        body, payer_email=PAYER, face_cents=100000, discount_cents=1000, expected_due_date=DUE
    )
    assert any(needle in p for p in problems), problems


def test_recipient_matches_requires_exactly_one():
    assert recipient_matches(_draft(), PAYER)
    assert not recipient_matches(_draft(primary_recipients=[]), PAYER)


def test_payer_link_prefers_send_response():
    send = {"links": [{"rel": "payer-view", "href": "https://www.sandbox.paypal.com/invoice/p/#A"}]}
    assert extract_payer_link(send, _draft()) == (
        "https://www.sandbox.paypal.com/invoice/p/#A",
        "send response links (rel=payer-view)",
    )


def test_payer_link_falls_back_to_invoice_metadata():
    url, source = extract_payer_link({}, _draft())
    assert url == "https://www.sandbox.paypal.com/invoice/p/#X"
    assert "metadata" in source


def test_payer_link_refuses_non_sandbox_and_never_constructs():
    live = {"links": [{"rel": "payer-view", "href": "https://www.paypal.com/invoice/p/#A"}]}
    body = _draft()
    body["detail"]["metadata"] = {}
    assert extract_payer_link(live, body) is None
    assert extract_payer_link({}, None) is None


def test_paid_invoice_requires_zero_remaining_balance():
    paid = _with_due(_draft(status="PAID"))
    paid["payments"] = {"paid_amount": _usd("990.00"), "transactions": [{"payment_id": "P"}]}
    paid["due_amount"] = _usd("0.00")
    t = invoice_terms(paid)
    assert compare_terms(t, face_cents=100000, discount_cents=1000, expected_status="PAID") == []
    paid["due_amount"] = _usd("10.00")
    m = compare_terms(
        invoice_terms(paid), face_cents=100000, discount_cents=1000, expected_status="PAID"
    )
    assert m == ["due_cents (remaining): provider=1000 expected=0"]
