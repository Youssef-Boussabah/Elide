"""Redacted evidence of the P5 pilot dispatch (owner-authorized single invoice). READ-ONLY.

    DATABASE_URL=<pilot database url> uv run python scripts/p5_dispatch_evidence.py \\
        --intent pin_7452d6099eaf9caf

Collects from the pilot database every provider operation (request id, attempt, state,
HTTP status, PayPal debug id, times), the persisted request identities, the payment
session, the state transitions, the outbox and the reservation; then makes ONE read-only
`GET /v2/invoicing/invoices/{id}` and compares PayPal's recorded terms with the expected
ones using the strict comparator. Writes `evidence/phase5/dispatch_<UTC>.json` with ids,
amounts, statuses and fingerprints only (no token, email, name or URL).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, select, text  # noqa: E402

from api.config import get_settings  # noqa: E402
from api.execution.invoice_session import UNPAID, terms_problems  # noqa: E402
from api.execution.service import _expected  # noqa: E402
from api.execution.tables import (  # noqa: E402
    payment_session,
    payment_state_event,
    provider_operation,
    provider_request,
)
from api.integrations.paypal.client import PayPalSandboxClient  # noqa: E402
from api.integrations.paypal.invoice_terms import invoice_terms  # noqa: E402
from api.integrations.paypal.redaction import fingerprint  # noqa: E402
from api.liquidity.tables import capacity_commitment  # noqa: E402
from api.workflow.tables import outbox_message, payment_intent  # noqa: E402


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--intent", required=True)
    args = ap.parse_args(argv)
    s = get_settings()
    engine = create_engine(s.database_url, connect_args={"connect_timeout": 5})
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
        ops = [
            dict(r)
            for r in c.execute(
                select(provider_operation)
                .where(provider_operation.c.intent_id == args.intent)
                .order_by(provider_operation.c.id)
            ).mappings()
        ]
        reqs = [
            dict(r)
            for r in c.execute(
                select(provider_request)
                .where(provider_request.c.intent_id == args.intent)
                .order_by(provider_request.c.id)
            ).mappings()
        ]
        events = [
            dict(r)
            for r in c.execute(
                select(payment_state_event)
                .where(payment_state_event.c.intent_id == args.intent)
                .order_by(payment_state_event.c.id)
            ).mappings()
        ]
        outbox = [
            dict(r)
            for r in c.execute(
                select(
                    outbox_message.c.id,
                    outbox_message.c.kind,
                    outbox_message.c.status,
                    outbox_message.c.attempts,
                    outbox_message.c.available_at,
                    outbox_message.c.finished_at,
                    outbox_message.c.last_error,
                )
            ).mappings()
        ]
        commits = [
            dict(r)
            for r in c.execute(
                select(
                    capacity_commitment.c.commitment_id,
                    capacity_commitment.c.kind,
                    capacity_commitment.c.amount_cents,
                    capacity_commitment.c.released_at,
                )
            ).mappings()
        ]
        c.rollback()

    email = s.paypal_sandbox_payer_email.get_secret_value()
    client = PayPalSandboxClient(s)
    try:
        g = client.get_invoice(sess["provider_invoice_id"])
    finally:
        client.close()
    body = g.body if isinstance(g.body, dict) else {}
    terms = invoice_terms(body)
    problems = terms_problems(
        body,
        _expected(sess, intent),
        invoice_id=sess["provider_invoice_id"],
        payer_email=email,
        expected_status=None,
        allowed_statuses=UNPAID,
    )
    meta = (body.get("detail") or {}).get("metadata") or {}
    cfg = body.get("configuration") or {}
    record = {
        "run_id": datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
        "environment": "sandbox",
        "evidence_class": "Sandbox-verified (provider GET) for the invoice; payment not yet made",
        "intent": {
            k: _iso(intent[k])
            for k in (
                "intent_id",
                "authorization_id",
                "obligation_id",
                "revision_no",
                "amount_cents",
                "status",
                "status_reason",
                "status_updated_at",
                "commitment_id",
            )
        },
        "session": {
            k: _iso(sess[k])
            for k in (
                "session_id",
                "invoice_number",
                "provider_invoice_id",
                "expected_item_cents",
                "expected_discount_cents",
                "expected_total_cents",
                "currency",
                "due_date",
                "pay_deadline",
                "last_provider_status",
                "draft_created_at",
                "sent_at",
            )
        },
        "provider_requests": [
            {k: _iso(r[k]) for k in ("operation", "request_id", "request_sha256", "created_at")}
            for r in reqs
        ],
        "provider_operations": [
            {
                k: _iso(o[k])
                for k in (
                    "id",
                    "operation",
                    "mutating",
                    "request_id",
                    "attempt_no",
                    "state",
                    "http_status",
                    "debug_id",
                    "outcome_code",
                    "worker",
                    "started_at",
                    "finished_at",
                )
            }
            for o in ops
        ],
        "state_transitions": [
            f"{e['from_status']} -> {e['to_status']} ({e['reason']}) by "
            f"{e['actor']} at {e['occurred_at'].isoformat()}"
            for e in events
        ],
        "outbox": [{k: _iso(m[k]) for k in m} for m in outbox],
        "commitments": [{k: _iso(x[k]) for k in x} for x in commits],
        "independent_get": {
            "http_status": g.status_code,
            "debug_id": g.debug_id,
            "status": terms.status,
            "invoice_number": (body.get("detail") or {}).get("invoice_number"),
            "currency": terms.currency,
            "item_total_cents": terms.item_total_cents,
            "invoice_discount_cents": terms.invoice_discount_cents,
            "invoice_discount_sign": terms.invoice_discount_sign,
            "total_cents": terms.total_cents,
            "due_cents": terms.due_cents,
            "due_date": terms.due_date,
            "payment_term": terms.payment_term_type,
            "recipient_count": terms.recipient_count,
            "recipient_fingerprint": fingerprint(email.strip().lower()),
            "partial_payment_allowed": (cfg.get("partial_payment") or {}).get(
                "allow_partial_payment"
            ),
            "tips_allowed": cfg.get("allow_tip"),
            "transactions": terms.payment_transaction_count,
            "created": meta.get("create_time"),
            "first_sent": meta.get("first_sent_time"),
            "recipient_view_url_present": bool(meta.get("recipient_view_url")),
            "terms_problems": problems,
        },
    }
    out_text = json.dumps(record, indent=2, sort_keys=True, default=str)
    secrets = [
        s.paypal_client_id.get_secret_value(),
        s.paypal_client_secret.get_secret_value(),
        email,
    ]
    if any(v in out_text for v in secrets) or "@" in out_text or "https://" in out_text:
        print("REFUSED: evidence would contain a secret, email or URL; nothing written")
        return 3
    out = ROOT / "evidence" / "phase5" / f"dispatch_{record['run_id']}.json"
    out.write_text(out_text + "\n", encoding="utf-8")
    print(out_text)
    print(f"wrote {out.relative_to(ROOT)}")
    return 0 if g.ok and not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
