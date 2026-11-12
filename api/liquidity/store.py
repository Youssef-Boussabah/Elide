"""PostgreSQL persistence for capacity evidence and commitments.

`reserve_capacity` is the only way to commit cash for a plan. It takes a per-merchant
transaction-scoped advisory lock, reloads the evidence and all active commitments, runs
the pure `commit_capacity` check and inserts the commitment, all in one transaction. Two
concurrent plans therefore cannot both count the same cash: the second one waits for the
lock, then sees the first commitment.

Evidence is immutable and round-trips exactly, including the receipt ids already
reflected in the observed balance. Evidence selection: the caller names an evidence id,
and the store refuses it (`SupersededEvidenceError`) if a newer observation of the same
merchant's balance in the same currency exists at the evaluated instant
(`select_current_evidence`). `current_evidence` returns the authoritative one.

Incoming receipts are supplied by the caller (they are not persisted yet); they are
de-duplicated and checked by the pure engine.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
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
    EvidenceConflictError,
    EvidenceKind,
    IncomingReceipt,
    check_not_superseded,
    commit_capacity,
    compute_capacity,
    select_current_evidence,
)
from api.liquidity.tables import capacity_commitment, capacity_evidence


class EvidenceNotFoundError(CapacityError):
    pass


def save_evidence(engine: Engine, ev: CapacityEvidence) -> None:
    """Insert immutable evidence. Re-saving identical evidence is a no-op; the same id with
    other content, or another observation of the same balance at the same instant, raises
    `EvidenceConflictError`."""
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
        "included_receipt_ids": sorted(ev.included_receipt_ids),
        "included_outflow_ids": sorted(ev.included_outflow_ids),
    }
    try:
        with engine.begin() as conn:
            inserted = conn.execute(
                insert(capacity_evidence)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["evidence_id"])
                .returning(capacity_evidence.c.evidence_id)
            ).first()
            if inserted is None and _load_evidence(conn, ev.evidence_id) != ev:
                raise EvidenceConflictError(
                    f"evidence {ev.evidence_id} already exists with other values"
                )
            if inserted is not None:
                # P5: a newer observation that lists settled outflows releases exactly those.
                release_reflected_outflows_in(conn, ev)
    except IntegrityError as exc:
        if "uq_capacity_evidence_observation" in str(exc.orig):
            raise EvidenceConflictError(
                f"{ev.merchant_id} already has a {ev.currency} observation at "
                f"{ev.observed_at.isoformat()}"
            ) from exc
        raise


def load_evidence(engine: Engine, evidence_id: str) -> CapacityEvidence:
    with engine.connect() as conn:
        return _load_evidence(conn, evidence_id)


def current_evidence(
    engine: Engine, merchant_id: str, currency: str, at: datetime
) -> CapacityEvidence | None:
    """The authoritative observation of this merchant's balance in `currency` at `at`."""
    with engine.connect() as conn:
        return _current_evidence(conn, merchant_id, currency, at)


def _current_evidence(
    conn: Connection, merchant_id: str, currency: str, at: datetime
) -> CapacityEvidence | None:
    c = capacity_evidence.c
    ids = conn.execute(
        select(c.evidence_id)
        .where(c.merchant_id == merchant_id, c.currency == currency, c.observed_at <= at)
        .order_by(c.observed_at.desc())
        .limit(2)
    ).scalars()
    candidates = [_load_evidence(conn, i) for i in list(ids)]
    return select_current_evidence(candidates, merchant_id=merchant_id, currency=currency, at=at)


def _require_current(conn: Connection, ev: CapacityEvidence, at: datetime) -> None:
    newest = _current_evidence(conn, ev.merchant_id, ev.currency, at)
    check_not_superseded(ev, [] if newest is None else [newest], at=at)


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
        included_receipt_ids=frozenset(r.included_receipt_ids),
        included_outflow_ids=frozenset(r.included_outflow_ids),
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
    receipts: Iterable[IncomingReceipt] = (),
    allow_synthetic: bool = False,
) -> Commitment:
    with engine.begin() as conn:
        return reserve_capacity_in(
            conn,
            evidence_id=evidence_id,
            commitment=commitment,
            at=at,
            receipts=receipts,
            allow_synthetic=allow_synthetic,
        )


