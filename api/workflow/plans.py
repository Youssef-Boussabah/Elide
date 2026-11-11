"""Opportunity analysis, immutable plan snapshots and payment-leg authorization.

- `analyze_for`: builds the permissioned network from PostgreSQL for the initiating
  merchant, runs the P3 analyzer (which runs the P2 planner), checks the plan with the
  independent P2 replay verifier, stores the snapshot (only verified plans are stored,
  also enforced by a CHECK) and returns the merchant-scoped views only.
- `authorize_leg`: the payer's own approval of its own leg, bound to the plan, option,
  revision, payee, amount, fee ceiling, currency and expiry (`binding_digest`).
  Amendment terms must already be agreed (an open offer or unsigned proposal cannot be
  payment-authorized), and the leg is re-validated against a freshly loaded network.
  A recorded authorization does not move money; see `payments.create_payment_intent`.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from api.opportunities.analysis import analyze, merchant_view
from api.planning import plan as plan_mod
from api.planning.plan import verify_plan_record
from api.workflow import audit
from api.workflow.networks import LoadedNetwork, load_network
from api.workflow.store import ConflictError, NotVisibleError, WorkflowError
from api.workflow.tables import payment_authorization, plan_snapshot

AUTH_TTL = timedelta(hours=24)
MIN_GROUP = 2  # small-group suppression for aggregates shown to the initiator (doc 11)


def analyze_for(
    conn: Connection, *, network_id: str, merchant_id: str, user_id: str, at: datetime
) -> tuple[dict[str, Any], dict[str, Any], LoadedNetwork]:
    loaded = load_network(conn, network_id, initiator=merchant_id, as_of=at)
    report = analyze(loaded.net)
    record = report["_plan"]
    issues = verify_plan_record(loaded.net, record)
    if issues:  # never store or show an unverified plan
        raise WorkflowError("plan failed independent verification", code="UNVERIFIED", status=500)
    conn.execute(
        pg_insert(plan_snapshot)
        .values(
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
            record=json.loads(json.dumps(record, default=str)),
            verifier_issues=[],
            solver_status=record["solver"]["status"],
        )
        .on_conflict_do_nothing(index_elements=["plan_id"])
    )
    return report, record, loaded


def suppress_aggregates(view: dict[str, Any], report: dict[str, Any], loaded: LoadedNetwork):
    """Initiator aggregates need consent from every other contributing payer and at least
    MIN_GROUP of them (doc 11: sparse-network totals can reveal hidden relations)."""
    if "comparison" not in view:
        return view
    me = view["viewer"]
    contributors = {x["payer"] for x in report["plan_legs"] if x["payer"] != me}
    contributors |= {x["payee"] for x in report["plan_legs"] if x["payer"] != me} - {me}
    refused = sorted(m for m in contributors if not loaded.share_aggregates.get(m, False))
    reason = None
    if refused:
        reason = "WITHHELD_NO_CONSENT"
    elif 0 < len(contributors) < MIN_GROUP:
        reason = "WITHHELD_SMALL_GROUP"
    if reason:
        for k in ("comparison", "other_bottlenecks_count", "other_legs_count"):
            view.pop(k, None)
        if "decision" in view:
            view["decision"] = {
                k: v for k, v in view["decision"].items() if k not in ("advantage",)
            }
        view["aggregates"] = {
            "status": reason,
            "explanation": "network totals are withheld: other participants have not all "
            "agreed to contribute to aggregates, or too few contribute to keep them private",
        }
    else:
        view["aggregates"] = {"status": "SHOWN", "contributors": len(contributors)}
    return view


def opportunity_view(
    report: dict[str, Any], loaded: LoadedNetwork, merchant_id: str
) -> dict[str, Any]:
    view = merchant_view(report, merchant_id)
    view = suppress_aggregates(view, report, loaded)
    if loaded.policy_versions.get(merchant_id) is None:
        view["my_flags"].append(
            {
                "code": "POLICY_NOT_CONFIRMED",
                "merchant_id": merchant_id,
                "detail": "you have no confirmed policy: thresholds are unknown, so no "
                "change is assumed acceptable to you",
            }
        )
        if view["recommendation"]["action"] in ("RECOMMEND_PLAN", "REVIEW_AND_DECIDE"):
            view["recommendation"] = {
                "action": "REVIEW_ECONOMICS",
                "reason": "confirm your policy first; " + view["recommendation"]["reason"],
            }
    return view


# --- payment authorization -----------------------------------------------------------------
def load_snapshot(conn: Connection, plan_id: str, merchant_id: str) -> dict[str, Any]:
    r = conn.execute(
        select(plan_snapshot.c.record, plan_snapshot.c.network_id).where(
            plan_snapshot.c.plan_id == plan_id
        )
    ).one_or_none()
    if r is None:
        raise NotVisibleError("not found")
    record = r.record
    involved = {x["payer"] for x in record["selected_legs"]} | {
        x["payee"] for x in record["selected_legs"]
    }
    if merchant_id != record["focal_merchant"] and merchant_id not in involved:
        raise NotVisibleError("not found")
    return {"record": record, "network_id": r.network_id}


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
    """Deterministic from the immutable snapshot: the same plan always yields the same
    binding, so the digest a merchant reviewed can be re-derived exactly."""
    fee_ceiling = int(leg["payer_fee_cents"])
    expires = datetime.fromisoformat(record["as_of"]) + AUTH_TTL
    if record.get("expires_at"):
        expires = min(expires, datetime.fromisoformat(record["expires_at"]))
    return {
        "fee_ceiling_cents": fee_ceiling,
        "expires_at": expires,
        "binding_digest": _binding(record, leg, fee_ceiling, expires),
    }


def revalidate_leg(
    conn: Connection, record: dict[str, Any], leg: dict[str, Any], network_id: str, at: datetime
) -> list[str]:
    """Re-derive the leg from current persisted inputs. Any change makes the plan stale."""
    issues = []
    if leg["terms"] != "in_force":
        issues.append(
            "TERMS_NOT_AGREED: this leg relies on an offer or proposal that is not yet an "
            "agreed amendment; agree it in Negotiations, then re-plan"
        )
    if record.get("expires_at") and at >= datetime.fromisoformat(record["expires_at"]):
        issues.append("PLAN_EXPIRED: the plan's evidence window has passed; re-plan")
    fresh = load_network(conn, network_id, initiator=record["focal_merchant"], as_of=at).net
    if not fresh.has_option(leg["leg_id"]):
        issues.append("STALE_PLAN: the invoice terms changed since this plan was computed")
    else:
        o = fresh.option(leg["leg_id"])
        if (o.payer, o.payee, o.cash.cents) != (leg["payer"], leg["payee"], leg["amount_cents"]):
            issues.append("STALE_PLAN: payer, payee or amount differs from the current terms")
    return issues


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
    existing = conn.execute(
        select(payment_authorization.c.decision).where(
            payment_authorization.c.plan_id == plan_id,
            payment_authorization.c.option_id == leg_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError("this leg already has a recorded decision", code="ALREADY_DECIDED")
    # The binding the merchant reviewed must equal the one derivable now.
    terms = leg_terms(record, leg)
    if terms["binding_digest"] != binding_digest:
        raise ConflictError("the reviewed terms are stale; reload the plan", code="STALE_DIGEST")
    if at >= terms["expires_at"]:
        raise ConflictError("this plan can no longer be authorized; re-plan", code="PLAN_EXPIRED")
    if decision == "approve":
        issues = revalidate_leg(conn, record, leg, snap["network_id"], at)
        if issues:
            code = issues[0].split(":", 1)[0]
            raise ConflictError("; ".join(issues), code=code)
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
            "money_moved": False,
        },
    )
    return {"authorization_id": auth_id, "decision": decision, "money_moved": False}


def plan_view(conn: Connection, record: dict[str, Any], merchant_id: str, at: datetime):
    view = plan_mod.merchant_view(record, merchant_id)
    auths = {
        r.option_id: r
        for r in conn.execute(
            select(payment_authorization).where(
                payment_authorization.c.plan_id == record["plan_id"]
            )
        )
    }
    by_leg = {x["leg_id"]: x for x in record["selected_legs"]}
    for leg in view["my_legs"]:
        full = by_leg[leg["leg_id"]]
        a = auths.get(leg["leg_id"])
        mine_to_pay = full["payer"] == merchant_id
        leg["payment_authorization"] = (
            {
                "status": a.decision + "d",
                "authorization_id": a.authorization_id if mine_to_pay else None,
                "decided_at": a.decided_at.isoformat(),
                "expires_at": a.expires_at.isoformat(),
            }
            if a is not None
            else {"status": "pending"}
        )
        if mine_to_pay and a is None:
            t = leg_terms(record, full)
            leg["authorize"] = {
                "binding_digest": t["binding_digest"],
                "fee_ceiling_cents": t["fee_ceiling_cents"],
                "expires_at": t["expires_at"].isoformat(),
                "terms_agreed": full["terms"] == "in_force",
            }
    return view
