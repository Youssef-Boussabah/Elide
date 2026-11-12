"""The PayPal invoice that carries one payment intent: build it and verify PayPal's copy.

Pure functions, no I/O. The invoice represents the agreed obligation revision exactly:
one line of `A + C` (amount owed before the agreed concession), an invoice-level discount
`C`, total `A`, USD, due on the agreed date, one recipient (the bound Sandbox payer),
partial payments and tips disabled. The invoice number is derived from the intent id so a
lost response can be found again by search (ADR-0012 §6).

Every check compares what the **provider recorded** (from a GET) with what Elide expects,
using the strict P0 parser (`invoice_terms`), never Elide's own request.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass
from typing import Any

from api.integrations.paypal.invoice_ops import recipient_matches
from api.integrations.paypal.invoice_terms import compare_terms, invoice_terms
from api.integrations.paypal.money import cents_to_paypal

UNPAID = frozenset({"SENT", "UNPAID", "SCHEDULED"})
PAYABLE_OR_LATER = UNPAID | {"PAYMENT_PENDING", "PARTIALLY_PAID", "PAID"}


@dataclass(frozen=True, slots=True)
class ExpectedInvoice:
    session_id: str
    invoice_number: str
    obligation_id: str
    revision_no: int
    item_cents: int  # A + C
    discount_cents: int  # C
    total_cents: int  # A
    due_date: dt.date

    def __post_init__(self) -> None:
        if self.total_cents <= 0 or self.discount_cents < 0:
            raise ValueError("invalid expected amounts")
        if self.item_cents != self.total_cents + self.discount_cents:
            raise ValueError("item total must equal total + discount")


def invoice_number_for(intent_id: str) -> str:
    return "ELIDE-" + hashlib.sha256(intent_id.encode()).hexdigest()[:12].upper()


def build_payload(e: ExpectedInvoice, payer_email: str) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "currency_code": "USD",
        "invoice_number": e.invoice_number,
        "reference": e.session_id,
        "note": (
            f"Agreed payment for obligation {e.obligation_id} (revision {e.revision_no}). "
            "Elide Sandbox test identities; no real money."
        ),
        "payment_term": {"term_type": "DUE_ON_DATE_SPECIFIED", "due_date": e.due_date.isoformat()},
    }
    payload: dict[str, Any] = {
        "detail": detail,
        "primary_recipients": [{"billing_info": {"email_address": payer_email}}],
        "items": [
            {
                "name": f"Obligation {e.obligation_id} revision {e.revision_no}"[:200],
                "quantity": "1",
                "unit_amount": cents_to_paypal(e.item_cents),
                "unit_of_measure": "QUANTITY",
            }
        ],
        "configuration": {
            "partial_payment": {"allow_partial_payment": False},
            "allow_tip": False,
            "tax_calculated_after_discount": True,
            "tax_inclusive": False,
        },
    }
    if e.discount_cents:
        payload["amount"] = {
            "breakdown": {
                "discount": {"invoice_discount": {"amount": cents_to_paypal(e.discount_cents)}}
            }
        }
    return payload


def identity_problems(body: dict[str, Any], e: ExpectedInvoice, invoice_id: str) -> list[str]:
    detail = body.get("detail") or {}
    out = []
    if body.get("id") != invoice_id:
        out.append("id: provider resource is not the session's invoice")
    if detail.get("invoice_number") != e.invoice_number:
        out.append("invoice_number: differs from the session")
    if detail.get("reference") not in (None, e.session_id):
        out.append("reference: differs from the session")
    return out


def config_problems(body: dict[str, Any]) -> list[str]:
    cfg = body.get("configuration") or {}
    out = []
    if (cfg.get("partial_payment") or {}).get("allow_partial_payment") is True:
        out.append("configuration: partial payments are allowed")
    if cfg.get("allow_tip") is True:
        out.append("configuration: tips are allowed")
    return out


def terms_problems(
    body: dict[str, Any],
    e: ExpectedInvoice,
    *,
    invoice_id: str,
    payer_email: str,
    expected_status: str | None,
    allowed_statuses: frozenset[str] | None = None,
) -> list[str]:
    """Everything that must hold for the invoice to represent the agreed terms."""
    terms = invoice_terms(body)
    problems = identity_problems(body, e, invoice_id)
    problems += compare_terms(
        terms,
        face_cents=e.item_cents,
        discount_cents=e.discount_cents,
        expected_status=expected_status,
        expected_due_date=e.due_date,
    )
    if allowed_statuses is not None and terms.status not in allowed_statuses:
        problems.append(f"status: {terms.status!r} not in {sorted(allowed_statuses)}")
    if not recipient_matches(body, payer_email):
        problems.append("recipient: not exactly the bound Sandbox payer")
    problems += config_problems(body)
    return problems


def recipient_view_url(body: dict[str, Any]) -> str | None:
    url = ((body.get("detail") or {}).get("metadata") or {}).get("recipient_view_url")
    if isinstance(url, str) and url.startswith("https://www.sandbox.paypal.com/"):
        return url
    return None
