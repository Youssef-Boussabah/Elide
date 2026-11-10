"""Requires `docker compose up -d db`. Skipped (not passed) when PostgreSQL is unreachable."""

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from api.app.main import create_app
from api.db import check_database

pytestmark = pytest.mark.integration

DEFAULT_URL = "postgresql+psycopg://elide:elide_local_dev_only@localhost:5433/elide"


@pytest.fixture
def live_db(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ.get("ELIDE_TEST_DATABASE_URL", DEFAULT_URL))
    try:
        return check_database()
    except OperationalError as exc:
        pytest.skip(f"PostgreSQL not reachable ({type(exc).__name__}); run docker compose up -d db")


def test_postgres_16_reachable(live_db):
    assert live_db.startswith("16")


def test_readiness_endpoint_with_database(live_db):
    resp = TestClient(create_app()).get("/health/ready")
    assert resp.status_code == 200
    assert resp.json()["database"] == "ok"