def reserve_capacity_in(
    conn: Connection,
    *,
    evidence_id: str,
    commitment: Commitment,
    at: datetime,
    receipts: Iterable[IncomingReceipt] = (),
    allow_synthetic: bool = False,
    min_protected_reserve_cents: int = 0,
) -> Commitment:
    """`reserve_capacity` inside the caller's transaction (P4: a payment intent and its
    cash reservation commit or roll back together). Same lock, checks and insert.

    `min_protected_reserve_cents` is the merchant's current confirmed policy floor: the
    reserve that must hold is max(evidence reserve, policy floor), so a newer confirmed
    restriction can never be bypassed by an older plan or authorization."""
    try:
        with conn.begin_nested():
            _lock_merchant(conn, commitment.merchant_id)
            ev = _load_evidence(conn, evidence_id)
            _require_current(conn, ev, at)
            if min_protected_reserve_cents > ev.protected_reserve.cents:
                ev = replace(
                    ev,
                    protected_reserve=Money(min_protected_reserve_cents, ev.currency),
                )
            existing = _active_commitments(conn, commitment.merchant_id)
            commit_capacity(
                ev,
                existing,
                commitment,
                at=at,
                receipts=tuple(receipts),
                allow_synthetic=allow_synthetic,
            )
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


def release_capacity(
    engine: Engine, commitment_id: str, at: datetime, reason: str = "released"
) -> bool:
    """Release an active commitment once. Returns False if it was already released."""
    with engine.begin() as conn:
        return release_commitment_in(conn, commitment_id, at, reason)


def release_commitment_in(conn: Connection, commitment_id: str, at: datetime, reason: str) -> bool:
    """Release once, with the reason, in the caller's transaction (P5 lifecycle)."""
    res = conn.execute(
        capacity_commitment.update()
        .where(
            capacity_commitment.c.commitment_id == commitment_id,
            capacity_commitment.c.released_at.is_(None),
        )
        .values(released_at=at, release_reason=reason[:120])
    )
    return res.rowcount == 1


def settle_commitment_in(
    conn: Connection,
    *,
    reservation_id: str,
    settled_id: str,
    outflow_cents: int,
    at: datetime,
    reason: str,
) -> None:
    """A reserved payment was confirmed by the provider (ADR-0012 §4.5).

    In one transaction: the reservation (amount + fee ceiling) is released and a
    `settled_outflow` commitment of the actual outflow is inserted. The cash has left the
    account, but the newest balance evidence predates the payment, so the outflow stays
    committed until an observation that lists it in `included_outflow_ids` arrives
    (`release_reflected_outflows_in`). It is recorded even if it creates a deficit: it is a
    fact, not a request.
    """
    _lock_merchant_of(conn, reservation_id)
    r = conn.execute(
        select(capacity_commitment).where(capacity_commitment.c.commitment_id == reservation_id)
    ).first()
    if r is None or r.kind != "reservation":
        raise CapacityError(f"no reservation {reservation_id}")
    if outflow_cents <= 0:
        raise CapacityError("a settled outflow must be positive")
    if r.released_at is None:
        release_commitment_in(conn, reservation_id, at, reason)
    conn.execute(
        insert(capacity_commitment)
        .values(
            commitment_id=settled_id,
            merchant_id=r.merchant_id,
            evidence_id=r.evidence_id,
            plan_id=r.plan_id,
            currency=r.currency,
            amount_cents=outflow_cents,
            created_at=at,
            kind="settled_outflow",
        )
        .on_conflict_do_nothing(index_elements=["commitment_id"])
    )


def release_reflected_outflows_in(conn: Connection, ev: CapacityEvidence) -> list[str]:
    """Release settled outflows that `ev` explicitly reflects (and only those).

    An outflow is released only if the observation lists it and was made at or after the
    payment was recorded: a balance cannot reflect a payment that had not happened. A
    snapshot that merely changed releases nothing."""
    if not ev.included_outflow_ids:
        return []
    _lock_merchant(conn, ev.merchant_id)
    c = capacity_commitment.c
    rows = conn.execute(
        select(c.commitment_id).where(
            c.merchant_id == ev.merchant_id,
            c.kind == "settled_outflow",
            c.released_at.is_(None),
            c.commitment_id.in_(sorted(ev.included_outflow_ids)),
            c.created_at <= ev.observed_at,
        )
    ).scalars()
    released = []
    for cid in list(rows):
        if release_commitment_in(
            conn, cid, ev.observed_at, f"reflected_in_evidence:{ev.evidence_id}"
        ):
            released.append(cid)
    return released


def _lock_merchant_of(conn: Connection, commitment_id: str) -> None:
    m = conn.execute(
        select(capacity_commitment.c.merchant_id).where(
            capacity_commitment.c.commitment_id == commitment_id
        )
    ).scalar_one_or_none()
    if m is not None:
        _lock_merchant(conn, m)


def capacity_now(
    engine: Engine,
    *,
    evidence_id: str,
    at: datetime,
    receipts: Iterable[IncomingReceipt] = (),
    allow_synthetic: bool = False,
) -> CapacitySnapshot:
    with engine.connect() as conn:
        ev = _load_evidence(conn, evidence_id)
        _require_current(conn, ev, at)
        return compute_capacity(
            ev,
            _active_commitments(conn, ev.merchant_id),
            tuple(receipts),
            at=at,
            allow_synthetic=allow_synthetic,
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
