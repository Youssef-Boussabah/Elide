"""Redacted settlement evidence for the P5 pilot payment. READ-ONLY.

    DATABASE_URL=<pilot database url> uv run python scripts/p5_settlement_evidence.py \\
        --intent pin_7452d6099eaf9caf --stage before-booking|after-booking

PayPal calls (read-only): `GET /v2/invoicing/invoices/{id}` and, if the invoice lists a
payment, `GET /v2/payments/captures/{id}`. Database: read in a rolled-back transaction:
- after booking, the journal entries and postings citing the invoice or capture, the
  settlement record and event, the commitments, the state history, the independent
  reconciliation checker and the per-currency trial balance.

Writes `evidence/phase5/settlement_<stage>_<UTC>.json`: ids, amounts, currencies,
statuses, debug ids and fingerprints only (no token, email, name, host name or URL).
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, select, text  # noqa: E402

from api.accounting.reconciliation_check import CaptureExpectation, check_capture  # noqa: E402
from api.config import get_settings  # noqa: E402
from api.execution import bindings  # noqa: E402
from api.execution.invoice_session import terms_problems  # noqa: E402
from api.execution.service import _expected  # noqa: E402
from api.execution.tables import (  # noqa: E402
    obligation_settlement_event,
    payment_exception,
    payment_session,
    payment_state_event,
    provider_settlement,
)
from api.integrations.paypal.client import PayPalSandboxClient  # noqa: E402
from api.integrations.paypal.invoice_terms import invoice_terms  # noqa: E402
from api.integrations.paypal.redaction import fingerprint  # noqa: E402
from api.liquidity.tables import capacity_commitment  # noqa: E402
from api.workflow.store import load_record  # noqa: E402
from api.workflow.tables import outbox_message, payment_intent  # noqa: E402


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def _money(obj):
    return None if not isinstance(obj, dict) else f"{obj.get('currency_code')} {obj.get('value')}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--intent", required=True)
    ap.add_argument("--stage", choices=("before-booking", "after-booking"), required=True)
    args = ap.parse_args(argv)
    s = get_settings()
    engine = create_engine(s.database_url, connect_args={"connect_timeout": 5})
    out: dict = {
        "stage": args.stage,
        "environment": "sandbox",
        "run_id": datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
    }
    with engine.connect() as c:
        if c.execute(text("SELECT current_database()")).scalar_one() != "elide_sandbox_pilot":
            print("REFUSED: not the pilot database")
            return 2
        intent = dict(
            c.execute(select(payment_intent).where(payment_intent.c.intent_id == args.intent))
            .mappings()
            .one()
        )
        sess = dict(
            c.execute(select(payment_session).where(payment_session.c.intent_id == args.intent))
            .mappings()
            .one()
        )
        inv_binding = bindings.by_id(c, sess["invoicer_account_id"])
        c.rollback()

    email = s.paypal_sandbox_payer_email.get_secret_value()
    client = PayPalSandboxClient(s)
    try:
        g = client.get_invoice(sess["provider_invoice_id"])
        inv = g.body if isinstance(g.body, dict) else {}
        t = invoice_terms(inv)
        cap_id = t.transaction_ids[0] if t.transaction_ids else None
        cg = client.get_capture(cap_id) if cap_id else None
    finally:
        client.close()
    cap = cg.body if cg is not None and isinstance(cg.body, dict) else {}
    srb = cap.get("seller_receivable_breakdown") or {}
    payee_fp = fingerprint((cap.get("payee") or {}).get("merchant_id")) if cap else None
    txn = ((inv.get("payments") or {}).get("transactions") or [{}])[0]
    out["provider"] = {
        "invoice": {
            "http_status": g.status_code,
            "debug_id": g.debug_id,
            "id": inv.get("id"),
            "number": (inv.get("detail") or {}).get("invoice_number"),
            "status": t.status,
            "currency": t.currency,
            "item_total_cents": t.item_total_cents,
            "discount_cents": t.invoice_discount_cents,
            "total_cents": t.total_cents,
            "paid_cents": t.paid_cents,
            "due_cents": t.due_cents,
            "transactions": t.payment_transaction_count,
            "transaction_statuses": t.transaction_statuses,
            "transaction_ids": t.transaction_ids,
            "transaction_type": txn.get("type"),
            "transaction_payment_date_time": txn.get("payment_date_time"),
            "strict_paid_problems": terms_problems(
                inv,
                _expected(sess, intent),
                invoice_id=sess["provider_invoice_id"],
                payer_email=email,
                expected_status="PAID",
            ),
        },
        "capture": None
        if cg is None
        else {
            "http_status": cg.status_code,
            "debug_id": cg.debug_id,
            "id": cap.get("id"),
            "status": cap.get("status"),
            "final_capture": cap.get("final_capture"),
            "invoice_id": cap.get("invoice_id"),
            "amount": _money(cap.get("amount")),
            "gross": _money(srb.get("gross_amount")),
            "paypal_fee": _money(srb.get("paypal_fee")),
            "net": _money(srb.get("net_amount")),
            "receivable": _money(srb.get("receivable_amount")),
            "exchange_rate": (srb.get("exchange_rate") or {}).get("value"),
            "exchange_direction": None
            if not srb.get("exchange_rate")
            else (
                f"{srb['exchange_rate'].get('source_currency')}->"
                f"{srb['exchange_rate'].get('target_currency')}"
            ),
            "create_time": cap.get("create_time"),
            "payee_fingerprint": payee_fp,
            "payee_matches_binding": payee_fp == inv_binding.account_fingerprint,
            "seller_protection": (cap.get("seller_protection") or {}).get("status"),
        },
        "note": "payer identity: PayPal's invoice and capture do not identify the paying "
        "account; the invoice recipient is the approved Sandbox payer "
        f"({fingerprint(email.strip().lower())})",
    }

    with engine.connect() as c:
        intent = dict(
            c.execute(select(payment_intent).where(payment_intent.c.intent_id == args.intent))
            .mappings()
            .one()
        )
        out["intent"] = {k: _iso(intent[k]) for k in ("intent_id", "status", "status_reason")}
        out["state_transitions"] = [
            f"{r.from_status} -> {r.to_status} ({r.reason}) at {r.occurred_at.isoformat()}"
            for r in c.execute(
                select(payment_state_event)
                .where(payment_state_event.c.intent_id == args.intent)
                .order_by(payment_state_event.c.id)
            )
        ]
        out["exceptions"] = (
            c.execute(
                select(payment_exception.c.code).where(payment_exception.c.intent_id == args.intent)
            )
            .scalars()
            .all()
        )
        out["commitments"] = [
            {k: _iso(r[k]) for k in r}
            for r in c.execute(
                select(
                    capacity_commitment.c.commitment_id,
                    capacity_commitment.c.kind,
                    capacity_commitment.c.amount_cents,
                    capacity_commitment.c.released_at,
                    capacity_commitment.c.release_reason,
                )
            ).mappings()
        ]
        out["outbox"] = [
            {k: _iso(r[k]) for k in r}
            for r in c.execute(
                select(
                    outbox_message.c.id,
                    outbox_message.c.kind,
                    outbox_message.c.status,
                    outbox_message.c.attempts,
                    outbox_message.c.last_error,
                )
            ).mappings()
        ]
        refs = [x for x in (sess["provider_invoice_id"], cap.get("id")) if x]
        entries = (
            c.execute(
                text(
                    "SELECT id, entity_id, kind, evidence_class, idempotency_key, "
                    "provider_resource_type, provider_resource_id FROM journal_entry "
                    "WHERE provider_resource_id = ANY(:r) "
                    "OR idempotency_key LIKE 'elide:obligation:pilot_001:%' ORDER BY id"
                ),
                {"r": refs},
            )
            .mappings()
            .all()
        )
        out["journal"] = []
        for e in entries:
            lines = c.execute(
                text(
                    "SELECT account, currency, amount_cents FROM journal_posting "
                    "WHERE entry_id = :e ORDER BY line_no"
                ),
                {"e": e["id"]},
            ).all()
            out["journal"].append(
                {
                    **dict(e),
                    "postings": [
                        {"account": a, "currency": cur, "cents": int(n)} for a, cur, n in lines
                    ],
                }
            )
        out["trial_balance"] = {
            cur: int(n)
            for cur, n in c.execute(
                text("SELECT currency, SUM(amount_cents) FROM journal_posting GROUP BY currency")
            ).all()
        }
        st = c.execute(select(provider_settlement)).mappings().all()
        out["settlement_records"] = [{k: _iso(r[k]) for k in r} for r in st]
        out["settlement_events"] = [
            {k: _iso(r[k]) for k in r}
            for r in c.execute(
                select(
                    obligation_settlement_event.c.event_id,
                    obligation_settlement_event.c.kind,
                    obligation_settlement_event.c.amount_cents,
                    obligation_settlement_event.c.currency,
                    obligation_settlement_event.c.occurred_at,
                )
            ).mappings()
        ]
        pos = load_record(c, "pilot_001").record.position()
        out["obligation_position"] = {
            "original_open": pos.original_open.cents,
            "cash_applied": pos.cash_applied.cents,
            "authorized_concession": pos.authorized_concession.cents,
            "remaining_open": pos.remaining_open.cents,
            "status": pos.status.value,
        }
        if st:
            out["independent_checker"] = check_capture(
                c,
                CaptureExpectation(
                    st[0]["capture_id"],
                    st[0]["invoice_id"],
                    intent["payee_merchant"],
                    intent["payer_merchant"],
                    intent["obligation_id"],
                    2500,
                    2400,
                    100,
                ),
            )
        c.rollback()

    body = json.dumps(out, indent=2, sort_keys=True, default=str)
    body = body.replace(socket.gethostname() + ":", "redacted-host:")
    secrets = [
        s.paypal_client_id.get_secret_value(),
        s.paypal_client_secret.get_secret_value(),
        email,
    ]
    if any(v in body for v in secrets) or "@" in body or "https://" in body:
        print("REFUSED: evidence would contain a secret, email or URL; nothing written")
        return 3
    path = ROOT / "evidence" / "phase5" / f"settlement_{args.stage}_{out['run_id']}.json"
    path.write_text(body + "\n", encoding="utf-8")
    print(body)
    print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
