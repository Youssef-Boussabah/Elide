"""Deterministic scripted adapter for tests and offline demos. Always labelled MOCK.

`ScriptedLLMAdapter(responder)` calls `responder(schema_name, system, user)` to get a raw
JSON-like dict, then validates it with the same schema as a real call. Nothing here is
model inference; results carry `mocked=True` and `label: MOCK_MODEL_OUTPUT`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from api.ai.adapter import LLMOutputError, LLMResult, ModelCall, context_hash

T = TypeVar("T", bound=BaseModel)
Responder = Callable[[str, str, str], dict[str, Any]]


class ScriptedLLMAdapter:
    provider = "mock"
    mocked = True

    def __init__(self, responder: Responder, model: str = "scripted") -> None:
        self._responder = responder
        self.model = model
        self.calls: list[tuple[str, str, str]] = []

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
        self.calls.append((schema_name, system, user))
        raw = self._responder(schema_name, system, user)
        try:
            value = schema.model_validate(raw)
        except ValidationError as exc:
            raise LLMOutputError(
                f"mock output failed schema validation: {exc.error_count()}"
            ) from None
        return LLMResult(
            value,
            ModelCall(
                provider=self.provider,
                model=self.model,
                prompt_version=prompt_version,
                mocked=True,
                context_sha256=context_hash(system, user),
                input_tokens=0,
                output_tokens=0,
                estimated_cost_usd="0",
            ),
        )
