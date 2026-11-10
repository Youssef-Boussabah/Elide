"""Pure helpers for acting on an existing PayPal invoice (Invoicing v2, Sandbox).

- `build_due_date_update`: full-replace (PUT) body built from the provider's own current
  representation, changing only the payment term. Everything else is carried over.
- `presend_problems`: checks that must all pass before an invoice may be sent.
- `extract_payer_link`: reads the hosted payer URL PayPal returned. Never constructs one.

No network access here; the caller fetches and sends.
"""

import datetime as dt
from typing import Any

from api.integrations.paypal.invoice_terms import compare_terms, invoice_terms

SANDBOX_PAYER_PREFIX = "https://www.sandbox.paypal.com/"

# Read-only / provider-managed fields that are not sent back on a full update.
_ITEM_FIELDS = ("name", "description", "quantity", "unit_amount", "unit_of_measure", "tax")
_DETAIL_FIELDS = ("currency_code", "invoice_number", "invoice_date", "note", "reference", "memo")


class InvoiceOpError(ValueError):
    pass


def build_due_date_update(body: dict[str, Any], due_date: dt.date) -> dict[str, Any]:
    """Return a PUT body equal to the current invoice except `payment_term`.

    The invoice discount is sent back as a positive amount, as on creation; PayPal
    returns it negative (see invoice_terms). Only DRAFT invoices are updated here.
    """
    if body.get("status") != "DRAFT":
        raise InvoiceOpError(f"refusing to update invoice in status {body.get('status')!r}")
    detail = body.get("detail") or {}
    invoice_date = dt.date.fromisoformat(detail["invoice_date"])
    if due_date <= invoice_date:
        raise InvoiceOpError("due date must be after the invoice date")

    new_detail = {k: detail[k] for k in _DETAIL_FIELDS if k in detail}
    new_detail["payment_term"] = {
        "term_type": "DUE_ON_DATE_SPECIFIED",
        "due_date": due_date.isoformat(),
    }
    payload: dict[str, Any] = {
        "detail": new_detail,
        "primary_recipients": body["primary_recipients"],
        "items": [{k: i[k] for k in _ITEM_FIELDS if k in i} for i in body.get("items") or []],
    }
    if body.get("invoicer"):
        payload["invoicer"] = body["invoicer"]
    if body.get("configuration"):
        payload["configuration"] = body["configuration"]

    discount = ((body.get("amount") or {}).get("breakdown") or {}).get("discount") or {}
    inv_disc = (discount.get("invoice_discount") or {}).get("amount")
    if inv_disc:
        value = inv_disc["value"].removeprefix("-")
        payload["amount"] = {
            "breakdown": {
                "discount": {
                    "invoice_discount": {
                        "amount": {"currency_code": inv_disc["currency_code"], "value": value}
                    }
                }
            }
        }
    return payload


def _recipient_emails(body: dict[str, Any]) -> list[str]:
    return [
        ((r.get("billing_info") or {}).get("email_address") or "").strip().lower()
        for r in body.get("primary_recipients") or []
    ]


def recipient_matches(body: dict[str, Any], email: str) -> bool:
    """True only if there is exactly one recipient and it is `email` (case-insensitive)."""
    return _recipient_emails(body) == [email.strip().lower()]


def presend_problems(
    body: dict[str, Any],
    *,
    payer_email: str,
    face_cents: int,
    discount_cents: int,
    expected_due_date: dt.date | None,
) -> list[str]:
    """Everything that must hold before sending. Empty list means safe to send."""
    terms = invoice_terms(body)
    problems = compare_terms(
        terms,
        face_cents=face_cents,
        discount_cents=discount_cents,
        expected_status="DRAFT",
        expected_due_date=expected_due_date,
    )
    if not recipient_matches(body, payer_email):
        problems.append("recipient: not exactly one recipient equal to PAYPAL_SANDBOX_PAYER_EMAIL")
    return problems


def extract_payer_link(
    send_body: Any, invoice_body: dict[str, Any] | None
) -> tuple[str, str] | None:
    """Return (url, source) from PayPal's own data, or None. Sandbox URLs only."""
    candidates: list[tuple[Any, str]] = []
    if isinstance(send_body, dict):
        if send_body.get("rel") == "payer-view":
            candidates.append((send_body.get("href"), "send response (rel=payer-view)"))
        for link in send_body.get("links") or []:
            if link.get("rel") == "payer-view":
                candidates.append((link.get("href"), "send response links (rel=payer-view)"))
    if invoice_body:
        meta = (invoice_body.get("detail") or {}).get("metadata") or {}
        candidates.append(
            (meta.get("recipient_view_url"), "invoice detail.metadata.recipient_view_url")
        )
    for url, source in candidates:
        if isinstance(url, str) and url.startswith(SANDBOX_PAYER_PREFIX):
            return url, source
    return None
