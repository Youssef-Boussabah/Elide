from pathlib import Path

import pytest
from pydantic import BaseModel

from api.ai import LLMUnavailableError, UnconfiguredLLMAdapter, get_llm_adapter
from api.config import Settings


class _Schema(BaseModel):
    x: int


def test_secrets_hidden_in_repr(monkeypatch):
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "s3cr3t-value")
    s = Settings(_env_file=None)
    assert "s3cr3t-value" not in repr(s)
    assert s.paypal_client_secret.get_secret_value() == "s3cr3t-value"


def test_paypal_not_configured_by_default():
    assert Settings(_env_file=None).paypal_configured is False


def test_env_example_placeholders_count_as_unconfigured():
    s = Settings(
        _env_file=None,
        paypal_client_id="replace-with-sandbox-client-id",
        paypal_client_secret="replace-with-sandbox-client-secret",
    )
    assert s.paypal_configured is False
    assert s.paypal_client_id is None


def test_unconfigured_llm_refuses_rather_than_fabricating():
    adapter = get_llm_adapter(Settings(_env_file=None))
    assert isinstance(adapter, UnconfiguredLLMAdapter)
    with pytest.raises(LLMUnavailableError):
        adapter.generate_structured(system="s", user="u", schema=_Schema)


def test_ai_package_never_imports_payment_code():
    ai_dir = Path(__file__).resolve().parents[2] / "api" / "ai"
    for py in ai_dir.rglob("*.py"):
        source = py.read_text(encoding="utf-8").lower()
        assert "paypal" not in source.replace("no paypal access", ""), py.name
        assert "integrations" not in source, py.name
