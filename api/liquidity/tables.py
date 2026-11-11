"""SQLAlchemy Core tables for capacity evidence and commitments (migrations 0002, 0003)."""

from sqlalchemy import (
    ARRAY,
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
    # Receipt ids already reflected in this observed balance (0003). Sorted, immutable.
    Column(
        "included_receipt_ids",
        ARRAY(String(80)),
        nullable=False,
        server_default=text("'{}'::varchar(80)[]"),
    ),
    CheckConstraint("currency IN ('USD', 'CAD')", name="ck_capacity_evidence_currency"),
    CheckConstraint(
        "verified_spendable_cents >= 0 AND protected_reserve_cents >= 0 "
        "AND held_cents >= 0 AND pending_cents >= 0",
        name="ck_capacity_evidence_nonnegative",
    ),
    CheckConstraint("valid_until > observed_at", name="ck_capacity_evidence_window"),
    CheckConstraint(
        "array_position(included_receipt_ids, NULL) IS NULL",
        name="ck_capacity_evidence_included_receipt_ids",
    ),
)
# One observation of a merchant's balance per currency and instant (supersession order).
Index(
    "uq_capacity_evidence_observation",
    capacity_evidence.c.merchant_id,
    capacity_evidence.c.currency,
    capacity_evidence.c.observed_at,
    unique=True,
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
