"""PayPal Sandbox feasibility check (Phase 0). Sandbox only; never moves real money.

Reads credentials from the untracked local `.env`. Never prints secrets or tokens.
Each step writes a redacted evidence record to evidence/paypal-sandbox/runs/.

Steps (each later step is opt-in because it creates provider state):
  1. auth        OAuth client-credentials token                      (always)
  2. access      GET /v2/invoicing/invoices (invoice API reachable)   (always)
  3. draft       create a merchant-owned draft invoice w/ discount    (--create-draft)
  4. send        send it; check for a hosted payer-view link          (--send)
  5. fetch       GET the invoice; compare provider amount/status      (with 3, or --invoice-id)

Usage (PowerShell, from repo root):
  uv run python scripts/paypal_sandbox_check.py
  uv run python scripts/paypal_sandbox_check.py --create-draft
  uv run python scripts/paypal_sandbox_check.py --create-draft --send
  uv run python scripts/paypal_sandbox_check.py --invoice-id INV2-XXXX --expected-cents 99000
  uv run python scripts/paypal_sandbox_check.py --check-number   (numbering only)

The payment itself is performed by a human in the PayPal-hosted payer page, logged in
as the Sandbox *personal* account. This script only observes the result afterwards.
"""

import argparse
import datetime as dt
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from api.config import Settings  # noqa: E402
from api.integrations.paypal.client import (  # noqa: E402
    PayPalConfigError,
    PayPalSandboxClient,
    ProviderResponse,
    build_draft_invoice,
    new_request_id,
)
from api.integrations.paypal.invoice_terms import compare_terms, invoice_terms  # noqa: E402
from api.integrations.paypal.redaction import redact  # noqa: E402

EVIDENCE_DIR = ROOT / "evidence" / "paypal-sandbox" / "runs"
INVOICING_SCOPE = "https://uri.paypal.com/services/invoicing"


