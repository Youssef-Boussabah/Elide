"""Re-verify the P5 pilot settlement with the independent checker v2. Provider: READ-ONLY.

    DATABASE_URL=<pilot database url> PAYMENTS_EXECUTION_ENABLED=false \\
        uv run python scripts/p5_reverify_settlement.py --intent pin_7452d6099eaf9caf

For a capture booked before migration 0008 (the real pilot), there is no stored provider
body and `check_sha256` is a placeholder. This script:

1. refuses unless provider-mutating execution is disabled, the database is the pilot's and
   the schema is at 0008 (so the corrected provenance exists);
2. makes two read-only GETs (`/v2/invoicing/invoices/{id}`, `/v2/payments/captures/{id}`);
3. runs `verify_settlement` (checker v2) against the raw rows, reporting whether the fresh
   bodies are byte-identical (canonical hash) to the bodies Elide's own GETs recorded at
   booking time;
4. with `--record` only, appends ONE `settlement_verification` row (purpose
   `reverification`) with the canonical document and its SHA-256; nothing financial is
   written, rebooked or changed (without `--record` it prints the verdict only);
5. writes `evidence/phase5/reverification_<UTC>.json` (redacted: ids, amounts, statuses,
   digests and fingerprints only) and proves the financial row counts did not change.
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

from api.accounting.reconciliation_check import (  # noqa: E402
    CHECKER_VERSION,
    canonical,
    sha256_hex,
    verify_settlement,
)
from api.config import get_settings  # noqa: E402
from api.execution.attribution import payer_attribution, verification_view  # noqa: E402
from api.execution.tables import (  # noqa: E402
    payment_session,
    provider_settlement,
    settlement_verification,
)
from api.integrations.paypal.client import PayPalSandboxClient  # noqa: E402

FINANCIAL = (
    "journal_entry",
    "journal_posting",
    "obligation_settlement_event",
    "provider_settlement",
    "capacity_commitment",
    "payment_source_attestation",
    "payment_exception",
)


def _counts(engine) -> dict:
    with engine.connect() as c:
        out = {t: c.execute(text(f"SELECT count(*) FROM {t}")).scalar_one() for t in FINANCIAL}  # noqa: S608
        out["trial_balance"] = {
            cur: int(n)
            for cur, n in c.execute(
                text("SELECT currency, SUM(amount_cents) FROM journal_posting GROUP BY currency")
            ).all()
        }
        return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--intent", required=True)
    ap.add_argument(
        "--record",
        action="store_true",
        help="append the reverification row and write evidence (default: print only)",
    )
    args = ap.parse_args(argv)
    s = get_settings()
    if s.payments_execution_enabled:
        print("REFUSED: run with PAYMENTS_EXECUTION_ENABLED=false (read-only provider access)")
        return 2
    engine = create_engine(s.database_url, connect_args={"connect_timeout": 5})
    with engine.connect() as c:
        if c.execute(text("SELECT current_database()")).scalar_one() != "elide_sandbox_pilot":
            print("REFUSED: not the pilot database")
            return 2
        head = c.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        if head != "0008_p5_verification_attribution":
            print(f"REFUSED: schema is at {head}, not 0008")
            return 2
        sess = dict(
            c.execute(select(payment_session).where(payment_session.c.intent_id == args.intent))
            .mappings()
            .one()
        )
        st = dict(
            c.execute(
                select(provider_settlement).where(provider_settlement.c.intent_id == args.intent)
            )
            .mappings()
            .one()
        )
        c.rollback()
    before = _counts(engine)
    client = PayPalSandboxClient(s)
    try:
        gi = client.get_invoice(sess["provider_invoice_id"])
        gc = client.get_capture(st["capture_id"])
    finally:
        client.close()
    if not (gi.ok and gc.ok):
        print(f"STOPPED: provider GET failed ({gi.status_code}, {gc.status_code}); nothing written")
        return 1
    now = datetime.now(UTC)
    with engine.begin() as c:
        v = verify_settlement(
            c,
            capture_id=st["capture_id"],
            invoice_body=gi.body,
            capture_body=gc.body,
            require_recorded_reads=False,
        )
        if not args.record:
            print(json.dumps({"dry_run": True, "problems": v.problems, "digest": v.digest}))
            c.rollback()
            return 0 if v.passed else 1
        c.execute(
            settlement_verification.insert().values(
                capture_id=st["capture_id"],
                intent_id=args.intent,
                checker_version=v.checker_version,
                purpose="reverification",
                verdict="passed" if v.passed else "failed",
                problems=v.problems,
                document=v.document,
                document_sha256=v.digest,
                invoice_body_sha256=v.document["inputs"]["invoice_body_sha256"],
                capture_body_sha256=v.document["inputs"]["capture_body_sha256"],
                verified_at=now,
                actor="script:p5_reverify_settlement",
            )
        )
    after = _counts(engine)
    with engine.connect() as c:
        stored = c.execute(
            select(settlement_verification.c.document, settlement_verification.c.document_sha256)
            .where(settlement_verification.c.capture_id == st["capture_id"])
            .order_by(settlement_verification.c.id.desc())
        ).first()
        corrections = [
            dict(r)
            for r in c.execute(
                text(
                    "SELECT subject_table, subject_id, aspect, recorded_as, corrected_to, "
                    "corrected_by FROM evidence_correction ORDER BY id"
                )
            ).mappings()
        ]
        st_now = (
            c.execute(
                select(provider_settlement).where(provider_settlement.c.intent_id == args.intent)
            )
            .mappings()
            .one()
        )
        attribution = payer_attribution(c, st_now)
        verification = verification_view(c, st_now)
        c.rollback()
    report = {
        "run_id": now.strftime("%Y%m%dT%H%M%SZ"),
        "environment": "sandbox",
        "evidence_class": "Sandbox-verified (fresh provider GETs) for the provider facts; "
        "the payer's source account remains unverified",
        "intent": args.intent,
        "capture_id": st["capture_id"],
        "invoice_id": st["invoice_id"],
        "provider_reads": {
            "invoice": {"http_status": gi.status_code, "debug_id": gi.debug_id},
            "capture": {"http_status": gc.status_code, "debug_id": gc.debug_id},
            "bodies_match_booking_time_reads": v.document["inputs"]["bodies_match_recorded_reads"],
        },
        "checker_version": CHECKER_VERSION,
        "verdict": "passed" if v.passed else "failed",
        "problems": v.problems,
        "document_sha256": v.digest,
        "stored_document_reproduces_digest": stored is not None
        and sha256_hex(canonical(stored[0])) == stored[1] == v.digest,
        "provider_facts": v.document["inputs"]["provider_facts"],
        "records_examined": v.document["records"],
        "historical_record": {
            "check_sha256": st["check_sha256"],
            "check_sha256_is_placeholder": st["check_sha256"] == "0" * 64,
            "bundle_sha256": st["bundle_sha256"],
            "payer_merchant_column": st["payer_merchant"],
        },
        "evidence_corrections": corrections,
        "payer_attribution": attribution,
        "verification_view": verification,
        "financial_rows_before": before,
        "financial_rows_after": after,
        "financial_rows_unchanged": before == after,
    }
    body = json.dumps(report, indent=2, sort_keys=True, default=str)
    body = body.replace(socket.gethostname() + ":", "redacted-host:")
    secrets = [
        s.paypal_client_id.get_secret_value(),
        s.paypal_client_secret.get_secret_value(),
        s.paypal_sandbox_payer_email.get_secret_value(),
    ]
    if any(x and x in body for x in secrets) or "@" in body or "https://" in body:
        print("REFUSED: evidence would contain a secret, email or URL; nothing written")
        return 3
    path = ROOT / "evidence" / "phase5" / f"reverification_{report['run_id']}.json"
    path.write_text(body + "\n", encoding="utf-8")
    print(body)
    ok = v.passed and before == after and report["stored_document_reproduces_digest"]
    print(f"{'REVERIFIED: PASSED' if ok else 'REVIEW REQUIRED'}; wrote {path.relative_to(ROOT)}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
