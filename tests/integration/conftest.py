"""PostgreSQL fixtures for integration tests.

`kernel_db` creates a fresh, uniquely named database on the configured server, runs the
real Alembic migrations against it, yields an engine, and drops the database afterwards.
The developer's `elide` database is never written to. Skipped (not passed) when
PostgreSQL is unreachable.
"""

import os
import uuid
from contextlib import contextmanager

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.exc import OperationalError

from api.config import get_settings

DEFAULT_URL = "postgresql+psycopg://elide:elide_local_dev_only@127.0.0.1:5433/elide"
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _server_url():
    return make_url(os.environ.get("ELIDE_TEST_DATABASE_URL", DEFAULT_URL))


def migrate(url: str, target: str = "head", direction: str = "up") -> None:
    """Run Alembic against `url` (env.py reads DATABASE_URL through settings)."""
    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    get_settings.cache_clear()
    try:
        cfg = Config(os.path.join(ROOT, "alembic.ini"))
        cfg.set_main_option("script_location", os.path.join(ROOT, "migrations"))
        (command.upgrade if direction == "up" else command.downgrade)(cfg, target)
    finally:
        if old is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old
        get_settings.cache_clear()


@contextmanager
def throwaway_database(pool_size: int = 12):
    """Create a uniquely named database, migrate it to head, yield (engine, url), drop it."""
    server = _server_url()
    admin = create_engine(server, isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 3})
    try:
        with admin.connect() as c:
            c.execute(text("SELECT 1"))
    except OperationalError as exc:
        admin.dispose()
        pytest.skip(f"PostgreSQL not reachable ({type(exc).__name__}); run docker compose up -d db")
    name = f"elide_test_{uuid.uuid4().hex[:10]}"
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = server.set(database=name).render_as_string(hide_password=False)
    engine = create_engine(url, pool_size=pool_size, max_overflow=4)
    try:
        migrate(url)
        yield engine, url
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(scope="session")
def kernel_db():
    with throwaway_database() as (engine, _url):
        yield engine


@pytest.fixture
def fresh_db():
    """A database of its own for tests that need a clean journal (yields engine, url)."""
    with throwaway_database(pool_size=6) as pair:
        yield pair


@pytest.fixture
def uid():
    """Unique suffix so tests sharing the session database never collide."""
    return uuid.uuid4().hex[:8]
