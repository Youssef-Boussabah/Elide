"""SQLAlchemy Core tables for capacity evidence and commitments (see migration 0002)."""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    String,
    Table,
    Text,
    text,
)

from api.db import Base

metadata = Base.metadata

capacity_evidence = Table(
    "capacity_evidence",
    metadata,
    Column("evidence_id", String(80), primary_key=True),
    Column("merchant_id", String(80), nullable=False),
    Column("currency", String(3), nullable=False),
    Column("verified_spendable_cents", BigInteger, nullable=False),
    Column("protected_reserve_cents", BigInteger, nullable=False),
    Column("held_cents", BigInteger, nullable=False),
    Column("pending_cents", BigInteger, nullable=False),
    Column("kind", String(40), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("valid_until", DateTime(timezone=True), nullable=False),
    Column("provenance_source", String(40), nullable=False),
    Column("provenance_reference", Text, nullable=False),
    Column("provenance_recorded_at", DateTime(timezone=True), nullable=False),
    Column("provenance_evidence_ref", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    CheckConstraint("currency IN ('USD', 'CAD')", name="ck_capacity_evidence_currency"),
    CheckConstraint(
        "verified_spendable_cents >= 0 AND protected_reserve_cents >= 0 "
        "AND held_cents >= 0 AND pending_cents >= 0",
        name="ck_capacity_evidence_nonnegative",
    ),
    CheckConstraint("valid_until > observed_at", name="ck_capacity_evidence_window"),
)

capacity_commitment = Table(
    "capacity_commitment",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("commitment_id", String(80), nullable=False, unique=True),
    Column("merchant_id", String(80), nullable=False),
    Column(
        "evidence_id",
        String(80),
        ForeignKey("capacity_evidence.evidence_id"),
        nullable=False,
    ),
    Column("plan_id", String(80), nullable=False),
    Column("currency", String(3), nullable=False),
    Column("amount_cents", BigInteger, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("released_at", DateTime(timezone=True)),
    CheckConstraint("currency = 'USD'", name="ck_capacity_commitment_usd"),
    CheckConstraint("amount_cents > 0", name="ck_capacity_commitment_positive"),
    CheckConstraint(
        "released_at IS NULL OR released_at >= created_at", name="ck_capacity_commitment_release"
    ),
)
Index(
    "ix_capacity_commitment_active",
    capacity_commitment.c.merchant_id,
    postgresql_where=text("released_at IS NULL"),
)
