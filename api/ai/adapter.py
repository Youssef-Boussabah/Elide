"""Provider-neutral LLM interface. Concrete providers are added in Phase 3.

Design constraints enforced by the interface itself:
- The only operation is "produce JSON matching this Pydantic schema". There is no
  tool-calling, function-calling or network-action hook to pass through.
- The caller validates the result with the same schema; the adapter's output is
  untrusted until that validation and the deterministic financial checks pass.
"""

from typing import Protocol, TypeVar

from pydantic import BaseModel

from api.config import Settings, get_settings

T = TypeVar("T", bound=BaseModel)


class LLMUnavailableError(RuntimeError):
    """No model configured or the call failed. Callers fall back to manual entry."""


class LLMAdapter(Protocol):
    provider: str
    model: str

    def generate_structured(self, *, system: str, user: str, schema: type[T]) -> T:
        """Return an instance of `schema` parsed from the model's JSON output."""
        ...


class UnconfiguredLLMAdapter:
    """Used until a provider/model/key is supplied. Always raises; never fabricates output."""

    provider = "none"
    model = "none"

    def generate_structured(self, *, system: str, user: str, schema: type[T]) -> T:
        raise LLMUnavailableError(
            "No LLM provider configured (set LLM_PROVIDER, LLM_MODEL, LLM_API_KEY in .env)."
        )


def get_llm_adapter(settings: Settings | None = None) -> LLMAdapter:
    settings = settings or get_settings()
    if not (settings.llm_provider and settings.llm_model and settings.llm_api_key):
        return UnconfiguredLLMAdapter()
    raise LLMUnavailableError(
        f"LLM provider {settings.llm_provider!r} is not implemented yet (Phase 3)."
    )
