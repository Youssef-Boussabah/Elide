"""P3 demo API contracts with a MOCK model (dependency override). Not model evidence."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.ai.mock import ScriptedLLMAdapter
from api.app.demo_ai import get_adapter
from api.app.main import create_app
from api.planning.plan import build_plan
from fixtures import planning as fx
from fixtures.ai_eval_cases import CASES

BY_ID = {c["id"]: c for c in CASES}
PROPOSAL = {
    "invoice_id": "food_001",
    "proposed_concession": "10.00",
    "currency": "USD",
    "message_to_counterparty": "Could we settle invoice food_001 ($1,000.00) today for $990.00, "
    "with a $10.00 early-payment discount?",
    "rationale_for_requester": "The $5 offer is below your minimum.",
    "reason_codes": ["OFFER_BELOW_REQUESTER_MINIMUM"],
}


def responder(name, system, user):
    if name == "policy_extraction":
        text = user.split("<instruction>\n", 1)[1].rsplit("\n</instruction>", 1)[0]
        return next(c["reference"] for c in CASES if c["text"] == text)
    if name == "counteroffer":
        return PROPOSAL
    return {
        "headline": "Plan",
        "explanation": "A simulation; nothing was paid.",
        "cited_facts": ["F1"],
    }


@pytest.fixture
def client():
    app = create_app()
    app.dependency_overrides[get_adapter] = lambda: ScriptedLLMAdapter(responder)
    return TestClient(app)


def test_policy_draft_separates_model_output_from_deterministic_result(client):
    r = client.post(
        "/v1/demo/policies/draft", json={"merchant_id": "buyer", "text": BY_ID["ord-01"]["text"]}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "READY_FOR_CONFIRMATION"
    assert body["model_output"]["label"] == "UNTRUSTED_MODEL_OUTPUT"
    assert body["model_call"]["label"] == "MOCK_MODEL_OUTPUT" and body["model_call"]["mocked"]
    det = body["deterministic"]
    assert det["label"] == "DETERMINISTIC_VALIDATION"
    assert det["proposed"]["reserve_floor_cents"] == 60000
    assert "not active" in det["activation"]
    assert "SYNTHETIC_DEMO" in body["labels"]


def test_injection_through_the_api_is_not_ready(client):
    c = BY_ID["inj-01"]
    r = client.post(
        "/v1/demo/policies/draft",
        json={"merchant_id": "buyer", "text": c["text"], "source": "document_text"},
    )
    assert r.json()["status"] in ("NEEDS_CLARIFICATION", "REJECTED")


@pytest.mark.parametrize(
    "payload",
    [
        {"merchant_id": "real_merchant_42", "text": "Keep $600."},
        {"merchant_id": "buyer", "text": ""},
        {"merchant_id": "buyer", "text": "x" * 2001},
        {"merchant_id": "buyer", "text": "Keep $600.", "authorize": True},
    ],
)
def test_requests_outside_the_synthetic_contract_are_422(client, payload):
    assert client.post("/v1/demo/policies/draft", json=payload).status_code == 422


def test_validate_runs_without_any_model(client):
    c = BY_ID["rel-01"]
    r = client.post(
        "/v1/demo/policies/validate",
        json={"merchant_id": "buyer", "text": c["text"], "extraction": c["reference"]},
    )
    assert r.json()["model_call"] is None
    assert r.json()["deterministic"]["status"] == "REJECTED"


def test_counteroffer_draft_and_views(client):
    r = client.post("/v1/demo/negotiation/counteroffer", json={})
    body = r.json()
    assert body["status"] == "DRAFT_PROPOSED"
    d = body["deterministic"]["draft_revision"]
    assert d["status"] == "draft" and d["authorized_by"] == [] and d["approval_evidence"] is None
    assert body["deterministic"]["view"]["plan_includes_invoice"] is True
    supplier = client.post(
        "/v1/demo/negotiation/counteroffer", json={"viewer": "food_supplier"}
    ).json()
    assert supplier["model_output"] is None  # buyer's draft rationale is not shown to the supplier
    assert supplier["deterministic"]["reason_codes"] == []
    assert supplier["deterministic"]["allowed_range_cents"] is None
    assert "meets_your_declared_rules" in supplier["deterministic"]["view"]


def test_counteroffer_without_a_bounded_range(client):
    r = client.post("/v1/demo/negotiation/counteroffer", json={"requester_max_concession": "9.00"})
    assert r.json()["status"] == "NO_BOUNDED_PROPOSAL" and r.json()["model_call"] is None


def test_explain_fresh_and_stale_plans(client):
    r = client.post("/v1/demo/plans/explain", json={"viewer": "carrier"}).json()
    assert r["status"] == "EXPLAINED" and r["model_call"]["mocked"]
    stale = build_plan(fx.network_two_candidates(buyer_hurdle_bps=900))
    s = client.post("/v1/demo/plans/explain", json={"viewer": "buyer", "plan": stale}).json()
    assert s["status"] == "STALE_OR_TAMPERED" and s["model_call"] is None


def test_no_confirmation_or_payment_endpoints_exist(client):
    """The unauthenticated P3 demo surface can never confirm, approve, sign or pay. Since
    P4, those actions exist only under the authenticated, merchant-scoped `/v1/m/...`."""
    paths = set(client.app.openapi()["paths"])
    demo = [p for p in paths if p.startswith("/v1/demo/")]
    assert demo
    assert not any(("confirm" in p or "pay" in p or "approve" in p or "sign" in p) for p in demo)
    allowed = ("/health", "/v1/demo/", "/v1/auth/", "/v1/m/{merchant_id}/")
    assert all(p.startswith(allowed) or p == "/v1/provider/paypal/webhook" for p in paths)


def test_every_merchant_route_requires_a_session(client):
    """No P4 workspace route answers without an authenticated session (401 before any
    database access or merchant lookup)."""
    spec = client.app.openapi()["paths"]
    checked = 0
    for path, ops in spec.items():
        if not path.startswith("/v1/m/"):
            continue
        url = path.replace("{merchant_id}", "buyer")
        for name in ("{offer_id}", "{plan_id}", "{draft_id}", "{user_id}", "{obligation_id}"):
            url = url.replace(name, "x")
        for method in ops:
            r = client.request(method.upper(), url, json={})
            assert r.status_code == 401, (method, path, r.status_code)
            checked += 1
    assert checked >= 15
