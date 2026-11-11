"""OpenAI-compatible chat-completions adapter (used for Nebius Token Factory).

One HTTP call per request: `POST {base_url}chat/completions` with
`response_format = {"type": "json_schema", "json_schema": {name, schema, strict: true}}`,
temperature 0, and no `tools` / `functions` field ever. The key comes from `.env`
(`LLM_API_KEY`, a SecretStr) and is only placed in the Authorization header; it is never
logged, returned or included in error messages. HTTPS only.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import TypeVar
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, SecretStr, ValidationError

from api.ai.adapter import LLMOutputError, LLMResult, LLMUnavailableError, ModelCall, context_hash
from api.ai.spend import Prices, SpendGuard, estimate_tokens
from api.config import Settings

T = TypeVar("T", bound=BaseModel)

NEBIUS_BASE_URL = "https://api.tokenfactory.nebius.com/v1/"
DEFAULT_NEBIUS_MODEL = "Qwen/Qwen3-235B-A22B-Instruct-2507"
_PRICE_SOURCE = (
    "Nebius Token Factory public model list as mirrored by benchlm.ai/hosts/nebius on "
    "2026-11-11 (Nebius: prices approximate); used only to enforce the spend cap"
)
KNOWN_PRICES: dict[str, Prices] = {
    "Qwen/Qwen3-235B-A22B-Instruct-2507": Prices(Decimal("0.20"), Decimal("0.60"), _PRICE_SOURCE),
    "openai/gpt-oss-120b": Prices(Decimal("0.15"), Decimal("0.60"), _PRICE_SOURCE),
    "Qwen/Qwen3-30B-A3B-Instruct-2507": Prices(Decimal("0.10"), Decimal("0.30"), _PRICE_SOURCE),
}


class OpenAICompatibleAdapter:
    mocked = False

    def __init__(
        self,
        *,
        provider: str,
        base_url: str,
        api_key: SecretStr,
        model: str,
        prices: Prices,
        spend: SpendGuard,
        timeout: float = 90.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise LLMUnavailableError("the LLM base URL must be an https URL")
        self.provider = provider
        self.model = model
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._key = api_key
        self._prices = prices
        self._spend = spend
        self._timeout = timeout
        self._transport = transport

    def generate_structured(
        self,
        *,
        system: str,
        user: str,
        schema: type[T],
        schema_name: str = "result",
        prompt_version: str = "unversioned",
        max_tokens: int = 1500,
    ) -> LLMResult[T]:
        body = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "schema": schema.model_json_schema(),
                    "strict": True,
                },
            },
        }
        estimate = self._prices.cost(estimate_tokens(system + user) * 2, max_tokens)
        reservation = self._spend.reserve(estimate)  # atomic; refuses before any request
        headers = {"Authorization": f"Bearer {self._key.get_secret_value()}"}
        try:
            with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
                resp = client.post(self._url, json=body, headers=headers)
        except (httpx.ConnectError, httpx.UnsupportedProtocol) as exc:
            self._spend.release(reservation)  # never reached the provider
            raise LLMUnavailableError(f"model call failed: {type(exc).__name__}") from None
        except httpx.HTTPError as exc:
            # outcome unknown (timeout, dropped connection): keep the full estimate
            self._spend.commit(
                reservation,
                cost=estimate,
                model=self.model,
                prompt_version=prompt_version,
                tokens=(0, 0),
                note=f"unknown outcome: {type(exc).__name__}",
            )
            raise LLMUnavailableError(f"model call failed: {type(exc).__name__}") from None
        if resp.status_code != 200:
            self._spend.release(reservation)
            raise LLMUnavailableError(f"model call returned HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            data = {}
        usage = data.get("usage") or {}
        tin = int(usage.get("prompt_tokens") or estimate_tokens(system + user))
        tout = int(usage.get("completion_tokens") or max_tokens)
        cost = self._prices.cost(tin, tout)
        self._spend.commit(
            reservation,
            cost=cost,
            model=self.model,
            prompt_version=prompt_version,
            tokens=(tin, tout),
        )
        call = ModelCall(
            provider=self.provider,
            model=str(data.get("model") or self.model),
            prompt_version=prompt_version,
            mocked=False,
            context_sha256=context_hash(system, user),
            input_tokens=tin,
            output_tokens=tout,
            estimated_cost_usd=str(cost),
            request_id=str(data.get("id")) if data.get("id") else None,
        )
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise LLMOutputError("model response has no message") from None
        if message.get("refusal"):
            raise LLMOutputError("model refused")
        content = message.get("content")
        if not isinstance(content, str):
            raise LLMOutputError("model returned no text content")
        try:
            value = schema.model_validate(json.loads(_strip_fences(content)))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise LLMOutputError(
                f"model output failed schema validation: {type(exc).__name__}"
            ) from None
        return LLMResult(value, call)


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    return t.strip()


def adapter_from_settings(settings: Settings) -> OpenAICompatibleAdapter:
    provider = (settings.llm_provider or "").lower()
    if provider not in ("nebius", "openai_compatible"):
        raise LLMUnavailableError(f"LLM provider {settings.llm_provider!r} is not supported")
    base_url = settings.llm_base_url or (NEBIUS_BASE_URL if provider == "nebius" else "")
    model = settings.llm_model or (DEFAULT_NEBIUS_MODEL if provider == "nebius" else "")
    if not base_url or not model:
        raise LLMUnavailableError("set LLM_BASE_URL and LLM_MODEL for this provider")
    if settings.llm_price_input_per_m is not None and settings.llm_price_output_per_m is not None:
        prices = Prices(
            settings.llm_price_input_per_m, settings.llm_price_output_per_m, "configured in .env"
        )
    elif model in KNOWN_PRICES:
        prices = KNOWN_PRICES[model]
    else:
        raise LLMUnavailableError(
            f"no price known for {model}; set LLM_PRICE_INPUT_PER_M / LLM_PRICE_OUTPUT_PER_M "
            "so the spend cap can be enforced"
        )
    if settings.llm_api_key is None:
        raise LLMUnavailableError("LLM_API_KEY is not set")
    return OpenAICompatibleAdapter(
        provider=provider,
        base_url=base_url,
        api_key=settings.llm_api_key,
        model=model,
        prices=prices,
        spend=SpendGuard(settings.llm_spend_cap_usd),
    )
