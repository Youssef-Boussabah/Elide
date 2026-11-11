"""Financial kernel: journal, provider event inbox, capacity evidence and commitments.

Database-level guards (defense in depth behind the Python checks):
- journal_entry, journal_posting, provider_event_inbox and capacity_evidence are
  append-only: UPDATE, DELETE and TRUNCATE raise.
- capacity_commitment rows cannot be deleted; the only allowed update is setting
  released_at once.
- A deferred constraint trigger requires every journal entry to have >= 2 postings that
  sum to zero per currency at COMMIT.

Revision ID: 0002_financial_kernel
Revises: 0001_baseline
Create Date: 2026-11-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_financial_kernel"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY = ("journal_entry", "journal_posting", "provider_event_inbox", "capacity_evidence")


def upgrade() -> None:
    op.create_table(
        "journal_entry",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("entity_id", sa.String(80), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("evidence_class", sa.String(40), nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("provider", sa.String(40)),
        sa.Column("provider_environment", sa.String(40)),
        sa.Column("provider_resource_type", sa.String(40)),
        sa.Column("provider_resource_id", sa.String(120)),
        sa.Column("reverses_entry_id", sa.BigInteger, sa.ForeignKey("journal_entry.id")),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_journal_entry_idempotency_key"),
        sa.UniqueConstraint("reverses_entry_id", name="uq_journal_entry_reverses"),
        sa.CheckConstraint(
            "evidence_class IN ('sandbox_verified', 'merchant_attested', 'synthetic')",
            name="ck_journal_entry_evidence_class",
        ),
        sa.CheckConstraint(
            "(provider IS NULL) = (provider_resource_id IS NULL)",
            name="ck_journal_entry_provider_ref",
        ),
        sa.CheckConstraint(
            "(kind = 'reversal') = (reverses_entry_id IS NOT NULL)",
            name="ck_journal_entry_reversal_link",
        ),
    )
    op.create_index(
        "uq_journal_entry_provider_event",
        "journal_entry",
        [
            "provider",
            "provider_environment",
            "provider_resource_type",
            "provider_resource_id",
            "kind",
        ],
        unique=True,
        postgresql_where=sa.text("provider IS NOT NULL AND kind <> 'reversal'"),
    )
    op.create_table(
        "journal_posting",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("entry_id", sa.BigInteger, sa.ForeignKey("journal_entry.id"), nullable=False),
        sa.Column("line_no", sa.SmallInteger, nullable=False),
        sa.Column("account", sa.String(160), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("amount_cents", sa.BigInteger, nullable=False),
        sa.UniqueConstraint("entry_id", "line_no", name="uq_journal_posting_line"),
        sa.CheckConstraint("currency IN ('USD', 'CAD')", name="ck_journal_posting_currency"),
        sa.CheckConstraint("amount_cents <> 0", name="ck_journal_posting_nonzero"),
    )
    op.create_index("ix_journal_posting_account", "journal_posting", ["account", "currency"])
    op.create_index("ix_journal_posting_entry", "journal_posting", ["entry_id"])

    op.create_table(
        "provider_event_inbox",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("environment", sa.String(40), nullable=False),
        sa.Column("provider_event_id", sa.String(120), nullable=False),
        sa.Column("event_type", sa.String(120), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "provider", "environment", "provider_event_id", name="uq_provider_event_inbox_event"
        ),
    )

    op.create_table(
        "capacity_evidence",
        sa.Column("evidence_id", sa.String(80), primary_key=True),
        sa.Column("merchant_id", sa.String(80), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("verified_spendable_cents", sa.BigInteger, nullable=False),
        sa.Column("protected_reserve_cents", sa.BigInteger, nullable=False),
        sa.Column("held_cents", sa.BigInteger, nullable=False),
        sa.Column("pending_cents", sa.BigInteger, nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provenance_source", sa.String(40), nullable=False),
        sa.Column("provenance_reference", sa.Text, nullable=False),
        sa.Column("provenance_recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provenance_evidence_ref", sa.Text),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("currency IN ('USD', 'CAD')", name="ck_capacity_evidence_currency"),
        sa.CheckConstraint(
            "verified_spendable_cents >= 0 AND protected_reserve_cents >= 0 "
            "AND held_cents >= 0 AND pending_cents >= 0",
            name="ck_capacity_evidence_nonnegative",
        ),
        sa.CheckConstraint("valid_until > observed_at", name="ck_capacity_evidence_window"),
    )
    op.create_table(
        "capacity_commitment",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("commitment_id", sa.String(80), nullable=False),
        sa.Column("merchant_id", sa.String(80), nullable=False),
        sa.Column(
            "evidence_id",
            sa.String(80),
            sa.ForeignKey("capacity_evidence.evidence_id"),
            nullable=False,
        ),
        sa.Column("plan_id", sa.String(80), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("amount_cents", sa.BigInteger, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("commitment_id", name="uq_capacity_commitment_id"),
        sa.CheckConstraint("currency = 'USD'", name="ck_capacity_commitment_usd"),
        sa.CheckConstraint("amount_cents > 0", name="ck_capacity_commitment_positive"),
        sa.CheckConstraint(
            "released_at IS NULL OR released_at >= created_at",
            name="ck_capacity_commitment_release",
        ),
    )
    op.create_index(
        "ix_capacity_commitment_active",
        "capacity_commitment",
        ["merchant_id"],
        postgresql_where=sa.text("released_at IS NULL"),
    )

    # --- database guards ------------------------------------------------------------
    op.execute(
        """
        CREATE FUNCTION elide_forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'table % is append-only (% refused)', TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'restrict_violation';
        END $$;
        """
    )
    for table in APPEND_ONLY:
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION elide_forbid_mutation()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION elide_forbid_mutation()"
        )

    op.execute(
        """
        CREATE FUNCTION elide_commitment_release_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'capacity_commitment rows cannot be deleted'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF OLD.released_at IS NOT NULL
               OR NEW.released_at IS NULL
               OR (NEW.id, NEW.commitment_id, NEW.merchant_id, NEW.evidence_id, NEW.plan_id,
                   NEW.currency, NEW.amount_cents, NEW.created_at)
                  IS DISTINCT FROM
                  (OLD.id, OLD.commitment_id, OLD.merchant_id, OLD.evidence_id, OLD.plan_id,
                   OLD.currency, OLD.amount_cents, OLD.created_at) THEN
                RAISE EXCEPTION 'capacity_commitment may only be released once'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END $$;
        """
    )
    op.execute(
        "CREATE TRIGGER capacity_commitment_release_only BEFORE UPDATE OR DELETE "
        "ON capacity_commitment FOR EACH ROW EXECUTE FUNCTION elide_commitment_release_only()"
    )
    op.execute(
        "CREATE TRIGGER capacity_commitment_no_truncate BEFORE TRUNCATE ON capacity_commitment "
        "FOR EACH STATEMENT EXECUTE FUNCTION elide_forbid_mutation()"
    )

    op.execute(
        """
        CREATE FUNCTION elide_assert_entry_balanced(eid bigint) RETURNS void
        LANGUAGE plpgsql AS $$
        DECLARE
            n integer;
            bad text;
        BEGIN
            SELECT count(*) INTO n FROM journal_posting WHERE entry_id = eid;
            IF n < 2 THEN
                RAISE EXCEPTION 'journal entry % has % postings (needs >= 2)', eid, n
                    USING ERRCODE = 'check_violation';
            END IF;
            SELECT currency INTO bad FROM journal_posting WHERE entry_id = eid
                GROUP BY currency HAVING sum(amount_cents) <> 0 LIMIT 1;
            IF bad IS NOT NULL THEN
                RAISE EXCEPTION 'journal entry % is unbalanced in %', eid, bad
                    USING ERRCODE = 'check_violation';
            END IF;
        END $$;
        """
    )
    op.execute(
        """
        CREATE FUNCTION elide_check_posting_entry() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM elide_assert_entry_balanced(NEW.entry_id);
            RETURN NULL;
        END $$;
        """
    )
    op.execute(
        """
        CREATE FUNCTION elide_check_new_entry() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM elide_assert_entry_balanced(NEW.id);
            RETURN NULL;
        END $$;
        """
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER journal_posting_balanced AFTER INSERT ON journal_posting "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION elide_check_posting_entry()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER journal_entry_has_postings AFTER INSERT ON journal_entry "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION elide_check_new_entry()"
    )


def downgrade() -> None:
    op.drop_table("capacity_commitment")
    op.drop_table("capacity_evidence")
    op.drop_table("provider_event_inbox")
    op.drop_table("journal_posting")
    op.drop_table("journal_entry")
    op.execute("DROP FUNCTION elide_check_new_entry()")
    op.execute("DROP FUNCTION elide_check_posting_entry()")
    op.execute("DROP FUNCTION elide_assert_entry_balanced(bigint)")
    op.execute("DROP FUNCTION elide_commitment_release_only()")
    op.execute("DROP FUNCTION elide_forbid_mutation()")
