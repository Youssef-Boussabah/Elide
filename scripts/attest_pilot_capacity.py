"""Owner-run: attest the pilot payer's OWN USD balance (merchant-attested). OWNER ACTION.

    DATABASE_URL=<pilot database url> uv run python scripts/attest_pilot_capacity.py \\
        --spendable 200.00 --reserve 50.00 --confirm

The merchant declares its own balance; Elide never infers it, and this is labelled
MERCHANT_ATTESTED (not provider-verified) everywhere. The workspace has no attestation
screen yet, so this script calls the same service as `POST /v1/m/pilot_buyer/capacity-evidence`,
on behalf of the pilot buyer's owner. It refuses any database other than
`elide_sandbox_pilot` and any merchant other than `pilot_buyer`. It makes no PayPal call.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, select, text

from api.app.auth.tables import membership
from api.config import get_settings
from api.execution import views


def _cents(value: str) -> int:
    d = Decimal(value)
    if d < 0 or d != d.quantize(Decimal("0.01")):
        raise SystemExit(f"not an exact non-negative USD amount: {value}")
    return int(d * 100)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--spendable", required=True, help="USD, e.g. 200.00")
    ap.add_argument("--reserve", required=True, help="USD protected reserve, e.g. 50.00")
    ap.add_argument("--valid-hours", type=int, default=24)
    ap.add_argument("--confirm", action="store_true")
    args = ap.parse_args(argv)
    spendable, reserve = _cents(args.spendable), _cents(args.reserve)
    s = get_settings()
    engine = create_engine(s.database_url, connect_args={"connect_timeout": 5})
    with engine.connect() as c:
        db = c.execute(text("SELECT current_database()")).scalar_one()
    if db != "elide_sandbox_pilot":
        print(f"REFUSED: {db} is not the isolated pilot database")
        return 2
    print(
        f"pilot_buyer attests: verified spendable USD {spendable / 100:.2f}, protected reserve "
        f"USD {reserve / 100:.2f}, valid {args.valid_hours} h (MERCHANT_ATTESTED)"
    )
    if not args.confirm:
        print("dry run: nothing written (add --confirm)")
        return 0
    with engine.begin() as c:
        owner = c.execute(
            select(membership.c.user_id).where(
                membership.c.merchant_id == "pilot_buyer",
                membership.c.role == "owner",
                membership.c.revoked_at.is_(None),
            )
        ).scalar_one()
        res = views.attest_capacity(
            c,
            merchant_id="pilot_buyer",
            user_id=owner,
            spendable_cents=spendable,
            reserve_cents=reserve,
            valid_hours=args.valid_hours,
            included_receipt_ids=[],
            included_outflow_ids=[],
            at=datetime.now(UTC),
        )
    print(f"recorded {res['evidence_id']} ({res['label']}), valid until {res['valid_until']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
