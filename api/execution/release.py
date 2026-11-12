"""Payer-confirmed release of a HELD payment intent (owner-approved fix, 2026-11-12).

An intent created while execution was off (or before both PayPal bindings existed) keeps a
`held` dispatch message forever: enabling execution later never dispatches it by itself.
`release_held_intent` is the payer's explicit, re-verified confirmation to dispatch **the
same** intent. In one transaction, with rows locked in the worker's order (outbox message,
then intent, then memberships through `revalidate`), it:

1. accepts only the payer's own, approved, non-synthetic intent held for a releasable
   reason, with execution enabled now;
2. proves there is exactly one dispatch message for it, `create_payment_session`, held,
   never attempted (no attempt, lease or finish recorded), and no provider session, request
   or operation exists;
3. refuses if another non-terminal intent owns the same invoice;
4. runs the authoritative `revalidate()` the worker runs before any provider mutation
   (approval unexpired, approver authority share-locked, conditions digest recomputed with
   the intent's own invoice included and its reservation excluded, the independent verifier,
   the exact active commitment, both bindings against the configured credential and payer
   fingerprints, Sandbox only);
5. moves the message held -> pending and the intent's reason to QUEUED_FOR_PROVIDER, keeping
   the intent, its authorization and its commitment, and records a state event and an audit
   event.

Idempotency: the HTTP route wraps this in the durable idempotency record (same key and body
-> the stored response; other body -> 409). A later release of an already queued intent is
refused as `ALREADY_QUEUED`: nothing changes twice. Any refusal leaves every row unchanged.
The worker revalidates again before any provider call.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, func, select, update

from api.app.auth import service as auth_service
from api.app.auth.permissions import Permission
from api.execution.service import RevalidationError, revalidate
from api.execution.states import IN_EXECUTION, IntentStatus
from api.execution.tables import (
    payment_session,
    payment_state_event,
    provider_operation,
    provider_request,
)
from api.integrations.paypal.execution import PaymentProvider
from api.workflow import audit
from api.workflow.store import ConflictError, NotVisibleError
from api.workflow.tables import outbox_message, payment_intent

RELEASABLE = frozenset({"EXECUTION_DISABLED", "PROVIDER_NOT_CONNECTED"})


@dataclass(frozen=True)
class _Facts:
    """What `revalidate()` needs from a runtime, without a worker: local facts only."""

    provider: PaymentProvider
    execution_enabled: Callable[[], bool]
    payer_email: Callable[[], str | None]


def release_held_intent(
    conn: Connection,
    *,
    merchant_id: str,
    user_id: str,
    intent_id: str,
    at: datetime,
    execution_enabled: bool,
    provider: PaymentProvider | None,
    payer_email: str | None,
) -> dict[str, Any]:
    # The actor's own authority, share-locked until commit (a revocation waits or is seen).
    try:
        auth_service.lock_membership(conn, user_id, merchant_id, Permission.PAYMENT_AUTHORIZE)
    except (auth_service.NotFoundError, auth_service.ForbiddenError):
        raise NotVisibleError("not found") from None

    # 1. Lock order = the worker's: outbox message(s) first, then the intent.
    msgs = (
        conn.execute(
            select(outbox_message)
            .where(outbox_message.c.intent_id == intent_id)
            .order_by(outbox_message.c.id)
            .with_for_update()
        )
        .mappings()
        .all()
    )
    intent = (
        conn.execute(
            select(payment_intent).where(payment_intent.c.intent_id == intent_id).with_for_update()
        )
        .mappings()
        .one_or_none()
    )
    if intent is None or intent["payer_merchant"] != merchant_id:
        raise NotVisibleError("not found")  # the payee and outsiders see nothing
    if intent["evidence_kind"] == "synthetic":
        raise ConflictError("synthetic evidence never executes", code="SYNTHETIC_EVIDENCE")
    if (
        intent["status"] == IntentStatus.APPROVED.value
        and intent["status_reason"] == "QUEUED_FOR_PROVIDER"
        and len(msgs) == 1
        and msgs[0]["status"] == "pending"
    ):
        raise ConflictError("this payment is already queued for PayPal", code="ALREADY_QUEUED")
    if intent["status"] != IntentStatus.APPROVED.value:
        raise ConflictError(
            f"only an approved, undispatched intent can be released (it is {intent['status']})",
            code="NOT_RELEASABLE",
        )
    if intent["status_reason"] not in RELEASABLE:
        raise ConflictError(
            f"the intent is not held for a releasable reason ({intent['status_reason']})",
            code="NOT_HELD",
        )

    # 2. Exactly one held, never-attempted creation job; nothing sent to a provider yet.
    if len(msgs) != 1 or msgs[0]["kind"] != "create_payment_session":
        raise ConflictError(
            "expected exactly one dispatch job for this intent", code="CONFLICTING_JOB"
        )
    m = msgs[0]
    if m["status"] != "held":
        raise ConflictError(f"the dispatch job is {m['status']}, not held", code="NOT_HELD")
    if m["attempts"] != 0 or m["lease_owner"] is not None or m["finished_at"] is not None:
        raise ConflictError("the dispatch job was attempted before", code="PREVIOUSLY_ATTEMPTED")
    for table in (payment_session, provider_request, provider_operation):
        n = conn.execute(
            select(func.count()).select_from(table).where(table.c.intent_id == intent_id)
        ).scalar_one()
        if n:
            raise ConflictError(
                f"a provider {table.name.replace('_', ' ')} already exists for this intent",
                code="PREVIOUSLY_ATTEMPTED",
            )

    # 3. No competing intent for the same invoice.
    competing = conn.execute(
        select(payment_intent.c.intent_id).where(
            payment_intent.c.obligation_id == intent["obligation_id"],
            payment_intent.c.intent_id != intent_id,
            payment_intent.c.status.in_(IN_EXECUTION),
        )
    ).first()
    if competing is not None:
        raise ConflictError(
            "another payment of this invoice is in execution", code="COMPETING_INTENT"
        )

    # 4. The worker's authoritative pre-mutation checks.
    if not execution_enabled:
        raise ConflictError("payment execution is switched off", code="EXECUTION_DISABLED")
    if provider is None:
        raise ConflictError("no payment provider is configured", code="PROVIDER_UNAVAILABLE")
    facts = _Facts(provider, lambda: execution_enabled, lambda: payer_email)
    try:
        revalidate(conn, facts, dict(intent), at)  # type: ignore[arg-type]
    except RevalidationError as exc:
        raise ConflictError(str(exc), code=exc.code) from None

    # 5. Release: the same intent, authorization and commitment; nothing re-reserved.
    conn.execute(
        update(outbox_message)
        .where(outbox_message.c.id == m["id"], outbox_message.c.status == "held")
        .values(status="pending", available_at=at)
    )
    conn.execute(
        update(payment_intent)
        .where(payment_intent.c.intent_id == intent_id)
        .values(status_reason="QUEUED_FOR_PROVIDER", status_updated_at=at)
    )
    conn.execute(
        payment_state_event.insert().values(
            intent_id=intent_id,
            from_status=IntentStatus.APPROVED.value,
            to_status=IntentStatus.APPROVED.value,
            reason="RELEASED_BY_PAYER",
            actor=f"user:{user_id}"[:80],
            occurred_at=at,
            detail={"held_reason": intent["status_reason"], "outbox_id": m["id"]},
        )
    )
    audit.append(
        conn,
        at=at,
        action="payment.released",
        object_type="payment_intent",
        object_id=intent_id,
        actor_user_id=user_id,
        merchant_id=merchant_id,
        detail={
            "held_reason": intent["status_reason"],
            "authorization_id": intent["authorization_id"],
            "commitment_id": intent["commitment_id"],
            "amount_cents": int(intent["amount_cents"]),
        },
    )
    return {
        "intent_id": intent_id,
        "status": IntentStatus.APPROVED.value,
        "status_reason": "QUEUED_FOR_PROVIDER",
        "authorization_id": intent["authorization_id"],
        "commitment_id": intent["commitment_id"],
        "dispatch": "queued",
        "money_moved": False,
        "note": "queued for the worker, which revalidates again before any PayPal call",
    }
