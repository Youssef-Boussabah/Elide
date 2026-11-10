"""Parsing of provider-recorded invoice terms. Synthetic bodies shaped like Invoicing v2
responses; not evidence of Sandbox behavior."""

from api.integrations.paypal.invoice_terms import compare_terms, invoice_terms


def _usd(v: str) -> dict[str, str]:
    return {"currency_code": "USD", "value": v}


def _body(total="990.00", discount="-10.00", status="DRAFT") -> dict:
    # Shape mirrors the observed Sandbox draft (discount returned negative).
    return {
        "id": "INV2-TEST",
        "status": status,
        "detail": {"currency_code": "USD", "payment_term": {"term_type": "NO_DUE_DATE"}},
        "primary_recipients": [{"billing_info": {"email_address": "payer@example.com"}}],
        "items": [{"name": "x", "quantity": "1", "unit_amount": _usd("1000.00")}],
        "amount": {
            **_usd(total),
            "breakdown": {
                "item_total": _usd("1000.00"),
                "discount": {"invoice_discount": {"amount": _usd(discount)}},
                "tax_total": _usd("0.00"),
            },
        },
        "due_amount": _usd(total),
    }


def test_parses_exact_cents():
    t = invoice_terms(_body())
    assert (t.total_cents, t.item_total_cents, t.invoice_discount_cents) == (99000, 100000, 1000)
    assert t.invoice_discount_sign == "negative"
    assert (t.due_cents, t.tax_total_cents, t.payment_term_type) == (99000, 0, "NO_DUE_DATE")
    assert t.item_unit_cents == [100000]
    assert t.status == "DRAFT"
    assert t.problems == ()


def test_positive_discount_also_accepted_and_labeled():
    t = invoice_terms(_body(discount="10.00"))
    assert (t.invoice_discount_cents, t.invoice_discount_sign) == (1000, "positive")


def test_negative_sign_only_tolerated_on_discount():
    body = _body()
    body["amount"]["breakdown"]["item_total"]["value"] = "-1000.00"
    t = invoice_terms(body)
    assert t.item_total_cents is None
    assert any(p.startswith("item_total") for p in t.problems)


def test_due_amount_mismatch_reported():
    body = _body()
    body["due_amount"] = _usd("1000.00")
    m = compare_terms(invoice_terms(body), face_cents=100000, discount_cents=1000)
    assert m == ["due_cents: provider=100000 expected=99000"]


def test_matching_terms_have_no_mismatches():
    t = invoice_terms(_body())
    assert compare_terms(t, face_cents=100000, discount_cents=1000, expected_status="DRAFT") == []


def test_ignored_discount_is_reported():
    t = invoice_terms(_body(total="1000.00", discount="-0.00"))
    m = compare_terms(t, face_cents=100000, discount_cents=1000)
    assert any(s.startswith("total_cents") for s in m)
    assert any(s.startswith("invoice_discount_cents") for s in m)


def test_missing_fields_are_none_not_guessed():
    t = invoice_terms({"status": "DRAFT"})
    assert t.total_cents is None and t.invoice_discount_cents is None
    assert compare_terms(t, face_cents=100000, discount_cents=1000) != []


def test_wrong_status_reported():
    t = invoice_terms(_body(status="SENT"))
    m = compare_terms(t, face_cents=100000, discount_cents=1000, expected_status="DRAFT")
    assert m == ["status: provider='SENT' expected='DRAFT'"]


def test_inexact_amount_is_a_problem_not_rounded():
    body = _body()
    body["amount"]["value"] = "990.001"
    t = invoice_terms(body)
    assert t.total_cents is None
    assert any("amount" in p for p in t.problems)
