"""In-memory PayPal Invoicing/Payments double for tests and local demos. NOT a provider.

It reproduces the response shapes observed in the P0 Sandbox runs (negative invoice
discount, `due_amount`, one transaction per payment, a capture with a fee and an optional
CAD settlement), so the same strict parsers run against it and against PayPal. Payer
actions (`pay`, `pay_pending`, `refund`, `mark_as_paid`) exist only here, to simulate what
a human payer or merchant does at PayPal; the execution layer can never call them.

Fault injection (`inject(operation, fault)`, consumed in order):
- `"before"`: the request never reaches the provider -> RETRYABLE, nothing happens;
- `"after"`: the provider processes it, the response is lost -> UNKNOWN;
- `"5xx_after"`: processed, then HTTP 500 -> UNKNOWN;
- `"reject"` (422), `"forbidden"` (403), `"not_found"` (404): refused, nothing happens.
`search_lag(n)` hides newly created invoices from the next n searches (eventual
consistency). Every call is logged in `calls` (operation, args), and the PayPal-Request-Id
of every mutating call in `request_log` (operation, request id).

Documented PayPal behaviour it models (Invoicing "Core concepts", 2026-10-02), each
switchable so the conservative paths are tested both ways:
- `honor_request_id`: a mutating call repeated with the same request id returns the
  original result instead of acting again;
- `unique_numbers`: invoice numbers are unique per merchant; a second create with a used
  number is refused (422; the error detail used here is a mock choice, not PayPal's);
- `inject(op, "conflict")`: a concurrent duplicate with the same id fails (the generic
  idempotency guide says PayPal "might fail the second request").

Refused in production (`build_mock_provider`).
"""

from __future__ import annotations

import copy
import itertools
from collections import defaultdict, deque
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from api.integrations.paypal.execution import SANDBOX, Outcome, ProviderResult
from api.integrations.paypal.money import cents_to_paypal, paypal_to_cents
from api.integrations.paypal.redaction import fingerprint


class MockRefusedInProductionError(RuntimeError):
    pass


def build_mock_provider(app_env: str, **kw: Any) -> MockPayPal:
    if app_env == "production":
        raise MockRefusedInProductionError("the mock payment provider is refused in production")
    return MockPayPal(**kw)


def _money(cents: int, currency: str = "USD") -> dict[str, str]:
    # USD goes through the real boundary; a CAD settlement credit (provider-side only, as
    # observed in P0) is formatted here because Elide's boundary refuses non-USD (D008).
    if currency == "USD":
        return cents_to_paypal(cents, currency)
    return {"currency_code": currency, "value": f"{cents // 100}.{cents % 100:02d}"}


