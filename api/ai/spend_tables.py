"""PostgreSQL LLM metering tables (migration `0004_merchant_platform`).

Amounts are integer micro-USD (1e-6 USD) so no float ever holds money. These are LOCAL
estimates over published prices, not provider billing.
"""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
    Table,
    Text,
    text,
)

from api.db import Base

metadata = Base.metadata

llm_budget = Table(
    "llm_budget",
    metadata,
    Column("budget_id", String(80), primary_key=True),
    Column("cap_micro_usd", BigInteger, nullable=False),
    Column("committed_micro_usd", BigInteger, nullable=False, server_default=text("0")),
    Column("reserved_micro_usd", BigInteger, nullable=False, server_default=text("0")),
    Column("overage_micro_usd", BigInteger, nullable=False, server_default=text("0")),
    Column("exceeded_at", DateTime(timezone=True)),
    Column("acknowledged_at", DateTime(timezone=True)),
    Column("acknowledged_reason", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    CheckConstraint("cap_micro_usd > 0", name="ck_llm_budget_cap"),
    CheckConstraint(
        "committed_micro_usd >= 0 AND reserved_micro_usd >= 0 AND overage_micro_usd >= 0",
        name="ck_llm_budget_nonnegative",
    ),
    CheckConstraint(
        "(acknowledged_at IS NULL) = (acknowledged_reason IS NULL)", name="ck_llm_budget_ack"
    ),
)

llm_spend_reservation = Table(
    "llm_spend_reservation",
    metadata,
    Column("reservation_id", String(36), primary_key=True),
    Column("budget_id", String(80), ForeignKey("llm_budget.budget_id"), nullable=False),
    Column("estimate_micro_usd", BigInteger, nullable=False),
    Column("status", String(20), nullable=False),
    Column("actual_micro_usd", BigInteger),
    Column("model", String(120)),
    Column("prompt_version", String(40)),
    Column("note", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("settled_at", DateTime(timezone=True)),
    CheckConstraint("estimate_micro_usd > 0", name="ck_llm_reservation_estimate"),
    CheckConstraint(
        "status IN ('reserved', 'committed', 'released')", name="ck_llm_reservation_status"
    ),
    CheckConstraint(
        "(status = 'committed') = (actual_micro_usd IS NOT NULL) "
        "AND (status = 'reserved') = (settled_at IS NULL)",
        name="ck_llm_reservation_settlement",
    ),
    CheckConstraint(
        "actual_micro_usd IS NULL OR actual_micro_usd >= 0", name="ck_llm_reservation_actual"
    ),
)
Index(
    "ix_llm_reservation_open",
    llm_spend_reservation.c.budget_id,
    postgresql_where=text("status = 'reserved'"),
)
