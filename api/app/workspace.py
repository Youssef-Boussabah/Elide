"""Authenticated, merchant-scoped workspace API: `/v1/m/{merchant_id}/...` (ADR-0011).

The merchant in the path is a resource selector: every route requires the principal's
active membership in it with the route's permission (else the same 404 as a missing
merchant). Money- and terms-changing routes also need a recent step-up and an
`Idempotency-Key`. Responses contain only that merchant's own data and the scoped views
from P2/P3; no other merchant's balances, reserves, rules or hidden invoices.

No route here calls PayPal, consumes paid model tokens (narration is the deterministic
template unless a workspace model is explicitly configured on app state), or moves money.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select

from api.ai.adapter import UnconfiguredLLMAdapter
from api.ai.schemas import PolicyExtraction
from api.app.auth import service as auth_service
from api.app.auth.deps import MerchantCtx, error, merchant_scope
from api.app.auth.permissions import Permission as P  # noqa: N817
from api.app.auth.tables import app_user, membership, merchant_org
from api.config import get_settings
from api.domain.money import Money
from api.execution import views as exec_views
from api.execution.runtime import payer_email_resolver
from api.negotiation.counteroffer import NegotiationCase, propose_counteroffer
from api.negotiation.explain import narrate
from api.opportunities.analysis import facts
from api.policy.service import PolicyDraft, draft_policy
from api.policy.validation import validate_policy
from api.workflow import audit, idempotency, negotiation, payments, plans, policies
from api.workflow.networks import capacity_summary, load_network
from api.workflow.store import (
    NotVisibleError,
    WorkflowError,
    iso,
    load_record,
    networks_of,
    require_party,
)
from api.workflow.tables import network as network_table
from api.workflow.tables import obligation, offer, payment_intent

router = APIRouter(prefix="/v1/m/{merchant_id}", tags=["merchant workspace"])


def ctx(permission: P, *, step_up: bool = False):
    return Annotated[MerchantCtx, Depends(merchant_scope(permission, step_up=step_up))]


Read = ctx(P.WORKSPACE_READ)
IdemKey = Annotated[str | None, Header(alias="Idempotency-Key")]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


def workflow_error_handler(_request: Request, exc: WorkflowError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status, content={"detail": {"code": exc.code, "message": str(exc)}}
    )


def _relock(conn, c: MerchantCtx, permission: P) -> None:
    """Re-check the actor's membership inside the writing transaction (share lock until
    commit), so a revocation committed before this write is always honoured."""
    try:
        auth_service.lock_membership(conn, c.principal.user_id, c.merchant_id, permission)
    except auth_service.NotFoundError:
        raise NotVisibleError("not found") from None
    except auth_service.ForbiddenError as exc:
        raise WorkflowError(str(exc), code="FORBIDDEN", status=403) from None


def _network_id(c: MerchantCtx, requested: str | None) -> str:
    with c.engine.begin() as conn:
        ids = networks_of(conn, c.merchant_id)
    if requested is None:
        if not ids:
            raise NotVisibleError("this merchant is not in any network", code="NO_NETWORK")
        return ids[0]
    if requested not in ids:
        raise NotVisibleError("not found")
    return requested


def _is_synthetic(conn, network_id: str) -> bool:
    return bool(
        conn.execute(
            select(network_table.c.is_synthetic).where(network_table.c.network_id == network_id)
        ).scalar_one()
    )


def _names(conn, ids: set[str]) -> dict[str, str]:
    rows = conn.execute(
        select(merchant_org.c.merchant_id, merchant_org.c.display_name).where(
            merchant_org.c.merchant_id.in_(ids)
        )
    )
    return {r.merchant_id: r.display_name for r in rows}


def _my_invoices(conn, c: MerchantCtx) -> list[dict[str, Any]]:
    oids = conn.execute(
        select(obligation.c.obligation_id)
        .where(or_(obligation.c.debtor == c.merchant_id, obligation.c.creditor == c.merchant_id))
        .order_by(obligation.c.obligation_id)
    ).scalars()
    out = []
    for oid in oids:
        rec = load_record(conn, oid).record
        ob, rev, pos = rec.obligation, rec.in_force_revision(c.now), rec.position(c.now)
        open_offer = conn.execute(
            select(offer.c.offer_id).where(
                offer.c.obligation_id == oid, offer.c.status == "proposed"
            )
        ).scalar_one_or_none()
        # An invoice can have several intents over time (a failed or expired one, then a
        # new one); the newest is its current payment state.
        intent = conn.execute(
            select(payment_intent.c.status)
            .where(payment_intent.c.obligation_id == oid)
            .order_by(payment_intent.c.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        out.append(
            {
                "obligation_id": oid,
                "direction": "payable" if ob.debtor == c.merchant_id else "receivable",
                "counterparty": ob.creditor if ob.debtor == c.merchant_id else ob.debtor,
                "face_cents": ob.face.cents,
                "currency": ob.currency,
                "open_cents": pos.remaining_open.cents,
                "concession_cents": pos.authorized_concession.cents,
                "due_at": rev.due_at.isoformat(),
                "revision_no": rev.revision_no,
                "settlement_status": pos.status.value,
                "dispute": rev.dispute.value,
                "open_offer_id": open_offer,
                "provenance": ob.provenance.source.value,
                "payment_state": _payment_state(intent),
            }
        )
    return out


PAYMENT_STATE_LABELS = {
    "approved": "INTENT_CREATED_NOT_SENT",
    "session_pending": "PREPARING_PAYPAL_INVOICE",
    "payer_action_pending": "AWAITING_PAYER_AT_PAYPAL",
    "provider_pending": "PROVIDER_PENDING",
    "provider_unknown": "PROVIDER_OUTCOME_UNKNOWN",
    "provider_confirmed": "PAYPAL_CONFIRMED_NOT_BOOKED",
    "reconciled": "PAYPAL_CONFIRMED_RECONCILED",
    "failed": "FAILED",
    "expired": "EXPIRED",
    "cancelled": "CANCELLED",
    "exception": "NEEDS_OPERATOR_REVIEW",
}


def _payment_state(intent_status: str | None) -> str:
    """Distinct labels (doc 13). PAYPAL_CONFIRMED never means spendable (D006)."""
    if intent_status is None:
        return "NOT_STARTED"
    return PAYMENT_STATE_LABELS.get(intent_status, intent_status.upper())


# --- read models ---------------------------------------------------------------------------
@router.get("/overview")
def overview(c: Read, network: str | None = None) -> dict[str, Any]:
    nid = _network_id(c, network)
    with c.engine.begin() as conn:
        synthetic = _is_synthetic(conn, nid)
        invoices = _my_invoices(conn, c)
        offers = negotiation.offers_for(conn, c.merchant_id, c.now)
        cap = capacity_summary(conn, c.merchant_id, c.now, synthetic=synthetic)
        pol = policies.latest(conn, c.merchant_id)
        names = _names(conn, {i["counterparty"] for i in invoices} | {c.merchant_id})
    return {
        "merchant": {
            "merchant_id": c.merchant_id,
            "display_name": c.membership.display_name,
            "is_synthetic": c.membership.is_synthetic,
            "my_role": c.membership.role,
            "my_permissions": sorted(p.value for p in c.membership.permissions),
        },
        "network_id": nid,
        "as_of": c.now.isoformat(),
        "labels": ["SYNTHETIC_NETWORK"] if synthetic else [],
        "capacity": cap,
        "invoices": [{**i, "counterparty_name": names.get(i["counterparty"])} for i in invoices],
        "offers_awaiting_me": sum(1 for o in offers if o["can_decide"]),
        "policy": {"confirmed_revision": pol.confirmed.version if pol else None},
    }


@router.get("/opportunities")
def opportunities(c: Read, network: str | None = None) -> dict[str, Any]:
    """READ-ONLY preview: nothing is written (durable plans: POST /plans)."""
    nid = _network_id(c, network)
    with c.engine.connect() as conn:  # no transaction is committed: a read cannot persist
        view, pview, _d = plans.preview(conn, network_id=nid, merchant_id=c.merchant_id, at=c.now)
        synthetic = _is_synthetic(conn, nid)
        conn.rollback()
    adapter = getattr(c.request.app.state, "workspace_llm", None)
    explanation = narrate(adapter, facts(view), c.merchant_id, ())
    return {
        "as_of": c.now.isoformat(),
        "network_id": nid,
        "labels": (["SYNTHETIC_NETWORK"] if synthetic else []) + ["READ_ONLY_ANALYSIS"],
        "opportunity": view,
        "plan_preview": pview,
        "explanation": {
            "status": explanation.status,
            "text": explanation.text,
            "facts": [{"id": k, "text": v} for k, v in explanation.facts],
            "model": None if adapter is None else getattr(adapter, "model", None),
        },
    }


@router.get("/network")
def network_view(c: Read, network: str | None = None) -> dict[str, Any]:
    """Only edges this merchant is a party to, and only its direct counterparties."""
    nid = _network_id(c, network)
    with c.engine.begin() as conn:
        invoices = _my_invoices(conn, c)
        nodes = {c.merchant_id} | {i["counterparty"] for i in invoices}
        names = _names(conn, nodes)
    return {
        "network_id": nid,
        "scope": "DIRECT_RELATIONSHIPS_ONLY",
        "nodes": [
            {"merchant_id": m, "display_name": names.get(m), "is_me": m == c.merchant_id}
            for m in sorted(nodes)
        ],
        "edges": [
            {
                "obligation_id": i["obligation_id"],
                "from": c.merchant_id if i["direction"] == "payable" else i["counterparty"],
                "to": i["counterparty"] if i["direction"] == "payable" else c.merchant_id,
                "open_cents": i["open_cents"],
                "due_at": i["due_at"],
                "terms_state": "OFFER_OPEN" if i["open_offer_id"] else "IN_FORCE",
                "payment_state": i["payment_state"],
                "funds_state": "NOT_USABLE_UNTIL_VERIFIED",
            }
            for i in invoices
        ],
        "legend": [
            "SIMULATED",
            "PROPOSED",
            "APPROVED",
            "INTENT_CREATED_NOT_SENT",
            "PAYPAL_CONFIRMED",
            "FUNDS_USABLE_ATTESTED",
            "BLOCKED",
        ],
    }


@router.get("/invoices/{obligation_id}")
def invoice_detail(c: Read, obligation_id: str) -> dict[str, Any]:
    with c.engine.begin() as conn:
        inv = require_party(conn, obligation_id, c.merchant_id)
    rec = inv.record
    return {
        "obligation_id": obligation_id,
        "debtor": rec.obligation.debtor,
        "creditor": rec.obligation.creditor,
        "face_cents": rec.obligation.face.cents,
        "currency": rec.currency,
        "position": {
            k: getattr(rec.position(c.now), k).cents
            for k in ("original_open", "cash_applied", "authorized_concession", "remaining_open")
        },
        "revisions": [
            {
                "revision_no": r.revision_no,
                "status": r.status.value,
                "due_at": r.due_at.isoformat(),
                "concession_cents": r.concession.cents,
                "created_at": r.created_at.isoformat(),
                "expires_at": iso(r.expires_at),
                "authorized_by": sorted(r.authorized_by),
                "supersedes": r.supersedes,
            }
            for r in rec.revisions
        ],
    }


# --- negotiation -------------------------------------------------------------------------
class OfferBody(_Body):
    obligation_id: str = Field(min_length=1, max_length=80)
    concession_cents: int = Field(ge=0)
    pay_by: datetime
    expires_at: datetime
    message: str | None = Field(default=None, max_length=500)
    source: Literal["manual", "ai_draft_reviewed"] = "manual"


class CounterTerms(_Body):
    concession_cents: int = Field(ge=0)
    pay_by: datetime
    expires_at: datetime
    message: str | None = Field(default=None, max_length=500)


class DecisionBody(_Body):
    decision: Literal["accept", "reject", "counter", "withdraw"]
    terms_digest: str = Field(min_length=64, max_length=64)
    counter: CounterTerms | None = None


@router.get("/offers")
def list_offers(c: Read) -> dict[str, Any]:
    with c.engine.begin() as conn:
        return {"offers": negotiation.offers_for(conn, c.merchant_id, c.now)}


@router.post("/offers")
def make_offer(
    body: OfferBody, c: ctx(P.OFFER_PROPOSE, step_up=True), key: IdemKey = None
) -> dict[str, Any]:
    if P.OFFER_DECIDE not in c.membership.permissions:
        raise error(403, "FORBIDDEN", "making an offer signs it; your role cannot sign terms")

    def op(conn):
        _relock(conn, c, P.OFFER_DECIDE)
        return negotiation.propose(
            conn,
            merchant_id=c.merchant_id,
            user_id=c.principal.user_id,
            session_id=c.principal.session_id,
            obligation_id=body.obligation_id,
            concession_cents=body.concession_cents,
            pay_by=body.pay_by,
            expires_at=body.expires_at,
            at=c.now,
            message=body.message,
            source=body.source,
            operation_key=key or "",
        )

    res, replayed = idempotency.run(
        c.engine,
        merchant_id=c.merchant_id,
        user_id=c.principal.user_id,
        key=key,
        route="offers.create",
        body=body.model_dump(mode="json"),
        at=c.now,
        operation=op,
    )
    return {**res, "replayed": replayed}


@router.post("/offers/{offer_id}/decision")
def decide_offer(
    offer_id: str,
    body: DecisionBody,
    c: ctx(P.OFFER_DECIDE, step_up=True),
    key: IdemKey = None,
) -> dict[str, Any]:
    def op(conn):
        _relock(conn, c, P.OFFER_DECIDE)
        return negotiation.decide(
            conn,
            merchant_id=c.merchant_id,
            user_id=c.principal.user_id,
            session_id=c.principal.session_id,
            offer_id=offer_id,
            decision=body.decision,
            terms_digest=body.terms_digest,
            at=c.now,
            operation_key=key or "",
            counter=body.counter.model_dump() if body.counter else None,
        )

    res, replayed = idempotency.run(
        c.engine,
        merchant_id=c.merchant_id,
        user_id=c.principal.user_id,
        key=key,
        route=f"offers.decision:{offer_id}",
        body=body.model_dump(mode="json"),
        at=c.now,
        operation=op,
    )
    return {**res, "replayed": replayed}


class DraftCounterBody(_Body):
    obligation_id: str = Field(min_length=1, max_length=80)
    max_concession_cents: int = Field(ge=1, le=10_000_000)


@router.post("/offers/draft")
def draft_counteroffer(body: DraftCounterBody, c: ctx(P.OFFER_PROPOSE)) -> dict[str, Any]:
    """Deterministic bounds (+ a model-drafted message only if a workspace model is
    configured). Creates nothing: the merchant reviews and submits via POST /offers."""
    nid = _network_id(c, None)
    with c.engine.begin() as conn:
        inv = require_party(conn, body.obligation_id, c.merchant_id)
        loaded = load_network(conn, nid, initiator=c.merchant_id, as_of=c.now)
    ob = inv.record.obligation
    if c.merchant_id != ob.debtor:
        raise error(422, "PAYER_ONLY", "the bounded counteroffer is drafted by the payer")
    net = loaded.net
    case = NegotiationCase(
        network_id=nid,
        as_of=c.now,
        records=loaded.inputs,
        build={
            "clock": net.clock,
            "availability": net.availability,
            "focal_merchant": c.merchant_id,
            "positions": net.positions,
            "policies": net.policies,
            "labels": net.labels,
        },
        invoice_id=body.obligation_id,
        requester=c.merchant_id,
        counterparty=ob.creditor,
        requester_max_concession=Money(body.max_concession_cents, ob.currency),
    )
    workspace_model = getattr(c.request.app.state, "workspace_llm", None)
    r = propose_counteroffer(workspace_model or UnconfiguredLLMAdapter(), case)
    return {
        "status": r.status,
        "reason_codes": r.reason_codes,
        "allowed_range_cents": list(r.allowed_range) if r.allowed_range else None,
        "suggested_concession_cents": r.allowed_range[0] if r.allowed_range else None,
        "proposal": r.proposal.model_dump() if r.proposal else None,
        "message_issues": [i.code for i in r.message_issues],
        "requester_view": r.requester_view,
        "note": (
            "Deterministic bounds only: the workspace makes no model call (no paid inference "
            "is authorized). Nothing is sent, signed or changed until you submit an offer."
            if workspace_model is None
            else r.note or "Draft only: nothing is sent, signed or changed until you submit."
        ),
        "labels": ["DRAFT_NOT_SENT", "COUNTERPARTY_PRIVATE_RULES_NOT_USED"],
    }


# --- plans and payment authorization -------------------------------------------------------
class PlanBody(_Body):
    network: str | None = Field(default=None, max_length=80)


@router.post("/plans")
def create_plan(body: PlanBody, c: ctx(P.PAYMENT_AUTHORIZE), key: IdemKey = None) -> dict[str, Any]:
    """Explicitly create (or reuse) a durable, independently verified plan for review and
    approval. CSRF + Idempotency-Key + per-merchant rate limit; unchanged economics reuse
    the existing snapshot instead of writing a new immutable record."""
    nid = _network_id(c, body.network)

    def op(conn):
        record, reused = plans.create_snapshot(
            conn,
            network_id=nid,
            merchant_id=c.merchant_id,
            user_id=c.principal.user_id,
            at=c.now,
        )
        snap = plans.load_snapshot(conn, record["plan_id"], c.merchant_id)
        return {"plan": plans.durable_plan_view(conn, snap, c.merchant_id, c.now), "reused": reused}

    res, replayed = idempotency.run(
        c.engine,
        merchant_id=c.merchant_id,
        user_id=c.principal.user_id,
        key=key,
        route="plans.create",
        body=body.model_dump(mode="json"),
        at=c.now,
        operation=op,
    )
    if replayed:  # statuses may have changed since: serve the current view of the same plan
        with c.engine.connect() as conn:
            snap = plans.load_snapshot(conn, res["plan"]["plan_id"], c.merchant_id)
            res = {**res, "plan": plans.durable_plan_view(conn, snap, c.merchant_id, c.now)}
    return {**res, "replayed": replayed}


@router.get("/plans")
def list_plans(c: Read) -> dict[str, Any]:
    with c.engine.connect() as conn:
        return {"plans": plans.list_snapshots(conn, c.merchant_id)}


@router.get("/plans/{plan_id}")
def get_plan(plan_id: str, c: Read) -> dict[str, Any]:
    with c.engine.connect() as conn:
        snap = plans.load_snapshot(conn, plan_id, c.merchant_id)
        return plans.durable_plan_view(conn, snap, c.merchant_id, c.now)


class AuthorizeBody(_Body):
    leg_id: str = Field(min_length=1, max_length=120)
    decision: Literal["approve", "decline"]
    binding_digest: str = Field(min_length=64, max_length=64)


@router.post("/plans/{plan_id}/authorizations")
def authorize_leg(
    plan_id: str,
    body: AuthorizeBody,
    c: ctx(P.PAYMENT_AUTHORIZE, step_up=True),
    key: IdemKey = None,
) -> dict[str, Any]:
    def op(conn):
        return plans.authorize_leg(
            conn,
            merchant_id=c.merchant_id,
            user_id=c.principal.user_id,
            session_id=c.principal.session_id,
            plan_id=plan_id,
            leg_id=body.leg_id,
            decision=body.decision,
            binding_digest=body.binding_digest,
            at=c.now,
            operation_key=key or "",
        )

    res, replayed = idempotency.run(
        c.engine,
        merchant_id=c.merchant_id,
        user_id=c.principal.user_id,
        key=key,
        route=f"plans.authorize:{plan_id}",
        body=body.model_dump(mode="json"),
        at=c.now,
        operation=op,
    )
    return {**res, "replayed": replayed}


class IntentBody(_Body):
    authorization_id: str = Field(min_length=1, max_length=40)


@router.post("/payment-intents")
def create_intent(
    body: IntentBody, c: ctx(P.PAYMENT_AUTHORIZE, step_up=True), key: IdemKey = None
) -> dict[str, Any]:
    if not key:
        raise error(422, "IDEMPOTENCY_KEY_REQUIRED", "an Idempotency-Key header is required")
    with c.engine.begin() as conn:
        return payments.create_payment_intent(
            conn,
            merchant_id=c.merchant_id,
            user_id=c.principal.user_id,
            authorization_id=body.authorization_id,
            operation_key=key,
            at=c.now,
            execution_enabled=_execution_enabled(c),
            allow_synthetic_authority=synthetic_authority_allowed(),
        )


def synthetic_authority_allowed() -> bool:
    """Synthetic evidence may back a (held, never-executable) intent only outside
    production; in production it can never reach a payment intent at all."""
    return get_settings().app_env != "production"


def _execution_enabled(c: MerchantCtx) -> bool:
    override = getattr(c.request.app.state, "payments_execution_enabled", None)
    return bool(get_settings().payments_execution_enabled if override is None else override)


@router.get("/payment-intents")
def list_intents(c: Read) -> dict[str, Any]:
    with c.engine.begin() as conn:
        return {"intents": payments.intents_for(conn, c.merchant_id)}


# --- P5 execution (ADR-0012 §8, §9) ---------------------------------------------------------
@router.get("/payment-intents/{intent_id}")
def intent_detail(intent_id: str, c: Read) -> dict[str, Any]:
    with c.engine.connect() as conn:
        return exec_views.intent_detail(conn, c.merchant_id, intent_id)


def _payer_email(c: MerchantCtx) -> str | None:
    override = getattr(c.request.app.state, "payer_email", None)
    if override is not None:
        return override()
    return payer_email_resolver(get_settings())()


@router.get("/payment-intents/{intent_id}/payer-link")
def payer_link(intent_id: str, c: ctx(P.PAYMENT_AUTHORIZE, step_up=True)) -> JSONResponse:
    """The PayPal-hosted payer page, fetched by GET now; never stored or cached."""
    with c.engine.begin() as conn:
        url = exec_views.payer_link(
            conn,
            merchant_id=c.merchant_id,
            user_id=c.principal.user_id,
            intent_id=intent_id,
            provider=getattr(c.request.app.state, "payment_provider", None),
            payer_email=_payer_email(c),
            at=c.now,
        )
    return JSONResponse(
        {
            "intent_id": intent_id,
            "url": url,
            "host": "www.sandbox.paypal.com",
            "note": "PayPal Sandbox hosted page; you approve the payment at PayPal, not in Elide",
        },
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@router.post("/payment-intents/{intent_id}/cancel")
def cancel_intent(
    intent_id: str, c: ctx(P.PAYMENT_AUTHORIZE, step_up=True), key: IdemKey = None
) -> dict[str, Any]:
    if not key:
        raise error(422, "IDEMPOTENCY_KEY_REQUIRED", "an Idempotency-Key header is required")

    def op(conn):
        return exec_views.cancel(
            conn,
            merchant_id=c.merchant_id,
            user_id=c.principal.user_id,
            intent_id=intent_id,
            at=c.now,
        )

    res, replayed = idempotency.run(
        c.engine,
        merchant_id=c.merchant_id,
        user_id=c.principal.user_id,
        key=key,
        route=f"intents.cancel:{intent_id}",
        body={},
        at=c.now,
        operation=op,
    )
    return {**res, "replayed": replayed}


class AttestBody(_Body):
    verified_spendable_cents: int = Field(ge=0, le=10**12)
    protected_reserve_cents: int = Field(ge=0, le=10**12)
    valid_hours: int = Field(default=24, ge=1, le=72)
    included_receipt_ids: list[str] = Field(default_factory=list, max_length=50)
    included_outflow_ids: list[str] = Field(default_factory=list, max_length=50)


@router.post("/capacity-evidence")
def attest_capacity(
    body: AttestBody, c: ctx(P.PAYMENT_AUTHORIZE, step_up=True), key: IdemKey = None
) -> dict[str, Any]:
    if not key:
        raise error(422, "IDEMPOTENCY_KEY_REQUIRED", "an Idempotency-Key header is required")

    def op(conn):
        return exec_views.attest_capacity(
            conn,
            merchant_id=c.merchant_id,
            user_id=c.principal.user_id,
            spendable_cents=body.verified_spendable_cents,
            reserve_cents=body.protected_reserve_cents,
            valid_hours=body.valid_hours,
            included_receipt_ids=body.included_receipt_ids,
            included_outflow_ids=body.included_outflow_ids,
            at=c.now,
        )

    res, replayed = idempotency.run(
        c.engine,
        merchant_id=c.merchant_id,
        user_id=c.principal.user_id,
        key=key,
        route="capacity.attest",
        body=body.model_dump(mode="json"),
        at=c.now,
        operation=op,
    )
    return {**res, "replayed": replayed}


@router.get("/settlements")
def list_settlements(c: Read) -> dict[str, Any]:
    with c.engine.connect() as conn:
        return {"settlements": exec_views.settlements_for(conn, c.merchant_id)}


# --- policy --------------------------------------------------------------------------------
class PolicyDraftBody(_Body):
    text: str = Field(min_length=1, max_length=2000)
    source: Literal["merchant_message", "document_text"] = "merchant_message"
    extraction: PolicyExtraction | None = None  # merchant-edited fields (no model call)


def _policy_context(conn, c: MerchantCtx):
    rows = conn.execute(
        select(obligation.c.debtor, obligation.c.creditor).where(
            or_(obligation.c.debtor == c.merchant_id, obligation.c.creditor == c.merchant_id)
        )
    ).all()
    cps = {r.creditor if r.debtor == c.merchant_id else r.debtor for r in rows}
    names = _names(conn, cps)
    aliases = {m.lower(): m for m in cps} | {n.lower(): m for m, n in names.items()}
    tz = conn.execute(
        select(merchant_org.c.business_tz).where(merchant_org.c.merchant_id == c.merchant_id)
    ).scalar_one()
    nid = (networks_of(conn, c.merchant_id) or [None])[0]
    synthetic = _is_synthetic(conn, nid) if nid else False
    cap = capacity_summary(conn, c.merchant_id, c.now, synthetic=synthetic)
    reserve = cap.get("protected_reserve_cents")
    return policies.merchant_context(conn, c.merchant_id, tz, aliases, reserve)


@router.get("/policy")
def get_policy(c: Read) -> dict[str, Any]:
    with c.engine.begin() as conn:
        return policies.view(conn, c.merchant_id)


@router.post("/policy/drafts")
def create_policy_draft(body: PolicyDraftBody, c: ctx(P.POLICY_EDIT)) -> dict[str, Any]:
    with c.engine.begin() as conn:
        context = _policy_context(conn, c)
        if body.extraction is not None:
            report = validate_policy(
                merchant_id=c.merchant_id,
                text=body.text,
                source=body.source,
                as_of=c.now,
                extraction=body.extraction,
                context=context,
            )
            draft = PolicyDraft(
                "pending",
                c.merchant_id,
                body.source,
                body.text,
                c.now,
                "VALIDATED",
                body.extraction,
                None,
                report,
            )
        else:
            adapter = getattr(c.request.app.state, "workspace_llm", None)
            draft = draft_policy(
                adapter or UnconfiguredLLMAdapter(),
                merchant_id=c.merchant_id,
                text=body.text,
                source=body.source,
                as_of=c.now,
                context=context,
            )
        draft_id = policies.save_draft(
            conn, draft, user_id=c.principal.user_id, session_id=c.principal.session_id, at=c.now
        )
        audit.append(
            conn,
            at=c.now,
            action="policy.drafted",
            object_type="policy_draft",
            object_id=draft_id,
            actor_user_id=c.principal.user_id,
            session_id=c.principal.session_id,
            merchant_id=c.merchant_id,
            detail={"status": draft.status, "model_used": draft.model_call is not None},
        )
        row = (
            conn.execute(
                select(policies.policy_draft).where(policies.policy_draft.c.draft_id == draft_id)
            )
            .mappings()
            .one()
        )
        return policies.draft_json(row)


class ConfirmBody(_Body):
    digest: str = Field(min_length=64, max_length=64)


@router.post("/policy/drafts/{draft_id}/confirm")
def confirm_policy(
    draft_id: str, body: ConfirmBody, c: ctx(P.POLICY_EDIT, step_up=True)
) -> dict[str, Any]:
    with c.engine.begin() as conn:
        _relock(conn, c, P.POLICY_EDIT)
        prev = policies.latest(conn, c.merchant_id)
        stored = policies.confirm(
            conn,
            merchant_id=c.merchant_id,
            draft_id=draft_id,
            digest=body.digest,
            user_id=c.principal.user_id,
            session_id=c.principal.session_id,
            at=c.now,
            planning_assumptions=prev.planning_assumptions if prev else None,
        )
        audit.append(
            conn,
            at=c.now,
            action="policy.confirmed",
            object_type="policy_revision",
            object_id=f"{c.merchant_id}:{stored.confirmed.version}",
            actor_user_id=c.principal.user_id,
            session_id=c.principal.session_id,
            merchant_id=c.merchant_id,
            detail={"digest": body.digest, "draft_id": draft_id},
        )
        return policies.view(conn, c.merchant_id)


# --- audit and members ---------------------------------------------------------------------
@router.get("/audit")
def audit_trail(c: ctx(P.AUDIT_READ), limit: int = 100) -> dict[str, Any]:
    with c.engine.begin() as conn:
        return {"events": audit.for_merchant(conn, c.merchant_id, max(1, min(limit, 500)))}


@router.get("/members")
def members(c: ctx(P.MEMBERS_MANAGE)) -> dict[str, Any]:
    with c.engine.begin() as conn:
        rows = conn.execute(
            select(
                membership.c.user_id,
                membership.c.role,
                membership.c.granted_at,
                membership.c.revoked_at,
                app_user.c.display_name,
                app_user.c.is_test_identity,
            )
            .join(app_user, app_user.c.user_id == membership.c.user_id)
            .where(membership.c.merchant_id == c.merchant_id)
            .order_by(membership.c.granted_at)
        ).mappings()
        return {
            "members": [
                {
                    "user_id": r["user_id"],
                    "display_name": r["display_name"],
                    "role": r["role"],
                    "is_test_identity": r["is_test_identity"],
                    "granted_at": r["granted_at"].isoformat(),
                    "revoked_at": iso(r["revoked_at"]),
                }
                for r in rows
            ]
        }


class RevokeBody(_Body):
    reason: str = Field(min_length=3, max_length=200)


@router.post("/members/{user_id}/revoke")
def revoke(user_id: str, body: RevokeBody, c: ctx(P.MEMBERS_MANAGE, step_up=True)) -> dict:
    with c.engine.begin() as conn:
        ok = auth_service.revoke_membership(
            conn,
            user_id,
            c.merchant_id,
            at=c.now,
            revoked_by=c.principal.user_id,
            reason=body.reason,
        )
    if not ok:
        raise error(404, "NOT_FOUND", "not found")
    return {"revoked": True, "effective": "immediately, on every session of that user"}
