"""Build a `ProviderSettlement` from redacted PayPal Sandbox evidence runs.

Reads the provider GET responses saved by `scripts/paypal_sandbox_check.py`:
- an invoice run (`GET /v2/invoicing/invoices/{id}`, record step "fetch")
- a capture run (`GET /v2/payments/captures/{id}`, record step "payment")

Nothing is assumed: every amount comes from the provider body and is parsed exactly.
The invoice transaction timestamp is marked DISPUTED because it disagrees with the
capture `create_time` by ~8 h while both are labeled UTC (tracker item INV-1).
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from api.domain.invoice import Provenance, SourceKind
from api.domain.money import ExchangeRate, Money
from api.domain.settlement import (
    ProviderSettlement,
    ProviderTimestamp,
    SettlementEvidenceError,
    TimestampReliability,
)

INV1_NOTE = (
    "INV-1: labeled UTC but ~8 h from capture.create_time; cause not determined; "
    "not used for ordering"
)


def _record(run: dict[str, Any], step: str) -> dict[str, Any]:
    for rec in run.get("records", []):
        if rec.get("step") == step and rec.get("http_status") == 200:
            return rec
    raise SettlementEvidenceError(f"run {run.get('run_id')} has no successful '{step}' record")


def _money(obj: Any) -> Money:
    if not isinstance(obj, dict):
        raise SettlementEvidenceError(f"expected a money object, got {obj!r}")
    return Money.parse(obj.get("value"), obj.get("currency_code"))


def _ts(value: Any) -> datetime:
    if not isinstance(value, str):
        raise SettlementEvidenceError(f"expected a timestamp string, got {value!r}")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def settlement_from_runs(
    invoice_run: dict[str, Any], capture_run: dict[str, Any]
) -> ProviderSettlement:
    if invoice_run.get("environment") != "sandbox" or capture_run.get("environment") != "sandbox":
        raise SettlementEvidenceError("evidence runs must be Sandbox runs")
    inv_rec = _record(invoice_run, "fetch")
    cap_rec = _record(capture_run, "payment")
    inv, cap = inv_rec["response_redacted"], cap_rec["response_redacted"]

    if cap.get("invoice_id") != inv.get("id"):
        raise SettlementEvidenceError("capture does not belong to this invoice")
    txns = (inv.get("payments") or {}).get("transactions") or []
    if len(txns) != 1 or txns[0].get("payment_id") != cap.get("id"):
        raise SettlementEvidenceError("expected exactly one invoice transaction for this capture")

    breakdown = (inv.get("amount") or {}).get("breakdown") or {}
    discount_obj = ((breakdown.get("discount") or {}).get("invoice_discount") or {}).get("amount")
    discount = Money.zero(inv["amount"]["currency_code"])
    if discount_obj:
        # PayPal returns the invoice discount with a negative sign (ADR-0005).
        discount = _money({**discount_obj, "value": discount_obj["value"].removeprefix("-")})

    srb = cap.get("seller_receivable_breakdown") or {}
    gross, fee, net = (_money(srb.get(k)) for k in ("gross_amount", "paypal_fee", "net_amount"))
    credit = _money(srb.get("receivable_amount") or srb.get("net_amount"))
    rate = None
    er = srb.get("exchange_rate")
    if er:
        try:
            rate = ExchangeRate(er["source_currency"], er["target_currency"], Decimal(er["value"]))
        except (KeyError, InvalidOperation) as exc:
            raise SettlementEvidenceError(f"unreadable exchange rate: {exc}") from exc

    inv_ref = f"evidence/paypal-sandbox/runs/{invoice_run['run_id']}.json"
    cap_ref = f"evidence/paypal-sandbox/runs/{capture_run['run_id']}.json"
    timestamps = (
        ProviderTimestamp(
            "capture.create_time",
            _ts(cap.get("create_time")),
            TimestampReliability.PROVIDER_REPORTED,
            cap_ref,
        ),
        ProviderTimestamp(
            "invoice.payments.transactions[0].payment_date_time",
            _ts(txns[0].get("payment_date_time")),
            TimestampReliability.DISPUTED,
            inv_ref,
            INV1_NOTE,
        ),
    )
    return ProviderSettlement(
        provider="paypal",
        environment="sandbox",
        capture_id=cap["id"],
        invoice_id=inv["id"],
        order_id=((cap.get("supplementary_data") or {}).get("related_ids") or {}).get("order_id"),
        capture_status=cap.get("status", ""),
        invoice_status=inv.get("status", ""),
        invoice_face=_money(breakdown.get("item_total")),
        authorized_discount=discount,
        gross=gross,
        provider_fee=fee,
        net=net,
        settlement_credit=credit,
        exchange_rate=rate,
        timestamps=timestamps,
        provenance=Provenance(
            SourceKind.PAYPAL_SANDBOX,
            f"capture {cap['id']}",
            _ts(cap.get("create_time")),
            evidence_ref=f"{inv_ref}; {cap_ref}",
        ),
    )


def load_settlement(invoice_run_path: Path, capture_run_path: Path) -> ProviderSettlement:
    return settlement_from_runs(
        json.loads(invoice_run_path.read_text(encoding="utf-8")),
        json.loads(capture_run_path.read_text(encoding="utf-8")),
    )
