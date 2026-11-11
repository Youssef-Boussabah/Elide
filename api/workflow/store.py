"""Persistence of P1 invoice records (obligation + immutable revisions).

Loading rebuilds the domain `InvoiceRecord` through `InvoiceRecord.open` and
`add_revision`, so every P1 invariant (both-party authorization, offer lifecycle,
over-settlement) is re-checked on every read, not only on write.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, insert, select

from api.domain.invoice import (
    DisputeStatus,
    InvoiceRecord,
    Obligation,
    ObligationRevision,
    Provenance,
    ProviderRef,
    RevisionStatus,
    SourceKind,
)
from api.domain.money import USD, Money
from api.planning.network import FeeStatus, FeeTerms
from api.workflow.tables import (
    network_participation,
    obligation,
    obligation_revision,
)


class WorkflowError(Exception):
    """Base for workflow errors mapped to HTTP responses (status, code)."""

    status = 400
    code = "WORKFLOW_ERROR"

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None):
        super().__init__(message)
        if code:
            self.code = code
        if status:
            self.status = status


class NotVisibleError(WorkflowError):
    status, code = 404, "NOT_FOUND"


class ConflictError(WorkflowError):
    status, code = 409, "CONFLICT"


class InvalidRequestError(WorkflowError):
    status, code = 422, "INVALID_REQUEST"


@dataclass(frozen=True, slots=True)
class StoredInvoice:
    record: InvoiceRecord
    fee: FeeTerms
    network_id: str
    provider_ref: str | None


def _prov(row: Any) -> Provenance:
    return Provenance(
        SourceKind(row["provenance_source"]),
        row["provenance_reference"],
        row["provenance_recorded_at"],
        row["provenance_evidence_ref"],
    )


def _prov_cols(p: Provenance) -> dict[str, Any]:
    return {
        "provenance_source": p.source.value,
        "provenance_reference": p.reference,
        "provenance_recorded_at": p.recorded_at,
        "provenance_evidence_ref": p.evidence_ref,
    }


def revision_digest(rev: ObligationRevision) -> str:
    blob = {
        "obligation_id": rev.obligation_id,
        "revision_no": rev.revision_no,
        "status": rev.status.value,
        "due_at": rev.due_at.isoformat(),
        "business_tz": rev.business_tz,
        "concession_cents": rev.concession.cents,
        "currency": rev.concession.currency,
        "created_at": rev.created_at.isoformat(),
        "expires_at": rev.expires_at.isoformat() if rev.expires_at else None,
        "authorized_by": sorted(rev.authorized_by),
        "supersedes": rev.supersedes,
        "dispute": rev.dispute.value,
    }
    return hashlib.sha256(json.dumps(blob, sort_keys=True).encode()).hexdigest()


def insert_revision(conn: Connection, rev: ObligationRevision) -> None:
    conn.execute(
        insert(obligation_revision).values(
            obligation_id=rev.obligation_id,
            revision_no=rev.revision_no,
            status=rev.status.value,
            due_at=rev.due_at,
            business_tz=rev.business_tz,
            concession_cents=rev.concession.cents,
            created_at=rev.created_at,
            expires_at=rev.expires_at,
            authorized_by=sorted(rev.authorized_by),
            supersedes=rev.supersedes,
            dispute=rev.dispute.value,
            content_sha256=revision_digest(rev),
            **_prov_cols(rev.provenance),
        )
    )


def save_record(
    conn: Connection, network_id: str, record: InvoiceRecord, fee: FeeTerms | None = None
) -> None:
    fee = fee or FeeTerms()
    ob = record.obligation
    pr = ob.provider_ref
    conn.execute(
        insert(obligation).values(
            obligation_id=ob.obligation_id,
            network_id=network_id,
            debtor=ob.debtor,
            creditor=ob.creditor,
            currency=ob.currency,
            face_cents=ob.face.cents,
            issued_at=ob.issued_at,
            provider=pr.provider if pr else None,
            provider_environment=pr.environment if pr else None,
            provider_resource_id=pr.resource_id if pr else None,
            fee_status=fee.status.value,
            payer_fee_cents=fee.payer_fee.cents,
            payee_fee_cents=fee.payee_fee.cents,
            fee_source=fee.source[:200],
            **_prov_cols(ob.provenance),
        )
    )
    for rev in record.revisions:
        insert_revision(conn, rev)


def _revision(row: Any, currency: str) -> ObligationRevision:
    return ObligationRevision(
        obligation_id=row["obligation_id"],
        revision_no=row["revision_no"],
        status=RevisionStatus(row["status"]),
        due_at=row["due_at"],
        business_tz=row["business_tz"],
        concession=Money(int(row["concession_cents"]), currency),
        created_at=row["created_at"],
        provenance=_prov(row),
        dispute=DisputeStatus(row["dispute"]),
        expires_at=row["expires_at"],
        authorized_by=frozenset(row["authorized_by"]),
        supersedes=row["supersedes"],
    )


def load_record(conn: Connection, obligation_id: str, *, lock: bool = False) -> StoredInvoice:
    q = select(obligation).where(obligation.c.obligation_id == obligation_id)
    if lock:
        q = q.with_for_update()
    ob = conn.execute(q).mappings().one_or_none()
    if ob is None:
        raise NotVisibleError("not found")
    cur = ob["currency"]
    provider_ref = (
        ProviderRef(ob["provider"], ob["provider_environment"], ob["provider_resource_id"])
        if ob["provider"]
        else None
    )
    domain_ob = Obligation(
        ob["obligation_id"],
        ob["debtor"],
        ob["creditor"],
        Money(int(ob["face_cents"]), cur),
        ob["issued_at"],
        _prov(ob),
        provider_ref,
    )
    revs = [
        _revision(r, cur)
        for r in conn.execute(
            select(obligation_revision)
            .where(obligation_revision.c.obligation_id == obligation_id)
            .order_by(obligation_revision.c.revision_no)
        ).mappings()
    ]
    record = InvoiceRecord.open(domain_ob, revs[0])
    for r in revs[1:]:
        record = record.add_revision(r)  # re-validates every P1 rule
    fee = FeeTerms(
        payer_fee=Money(int(ob["payer_fee_cents"]), USD),
        payee_fee=Money(int(ob["payee_fee_cents"]), USD),
        status=FeeStatus(ob["fee_status"]),
        source=ob["fee_source"],
    )
    ref = provider_ref.resource_id if provider_ref else None
    return StoredInvoice(record, fee, ob["network_id"], ref)


def require_party(conn: Connection, obligation_id: str, merchant_id: str) -> StoredInvoice:
    """The invoice if `merchant_id` is its debtor or creditor; otherwise 404 (same as
    a missing invoice, so existence is not revealed)."""
    row = conn.execute(
        select(obligation.c.debtor, obligation.c.creditor).where(
            obligation.c.obligation_id == obligation_id
        )
    ).one_or_none()
    if row is None or merchant_id not in (row.debtor, row.creditor):
        raise NotVisibleError("not found")
    return load_record(conn, obligation_id)


def active_participants(conn: Connection, network_id: str) -> dict[str, bool]:
    """merchant_id -> share_aggregates, for merchants currently in the network."""
    rows = conn.execute(
        select(network_participation.c.merchant_id, network_participation.c.share_aggregates)
        .where(
            network_participation.c.network_id == network_id,
            network_participation.c.left_at.is_(None),
        )
        .order_by(network_participation.c.merchant_id)
    ).all()
    return {r.merchant_id: bool(r.share_aggregates) for r in rows}


def networks_of(conn: Connection, merchant_id: str) -> list[str]:
    return list(
        conn.execute(
            select(network_participation.c.network_id)
            .where(
                network_participation.c.merchant_id == merchant_id,
                network_participation.c.left_at.is_(None),
            )
            .order_by(network_participation.c.network_id)
        ).scalars()
    )


def network_obligation_ids(conn: Connection, network_id: str) -> list[str]:
    return list(
        conn.execute(
            select(obligation.c.obligation_id)
            .where(obligation.c.network_id == network_id)
            .order_by(obligation.c.obligation_id)
        ).scalars()
    )


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None
