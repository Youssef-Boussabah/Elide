from fastapi.testclient import TestClient

from api.app.main import create_app

UNREACHABLE_DB = "postgresql+psycopg://x:y@127.0.0.1:1/none"  # port 1 is never PostgreSQL


def test_liveness_ok_without_database():
    resp = TestClient(create_app()).get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_readiness_reports_503_when_database_unreachable(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", UNREACHABLE_DB)
    resp = TestClient(create_app()).get("/health/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["database"] == "unreachable"
    assert body["paypal_sandbox_configured"] is False


def test_readiness_never_echoes_secret_values(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", UNREACHABLE_DB)
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "id-should-not-appear")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "secret-should-not-appear")
    text = TestClient(create_app()).get("/health/ready").text
    assert "should-not-appear" not in text
