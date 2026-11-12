"""PayPal webhook signature verification by postback (ADR-0012 §7.1).

Uses PayPal's documented `POST /v1/notifications/verify-webhook-signature` with the
webhook id configured for this app (`PAYPAL_WEBHOOK_ID`); the event is posted back
verbatim (PayPal requires it "exactly as it was received"). Local pre-checks refuse
before any call:
- `paypal-cert-url` must be HTTPS on a PayPal **Sandbox** API host (environment check;
  a production or foreign certificate host is refused);
- `paypal-auth-algo` must be `SHA256withRSA`;
- `paypal-transmission-time` must parse and may not lie in the future beyond the skew.

Webhook simulator (mock) events cannot be verified by postback (PayPal documentation), so
they are refused here; Elide never trusts an unverified body.

`verify` returns True/False for a definite answer and raises `VerifierUnavailableError`
when PayPal could not be asked (the endpoint then answers 503 so PayPal retries).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import httpx

from api.integrations.paypal.client import PayPalSandboxClient

SANDBOX_CERT_HOSTS = frozenset({"api.sandbox.paypal.com", "api-m.sandbox.paypal.com"})


class VerifierUnavailableError(RuntimeError):
    pass


def precheck(headers: dict[str, str], now: datetime, skew: timedelta) -> str | None:
    """A reason to refuse without calling PayPal, or None."""
    cert = urlparse(headers.get("paypal-cert-url", ""))
    if cert.scheme != "https" or cert.hostname not in SANDBOX_CERT_HOSTS:
        return "CERT_URL_NOT_SANDBOX"
    if headers.get("paypal-auth-algo") != "SHA256withRSA":
        return "AUTH_ALGO"
    try:
        sent = datetime.fromisoformat(
            headers.get("paypal-transmission-time", "").replace("Z", "+00:00")
        )
    except ValueError:
        return "TRANSMISSION_TIME"
    if sent.tzinfo is None or sent > now + skew:
        return "TRANSMISSION_TIME"
    return None


class PayPalWebhookVerifier:
    configured = True

    def __init__(
        self,
        client_factory: Callable[[], PayPalSandboxClient],
        webhook_id: str,
        *,
        clock: Callable[[], datetime] | None = None,
        skew: timedelta = timedelta(minutes=5),
    ):
        if not webhook_id:
            raise ValueError("a webhook id is required")
        self._client_factory = client_factory
        self._webhook_id = webhook_id
        self._clock = clock or (lambda: datetime.now(UTC))
        self._skew = skew
        self.last_reason: str | None = None

    def verify(self, headers: dict[str, str], body: bytes) -> bool:
        h = {k.lower(): v for k, v in headers.items()}
        reason = precheck(h, self._clock(), self._skew)
        if reason:
            self.last_reason = reason
            return False
        client = self._client_factory()
        try:
            res = client.verify_webhook_signature(h, body, self._webhook_id)
        except httpx.HTTPError as exc:
            raise VerifierUnavailableError(type(exc).__name__) from None
        finally:
            client.close()
        if res.status_code >= 500 or res.status_code in (401, 429):
            raise VerifierUnavailableError(f"HTTP {res.status_code}")
        ok = (
            res.status_code == 200
            and isinstance(res.body, dict)
            and res.body.get("verification_status") == "SUCCESS"
        )
        self.last_reason = None if ok else "SIGNATURE_INVALID"
        return ok
