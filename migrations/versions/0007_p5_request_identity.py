"""P5 gate review: stable provider request identity (owner review of 613d72f, finding 2).

- `provider_request`: one persisted PayPal-Request-Id per (intent, logical mutating
  operation), with the digest of the request it was first used for. Append-only.
- `provider_operation.request_id` / `attempt_no`: every attempt of a mutating call names
  its logical request. The CHECK is added NOT VALID: rows written before this migration
  are immutable (guard trigger) and cannot be backfilled; every new row is checked.
- The provider_operation guard now also freezes the two new columns.

Revision ID: 0007_p5_request_identity
Revises: 0006_p5_execution
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_p5_request_identity"
down_revision: str | None = "0006_p5_execution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _guard(extra: bool) -> str:
    cols = (
        "NEW.id, NEW.intent_id, NEW.session_id, NEW.operation, NEW.mutating, NEW.request_key, "
        "NEW.request_sha256, NEW.worker, NEW.started_at"
    )
    if extra:
        cols += ", NEW.request_id, NEW.attempt_no"
    old = cols.replace("NEW.", "OLD.")
    return f"""
        CREATE OR REPLACE FUNCTION elide_provider_operation_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'provider_operation rows cannot be deleted'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF OLD.state <> 'started' OR NEW.state = 'started'
               OR ({cols}) IS DISTINCT FROM ({old}) THEN
                RAISE EXCEPTION 'provider_operation may only be finished once'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END $$;
    """


def upgrade() -> None:
    op.create_table(
        "provider_request",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "intent_id", sa.String(40), sa.ForeignKey("payment_intent.intent_id"), nullable=False
        ),
        sa.Column("operation", sa.String(30), nullable=False),
        sa.Column("request_id", sa.String(80), nullable=False, unique=True),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("intent_id", "operation", name="uq_provider_request_logical"),
        sa.CheckConstraint(
            "operation IN ('create_invoice', 'send_invoice', 'cancel_invoice')",
            name="ck_provider_request_operation",
        ),
    )
    op.execute(
        "CREATE TRIGGER provider_request_append_only BEFORE UPDATE OR DELETE ON provider_request "
        "FOR EACH ROW EXECUTE FUNCTION elide_forbid_mutation()"
    )
    op.execute(
        "CREATE TRIGGER provider_request_no_truncate BEFORE TRUNCATE ON provider_request "
        "FOR EACH STATEMENT EXECUTE FUNCTION elide_forbid_mutation()"
    )
    op.add_column(
        "provider_operation",
        sa.Column("request_id", sa.String(80), sa.ForeignKey("provider_request.request_id")),
    )
    op.add_column("provider_operation", sa.Column("attempt_no", sa.Integer()))
    op.execute(
        "ALTER TABLE provider_operation ADD CONSTRAINT ck_provider_operation_request "
        "CHECK (NOT mutating OR (request_id IS NOT NULL AND attempt_no >= 1)) NOT VALID"
    )
    op.execute(_guard(extra=True))


def downgrade() -> None:
    op.execute(_guard(extra=False))
    op.drop_constraint("ck_provider_operation_request", "provider_operation")
    op.drop_column("provider_operation", "attempt_no")
    op.drop_column("provider_operation", "request_id")
    op.execute("DROP TRIGGER provider_request_no_truncate ON provider_request")
    op.execute("DROP TRIGGER provider_request_append_only ON provider_request")
    op.drop_table("provider_request")
