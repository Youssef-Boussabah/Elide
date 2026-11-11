"""P3 demo endpoints (SYNTHETIC fixtures only; no authentication yet, so no mutations).

- POST /v1/demo/policies/draft         model-assisted policy draft + deterministic validation
- POST /v1/demo/policies/validate      deterministic validation of edited fields (no model)
- POST /v1/demo/negotiation/counteroffer  bounded counteroffer draft for the $5 -> $10 case
- POST /v1/demo/plans/explain          plain-language explanation of a verified P2 plan
- POST /v1/demo/opportunities          proactive read-only opportunity analysis (no model)
- POST /v1/demo/opportunities/explain  the same, narrated from verified facts

Routes that can call the model answer loopback clients only (403 otherwise) until P4
adds authentication, so no public unauthenticated route can spend paid tokens.

Every response separates `model_output` (untrusted, labelled REAL or MOCK) from
`deterministic` results. Nothing here confirms a policy, signs terms, reserves cash,
alters an invoice or makes a payment; there is no confirmation endpoint until P4 adds
authenticated merchant sessions. Only fixture merchants and scenarios are accepted, and
no database is written.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from api.ai.adapter import LLMAdapter, get_llm_adapter
from api.ai.schemas import PolicyExtraction
from api.domain.money import USD, Money
from api.negotiation.counteroffer import propose_counteroffer
from api.negotiation.explain import explain_plan, narrate
from api.opportunities.analysis import analyze, facts, merchant_view
from api.planning.plan import build_plan
from api.policy.service import MAX_TEXT_CHARS, draft_policy
from api.policy.validation import ValidationReport, validate_policy
from fixtures import ai_demo
from fixtures import planning as fx

router = APIRouter(prefix="/v1/demo", tags=["P3 synthetic demo"])
DemoMerchant = Literal["buyer", "food_supplier", "packaging_supplier", "carrier", "mechanic"]
LABELS = ["SYNTHETIC_DEMO", "NOT_EXECUTABLE", "NO_APPROVALS_CREATED"]


LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


def require_local_client(request: Request) -> None:
    """Routes that can spend model tokens answer only loopback clients until P4 adds
    authentication (bind uvicorn to 127.0.0.1, its default)."""
    host = request.client.host if request.client else ""
    if host not in LOCAL_HOSTS:
        raise HTTPException(403, "model-backed demo routes are local-only until P4 security")


def get_adapter() -> LLMAdapter:
    return get_llm_adapter()


Adapter = Annotated[LLMAdapter, Depends(get_adapter)]
LocalOnly = [Depends(require_local_client)]


class _Req(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PolicyDraftRequest(_Req):
    merchant_id: DemoMerchant
    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    source: Literal["merchant_message", "document_text"] = "merchant_message"


class PolicyValidateRequest(PolicyDraftRequest):
    extraction: PolicyExtraction


class CounterofferRequest(_Req):
    scenario: Literal["food_5_to_10"] = "food_5_to_10"
    viewer: Literal["buyer", "food_supplier"] = "buyer"
    requester_max_concession: str = Field(default="15.00", pattern=r"^\d{1,6}(\.\d{2})?$")
    disclosed_max_concession: str | None = Field(default=None, pattern=r"^\d{1,6}(\.\d{2})?$")


class ExplainRequest(_Req):
    scenario: Literal["two_candidates", "reserve_shortfall"] = "two_candidates"
    viewer: DemoMerchant = "buyer"
    plan: dict[str, Any] | None = None  # a stored plan record to re-check; default: fresh


def _report(rep: ValidationReport | None) -> dict[str, Any] | None:
    if rep is None:
        return None
    return {
        "label": "DETERMINISTIC_VALIDATION",
        "status": rep.status.value,
        "issues": [asdict(i) for i in rep.issues],
        "proposed": asdict(rep.proposed) if rep.proposed else None,
        "changes": list(rep.changes),
        "explanation": rep.explanation,
        "digest": rep.digest,
        "activation": "not active; requires the merchant's explicit confirmation of this "
        "digest (authenticated confirmation arrives in P4)",
    }


@router.post("/policies/draft", dependencies=LocalOnly)
def policy_draft(req: PolicyDraftRequest, adapter: Adapter) -> dict[str, Any]:
    d = draft_policy(
        adapter,
        merchant_id=req.merchant_id,
        text=req.text,
        source=req.source,
        as_of=ai_demo.AS_OF,
        context=ai_demo.merchant_context(req.merchant_id),
    )
    return {
        "labels": LABELS,
        "draft_id": d.draft_id,
        "status": d.status,
        "outcome": d.outcome,
        "message": d.message,
        "model_output": (
            {"label": "UNTRUSTED_MODEL_OUTPUT", "extraction": d.extraction.model_dump()}
            if d.extraction
            else None
        ),
        "model_call": d.model_call.as_dict() if d.model_call else None,
        "deterministic": _report(d.report),
    }


@router.post("/policies/validate")
def policy_validate(req: PolicyValidateRequest) -> dict[str, Any]:
    rep = validate_policy(
        merchant_id=req.merchant_id,
        text=req.text,
        source=req.source,
        as_of=ai_demo.AS_OF,
        extraction=req.extraction,
        context=ai_demo.merchant_context(req.merchant_id),
    )
    return {"labels": LABELS, "model_call": None, "deterministic": _report(rep)}


def _cents(s: str) -> int:
    whole, _, frac = s.partition(".")
    return int(whole) * 100 + int((frac or "0").ljust(2, "0"))


@router.post("/negotiation/counteroffer", dependencies=LocalOnly)
def counteroffer(req: CounterofferRequest, adapter: Adapter) -> dict[str, Any]:
    case = ai_demo.negotiation_case(
        requester_max_cents=_cents(req.requester_max_concession),
        disclosed_max_cents=(
            _cents(req.disclosed_max_concession) if req.disclosed_max_concession else None
        ),
    )
    r = propose_counteroffer(adapter, case)
    view = r.requester_view if req.viewer == "buyer" else r.counterparty_view
    return {
        "labels": LABELS,
        "status": r.status,
        "invoice_id": r.invoice_id,
        "note": r.note,
        "model_output": (
            {"label": "UNTRUSTED_MODEL_OUTPUT", "proposal": r.proposal.model_dump()}
            if r.proposal and req.viewer == "buyer"
            else None
        ),
        "model_call": r.model_call.as_dict() if r.model_call else None,
        "deterministic": {
            "label": "DETERMINISTIC_CHECKS",
            "reason_codes": r.reason_codes if req.viewer == "buyer" else [],
            "allowed_range_cents": list(r.allowed_range)
            if r.allowed_range and req.viewer == "buyer"
            else None,
            "message_issues": [asdict(i) for i in r.message_issues],
            "draft_revision": r.draft_revision,
            "view": view,
        },
        "message_for_counterparty": (
            r.proposal.message_to_counterparty
            if r.proposal and r.status == "DRAFT_PROPOSED"
            else None
        ),
        "currency": USD,
        "requester_max_concession": str(Money(_cents(req.requester_max_concession), USD)),
    }


@router.post("/plans/explain", dependencies=LocalOnly)
def plans_explain(req: ExplainRequest, adapter: Adapter) -> dict[str, Any]:
    if req.scenario == "two_candidates":
        net = fx.network_two_candidates()
        fresh = build_plan(net)
    else:
        net = fx.network_reserve_shortfall()
        fresh = build_plan(net, required=frozenset({"freight_001"}))
    record = req.plan if req.plan is not None else fresh
    try:
        r = explain_plan(adapter, net, record, req.viewer)
    except (KeyError, TypeError) as exc:
        raise HTTPException(422, f"malformed plan record: {type(exc).__name__}") from None
    return {
        "labels": LABELS,
        "status": r.status,
        "viewer": r.viewer,
        "text": r.text,
        "text_source": r.source,
        "model_call": r.model_call.as_dict() if r.model_call else None,
        "deterministic": {
            "label": "DETERMINISTIC_FACTS",
            "facts": [{"id": k, "text": v} for k, v in r.facts],
            "issues": [asdict(i) for i in r.issues],
            "plan_problems": r.problems,
        },
    }


OPPORTUNITY_SCENARIOS = {
    "two_candidates": fx.network_two_candidates,
    "negotiation": fx.network_negotiation,
    "timing_matters": fx.network_timing_matters,
    "positive_sum": fx.network_positive_sum,
    "held_receipt": fx.network_held_receipt,
    "reserve_shortfall": fx.network_reserve_shortfall,
    "cad_settlement": fx.network_cad_settlement,
    "fee_cascade": fx.network_fee_cascade,
}


class OpportunityRequest(_Req):
    scenario: Literal[
        "two_candidates",
        "negotiation",
        "timing_matters",
        "positive_sum",
        "held_receipt",
        "reserve_shortfall",
        "cad_settlement",
        "fee_cascade",
    ] = "two_candidates"
    viewer: DemoMerchant = "buyer"


def _opportunity_view(req: OpportunityRequest) -> tuple[dict[str, Any], tuple[str, ...]]:
    net = OPPORTUNITY_SCENARIOS[req.scenario]()
    if req.viewer not in net.merchants:
        raise HTTPException(404, "no such merchant in this scenario")
    view = merchant_view(analyze(net), req.viewer)
    visible = (
        {req.viewer}
        | {x for a in view["my_actions"] for x in (a["payer"], a["payee"])}
        | {x for b in view["my_bottlenecks"] for x in (b["debtor"], b["creditor"])}
    )
    return view, tuple(sorted(set(net.merchants) - visible))


@router.post("/opportunities")
def opportunities(req: OpportunityRequest) -> dict[str, Any]:
    """Deterministic, read-only opportunity analysis for one merchant (no model call)."""
    view, _ = _opportunity_view(req)
    return {
        "labels": LABELS,
        "model_call": None,
        "deterministic": {"label": "DETERMINISTIC_ANALYSIS", **view},
    }


@router.post("/opportunities/explain", dependencies=LocalOnly)
def opportunities_explain(req: OpportunityRequest, adapter: Adapter) -> dict[str, Any]:
    view, others = _opportunity_view(req)
    fs = facts(view)
    r = narrate(adapter, fs, req.viewer, others)
    return {
        "labels": LABELS,
        "status": r.status,
        "text": r.text,
        "text_source": r.source,
        "model_call": r.model_call.as_dict() if r.model_call else None,
        "deterministic": {
            "label": "DETERMINISTIC_FACTS",
            "recommendation": view["recommendation"],
            "facts": [{"id": k, "text": v} for k, v in fs],
            "issues": [asdict(i) for i in r.issues],
        },
    }