class MockPayPal:
    environment = SANDBOX

    def __init__(
        self,
        *,
        client_id: str = "mock-client-id",
        payee_merchant_id: str = "MOCKPAYEE001",
        settlement_currency: str = "USD",
        fx_rate: str | None = None,
        fee_bps: int = 349,
        fixed_fee_cents: int = 49,
        clock=None,
        honor_request_id: bool = True,
        unique_numbers: bool = True,
    ):
        self.honor_request_id = honor_request_id
        self.unique_numbers = unique_numbers
        self.request_log: list[tuple[str, str]] = []
        self._by_request: dict[tuple[str, str], ProviderResult] = {}
        self.client_id = client_id
        self.payee_merchant_id = payee_merchant_id
        self.settlement_currency = settlement_currency
        self.fx_rate = Decimal(fx_rate) if fx_rate else None
        self.fee_bps = fee_bps
        self.fixed_fee_cents = fixed_fee_cents
        self.clock = clock or (lambda: datetime.now(UTC))
        self.invoices: dict[str, dict[str, Any]] = {}
        self.captures: dict[str, dict[str, Any]] = {}
        self.refunds: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, tuple]] = []
        self._faults: dict[str, deque[str]] = defaultdict(deque)
        self._search_lag = 0
        self._hidden: set[str] = set()
        self._seq = itertools.count(1)

    # -- test controls ----------------------------------------------------------------------
    def inject(self, operation: str, *faults: str) -> None:
        self._faults[operation].extend(faults)

    def search_lag(self, n: int) -> None:
        self._search_lag = n

    def credential_fingerprint(self) -> str:
        return fingerprint(self.client_id)

    def created_count(self) -> int:
        return len(self.invoices)

    # -- helpers ----------------------------------------------------------------------------
    def _now(self) -> str:
        return self.clock().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _fault(self, op: str) -> str | None:
        q = self._faults.get(op)
        return q.popleft() if q else None

    def _result(self, op: str, status: int, body: Any, outcome: Outcome | None = None):
        from api.integrations.paypal.execution import classify_status

        name = body.get("name") if isinstance(body, dict) and status >= 400 else None
        return ProviderResult(
            op, outcome or classify_status(op, status), status, copy.deepcopy(body), "mockdbg", name
        )

    def _replayed(self, op: str, key: str) -> ProviderResult | None:
        self.request_log.append((op, key))
        if self.honor_request_id and (op, key) in self._by_request:
            return copy.deepcopy(self._by_request[(op, key)])
        return None

    def _remember(self, op: str, key: str, res: ProviderResult) -> None:
        if res.http_status is not None and res.http_status < 300:
            self._by_request[(op, key)] = copy.deepcopy(res)

    def _pre(self, op: str) -> tuple[str | None, ProviderResult | None]:
        f = self._fault(op)
        if f == "conflict":
            return f, self._result(op, 409, {"name": "DUPLICATE_REQUEST_IN_PROGRESS"})
        if f == "before":
            return f, ProviderResult(op, Outcome.RETRYABLE, None, None, None, "ConnectError")
        if f == "reject":
            return f, self._result(op, 422, {"name": "UNPROCESSABLE_ENTITY"})
        if f == "forbidden":
            return f, self._result(op, 403, {"name": "NOT_AUTHORIZED"})
        if f == "not_found":
            return f, self._result(op, 404, {"name": "RESOURCE_NOT_FOUND"})
        return f, None

    def _post(self, op: str, fault: str | None, ok: ProviderResult) -> ProviderResult:
        if fault == "after":
            return ProviderResult(op, Outcome.UNKNOWN, None, None, None, "ReadTimeout")
        if fault == "5xx_after":
            return self._result(op, 500, {"name": "INTERNAL_SERVER_ERROR"})
        return ok

    def _view(self, inv: dict[str, Any]) -> dict[str, Any]:
        return copy.deepcopy(inv)

    # -- PaymentProvider ---------------------------------------------------------------------
    def create_invoice(self, payload: dict[str, Any], request_key: str) -> ProviderResult:
        self.calls.append(("create_invoice", (payload.get("detail", {}).get("invoice_number"),)))
        replay = self._replayed("create_invoice", request_key)
        if replay is not None:
            return replay
        fault, early = self._pre("create_invoice")
        if early:
            return early
        number = payload["detail"].get("invoice_number")
        if self.unique_numbers and any(
            i["detail"].get("invoice_number") == number for i in self.invoices.values()
        ):
            detail = [{"issue": "MOCK_DUPLICATE_INVOICE_NUMBER"}]
            return self._result(
                "create_invoice", 422, {"name": "UNPROCESSABLE_ENTITY", "details": detail}
            )
        detail = payload["detail"]
        cur = detail["currency_code"]
        items = payload["items"]
        item_total = sum(paypal_to_cents(i["unit_amount"]) * int(i["quantity"]) for i in items)
        disc_obj = (
            (((payload.get("amount") or {}).get("breakdown") or {}).get("discount") or {})
            .get("invoice_discount", {})
            .get("amount")
        )
        discount = paypal_to_cents(disc_obj) if disc_obj else 0
        total = item_total - discount
        inv_id = f"INV2-MOCK-{next(self._seq):04d}-{request_key[-6:].upper()}"
        breakdown: dict[str, Any] = {
            "item_total": _money(item_total, cur),
            "tax_total": _money(0, cur),
        }
        if discount:
            breakdown["discount"] = {
                "invoice_discount": {
                    "amount": {"currency_code": cur, "value": "-" + _money(discount, cur)["value"]}
                }
            }
        inv = {
            "id": inv_id,
            "status": "DRAFT",
            "detail": {
                **{k: v for k, v in detail.items()},
                "invoice_date": self.clock().date().isoformat(),
                "metadata": {
                    "create_time": self._now(),
                    "recipient_view_url": f"https://www.sandbox.paypal.com/invoice/p/#MOCK-{inv_id}",
                    "invoicer_view_url": f"https://www.sandbox.paypal.com/invoice/details/MOCK-{inv_id}",
                },
            },
            "primary_recipients": copy.deepcopy(payload["primary_recipients"]),
            "items": copy.deepcopy(items),
            "configuration": copy.deepcopy(payload.get("configuration") or {}),
            "amount": {
                "currency_code": cur,
                "value": _money(total, cur)["value"],
                "breakdown": breakdown,
            },
            "due_amount": _money(total, cur),
        }
        self.invoices[inv_id] = inv
        if self._search_lag:
            self._hidden.add(inv_id)
        ok = self._result("create_invoice", 201, self._view(inv))
        self._remember("create_invoice", request_key, ok)
        return self._post("create_invoice", fault, ok)

    def send_invoice(self, invoice_id: str, request_key: str) -> ProviderResult:
        self.calls.append(("send_invoice", (invoice_id,)))
        replay = self._replayed("send_invoice", request_key)
        if replay is not None:
            return replay
        fault, early = self._pre("send_invoice")
        if early:
            return early
        inv = self.invoices.get(invoice_id)
        if inv is None:
            return self._result("send_invoice", 404, {"name": "RESOURCE_NOT_FOUND"})
        if inv["status"] not in ("DRAFT", "SENT", "UNPAID"):
            return self._result("send_invoice", 422, {"name": "UNPROCESSABLE_ENTITY"})
        inv["status"] = "SENT"
        inv["detail"]["metadata"]["first_sent_time"] = self._now()
        body = {
            "rel": "payer-view",
            "href": inv["detail"]["metadata"]["recipient_view_url"],
            "method": "GET",
        }
        ok = self._result("send_invoice", 200, body)
        self._remember("send_invoice", request_key, ok)
        return self._post("send_invoice", fault, ok)

    def cancel_invoice(self, invoice_id: str, request_key: str) -> ProviderResult:
        self.calls.append(("cancel_invoice", (invoice_id,)))
        replay = self._replayed("cancel_invoice", request_key)
        if replay is not None:
            return replay
        fault, early = self._pre("cancel_invoice")
        if early:
            return early
        inv = self.invoices.get(invoice_id)
        if inv is None:
            return self._result("cancel_invoice", 404, {"name": "RESOURCE_NOT_FOUND"})
        if inv["status"] not in ("SENT", "UNPAID"):
            return self._result("cancel_invoice", 422, {"name": "UNPROCESSABLE_ENTITY"})
        inv["status"] = "CANCELLED"
        inv["detail"]["metadata"]["cancel_time"] = self._now()
        ok = self._result("cancel_invoice", 204, None)
        self._remember("cancel_invoice", request_key, ok)
        return self._post("cancel_invoice", fault, ok)

    def get_invoice(self, invoice_id: str) -> ProviderResult:
        self.calls.append(("get_invoice", (invoice_id,)))
        fault, early = self._pre("get_invoice")
        if early:
            return early
        inv = self.invoices.get(invoice_id)
        if inv is None:
            return self._result("get_invoice", 404, {"name": "RESOURCE_NOT_FOUND"})
        return self._post("get_invoice", fault, self._result("get_invoice", 200, self._view(inv)))

    def search_invoices(self, invoice_number: str) -> ProviderResult:
        self.calls.append(("search_invoices", (invoice_number,)))
        fault, early = self._pre("search_invoices")
        if early:
            return early
        visible = [
            self._view(i)
            for k, i in self.invoices.items()
            if i["detail"].get("invoice_number") == invoice_number and k not in self._hidden
        ]
        if self._search_lag:
            self._search_lag -= 1
            if not self._search_lag:
                self._hidden.clear()
        body = {"items": visible, "total_items": len(visible), "total_pages": 1}
        return self._post("search_invoices", fault, self._result("search_invoices", 200, body))

    def get_capture(self, capture_id: str) -> ProviderResult:
        self.calls.append(("get_capture", (capture_id,)))
        fault, early = self._pre("get_capture")
        if early:
            return early
        cap = self.captures.get(capture_id)
        if cap is None:
            return self._result("get_capture", 404, {"name": "RESOURCE_NOT_FOUND"})
        return self._post(
            "get_capture", fault, self._result("get_capture", 200, copy.deepcopy(cap))
        )

    def get_refund(self, refund_id: str) -> ProviderResult:
        self.calls.append(("get_refund", (refund_id,)))
        ref = self.refunds.get(refund_id)
        if ref is None:
            return self._result("get_refund", 404, {"name": "RESOURCE_NOT_FOUND"})
        return self._result("get_refund", 200, copy.deepcopy(ref))

    # -- simulated payer / merchant actions (never reachable from the execution layer) -----
    def pay(
        self,
        invoice_id: str,
        *,
        amount_cents: int | None = None,
        pending: bool = False,
        currency: str | None = None,
        payee_merchant_id: str | None = None,
    ) -> str:
        """The payer pays in the hosted page. Returns the capture id."""
        inv = self.invoices[invoice_id]
        if inv["status"] not in ("SENT", "UNPAID", "PARTIALLY_PAID", "PAYMENT_PENDING"):
            raise ValueError(f"cannot pay an invoice in {inv['status']}")
        cur = currency or inv["amount"]["currency_code"]
        total = paypal_to_cents(inv["due_amount"])
        paid = total if amount_cents is None else amount_cents
        cap_id = f"MOCKCAP{next(self._seq):06d}"
        fee = 0 if pending else (paid * self.fee_bps + 5000) // 10000 + self.fixed_fee_cents
        net = paid - fee
        srb: dict[str, Any] = {
            "gross_amount": _money(paid, cur),
            "paypal_fee": _money(fee, cur),
            "net_amount": _money(net, cur),
        }
        if self.settlement_currency != cur and self.fx_rate is not None:
            credit = int((Decimal(net) * self.fx_rate).quantize(Decimal(1), ROUND_HALF_UP))
            srb["receivable_amount"] = _money(credit, self.settlement_currency)
            srb["exchange_rate"] = {
                "source_currency": cur,
                "target_currency": self.settlement_currency,
                "value": str(self.fx_rate),
            }
        self.captures[cap_id] = {
            "id": cap_id,
            "status": "PENDING" if pending else "COMPLETED",
            "amount": _money(paid, cur),
            "final_capture": True,
            "invoice_id": invoice_id,
            "seller_receivable_breakdown": srb,
            "payee": {"merchant_id": payee_merchant_id or self.payee_merchant_id},
            "supplementary_data": {"related_ids": {"order_id": f"MOCKORD{cap_id[-6:]}"}},
            "create_time": self._now(),
            "update_time": self._now(),
        }
        txns = inv.setdefault("payments", {"transactions": []})["transactions"]
        txns.append(
            {
                "payment_id": cap_id,
                "type": "PAYPAL",
                "transaction_type": "SALE",
                "transaction_status": "PENDING" if pending else "SUCCESS",
                "amount": _money(paid, cur),
                "payment_date_time": self._now(),
            }
        )
        paid_total = sum(
            paypal_to_cents(t["amount"]) for t in txns if t["transaction_status"] == "SUCCESS"
        )
        inv["payments"]["paid_amount"] = _money(paid_total, cur)
        remaining = (
            paypal_to_cents({"currency_code": cur, "value": inv["amount"]["value"]}) - paid_total
        )
        inv["due_amount"] = _money(max(remaining, 0), cur)
        if pending:
            inv["status"] = "PAYMENT_PENDING"
        elif remaining <= 0:
            inv["status"] = "PAID"
        else:
            inv["status"] = "PARTIALLY_PAID"
        return cap_id

    def complete_pending(self, capture_id: str) -> None:
        cap = self.captures[capture_id]
        inv = self.invoices[cap["invoice_id"]]
        cur = cap["amount"]["currency_code"]
        paid = paypal_to_cents(cap["amount"])
        fee = (paid * self.fee_bps + 5000) // 10000 + self.fixed_fee_cents
        cap["status"] = "COMPLETED"
        cap["seller_receivable_breakdown"].update(
            paypal_fee=_money(fee, cur), net_amount=_money(paid - fee, cur)
        )
        for t in inv["payments"]["transactions"]:
            if t["payment_id"] == capture_id:
                t["transaction_status"] = "SUCCESS"
        inv["payments"]["paid_amount"] = _money(paid, cur)
        inv["due_amount"] = _money(0, cur)
        inv["status"] = "PAID"

    def refund(self, capture_id: str, amount_cents: int) -> str:
        cap = self.captures[capture_id]
        inv = self.invoices[cap["invoice_id"]]
        cur = cap["amount"]["currency_code"]
        rid = f"MOCKREF{next(self._seq):06d}"
        self.refunds[rid] = {
            "id": rid,
            "status": "COMPLETED",
            "amount": _money(amount_cents, cur),
            "invoice_id": cap["invoice_id"],
            "links": [{"rel": "up", "href": f"/v2/payments/captures/{capture_id}"}],
        }
        full = amount_cents >= paypal_to_cents(cap["amount"])
        cap["status"] = "REFUNDED" if full else "PARTIALLY_REFUNDED"
        inv["status"] = "REFUNDED" if full else "PARTIALLY_REFUNDED"
        inv.setdefault("refunds", {"transactions": []})["transactions"].append(
            {"refund_id": rid, "amount": _money(amount_cents, cur)}
        )
        return rid

    def mark_as_paid(self, invoice_id: str) -> None:
        """The merchant records an external payment in PayPal's UI (not a PayPal payment)."""
        inv = self.invoices[invoice_id]
        inv["status"] = "MARKED_AS_PAID"
        inv.setdefault("payments", {"transactions": []})["transactions"].append(
            {
                "payment_id": None,
                "type": "EXTERNAL",
                "method": "BANK_TRANSFER",
                "amount": inv["due_amount"],
            }
        )
        inv["payments"]["paid_amount"] = inv["due_amount"]
        inv["due_amount"] = _money(0, inv["amount"]["currency_code"])

    def tamper(self, invoice_id: str, **fields: Any) -> None:
        """Simulate a provider-side change (e.g. a different total) for mismatch tests."""
        self.invoices[invoice_id].update(copy.deepcopy(fields))
