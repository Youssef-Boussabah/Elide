"""Opportunity preview, durable plan snapshots and condition-bound payment authorization.

- `preview`: READ-ONLY. Builds the permissioned network from PostgreSQL, runs the P3
  analyzer (P2 planner inside) and returns allowlisted, disclosure-checked views. Writes
  nothing.
- `create_snapshot`: the explicit, durable step (POST). Re-plans, checks the plan with the
  independent replay verifier, and stores it only if no unexpired snapshot with the same
  economic inputs exists for this initiator (reuse instead of a new immutable row);
  new snapshots are rate-limited per merchant.
- `authorize_leg`: the payer's own decision on its own leg of a durable plan whose
  economic inputs are still current, re-checked by the independent verifier, under a
  share lock on the actor's membership, and bound to the material conditions
  (`conditions.payment_conditions`) it was given under.
- `existing_authorization` / `authorization_status`: an approval is reusable only for the
  identical leg terms AND identical material conditions AND while unexpired.
- `verify_leg_now`: the independent verifier on the single leg against current data.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, func, insert, select

from api.app.auth import service as auth_service
from api.app.auth.permissions import Permission
from api.opportunities.analysis import analyze
from api.planning.plan import verify_plan_record
from api.planning.solver import ScheduledLeg
from api.planning.verifier import verify
from api.workflow import audit, conditions, disclosure
from api.workflow.networks import LoadedNetwork, load_network
from api.workflow.store import ConflictError, NotVisibleError, WorkflowError, active_participants
from api.workflow.tables import payment_authorization, plan_snapshot

AUTH_TTL = timedelta(hours=24)
REUSE_WINDOW = AUTH_TTL  # a snapshot older than this is never reused (its approvals expire)
NEW_SNAPSHOTS_PER_HOUR = 30


class RateLimitedError(WorkflowError):
    status, code = 429, "RATE_LIMITED"


def economic_digest(loaded: LoadedNetwork) -> str:
    """Digest of the planning inputs without the clock (as_of, slot starts): unchanged
    economics -> the same digest, so re-planning reuses one durable snapshot."""
    canon = loaded.net.to_canonical()
    for k in ("as_of", "slots", "labels"):
        canon.pop(k, None)
    canon["in_execution"] = sorted(loaded.in_execution)
    blob = json.dumps(canon, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _analyze(conn: Connection, network_id: str, merchant_id: str, at: datetime):
    loaded = load_network(conn, network_id, initiator=merchant_id, as_of=at)
    report = analyze(loaded.net)
    record = report["_plan"]
    if verify_plan_record(loaded.net, record):  # never show or store an unverified plan
        raise WorkflowError("plan failed independent verification", code="UNVERIFIED", status=500)
    return report, record, loaded


def preview(conn: Connection, *, network_id: str, merchant_id: str, at: datetime):
    """Read-only recommendations: (opportunity view, plan preview, disclosure)."""
    report, record, loaded = _analyze(conn, network_id, merchant_id, at)
    d = disclosure.decide(merchant_id, record["focal_merchant"], loaded.share_aggregates)
    opp = disclosure.opportunity_view(report, merchant_id, d)
    if loaded.policy_versions.get(merchant_id) is None:  # thresholds unknown: never assume
        opp["my_flags"].append(
            {
                "code": "POLICY_NOT_CONFIRMED",
                "merchant_id": merchant_id,
                "detail": "you have no confirmed policy: thresholds are unknown, so no change "
                "is assumed acceptable to you",
            }
        )
        if opp["recommendation"]["action"] in ("RECOMMEND_PLAN", "REVIEW_AND_DECIDE"):
            opp["recommendation"] = {
                "action": "REVIEW_ECONOMICS",
                "reason": "confirm your policy first; " + opp["recommendation"]["reason"],
            }
    plan_preview = disclosure.plan_view(record, merchant_id, d)
    plan_preview["durable"] = False
    return opp, plan_preview, d


def create_snapshot(
    conn: Connection, *, network_id: str, merchant_id: str, user_id: str, at: datetime
) -> tuple[dict[str, Any], bool]:
    """(plan record, reused). Serialized per initiator so concurrent requests reuse."""
    conn.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(f"plan:{merchant_id}", 2)))
    )
    _report, record, loaded = _analyze(conn, network_id, merchant_id, at)
    econ = economic_digest(loaded)
    ps = plan_snapshot.c
    prior = conn.execute(
        select(ps.record)
        .where(
            ps.network_id == network_id,
            ps.initiator_merchant == merchant_id,
            ps.economic_digest == econ,
            ps.created_at > at - REUSE_WINDOW,
        )
        .order_by(ps.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if prior is not None:
        return prior, True
    recent = conn.execute(
        select(func.count())
        .select_from(plan_snapshot)
        .where(ps.initiator_merchant == merchant_id, ps.created_at > at - timedelta(hours=1))
    ).scalar_one()
    if recent >= NEW_SNAPSHOTS_PER_HOUR:
        raise RateLimitedError("too many new plans in the last hour; reuse the current one")
    stored = json.loads(json.dumps(record, default=str))
    conn.execute(
        insert(plan_snapshot).values(
            plan_id=record["plan_id"],
            network_id=network_id,
            initiator_merchant=merchant_id,
            created_by=user_id,
            created_at=at,
            as_of=loaded.net.as_of,
            expires_at=datetime.fromisoformat(record["expires_at"])
            if record.get("expires_at")
            else None,
            input_digest=record["input_digest"],
            network_digest=loaded.net.digest(),
            economic_digest=econ,
            record=stored,
            verifier_issues=[],
            solver_status=record["solver"]["status"],
        )
    )
    audit.append(
        conn,
        at=at,
        action="plan.snapshot_created",
        object_type="plan",
        object_id=record["plan_id"],
        actor_user_id=user_id,
        merchant_id=merchant_id,
        detail={"economic_digest": econ, "solver_status": record["solver"]["status"]},
    )
    return stored, False


def load_snapshot(conn: Connection, plan_id: str, merchant_id: str) -> dict[str, Any]:
    r = (
        conn.execute(select(*ps_cols()).where(plan_snapshot.c.plan_id == plan_id))
        .mappings()
        .one_or_none()
    )
    if r is None:
        raise NotVisibleError("not found")
    record = r["record"]
    involved = {x["payer"] for x in record["selected_legs"]} | {
        x["payee"] for x in record["selected_legs"]
    }
    if merchant_id != record["focal_merchant"] and merchant_id not in involved:
        raise NotVisibleError("not found")
    return {
        "record": record,
        "network_id": r["network_id"],
        "economic_digest": r["economic_digest"],
        "created_at": r["created_at"],
    }


def ps_cols():
    ps = plan_snapshot.c
    return [ps.record, ps.network_id, ps.economic_digest, ps.created_at]


def list_snapshots(conn: Connection, merchant_id: str, limit: int = 10) -> list[dict[str, Any]]:
    ps = plan_snapshot.c
    rows = conn.execute(
        select(ps.plan_id, ps.created_at, ps.as_of, ps.solver_status)
        .where(ps.initiator_merchant == merchant_id)
        .order_by(ps.created_at.desc())
        .limit(limit)
    ).mappings()
    return [
        {
            "plan_id": r["plan_id"],
            "created_at": r["created_at"].isoformat(),
            "as_of": r["as_of"].isoformat(),
            "solver_status": r["solver_status"],
        }
        for r in rows
    ]


# --- authorization ---------------------------------------------------------------------------
def _binding(record: dict[str, Any], leg: dict[str, Any], fee_ceiling: int, expires: datetime):
    blob = {
        "schema": "elide.payment-authorization.v1",
        "plan_id": record["plan_id"],
        "input_digest": record["input_digest"],
        "option_id": leg["leg_id"],
        "obligation_id": leg["obligation_id"],
        "revision_no": leg["revision_no"],
        "payer": leg["payer"],
        "payee": leg["payee"],
        "amount_cents": leg["amount_cents"],
        "fee_ceiling_cents": fee_ceiling,
        "currency": record["currency"],
        "expires_at": expires.isoformat(),
    }
    return hashlib.sha256(json.dumps(blob, sort_keys=True).encode()).hexdigest()


def leg_terms(record: dict[str, Any], leg: dict[str, Any]) -> dict[str, Any]:
    fee_ceiling = int(leg["payer_fee_cents"])
    expires = datetime.fromisoformat(record["as_of"]) + AUTH_TTL
    if record.get("expires_at"):
        expires = min(expires, datetime.fromisoformat(record["expires_at"]))
    return {
        "fee_ceiling_cents": fee_ceiling,
        "expires_at": expires,
        "binding_digest": _binding(record, leg, fee_ceiling, expires),
    }


def _leg_match(leg: dict[str, Any]):
    pa = payment_authorization.c
    return (
        pa.option_id == leg["leg_id"],
        pa.obligation_id == leg["obligation_id"],
        pa.revision_no == leg["revision_no"],
        pa.payer_merchant == leg["payer"],
        pa.payee_merchant == leg["payee"],
        pa.amount_cents == leg["amount_cents"],
        pa.fee_ceiling_cents == int(leg["payer_fee_cents"]),
    )


def existing_authorization(
    conn: Connection, leg: dict[str, Any], conditions_digest: str, at: datetime
):
    """The latest decision that is still valid for these exact leg terms AND these exact
    material conditions, or None. Equal amounts alone are never enough."""
    pa = payment_authorization.c
    return conn.execute(
        select(payment_authorization)
        .where(*_leg_match(leg), pa.conditions_digest == conditions_digest, pa.expires_at > at)
        .order_by(pa.decided_at.desc())
        .limit(1)
    ).first()


def authorization_status(
    conn: Connection, leg: dict[str, Any], conditions_digest: str | None, at: datetime
) -> dict[str, Any]:
    pa = payment_authorization.c
    latest = conn.execute(
        select(payment_authorization)
        .where(*_leg_match(leg))
        .order_by(pa.decided_at.desc())
        .limit(1)
    ).first()
    if latest is None:
        return {"status": "pending"}
    if latest.expires_at <= at:
        return {"status": "expired", "expired_at": latest.expires_at.isoformat()}
    if conditions_digest is None or latest.conditions_digest != conditions_digest:
        return {
            "status": "needs_reapproval",
            "reason": "the conditions this decision was "
            "given under have changed (policy, reserve, terms, fees, participation or "
            "evidence basis); a new decision is required",
        }
    return {
        "status": latest.decision + "d",
        "authorization_id": latest.authorization_id,
        "decided_at": latest.decided_at.isoformat(),
        "expires_at": latest.expires_at.isoformat(),
    }


def verify_leg_now(
    loaded: LoadedNetwork,
    leg: dict[str, Any],
    at: datetime,
    record: dict[str, Any] | None = None,
) -> list[str]:
    """Independent replay against CURRENT data: reserve (incl. the confirmed policy floor)
    after amount + fee, commitments, evidence validity, fees and ceilings, payer rules,
    consent and timing. With `record`, the leg is replayed together with the upstream legs
    it depends on (conditional consent to a downstream payment); without it, alone (what an
    intent needs: no payment may rely on predicted incoming cash)."""
    net = loaded.net
    by_id = {x["leg_id"]: x for x in (record or {}).get("selected_legs", [])}
    chain, todo = [], [leg]
    while todo:
        x = todo.pop()
        if any(c["leg_id"] == x["leg_id"] for c in chain):
            continue
        chain.append(x)
        todo.extend(by_id[d] for d in x.get("depends_on", []) if d in by_id)
    schedule = []
    for x in chain:
        if not net.has_option(x["leg_id"]):
            return [f"STALE_PLAN: the terms of {x['obligation_id']} changed since this plan"]
        o = net.option(x["leg_id"])
        if (o.payer, o.payee, o.cash.cents) != (x["payer"], x["payee"], x["amount_cents"]):
            return ["STALE_PLAN: payer, payee or amount differs from the current terms"]
        # Keep the plan's timing on the current clock: an on-due-date leg is checked at
        # the invoice's current due slot; an accelerated leg at the slot of its planned
        # date (never in the past).
        if x.get("accelerated"):
            target = max(at, datetime.fromisoformat(x["slot_start"]))
        else:
            target = max(at, net.obligation(x["obligation_id"]).due_at)
        slot = net.clock.last_slot_at_or_before(target)
        schedule.append(ScheduledLeg(x["leg_id"], 0 if slot is None else slot))
    rep = verify(net, schedule)
    return [f"{v.code}: {v.detail}" for v in rep.violations]


def _lock_actor(conn: Connection, user_id: str, merchant_id: str) -> None:
    try:
        auth_service.lock_membership(conn, user_id, merchant_id, Permission.PAYMENT_AUTHORIZE)
    except auth_service.NotFoundError:
        raise NotVisibleError("not found") from None
    except auth_service.ForbiddenError as exc:
        raise WorkflowError(str(exc), code="FORBIDDEN", status=403) from None


def authorize_leg(
    conn: Connection,
    *,
    merchant_id: str,
    user_id: str,
    session_id: str,
    plan_id: str,
    leg_id: str,
    decision: str,
    binding_digest: str,
    at: datetime,
    operation_key: str,
) -> dict[str, Any]:
    _lock_actor(conn, user_id, merchant_id)  # revocation racing this write wins
    snap = load_snapshot(conn, plan_id, merchant_id)
    record = snap["record"]
    leg = next((x for x in record["selected_legs"] if x["leg_id"] == leg_id), None)
    if leg is None:
        raise NotVisibleError("not found")
    if leg["payer"] != merchant_id:
        if merchant_id == leg["payee"]:
            raise ConflictError("only the payer can authorize its own payment", code="NOT_PAYER")
        raise NotVisibleError("not found")
    if decision not in ("approve", "decline"):
        raise WorkflowError("decision must be approve or decline", status=422)
    terms = leg_terms(record, leg)
    if terms["binding_digest"] != binding_digest:
        raise ConflictError("the reviewed terms are stale; reload the plan", code="STALE_DIGEST")
    if at >= terms["expires_at"]:
        raise ConflictError("this plan can no longer be authorized; re-plan", code="PLAN_EXPIRED")
    loaded = load_network(conn, snap["network_id"], initiator=record["focal_merchant"], as_of=at)
    if economic_digest(loaded) != snap["economic_digest"]:
        raise ConflictError(
            "the economic inputs changed since this plan was created; create a new plan",
            code="STALE_PLAN",
        )
    cond = conditions.payment_conditions(conn, loaded, leg, at)
    if cond is None:
        raise ConflictError("the invoice is no longer in the network", code="STALE_PLAN")
    cdig = conditions.digest(cond)
    if existing_authorization(conn, leg, cdig, at) is not None:
        raise ConflictError(
            "this leg already has a valid recorded decision", code="ALREADY_DECIDED"
        )
    if decision == "approve":
        if leg["terms"] != "in_force":
            raise ConflictError(
                "this leg relies on an offer or proposal that is not yet an agreed amendment; "
                "agree it in Negotiations, then re-plan",
                code="TERMS_NOT_AGREED",
            )
        issues = verify_leg_now(loaded, leg, at, record)
        if issues:
            raise ConflictError("; ".join(issues), code="VERIFIER_REJECTED")
    auth_id = "pau_" + secrets.token_hex(8)
    conn.execute(
        insert(payment_authorization).values(
            authorization_id=auth_id,
            plan_id=plan_id,
            option_id=leg_id,
            leg_id=leg_id,
            obligation_id=leg["obligation_id"],
            revision_no=leg["revision_no"],
            payer_merchant=leg["payer"],
            payee_merchant=leg["payee"],
            amount_cents=leg["amount_cents"],
            fee_ceiling_cents=terms["fee_ceiling_cents"],
            currency=record["currency"],
            plan_input_digest=record["input_digest"],
            binding_digest=binding_digest,
            decision=decision,
            expires_at=terms["expires_at"],
            user_id=user_id,
            session_id=session_id,
            decided_at=at,
            operation_key=operation_key,
            conditions=cond,
            conditions_digest=cdig,
        )
    )
    audit.append(
        conn,
        at=at,
        action=f"payment.{decision}d",
        object_type="payment_authorization",
        object_id=auth_id,
        actor_user_id=user_id,
        session_id=session_id,
        merchant_id=merchant_id,
        detail={
            "plan_id": plan_id,
            "leg_id": leg_id,
            "payee": leg["payee"],
            "amount_cents": leg["amount_cents"],
            "binding_digest": binding_digest,
            "conditions_digest": cdig,
            "money_moved": False,
        },
    )
    return {"authorization_id": auth_id, "decision": decision, "money_moved": False}


def durable_plan_view(conn: Connection, snap: dict[str, Any], merchant_id: str, at: datetime):
    """Allowlisted view of a durable plan, with each leg's CURRENT authorization status."""
    record = snap["record"]
    share = active_participants(conn, snap["network_id"])
    d = disclosure.decide(merchant_id, record["focal_merchant"], share)
    view = disclosure.plan_view(record, merchant_id, d)
    view["durable"] = True
    by_leg = {x["leg_id"]: x for x in record["selected_legs"]}
    loaded = None
    for item in view["my_legs"]:
        full = by_leg[item["leg_id"]]
        try:
            if loaded is None:
                loaded = load_network(
                    conn, snap["network_id"], initiator=record["focal_merchant"], as_of=at
                )
            cond = conditions.payment_conditions(conn, loaded, full, at)
        except NotVisibleError:
            cond = None
        cdig = conditions.digest(cond) if cond is not None else None
        status = authorization_status(conn, full, cdig, at)
        if full["payer"] != merchant_id:  # the payee sees only valid / not yet
            status = {"status": "approved" if status["status"] == "approved" else "pending"}
        item["payment_authorization"] = status
        if full["payer"] == merchant_id and status["status"] in (
            "pending",
            "expired",
            "needs_reapproval",
        ):
            t = leg_terms(record, full)
            item["authorize"] = {
                "binding_digest": t["binding_digest"],
                "fee_ceiling_cents": t["fee_ceiling_cents"],
                "expires_at": t["expires_at"].isoformat(),
                "terms_agreed": full["terms"] == "in_force",
                "plan_current": loaded is not None
                and economic_digest(loaded) == snap["economic_digest"],
            }
    return view
