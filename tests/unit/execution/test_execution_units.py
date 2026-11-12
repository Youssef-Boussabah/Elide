"""P5 pure units: state machine, outcome classification, invoice terms, retry policy,
webhook verification, provider wiring and module boundaries (ADR-0012)."""

from __future__ import annotations

import ast
import datetime as dt
import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from api.config import Settings
from api.execution.invoice_session import (
    ExpectedInvoice,
    build_payload,
    invoice_number_for,
    recipient_view_url,
    terms_problems,
)
from api.execution.states import IN_EXECUTION, TERMINAL, TRANSITIONS, IntentStatus, allowed
from api.integrations.paypal.client import PayPalSandboxClient
from api.integrations.paypal.execution import Outcome, classify_exception, classify_status
from api.integrations.paypal.mock import (
    MockPayPal,
    MockRefusedInProductionError,
    build_mock_provider,
)
from api.integrations.paypal.webhook_verify import (
    PayPalWebhookVerifier,
    VerifierUnavailableError,
    precheck,
)
from api.workers.outbox import RetryPolicy
from api.workflow.networks import ACTIVE_INTENT
from api.workflow.webhooks import resource_identity

ROOT = Path(__file__).resolve().parents[3]
S = IntentStatus


# --- state machine -------------------------------------------------------------------------
def _migration():
    spec = importlib.util.spec_from_file_location(
        "m0006", ROOT / "migrations" / "versions" / "0006_p5_execution.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_python_transition_table_equals_the_database_trigger():
    sql = {(a, b) for a, bs in _migration().TRANSITIONS.items() for b in bs}
    py = {(a.value, b.value) for a, bs in TRANSITIONS.items() for b in bs}
    assert sql == py


def test_reconciled_is_final_and_nothing_returns_to_approved():
    assert TRANSITIONS[S.RECONCILED] == frozenset()
    assert all(S.APPROVED not in nxt for nxt in TRANSITIONS.values())
    assert {S.RECONCILED, S.FAILED, S.EXPIRED, S.CANCELLED, S.EXCEPTION} == TERMINAL
    assert not allowed(S.PROVIDER_CONFIRMED, S.PROVIDER_PENDING)  # no regression
    assert not allowed(S.PAYER_ACTION_PENDING, S.FAILED)  # once sent, never blindly "failed"


def test_planning_exclusion_set_equals_the_in_execution_states():
    assert set(ACTIVE_INTENT) == set(IN_EXECUTION)


# --- outcome classification ---------------------------------------------------------------
@pytest.mark.parametrize(
    ("op", "status", "want"),
    [
        ("create_invoice", 201, Outcome.OK),
        ("create_invoice", 422, Outcome.REJECTED),
        ("create_invoice", 403, Outcome.FORBIDDEN),
        ("create_invoice", 500, Outcome.UNKNOWN),
        ("send_invoice", 503, Outcome.UNKNOWN),
        ("get_invoice", 500, Outcome.RETRYABLE),
        ("get_invoice", 404, Outcome.NOT_FOUND),
        ("cancel_invoice", 429, Outcome.RETRYABLE),
    ],
)
def test_status_classification(op, status, want):
    assert classify_status(op, status) is want


def test_transport_errors_after_sending_are_unknown_for_mutations():
    req = httpx.Request("POST", "https://api-m.sandbox.paypal.com/x")
    connect = httpx.ConnectError("x", request=req)
    timeout = httpx.ReadTimeout("x", request=req)
    reset = httpx.RemoteProtocolError("x", request=req)
    assert classify_exception("create_invoice", connect) is Outcome.RETRYABLE
    assert classify_exception("create_invoice", timeout) is Outcome.UNKNOWN
    assert classify_exception("send_invoice", reset) is Outcome.UNKNOWN
    assert classify_exception("get_invoice", timeout) is Outcome.RETRYABLE


# --- invoice terms ------------------------------------------------------------------------
E = ExpectedInvoice(
    "ps_1", invoice_number_for("pin_1"), "pilot_001", 2, 2500, 100, 2400, dt.date(2026, 11, 14)
)
EMAIL = "payer@example.test"


def _created():
    mock = MockPayPal()
    r = mock.create_invoice(build_payload(E, EMAIL), "op-abcdef")
    return r.body["id"], mock


def _problems(body, inv_id, email=EMAIL):
    return terms_problems(body, E, invoice_id=inv_id, payer_email=email, expected_status="DRAFT")


def test_invoice_number_is_deterministic_and_bounded():
    assert invoice_number_for("pin_1") == invoice_number_for("pin_1")
    assert invoice_number_for("pin_1") != invoice_number_for("pin_2")
    assert invoice_number_for("x").startswith("ELIDE-") and len(invoice_number_for("x")) == 18


def test_payload_disables_partial_payment_and_tips_and_carries_the_concession():
    body = build_payload(E, EMAIL)
    assert body["configuration"]["partial_payment"]["allow_partial_payment"] is False
    assert body["configuration"]["allow_tip"] is False
    assert body["items"][0]["unit_amount"] == {"currency_code": "USD", "value": "25.00"}
    disc = body["amount"]["breakdown"]["discount"]["invoice_discount"]["amount"]
    assert disc["value"] == "1.00"


def test_provider_copy_matches_and_every_deviation_is_reported():
    inv_id, mock = _created()
    body = mock.get_invoice(inv_id).body
    assert _problems(body, inv_id, "PAYER@example.test") == []  # case-insensitive email
    assert _problems(body, inv_id, "other@example.test")
    assert _problems(body, "INV2-OTHER")
    tampered = json.loads(json.dumps(body))
    tampered["configuration"]["partial_payment"]["allow_partial_payment"] = True
    assert any("partial" in x for x in _problems(tampered, inv_id))
    tampered = json.loads(json.dumps(body))
    tampered["amount"]["value"] = "24.01"
    assert _problems(tampered, inv_id)
    tampered = json.loads(json.dumps(body))
    tampered["detail"]["invoice_number"] = "ELIDE-SOMEONEELSE"
    assert _problems(tampered, inv_id)


def test_only_sandbox_payer_urls_are_returned():
    inv_id, mock = _created()
    body = mock.get_invoice(inv_id).body
    assert recipient_view_url(body).startswith("https://www.sandbox.paypal.com/")
    body["detail"]["metadata"]["recipient_view_url"] = "https://www.paypal.com/invoice/p/#X"
    assert recipient_view_url(body) is None


# --- retry policy ---------------------------------------------------------------------------
def test_backoff_grows_is_capped_and_deterministic():
    pol = RetryPolicy(base_s=30, cap_s=600)
    d = [pol.delay(n, 7) for n in range(1, 8)]
    assert d == sorted(d) and d[-1] == 600 and d[0] >= 30
    assert pol.delay(3, 7) == pol.delay(3, 7)


# --- webhooks -----------------------------------------------------------------------------
NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
HEADERS = {
    "paypal-transmission-id": "abc",
    "paypal-transmission-time": "2026-11-12T11:59:00Z",
    "paypal-transmission-sig": "c2ln",
    "paypal-cert-url": "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-1",
    "paypal-auth-algo": "SHA256withRSA",
}


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"paypal-cert-url": "https://api.paypal.com/v1/certs/C"}, "CERT_URL_NOT_SANDBOX"),
        ({"paypal-cert-url": "http://api.sandbox.paypal.com/c"}, "CERT_URL_NOT_SANDBOX"),
        ({"paypal-cert-url": "https://evil.example/c"}, "CERT_URL_NOT_SANDBOX"),
        ({"paypal-auth-algo": "none"}, "AUTH_ALGO"),
        ({"paypal-transmission-time": "2026-11-12T13:00:00Z"}, "TRANSMISSION_TIME"),
        ({"paypal-transmission-time": "yesterday"}, "TRANSMISSION_TIME"),
    ],
)
def test_precheck_refuses_before_calling_paypal(change, reason):
    assert precheck({**HEADERS, **change}, NOW, timedelta(minutes=5)) == reason
    assert precheck(HEADERS, NOW, timedelta(minutes=5)) is None


