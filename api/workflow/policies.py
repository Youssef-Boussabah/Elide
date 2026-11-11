"""Persisted policy drafts and confirmed revisions (P3 policy service, now authenticated).

A draft is never active. Only `confirm` (same merchant, READY draft, the exact digest the
merchant reviewed, authenticated user with `policy:edit` and a recent step-up) creates a
`policy_revision`; the newest revision is the merchant's policy.

`planning_assumptions` (e.g. an opportunity-cost hurdle) are merchant-declared modelling
inputs that the P3 schema does not extract; in P4 only the synthetic seed sets them, and
they are labelled with their source wherever they are used.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, func, insert, select, text

from api.policy.service import ConfirmedPolicy, PolicyDraft
from api.policy.validation import CurrentPolicy, MerchantContext, ProposedPolicy, Status
from api.workflow.store import ConflictError, NotVisibleError
from api.workflow.tables import policy_draft, policy_revision


@dataclass(frozen=True, slots=True)
class StoredPolicy:
    confirmed: ConfirmedPolicy
    planning_assumptions: dict[str, Any]
    confirmed_by: str


def report_json(draft: PolicyDraft) -> dict[str, Any]:
    rep = draft.report
    if rep is None:
        return {"status": draft.outcome, "issues": [], "changes": [], "explanation": draft.message}
    return {
        "status": rep.status.value,
        "issues": [asdict(i) for i in rep.issues],
        "changes": json.loads(json.dumps(list(rep.changes), default=str)),
        "explanation": rep.explanation,
    }


def proposed_from_json(d: dict[str, Any]) -> ProposedPolicy:
    return ProposedPolicy(
        merchant_id=d["merchant_id"],
        reserve_floor_cents=d["reserve_floor_cents"],
        max_early_commitment_cents=d["max_early_commitment_cents"],
        min_early_discount_cents=d["min_early_discount_cents"],
        min_net_benefit_cents=d["min_net_benefit_cents"],
        fee_ceiling_cents=d["fee_ceiling_cents"],
        allowed=tuple(d["allowed"]),
        prohibited=tuple(d["prohibited"]),
        valid_until=d["valid_until"],
        offer_response_deadline=d["offer_response_deadline"],
        participation=d["participation"],
        approval_preference=d["approval_preference"],
        spans={k: tuple(v) for k, v in d["spans"].items()},
    )


def save_draft(
    conn: Connection, draft: PolicyDraft, *, user_id: str, session_id: str, at: datetime
) -> str:
    draft_id = "pdr_" + secrets.token_hex(8)
    rep = draft.report
    ready = rep is not None and rep.status is Status.READY_FOR_CONFIRMATION
    conn.execute(
        insert(policy_draft).values(
            draft_id=draft_id,
            merchant_id=draft.merchant_id,
            created_by=user_id,
            session_id=session_id,
            created_at=at,
            source=draft.source,
            source_text=draft.text,
            status=draft.status,
            report=report_json(draft),
            proposed=asdict(rep.proposed) if ready and rep.proposed else None,
            digest=rep.digest if ready else None,
            model_call=draft.model_call.as_dict() if draft.model_call else None,
        )
    )
    return draft_id


def latest(conn: Connection, merchant_id: str) -> StoredPolicy | None:
    r = (
        conn.execute(
            select(policy_revision)
            .where(policy_revision.c.merchant_id == merchant_id)
            .order_by(policy_revision.c.revision_no.desc())
            .limit(1)
        )
        .mappings()
        .one_or_none()
    )
    if r is None:
        return None
    rules = dict(r["rules"])
    assumptions = rules.pop("planning_assumptions", {}) or {}
    return StoredPolicy(
        ConfirmedPolicy(
            merchant_id=merchant_id,
            version=r["revision_no"],
            policy=proposed_from_json(rules),
            digest=r["digest"],
            draft_id=r["draft_id"],
            confirmed_at=r["confirmed_at"],
            basis="authenticated_merchant_confirmation",
        ),
        assumptions,
        r["confirmed_by"],
    )


def confirm(
    conn: Connection,
    *,
    merchant_id: str,
    draft_id: str,
    digest: str,
    user_id: str,
    session_id: str,
    at: datetime,
    planning_assumptions: dict[str, Any] | None = None,
) -> StoredPolicy:
    conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
        {"k": f"policy:{merchant_id}"},
    )
    d = (
        conn.execute(select(policy_draft).where(policy_draft.c.draft_id == draft_id))
        .mappings()
        .one_or_none()
    )
    if d is None or d["merchant_id"] != merchant_id:
        raise NotVisibleError("not found")
    if d["status"] != Status.READY_FOR_CONFIRMATION.value or d["digest"] is None:
        raise ConflictError(
            f"draft is {d['status']}; only a READY draft can be confirmed", code="DRAFT_NOT_READY"
        )
    if d["digest"] != digest:
        raise ConflictError("digest does not match the reviewed draft", code="STALE_DIGEST")
    already = conn.execute(
        select(policy_revision.c.revision_no).where(policy_revision.c.draft_id == draft_id)
    ).scalar_one_or_none()
    if already is not None:
        raise ConflictError("this draft was already confirmed", code="ALREADY_CONFIRMED")
    n = conn.execute(
        select(func.coalesce(func.max(policy_revision.c.revision_no), 0)).where(
            policy_revision.c.merchant_id == merchant_id
        )
    ).scalar_one()
    rules = dict(d["proposed"])
    if planning_assumptions:
        rules["planning_assumptions"] = planning_assumptions
    conn.execute(
        insert(policy_revision).values(
            merchant_id=merchant_id,
            revision_no=n + 1,
            draft_id=draft_id,
            rules=rules,
            digest=digest,
            confirmed_by=user_id,
            session_id=session_id,
            confirmed_at=at,
        )
    )
    stored = latest(conn, merchant_id)
    if stored is None:  # pragma: no cover - just inserted in this transaction
        raise ConflictError("policy revision not readable after insert")
    return stored


def current_policy(stored: StoredPolicy | None) -> CurrentPolicy:
    if stored is None:
        return CurrentPolicy()
    p = stored.confirmed.policy
    return CurrentPolicy(
        reserve_floor_cents=p.reserve_floor_cents,
        max_early_commitment_cents=p.max_early_commitment_cents,
        min_early_discount_cents=p.min_early_discount_cents,
        min_net_benefit_cents=p.min_net_benefit_cents,
        fee_ceiling_cents=p.fee_ceiling_cents,
        allowed=p.allowed,
        prohibited=p.prohibited,
        participation=p.participation,
        approval_preference=p.approval_preference,
    )


def merchant_context(
    conn: Connection,
    merchant_id: str,
    business_tz: str,
    counterparties: dict[str, str],
    reserve_cents: int | None,
) -> MerchantContext:
    return MerchantContext(
        merchant_id=merchant_id,
        business_tz=business_tz,
        counterparties=counterparties,
        current=current_policy(latest(conn, merchant_id)),
        evidence_reserve_cents=reserve_cents,
    )


def view(conn: Connection, merchant_id: str) -> dict[str, Any]:
    s = latest(conn, merchant_id)
    drafts = conn.execute(
        select(policy_draft)
        .where(policy_draft.c.merchant_id == merchant_id)
        .order_by(policy_draft.c.created_at.desc())
        .limit(10)
    ).mappings()
    return {
        "confirmed": None
        if s is None
        else {
            "revision_no": s.confirmed.version,
            "digest": s.confirmed.digest,
            "confirmed_at": s.confirmed.confirmed_at.isoformat(),
            "confirmed_by": s.confirmed_by,
            "rules": asdict(s.confirmed.policy),
            "planning_assumptions": s.planning_assumptions,
        },
        "drafts": [draft_json(d) for d in drafts],
    }


def draft_json(d: Any) -> dict[str, Any]:
    return {
        "draft_id": d["draft_id"],
        "created_at": d["created_at"].isoformat(),
        "source": d["source"],
        "text": d["source_text"],
        "status": d["status"],
        "report": d["report"],
        "proposed": d["proposed"],
        "digest": d["digest"],
        "model_call": d["model_call"],
        "active": False,
    }
