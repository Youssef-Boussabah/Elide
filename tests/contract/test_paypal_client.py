"""PayPal client behavior against a local mock transport.

These tests do NOT contact PayPal and are not evidence of Sandbox behavior. They check
our request shaping, the sandbox-only guard and secret handling.
"""

import httpx
import pytest

from api.config import Settings
from api.integrations.paypal.client import (
    PayPalConfigError,
    PayPalSandboxClient,
    build_draft_invoice,
)
from api.integrations.paypal.redaction import redact

INVOICING = "https://uri.paypal.com/services/invoicing"


def _settings() -> Settings:
    return Settings(_env_file=None, paypal_client_id="cid", paypal_client_secret="csecret")


def _token_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "access_token": "TOKEN123",
            "token_type": "Bearer",
            "expires_in": 3600,
            "app_id": "APP",
            "scope": INVOICING,
        },
    )


def test_refuses_without_credentials():
    with pytest.raises(PayPalConfigError):
        PayPalSandboxClient(Settings(_env_file=None))


def test_refuses_non_sandbox_base_url():
    with pytest.raises(PayPalConfigError):
        PayPalSandboxClient(_settings(), base_url="https://api-m.paypal.com")


def test_token_not_exposed_and_bearer_used():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return _token_response()
        seen["auth"] = request.headers["Authorization"]
        return httpx.Response(
            200, json={"total_items": 0, "items": []}, headers={"paypal-debug-id": "dbg1"}
        )

    client = PayPalSandboxClient(_settings(), transport=httpx.MockTransport(handler))
    auth = client.authenticate()
    assert auth.ok
    assert "TOKEN123" not in str(auth.body)
    assert INVOICING in client.token_scopes
    r = client.list_invoices()
    assert r.ok
    assert r.debug_id == "dbg1"
    assert seen["auth"] == "Bearer TOKEN123"


def test_403_returned_as_evidence_not_raised():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return _token_response()
        return httpx.Response(403, json={"name": "NOT_AUTHORIZED"})

    r = PayPalSandboxClient(_settings(), transport=httpx.MockTransport(handler)).list_invoices()
    assert r.status_code == 403
    assert not r.ok


def test_generate_number_sends_json_content_type():
    # Regression: Sandbox returned 415 for this body-less POST without a content type.
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return _token_response()
        seen["ct"] = request.headers.get("Content-Type")
        return httpx.Response(200, json={"invoice_number": "0002"})

    client = PayPalSandboxClient(_settings(), transport=httpx.MockTransport(handler))
    assert client.generate_invoice_number().body == {"invoice_number": "0002"}
    assert seen["ct"] == "application/json"


def test_get_capture_is_read_only_get():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return _token_response()
        seen["call"] = (request.method, request.url.path, request.content)
        return httpx.Response(200, json={"id": "CAP1", "status": "COMPLETED"})

    client = PayPalSandboxClient(_settings(), transport=httpx.MockTransport(handler))
    assert client.get_capture("CAP1").ok
    assert seen["call"] == ("GET", "/v2/payments/captures/CAP1", b"")


def test_update_is_put_without_notifications():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return _token_response()
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        seen["prefer"] = request.headers.get("Prefer")
        return httpx.Response(200, json={"id": "INV2-X", "status": "DRAFT"})

    client = PayPalSandboxClient(_settings(), transport=httpx.MockTransport(handler))
    assert client.update_invoice("INV2-X", {"detail": {}}).ok
    assert seen == {
        "method": "PUT",
        "path": "/v2/invoicing/invoices/INV2-X",
        "params": {"send_to_recipient": "false", "send_to_invoicer": "false"},
        "prefer": "return=representation",
    }


def test_create_sends_idempotency_key():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return _token_response()
        seen.update(request.headers)
        return httpx.Response(201, json={"id": "INV2-TEST", "status": "DRAFT"})

    client = PayPalSandboxClient(_settings(), transport=httpx.MockTransport(handler))
    client.create_draft_invoice({"detail": {}}, request_id="elide-abc")
    assert seen["paypal-request-id"] == "elide-abc"


def test_draft_invoice_uses_exact_strings_and_discount():
    p = build_draft_invoice(
        invoice_number="0001",
        face_cents=100000,
        discount_cents=1000,
        payer_email="payer@example.com",
        note="n",
    )
    assert p["items"][0]["unit_amount"] == {"currency_code": "USD", "value": "1000.00"}
    assert p["amount"]["breakdown"]["discount"]["invoice_discount"]["amount"]["value"] == "10.00"
    with pytest.raises(ValueError):
        build_draft_invoice(
            invoice_number=None,
            face_cents=100,
            discount_cents=100,
            payer_email="p@example.com",
            note="n",
        )


def test_redaction_removes_pii_and_payer_links():
    body = {
        "primary_recipients": [{"billing_info": {"email_address": "payer@example.com"}}],
        "links": [{"rel": "payer-view", "href": "https://www.sandbox.paypal.com/invoice/p/#ABC"}],
        "note": "contact a@b.com",
        "id": "INV2-AAAA",
    }
    out = str(redact(body))
    assert "payer@example.com" not in out
    assert "a@b.com" not in out
    assert "sandbox.paypal.com/invoice" not in out
    assert "INV2-AAAA" in out
