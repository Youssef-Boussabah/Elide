"""Payment-intent state machine (ADR-0012 §4.1) and its persistence helper.

The same transition table is installed as a PostgreSQL trigger by migration 0006; a test
asserts the two agree. `transition` is a compare-and-set: it changes the status only if
the intent is still in one of the expected states, records the step in the append-only
`payment_state_event` log and the hash-chained audit trail, all in the caller's
transaction. A late, duplicate or reordered observation therefore cannot regress state.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import Connection, select, update

from api.execution.tables import payment_state_event
from api.workflow import audit
from api.workflow.tables import payment_intent


class IntentStatus(StrEnum):
    APPROVED = "approved"
    SESSION_PENDING = "session_pending"
    PAYER_ACTION_PENDING = "payer_action_pending"
    PROVIDER_PENDING = "provider_pending"
    PROVIDER_UNKNOWN = "provider_unknown"
    PROVIDER_CONFIRMED = "provider_confirmed"
    RECONCILED = "reconciled"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    EXCEPTION = "exception"


S = IntentStatus
TRANSITIONS: dict[IntentStatus, frozenset[IntentStatus]] = {
    S.APPROVED: frozenset({S.SESSION_PENDING, S.CANCELLED}),
    S.SESSION_PENDING: frozenset(
        {S.PAYER_ACTION_PENDING, S.PROVIDER_UNKNOWN, S.FAILED, S.CANCELLED}
    ),
    S.PROVIDER_UNKNOWN: frozenset(
        {
            S.SESSION_PENDING,
            S.PAYER_ACTION_PENDING,
            S.PROVIDER_PENDING,
            S.PROVIDER_CONFIRMED,
            S.FAILED,
            S.CANCELLED,
            S.EXPIRED,
            S.EXCEPTION,
        }
    ),
    S.PAYER_ACTION_PENDING: frozenset(
        {
            S.PROVIDER_PENDING,
            S.PROVIDER_CONFIRMED,
            S.PROVIDER_UNKNOWN,
            S.EXPIRED,
            S.CANCELLED,
            S.EXCEPTION,
        }
    ),
    S.PROVIDER_PENDING: frozenset(
        {S.PROVIDER_CONFIRMED, S.PAYER_ACTION_PENDING, S.PROVIDER_UNKNOWN, S.EXCEPTION}
    ),
    S.PROVIDER_CONFIRMED: frozenset({S.RECONCILED, S.EXCEPTION}),
    S.RECONCILED: frozenset(),
    S.FAILED: frozenset(),
    S.EXPIRED: frozenset(),
    S.CANCELLED: frozenset(),
    S.EXCEPTION: frozenset(),
}

TERMINAL = frozenset(s for s, nxt in TRANSITIONS.items() if not nxt)
# Cash stays reserved in every non-terminal state, and in `exception` (never released
# automatically while the money state may be uncertain).
HOLDS_CASH = frozenset(set(IntentStatus) - {S.FAILED, S.EXPIRED, S.CANCELLED, S.RECONCILED})
# States in which the invoice is "in execution" and must not be planned again.
IN_EXECUTION = tuple(
    s.value for s in IntentStatus if s not in (S.FAILED, S.EXPIRED, S.CANCELLED, S.RECONCILED)
)
# States in which a provider session may exist that the payer could still pay.
SESSION_LIVE = frozenset(
    {S.SESSION_PENDING, S.PAYER_ACTION_PENDING, S.PROVIDER_PENDING, S.PROVIDER_UNKNOWN}
)


class TransitionError(RuntimeError):
    """The intent was not in an expected state (lost a race or a stale observation)."""

    def __init__(self, intent_id: str, current: str | None, wanted: IntentStatus):
        super().__init__(f"intent {intent_id}: {current} -> {wanted.value} refused")
        self.current = current


def allowed(src: IntentStatus, dst: IntentStatus) -> bool:
    return dst in TRANSITIONS[src]


def current_status(conn: Connection, intent_id: str, *, lock: bool = False) -> IntentStatus:
    q = select(payment_intent.c.status).where(payment_intent.c.intent_id == intent_id)
    if lock:
        q = q.with_for_update()
    return IntentStatus(conn.execute(q).scalar_one())


def transition(
    conn: Connection,
    intent_id: str,
    to: IntentStatus,
    *,
    expect: frozenset[IntentStatus] | set[IntentStatus],
    reason: str,
    actor: str,
    at: datetime,
    detail: dict[str, Any] | None = None,
) -> IntentStatus:
    """Move `intent_id` to `to` iff it is currently in `expect`. Returns the old status."""
    row = (
        conn.execute(
            select(payment_intent.c.status, payment_intent.c.payer_merchant)
            .where(payment_intent.c.intent_id == intent_id)
            .with_for_update()
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise TransitionError(intent_id, None, to)
    src = IntentStatus(row["status"])
    if src not in expect or not allowed(src, to):
        raise TransitionError(intent_id, src.value, to)
    conn.execute(
        update(payment_intent)
        .where(payment_intent.c.intent_id == intent_id)
        .values(status=to.value, status_reason=reason[:80], status_updated_at=at)
    )
    conn.execute(
        payment_state_event.insert().values(
            intent_id=intent_id,
            from_status=src.value,
            to_status=to.value,
            reason=reason[:80],
            actor=actor[:80],
            occurred_at=at,
            detail=detail or {},
        )
    )
    audit.append(
        conn,
        at=at,
        action=f"payment.{to.value}",
        object_type="payment_intent",
        object_id=intent_id,
        merchant_id=row["payer_merchant"],
        detail={"from": src.value, "reason": reason, **(detail or {})},
    )
    return src
