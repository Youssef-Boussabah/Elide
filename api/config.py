"""Application settings, read from environment variables or an untracked local `.env`.

Secrets are held as `SecretStr` so they are never printed by repr/logging.
"""

from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PAYPAL_SANDBOX_API_BASE = "https://api-m.sandbox.paypal.com"
PLACEHOLDER_PREFIX = "replace-with"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["local", "test", "ci", "production"] = "local"
    database_url: str = Field(
        default="postgresql+psycopg://elide:elide_local_dev_only@127.0.0.1:5433/elide",
        description="SQLAlchemy URL. Local dev default matches compose.yaml.",
    )
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    # P4 sessions (ADR-0011 §3). Browser origins allowed to make state-changing requests.
    app_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ]
    # Secure + __Host- cookie prefix. Must be true behind TLS; forced true in production.
    session_cookie_secure: bool = False
    session_idle_minutes: int = Field(default=30, ge=1, le=240)
    session_absolute_hours: int = Field(default=8, ge=1, le=24)
    step_up_minutes: int = Field(default=10, ge=1, le=60)
    # Kill switch for provider-MUTATING calls (create/send/cancel invoice). With it off the
    # worker still reconciles (read-only GETs), as doc 08/11 require. P5 turns it on only
    # for an owner-authorized Sandbox operation.
    payments_execution_enabled: bool = False
    # Which provider the worker and the payer-link route use (ADR-0012). "none": nothing is
    # dispatched. "mock": in-memory double, refused in production. "paypal_sandbox": the
    # Sandbox REST API with the credentials below.
    payment_provider: Literal["none", "mock", "paypal_sandbox"] = "none"
    # Cancellation policy (ADR-0012 §16). False (the initial pilot): a passed pay-by time or
    # a revoked authority opens an operator review; nothing is cancelled automatically.
    auto_cancel_unpaid: bool = False
    worker_poll_seconds: int = Field(default=60, ge=5, le=3600)
    worker_lease_seconds: int = Field(default=120, ge=30, le=3600)

    # PayPal: Sandbox only. There is deliberately no live/production setting.
    paypal_client_id: SecretStr | None = None
    paypal_client_secret: SecretStr | None = None
    paypal_sandbox_payer_email: SecretStr | None = None
    paypal_webhook_id: SecretStr | None = None

    # LLM adapter (P3): an OpenAI-compatible provider, e.g. LLM_PROVIDER=nebius. Structured
    # JSON output only; the model gets no tools and no payment access.
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_api_key: SecretStr | None = None
    llm_base_url: str | None = None
    llm_price_input_per_m: Decimal | None = None  # USD per 1M tokens, for the spend cap
    llm_price_output_per_m: Decimal | None = None
    llm_spend_cap_usd: Decimal = Decimal("10.00")  # owner-authorized P3 budget

    @field_validator(
        "paypal_client_id",
        "paypal_client_secret",
        "paypal_sandbox_payer_email",
        "paypal_webhook_id",
        "llm_api_key",
        "llm_provider",
        "llm_model",
        "llm_base_url",
        mode="before",
    )
    @classmethod
    def _placeholder_is_unset(cls, v: object) -> object:
        """Empty strings and `.env.example` placeholders count as not configured."""
        if isinstance(v, str) and (not v.strip() or v.startswith(PLACEHOLDER_PREFIX)):
            return None
        return v

    @field_validator("llm_price_input_per_m", "llm_price_output_per_m", mode="before")
    @classmethod
    def _blank_price_is_unset(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("llm_spend_cap_usd", mode="before")
    @classmethod
    def _blank_cap_is_default(cls, v: object) -> object:
        return Decimal("10.00") if isinstance(v, str) and not v.strip() else v

    @property
    def cookie_secure(self) -> bool:
        return self.session_cookie_secure or self.app_env == "production"

    @property
    def demo_identities_allowed(self) -> bool:
        return self.app_env in ("local", "test", "ci")

    @property
    def paypal_configured(self) -> bool:
        return bool(self.paypal_client_id and self.paypal_client_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()
