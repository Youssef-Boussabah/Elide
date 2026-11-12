"""Seed the P5 Sandbox pilot network (`sandbox-pilot-v1`) into the configured database.

    uv run python scripts/seed_sandbox_pilot.py

Local/test/ci only. Creates two TEST-identity merchants, `pilot_001` (USD 25.00) and the
seller's USD 1.00 early-payment offer. It does NOT bind PayPal accounts, attest capacity,
accept the offer, approve a payment or call PayPal: each of those is a separate human step
(see `evidence/phase5/sandbox_operation_gate_2026-11-12.md`). Generated passwords go to the
git-ignored `.local/pilot_credentials.json` and are never printed.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from api.config import get_settings  # noqa: E402
from api.db import get_engine  # noqa: E402
from fixtures.sandbox_pilot import seed_pilot  # noqa: E402

CREDENTIALS = ROOT / ".local" / "pilot_credentials.json"


def main() -> int:
    settings = get_settings()
    if not settings.demo_identities_allowed:
        print("refused: pilot identities are only seeded when APP_ENV is local, test or ci")
        return 2
    now = datetime.now(UTC)
    result = seed_pilot(get_engine(), now=now)
    CREDENTIALS.parent.mkdir(exist_ok=True)
    CREDENTIALS.write_text(
        json.dumps(
            {
                "label": "TEST IDENTITIES for the Sandbox pilot; not real merchants",
                "created_at": now.isoformat(),
                "users": result.passwords,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"seeded {len(result.users)} TEST identities; passwords written to {CREDENTIALS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
