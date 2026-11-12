"""P4 review remediation: condition-bound authorizations, durable plan reuse, synthetic guard.

- payment_authorization.conditions / conditions_digest: the material conditions an
  approval was given under (confirmed policy version and digest, effective reserve,
  participation, evidence basis, fee terms, invoice revision content, payee, currency,
  due date, open balance). Existing rows get 'legacy-unbound', which never matches a real
  digest, so they require re-approval.
- plan_snapshot.economic_digest (+ index): digest of the economic inputs without the
  clock; an unchanged economy reuses one durable snapshot instead of writing a new one.
- payment_intent.evidence_kind (+ CHECKs): a synthetic intent can never leave
  'awaiting_payer_action' / 'cancelled'; a trigger keeps the outbox message of a synthetic
  intent 'held'. Simulation data can never become payment authority.

Adding columns with constant defaults is metadata-only in PostgreSQL 11+: the append-only
row triggers of 0004 are not fired and no row is rewritten.

Revision ID: 0005_p4_review_remediation
Revises: 0004_merchant_platform
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_p4_review_remediation"
down_revision: str | None = "0004_merchant_platform"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "payment_authorization",
        sa.Column(
            "conditions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "payment_authorization",
        sa.Column(
            "conditions_digest",
            sa.String(length=64),
            server_default=sa.text("'legacy-unbound'"),
            nullable=False,
        ),
    )
    op.add_column(
        "payment_intent",
        sa.Column(
            "evidence_kind",
            sa.String(length=20),
            server_default=sa.text("'synthetic'"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_payment_intent_evidence_kind",
        "payment_intent",
        "evidence_kind IN ('provider_verified', 'merchant_attested', 'synthetic')",
    )
    op.create_check_constraint(
        "ck_payment_intent_synthetic_never_executes",
        "payment_intent",
        "evidence_kind <> 'synthetic' OR status IN ('awaiting_payer_action', 'cancelled')",
    )
    op.add_column(
        "plan_snapshot",
        sa.Column(
            "economic_digest",
            sa.String(length=64),
            server_default=sa.text("'legacy'"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_plan_snapshot_economic",
        "plan_snapshot",
        ["network_id", "initiator_merchant", "economic_digest"],
        unique=False,
    )
    op.execute(
        """
        CREATE FUNCTION elide_synthetic_outbox_held() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.intent_id IS NOT NULL AND NEW.status <> 'held' AND EXISTS (
                SELECT 1 FROM payment_intent
                WHERE intent_id = NEW.intent_id AND evidence_kind = 'synthetic'
            ) THEN
                RAISE EXCEPTION 'outbox message of a synthetic payment intent must stay held'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END $$;
        """
    )
    op.execute(
        "CREATE TRIGGER outbox_message_synthetic_held BEFORE INSERT OR UPDATE "
        "ON outbox_message FOR EACH ROW EXECUTE FUNCTION elide_synthetic_outbox_held()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER outbox_message_synthetic_held ON outbox_message")
    op.execute("DROP FUNCTION elide_synthetic_outbox_held()")
    op.drop_index("ix_plan_snapshot_economic", table_name="plan_snapshot")
    op.drop_column("plan_snapshot", "economic_digest")
    op.drop_constraint("ck_payment_intent_synthetic_never_executes", "payment_intent")
    op.drop_constraint("ck_payment_intent_evidence_kind", "payment_intent")
    op.drop_column("payment_intent", "evidence_kind")
    op.drop_column("payment_authorization", "conditions_digest")
    op.drop_column("payment_authorization", "conditions")
