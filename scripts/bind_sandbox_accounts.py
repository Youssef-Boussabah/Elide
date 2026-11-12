"""Bind Elide merchants to PayPal Sandbox accounts (ADR-0012 §2.1). OWNER ACTION.

Writes two `provider_account` rows in the local database. It makes **no PayPal call** and
stores no credential: only the reference names (`env:PAYPAL_CLIENT_ID`,
`env:PAYPAL_SANDBOX_PAYER_EMAIL`) and fingerprints computed from the local `.env`.

- invoicer (payee): the Sandbox Business account that owns the app credentials. Its
  account fingerprint is the provider merchant id fingerprint recorded from a provider GET
  (`--payee-fingerprint`, e.g. from `scripts/p5_readonly_probe.py`); a capture whose payee
  differs is refused at reconciliation (wrong-payee guard).
- payer: the Sandbox Personal account in `PAYPAL_SANDBOX_PAYER_EMAIL`.

    uv run python scripts/bind_sandbox_accounts.py \\
        --payee pilot_seller --payer pilot_buyer \\
        --payee-fingerprint redacted:xxxxxxxxxxxx \\
        --consent "Owner consents: Sandbox test accounts; collect pilot_001 only" --confirm

Without `--confirm` it prints what it would bind and exits. Refused outside local/test/ci.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine

from api.config import get_settings
from api.execution import bindings
from api.integrations.paypal.redaction import fingerprint


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--payee", required=True, help="Elide merchant id of the payee (invoicer)")
    ap.add_argument("--payer", required=True, help="Elide merchant id of the payer")
    ap.add_argument("--payee-fingerprint", required=True)
    ap.add_argument("--consent", required=True)
    ap.add_argument("--confirm", action="store_true")
    args = ap.parse_args(argv)
    s = get_settings()
    if s.app_env not in ("local", "test", "ci"):
        print("REFUSED: Sandbox bindings are only created in local/test/ci")
        return 2
    if not args.payee_fingerprint.startswith("redacted:"):
        print("REFUSED: --payee-fingerprint must be a fingerprint, never a raw merchant id")
        return 2
    try:
        client_id = bindings.resolve(s, bindings.INVOICER_REF)
        email = bindings.resolve(s, bindings.PAYER_REF).strip().lower()
    except bindings.BindingError as exc:
        print(f"BLOCKED: {exc}")
        return 2
    plan = {
        "invoicer": (args.payee, fingerprint(client_id), args.payee_fingerprint),
        "payer": (args.payer, fingerprint(email), fingerprint(email)),
    }
    for role, (m, cred, acct) in plan.items():
        print(f"{role}: merchant={m} credential={cred} account={acct} (Sandbox, test account)")
    if not args.confirm:
        print("dry run: nothing written (add --confirm)")
        return 0
    engine = create_engine(s.database_url)
    now = datetime.now(UTC)
    with engine.begin() as conn:
        for role, (m, cred, acct) in plan.items():
            aid = bindings.bind(
                conn,
                merchant_id=m,
                role=role,
                credential_fingerprint=cred,
                account_fingerprint=acct,
                consent_note=args.consent,
                bound_by="owner-script",
                at=now,
                settlement_currency="CAD" if role == "invoicer" else None,
                country="CA" if role == "invoicer" else None,
                evidence_ref="evidence/phase5 (read-only probe)" if role == "invoicer" else None,
            )
            print(f"bound {role} {m}: {aid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
