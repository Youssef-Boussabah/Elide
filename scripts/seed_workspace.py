"""Seed the SYNTHETIC P4 workspace into the configured database (local/test/ci only).

    uv run python scripts/seed_workspace.py              # first seed
    uv run python scripts/seed_workspace.py --refresh    # new synthetic capacity evidence

Generated TEST-identity passwords are written to the git-ignored
`.local/demo_credentials.json` and never printed. These are not real merchant accounts.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from api.config import get_settings  # noqa: E402
from api.db import get_engine  # noqa: E402
from fixtures.workspace_seed import refresh_evidence, seed  # noqa: E402

CREDENTIALS = ROOT / ".local" / "demo_credentials.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="only add fresh synthetic evidence")
    args = ap.parse_args()
    settings = get_settings()
    if not settings.demo_identities_allowed:
        print("refused: demo identities are only seeded when APP_ENV is local, test or ci")
        return 2
    now = datetime.now(UTC)
    engine = get_engine()
    if args.refresh:
        refresh_evidence(engine, now)
        print("synthetic capacity evidence refreshed")
        return 0
    result = seed(engine, now=now)
    CREDENTIALS.parent.mkdir(exist_ok=True)
    CREDENTIALS.write_text(
        json.dumps(
            {
                "label": "TEST IDENTITIES for the synthetic workspace; not real merchants",
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
