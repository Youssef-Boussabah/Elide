"""Negotiated amendments between the two parties of one invoice (ADR-0011 §6).

An offer wraps one P1 DRAFT revision. Making an offer records the proposer's own
acceptance (the DRAFT is signed by the proposer). The counterparty independently
accepts, rejects or counters from its own session; the proposer may withdraw. Only an
acceptance by the counterparty writes the AUTHORIZED revision (signed by both), which
P1 `InvoiceRecord.add_revision` re-checks: both parties, terms equal to the offer, offer
still open and unexpired. An agreed amendment authorizes no payment.

Every decision is bound to `terms_digest` and taken under row locks (obligation, then
offer), so stale, replayed, expired, duplicate or racing decisions are rejected.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, insert, or_, select, update

from api.domain.invoice import (
    InvoiceError,
    ObligationRevision,
    Provenance,
    RevisionStatus,
    SourceKind,
)
from api.domain.money import USD, Money
from api.negotiation.guards import money_amounts
from api.workflow import audit
from api.workflow.store import (
    ConflictError,
    InvalidRequestError,
    NotVisibleError,
    insert_revision,
    load_record,
)
from api.workflow.tables import obligation, offer, offer_decision

MAX_OFFER_TTL = timedelta(days=14)
MAX_MESSAGE = 500


@dataclass(frozen=True, slots=True)
class OfferTerms:
    offer_id: str
    obligation_id: str
    base_revision_no: int
    revision_no: int
    debtor: str
    creditor: str
    proposer: str
    counterparty: str
    cash_cents: int
    concession_cents: int
    currency: str
    due_at: datetime
    expires_at: datetime

    def digest(self) -> str:
        blob = {
            "schema": "elide.offer-terms.v1",
            "offer_id": self.offer_id,
            "obligation_id": self.obligation_id,
            "base_revision_no": self.base_revision_no,
            "revision_no": self.revision_no,
            "debtor": self.debtor,
            "creditor": self.creditor,
            "proposer": self.proposer,
            "counterparty": self.counterparty,
            "cash_cents": self.cash_cents,
            "concession_cents": self.concession_cents,
            "currency": self.currency,
            "due_at": self.due_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }
        return hashlib.sha256(json.dumps(blob, sort_keys=True).encode()).hexdigest()


def _terms(row: Any, debtor: str, creditor: str) -> OfferTerms:
    return OfferTerms(
        offer_id=row["offer_id"],
        obligation_id=row["obligation_id"],
        base_revision_no=row["base_revision_no"],
        revision_no=row["revision_no"],
        debtor=debtor,
        creditor=creditor,
        proposer=row["proposer_merchant"],
        counterparty=row["counterparty_merchant"],
        cash_cents=int(row["cash_cents"]),
        concession_cents=int(row["concession_cents"]),
        currency=row["currency"],
        due_at=row["due_at"],
        expires_at=row["expires_at"],
    )


def effective_status(row: Any, in_force_revision_no: int, now: datetime) -> str:
    if row["status"] != "proposed":
        return row["status"]
    if now >= row["expires_at"]:
        return "expired"
    if in_force_revision_no != row["base_revision_no"]:
        return "superseded"
    return "proposed"


def _close(conn: Connection, offer_id: str, status: str, at: datetime, rev: int | None) -> None:
    conn.execute(
        update(offer)
        .where(offer.c.offer_id == offer_id, offer.c.status == "proposed")
        .values(status=status, closed_at=at, closed_by_revision=rev)
    )


def _check_message(message: str | None, allowed_cents: set[int]) -> str | None:
    if message is None:
        return None
    message = message.strip()
    if len(message) > MAX_MESSAGE:
        raise InvalidRequestError(f"message must be at most {MAX_MESSAGE} characters")
    stray = money_amounts(message) - allowed_cents
    if stray:
        raise InvalidRequestError(
            "message mentions amounts that are not part of these terms; remove them",
            code="MESSAGE_AMOUNT_MISMATCH",
        )
    return message or None


def _audit_both(conn: Connection, t: OfferTerms, **kw: Any) -> None:
    for m in sorted({t.debtor, t.creditor}):
        audit.append(conn, merchant_id=m, object_type="offer", object_id=t.offer_id, **kw)


def propose(
    conn: Connection,
    *,
    merchant_id: str,
    user_id: str,
    session_id: str,
    obligation_id: str,
    concession_cents: int,
    pay_by: datetime,
    expires_at: datetime,
    at: datetime,
    message: str | None = None,
    source: str = "manual",
    ai_reference: dict[str, Any] | None = None,
    parent_offer_id: str | None = None,
    parent_revision_no: int | None = None,
    operation_key: str = "",
) -> dict[str, Any]:
    inv = load_record(conn, obligation_id, lock=True)
    record, ob = inv.record, inv.record.obligation
    if merchant_id not in (ob.debtor, ob.creditor):
        raise NotVisibleError("not found")
    counterparty = ob.creditor if merchant_id == ob.debtor else ob.debtor
    in_force = record.in_force_revision(at)

    open_rows = (
        conn.execute(
            select(offer)
            .where(offer.c.obligation_id == obligation_id, offer.c.status == "proposed")
            .with_for_update()
        )
        .mappings()
        .all()
    )
    for r in open_rows:
        st = effective_status(r, in_force.revision_no, at)
        if st != "proposed":
            _close(conn, r["offer_id"], st, at, None)
        else:
            raise ConflictError(
                "an offer is already open on this invoice; accept, reject or counter it",
                code="OPEN_OFFER_EXISTS",
            )

    if ob.currency != USD:
        raise InvalidRequestError("only USD invoices can be negotiated (D008)", code="CURRENCY")
    if isinstance(concession_cents, bool) or not isinstance(concession_cents, int):
        raise InvalidRequestError("concession must be integer cents")
    pos = record.position(at)
    if not 0 <= concession_cents < pos.original_open.cents - pos.cash_applied.cents:
        raise InvalidRequestError("concession must leave a positive amount to pay")
    cash = pos.original_open.cents - pos.cash_applied.cents - concession_cents
    if not at < expires_at <= at + MAX_OFFER_TTL:
        raise InvalidRequestError("expiry must be in the future and at most 14 days away")
    if not expires_at <= pay_by <= in_force.due_at:
        raise InvalidRequestError(
            "pay-by date must be on or after the offer expiry and no later than the current "
            "due date (Elide does not extend credit)",
            code="PAY_BY_OUT_OF_RANGE",
        )
    message = _check_message(message, {cash, concession_cents, ob.face.cents})

    offer_id = "off_" + secrets.token_hex(8)
    rev_no = record.revisions[-1].revision_no + 1
    parent_rev = parent_revision_no if parent_offer_id is not None else None
    rev = ObligationRevision(
        obligation_id=obligation_id,
        revision_no=rev_no,
        status=RevisionStatus.DRAFT,
        due_at=pay_by,
        business_tz=in_force.business_tz,
        concession=Money(concession_cents, USD),
        created_at=at,
        provenance=Provenance(SourceKind.MERCHANT_ENTERED, f"offer:{offer_id}:{user_id}", at),
        expires_at=expires_at,
        authorized_by=frozenset({merchant_id}),
        supersedes=parent_rev,
    )
    try:
        record.add_revision(rev)
    except InvoiceError as exc:
        raise ConflictError(str(exc), code="TERMS_REJECTED_BY_LEDGER") from None
    insert_revision(conn, rev)
    t = OfferTerms(
        offer_id,
        obligation_id,
        in_force.revision_no,
        rev_no,
        ob.debtor,
        ob.creditor,
        merchant_id,
        counterparty,
        cash,
        concession_cents,
        USD,
        pay_by,
        expires_at,
    )
    conn.execute(
        insert(offer).values(
            offer_id=offer_id,
            obligation_id=obligation_id,
            revision_no=rev_no,
            base_revision_no=in_force.revision_no,
            proposer_merchant=merchant_id,
            counterparty_merchant=counterparty,
            cash_cents=cash,
            concession_cents=concession_cents,
            currency=USD,
            due_at=pay_by,
            expires_at=expires_at,
            terms_digest=t.digest(),
            parent_offer_id=parent_offer_id,
            source=source,
            ai_reference=ai_reference,
            message=message,
            status="proposed",
            created_by=user_id,
            created_at=at,
        )
    )
    conn.execute(
        insert(offer_decision).values(
            offer_id=offer_id,
            merchant_id=merchant_id,
            user_id=user_id,
            session_id=session_id,
            decision="accept",
            terms_digest=t.digest(),
            decided_at=at,
            operation_key=operation_key or f"propose:{offer_id}",
            resulting_revision_no=rev_no,
        )
    )
    _audit_both(
        conn,
        t,
        at=at,
        action="offer.proposed",
        actor_user_id=user_id,
        session_id=session_id,
        detail={
            "obligation_id": obligation_id,
            "proposer": merchant_id,
            "cash_cents": cash,
            "concession_cents": concession_cents,
            "terms_digest": t.digest(),
            "source": source,
            "parent_offer_id": parent_offer_id,
        },
    )
    return {"offer_id": offer_id, "terms_digest": t.digest(), "revision_no": rev_no}


def _locked_offer(conn: Connection, offer_id: str, merchant_id: str):
    head = conn.execute(
        select(
            offer.c.obligation_id, offer.c.proposer_merchant, offer.c.counterparty_merchant
        ).where(offer.c.offer_id == offer_id)
    ).one_or_none()
    if head is None or merchant_id not in (head.proposer_merchant, head.counterparty_merchant):
        raise NotVisibleError("not found")
    inv = load_record(conn, head.obligation_id, lock=True)  # lock order: obligation, offer
    row = (
        conn.execute(select(offer).where(offer.c.offer_id == offer_id).with_for_update())
        .mappings()
        .one()
    )
    return inv, row


def decide(
    conn: Connection,
    *,
    merchant_id: str,
    user_id: str,
    session_id: str,
    offer_id: str,
    decision: str,
    terms_digest: str,
    at: datetime,
    operation_key: str,
    counter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if decision not in ("accept", "reject", "counter", "withdraw"):
        raise InvalidRequestError("unknown decision")
    inv, row = _locked_offer(conn, offer_id, merchant_id)
    record, ob = inv.record, inv.record.obligation
    t = _terms(row, ob.debtor, ob.creditor)
    in_force = record.in_force_revision(at)
    status = effective_status(row, in_force.revision_no, at)
    if status != "proposed":
        code = {"expired": "OFFER_EXPIRED", "superseded": "STALE_REVISION"}.get(
            status, "OFFER_CLOSED"
        )
        raise ConflictError(f"offer is {status}; no decision can be recorded", code=code)
    if terms_digest != row["terms_digest"]:
        raise ConflictError(
            "the terms you reviewed differ from the stored offer; reload it", code="STALE_DIGEST"
        )
    already = conn.execute(
        select(offer_decision.c.decision).where(
            offer_decision.c.offer_id == offer_id, offer_decision.c.merchant_id == merchant_id
        )
    ).scalar_one_or_none()
    if decision == "withdraw":
        if merchant_id != t.proposer:
            raise ConflictError("only the proposer can withdraw an offer", code="NOT_PROPOSER")
    elif merchant_id != t.counterparty:
        raise ConflictError(
            "your merchant made this offer and already accepted it; only the counterparty "
            "can accept, reject or counter",
            code="NOT_COUNTERPARTY",
        )
    elif already is not None:
        raise ConflictError("your merchant already decided this offer", code="ALREADY_DECIDED")

    base = {
        "obligation_id": t.obligation_id,
        "status": RevisionStatus.REJECTED,
        "due_at": t.due_at,
        "business_tz": in_force.business_tz,
        "concession": Money(t.concession_cents, USD),
        "created_at": at,
        "provenance": Provenance(
            SourceKind.MERCHANT_ENTERED, f"decision:{offer_id}:{merchant_id}:{user_id}", at
        ),
        "supersedes": t.revision_no,
    }
    rev_no = record.revisions[-1].revision_no + 1
    child = None
    new_status = {"accept": "agreed", "reject": "rejected", "withdraw": "withdrawn"}.get(decision)
    if decision == "counter":
        if counter is None:
            raise InvalidRequestError("a counter needs new terms")
        _close(conn, offer_id, "countered", at, None)  # frees the one-open-offer slot
        child = propose(
            conn,
            merchant_id=merchant_id,
            user_id=user_id,
            session_id=session_id,
            obligation_id=t.obligation_id,
            parent_offer_id=offer_id,
            parent_revision_no=t.revision_no,
            at=at,
            operation_key=operation_key,
            **counter,
        )
        new_status = "countered"
        rev_no = child["revision_no"]
    else:
        signers = {ob.debtor, ob.creditor} if decision == "accept" else {merchant_id}
        status_enum = RevisionStatus.AUTHORIZED if decision == "accept" else RevisionStatus.REJECTED
        rev = ObligationRevision(
            revision_no=rev_no,
            authorized_by=frozenset(signers),
            **{**base, "status": status_enum},
        )
        try:
            record.add_revision(rev)  # P1: both parties, matching terms, open and unexpired
        except InvoiceError as exc:
            raise ConflictError(str(exc), code="TERMS_REJECTED_BY_LEDGER") from None
        insert_revision(conn, rev)
    if decision != "withdraw" or already is None:
        conn.execute(
            insert(offer_decision).values(
                offer_id=offer_id,
                merchant_id=merchant_id,
                user_id=user_id,
                session_id=session_id,
                decision=decision,
                terms_digest=terms_digest,
                decided_at=at,
                operation_key=operation_key,
                resulting_revision_no=rev_no,
                child_offer_id=child["offer_id"] if child else None,
            )
        )
    _close(conn, offer_id, new_status, at, rev_no)
    _audit_both(
        conn,
        t,
        at=at,
        action=f"offer.{new_status}",
        actor_user_id=user_id,
        session_id=session_id,
        detail={
            "decided_by": merchant_id,
            "terms_digest": terms_digest,
            "revision_no": rev_no,
            "child_offer_id": child["offer_id"] if child else None,
            "amendment_authoritative": decision == "accept",
            "payment_authorized": False,
        },
    )
    return {
        "offer_id": offer_id,
        "status": new_status,
        "revision_no": rev_no,
        "child": child,
        "amendment_authoritative": decision == "accept",
    }


# --- views -------------------------------------------------------------------------------
def offers_for(conn: Connection, merchant_id: str, at: datetime) -> list[dict[str, Any]]:
    rows = (
        conn.execute(
            select(offer, obligation.c.debtor, obligation.c.creditor, obligation.c.face_cents)
            .join(obligation, obligation.c.obligation_id == offer.c.obligation_id)
            .where(
                or_(
                    offer.c.proposer_merchant == merchant_id,
                    offer.c.counterparty_merchant == merchant_id,
                )
            )
            .order_by(offer.c.created_at.desc())
        )
        .mappings()
        .all()
    )
    out, records = [], {}
    for r in rows:
        oid = r["obligation_id"]
        if oid not in records:
            records[oid] = load_record(conn, oid).record
        out.append(offer_json(conn, r, records[oid], merchant_id, at))
    return out


def offer_json(conn: Connection, r: Any, record, viewer: str, at: datetime) -> dict[str, Any]:
    in_force = record.in_force_revision(at)
    status = effective_status(r, in_force.revision_no, at)
    decisions = conn.execute(
        select(
            offer_decision.c.merchant_id,
            offer_decision.c.decision,
            offer_decision.c.decided_at,
        )
        .where(offer_decision.c.offer_id == r["offer_id"])
        .order_by(offer_decision.c.id)
    ).mappings()
    t = _terms(r, r["debtor"], r["creditor"])
    my_decision = None
    by = []
    for d in decisions:
        by.append(
            {
                "merchant_id": d["merchant_id"],
                "decision": d["decision"],
                "decided_at": d["decided_at"].isoformat(),
            }
        )
        if d["merchant_id"] == viewer:
            my_decision = d["decision"]
    role = "debtor" if viewer == t.debtor else "creditor"
    can_decide = status == "proposed" and viewer == t.counterparty and my_decision is None
    return {
        "offer_id": t.offer_id,
        "obligation_id": t.obligation_id,
        "status": status,
        "my_role": role,
        "proposer": t.proposer,
        "counterparty": t.counterparty,
        "debtor": t.debtor,
        "creditor": t.creditor,
        "face_cents": int(r["face_cents"]),
        "cash_cents": t.cash_cents,
        "concession_cents": t.concession_cents,
        "currency": t.currency,
        "pay_by": t.due_at.isoformat(),
        "expires_at": t.expires_at.isoformat(),
        "base_revision_no": t.base_revision_no,
        "revision_no": t.revision_no,
        "terms_digest": r["terms_digest"],
        "parent_offer_id": r["parent_offer_id"],
        "source": r["source"],
        "message": r["message"],
        "created_at": r["created_at"].isoformat(),
        "decisions": by,
        "required_parties": sorted({t.debtor, t.creditor}),
        "my_decision": my_decision,
        "can_decide": can_decide,
        "can_withdraw": status == "proposed" and viewer == t.proposer,
        "amendment_authoritative": status == "agreed",
        "payment_authorized": False,
        "labels": ["AMENDMENT_ONLY", "PAYMENT_AUTHORIZATION_SEPARATE"],
    }