def _settings():
    return Settings(
        paypal_client_id="test-client", paypal_client_secret="test-secret", _env_file=None
    )


def _client(handler):
    return PayPalSandboxClient(_settings(), transport=httpx.MockTransport(handler))


def _paypal(verify_status=200, verification="SUCCESS", seen=None):
    def handler(req: httpx.Request):
        if req.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600, "scope": ""})
        if seen is not None:
            seen.append(req.content)
        return httpx.Response(verify_status, json={"verification_status": verification})

    return handler


def test_verifier_posts_the_event_verbatim_with_the_configured_webhook_id():
    seen: list[bytes] = []
    raw = (
        b'{"id":"WH-1","event_type":"INVOICING.INVOICE.PAID","resource":{"id":"INV2-A"}, "x":1.50}'
    )
    v = PayPalWebhookVerifier(
        lambda: _client(_paypal(seen=seen)), "WH-CONFIGURED", clock=lambda: NOW
    )
    assert v.verify(HEADERS, raw) is True
    assert raw in seen[0]  # not re-serialized: "1.50" and the space survive
    sent = json.loads(seen[0])
    assert sent["webhook_id"] == "WH-CONFIGURED" and sent["webhook_event"]["id"] == "WH-1"


def test_verifier_failure_and_outage_are_distinct():
    bad = PayPalWebhookVerifier(
        lambda: _client(_paypal(verification="FAILURE")), "WH", clock=lambda: NOW
    )
    assert bad.verify(HEADERS, b"{}") is False and bad.last_reason == "SIGNATURE_INVALID"
    down = PayPalWebhookVerifier(
        lambda: _client(_paypal(verify_status=503)), "WH", clock=lambda: NOW
    )
    with pytest.raises(VerifierUnavailableError):
        down.verify(HEADERS, b"{}")


