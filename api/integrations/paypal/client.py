"""Minimal PayPal REST client for merchant-owned Invoicing v2 (Sandbox only).

Scope (Phase 0 feasibility): OAuth client-credentials token, list/generate-number/create/
send/get invoices. It never pays, captures, refunds or marks invoices paid, and it is not
reachable by the LLM adapter.

Responses are returned as `ProviderResponse` (status, body, PayPal debug id) instead of
raising on 4xx, so the caller can record exact provider behavior, including 403s, as
evidence. A 403 is an access boundary to record, not something to work around.
"""

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from api.config import PAYPAL_SANDBOX_API_BASE, Settings
from api.integrations.paypal.money import cents_to_paypal


class PayPalConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderResponse:
    operation: str
    method: str
    path: str
    status_code: int
    body: Any
    debug_id: str | None
    request_id: str | None = None
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


@dataclass
class _Token:
    value: str
    expires_at: float
    scope: str = ""
    raw_meta: dict[str, Any] = field(default_factory=dict)


class PayPalSandboxClient:
    def __init__(
        self,
        settings: Settings,
        *,
        base_url: str = PAYPAL_SANDBOX_API_BASE,
        transport: httpx.BaseTransport | None = None,
        timeout_s: float = 20.0,
    ):
        if base_url != PAYPAL_SANDBOX_API_BASE:
            raise PayPalConfigError("Only the PayPal Sandbox API base URL is permitted.")
        if not (settings.paypal_client_id and settings.paypal_client_secret):
            raise PayPalConfigError(
                "PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET are not set in the local .env."
            )
        self._client_id = settings.paypal_client_id.get_secret_value()
        self._secret = settings.paypal_client_secret.get_secret_value()
        self._http = httpx.Client(base_url=base_url, timeout=timeout_s, transport=transport)
        self._token: _Token | None = None

    def close(self) -> None:
        self._http.close()

    # -- auth -------------------------------------------------------------------------
    def authenticate(self) -> ProviderResponse:
        start = time.monotonic()
        resp = self._http.post(
            "/v1/oauth2/token",
            data={"grant_type": "client_credentials"},
            auth=(self._client_id, self._secret),
            headers={"Accept": "application/json"},
        )
        body = _json(resp)
        meta: dict[str, Any] = {}
        if resp.status_code == 200 and isinstance(body, dict):
            self._token = _Token(
                value=body["access_token"],
                expires_at=time.monotonic() + int(body.get("expires_in", 0)) - 60,
                scope=body.get("scope", ""),
            )
            # Keep only non-secret metadata for evidence.
            meta = {
                "token_type": body.get("token_type"),
                "expires_in": body.get("expires_in"),
                "app_id_present": bool(body.get("app_id")),
                "scopes": sorted(body.get("scope", "").split()),
            }
        else:
            meta = body if isinstance(body, dict) else {"raw": str(body)[:300]}
        return ProviderResponse(
            "oauth_token",
            "POST",
            "/v1/oauth2/token",
            resp.status_code,
            meta,
            resp.headers.get("paypal-debug-id"),
            elapsed_ms=_ms(start),
        )

    @property
    def token_scopes(self) -> list[str]:
        return sorted(self._token.scope.split()) if self._token else []

    def _auth_headers(self) -> dict[str, str]:
        if self._token is None or time.monotonic() >= self._token.expires_at:
            r = self.authenticate()
            if not r.ok or self._token is None:
                raise PayPalConfigError(f"OAuth failed with HTTP {r.status_code}")
        return {"Authorization": f"Bearer {self._token.value}", "Accept": "application/json"}

    def _call(
        self,
        operation: str,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        request_id: str | None = None,
        prefer_representation: bool = False,
        content_type_json: bool = False,
    ) -> ProviderResponse:
        headers = self._auth_headers()
        if content_type_json:
            headers["Content-Type"] = "application/json"
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        if prefer_representation:
            headers["Prefer"] = "return=representation"
        start = time.monotonic()
        resp = self._http.request(method, path, json=json, params=params, headers=headers)
        return ProviderResponse(
            operation,
            method,
            path,
            resp.status_code,
            _json(resp),
            resp.headers.get("paypal-debug-id"),
            request_id,
            _ms(start),
        )

    # -- invoicing v2 -----------------------------------------------------------------
    def list_invoices(self, page_size: int = 1) -> ProviderResponse:
        return self._call(
            "list_invoices",
            "GET",
            "/v2/invoicing/invoices",
            params={"page": 1, "page_size": page_size, "total_required": "true"},
        )

    def generate_invoice_number(self) -> ProviderResponse:
        # Body-less POST. Without an explicit JSON content type Sandbox answered
        # HTTP 415 UNSUPPORTED_MEDIA_TYPE (debug id f81253945e66d, 2026-11-10).
        return self._call(
            "generate_invoice_number",
            "POST",
            "/v2/invoicing/generate-next-invoice-number",
            content_type_json=True,
        )

    def create_draft_invoice(self, payload: dict[str, Any], request_id: str) -> ProviderResponse:
        return self._call(
            "create_draft_invoice",
            "POST",
            "/v2/invoicing/invoices",
            json=payload,
            request_id=request_id,
            prefer_representation=True,
        )

    def send_invoice(self, invoice_id: str, request_id: str) -> ProviderResponse:
        return self._call(
            "send_invoice",
            "POST",
            f"/v2/invoicing/invoices/{invoice_id}/send",
            json={"send_to_invoicer": False, "send_to_recipient": True},
            request_id=request_id,
        )

    def get_invoice(self, invoice_id: str) -> ProviderResponse:
        return self._call("get_invoice", "GET", f"/v2/invoicing/invoices/{invoice_id}")


def new_request_id() -> str:
    return f"elide-{uuid.uuid4()}"


def build_draft_invoice(
    *, invoice_number: str | None, face_cents: int, discount_cents: int, payer_email: str, note: str
) -> dict[str, Any]:
    """Merchant-owned draft with one line item and an invoice-level discount.

    The discount is sent in PayPal's documented invoice-level discount field so the
    provider computes the payable total; the caller must read the total back from the
    provider instead of assuming it.
    """
    if not 0 <= discount_cents < face_cents:
        raise ValueError("discount must be >= 0 and less than face amount")
    detail: dict[str, Any] = {"currency_code": "USD", "note": note}
    if invoice_number:
        detail["invoice_number"] = invoice_number
    payload: dict[str, Any] = {
        "detail": detail,
        "primary_recipients": [{"billing_info": {"email_address": payer_email}}],
        "items": [
            {
                "name": "Elide Sandbox feasibility test (synthetic, not a real obligation)",
                "quantity": "1",
                "unit_amount": cents_to_paypal(face_cents),
                "unit_of_measure": "QUANTITY",
            }
        ],
    }
    if discount_cents:
        payload["amount"] = {
            "breakdown": {
                "discount": {"invoice_discount": {"amount": cents_to_paypal(discount_cents)}}
            }
        }
    return payload


def _json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return {"non_json_body_length": len(resp.content)}


def _ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)
