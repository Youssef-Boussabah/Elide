"""Read the money terms PayPal actually recorded on an invoice (Invoicing v2 resource).

Used to compare what the provider stored against what we asked for, instead of trusting
our own request. Every amount is parsed exactly into integer cents; a missing field is
reported as `None`, never guessed.

Observed Sandbox convention (2026-11-10, evidence/paypal-sandbox/runs/20261110T214515Z.json):
we send `invoice_discount.amount` as a positive value ("10.00") and PayPal stores and
returns it as a negative value ("-10.00"), with the invoice total already reduced. We
therefore accept a negative sign on the discount field only, and report its magnitude.
"""

import datetime as dt
from dataclasses import dataclass
from typing import Any

from api.integrations.paypal.money import AmountError, paypal_to_cents


@dataclass(frozen=True)
class InvoiceTerms:
    status: str | None
    currency: str | None
    total_cents: int | None
    due_cents: int | None  # PayPal `due_amount`: remaining balance
    item_total_cents: int | None
    invoice_discount_cents: int | None  # magnitude; see module docstring
    invoice_discount_sign: str | None  # "negative" | "positive" | None if absent
    tax_total_cents: int | None
    payment_term_type: str | None
    due_date: str | None
    recipient_count: int
    item_unit_cents: list[int | None]
    item_quantities: list[str | None]
    paid_cents: int | None
    payment_transaction_count: int
    transaction_cents: list[int | None]
    transaction_statuses: list[str | None]
    transaction_ids: list[str | None]
    problems: tuple[str, ...]


def _cents(obj: Any, label: str, problems: list[str]) -> int | None:
    if obj is None:
        return None
    try:
        return paypal_to_cents(obj)
    except (AmountError, AttributeError) as exc:
        problems.append(f"{label}: {exc}")
        return None


def _discount(obj: Any, problems: list[str]) -> tuple[int | None, str | None]:
    """Discount magnitude in cents plus the sign PayPal used. Strict otherwise."""
    if obj is None:
        return None, None
    value = obj.get("value") if isinstance(obj, dict) else None
    sign = "positive"
    if isinstance(value, str) and value.startswith("-"):
        sign = "negative"
        obj = {**obj, "value": value[1:]}
    return _cents(obj, "invoice_discount", problems), sign


def invoice_terms(body: dict[str, Any]) -> InvoiceTerms:
    problems: list[str] = []
    amount = body.get("amount") or {}
    breakdown = amount.get("breakdown") or {}
    discount = (breakdown.get("discount") or {}).get("invoice_discount") or {}
    detail = body.get("detail") or {}
    term = detail.get("payment_term") or {}
    payments = body.get("payments") or {}
    items = body.get("items") or []
    txns = payments.get("transactions") or []
    discount_cents, discount_sign = _discount(discount.get("amount"), problems)
    return InvoiceTerms(
        status=body.get("status"),
        currency=detail.get("currency_code") or amount.get("currency_code"),
        total_cents=_cents(amount or None, "amount", problems),
        due_cents=_cents(body.get("due_amount"), "due_amount", problems),
        item_total_cents=_cents(breakdown.get("item_total"), "item_total", problems),
        invoice_discount_cents=discount_cents,
        invoice_discount_sign=discount_sign,
        tax_total_cents=_cents(breakdown.get("tax_total"), "tax_total", problems),
        payment_term_type=term.get("term_type"),
        due_date=term.get("due_date"),
        recipient_count=len(body.get("primary_recipients") or []),
        item_unit_cents=[
            _cents(i.get("unit_amount"), f"items[{n}].unit_amount", problems)
            for n, i in enumerate(items)
        ],
        item_quantities=[i.get("quantity") for i in items],
        paid_cents=_cents(payments.get("paid_amount"), "paid", problems),
        payment_transaction_count=len(txns),
        transaction_cents=[
            _cents(x.get("amount"), f"transactions[{n}].amount", problems)
            for n, x in enumerate(txns)
        ],
        transaction_statuses=[x.get("transaction_status") for x in txns],
        transaction_ids=[x.get("payment_id") for x in txns],
        problems=tuple(problems),
    )


def compare_terms(
    terms: InvoiceTerms,
    *,
    face_cents: int,
    discount_cents: int,
    expected_status: str | None = None,
    expected_due_date: dt.date | None = None,
) -> list[str]:
    """Return a list of mismatches between provider-recorded terms and what we requested.

    With `expected_status="PAID"` the remaining balance must be zero, the paid amount
    must equal the total, and there must be exactly one SUCCESS transaction for the total
    (a second transaction would be a duplicate payment to investigate, not to post).
    Otherwise an unpaid invoice must owe its full total.
    """
    expected_total = face_cents - discount_cents
    checks: list[tuple[str, Any, Any]] = [
        ("currency", terms.currency, "USD"),
        ("total_cents", terms.total_cents, expected_total),
        ("item_total_cents", terms.item_total_cents, face_cents),
        ("invoice_discount_cents", terms.invoice_discount_cents, discount_cents),
        ("tax_total_cents", terms.tax_total_cents or 0, 0),
        ("item_unit_cents", terms.item_unit_cents, [face_cents]),
        ("item_quantities", terms.item_quantities, ["1"]),
        ("recipient_count", terms.recipient_count, 1),
    ]
    if expected_status == "PAID":
        checks.append(("paid_cents", terms.paid_cents, expected_total))
        checks.append(("due_cents (remaining)", terms.due_cents, 0))
        checks.append(("payment_transaction_count", terms.payment_transaction_count, 1))
        checks.append(("transaction_cents", terms.transaction_cents, [expected_total]))
        checks.append(("transaction_statuses", terms.transaction_statuses, ["SUCCESS"]))
        checks.append(("transaction_id_present", all(terms.transaction_ids), True))
    elif terms.due_cents is not None and terms.paid_cents is None:
        checks.append(("due_cents", terms.due_cents, expected_total))
    if expected_status is not None:
        checks.append(("status", terms.status, expected_status))
    if expected_due_date is not None:
        checks.append(("payment_term", terms.payment_term_type, "DUE_ON_DATE_SPECIFIED"))
        checks.append(("due_date", terms.due_date, expected_due_date.isoformat()))
    mismatches = [
        f"{k}: provider={got!r} expected={want!r}" for k, got, want in checks if got != want
    ]
    return mismatches + list(terms.problems)
