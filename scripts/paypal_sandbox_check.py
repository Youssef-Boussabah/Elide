"""PayPal Sandbox feasibility check (Phase 0). Sandbox only; never moves real money.

Reads credentials from the untracked local `.env`. Never prints secrets, tokens or the
hosted payer link. Each run writes a redacted evidence record to
evidence/paypal-sandbox/runs/.

Every run does: 1. OAuth token, 2. GET /v2/invoicing/invoices (API reachable).
Then at most ONE of these modes (each acting on provider state is opt-in):

  (no mode)                    stop after 1-2
  --check-number               POST generate-next-invoice-number (creates no invoice)
  --create-draft               create ONE draft ($1,000 - $10), GET it back. Never sends.
  --invoice-id ID              GET an existing invoice and compare its terms
      [--set-due-date DATE]    ...first full-update (PUT) a DRAFT's due date, then re-GET
  --send-invoice-id ID         send an EXISTING draft after pre-send checks. Never creates.

Usage (PowerShell, from repo root):
  uv run python scripts/paypal_sandbox_check.py
  uv run python scripts/paypal_sandbox_check.py --create-draft
  uv run python scripts/paypal_sandbox_check.py --invoice-id INV2-X --set-due-date 2026-12-01
  uv run python scripts/paypal_sandbox_check.py --send-invoice-id INV2-X \\
      --expected-due-date 2026-12-01
  uv run python scripts/paypal_sandbox_check.py --invoice-id INV2-X --expected-status PAID

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

import httpx  # noqa: E402

from api.config import Settings  # noqa: E402
from api.integrations.paypal.client import (  # noqa: E402
    PayPalConfigError,
    PayPalSandboxClient,
    ProviderResponse,
    build_draft_invoice,
    new_request_id,
)
from api.integrations.paypal.invoice_ops import (  # noqa: E402
    InvoiceOpError,
    build_due_date_update,
    extract_payer_link,
    presend_problems,
    recipient_matches,
)
from api.integrations.paypal.invoice_terms import compare_terms, invoice_terms  # noqa: E402
from api.integrations.paypal.redaction import fingerprint, redact  # noqa: E402

EVIDENCE_DIR = ROOT / "evidence" / "paypal-sandbox" / "runs"
# Git-ignored (`evidence/**/raw/`): holds the hosted payer link for the owner only.
RAW_DIR = ROOT / "evidence" / "paypal-sandbox" / "raw"
INVOICING_SCOPE = "https://uri.paypal.com/services/invoicing"
_ITEM_KEYS = ("name", "quantity", "unit_amount")


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
    *,
    face_cents: int,
    discount_cents: int,
    expected_status: str | None = None,
    expected_due_date: dt.date | None = None,
    payer_email: str | None = None,
    step: str = "fetch",
) -> tuple[ProviderResponse, bool]:
    """Independent GET of the provider resource; compare what PayPal stored to our request.

    Returns (response, all_checks_passed).
    """
    r = client.get_invoice(invoice_id)
    if not r.ok:
        rec.add(step, r, "FAIL", "could not retrieve invoice")
        return r, False
    t = invoice_terms(r.body)
    notes = [
        f"status={t.status}",
        f"currency={t.currency}",
        f"total_cents={t.total_cents}",
        f"item_total_cents={t.item_total_cents}",
        f"invoice_discount_cents={t.invoice_discount_cents} (sign={t.invoice_discount_sign})",
        f"tax_cents={t.tax_total_cents}",
        f"due_amount_cents={t.due_cents}",
        f"payment_term={t.payment_term_type}",
        f"due_date={t.due_date}",
        f"recipients={t.recipient_count}",
    ]
    if t.paid_cents is not None or t.payment_transaction_count:
        notes.append(f"paid_cents={t.paid_cents}; transactions={t.payment_transaction_count}")
    mismatches = compare_terms(
        t,
        face_cents=face_cents,
        discount_cents=discount_cents,
        expected_status=expected_status,
        expected_due_date=expected_due_date,
    )
    if payer_email is not None:
        ok = recipient_matches(r.body, payer_email)
        notes.append(f"recipient_is_configured_sandbox_payer={ok}")
        if not ok:
            mismatches.append("recipient differs from PAYPAL_SANDBOX_PAYER_EMAIL")
    if mismatches:
        notes.append("mismatches=[" + " | ".join(mismatches) + "]")
    else:
        notes.append("all checked terms match provider record")
    if t.status == "PAID":
        notes.append("provider-confirmed PAID; NOT evidence funds are spendable (D006)")
    rec.add(step, r, "MISMATCH" if mismatches else "OK", "; ".join(notes))
    return r, not mismatches


def _recipient_fp(body: dict[str, Any]) -> str:
    return fingerprint(json.dumps(body.get("primary_recipients"), sort_keys=True))


def _items(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [{k: i.get(k) for k in _ITEM_KEYS} for i in body.get("items") or []]


def _update_due_date(client, rec, args, payer: str) -> int:
    invoice_id, due = args.invoice_id, args.set_due_date
    common = {"face_cents": args.face_cents, "discount_cents": args.discount_cents}
    before, ok = _fetch_and_compare(
        client, rec, invoice_id, expected_status="DRAFT", payer_email=payer, step="before", **common
    )
    if not ok:
        rec.note("update", "BLOCKED", "current invoice not as expected; not updating")
        return 1
    try:
        payload = build_due_date_update(before.body, due)
    except (InvoiceOpError, KeyError, ValueError) as exc:
        rec.note("update", "BLOCKED", f"cannot build update: {exc}")
        return 1
    r = client.update_invoice(invoice_id, payload)
    rec.add("update", r, "OK" if r.ok else "FAIL", f"PUT full update, due_date={due}")
    if not r.ok:
        rec.note("update", "STOPPED", "update failed; no new invoice created, nothing sent")
        return 1
    after, ok = _fetch_and_compare(
        client,
        rec,
        invoice_id,
        expected_status="DRAFT",
        expected_due_date=due,
        payer_email=payer,
        step="after",
        **common,
    )
    if not after.ok:
        return 1
    same_recipient = _recipient_fp(before.body) == _recipient_fp(after.body)
    same_items = _items(before.body) == _items(after.body)
    same_number = (before.body.get("detail") or {}).get("invoice_number") == (
        after.body.get("detail") or {}
    ).get("invoice_number")
    unchanged = same_recipient and same_items and same_number
    rec.note(
        "after",
        "OK" if unchanged else "MISMATCH",
        f"unchanged by update: recipient={same_recipient} line_item={same_items} "
        f"invoice_number={same_number}",
    )
    return 0 if ok and unchanged else 1


def _send_existing(client, rec, args, payer: str) -> int:
    invoice_id = args.send_invoice_id
    common = {"face_cents": args.face_cents, "discount_cents": args.discount_cents}
    pre = client.get_invoice(invoice_id)
    if not pre.ok:
        rec.add("presend", pre, "FAIL", "could not retrieve invoice; not sending")
        return 1
    problems = presend_problems(
        pre.body, payer_email=payer, expected_due_date=args.expected_due_date, **common
    )
    t = invoice_terms(pre.body)
    summary = (
        f"status={t.status}; total_cents={t.total_cents}; due_date={t.due_date}; "
        f"recipient_is_configured_sandbox_payer={recipient_matches(pre.body, payer)}"
    )
    if problems:
        rec.add("presend", pre, "BLOCKED", summary + "; NOT SENT: " + " | ".join(problems))
        return 1
    rec.add("presend", pre, "OK", summary + "; all pre-send checks pass")

    request_id = new_request_id()
    r: ProviderResponse | None
    try:
        r = client.send_invoice(invoice_id, request_id=request_id)
    except httpx.TransportError as exc:
        # Unknown outcome: look the invoice up, never blind-retry the send.
        rec.note("send", "UNKNOWN", f"{type(exc).__name__}; looking up invoice instead of retry")
        r = None
    if r is not None:
        rec.add("send", r, "OK" if r.ok else "FAIL", f"PayPal-Request-Id={request_id}")
    post, _ = _fetch_and_compare(
        client,
        rec,
        invoice_id,
        expected_status=None,  # record PayPal's status, do not assume it
        expected_due_date=args.expected_due_date,
        payer_email=payer,
        step="postsend",
        **common,
    )
    if r is None or not r.ok:
        return 1
    link = extract_payer_link(r.body, post.body if post.ok else None)
    if link is None:
        rec.note("link", "WARN", "PayPal returned no Sandbox payer-view URL")
        return 1
    url, source = link
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    out = RAW_DIR / f"payer_link_{invoice_id}.txt"
    out.write_text(url + "\n", encoding="utf-8")
    rec.note("link", "OK", f"payer link from {source}; fingerprint={fingerprint(url)}")
    print(
        f"\nHosted payer link saved (not printed) to the git-ignored file:\n  "
        f"{out.relative_to(ROOT)}\nOpen it in a private window logged in as the SANDBOX "
        "PERSONAL account. Then verify:\n  uv run python scripts/paypal_sandbox_check.py "
        f"--invoice-id {invoice_id} --expected-status PAID"
    )
    return 0


def _create_draft(client, rec, args, payer: str) -> int:
    r = client.generate_invoice_number()
    number = r.body.get("invoice_number") if r.ok else None
    rec.add("number", r, "OK" if r.ok else "WARN", f"invoice_number={number}")
    payload = build_draft_invoice(
        invoice_number=number,
        face_cents=args.face_cents,
        discount_cents=args.discount_cents,
        payer_email=payer,
        note="Synthetic Elide Sandbox feasibility test. Not a real obligation.",
    )
    r = client.create_draft_invoice(payload, request_id=new_request_id())
    if not r.ok:
        verdict = "BLOCKED" if r.status_code == 403 else "FAIL"
        rec.add("draft", r, verdict, "draft invoice creation rejected")
        return 1
    invoice_id = r.body.get("id")
    rec.add("draft", r, "OK", f"invoice_id={invoice_id} status={r.body.get('status')}")
    _fetch_and_compare(
        client,
        rec,
        invoice_id,
        face_cents=args.face_cents,
        discount_cents=args.discount_cents,
        expected_status="DRAFT",
        payer_email=payer,
    )
    rec.note("send", "SKIPPED", f"send separately: --send-invoice-id {invoice_id}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check-number", action="store_true")
    mode.add_argument("--create-draft", action="store_true")
    mode.add_argument("--invoice-id", help="GET/compare an existing invoice")
    mode.add_argument("--send-invoice-id", help="send an EXISTING draft (never creates one)")
    ap.add_argument("--set-due-date", type=dt.date.fromisoformat, help="with --invoice-id")
    ap.add_argument("--expected-due-date", type=dt.date.fromisoformat)
    ap.add_argument("--expected-status", help="with --invoice-id, e.g. DRAFT, SENT, PAID")
    ap.add_argument("--face-cents", type=int, default=100000, help="default $1,000.00 (synthetic)")
    ap.add_argument("--discount-cents", type=int, default=1000, help="default $10.00")
    args = ap.parse_args()
    if args.set_due_date and not args.invoice_id:
        ap.error("--set-due-date requires --invoice-id")
    if args.set_due_date and args.set_due_date <= dt.datetime.now(dt.UTC).date():
        ap.error("--set-due-date must be in the future")

    settings = Settings()
    try:
        client = PayPalSandboxClient(settings)
    except PayPalConfigError as exc:
        print(f"BLOCKED: {exc}\nSee README.md > 'PayPal Sandbox setup'. Nothing was called.")
        return 2
    payer_secret = settings.paypal_sandbox_payer_email
    payer = payer_secret.get_secret_value() if payer_secret else None
    needs_payer = args.create_draft or args.send_invoice_id or args.set_due_date

    rec = Recorder()
    try:
        r = client.authenticate()
        if not r.ok:
            rec.add("auth", r, "FAIL", "check client id/secret are Sandbox credentials")
            return 1
        has_scope = INVOICING_SCOPE in client.token_scopes
        rec.add("auth", r, "OK", f"invoicing scope present={has_scope}")

        r = client.list_invoices()
        if not r.ok:
            verdict = "BLOCKED" if r.status_code == 403 else "FAIL"
            rec.add("access", r, verdict, "invoice API not accessible to this app/account")
            return 1
        rec.add("access", r, "OK", f"total_items={r.body.get('total_items')}")

        if needs_payer and not payer:
            rec.note("config", "BLOCKED", "PAYPAL_SANDBOX_PAYER_EMAIL not set in .env")
            return 2
        if args.check_number:
            r = client.generate_invoice_number()
            number = r.body.get("invoice_number") if r.ok else None
            rec.add("number", r, "OK" if r.ok else "FAIL", f"invoice_number={number}")
            return 0 if r.ok else 1
        if args.create_draft:
            return _create_draft(client, rec, args, payer)
        if args.send_invoice_id:
            return _send_existing(client, rec, args, payer)
        if args.invoice_id and args.set_due_date:
            return _update_due_date(client, rec, args, payer)
        if args.invoice_id:
            _, ok = _fetch_and_compare(
                client,
                rec,
                args.invoice_id,
                face_cents=args.face_cents,
                discount_cents=args.discount_cents,
                expected_status=args.expected_status,
                expected_due_date=args.expected_due_date,
                payer_email=payer,
            )
            return 0 if ok else 1
        rec.note("mode", "SKIPPED", "no mode given; stopped after auth + access")
        return 0
    finally:
        path = rec.save()
        print(f"\nRedacted evidence: {path.relative_to(ROOT)}  (commit {rec.commit})")
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