def test_resource_identity_uses_identifiers_only():
    paid = "INVOICING.INVOICE.PAID"
    assert resource_identity({"event_type": paid, "resource": {"id": "INV2-AB12-CD"}}) == {
        "invoice_id": "INV2-AB12-CD"
    }
    nested = {"event_type": paid, "resource": {"invoice": {"id": "INV2-ZZ99"}}}
    assert resource_identity(nested) == {"invoice_id": "INV2-ZZ99"}
    cap = {
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {"id": "8AB12345", "invoice_id": "INV2-QQ11"},
    }
    assert resource_identity(cap) == {"capture_id": "8AB12345", "invoice_id": "INV2-QQ11"}
    assert resource_identity({"event_type": paid, "resource": {"id": "<script>"}}) == {}


# --- wiring and boundaries ------------------------------------------------------------------
def test_mock_provider_is_refused_in_production():
    with pytest.raises(MockRefusedInProductionError):
        build_mock_provider("production")
    assert isinstance(build_mock_provider("test"), MockPayPal)


def test_default_configuration_dispatches_nothing():
    from api.execution.runtime import build_provider, build_verifier, worker_kinds

    s = Settings(_env_file=None)
    assert s.payment_provider == "none" and s.payments_execution_enabled is False
    assert build_provider(s) is None
    assert build_verifier(s).configured is False
    assert set(worker_kinds(False)) == {"reconcile_payment", "provider_fetch"}


def test_sandbox_client_refuses_other_base_urls():
    from api.integrations.paypal.client import PayPalConfigError

    with pytest.raises(PayPalConfigError):
        PayPalSandboxClient(_settings(), base_url="https://api-m.paypal.com")


def _imports(pkg: str) -> set[str]:
    out = set()
    for f in (ROOT / "api" / pkg).rglob("*.py"):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module:
                out.add(node.module)
            elif isinstance(node, ast.Import):
                out.update(a.name for a in node.names)
    return out


@pytest.mark.parametrize("pkg", ["ai", "planning", "policy", "negotiation"])
def test_model_and_planner_cannot_reach_payments(pkg):
    forbidden = ("api.integrations", "api.execution", "api.workers")
    assert not [m for m in _imports(pkg) if m.startswith(forbidden)]


def test_execution_never_calls_payer_actions_of_the_mock():
    files = (ROOT / "api" / "execution").rglob("*.py")
    src = "\n".join(f.read_text(encoding="utf-8") for f in files)
    for action in (".pay(", ".mark_as_paid(", ".refund(", ".complete_pending(", ".tamper("):
        assert action not in src
