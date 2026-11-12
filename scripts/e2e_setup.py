"""Prepare a fresh database for browser end-to-end tests (local/test/ci only).

Drops and recreates the `elide_e2e` database on the configured PostgreSQL server,
migrates it to head, seeds the SYNTHETIC workspace and the P5 Sandbox-pilot network
(bound to the mock provider of `scripts/e2e_app.py`) with TEST identities, and writes their
generated passwords to the git-ignored `.local/e2e_credentials.json`. Never touches the
developer's `elide` database and never prints passwords.

    uv run python scripts/e2e_setup.py        # prints the database URL to use (no password)
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import create_engine, make_url, text  # noqa: E402

from api.config import get_settings  # noqa: E402

DEFAULT = "postgresql+psycopg://elide:elide_local_dev_only@127.0.0.1:5433/elide"
NAME = "elide_e2e"


def main() -> int:
    if not get_settings().demo_identities_allowed:
        print("refused: e2e setup only runs when APP_ENV is local, test or ci")
        return 2
    server = make_url(os.environ.get("ELIDE_TEST_DATABASE_URL", DEFAULT))
    admin = create_engine(server, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{NAME}" WITH (FORCE)'))
        c.execute(text(f'CREATE DATABASE "{NAME}"'))
    admin.dispose()
    url = server.set(database=NAME).render_as_string(hide_password=False)
    os.environ["DATABASE_URL"] = url
    get_settings.cache_clear()
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(cfg, "head")

    from fixtures.e2e_mock import bind_pilot
    from fixtures.sandbox_pilot import seed_pilot
    from fixtures.workspace_seed import seed

    engine = create_engine(url)
    now = datetime.now(UTC)
    result = seed(engine, now=now)
    # P5: a non-synthetic pilot network bound to the MOCK provider of scripts/e2e_app.py
    # (four independent USD 25.00 obligations, buyer capacity attested for the test).
    pilot = seed_pilot(engine, now=now, obligations=4, attest_buyer=(20000, 5000))
    bind_pilot(engine, now)
    engine.dispose()
    out = ROOT / ".local" / "e2e_credentials.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(
        json.dumps(
            {"label": "TEST IDENTITIES (e2e)", "users": {**result.passwords, **pilot.passwords}}
        ),
        encoding="utf-8",
    )
    print(f"e2e database {NAME} ready; credentials in {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
