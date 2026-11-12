"""P5 read-only Sandbox probe (ADR-0012 acceptance A12). NO provider mutation.

Re-reads the already-paid P0 Sandbox resources with the existing app credentials and runs
them through the P5 code paths that a live reconciliation would use:

1. OAuth token (client credentials).
2. GET  /v2/invoicing/invoices/{P0 invoice}            (read)
3. POST /v2/invoicing/search-invoices {invoice_number} (read-only search; P5 lookup path)
4. GET  /v2/payments/captures/{P0 capture}             (read)
5. Pure checks: `settlement_from_bodies` (strict P1 settlement identity), the capture's
   payee fingerprint (what a binding is compared with), the credential fingerprint, the
   PAID strict term comparison, and `recipient_view_url` host shape.

Writes `evidence/phase5/readonly_probe_<UTC>.json` with status codes, debug ids, derived
numbers and fingerprints only: no token, client id, secret, email, name or URL.
Nothing is created, sent, cancelled, refunded or paid. No database is touched.

    uv run python scripts/p5_readonly_probe.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from api.config import get_settings  # noqa: E402
from api.execution.invoice_session import recipient_view_url  # noqa: E402
from api.integrations.paypal.client import PayPalConfigError, PayPalSandboxClient  # noqa: E402
from api.integrations.paypal.invoice_terms import compare_terms, invoice_terms  # noqa: E402
from api.integrations.paypal.redaction import fingerprint  # noqa: E402
from api.integrations.paypal.settlement import settlement_from_bodies  # noqa: E402

P0_INVOICE = "INV2-4RUV-XPEH-4F3C-T8BW"
P0_CAPTURE = "4J016440MV5285528"
P0_PAYEE_FINGERPRINT = "redacted:6dbc83888cc3"  # recorded in the P0 tracker (capture run)


def _git(*args: str) -> str:
    git = shutil.which("git")
    if git is None:
        raise OSError("git not found")
    return subprocess.run(  # noqa: S603  (fixed arguments, no shell)
        [git, *args], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()


def _sha() -> str:
    try:
        return _git("rev-parse", "HEAD") + ("+dirty" if _git("status", "--porcelain") else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def main() -> int:
    settings = get_settings()
    try:
        client = PayPalSandboxClient(settings)
    except PayPalConfigError as exc:
        print(f"BLOCKED: {exc}")
        return 2
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    rec: dict = {
        "run_id": run_id,
        "environment": "sandbox",
        "commit": _sha(),
        "mutations": "none",
        "steps": [],
    }

    def step(name, r):
        rec["steps"].append(
            {
                "step": name,
                "method": r.method,
                "path_template": name,
                "http_status": r.status_code,
                "debug_id": r.debug_id,
            }
        )
        print(f"{name}: HTTP {r.status_code}")
        return r

    try:
        step("oauth_token", client.authenticate())
        inv = step("GET /v2/invoicing/invoices/{id}", client.get_invoice(P0_INVOICE))
        srch = step(
            "POST /v2/invoicing/search-invoices",
            client.search_invoices(
                (inv.body.get("detail") or {}).get("invoice_number", "") if inv.ok else "0001"
            ),
        )
        cap = step("GET /v2/payments/captures/{id}", client.get_capture(P0_CAPTURE))
    finally:
        client.close()

    checks: dict = {}
    if inv.ok and cap.ok:
        t = invoice_terms(inv.body)
        checks["invoice_status"] = t.status
        checks["paid_strict_problems"] = compare_terms(
            t,
            face_cents=t.item_total_cents or 0,
            discount_cents=t.invoice_discount_cents or 0,
            expected_status="PAID",
        )
        s = settlement_from_bodies(inv.body, cap.body, "probe:get_invoice", "probe:get_capture")
        checks["settlement"] = {
            "gross_cents": s.gross.cents,
            "gross_currency": s.gross.currency,
            "fee_cents": s.provider_fee.cents,
            "net_cents": s.net.cents,
            "credit_cents": s.settlement_credit.cents,
            "credit_currency": s.settlement_credit.currency,
            "exchange_rate": str(s.exchange_rate.rate) if s.exchange_rate else None,
            "identity": "face - discount = gross; gross - fee = net; rate x net = credit: OK",
        }
        payee_fp = fingerprint((cap.body.get("payee") or {}).get("merchant_id"))
        checks["capture_payee_fingerprint"] = payee_fp
        checks["payee_fingerprint_matches_p0_record"] = payee_fp == P0_PAYEE_FINGERPRINT
        url = recipient_view_url(inv.body)
        checks["recipient_view_url_is_sandbox_host"] = url is not None
    if srch.ok:
        items = srch.body.get("items") or []
        checks["search_matches"] = len(items)
        checks["search_contains_p0_invoice"] = any(i.get("id") == P0_INVOICE for i in items)
    checks["credential_fingerprint"] = fingerprint(settings.paypal_client_id.get_secret_value())
    rec["checks"] = checks
    out = ROOT / "evidence" / "phase5" / f"readonly_probe_{run_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    text_ = json.dumps(rec, indent=2, sort_keys=True)
    secret_values = [
        settings.paypal_client_id.get_secret_value(),
        settings.paypal_client_secret.get_secret_value(),
    ]
    if settings.paypal_sandbox_payer_email:
        secret_values.append(settings.paypal_sandbox_payer_email.get_secret_value())
    if any(v and v in text_ for v in secret_values) or "@" in text_ or "http" in text_:
        print("REFUSED: evidence would contain a secret, email or URL; nothing written")
        return 3
    out.write_text(text_ + "\n", encoding="utf-8")
    print(f"wrote {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
