"""OpenAI-compatible adapter against a local mock transport (no network, no real model).

These tests are NOT evidence of model behaviour; they check request shaping, the
no-tools rule, secret handling, error mapping and the spend cap.
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
from pydantic import BaseModel, SecretStr

from api.ai.adapter import (
    LLMOutputError,
    LLMUnavailableError,
    UnconfiguredLLMAdapter,
    get_llm_adapter,
)
from api.ai.openai_compat import (
    DEFAULT_NEBIUS_MODEL,
    NEBIUS_BASE_URL,
    OpenAICompatibleAdapter,
    adapter_from_settings,
)
from api.ai.spend import Prices, SpendCapExceededError, SpendGuard
from api.config import Settings

KEY = "nb-test-key-not-real"


class Out(BaseModel):
    x: int


def _adapter(tmp_path, handler, cap="10.00", model="m"):
    return OpenAICompatibleAdapter(
        provider="nebius",
        base_url=NEBIUS_BASE_URL,
        api_key=SecretStr(KEY),
        model=model,
        prices=Prices(Decimal("0.20"), Decimal("0.60"), "test"),
        spend=SpendGuard(Decimal(cap), tmp_path / "spend.json"),
        transport=httpx.MockTransport(handler),
    )


def _ok(content: str, usage=(100, 20)):
    def handler(request: httpx.Request) -> httpx.Response:
        handler.seen = request
        return httpx.Response(
            200,
            json={
                "id": "req-1",
                "model": "m",
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]},
            },
        )

    return handler


def test_request_shape_is_structured_output_without_tools(tmp_path):
    h = _ok('{"x": 3}')
    res = _adapter(tmp_path, h).generate_structured(
        system="sys", user="usr", schema=Out, schema_name="out", prompt_version="p-v1"
    )
    assert res.value.x == 3
    body = json.loads(h.seen.content)
    assert h.seen.url == "https://api.tokenfactory.nebius.com/v1/chat/completions"
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["schema"]["properties"] == {
        "x": {"title": "X", "type": "integer"}
    }
    assert body["temperature"] == 0
    assert "tools" not in body and "functions" not in body and "tool_choice" not in body
    assert h.seen.headers["authorization"] == f"Bearer {KEY}"
    assert res.call.mocked is False and res.call.prompt_version == "p-v1"
    assert res.call.input_tokens == 100 and res.call.output_tokens == 20


def test_spend_is_recorded_and_the_cap_refuses_before_calling(tmp_path):
    h = _ok('{"x": 1}', usage=(1000, 1000))
    a = _adapter(tmp_path, h, cap="0.002")
    a.generate_structured(system="s", user="u", schema=Out, max_tokens=100)
    ledger = json.loads((tmp_path / "spend.json").read_text())
    # (1000*0.20 + 1000*0.60)/1e6 * 1.5 = 0.0012
    assert Decimal(ledger["total_usd"]) == Decimal("0.0012")
    h.seen = None
    with pytest.raises(SpendCapExceededError):
        a.generate_structured(system="s" * 3000, user="u", schema=Out, max_tokens=1500)
    assert h.seen is None  # refused before any HTTP request


def test_errors_never_contain_the_key(tmp_path):
    def boom(request):
        return httpx.Response(401, json={"error": f"bad key {KEY}"})

    with pytest.raises(LLMUnavailableError) as e:
        _adapter(tmp_path, boom).generate_structured(system="s", user="u", schema=Out)
    assert KEY not in str(e.value) and "401" in str(e.value)

    def down(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(LLMUnavailableError) as e:
        _adapter(tmp_path, down).generate_structured(system="s", user="u", schema=Out)
    assert KEY not in str(e.value)


@pytest.mark.parametrize("content", ['{"x": "three"}', "not json", '{"y": 1}'])
def test_invalid_output_is_an_error_not_a_guess(tmp_path, content):
    with pytest.raises(LLMOutputError):
        _adapter(tmp_path, _ok(content)).generate_structured(system="s", user="u", schema=Out)


def test_refusal_and_fenced_json(tmp_path):
    def refuse(request):
        return httpx.Response(
            200, json={"choices": [{"message": {"refusal": "no", "content": None}}]}
        )

    with pytest.raises(LLMOutputError):
        _adapter(tmp_path, refuse).generate_structured(system="s", user="u", schema=Out)
    res = _adapter(tmp_path, _ok('```json\n{"x": 7}\n```')).generate_structured(
        system="s", user="u", schema=Out
    )
    assert res.value.x == 7


def test_https_only(tmp_path):
    with pytest.raises(LLMUnavailableError):
        OpenAICompatibleAdapter(
            provider="nebius",
            base_url="http://api.example.com/v1/",
            api_key=SecretStr(KEY),
            model="m",
            prices=Prices(Decimal(1), Decimal(1), "t"),
            spend=SpendGuard(Decimal(1), tmp_path / "s.json"),
        )


def test_settings_select_nebius_defaults_and_need_a_price():
    s = Settings(_env_file=None, llm_provider="nebius", llm_api_key=KEY)
    a = adapter_from_settings(s)
    assert (a.provider, a.model) == ("nebius", DEFAULT_NEBIUS_MODEL)
    assert KEY not in repr(s)
    unknown = Settings(_env_file=None, llm_provider="nebius", llm_api_key=KEY, llm_model="x/y")
    with pytest.raises(LLMUnavailableError):
        adapter_from_settings(unknown)  # cannot enforce the cap without a price
    priced = Settings(
        _env_file=None,
        llm_provider="nebius",
        llm_api_key=KEY,
        llm_model="x/y",
        llm_price_input_per_m="0.1",
        llm_price_output_per_m="0.2",
    )
    assert adapter_from_settings(priced).model == "x/y"
    with pytest.raises(LLMUnavailableError):
        adapter_from_settings(Settings(_env_file=None, llm_provider="other", llm_api_key=KEY))


def test_no_key_means_unconfigured():
    assert isinstance(
        get_llm_adapter(Settings(_env_file=None, llm_provider="nebius")), UnconfiguredLLMAdapter
    )
