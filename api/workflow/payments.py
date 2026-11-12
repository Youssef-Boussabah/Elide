"""Payment intents: the P5 execution boundary, with every P4 precondition enforced.

`create_payment_intent` succeeds only when, inside one transaction:
1. the payer merchant (path scope) owns an `approve` authorization for its own leg;
2. the authorization is unexpired and its approving user still holds an active
   membership with `payment:authorize` in that merchant (revocation invalidates it);
3. the leg re-validates against a freshly loaded network (terms in force and unchanged,
   plan not expired);
4. the leg does not depend on another payment (no downstream payment on predicted
   incoming cash, doc 11 / D006);
5. cash is reserved atomically through the P1 capacity store (current evidence, reserve
   kept after the payment and its fee ceiling; concurrent intents serialize on the
   merchant lock and cannot double-spend);
6. the operation key is new, or replays the identical request.

It then writes `payment_intent` (`awaiting_payer_action`) and a HELD `outbox_message`.
Nothing in P4 dispatches the outbox: `PAYMENTS_EXECUTION_ENABLED` is false and no
worker exists. The model, the browser and the optimizer have no path to this function
except through an authenticated payer request.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, insert, select

from api.app.auth import service as auth_service
from api.app.auth.permissions import Permission
from api.domain.money import USD, Money
from api.liquidity.capacity import CapacityError, Commitment
from api.liquidity.store import _current_evidence, reserve_capacity_in
from api.workflow import audit, conditions
from api.workflow.networks import ACTIVE_INTENT, load_network
from api.workflow.plans import load_snapshot, verify_leg_now
from api.workflow.store import ConflictError, NotVisibleError
from api.workflow.tables import obligation, outbox_message, payment_authorization, payment_intent


def request_hash(body: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def _lock_member(conn: Connection, user_id: str, merchant_id: str, *, as_code: str | None):
    """Share-lock and re-check a membership with payment authority inside this transaction."""
    try:
        auth_service.lock_membership(conn, user_id, merchant_id, Permission.PAYMENT_AUTHORIZE)
    except (auth_service.NotFoundError, auth_service.ForbiddenError):
        if as_code is None:
            raise NotVisibleError("not found") from None
        raise ConflictError(
            "the approving user no longer holds payment authority; authorize again",
            code=as_code,
        ) from None


def create_payment_intent(
    conn: Connection,
    *,
    merchant_id: str,
    user_id: str,
    authorization_id: str,
    operation_key: str,
    at: datetime,
    execution_enabled: bool,
    allow_synthetic_authority: bool = True,
) -> dict[str, Any]:
    # The actor's membership is share-locked until commit: a revocation committing before
    # this transaction is seen; one that arrives later waits (finding 1, revocation race).
    _lock_member(conn, user_id, merchant_id, as_code=None)
    body_hash = request_hash({"authorization_id": authorization_id})
    prior = (
        conn.execute(
            select(payment_intent).where(
                payment_intent.c.payer_merchant == merchant_id,
                payment_intent.c.operation_key == operation_key,
            )
        )
        .mappings()
        .one_or_none()
    )
    if prior is not None:
        if prior["request_sha256"] != body_hash:
            raise ConflictError(
                "operation key reused with a different request", code="IDEMPOTENCY_CONFLICT"
            )
        return {**_intent_json(prior), "replayed": True}

    a = (
        conn.execute(
            select(payment_authorization)
            .where(payment_authorization.c.authorization_id == authorization_id)
            .with_for_update()
        )
        .mappings()
        .one_or_none()
    )
    if a is None or a["payer_merchant"] != merchant_id:
        raise NotVisibleError("not found")
    if a["decision"] != "approve":
        raise ConflictError("this payment was declined", code="NOT_APPROVED")
    if at >= a["expires_at"]:
        raise ConflictError("the payment authorization expired", code="AUTHORIZATION_EXPIRED")
    _lock_member(conn, a["user_id"], merchant_id, as_code="AUTHORIZATION_REVOKED")
    used = conn.execute(
        select(payment_intent.c.intent_id).where(
            payment_intent.c.authorization_id == authorization_id
        )
    ).scalar_one_or_none()
    if used is not None:
        raise ConflictError("an intent already exists for this authorization", code="DUPLICATE")
    # Serialize intents per invoice: one invoice is in execution at most once.
    conn.execute(
        select(obligation.c.obligation_id)
        .where(obligation.c.obligation_id == a["obligation_id"])
        .with_for_update()
    )
    active = conn.execute(
        select(payment_intent.c.intent_id).where(
            payment_intent.c.obligation_id == a["obligation_id"],
            payment_intent.c.status.in_(ACTIVE_INTENT),
        )
    ).scalar_one_or_none()
    if active is not None:
        raise ConflictError(
            "this invoice already has a payment in execution", code="ALREADY_IN_EXECUTION"
        )

    snap = load_snapshot(conn, a["plan_id"], merchant_id)
    record = snap["record"]
    leg = next(x for x in record["selected_legs"] if x["leg_id"] == a["option_id"])
    if leg["depends_on"]:
        raise ConflictError(
            "this payment is funded by an incoming payment that is not provider-confirmed "
            "and verified spendable",
            code="UPSTREAM_NOT_CONFIRMED",
        )
    if leg["terms"] != "in_force":
        raise ConflictError("the leg's terms are not an agreed amendment", code="TERMS_NOT_AGREED")
    # Current, authoritative conditions: an approval never outlives what it consented to.
    loaded = load_network(conn, snap["network_id"], initiator=record["focal_merchant"], as_of=at)
    cond = conditions.payment_conditions(conn, loaded, leg, at)
    if cond is None or conditions.digest(cond) != a["conditions_digest"]:
        raise ConflictError(
            "the conditions this payment was approved under have changed (policy, reserve, "
            "terms, fees, participation or evidence basis); approve it again",
            code="CONDITIONS_CHANGED",
        )
    issues = verify_leg_now(loaded, leg, at)  # independent verifier on current data
    if issues:
        raise ConflictError("; ".join(issues), code="VERIFIER_REJECTED")

    ev = _current_evidence(conn, merchant_id, USD, at)
    if ev is None:
        raise ConflictError("no current capacity evidence", code="INSUFFICIENT_CAPACITY")
    synthetic = ev.kind.value == "synthetic"
    if synthetic and not allow_synthetic_authority:
        raise ConflictError(
            "synthetic evidence can never authorize a payment here", code="SYNTHETIC_EVIDENCE"
        )
    if ev.kind.value not in ("provider_verified", "merchant_attested", "synthetic"):
        raise ConflictError("capacity evidence is not verified", code="INSUFFICIENT_CAPACITY")
    intent_id = "pin_" + secrets.token_hex(8)
    amount = int(a["amount_cents"]) + int(a["fee_ceiling_cents"])
    try:
        reserve_capacity_in(
            conn,
            evidence_id=ev.evidence_id,
            commitment=Commitment(f"pi:{intent_id}", merchant_id, a["plan_id"], Money(amount, USD)),
            at=at,
            allow_synthetic=synthetic,
            min_protected_reserve_cents=conditions.policy_reserve_floor(conn, merchant_id),
        )
    except CapacityError as exc:
        raise ConflictError(str(exc), code="INSUFFICIENT_CAPACITY") from None

    values = {
        "intent_id": intent_id,
        "authorization_id": authorization_id,
        "plan_id": a["plan_id"],
        "payer_merchant": merchant_id,
        "payee_merchant": a["payee_merchant"],
        "obligation_id": a["obligation_id"],
        "revision_no": a["revision_no"],
        "amount_cents": a["amount_cents"],
        "fee_ceiling_cents": a["fee_ceiling_cents"],
        "currency": a["currency"],
        "operation_key": operation_key,
        "request_sha256": body_hash,
        "commitment_id": f"pi:{intent_id}",
        "status": "awaiting_payer_action",
        "evidence_kind": ev.kind.value,
        "created_by": user_id,
        "created_at": at,
    }
    conn.execute(insert(payment_intent).values(**values))
    payload = {
        "intent_id": intent_id,
        "obligation_id": a["obligation_id"],
        "amount_cents": int(a["amount_cents"]),
        "currency": a["currency"],
    }
    conn.execute(
        insert(outbox_message).values(
            dedupe_key=f"pay:{intent_id}",
            kind="create_payment_session",
            intent_id=intent_id,
            payload=payload,
            payload_sha256=request_hash(payload),
            status="pending" if execution_enabled and not synthetic else "held",
            available_at=at,
            created_at=at,
        )
    )
    audit.append(
        conn,
        at=at,
        action="payment.intent_created",
        object_type="payment_intent",
        object_id=intent_id,
        actor_user_id=user_id,
        merchant_id=merchant_id,
        detail={
            "authorization_id": authorization_id,
            "amount_cents": int(a["amount_cents"]),
            "reserved_cents": amount,
            "outbox": "pending" if execution_enabled and not synthetic else "held",
            "evidence_kind": ev.kind.value,
        },
    )
    return {
        **values,
        "created_at": at.isoformat(),
        "replayed": False,
        **_labels(execution_enabled and not synthetic),
    }


def _labels(execution_enabled: bool) -> dict[str, Any]:
    return {
        "execution": "enabled" if execution_enabled else "disabled",
        "money_moved": False,
        "provider_confirmed": False,
        "funds_usable": False,
        "note": "no PayPal call is made in P4; the payer-hosted action is created by the P5 "
        "worker only after the owner authorizes the final Sandbox payment",
    }


def _intent_json(r: Any) -> dict[str, Any]:
    out = dict(r)
    out["created_at"] = r["created_at"].isoformat()
    return {**out, **_labels(False)}


def intents_for(conn: Connection, merchant_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        select(payment_intent)
        .where(payment_intent.c.payer_merchant == merchant_id)
        .order_by(payment_intent.c.created_at.desc())
    ).mappings()
    return [_intent_json(r) for r in rows]
