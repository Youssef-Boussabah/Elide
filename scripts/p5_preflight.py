"""P5 Sandbox pilot preflight (owner instruction 2026-11-12). READ-ONLY. NO provider mutation.

    DATABASE_URL=<pilot database url> uv run python scripts/p5_preflight.py \\
        --expect-commit 0349162091196bd712620ca02b50f181903b4911 \\
        --expect-payee redacted:6dbc83888cc3 --expect-credential redacted:2b2f310e3f08 \\
        --expect-payer redacted:94281907e2c4

Checks every precondition of `evidence/phase5/sandbox_operation_gate_2026-11-12.md` and
writes `evidence/phase5/sandbox_preflight_<UTC>.json` (codes, counts, amounts and
fingerprints only; no token, id secret, email, name or URL). Exit 0 only if all pass.

PayPal calls made: `POST /v1/oauth2/token` and `GET /v2/invoicing/invoices` (list). Nothing
is created, sent, cancelled, refunded, captured, updated or paid. The database is only read.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, func, select, text  # noqa: E402

from api.config import PAYPAL_SANDBOX_API_BASE, get_settings  # noqa: E402
from api.execution.tables import (  # noqa: E402
    payment_exception,
    payment_session,
    provider_account,
    provider_operation,
    provider_request,
    provider_settlement,
)
from api.integrations.paypal.client import PayPalSandboxClient  # noqa: E402
from api.integrations.paypal.redaction import fingerprint  # noqa: E402
from api.workflow.store import load_record  # noqa: E402
from api.workflow.tables import (  # noqa: E402
    offer,
    outbox_message,
    payment_authorization,
    payment_intent,
)

HEAD = "0007_p5_request_identity"
OBLIGATION = "pilot_001"
EXPECT_FACE, EXPECT_DISCOUNT, EXPECT_TOTAL = 2500, 100, 2400


def _git(*args: str) -> str:
    git = shutil.which("git")
    if git is None:
        raise OSError("git not found")
    return subprocess.run(  # noqa: S603  (fixed arguments, no shell)
        [git, *args], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--expect-commit", required=True)
    ap.add_argument("--expect-payee", required=True)
    ap.add_argument("--expect-credential", required=True)
    ap.add_argument("--expect-payer", required=True)
    ap.add_argument("--no-provider", action="store_true", help="skip the read-only PayPal list")
    args = ap.parse_args(argv)
    s = get_settings()
    checks: list[dict] = []

    def check(name: str, ok: bool, observed) -> None:
        checks.append({"check": name, "ok": bool(ok), "observed": observed})
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {observed}")

    # 1. code identity -----------------------------------------------------------------------
    head = _git("rev-parse", "HEAD")
    dirty = _git("status", "--porcelain")
    code_diff = _git(
        "diff",
        "--name-only",
        args.expect_commit,
        "HEAD",
        "--",
        "api",
        "migrations",
        "web/src",
        "scripts/run_worker.py",
        "fixtures",
    )
    check(
        "1 code identical to the approved commit",
        head == args.expect_commit or not code_diff,
        {"head": head, "approved": args.expect_commit, "code_files_changed": code_diff.split()},
    )
    check("1 clean working tree", not dirty, {"dirty_entries": len(dirty.splitlines())})

    # 4, 5, 10, 11: configuration --------------------------------------------------------------
    check("4 AUTO_CANCEL_UNPAID is false", s.auto_cancel_unpaid is False, s.auto_cancel_unpaid)
    check(
        "5 PAYMENTS_EXECUTION_ENABLED is false (until authorized)",
        s.payments_execution_enabled is False,
        s.payments_execution_enabled,
    )
    check(
        "5 PAYMENT_PROVIDER is not yet paypal_sandbox",
        s.payment_provider != "paypal_sandbox",
        s.payment_provider,
    )
    check(
        "11 Sandbox-only API base",
        PAYPAL_SANDBOX_API_BASE == "https://api-m.sandbox.paypal.com",
        "api-m.sandbox.paypal.com",
    )
    check(
        "11 no webhook-driven path configured",
        s.paypal_webhook_id is None,
        {"webhook_id_configured": s.paypal_webhook_id is not None},
    )

    # 3. identities -----------------------------------------------------------------------------
    cred = fingerprint(s.paypal_client_id.get_secret_value()) if s.paypal_client_id else None
    payer = (
        fingerprint(s.paypal_sandbox_payer_email.get_secret_value().strip().lower())
        if s.paypal_sandbox_payer_email
        else None
    )
    check("3 payee credential fingerprint", cred == args.expect_credential, cred)
    check("3 payer fingerprint (= P0 invoice recipient)", payer == args.expect_payer, payer)

    # 11. static: no capture/refund/update/record-payment path from execution -------------------
    client_src = (ROOT / "api/integrations/paypal/client.py").read_text(encoding="utf-8")
    exec_src = "\n".join(
        p.read_text(encoding="utf-8") for p in (ROOT / "api/execution").rglob("*.py")
    )
    forbidden_client = [
        m
        for m in ("def capture", "def refund", "def record_payment", "def authorize_payment")
        if m in client_src
    ]
    # Every reference to the provider (called directly or passed to the recorder), not only
    # direct calls: a first version matched `rt.provider.x(` only and missed GET/search.
    exec_calls = sorted(set(re.findall(r"rt\.provider\.(\w+)", exec_src)))
    allowed = {
        "create_invoice",
        "send_invoice",
        "cancel_invoice",
        "get_invoice",
        "search_invoices",
        "get_capture",
        "get_refund",
        # local attributes, no network: the credential fingerprint and environment name
        "credential_fingerprint",
        "environment",
    }
    check(
        "11 the client has no capture/refund/record-payment operation",
        not forbidden_client,
        forbidden_client,
    )
    check(
        "11 execution calls only create/send/cancel/get/search",
        set(exec_calls) <= allowed,
        exec_calls,
    )
    check(
        "11 execution never updates or deletes an invoice",
        "update_invoice" not in exec_src
        and "delete" not in re.findall(r"rt\.provider\.(\w+)", exec_src),
        "no update_invoice / delete",
    )

    # 2, 6-9: database ----------------------------------------------------------------------------
    engine = create_engine(s.database_url, connect_args={"connect_timeout": 5})
    with engine.connect() as c:
        version = c.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        check("2 migrations through 0007 applied", version == HEAD, version)
        dbname = c.execute(text("SELECT current_database()")).scalar_one()
        check("6 isolated pilot database", dbname == "elide_sandbox_pilot", dbname)
        merchants = c.execute(
            text("SELECT merchant_id, is_synthetic FROM merchant_org ORDER BY merchant_id")
        ).all()
        check(
            "6 only the two pilot merchants exist",
            [m for m, _ in merchants] == ["pilot_buyer", "pilot_seller"]
            and not any(syn for _, syn in merchants),
            [f"{m} (synthetic={syn})" for m, syn in merchants],
        )

        def count(table, *where):
            return c.execute(select(func.count()).select_from(table).where(*where)).scalar_one()

        intents = count(payment_intent)
        auths = count(payment_authorization)
        outbox = c.execute(
            select(outbox_message.c.kind, outbox_message.c.status, func.count()).group_by(
                outbox_message.c.kind, outbox_message.c.status
            )
        ).all()
        check("6 no payment intent exists yet", intents == 0, intents)
        check(
            "7 no outbox message (pending, leased, held, cancel or other)",
            not outbox,
            [f"{k}:{st}={n}" for k, st, n in outbox],
        )
        check(
            "8 no provider session, request, operation, settlement or exception",
            all(
                count(t) == 0
                for t in (
                    payment_session,
                    provider_request,
                    provider_operation,
                    provider_settlement,
                    payment_exception,
                )
            ),
            {
                t.name: count(t)
                for t in (
                    payment_session,
                    provider_request,
                    provider_operation,
                    provider_settlement,
                    payment_exception,
                )
            },
        )
        bound = c.execute(
            select(
                provider_account.c.merchant_id,
                provider_account.c.role,
                provider_account.c.revoked_at,
            )
        ).all()
        check(
            "3 no binding yet (the owner binds at authorization)",
            not bound,
            [f"{m}:{r}" for m, r, _ in bound],
        )
        check(
            "9 no payment authorization yet (the owner approves at authorization)",
            auths == 0,
            auths,
        )

        rec = load_record(c, OBLIGATION).record
        ob, rev = rec.obligation, rec.in_force_revision()
        pos = rec.position()
        o = (
            c.execute(
                select(offer).where(
                    offer.c.obligation_id == OBLIGATION, offer.c.status == "proposed"
                )
            )
            .mappings()
            .one_or_none()
        )
        terms = {
            "obligation": OBLIGATION,
            "debtor": ob.debtor,
            "creditor": ob.creditor,
            "face_cents": ob.face.cents,
            "currency": ob.currency,
            "in_force_revision": rev.revision_no,
            "open_cents": pos.remaining_open.cents,
            "original_due_at": rev.due_at.isoformat(),
            "open_offer": None
            if o is None
            else {
                "proposer": o["proposer_merchant"],
                "concession_cents": int(o["concession_cents"]),
                "cash_cents": int(o["cash_cents"]),
                "pay_by": o["due_at"].isoformat(),
                "offer_expires_at": o["expires_at"].isoformat(),
            },
        }
        ok_terms = (
            ob.face.cents == EXPECT_FACE
            and ob.currency == "USD"
            and (ob.debtor, ob.creditor) == ("pilot_buyer", "pilot_seller")
            and o is not None
            and int(o["concession_cents"]) == EXPECT_DISCOUNT
            and int(o["cash_cents"]) == EXPECT_TOTAL
            and o["proposer_merchant"] == "pilot_seller"
            and o["expires_at"] > datetime.now(UTC)
        )
        check(
            "9 terms: USD 25.00 face, USD 1.00 offered off, USD 24.00 payable, offer open",
            ok_terms,
            terms,
        )
    engine.dispose()

    # 8. provider (read-only) ---------------------------------------------------------------
    if not args.no_provider:
        client = PayPalSandboxClient(s)
        try:
            tok = client.authenticate()
            lst = client.list_invoices(page_size=100)
        finally:
            client.close()
        items = (lst.body or {}).get("items") or [] if lst.ok else []
        numbers = [((i.get("detail") or {}).get("invoice_number")) for i in items]
        summary = {
            "oauth_http": tok.status_code,
            "list_http": lst.status_code,
            "list_debug_id": lst.debug_id,
            "total_items": (lst.body or {}).get("total_items") if lst.ok else None,
            "invoices": [
                {
                    "id": i.get("id"),
                    "status": i.get("status"),
                    "number": (i.get("detail") or {}).get("invoice_number"),
                }
                for i in items
            ],
        }
        check(
            "8 no existing PayPal invoice numbered ELIDE-* (no conflict with the pilot)",
            lst.ok and not any(str(n).startswith("ELIDE-") for n in numbers),
            summary,
        )
        check(
            "8 no unpaid PayPal invoice that could be paid by mistake",
            lst.ok
            and not any(
                i.get("status")
                in ("SENT", "UNPAID", "SCHEDULED", "PAYMENT_PENDING", "PARTIALLY_PAID")
                for i in items
            ),
            [i.get("status") for i in items],
        )

    ok = all(x["ok"] for x in checks)
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report = {
        "run_id": run_id,
        "environment": "sandbox",
        "mutations": "none",
        "provider_calls": []
        if args.no_provider
        else ["POST /v1/oauth2/token", "GET /v2/invoicing/invoices"],
        "commit": head + ("+dirty" if dirty else ""),
        "all_passed": ok,
        "checks": checks,
    }
    body = json.dumps(report, indent=2, sort_keys=True, default=str)
    secrets = [
        v.get_secret_value()
        for v in (s.paypal_client_id, s.paypal_client_secret, s.paypal_sandbox_payer_email)
        if v
    ]
    if any(v in body for v in secrets) or "@" in body or "https://" in body or "http://" in body:
        print("REFUSED: the report would contain a secret, email or URL; nothing written")
        return 3
    out = ROOT / "evidence" / "phase5" / f"sandbox_preflight_{run_id}.json"
    out.write_text(body + "\n", encoding="utf-8")
    print(
        f"{'ALL PRECONDITIONS PASS' if ok else 'PRECONDITION FAILED: STOP'}; wrote "
        f"{out.relative_to(ROOT)}"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