def _git(*args: str) -> str:
    git = shutil.which("git")
    if git is None:
        raise OSError("git not found")
    return subprocess.run(  # noqa: S603  (fixed arguments, no shell)
        [git, *args], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit_sha() -> str:
    try:
        return _git("rev-parse", "HEAD") + ("+dirty" if _git("status", "--porcelain") else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


class Recorder:
    def __init__(self) -> None:
        self.run_id = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        self.records: list[dict[str, Any]] = []
        self.commit = _commit_sha()

    def add(self, step: str, r: ProviderResponse, verdict: str, notes: str = "") -> None:
        rec = {
            "step": step,
            "verdict": verdict,
            "notes": notes,
            "operation": r.operation,
            "method": r.method,
            "endpoint": r.path,
            "http_status": r.status_code,
            "paypal_debug_id": r.debug_id,
            "paypal_request_id": r.request_id,
            "elapsed_ms": r.elapsed_ms,
            "response_redacted": redact(r.body),
        }
        self.records.append(rec)
        print(
            f"[{verdict:>7}] {step:<8} {r.method} {r.path} -> HTTP {r.status_code}"
            f"{'  (' + notes + ')' if notes else ''}"
        )

    def note(self, step: str, verdict: str, notes: str) -> None:
        self.records.append({"step": step, "verdict": verdict, "notes": notes})
        print(f"[{verdict:>7}] {step:<8} {notes}")

    def save(self) -> Path:
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        path = EVIDENCE_DIR / f"{self.run_id}.json"
        manifest = {
            "run_id": self.run_id,
            "environment": "sandbox",
            "commit_sha": self.commit,
            "evidence_class": "sandbox-observation",
            "records": self.records,
        }
        path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return path


def _fetch_and_compare(
    client: PayPalSandboxClient,
    rec: Recorder,
    invoice_id: str,
    expected_cents: int | None,
    *,
    face_cents: int | None = None,
    discount_cents: int | None = None,
    expected_status: str | None = None,
) -> None:
    """Independent GET of the provider resource; compare what PayPal stored to our request."""
    r = client.get_invoice(invoice_id)
    if not r.ok:
        rec.add("fetch", r, "FAIL", "could not retrieve invoice")
        return
    t = invoice_terms(r.body)
    notes = [
        f"status={t.status}",
        f"currency={t.currency}",
        f"provider_total_cents={t.total_cents}",
        f"provider_item_total_cents={t.item_total_cents}",
        f"provider_invoice_discount_cents={t.invoice_discount_cents}"
        f" (sign={t.invoice_discount_sign})",
        f"provider_due_cents={t.due_cents}",
        f"provider_tax_cents={t.tax_total_cents}",
        f"payment_term={t.payment_term_type}",
    ]
    verdict = "OK"
    if face_cents is not None and discount_cents is not None:
        mismatches = compare_terms(
            t, face_cents=face_cents, discount_cents=discount_cents, expected_status=expected_status
        )
    else:
        mismatches = list(t.problems)
        if expected_cents is not None and t.total_cents != expected_cents:
            mismatches.append(f"total_cents: provider={t.total_cents} expected={expected_cents}")
    if mismatches:
        verdict = "MISMATCH"
        notes.append("mismatches=[" + " | ".join(mismatches) + "]")
    else:
        notes.append("all requested terms match provider record")
    if t.paid_cents is not None:
        notes.append(f"provider_paid_cents={t.paid_cents}")
    if t.status == "PAID":
        notes.append("provider-confirmed PAID; NOT evidence funds are spendable (D006)")
    rec.add("fetch", r, verdict, "; ".join(notes))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--create-draft", action="store_true")
    ap.add_argument(
        "--send", action="store_true", help="send the created draft (needs --create-draft)"
    )
    ap.add_argument("--invoice-id", help="only fetch/compare an existing invoice")
    ap.add_argument("--expected-status", help="with --invoice-id, e.g. DRAFT, UNPAID, PAID")
    ap.add_argument(
        "--check-number",
        action="store_true",
        help="only call generate-next-invoice-number (creates no invoice)",
    )
    ap.add_argument("--expected-cents", type=int, help="expected provider total for --invoice-id")
    ap.add_argument("--face-cents", type=int, default=100000, help="default $1,000.00 (synthetic)")
    ap.add_argument("--discount-cents", type=int, default=1000, help="default $10.00")
    args = ap.parse_args()
    if args.send and not args.create_draft:
        ap.error("--send requires --create-draft")

    settings = Settings()
    try:
        client = PayPalSandboxClient(settings)
    except PayPalConfigError as exc:
        print(f"BLOCKED: {exc}\nSee README.md > 'PayPal Sandbox setup'. Nothing was called.")
        return 2

    rec = Recorder()
    try:
        r = client.authenticate()
        if not r.ok:
            rec.add("auth", r, "FAIL", "check client id/secret are Sandbox credentials")
            return 1
        has_scope = INVOICING_SCOPE in client.token_scopes
        rec.add("auth", r, "OK", f"invoicing scope present={has_scope}")

        r = client.list_invoices()
        if r.ok:
            rec.add("access", r, "OK", f"total_items={r.body.get('total_items')}")
        else:
            rec.add(
                "access",
                r,
                "BLOCKED" if r.status_code == 403 else "FAIL",
                "invoice API not accessible to this app/account",
            )
            return 1

        if args.invoice_id:
            if (
                args.expected_cents is not None
                and args.expected_cents != args.face_cents - args.discount_cents
            ):
                ap.error("--expected-cents must equal --face-cents minus --discount-cents")
            _fetch_and_compare(
                client,
                rec,
                args.invoice_id,
                args.expected_cents,
                face_cents=args.face_cents,
                discount_cents=args.discount_cents,
                expected_status=args.expected_status,
            )
            return 0

        if args.check_number:
            r = client.generate_invoice_number()
            rec.add(
                "number",
                r,
                "OK" if r.ok else "FAIL",
                f"invoice_number={r.body.get('invoice_number') if r.ok else None}",
            )
            return 0 if r.ok else 1

        if not args.create_draft:
            rec.note("draft", "SKIPPED", "re-run with --create-draft to test draft creation")
            return 0

        if not settings.paypal_sandbox_payer_email:
            rec.note("draft", "BLOCKED", "PAYPAL_SANDBOX_PAYER_EMAIL not set in .env")
            return 2
        payer = settings.paypal_sandbox_payer_email.get_secret_value()

        r = client.generate_invoice_number()
        number = r.body.get("invoice_number") if r.ok else None
        rec.add("number", r, "OK" if r.ok else "WARN", f"invoice_number={number}")

        expected = args.face_cents - args.discount_cents
        payload = build_draft_invoice(
            invoice_number=number,
            face_cents=args.face_cents,
            discount_cents=args.discount_cents,
            payer_email=payer,
            note="Synthetic Elide Sandbox feasibility test. Not a real obligation.",
        )
        r = client.create_draft_invoice(payload, request_id=new_request_id())
        if not r.ok:
            rec.add(
                "draft",
                r,
                "BLOCKED" if r.status_code == 403 else "FAIL",
                "draft invoice creation rejected",
            )
            return 1
        invoice_id = r.body.get("id")
        rec.add("draft", r, "OK", f"invoice_id={invoice_id} status={r.body.get('status')}")
        _fetch_and_compare(
            client,
            rec,
            invoice_id,
            expected,
            face_cents=args.face_cents,
            discount_cents=args.discount_cents,
            expected_status="DRAFT",
        )

        if not args.send:
            rec.note(
                "send", "SKIPPED", "re-run with --create-draft --send to test hosted payer flow"
            )
            return 0
        r = client.send_invoice(invoice_id, request_id=new_request_id())
        links = r.body.get("links", []) if isinstance(r.body, dict) else []
        payer_view = next((link["href"] for link in links if link.get("rel") == "payer-view"), None)
        rec.add(
            "send",
            r,
            "OK" if r.ok and payer_view else ("FAIL" if not r.ok else "WARN"),
            f"payer_view_link_returned={bool(payer_view)}",
        )
        _fetch_and_compare(
            client,
            rec,
            invoice_id,
            expected,
            face_cents=args.face_cents,
            discount_cents=args.discount_cents,
            expected_status=None,  # post-send status is recorded, not assumed
        )
        if payer_view:
            # Printed to your local terminal only; redacted in saved evidence.
            print(
                "\nHosted payer page (open in a browser logged in as the SANDBOX PERSONAL "
                f"account, then pay):\n  {payer_view}\n"
                f"Afterwards verify independently:\n  uv run python scripts/paypal_sandbox_check.py"
                f" --invoice-id {invoice_id} --expected-cents {expected}"
            )
        return 0
    finally:
        path = rec.save()
        print(f"\nRedacted evidence: {path.relative_to(ROOT)}  (commit {rec.commit})")
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
