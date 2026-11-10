"""Application settings, read from environment variables or an untracked local `.env`.

Secrets are held as `SecretStr` so they are never printed by repr/logging.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PAYPAL_SANDBOX_API_BASE = "https://api-m.sandbox.paypal.com"
PLACEHOLDER_PREFIX = "replace-with"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["local", "test", "ci"] = "local"
    database_url: str = Field(
        default="postgresql+psycopg://elide:elide_local_dev_only@localhost:5433/elide",
        description="SQLAlchemy URL. Local dev default matches compose.yaml.",
    )
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    # PayPal: Sandbox only. There is deliberately no live/production setting.
    paypal_client_id: SecretStr | None = None
    paypal_client_secret: SecretStr | None = None
    paypal_sandbox_payer_email: SecretStr | None = None
    paypal_webhook_id: SecretStr | None = None

    # LLM adapter: provider/model chosen later (open question 9). No payment tools.
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_api_key: SecretStr | None = None

    @field_validator(
        "paypal_client_id",
        "paypal_client_secret",
        "paypal_sandbox_payer_email",
        "paypal_webhook_id",
        "llm_api_key",
        mode="before",
    )
    @classmethod
    def _placeholder_is_unset(cls, v: object) -> object:
        """Empty strings and `.env.example` placeholders count as not configured."""
        if isinstance(v, str) and (not v.strip() or v.startswith(PLACEHOLDER_PREFIX)):
            return None
        return v

    @property
    def paypal_configured(self) -> bool:
        return bool(self.paypal_client_id and self.paypal_client_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()
