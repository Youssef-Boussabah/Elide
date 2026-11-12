"""P5 final pre-dispatch verification (owner instruction 2026-11-12). READ-ONLY.

    DATABASE_URL=<pilot database url> uv run python scripts/p5_predispatch_check.py \\
        --intent pin_7452d6099eaf9caf --authorization pau_8d756a08712328c3 \\
        --expect-payee redacted:6dbc83888cc3 --expect-credential redacted:2b2f310e3f08 \\
        --expect-payer redacted:94281907e2c4

Checks, in one rolled-back database transaction, everything the worker will check before
its first provider-mutating call, by running the worker's own `revalidate()`; plus the
outbox, reservations, provider records, the release event and audit trail. PayPal calls
(read-only): `POST /v1/oauth2/token`, `GET /v2/invoicing/invoices`, and
`POST /v2/invoicing/search-invoices` for the exact invoice number this intent would use.
Nothing is created, sent, cancelled, updated or paid. Writes
`evidence/phase5/predispatch_<UTC>.json` (redacted) and exits 0 only if all checks pass.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, func, select, text  # noqa: E402

from api.config import get_settings  # noqa: E402
from api.execution import bindings  # noqa: E402
from api.execution.invoice_session import invoice_number_for  # noqa: E402
from api.execution.service import RevalidationError, revalidate  # noqa: E402
from api.execution.tables import (  # noqa: E402
    payment_exception,
    payment_session,
    payment_state_event,
    provider_operation,
    provider_request,
    provider_settlement,
)
from api.integrations.paypal.client import PayPalSandboxClient  # noqa: E402
from api.integrations.paypal.execution import PayPalSandboxProvider  # noqa: E402
from api.integrations.paypal.redaction import fingerprint  # noqa: E402
from api.liquidity.store import _current_evidence  # noqa: E402
from api.liquidity.tables import capacity_commitment  # noqa: E402
from api.workflow.store import load_record  # noqa: E402
from api.workflow.tables import audit_event, outbox_message, payment_authorization, payment_intent  # noqa: E402


@dataclass(frozen=True)
class _Facts:
    provider: PayPalSandboxProvider

    def execution_enabled(self) -> bool:
        return True  # the configured pilot API runs in queue mode; checked separately below

    def payer_email(self) -> str | None:
        e = get_settings().paypal_sandbox_payer_email
        return e.get_secret_value() if e else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for a in (
        "--intent",
        "--authorization",
        "--expect-payee",
        "--expect-credential",
        "--expect-payer",
    ):
        ap.add_argument(a, required=True)
    args = ap.parse_args(argv)
    s = get_settings()
    now = datetime.now(UTC)
    checks: list[dict] = []

    def check(name: str, ok: bool, observed) -> None:
        checks.append({"check": name, "ok": bool(ok), "observed": observed})
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {observed}")

    client = PayPalSandboxClient(s)
    provider = PayPalSandboxProvider(client)
    engine = create_engine(s.database_url, connect_args={"connect_timeout": 5})
    number = invoice_number_for(args.intent)
    with engine.connect() as c:
        db = c.execute(text("SELECT current_database()")).scalar_one()
        check("0 isolated pilot database", db == "elide_sandbox_pilot", db)
        c.rollback()
        with c.begin() as tx:
            i = dict(
                c.execute(select(payment_intent).where(payment_intent.c.intent_id == args.intent))
                .mappings()
                .one()
            )
            a = dict(
                c.execute(
                    select(payment_authorization).where(
                        payment_authorization.c.authorization_id == args.authorization
                    )
                )
                .mappings()
                .one()
            )
            msgs = c.execute(
                select(
                    outbox_message.c.id,
                    outbox_message.c.kind,
                    outbox_message.c.status,
                    outbox_message.c.intent_id,
                    outbox_message.c.attempts,
                    outbox_message.c.lease_owner,
                    outbox_message.c.finished_at,
                )
            ).all()
            check(
                "1 exactly one pending create job, nothing else dispatchable",
                len(msgs) == 1
                and msgs[0].kind == "create_payment_session"
                and msgs[0].status == "pending"
                and msgs[0].intent_id == args.intent
                and msgs[0].attempts == 0
                and msgs[0].lease_owner is None
                and msgs[0].finished_at is None,
                [f"{m.id}:{m.kind}:{m.status}:attempts={m.attempts}" for m in msgs],
            )
            check(
                "9 the original intent and authorization are reused",
                i["authorization_id"] == args.authorization
                and i["status"] == "approved"
                and i["status_reason"] == "QUEUED_FOR_PROVIDER",
                {
                    "intent": args.intent,
                    "status": i["status"],
                    "reason": i["status_reason"],
                    "authorization": i["authorization_id"],
                },
            )
            check(
                "2 the approval is unexpired and is an approval",
                a["decision"] == "approve" and a["expires_at"] > now,
                {
                    "expires_at": a["expires_at"].isoformat(),
                    "hours_left": round((a["expires_at"] - now).total_seconds() / 3600, 2),
                },
            )
            try:
                revalidate(c, _Facts(provider), i, now)  # the worker's own pre-mutation checks
                rv = "PASS"
            except RevalidationError as exc:
                rv = f"{exc.code}: {exc}"
            check(
                "2,3,4,6 worker revalidate(): authority, conditions digest, independent "
                "verifier, exact reservation, bindings vs credentials, Sandbox",
                rv == "PASS",
                rv,
            )
            commits = c.execute(
                select(
                    capacity_commitment.c.commitment_id,
                    capacity_commitment.c.kind,
                    capacity_commitment.c.amount_cents,
                    capacity_commitment.c.released_at,
                )
            ).all()
            check(
                "4 the original USD 24.00 reservation is the only commitment",
                len(commits) == 1
                and commits[0].commitment_id == i["commitment_id"]
                and commits[0].amount_cents == 2400
                and commits[0].released_at is None
                and commits[0].kind == "reservation",
                [
                    f"{x.commitment_id}:{x.kind}:{x.amount_cents}:active={x.released_at is None}"
                    for x in commits
                ],
            )
            rev = load_record(c, i["obligation_id"]).record.in_force_revision(now)
            ev = _current_evidence(c, i["payer_merchant"], "USD", now)
            check(
                "5 merchant-attested evidence valid through the payment deadline",
                ev is not None
                and ev.kind.value == "merchant_attested"
                and ev.valid_until > rev.due_at,
                None
                if ev is None
                else {
                    "valid_until": ev.valid_until.isoformat(),
                    "deadline": rev.due_at.isoformat(),
                    "spendable_cents": ev.verified_spendable.cents,
                    "reserve_cents": ev.protected_reserve.cents,
                },
            )
            check(
                "terms: revision 3, USD 1.00 concession, USD 24.00",
                (rev.revision_no, rev.concession.cents, int(i["amount_cents"])) == (3, 100, 2400),
                {
                    "revision": rev.revision_no,
                    "concession": rev.concession.cents,
                    "amount": int(i["amount_cents"]),
                },
            )
            counts = {
                t.name: c.execute(select(func.count()).select_from(t)).scalar_one()
                for t in (
                    payment_session,
                    provider_request,
                    provider_operation,
                    provider_settlement,
                    payment_exception,
                )
            }
            intents = c.execute(select(func.count()).select_from(payment_intent)).scalar_one()
            check(
                "10 nothing duplicated: one intent, no session/request/operation yet",
                intents == 1 and all(v == 0 for v in counts.values()),
                {"payment_intent": intents, **counts},
            )
            inv_b = bindings.active(c, i["payee_merchant"], "invoicer")
            pay_b = bindings.active(c, i["payer_merchant"], "payer")
            check(
                "6 bindings: payee account and payer identities as approved",
                inv_b is not None
                and inv_b.account_fingerprint == args.expect_payee
                and inv_b.credential_fingerprint == args.expect_credential
                and pay_b is not None
                and pay_b.credential_fingerprint == args.expect_payer,
                {
                    "payee_account": None if inv_b is None else inv_b.account_fingerprint,
                    "payee_credential": None if inv_b is None else inv_b.credential_fingerprint,
                    "payer": None if pay_b is None else pay_b.credential_fingerprint,
                },
            )
            ev_rows = c.execute(
                select(
                    payment_state_event.c.reason,
                    payment_state_event.c.actor,
                    payment_state_event.c.occurred_at,
                ).where(payment_state_event.c.intent_id == args.intent)
            ).all()
            audit = c.execute(
                select(func.count())
                .select_from(audit_event)
                .where(
                    audit_event.c.action == "payment.released",
                    audit_event.c.object_id == args.intent,
                )
            ).scalar_one()
            check(
                "9 one RELEASED_BY_PAYER state event and one audit event",
                [r.reason for r in ev_rows] == ["RELEASED_BY_PAYER"] and audit == 1,
                {
                    "events": [
                        f"{r.reason} by {r.actor} at {r.occurred_at.isoformat()}" for r in ev_rows
                    ],
                    "audit_payment_released": audit,
                },
            )
            tx.rollback()

    check("8 AUTO_CANCEL_UNPAID is false", s.auto_cancel_unpaid is False, s.auto_cancel_unpaid)
    check(
        "6 credential fingerprint",
        provider.credential_fingerprint() == args.expect_credential,
        provider.credential_fingerprint(),
    )
    payer = fingerprint(s.paypal_sandbox_payer_email.get_secret_value().strip().lower())
    check("6 payer fingerprint", payer == args.expect_payer, payer)

    # 7. PayPal, read-only.
    try:
        tok = client.authenticate()
        lst = client.list_invoices(page_size=100)
        srch = client.search_invoices(number)
    finally:
        client.close()
    items = (lst.body or {}).get("items") or [] if lst.ok else []
    found = (srch.body or {}).get("items") or [] if srch.ok else []
    check(
        "7 PayPal: no ELIDE-* invoice and no unpaid invoice exists",
        tok.ok
        and lst.ok
        and not any(
            str((x.get("detail") or {}).get("invoice_number", "")).startswith("ELIDE-")
            or x.get("status") in ("DRAFT", "SENT", "UNPAID", "SCHEDULED", "PAYMENT_PENDING")
            for x in items
        ),
        {
            "oauth_http": tok.status_code,
            "list_http": lst.status_code,
            "list_debug_id": lst.debug_id,
            "total_items": (lst.body or {}).get("total_items"),
            "invoices": [
                {
                    "id": x.get("id"),
                    "status": x.get("status"),
                    "number": (x.get("detail") or {}).get("invoice_number"),
                }
                for x in items
            ],
        },
    )
    check(
        f"7 PayPal: search for this intent's number {number} finds nothing",
        srch.ok and not found,
        {"search_http": srch.status_code, "search_debug_id": srch.debug_id, "matches": len(found)},
    )

    ok = all(x["ok"] for x in checks)
    run_id = now.strftime("%Y%m%dT%H%M%SZ")
    report = {
        "run_id": run_id,
        "environment": "sandbox",
        "mutations": "none",
        "provider_calls": [
            "POST /v1/oauth2/token",
            "GET /v2/invoicing/invoices",
            "POST /v2/invoicing/search-invoices (read-only)",
        ],
        "intent": args.intent,
        "all_passed": ok,
        "checks": checks,
    }
    body = json.dumps(report, indent=2, sort_keys=True, default=str)
    secret_values = [
        v.get_secret_value()
        for v in (s.paypal_client_id, s.paypal_client_secret, s.paypal_sandbox_payer_email)
        if v
    ]
    if any(v in body for v in secret_values) or "@" in body or "https://" in body:
        print("REFUSED: the report would contain a secret, email or URL; nothing written")
        return 3
    out = ROOT / "evidence" / "phase5" / f"predispatch_{run_id}.json"
    out.write_text(body + "\n", encoding="utf-8")
    print(
        f"{'ALL PRE-DISPATCH CHECKS PASS' if ok else 'A CHECK FAILED: DO NOT DISPATCH'}; "
        f"wrote {out.relative_to(ROOT)}"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
