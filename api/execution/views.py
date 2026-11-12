"""Merchant-facing P5 operations and read models (ADR-0012 §8, §9).

Every function is scoped to the calling merchant: an intent is visible to its payer and
its payee only, and each sees only its own private figures (the payer never sees the
payee's fee, net or settlement credit; the payee never sees the payer's reservation, fee
ceiling or capacity). Anything else is the identical 404 used across the workspace.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, or_, select

from api.app.auth.tables import merchant_org
from api.domain.invoice import Provenance, SourceKind
from api.domain.money import USD, Money
from api.execution.invoice_session import UNPAID, recipient_view_url, terms_problems
from api.execution.service import _expected, request_cancel  # noqa: F401  (shared rules)
from api.execution.states import IntentStatus, TransitionError, transition
from api.execution.tables import (
    payment_exception,
    payment_session,
    payment_state_event,
    provider_operation,
    provider_settlement,
)
from api.integrations.paypal.execution import PaymentProvider
from api.liquidity.capacity import CapacityEvidence, EvidenceKind
from api.liquidity.store import release_commitment_in, save_evidence_in
from api.liquidity.tables import capacity_commitment
from api.workflow import audit
from api.workflow.store import ConflictError, NotVisibleError
from api.workflow.tables import payment_intent

S = IntentStatus
D006 = "D006: provider-confirmed is not spendable until separately verified available"
D008 = "D008: settled in a non-USD currency; never USD capacity"


def _visible_intent(conn: Connection, merchant_id: str, intent_id: str) -> dict[str, Any]:
    r = (
        conn.execute(
            select(payment_intent).where(
                payment_intent.c.intent_id == intent_id,
                or_(
                    payment_intent.c.payer_merchant == merchant_id,
                    payment_intent.c.payee_merchant == merchant_id,
                ),
            )
        )
        .mappings()
        .one_or_none()
    )
    if r is None:
        raise NotVisibleError("not found")
    return dict(r)


def intent_detail(conn: Connection, merchant_id: str, intent_id: str) -> dict[str, Any]:
    i = _visible_intent(conn, merchant_id, intent_id)
    is_payer = i["payer_merchant"] == merchant_id
    sess = (
        conn.execute(select(payment_session).where(payment_session.c.intent_id == intent_id))
        .mappings()
        .one_or_none()
    )
    hist = conn.execute(
        select(
            payment_state_event.c.from_status,
            payment_state_event.c.to_status,
            payment_state_event.c.reason,
            payment_state_event.c.occurred_at,
        )
        .where(payment_state_event.c.intent_id == intent_id)
        .order_by(payment_state_event.c.id)
    ).all()
    exc = conn.execute(
        select(
            payment_exception.c.code, payment_exception.c.opened_at, payment_exception.c.resolved_at
        )
        .where(payment_exception.c.intent_id == intent_id)
        .order_by(payment_exception.c.id)
    ).all()
    st = (
        conn.execute(
            select(provider_settlement).where(provider_settlement.c.intent_id == intent_id)
        )
        .mappings()
        .one_or_none()
    )
    status = i["status"]
    out: dict[str, Any] = {
        "intent_id": intent_id,
        "role": "payer" if is_payer else "payee",
        "obligation_id": i["obligation_id"],
        "revision_no": i["revision_no"],
        "payer_merchant": i["payer_merchant"],
        "payee_merchant": i["payee_merchant"],
        "amount_cents": int(i["amount_cents"]),
        "currency": i["currency"],
        "status": status,
        "status_reason": i["status_reason"],
        "status_updated_at": i["status_updated_at"].isoformat() if i["status_updated_at"] else None,
        "evidence_kind": i["evidence_kind"],
        "history": [{"from": f, "to": t, "reason": r, "at": a.isoformat()} for f, t, r, a in hist],
        "exceptions": [
            {"code": c, "opened_at": o.isoformat(), "resolved": r is not None} for c, o, r in exc
        ],
        "session": None
        if sess is None
        else {
            "provider": "paypal",
            "environment": "sandbox",
            "invoice_number": sess["invoice_number"],
            "provider_invoice_created": sess["provider_invoice_id"] is not None,
            "provider_status": sess["last_provider_status"],
            "due_date": sess["due_date"].isoformat(),
            "pay_deadline": sess["pay_deadline"].isoformat(),
            "sent_at": sess["sent_at"].isoformat() if sess["sent_at"] else None,
            "cancel_requested": sess["cancel_requested_at"] is not None,
            "payer_link_available": is_payer and status == S.PAYER_ACTION_PENDING.value,
        },
        "provider_confirmed": status in ("provider_confirmed", "reconciled"),
        "reconciled": status == "reconciled",
        "funds_usable": False,
        "labels": [D006],
    }
    if is_payer:
        c = conn.execute(
            select(
                capacity_commitment.c.kind,
                capacity_commitment.c.amount_cents,
                capacity_commitment.c.released_at,
                capacity_commitment.c.release_reason,
            )
            .where(capacity_commitment.c.commitment_id.in_((i["commitment_id"], f"so:{intent_id}")))
            .order_by(capacity_commitment.c.id)
        ).all()
        out["fee_ceiling_cents"] = int(i["fee_ceiling_cents"])
        out["cash_reservation"] = [
            {"kind": k, "amount_cents": int(a), "active": r is None, "release_reason": rr}
            for k, a, r, rr in c
        ]
        if st is not None:
            out["settlement"] = {
                "capture_id": st["capture_id"],
                "paid_cents": int(st["gross_cents"]),
                "currency": st["gross_currency"],
            }
    elif st is not None:
        out["settlement"] = _payee_settlement(st)
    return out


def _payee_settlement(st: Any) -> dict[str, Any]:
    reasons = [D006]
    if st["credit_currency"] != "USD":
        reasons.append(D008)
    return {
        "capture_id": st["capture_id"],
        "invoice_id": st["invoice_id"],
        "gross_cents": int(st["gross_cents"]),
        "fee_cents": int(st["fee_cents"]),
        "net_cents": int(st["net_cents"]),
        "currency": st["gross_currency"],
        "credited_cents": int(st["credit_cents"]),
        "credited_currency": st["credit_currency"],
        "exchange_rate": st["exchange_rate"],
        "receipt_id": st["receipt_id"],
        "receipt_status": "PROVIDER_CONFIRMED_NOT_SPENDABLE",
        "usd_deployable_cents": 0,
        "reasons": reasons,
    }


def settlements_for(conn: Connection, merchant_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        select(provider_settlement)
        .where(
            or_(
                provider_settlement.c.payer_merchant == merchant_id,
                provider_settlement.c.payee_merchant == merchant_id,
            )
        )
        .order_by(provider_settlement.c.recorded_at.desc())
    ).mappings()
    out = []
    for st in rows:
        if st["payee_merchant"] == merchant_id:
            out.append({"role": "payee", "intent_id": st["intent_id"], **_payee_settlement(st)})
        else:
            out.append(
                {
                    "role": "payer",
                    "intent_id": st["intent_id"],
                    "capture_id": st["capture_id"],
                    "paid_cents": int(st["gross_cents"]),
                    "currency": st["gross_currency"],
                    "outflow_commitment_id": f"so:{st['intent_id']}",
                }
            )
    return out


def payer_link(
    conn: Connection,
    *,
    merchant_id: str,
    user_id: str,
    intent_id: str,
    provider: PaymentProvider | None,
    payer_email: str | None,
    at: datetime,
) -> str:
    """The PayPal-hosted payer page for the payer's own pending payment. Fetched now, by
    GET, checked against the agreed terms; never stored, logged or audited."""
    i = _visible_intent(conn, merchant_id, intent_id)
    if i["payer_merchant"] != merchant_id:
        raise NotVisibleError("not found")
    if i["status"] != S.PAYER_ACTION_PENDING.value:
        raise ConflictError("the payment is not waiting for the payer", code="NOT_AWAITING_PAYER")
    if provider is None or payer_email is None:
        raise ConflictError("no payment provider is configured", code="PROVIDER_UNAVAILABLE")
    sess = dict(
        conn.execute(select(payment_session).where(payment_session.c.intent_id == intent_id))
        .mappings()
        .one()
    )
    if at >= sess["pay_deadline"]:
        raise ConflictError(
            "the agreed pay-by time has passed; an operator must review this payment",
            code="PAY_DEADLINE_PASSED",
        )
    review = conn.execute(
        select(payment_exception.c.code).where(
            payment_exception.c.intent_id == intent_id, payment_exception.c.resolved_at.is_(None)
        )
    ).first()
    if review is not None:
        raise ConflictError("this payment is under operator review", code="NEEDS_REVIEW")
    inv_id = sess["provider_invoice_id"]
    g = provider.get_invoice(inv_id)
    conn.execute(
        provider_operation.insert().values(
            intent_id=intent_id,
            session_id=sess["session_id"],
            operation="get_invoice",
            mutating=False,
            request_key=f"op-{secrets.token_hex(12)}",
            request_sha256="0" * 64,
            state="succeeded" if g.ok else "failed",
            http_status=g.http_status,
            debug_id=g.debug_id,
            outcome_code=f"payer_link:{g.outcome.value}"[:60],
            worker=f"user:{user_id}"[:80],
            started_at=at,
            finished_at=at,
        )
    )
    if not g.ok:
        raise ConflictError("PayPal could not be reached; try again", code="PROVIDER_UNAVAILABLE")
    problems = terms_problems(
        g.body,
        _expected(sess, i),
        invoice_id=inv_id,
        payer_email=payer_email,
        expected_status=None,
        allowed_statuses=UNPAID,
    )
    url = recipient_view_url(g.body)
    if problems or url is None:
        raise ConflictError(
            "the PayPal invoice does not match the agreed terms", code="PROVIDER_TERMS_MISMATCH"
        )
    audit.append(
        conn,
        at=at,
        action="payment.payer_link_viewed",
        object_type="payment_intent",
        object_id=intent_id,
        actor_user_id=user_id,
        merchant_id=merchant_id,
        detail={"provider_status": g.body.get("status")},
    )
    return url


def cancel(
    conn: Connection, *, merchant_id: str, user_id: str, intent_id: str, at: datetime
) -> dict[str, Any]:
    """Payer withdraws before payment. Without a provider invoice: cancelled now, cash
    released. With one: the worker cancels it at PayPal and confirms by GET first."""
    i = _visible_intent(conn, merchant_id, intent_id)
    if i["payer_merchant"] != merchant_id:
        raise NotVisibleError("not found")
    st = S(i["status"])
    if st is S.APPROVED:
        try:
            transition(
                conn,
                intent_id,
                S.CANCELLED,
                expect={S.APPROVED},
                reason="CANCELLED_BY_PAYER",
                actor=f"user:{user_id}",
                at=at,
            )
        except TransitionError:
            raise ConflictError("the payment moved on; reload", code="STATE_CHANGED") from None
        release_commitment_in(conn, i["commitment_id"], at, "cancelled:by_payer")
        # Any queued dispatch message finds the intent cancelled and does nothing.
        return {"intent_id": intent_id, "status": "cancelled", "cash_released": True}
    if st in (S.SESSION_PENDING, S.PAYER_ACTION_PENDING):
        conn.execute(
            payment_session.update()
            .where(
                payment_session.c.intent_id == intent_id,
                payment_session.c.cancel_requested_at.is_(None),
            )
            .values(cancel_requested_at=at, updated_at=at)
        )
        from api.execution.service import schedule

        schedule(
            conn,
            kind="cancel_payment_session",
            dedupe_key=f"cancel:{intent_id}",
            intent_id=intent_id,
            payload={"intent_id": intent_id, "target": "cancelled", "reason": "CANCELLED_BY_PAYER"},
            at=at,
            revive=True,
        )
        audit.append(
            conn,
            at=at,
            action="payment.cancel_requested",
            object_type="payment_intent",
            object_id=intent_id,
            actor_user_id=user_id,
            merchant_id=merchant_id,
            detail={},
        )
        return {
            "intent_id": intent_id,
            "status": st.value,
            "cancel_requested": True,
            "cash_released": False,
            "note": "the PayPal invoice is cancelled and confirmed by GET before cash is released",
        }
    raise ConflictError(
        f"a payment in state {st.value} cannot be cancelled here (money may be moving)",
        code="CANNOT_CANCEL",
    )


def attest_capacity(
    conn: Connection,
    *,
    merchant_id: str,
    user_id: str,
    spendable_cents: int,
    reserve_cents: int,
    valid_hours: int,
    included_receipt_ids: list[str],
    included_outflow_ids: list[str],
    at: datetime,
) -> dict[str, Any]:
    """The merchant attests its OWN current USD balance (doc 03: allowed, visibly labelled
    merchant-attested, never called provider-verified). Listing a settled outflow states
    the balance already reflects that payment, which releases exactly that commitment."""
    synthetic = conn.execute(
        select(merchant_org.c.is_synthetic).where(merchant_org.c.merchant_id == merchant_id)
    ).scalar_one()
    own = set(
        conn.execute(
            select(capacity_commitment.c.commitment_id).where(
                capacity_commitment.c.merchant_id == merchant_id,
                capacity_commitment.c.kind == "settled_outflow",
            )
        ).scalars()
    )
    if synthetic:
        raise ConflictError(
            "a synthetic merchant cannot attest real capacity", code="SYNTHETIC_MERCHANT"
        )
    foreign = set(included_outflow_ids) - own
    if foreign:
        raise NotVisibleError("not found")
    ev = CapacityEvidence(
        evidence_id=f"att_{merchant_id}_{int(at.timestamp())}_{secrets.token_hex(3)}",
        merchant_id=merchant_id,
        verified_spendable=Money(spendable_cents, USD),
        protected_reserve=Money(reserve_cents, USD),
        kind=EvidenceKind.MERCHANT_ATTESTED,
        observed_at=at,
        valid_until=at + timedelta(hours=valid_hours),
        provenance=Provenance(SourceKind.MERCHANT_ATTESTED, f"attested by user {user_id}", at),
        included_receipt_ids=frozenset(included_receipt_ids),
        included_outflow_ids=frozenset(included_outflow_ids),
    )
    released = save_evidence_in(conn, ev)
    audit.append(
        conn,
        at=at,
        action="capacity.attested",
        object_type="capacity_evidence",
        object_id=ev.evidence_id,
        actor_user_id=user_id,
        merchant_id=merchant_id,
        detail={
            "spendable_cents": spendable_cents,
            "reserve_cents": reserve_cents,
            "released_outflows": list(released),
        },
    )
    return {
        "evidence_id": ev.evidence_id,
        "kind": "merchant_attested",
        "observed_at": at.isoformat(),
        "valid_until": ev.valid_until.isoformat(),
        "released_outflows": list(released),
        "label": "MERCHANT_ATTESTED: not provider-verified",
    }
