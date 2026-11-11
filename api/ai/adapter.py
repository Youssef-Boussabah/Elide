"""Provider-neutral LLM interface (07_AI_ML_INTELLIGENCE.md).

Design constraints enforced by the interface itself:
- The only operation is "produce JSON matching this Pydantic schema". There is no
  tool-calling, function-calling or network-action hook to pass through.
- The adapter validates the JSON against the schema; the result is still untrusted model
  output until the deterministic checks in `api.policy` / `api.negotiation` pass.
- Every result carries a `ModelCall` record: provider, model, prompt version, whether it
  was a MOCK, token usage, estimated cost and a hash of the context sent. Mocked results
  are always labelled `mocked=True` and must never be presented as real inference.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol, TypeVar

from pydantic import BaseModel

from api.config import Settings, get_settings

T = TypeVar("T", bound=BaseModel)


class LLMUnavailableError(RuntimeError):
    """No model configured, the call failed, or the spend cap would be exceeded.
    Callers fall back to manual entry or deterministic templates."""


class LLMOutputError(RuntimeError):
    """The model answered, but not with valid JSON for the schema (or it refused)."""


@dataclass(frozen=True, slots=True)
class ModelCall:
    provider: str
    model: str
    prompt_version: str
    mocked: bool
    context_sha256: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost_usd: str | None = None  # Decimal string, upper-bound estimate
    request_id: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "mocked": self.mocked,
            "label": "MOCK_MODEL_OUTPUT" if self.mocked else "REAL_MODEL_OUTPUT",
            "context_sha256": self.context_sha256,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
            "request_id": self.request_id,
        }


@dataclass(frozen=True, slots=True)
class LLMResult[T: BaseModel]:
    value: T
    call: ModelCall


def context_hash(system: str, user: str) -> str:
    return hashlib.sha256(f"{system}\n\x00\n{user}".encode()).hexdigest()


class LLMAdapter(Protocol):
    provider: str
    model: str
    mocked: bool

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
        """Return `schema` parsed from the model's JSON output, plus call metadata."""
        ...


class UnconfiguredLLMAdapter:
    """Used until a provider/model/key is supplied. Always raises; never fabricates output."""

    provider = "none"
    model = "none"
    mocked = False

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
        raise LLMUnavailableError(
            "No LLM provider configured (set LLM_PROVIDER and LLM_API_KEY in .env)."
        )


def get_llm_adapter(settings: Settings | None = None) -> LLMAdapter:
    settings = settings or get_settings()
    if not (settings.llm_provider and settings.llm_api_key):
        return UnconfiguredLLMAdapter()
    from api.ai.openai_compat import adapter_from_settings

    return adapter_from_settings(settings)
