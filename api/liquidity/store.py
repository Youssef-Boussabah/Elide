"""PostgreSQL persistence for capacity evidence and commitments.

`reserve_capacity` is the only way to commit cash for a plan. It takes a per-merchant
transaction-scoped advisory lock, reloads the evidence and all active commitments, runs
the pure `commit_capacity` check and inserts the commitment, all in one transaction. Two
concurrent plans therefore cannot both count the same cash: the second one waits for the
lock, then sees the first commitment.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Connection, Engine, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from api.domain.invoice import Provenance, SourceKind
from api.domain.money import Money
from api.liquidity.capacity import (
    CapacityError,
    CapacityEvidence,
    CapacitySnapshot,
    Commitment,
    DuplicateCommitmentError,
    EvidenceKind,
    commit_capacity,
    compute_capacity,
)
from api.liquidity.tables import capacity_commitment, capacity_evidence


class EvidenceNotFoundError(CapacityError):
    pass


def save_evidence(engine: Engine, ev: CapacityEvidence) -> None:
    """Insert immutable evidence. Re-saving identical evidence is a no-op."""
    values = {
        "evidence_id": ev.evidence_id,
        "merchant_id": ev.merchant_id,
        "currency": ev.currency,
        "verified_spendable_cents": ev.verified_spendable.cents,
        "protected_reserve_cents": ev.protected_reserve.cents,
        "held_cents": ev.held.cents,  # type: ignore[union-attr]
        "pending_cents": ev.pending.cents,  # type: ignore[union-attr]
        "kind": ev.kind.value,
        "observed_at": ev.observed_at,
        "valid_until": ev.valid_until,
        "provenance_source": ev.provenance.source.value,
        "provenance_reference": ev.provenance.reference,
        "provenance_recorded_at": ev.provenance.recorded_at,
        "provenance_evidence_ref": ev.provenance.evidence_ref,
    }
    with engine.begin() as conn:
        inserted = conn.execute(
            insert(capacity_evidence)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(capacity_evidence.c.evidence_id)
        ).first()
        if inserted is None and _load_evidence(conn, ev.evidence_id) != ev:
            raise CapacityError(f"evidence {ev.evidence_id} already exists with other values")


def _load_evidence(conn: Connection, evidence_id: str) -> CapacityEvidence:
    r = conn.execute(
        select(capacity_evidence).where(capacity_evidence.c.evidence_id == evidence_id)
    ).first()
    if r is None:
        raise EvidenceNotFoundError(f"evidence {evidence_id} not found")
    cur = r.currency
    return CapacityEvidence(
        evidence_id=r.evidence_id,
        merchant_id=r.merchant_id,
        verified_spendable=Money(int(r.verified_spendable_cents), cur),
        protected_reserve=Money(int(r.protected_reserve_cents), cur),
        held=Money(int(r.held_cents), cur),
        pending=Money(int(r.pending_cents), cur),
        kind=EvidenceKind(r.kind),
        observed_at=r.observed_at,
        valid_until=r.valid_until,
        provenance=Provenance(
            SourceKind(r.provenance_source),
            r.provenance_reference,
            r.provenance_recorded_at,
            r.provenance_evidence_ref,
        ),
    )


def _active_commitments(conn: Connection, merchant_id: str) -> list[Commitment]:
    rows = conn.execute(
        select(capacity_commitment).where(
            capacity_commitment.c.merchant_id == merchant_id,
            capacity_commitment.c.released_at.is_(None),
        )
    ).all()
    return [
        Commitment(
            r.commitment_id, r.merchant_id, r.plan_id, Money(int(r.amount_cents), r.currency)
        )
        for r in rows
    ]


def _lock_merchant(conn: Connection, merchant_id: str) -> None:
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:m, 0))"), {"m": merchant_id})


def reserve_capacity(
    engine: Engine,
    *,
    evidence_id: str,
    commitment: Commitment,
    at: datetime,
    allow_synthetic: bool = False,
) -> Commitment:
    try:
        with engine.begin() as conn:
            _lock_merchant(conn, commitment.merchant_id)
            ev = _load_evidence(conn, evidence_id)
            existing = _active_commitments(conn, commitment.merchant_id)
            commit_capacity(ev, existing, commitment, at=at, allow_synthetic=allow_synthetic)
            conn.execute(
                capacity_commitment.insert().values(
                    commitment_id=commitment.commitment_id,
                    merchant_id=commitment.merchant_id,
                    evidence_id=evidence_id,
                    plan_id=commitment.plan_id,
                    currency=commitment.amount.currency,
                    amount_cents=commitment.amount.cents,
                    created_at=at,
                )
            )
    except IntegrityError as exc:
        raise DuplicateCommitmentError(
            f"commitment {commitment.commitment_id} already exists"
        ) from exc
    return commitment


def release_capacity(engine: Engine, commitment_id: str, at: datetime) -> bool:
    """Release an active commitment once. Returns False if it was already released."""
    with engine.begin() as conn:
        res = conn.execute(
            capacity_commitment.update()
            .where(
                capacity_commitment.c.commitment_id == commitment_id,
                capacity_commitment.c.released_at.is_(None),
            )
            .values(released_at=at)
        )
        return res.rowcount == 1


def capacity_now(
    engine: Engine, *, evidence_id: str, at: datetime, allow_synthetic: bool = False
) -> CapacitySnapshot:
    with engine.connect() as conn:
        ev = _load_evidence(conn, evidence_id)
        return compute_capacity(
            ev, _active_commitments(conn, ev.merchant_id), at=at, allow_synthetic=allow_synthetic
        )


def active_commitment_total(engine: Engine, merchant_id: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                select(func.coalesce(func.sum(capacity_commitment.c.amount_cents), 0)).where(
                    capacity_commitment.c.merchant_id == merchant_id,
                    capacity_commitment.c.released_at.is_(None),
                )
            ).scalar_one()
        )
