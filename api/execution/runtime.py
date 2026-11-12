"""Wire settings to the P5 provider, worker runtime and webhook verifier (ADR-0012).

- `PAYMENT_PROVIDER=none` (default): nothing is dispatched; the payer link is unavailable.
- `mock`: the in-memory double, for local demos and tests; refused in production.
- `paypal_sandbox`: the Sandbox-only REST client (it refuses any non-Sandbox base URL).
- The webhook verifier exists only when `PAYPAL_WEBHOOK_ID` and Sandbox credentials are set.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import Engine

from api.config import Settings
from api.execution import bindings
from api.execution.service import HANDLERS, MUTATING_KINDS, Runtime
from api.integrations.paypal.client import PayPalSandboxClient
from api.integrations.paypal.execution import PaymentProvider, PayPalSandboxProvider
from api.integrations.paypal.mock import build_mock_provider
from api.integrations.paypal.webhook_verify import PayPalWebhookVerifier
from api.workflow.webhooks import RefusingVerifier, WebhookVerifier


def build_provider(settings: Settings) -> PaymentProvider | None:
    if settings.payment_provider == "none":
        return None
    if settings.payment_provider == "mock":
        return build_mock_provider(settings.app_env)
    return PayPalSandboxProvider(PayPalSandboxClient(settings))


def build_verifier(settings: Settings) -> WebhookVerifier:
    if not (settings.paypal_webhook_id and settings.paypal_configured):
        return RefusingVerifier()
    webhook_id = settings.paypal_webhook_id.get_secret_value()
    return PayPalWebhookVerifier(lambda: PayPalSandboxClient(settings), webhook_id)


def payer_email_resolver(settings: Settings) -> Callable[[], str | None]:
    def resolve() -> str | None:
        try:
            return bindings.resolve(settings, bindings.PAYER_REF)
        except bindings.BindingError:
            return None

    return resolve


def build_runtime(
    engine: Engine,
    settings: Settings,
    provider: PaymentProvider,
    *,
    clock: Callable[[], datetime] | None = None,
    worker_id: str | None = None,
    execution_enabled: Callable[[], bool] | None = None,
) -> Runtime:
    return Runtime(
        engine=engine,
        provider=provider,
        payer_email=payer_email_resolver(settings),
        execution_enabled=execution_enabled or (lambda: settings.payments_execution_enabled),
        clock=clock or (lambda: datetime.now(UTC)),
        worker_id=worker_id or f"{socket.gethostname()}:{os.getpid()}",
        poll_s=float(settings.worker_poll_seconds),
    )


def worker_kinds(execution_enabled: bool) -> list[str]:
    """With the kill switch off only read-only work is claimed (reconciliation continues)."""
    return [k for k in HANDLERS if execution_enabled or k not in MUTATING_KINDS]
