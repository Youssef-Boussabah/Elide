"""P5 final review: settlement verification attestation and corrected payer provenance.

ADR-0012 §17 (owner final architectural review, 2026-11-12).

- `settlement_verification` (append-only): the independent checker's canonical document and
  its SHA-256, checker version and purpose. A deferred constraint trigger requires every
  `provider_settlement` inserted from now on to have a passing `booking` verification in
  the same transaction.
- `provider_settlement.check_sha256`: was always a 64-zero placeholder. The column becomes
  nullable and a NOT VALID CHECK requires NULL on every new row; pre-0008 rows are
  immutable and keep the placeholder, which an `evidence_correction` labels as not a digest.
- `evidence_correction` (append-only): corrected provenance for immutable records. This
  migration appends, for rows that already exist:
    * each pre-0008 `provider_payment_sent` journal entry: its credit to
      `paypal_payments_sent` (the payer's own cash outflow) is **unverified**; PayPal's
      invoice and capture do not identify the paying account;
    * each pre-0008 settlement: `payer_merchant` is the debtor of the discharged
      obligation, not an identified paying account; `check_sha256` is a placeholder;
    * each existing settled-outflow commitment: a presumed outflow held conservatively.
- `payment_source_attestation` (append-only, one per capture): the payer merchant's own
  statement of the payment source (own PayPal account or a third party).

Nothing existing is updated or deleted; append-only and guard triggers are unchanged.

Revision ID: 0008_p5_verification_attribution
Revises: 0007_p5_request_identity
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_p5_verification_attribution"
down_revision: str | None = "0007_p5_request_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BY = "migration:0008_p5_verification_attribution"
NEW_TABLES = ("settlement_verification", "evidence_correction", "payment_source_attestation")

INSERT_CORRECTION = (
    "INSERT INTO evidence_correction (subject_table, subject_id, aspect, recorded_as, "
    "corrected_to, reason, corrected_by, corrected_at) SELECT CAST(:t AS text), x.*, "
    "CAST(:by AS text), now() FROM ({select_sql}) AS x"
)
CORRECTIONS = [
    (
        "journal_entry",
        "SELECT id::text, 'payer_source_debit', 'evidence_class=' || evidence_class, "
        "'unverified', CAST(:r AS text) FROM journal_entry WHERE kind = 'provider_payment_sent'",
        "The entry credits paypal_payments_sent (the payer's own cash outflow) as "
        "sandbox_verified, but PayPal's invoice and capture do not identify the paying "
        "account. The discharge of the payable and the concession are provider-confirmed; "
        "the payer's source-account debit is not verified (ADR-0012 §17.2).",
    ),
    (
        "provider_settlement",
        "SELECT capture_id, 'payer_merchant', 'payer_merchant=' || payer_merchant, "
        "'debtor_of_discharged_obligation', CAST(:r AS text) FROM provider_settlement",
        "payer_merchant is the debtor whose obligation the capture discharged; PayPal does "
        "not identify the paying account, so it is not a verified payer identity.",
    ),
    (
        "provider_settlement",
        "SELECT capture_id, 'check_sha256', 'check_sha256=' || check_sha256, "
        "'placeholder_not_a_digest', CAST(:r AS text) FROM provider_settlement "
        "WHERE check_sha256 = repeat('0', 64)",
        "A constant placeholder written before 0008; no verification digest was computed. "
        "Verification attestations live in settlement_verification.",
    ),
    (
        "capacity_commitment",
        "SELECT commitment_id, 'outflow_basis', 'kind=settled_outflow', "
        "'presumed_outflow_unverified_source', CAST(:r AS text) FROM capacity_commitment "
        "WHERE kind = 'settled_outflow'",
        "The outflow is presumed from the provider-confirmed settlement; the payer's "
        "source-account debit is not verified. It is held conservatively until newer "
        "balance evidence lists it.",
    ),
]


def upgrade() -> None:
    op.create_table(
        "settlement_verification",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "capture_id",
            sa.String(80),
            sa.ForeignKey("provider_settlement.capture_id"),
            nullable=False,
        ),
        sa.Column(
            "intent_id", sa.String(40), sa.ForeignKey("payment_intent.intent_id"), nullable=False
        ),
        sa.Column("checker_version", sa.String(60), nullable=False),
        sa.Column("purpose", sa.String(20), nullable=False),
        sa.Column("verdict", sa.String(10), nullable=False),
        sa.Column("problems", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("document", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("document_sha256", sa.String(64), nullable=False),
        sa.Column("invoice_body_sha256", sa.String(64), nullable=False),
        sa.Column("capture_body_sha256", sa.String(64), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(80), nullable=False),
        sa.CheckConstraint(
            "purpose IN ('booking', 'reverification')", name="ck_settlement_verification_purpose"
        ),
        sa.CheckConstraint(
            "verdict IN ('passed', 'failed')", name="ck_settlement_verification_verdict"
        ),
        sa.CheckConstraint(
            "(verdict = 'passed') = (problems = '[]'::jsonb)", name="ck_settlement_verification_ok"
        ),
        sa.CheckConstraint(
            "document_sha256 ~ '^[0-9a-f]{64}$'", name="ck_settlement_verification_digest"
        ),
    )
    op.create_index(
        "uq_settlement_verification_booking",
        "settlement_verification",
        ["capture_id"],
        unique=True,
        postgresql_where=sa.text("purpose = 'booking'"),
    )
    op.create_table(
        "evidence_correction",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("subject_table", sa.String(40), nullable=False),
        sa.Column("subject_id", sa.String(120), nullable=False),
        sa.Column("aspect", sa.String(40), nullable=False),
        sa.Column("recorded_as", sa.Text(), nullable=False),
        sa.Column("corrected_to", sa.String(60), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("corrected_by", sa.String(80), nullable=False),
        sa.Column("corrected_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("subject_table", "subject_id", "aspect", name="uq_evidence_correction"),
        sa.CheckConstraint(
            "subject_table IN ('journal_entry', 'provider_settlement', 'capacity_commitment')",
            name="ck_evidence_correction_subject",
        ),
    )
    op.create_table(
        "payment_source_attestation",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "capture_id",
            sa.String(80),
            sa.ForeignKey("provider_settlement.capture_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "intent_id", sa.String(40), sa.ForeignKey("payment_intent.intent_id"), nullable=False
        ),
        sa.Column(
            "merchant_id", sa.String(80), sa.ForeignKey("merchant_org.merchant_id"), nullable=False
        ),
        sa.Column("source", sa.String(30), nullable=False),
        sa.Column("attested_by", sa.String(80), nullable=False),
        sa.Column("attested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("journal_entry_id", sa.BigInteger(), sa.ForeignKey("journal_entry.id")),
        sa.CheckConstraint(
            "source IN ('own_paypal_account', 'third_party')",
            name="ck_payment_source_attestation_source",
        ),
    )
    for table in NEW_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION elide_forbid_mutation()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION elide_forbid_mutation()"
        )

    # A verification belongs to its settlement's intent.
    op.execute(
        """
        CREATE FUNCTION elide_settlement_verification_intent() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM provider_settlement s
                           WHERE s.capture_id = NEW.capture_id AND s.intent_id = NEW.intent_id)
            THEN
                RAISE EXCEPTION 'settlement_verification intent differs from the settlement'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END $$;
        """
    )
    op.execute(
        "CREATE TRIGGER settlement_verification_intent BEFORE INSERT ON settlement_verification "
        "FOR EACH ROW EXECUTE FUNCTION elide_settlement_verification_intent()"
    )
    # Every settlement inserted from now on needs a passing booking verification by commit.
    op.execute(
        """
        CREATE FUNCTION elide_settlement_requires_verification() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM settlement_verification v
                           WHERE v.capture_id = NEW.capture_id AND v.purpose = 'booking'
                             AND v.verdict = 'passed') THEN
                RAISE EXCEPTION 'provider_settlement % has no passing booking verification',
                    NEW.capture_id USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NULL;
        END $$;
        """
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER provider_settlement_requires_verification "
        "AFTER INSERT ON provider_settlement DEFERRABLE INITIALLY DEFERRED "
        "FOR EACH ROW EXECUTE FUNCTION elide_settlement_requires_verification()"
    )

    op.alter_column("provider_settlement", "check_sha256", nullable=True)
    op.execute(
        "ALTER TABLE provider_settlement ADD CONSTRAINT ck_provider_settlement_check_legacy "
        "CHECK (check_sha256 IS NULL) NOT VALID"
    )
    op.execute(
        "COMMENT ON COLUMN provider_settlement.check_sha256 IS 'LEGACY: pre-0008 rows hold a "
        "64-zero placeholder, never a digest; new rows are NULL. See settlement_verification.'"
    )
    op.execute(
        "COMMENT ON COLUMN provider_settlement.payer_merchant IS 'The debtor of the obligation "
        "the capture discharged; NOT an identified paying PayPal account (ADR-0012 §17.2).'"
    )

    bind = op.get_bind()
    for subject, select_sql, reason in CORRECTIONS:
        # select_sql is one of the constant statements above, never external input.
        sql = INSERT_CORRECTION.format(select_sql=select_sql)
        bind.execute(sa.text(sql), {"t": subject, "r": reason, "by": BY})


def downgrade() -> None:
    op.execute(
        "ALTER TABLE provider_settlement DROP CONSTRAINT ck_provider_settlement_check_legacy"
    )
    op.execute("COMMENT ON COLUMN provider_settlement.check_sha256 IS NULL")
    op.execute("COMMENT ON COLUMN provider_settlement.payer_merchant IS NULL")
    op.alter_column("provider_settlement", "check_sha256", nullable=False)
    op.execute("DROP TRIGGER provider_settlement_requires_verification ON provider_settlement")
    op.execute("DROP FUNCTION elide_settlement_requires_verification()")
    op.execute("DROP TRIGGER settlement_verification_intent ON settlement_verification")
    op.execute("DROP FUNCTION elide_settlement_verification_intent()")
    for table in reversed(NEW_TABLES):
        op.execute(f"DROP TRIGGER {table}_append_only ON {table}")
        op.execute(f"DROP TRIGGER {table}_no_truncate ON {table}")
        op.drop_table(table)
