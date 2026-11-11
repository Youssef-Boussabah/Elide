"""P1 review remediation: evidence receipt identities, supersession order, reversal evidence.

- capacity_evidence.included_receipt_ids (text[], NOT NULL, default '{}', no NULLs): the
  receipts already reflected in an observed balance. 0002 did not store them, so they
  were lost on save. The table stays append-only (UPDATE refused), so the set is
  immutable with its row. Rows that existed before 0003 keep '{}', the only value that
  was ever stored for them.
- uq_capacity_evidence_observation: one observation per (merchant, currency, instant), so
  "the latest observation" is always well defined.
- journal_entry.evidence_class gains 'manual_adjustment' (local corrections).
- uq_journal_entry_provider_event now also covers reversals: one provider refund or
  dispute can back only one reversal entry.

0002 is unchanged (it was already applied); this is a new revision.

Revision ID: 0003_review_remediation
Revises: 0002_financial_kernel
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_review_remediation"
down_revision: str | None = "0002_financial_kernel"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_CLASSES = "evidence_class IN ('sandbox_verified', 'merchant_attested', 'synthetic')"
NEW_CLASSES = (
    "evidence_class IN ('sandbox_verified', 'merchant_attested', 'synthetic', 'manual_adjustment')"
)
PROVIDER_EVENT_COLUMNS = [
    "provider",
    "provider_environment",
    "provider_resource_type",
    "provider_resource_id",
    "kind",
]


def upgrade() -> None:
    # Metadata-only change in PostgreSQL 11+: no row rewrite, append-only triggers not fired.
    op.add_column(
        "capacity_evidence",
        sa.Column(
            "included_receipt_ids",
            postgresql.ARRAY(sa.String(80)),
            nullable=False,
            server_default=sa.text("'{}'::varchar(80)[]"),
        ),
    )
    op.create_check_constraint(
        "ck_capacity_evidence_included_receipt_ids",
        "capacity_evidence",
        "array_position(included_receipt_ids, NULL) IS NULL",
    )
    op.create_index(
        "uq_capacity_evidence_observation",
        "capacity_evidence",
        ["merchant_id", "currency", "observed_at"],
        unique=True,
    )

    op.drop_constraint("ck_journal_entry_evidence_class", "journal_entry", type_="check")
    op.create_check_constraint("ck_journal_entry_evidence_class", "journal_entry", NEW_CLASSES)

    op.drop_index("uq_journal_entry_provider_event", table_name="journal_entry")
    op.create_index(
        "uq_journal_entry_provider_event",
        "journal_entry",
        PROVIDER_EVENT_COLUMNS,
        unique=True,
        postgresql_where=sa.text("provider IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_journal_entry_provider_event", table_name="journal_entry")
    op.create_index(
        "uq_journal_entry_provider_event",
        "journal_entry",
        PROVIDER_EVENT_COLUMNS,
        unique=True,
        postgresql_where=sa.text("provider IS NOT NULL AND kind <> 'reversal'"),
    )
    # Fails (on purpose) if manual adjustments exist: they cannot be relabeled silently.
    op.drop_constraint("ck_journal_entry_evidence_class", "journal_entry", type_="check")
    op.create_check_constraint("ck_journal_entry_evidence_class", "journal_entry", OLD_CLASSES)

    op.drop_index("uq_capacity_evidence_observation", table_name="capacity_evidence")
    op.drop_constraint(
        "ck_capacity_evidence_included_receipt_ids", "capacity_evidence", type_="check"
    )
    op.drop_column("capacity_evidence", "included_receipt_ids")
