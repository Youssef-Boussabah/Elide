"""Read the money terms PayPal actually recorded on an invoice (Invoicing v2 resource).

Used to compare what the provider stored against what we asked for, instead of trusting
our own request. Every amount is parsed exactly into integer cents; a missing field is
reported as `None`, never guessed.

Observed Sandbox convention (2026-11-10, evidence/paypal-sandbox/runs/20261110T214515Z.json):
we send `invoice_discount.amount` as a positive value ("10.00") and PayPal stores and
returns it as a negative value ("-10.00"), with the invoice total already reduced. We
therefore accept a negative sign on the discount field only, and report its magnitude.
"""

from dataclasses import dataclass
from typing import Any

from api.integrations.paypal.money import AmountError, paypal_to_cents


@dataclass(frozen=True)
class InvoiceTerms:
    status: str | None
    currency: str | None
    total_cents: int | None
    due_cents: int | None
    item_total_cents: int | None
    invoice_discount_cents: int | None  # magnitude; see module docstring
    invoice_discount_sign: str | None  # "negative" | "positive" | None if absent
    tax_total_cents: int | None
    payment_term_type: str | None
    item_unit_cents: list[int | None]
    item_quantities: list[str | None]
    paid_cents: int | None
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
    items = body.get("items") or []
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
        payment_term_type=(detail.get("payment_term") or {}).get("term_type"),
        item_unit_cents=[
            _cents(i.get("unit_amount"), f"items[{n}].unit_amount", problems)
            for n, i in enumerate(items)
        ],
        item_quantities=[i.get("quantity") for i in items],
        paid_cents=_cents((body.get("payments") or {}).get("paid_amount"), "paid", problems),
        problems=tuple(problems),
    )


def compare_terms(
    terms: InvoiceTerms,
    *,
    face_cents: int,
    discount_cents: int,
    expected_status: str | None = None,
) -> list[str]:
    """Return a list of mismatches between provider-recorded terms and what we requested."""
    expected_total = face_cents - discount_cents
    checks = [
        ("currency", terms.currency, "USD"),
        ("total_cents", terms.total_cents, expected_total),
        ("item_total_cents", terms.item_total_cents, face_cents),
        ("invoice_discount_cents", terms.invoice_discount_cents, discount_cents),
        ("tax_total_cents", terms.tax_total_cents or 0, 0),
        ("item_unit_cents", terms.item_unit_cents, [face_cents]),
        ("item_quantities", terms.item_quantities, ["1"]),
    ]
    if terms.due_cents is not None and terms.paid_cents is None:
        checks.append(("due_cents", terms.due_cents, expected_total))
    if expected_status is not None:
        checks.append(("status", terms.status, expected_status))
    mismatches = [
        f"{k}: provider={got!r} expected={want!r}" for k, got, want in checks if got != want
    ]
    return mismatches + list(terms.problems)
