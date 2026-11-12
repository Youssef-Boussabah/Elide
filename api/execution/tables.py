"""SQLAlchemy Core tables for P5 execution and reconciliation (migration 0006, ADR-0012).

- `provider_account`: binds an Elide merchant to a Sandbox account by *reference*
  (`credential_ref`, e.g. `env:PAYPAL_CLIENT_ID`) and fingerprint. No credential, email
  or provider merchant id is stored in clear.
- `payment_session`: the one PayPal invoice that carries a payment intent, with the exact
  terms Elide expects the provider to hold. The hosted payer link is never stored.
- `provider_operation`: one row per provider call, written before the call.
- `payment_state_event`: append-only history of intent transitions.
- `obligation_settlement_event`: append-only P1 settlement events (cash applied/reversed).
- `provider_settlement`: append-only record of one reconciled capture.
- `payment_exception`: operator-review items; resolved once.

Guards that do not appear here (triggers) are installed by the migration.
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

import api.app.auth.tables
import api.workflow.tables  # noqa: F401  (payment_intent, obligation)
from api.db import Base

metadata = Base.metadata
MERCHANT_FK = "merchant_org.merchant_id"
INTENT_FK = "payment_intent.intent_id"

provider_account = Table(
    "provider_account",
    metadata,
    Column("account_id", String(40), primary_key=True),
    Column("merchant_id", String(80), ForeignKey(MERCHANT_FK), nullable=False),
    Column("provider", String(20), nullable=False),
    Column("environment", String(20), nullable=False),
    Column("role", String(20), nullable=False),
    Column("credential_ref", String(80), nullable=False),
    # Fingerprint of the credential named by credential_ref (client id / payer email):
    # checked before every provider call. account_fingerprint identifies the provider
    # account itself (the Business account's merchant id; for a payer, its email) and is
    # checked against the provider's own records (capture payee).
    Column("credential_fingerprint", String(40), nullable=False),
    Column("account_fingerprint", String(40), nullable=False),
    Column("settlement_currency", String(3)),
    Column("country", String(2)),
    Column("is_test_account", Boolean, nullable=False),
    Column("consent_note", Text, nullable=False),
    Column("bound_by", String(80), nullable=False),
    Column("bound_at", DateTime(timezone=True), nullable=False),
    Column("evidence_ref", Text),
    Column("revoked_at", DateTime(timezone=True)),
    Column("revoke_reason", String(200)),
    CheckConstraint("provider = 'paypal'", name="ck_provider_account_provider"),
    CheckConstraint("environment = 'sandbox'", name="ck_provider_account_environment"),
    CheckConstraint("role IN ('invoicer', 'payer')", name="ck_provider_account_role"),
    CheckConstraint("is_test_account", name="ck_provider_account_test_only"),
    CheckConstraint("credential_ref LIKE 'env:%'", name="ck_provider_account_credential_ref"),
    CheckConstraint(
        "account_fingerprint LIKE 'redacted:%' AND credential_fingerprint LIKE 'redacted:%'",
        name="ck_provider_account_fingerprint",
    ),
    CheckConstraint(
        "(revoked_at IS NULL) = (revoke_reason IS NULL)", name="ck_provider_account_revocation"
    ),
)
# One active binding per merchant and role; one active merchant per invoicer credential.
Index(
    "uq_provider_account_active_role",
    provider_account.c.merchant_id,
    provider_account.c.role,
    unique=True,
    postgresql_where=text("revoked_at IS NULL"),
)
Index(
    "uq_provider_account_active_credential",
    provider_account.c.credential_ref,
    provider_account.c.role,
    unique=True,
    postgresql_where=text("revoked_at IS NULL"),
)

payment_session = Table(
    "payment_session",
    metadata,
    Column("session_id", String(40), primary_key=True),
    Column("intent_id", String(40), ForeignKey(INTENT_FK), nullable=False, unique=True),
    Column("provider", String(20), nullable=False),
    Column("environment", String(20), nullable=False),
    Column(
        "invoicer_account_id",
        String(40),
        ForeignKey("provider_account.account_id"),
        nullable=False,
    ),
    Column(
        "payer_account_id", String(40), ForeignKey("provider_account.account_id"), nullable=False
    ),
    Column("invoice_number", String(40), nullable=False, unique=True),
    Column("provider_invoice_id", String(80), unique=True),
    Column("expected_item_cents", BigInteger, nullable=False),
    Column("expected_discount_cents", BigInteger, nullable=False),
    Column("expected_total_cents", BigInteger, nullable=False),
    Column("currency", String(3), nullable=False),
    Column("due_date", Date, nullable=False),
    Column("pay_deadline", DateTime(timezone=True), nullable=False),
    Column("last_provider_status", String(30)),
    Column("draft_created_at", DateTime(timezone=True)),
    Column("sent_at", DateTime(timezone=True)),
    Column("cancel_requested_at", DateTime(timezone=True)),
    Column("cancelled_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("provider = 'paypal'", name="ck_payment_session_provider"),
    CheckConstraint("environment = 'sandbox'", name="ck_payment_session_environment"),
    CheckConstraint("currency = 'USD'", name="ck_payment_session_currency"),
    CheckConstraint(
        "expected_total_cents > 0 AND expected_discount_cents >= 0 "
        "AND expected_item_cents = expected_total_cents + expected_discount_cents",
        name="ck_payment_session_amounts",
    ),
    CheckConstraint("invoice_number LIKE 'ELIDE-%'", name="ck_payment_session_number"),
)

# One stable PayPal-Request-Id per logical provider-mutating operation of an intent
# (migration 0007, gate review finding 2). Every attempt of that operation reuses it; the
# request digest must match, so a changed payload can never reuse an old identity.
provider_request = Table(
    "provider_request",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("intent_id", String(40), ForeignKey(INTENT_FK), nullable=False),
    Column("operation", String(30), nullable=False),
    Column("request_id", String(80), nullable=False, unique=True),
    Column("request_sha256", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("intent_id", "operation", name="uq_provider_request_logical"),
    CheckConstraint(
        "operation IN ('create_invoice', 'send_invoice', 'cancel_invoice')",
        name="ck_provider_request_operation",
    ),
)

provider_operation = Table(
    "provider_operation",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("intent_id", String(40), ForeignKey(INTENT_FK), nullable=False),
    Column("session_id", String(40), ForeignKey("payment_session.session_id")),
    Column("operation", String(30), nullable=False),
    Column("mutating", Boolean, nullable=False),
    Column("request_key", String(80), nullable=False, unique=True),
    Column("request_sha256", String(64), nullable=False),
    Column("state", String(12), nullable=False),
    Column("http_status", Integer),
    Column("debug_id", String(60)),
    Column("outcome_code", String(60)),
    Column("response_sha256", String(64)),
    Column("worker", String(80), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True)),
    # 0007: the logical request this attempt belongs to (mutating calls only) and its number.
    Column("request_id", String(80), ForeignKey("provider_request.request_id")),
    Column("attempt_no", Integer),
    CheckConstraint(
        "NOT mutating OR (request_id IS NOT NULL AND attempt_no >= 1)",
        name="ck_provider_operation_request",
    ),
    CheckConstraint(
        "operation IN ('create_invoice', 'send_invoice', 'cancel_invoice', 'get_invoice', "
        "'search_invoices', 'get_capture', 'get_refund')",
        name="ck_provider_operation_kind",
    ),
    CheckConstraint(
        "state IN ('started', 'succeeded', 'failed', 'unknown')",
        name="ck_provider_operation_state",
    ),
    CheckConstraint(
        "(state = 'started') = (finished_at IS NULL)", name="ck_provider_operation_finished"
    ),
)
# The per-payment mutex: at most one in-flight provider-mutating call per intent.
Index(
    "uq_provider_operation_inflight",
    provider_operation.c.intent_id,
    unique=True,
    postgresql_where=text("state = 'started' AND mutating"),
)
Index("ix_provider_operation_intent", provider_operation.c.intent_id, provider_operation.c.id)

payment_state_event = Table(
    "payment_state_event",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("intent_id", String(40), ForeignKey(INTENT_FK), nullable=False),
    Column("from_status", String(30), nullable=False),
    Column("to_status", String(30), nullable=False),
    Column("reason", String(80), nullable=False),
    Column("actor", String(80), nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("detail", JSONB, nullable=False),
)
Index("ix_payment_state_event_intent", payment_state_event.c.intent_id, payment_state_event.c.id)

obligation_settlement_event = Table(
    "obligation_settlement_event",
    metadata,
    Column("event_id", String(120), primary_key=True),
    Column("obligation_id", String(80), ForeignKey("obligation.obligation_id"), nullable=False),
    Column("kind", String(20), nullable=False),
    Column("amount_cents", BigInteger, nullable=False),
    Column("currency", String(3), nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("provenance_source", String(40), nullable=False),
    Column("provenance_reference", Text, nullable=False),
    Column("provenance_recorded_at", DateTime(timezone=True), nullable=False),
    Column("provenance_evidence_ref", Text),
    Column("reverses_event_id", String(120)),
    Column("intent_id", String(40), ForeignKey(INTENT_FK)),
    Column("recorded_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    CheckConstraint(
        "kind IN ('cash_applied', 'cash_reversed')", name="ck_obligation_settlement_event_kind"
    ),
    CheckConstraint("amount_cents > 0", name="ck_obligation_settlement_event_amount"),
    CheckConstraint("currency = 'USD'", name="ck_obligation_settlement_event_currency"),
    CheckConstraint(
        "(kind = 'cash_reversed') = (reverses_event_id IS NOT NULL)",
        name="ck_obligation_settlement_event_reversal",
    ),
)
Index(
    "ix_obligation_settlement_event_obligation",
    obligation_settlement_event.c.obligation_id,
)

provider_settlement = Table(
    "provider_settlement",
    metadata,
    Column("capture_id", String(80), primary_key=True),
    Column("provider", String(20), nullable=False),
    Column("environment", String(20), nullable=False),
    Column("invoice_id", String(80), nullable=False),
    Column("intent_id", String(40), ForeignKey(INTENT_FK), nullable=False, unique=True),
    Column("payer_merchant", String(80), ForeignKey(MERCHANT_FK), nullable=False),
    Column("payee_merchant", String(80), ForeignKey(MERCHANT_FK), nullable=False),
    Column("receipt_id", String(120), nullable=False, unique=True),
    Column("gross_cents", BigInteger, nullable=False),
    Column("gross_currency", String(3), nullable=False),
    Column("fee_cents", BigInteger, nullable=False),
    Column("net_cents", BigInteger, nullable=False),
    Column("credit_cents", BigInteger, nullable=False),
    Column("credit_currency", String(3), nullable=False),
    Column("exchange_rate", String(40)),
    Column("capture_status", String(30), nullable=False),
    Column("invoice_status", String(30), nullable=False),
    Column("capture_create_time", DateTime(timezone=True), nullable=False),
    Column("payee_fingerprint", String(40), nullable=False),
    Column("bundle_sha256", String(64), nullable=False),
    Column("check_sha256", String(64), nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("environment = 'sandbox'", name="ck_provider_settlement_environment"),
    CheckConstraint("gross_currency = 'USD'", name="ck_provider_settlement_gross_currency"),
    CheckConstraint(
        "gross_cents > 0 AND fee_cents >= 0 AND net_cents = gross_cents - fee_cents "
        "AND credit_cents > 0",
        name="ck_provider_settlement_amounts",
    ),
    CheckConstraint(
        "(credit_currency = gross_currency) = (exchange_rate IS NULL)",
        name="ck_provider_settlement_fx",
    ),
)

payment_exception = Table(
    "payment_exception",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("intent_id", String(40), ForeignKey(INTENT_FK), nullable=False),
    Column("code", String(60), nullable=False),
    Column("detail", JSONB, nullable=False),
    Column("opened_at", DateTime(timezone=True), nullable=False),
    Column("resolved_at", DateTime(timezone=True)),
    Column("resolution", Text),
    CheckConstraint(
        "(resolved_at IS NULL) = (resolution IS NULL)", name="ck_payment_exception_resolution"
    ),
)
Index("ix_payment_exception_intent", payment_exception.c.intent_id)
Index(
    "uq_payment_exception_open",
    payment_exception.c.intent_id,
    payment_exception.c.code,
    unique=True,
    postgresql_where=text("resolved_at IS NULL"),
)

APPEND_ONLY = (
    "payment_state_event",
    "obligation_settlement_event",
    "provider_settlement",
    "provider_request",
)
