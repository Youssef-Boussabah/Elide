"""P5 execution and reconciliation (ADR-0012).

- payment_intent: status values per ADR-0012 §4.1 ('awaiting_payer_action' -> 'approved',
  'unknown' -> 'provider_unknown'), status_reason / status_updated_at, and a trigger that
  refuses any transition not in the state machine, any change of the bound terms, and
  deletion. A synthetic intent still never leaves approved/cancelled.
- outbox_message: lease owner + fencing token, attempts ceiling, error, dead letter, new
  kinds; partial index of due work.
- capacity_evidence.included_outflow_ids; capacity_commitment.kind / release_reason (the
  release-once trigger now also freezes kind).
- New tables: provider_account, payment_session, provider_operation, payment_state_event,
  obligation_settlement_event, provider_settlement, payment_exception, with guards.

Revision ID: 0006_p5_execution
Revises: 0005_p4_review_remediation
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_p5_execution"
down_revision: str | None = "0005_p4_review_remediation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_STATUSES = (
    "status IN ('approved', 'session_pending', 'payer_action_pending', "
    "'provider_pending', 'provider_unknown', 'provider_confirmed', 'reconciled', "
    "'failed', 'expired', 'cancelled', 'exception')"
)
OLD_STATUSES = (
    "status IN ('awaiting_payer_action', 'provider_pending', 'provider_confirmed', "
    "'failed', 'cancelled', 'unknown')"
)

# ADR-0012 §4.1. Kept in one place in SQL; api/execution/states.py holds the same table and
# a test asserts the two agree.
TRANSITIONS = {
    "approved": ("session_pending", "cancelled"),
    "session_pending": ("payer_action_pending", "provider_unknown", "failed", "cancelled"),
    "provider_unknown": (
        "session_pending",
        "payer_action_pending",
        "provider_pending",
        "provider_confirmed",
        "failed",
        "cancelled",
        "expired",
        "exception",
    ),
    "payer_action_pending": (
        "provider_pending",
        "provider_confirmed",
        "provider_unknown",
        "expired",
        "cancelled",
        "exception",
    ),
    "provider_pending": (
        "provider_confirmed",
        "payer_action_pending",
        "provider_unknown",
        "exception",
    ),
    "provider_confirmed": ("reconciled", "exception"),
}


def _transition_sql() -> str:
    pairs = ", ".join(f"('{a}', '{b}')" for a, bs in TRANSITIONS.items() for b in bs)
    return f"""
        CREATE FUNCTION elide_payment_intent_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'payment_intent rows cannot be deleted'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF (NEW.intent_id, NEW.authorization_id, NEW.plan_id, NEW.payer_merchant,
                NEW.payee_merchant, NEW.obligation_id, NEW.revision_no, NEW.amount_cents,
                NEW.fee_ceiling_cents, NEW.currency, NEW.operation_key, NEW.request_sha256,
                NEW.commitment_id, NEW.created_by, NEW.created_at, NEW.evidence_kind)
               IS DISTINCT FROM
               (OLD.intent_id, OLD.authorization_id, OLD.plan_id, OLD.payer_merchant,
                OLD.payee_merchant, OLD.obligation_id, OLD.revision_no, OLD.amount_cents,
                OLD.fee_ceiling_cents, OLD.currency, OLD.operation_key, OLD.request_sha256,
                OLD.commitment_id, OLD.created_by, OLD.created_at, OLD.evidence_kind) THEN
                RAISE EXCEPTION 'payment_intent terms are immutable'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF NEW.status IS DISTINCT FROM OLD.status AND (OLD.status, NEW.status) NOT IN
               (VALUES {pairs}) THEN
                RAISE EXCEPTION 'payment_intent transition % -> % is not allowed',
                    OLD.status, NEW.status USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END $$;
    """


def upgrade() -> None:
    # --- payment_intent -------------------------------------------------------------------
    op.drop_constraint("ck_payment_intent_synthetic_never_executes", "payment_intent")
    op.drop_constraint("ck_payment_intent_status", "payment_intent")
    op.execute(
        "UPDATE payment_intent SET status = 'approved' WHERE status = 'awaiting_payer_action'"
    )
    op.execute("UPDATE payment_intent SET status = 'provider_unknown' WHERE status = 'unknown'")
    op.create_check_constraint("ck_payment_intent_status", "payment_intent", NEW_STATUSES)
    op.create_check_constraint(
        "ck_payment_intent_synthetic_never_executes",
        "payment_intent",
        "evidence_kind <> 'synthetic' OR status IN ('approved', 'cancelled')",
    )
    op.add_column("payment_intent", sa.Column("status_reason", sa.String(80)))
    op.add_column("payment_intent", sa.Column("status_updated_at", sa.DateTime(timezone=True)))
    op.execute(_transition_sql())
    op.execute(
        "CREATE TRIGGER payment_intent_guard BEFORE UPDATE OR DELETE ON payment_intent "
        "FOR EACH ROW EXECUTE FUNCTION elide_payment_intent_guard()"
    )
    op.execute(
        "CREATE TRIGGER payment_intent_no_truncate BEFORE TRUNCATE ON payment_intent "
        "FOR EACH STATEMENT EXECUTE FUNCTION elide_forbid_mutation()"
    )

    # --- outbox_message -------------------------------------------------------------------
    op.drop_constraint("ck_outbox_kind", "outbox_message")
    op.drop_constraint("ck_outbox_status", "outbox_message")
    op.add_column("outbox_message", sa.Column("lease_owner", sa.String(80)))
    op.add_column(
        "outbox_message",
        sa.Column("lease_token", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "outbox_message",
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("8"), nullable=False),
    )
    op.add_column("outbox_message", sa.Column("last_error", sa.String(200)))
    op.add_column("outbox_message", sa.Column("last_attempt_at", sa.DateTime(timezone=True)))
    op.add_column("outbox_message", sa.Column("finished_at", sa.DateTime(timezone=True)))
    op.create_check_constraint(
        "ck_outbox_kind",
        "outbox_message",
        "kind IN ('create_payment_session', 'provider_fetch', 'reconcile_payment', "
        "'cancel_payment_session')",
    )
    op.create_check_constraint(
        "ck_outbox_status",
        "outbox_message",
        "status IN ('held', 'pending', 'leased', 'done', 'failed', 'dead')",
    )
    op.create_check_constraint(
        "ck_outbox_max_attempts", "outbox_message", "max_attempts BETWEEN 1 AND 50"
    )
    op.create_check_constraint(
        "ck_outbox_lease",
        "outbox_message",
        "status <> 'leased' OR (lease_owner IS NOT NULL AND lease_until IS NOT NULL)",
    )
    op.create_index(
        "ix_outbox_due",
        "outbox_message",
        ["available_at"],
        postgresql_where=sa.text("status IN ('pending', 'leased')"),
    )

    # --- capacity -------------------------------------------------------------------------
    op.add_column(
        "capacity_evidence",
        sa.Column(
            "included_outflow_ids",
            postgresql.ARRAY(sa.String(80)),
            server_default=sa.text("'{}'::varchar(80)[]"),
            nullable=False,
        ),
    )
    op.add_column(
        "capacity_commitment",
        sa.Column("kind", sa.String(20), server_default=sa.text("'reservation'"), nullable=False),
    )
    op.add_column("capacity_commitment", sa.Column("release_reason", sa.String(120)))
    op.create_check_constraint(
        "ck_capacity_commitment_kind",
        "capacity_commitment",
        "kind IN ('reservation', 'settled_outflow')",
    )
    op.create_check_constraint(
        "ck_capacity_commitment_release_reason",
        "capacity_commitment",
        "release_reason IS NULL OR released_at IS NOT NULL",
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION elide_commitment_release_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'capacity_commitment rows cannot be deleted'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF OLD.released_at IS NOT NULL
               OR NEW.released_at IS NULL
               OR (NEW.id, NEW.commitment_id, NEW.merchant_id, NEW.evidence_id, NEW.plan_id,
                   NEW.currency, NEW.amount_cents, NEW.created_at, NEW.kind)
                  IS DISTINCT FROM
                  (OLD.id, OLD.commitment_id, OLD.merchant_id, OLD.evidence_id, OLD.plan_id,
                   OLD.currency, OLD.amount_cents, OLD.created_at, OLD.kind) THEN
                RAISE EXCEPTION 'capacity_commitment may only be released once'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END $$;
        """
    )

    # --- new tables -----------------------------------------------------------------------
    op.create_table(
        "provider_account",
        sa.Column("account_id", sa.String(40), primary_key=True),
        sa.Column(
            "merchant_id",
            sa.String(80),
            sa.ForeignKey("merchant_org.merchant_id"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("environment", sa.String(20), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("credential_ref", sa.String(80), nullable=False),
        sa.Column("credential_fingerprint", sa.String(40), nullable=False),
        sa.Column("account_fingerprint", sa.String(40), nullable=False),
        sa.Column("settlement_currency", sa.String(3)),
        sa.Column("country", sa.String(2)),
        sa.Column("is_test_account", sa.Boolean(), nullable=False),
        sa.Column("consent_note", sa.Text(), nullable=False),
        sa.Column("bound_by", sa.String(80), nullable=False),
        sa.Column("bound_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence_ref", sa.Text()),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revoke_reason", sa.String(200)),
        sa.CheckConstraint("provider = 'paypal'", name="ck_provider_account_provider"),
        sa.CheckConstraint("environment = 'sandbox'", name="ck_provider_account_environment"),
        sa.CheckConstraint("role IN ('invoicer', 'payer')", name="ck_provider_account_role"),
        sa.CheckConstraint("is_test_account", name="ck_provider_account_test_only"),
        sa.CheckConstraint(
            "credential_ref LIKE 'env:%'", name="ck_provider_account_credential_ref"
        ),
        sa.CheckConstraint(
            "account_fingerprint LIKE 'redacted:%' AND credential_fingerprint LIKE 'redacted:%'",
            name="ck_provider_account_fingerprint",
        ),
        sa.CheckConstraint(
            "(revoked_at IS NULL) = (revoke_reason IS NULL)",
            name="ck_provider_account_revocation",
        ),
    )
    op.create_index(
        "uq_provider_account_active_role",
        "provider_account",
        ["merchant_id", "role"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_index(
        "uq_provider_account_active_credential",
        "provider_account",
        ["credential_ref", "role"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    op.create_table(
        "payment_session",
        sa.Column("session_id", sa.String(40), primary_key=True),
        sa.Column(
            "intent_id",
            sa.String(40),
            sa.ForeignKey("payment_intent.intent_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("environment", sa.String(20), nullable=False),
        sa.Column(
            "invoicer_account_id",
            sa.String(40),
            sa.ForeignKey("provider_account.account_id"),
            nullable=False,
        ),
        sa.Column(
            "payer_account_id",
            sa.String(40),
            sa.ForeignKey("provider_account.account_id"),
            nullable=False,
        ),
        sa.Column("invoice_number", sa.String(40), nullable=False, unique=True),
        sa.Column("provider_invoice_id", sa.String(80), unique=True),
        sa.Column("expected_item_cents", sa.BigInteger(), nullable=False),
        sa.Column("expected_discount_cents", sa.BigInteger(), nullable=False),
        sa.Column("expected_total_cents", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("pay_deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_provider_status", sa.String(30)),
        sa.Column("draft_created_at", sa.DateTime(timezone=True)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True)),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("provider = 'paypal'", name="ck_payment_session_provider"),
        sa.CheckConstraint("environment = 'sandbox'", name="ck_payment_session_environment"),
        sa.CheckConstraint("currency = 'USD'", name="ck_payment_session_currency"),
        sa.CheckConstraint(
            "expected_total_cents > 0 AND expected_discount_cents >= 0 "
            "AND expected_item_cents = expected_total_cents + expected_discount_cents",
            name="ck_payment_session_amounts",
        ),
        sa.CheckConstraint("invoice_number LIKE 'ELIDE-%'", name="ck_payment_session_number"),
    )

    op.create_table(
        "provider_operation",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "intent_id", sa.String(40), sa.ForeignKey("payment_intent.intent_id"), nullable=False
        ),
        sa.Column("session_id", sa.String(40), sa.ForeignKey("payment_session.session_id")),
        sa.Column("operation", sa.String(30), nullable=False),
        sa.Column("mutating", sa.Boolean(), nullable=False),
        sa.Column("request_key", sa.String(80), nullable=False, unique=True),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("state", sa.String(12), nullable=False),
        sa.Column("http_status", sa.Integer()),
        sa.Column("debug_id", sa.String(60)),
        sa.Column("outcome_code", sa.String(60)),
        sa.Column("response_sha256", sa.String(64)),
        sa.Column("worker", sa.String(80), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "operation IN ('create_invoice', 'send_invoice', 'cancel_invoice', 'get_invoice', "
            "'search_invoices', 'get_capture', 'get_refund')",
            name="ck_provider_operation_kind",
        ),
        sa.CheckConstraint(
            "state IN ('started', 'succeeded', 'failed', 'unknown')",
            name="ck_provider_operation_state",
        ),
        sa.CheckConstraint(
            "(state = 'started') = (finished_at IS NULL)",
            name="ck_provider_operation_finished",
        ),
    )
    op.create_index(
        "uq_provider_operation_inflight",
        "provider_operation",
        ["intent_id"],
        unique=True,
        postgresql_where=sa.text("state = 'started' AND mutating"),
    )
    op.create_index("ix_provider_operation_intent", "provider_operation", ["intent_id", "id"])

    op.create_table(
        "payment_state_event",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "intent_id", sa.String(40), sa.ForeignKey("payment_intent.intent_id"), nullable=False
        ),
        sa.Column("from_status", sa.String(30), nullable=False),
        sa.Column("to_status", sa.String(30), nullable=False),
        sa.Column("reason", sa.String(80), nullable=False),
        sa.Column("actor", sa.String(80), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    )
    op.create_index("ix_payment_state_event_intent", "payment_state_event", ["intent_id", "id"])

    op.create_table(
        "obligation_settlement_event",
        sa.Column("event_id", sa.String(120), primary_key=True),
        sa.Column(
            "obligation_id",
            sa.String(80),
            sa.ForeignKey("obligation.obligation_id"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provenance_source", sa.String(40), nullable=False),
        sa.Column("provenance_reference", sa.Text(), nullable=False),
        sa.Column("provenance_recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provenance_evidence_ref", sa.Text()),
        sa.Column("reverses_event_id", sa.String(120)),
        sa.Column("intent_id", sa.String(40), sa.ForeignKey("payment_intent.intent_id")),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('cash_applied', 'cash_reversed')",
            name="ck_obligation_settlement_event_kind",
        ),
        sa.CheckConstraint("amount_cents > 0", name="ck_obligation_settlement_event_amount"),
        sa.CheckConstraint("currency = 'USD'", name="ck_obligation_settlement_event_currency"),
        sa.CheckConstraint(
            "(kind = 'cash_reversed') = (reverses_event_id IS NOT NULL)",
            name="ck_obligation_settlement_event_reversal",
        ),
    )
    op.create_index(
        "ix_obligation_settlement_event_obligation",
        "obligation_settlement_event",
        ["obligation_id"],
    )

    op.create_table(
        "provider_settlement",
        sa.Column("capture_id", sa.String(80), primary_key=True),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("environment", sa.String(20), nullable=False),
        sa.Column("invoice_id", sa.String(80), nullable=False),
        sa.Column(
            "intent_id",
            sa.String(40),
            sa.ForeignKey("payment_intent.intent_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "payer_merchant",
            sa.String(80),
            sa.ForeignKey("merchant_org.merchant_id"),
            nullable=False,
        ),
        sa.Column(
            "payee_merchant",
            sa.String(80),
            sa.ForeignKey("merchant_org.merchant_id"),
            nullable=False,
        ),
        sa.Column("receipt_id", sa.String(120), nullable=False, unique=True),
        sa.Column("gross_cents", sa.BigInteger(), nullable=False),
        sa.Column("gross_currency", sa.String(3), nullable=False),
        sa.Column("fee_cents", sa.BigInteger(), nullable=False),
        sa.Column("net_cents", sa.BigInteger(), nullable=False),
        sa.Column("credit_cents", sa.BigInteger(), nullable=False),
        sa.Column("credit_currency", sa.String(3), nullable=False),
        sa.Column("exchange_rate", sa.String(40)),
        sa.Column("capture_status", sa.String(30), nullable=False),
        sa.Column("invoice_status", sa.String(30), nullable=False),
        sa.Column("capture_create_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payee_fingerprint", sa.String(40), nullable=False),
        sa.Column("bundle_sha256", sa.String(64), nullable=False),
        sa.Column("check_sha256", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("environment = 'sandbox'", name="ck_provider_settlement_environment"),
        sa.CheckConstraint("gross_currency = 'USD'", name="ck_provider_settlement_gross_currency"),
        sa.CheckConstraint(
            "gross_cents > 0 AND fee_cents >= 0 AND net_cents = gross_cents - fee_cents "
            "AND credit_cents > 0",
            name="ck_provider_settlement_amounts",
        ),
        sa.CheckConstraint(
            "(credit_currency = gross_currency) = (exchange_rate IS NULL)",
            name="ck_provider_settlement_fx",
        ),
    )

    op.create_table(
        "payment_exception",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "intent_id", sa.String(40), sa.ForeignKey("payment_intent.intent_id"), nullable=False
        ),
        sa.Column("code", sa.String(60), nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("resolution", sa.Text()),
        sa.CheckConstraint(
            "(resolved_at IS NULL) = (resolution IS NULL)",
            name="ck_payment_exception_resolution",
        ),
    )
    op.create_index("ix_payment_exception_intent", "payment_exception", ["intent_id"])
    op.create_index(
        "uq_payment_exception_open",
        "payment_exception",
        ["intent_id", "code"],
        unique=True,
        postgresql_where=sa.text("resolved_at IS NULL"),
    )

    # --- guards ---------------------------------------------------------------------------
    for table in ("payment_state_event", "obligation_settlement_event", "provider_settlement"):
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION elide_forbid_mutation()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION elide_forbid_mutation()"
        )
    guards = {
        # A binding can only be revoked, once; nothing else changes.
        "provider_account": """
            IF OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL
               OR (NEW.account_id, NEW.merchant_id, NEW.provider, NEW.environment, NEW.role,
                   NEW.credential_ref, NEW.credential_fingerprint, NEW.account_fingerprint,
                   NEW.settlement_currency,
                   NEW.country, NEW.is_test_account, NEW.consent_note, NEW.bound_by,
                   NEW.bound_at, NEW.evidence_ref) IS DISTINCT FROM
                  (OLD.account_id, OLD.merchant_id, OLD.provider, OLD.environment, OLD.role,
                   OLD.credential_ref, OLD.credential_fingerprint, OLD.account_fingerprint,
                   OLD.settlement_currency,
                   OLD.country, OLD.is_test_account, OLD.consent_note, OLD.bound_by,
                   OLD.bound_at, OLD.evidence_ref) THEN
                RAISE EXCEPTION 'provider_account may only be revoked, once'
                    USING ERRCODE = 'restrict_violation';
            END IF;""",
        # The expected terms are fixed; the provider invoice id is set once; step times
        # are set once.
        "payment_session": """
            IF (NEW.session_id, NEW.intent_id, NEW.provider, NEW.environment,
                NEW.invoicer_account_id, NEW.payer_account_id, NEW.invoice_number,
                NEW.expected_item_cents, NEW.expected_discount_cents,
                NEW.expected_total_cents, NEW.currency, NEW.due_date, NEW.pay_deadline,
                NEW.created_at) IS DISTINCT FROM
               (OLD.session_id, OLD.intent_id, OLD.provider, OLD.environment,
                OLD.invoicer_account_id, OLD.payer_account_id, OLD.invoice_number,
                OLD.expected_item_cents, OLD.expected_discount_cents,
                OLD.expected_total_cents, OLD.currency, OLD.due_date, OLD.pay_deadline,
                OLD.created_at)
               OR (OLD.provider_invoice_id IS NOT NULL
                   AND NEW.provider_invoice_id IS DISTINCT FROM OLD.provider_invoice_id)
               OR (OLD.draft_created_at IS NOT NULL
                   AND NEW.draft_created_at IS DISTINCT FROM OLD.draft_created_at)
               OR (OLD.sent_at IS NOT NULL AND NEW.sent_at IS DISTINCT FROM OLD.sent_at)
               OR (OLD.cancelled_at IS NOT NULL
                   AND NEW.cancelled_at IS DISTINCT FROM OLD.cancelled_at) THEN
                RAISE EXCEPTION 'payment_session terms and recorded steps are immutable'
                    USING ERRCODE = 'restrict_violation';
            END IF;""",
        # A provider call is recorded before it happens and finished exactly once.
        "provider_operation": """
            IF OLD.state <> 'started' OR NEW.state = 'started'
               OR (NEW.id, NEW.intent_id, NEW.session_id, NEW.operation, NEW.mutating,
                   NEW.request_key, NEW.request_sha256, NEW.worker, NEW.started_at)
                  IS DISTINCT FROM
                  (OLD.id, OLD.intent_id, OLD.session_id, OLD.operation, OLD.mutating,
                   OLD.request_key, OLD.request_sha256, OLD.worker, OLD.started_at) THEN
                RAISE EXCEPTION 'provider_operation may only be finished once'
                    USING ERRCODE = 'restrict_violation';
            END IF;""",
        "payment_exception": """
            IF OLD.resolved_at IS NOT NULL OR NEW.resolved_at IS NULL
               OR (NEW.id, NEW.intent_id, NEW.code, NEW.detail, NEW.opened_at)
                  IS DISTINCT FROM (OLD.id, OLD.intent_id, OLD.code, OLD.detail, OLD.opened_at)
               THEN
                RAISE EXCEPTION 'payment_exception may only be resolved, once'
                    USING ERRCODE = 'restrict_violation';
            END IF;""",
    }
    for table, body in guards.items():
        op.execute(
            f"""
            CREATE FUNCTION elide_{table}_guard() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION '{table} rows cannot be deleted'
                        USING ERRCODE = 'restrict_violation';
                END IF;
                {body}
                RETURN NEW;
            END $$;
            """
        )
        op.execute(
            f"CREATE TRIGGER {table}_guard BEFORE UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION elide_{table}_guard()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION elide_forbid_mutation()"
        )


def downgrade() -> None:
    for table in ("provider_account", "payment_session", "provider_operation", "payment_exception"):
        op.execute(f"DROP TRIGGER {table}_guard ON {table}")
        op.execute(f"DROP TRIGGER {table}_no_truncate ON {table}")
        op.execute(f"DROP FUNCTION elide_{table}_guard()")
    for table in ("payment_state_event", "obligation_settlement_event", "provider_settlement"):
        op.execute(f"DROP TRIGGER {table}_append_only ON {table}")
        op.execute(f"DROP TRIGGER {table}_no_truncate ON {table}")
    op.drop_table("payment_exception")
    op.drop_table("provider_settlement")
    op.drop_table("obligation_settlement_event")
    op.drop_table("payment_state_event")
    op.drop_table("provider_operation")
    op.drop_table("payment_session")
    op.drop_table("provider_account")

    op.execute(
        """
        CREATE OR REPLACE FUNCTION elide_commitment_release_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
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
    op.drop_constraint("ck_capacity_commitment_release_reason", "capacity_commitment")
    op.drop_constraint("ck_capacity_commitment_kind", "capacity_commitment")
    op.drop_column("capacity_commitment", "release_reason")
    op.drop_column("capacity_commitment", "kind")
    op.drop_column("capacity_evidence", "included_outflow_ids")

    op.drop_index("ix_outbox_due", table_name="outbox_message")
    op.drop_constraint("ck_outbox_lease", "outbox_message")
    op.drop_constraint("ck_outbox_max_attempts", "outbox_message")
    op.drop_constraint("ck_outbox_status", "outbox_message")
    op.drop_constraint("ck_outbox_kind", "outbox_message")
    for col in (
        "finished_at",
        "last_attempt_at",
        "last_error",
        "max_attempts",
        "lease_token",
        "lease_owner",
    ):
        op.drop_column("outbox_message", col)
    # Rows using P5-only values cannot be represented in the P4 schema; the downgrade maps
    # them conservatively (anything not terminal becomes 'unknown', i.e. needs review).
    op.execute("UPDATE outbox_message SET status = 'failed' WHERE status = 'dead'")
    op.execute(
        "DELETE FROM outbox_message WHERE kind IN ('reconcile_payment', 'cancel_payment_session')"
    )
    op.create_check_constraint(
        "ck_outbox_kind", "outbox_message", "kind IN ('create_payment_session', 'provider_fetch')"
    )
    op.create_check_constraint(
        "ck_outbox_status",
        "outbox_message",
        "status IN ('held', 'pending', 'leased', 'done', 'failed')",
    )

    op.execute("DROP TRIGGER payment_intent_guard ON payment_intent")
    op.execute("DROP TRIGGER payment_intent_no_truncate ON payment_intent")
    op.execute("DROP FUNCTION elide_payment_intent_guard()")
    op.drop_constraint("ck_payment_intent_synthetic_never_executes", "payment_intent")
    op.drop_constraint("ck_payment_intent_status", "payment_intent")
    op.execute(
        "UPDATE payment_intent SET status = 'awaiting_payer_action' WHERE status = 'approved'"
    )
    op.execute(
        "UPDATE payment_intent SET status = 'unknown' WHERE status IN "
        "('session_pending', 'payer_action_pending', 'provider_unknown', 'exception')"
    )
    op.execute(
        "UPDATE payment_intent SET status = 'provider_confirmed' WHERE status = 'reconciled'"
    )
    op.execute("UPDATE payment_intent SET status = 'cancelled' WHERE status = 'expired'")
    op.create_check_constraint("ck_payment_intent_status", "payment_intent", OLD_STATUSES)
    op.create_check_constraint(
        "ck_payment_intent_synthetic_never_executes",
        "payment_intent",
        "evidence_kind <> 'synthetic' OR status IN ('awaiting_payer_action', 'cancelled')",
    )
    op.drop_column("payment_intent", "status_updated_at")
    op.drop_column("payment_intent", "status_reason")
