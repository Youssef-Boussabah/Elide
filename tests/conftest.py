import pytest

from api.config import get_settings
from api.db import get_engine


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch):
    """Tests never read a developer's real .env secrets."""
    monkeypatch.setenv("APP_ENV", "test")
    for key in (
        "PAYPAL_CLIENT_ID",
        "PAYPAL_CLIENT_SECRET",
        "PAYPAL_SANDBOX_PAYER_EMAIL",
        "LLM_PROVIDER",
        "LLM_MODEL",
        "LLM_API_KEY",
    ):
        monkeypatch.setenv(key, "")
    get_settings.cache_clear()
    get_engine.cache_clear()
    yield
    get_settings.cache_clear()
    get_engine.cache_clear()
