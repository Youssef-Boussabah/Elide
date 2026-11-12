"""Durable workflow tables (migration `0004_merchant_platform`).

Append-only (UPDATE/DELETE/TRUNCATE refused by trigger): network_participation is the
only one with a lifecycle column handled by a narrow trigger; obligations, revisions,
policy drafts and revisions, decisions, plan snapshots, payment authorizations, audit
events and idempotency records never change. `offer.status` may move once from
`proposed` to a terminal state (trigger-enforced); `payment_intent` and `outbox_message`
are operational records whose transitions P5 will drive and audit.

No cash balance is stored here: capacity stays in the P1 `capacity_evidence` /
`capacity_commitment` tables and the journal stays the accounting record.
"""

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

import api.app.auth.tables  # noqa: F401  (merchant_org / app_user must be registered first)
from api.db import Base

metadata = Base.metadata

MERCHANT_FK = "merchant_org.merchant_id"
USER_FK = "app_user.user_id"
OFFER_STATUSES = (
    "proposed",
    "agreed",
    "rejected",
    "countered",
    "withdrawn",
    "expired",
    "superseded",
)

network = Table(
    "network",
    metadata,
    Column("network_id", String(80), primary_key=True),
    Column("display_name", String(120), nullable=False),
    Column("is_synthetic", Boolean, nullable=False),
    Column("label", String(200), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)

network_participation = Table(
    "network_participation",
    metadata,
    Column("network_id", String(80), ForeignKey("network.network_id"), nullable=False),
    Column("merchant_id", String(80), ForeignKey(MERCHANT_FK), nullable=False),
    Column("joined_at", DateTime(timezone=True), nullable=False),
    Column("left_at", DateTime(timezone=True)),
    # Consent to contribute to aggregate figures shown to another (initiating) merchant.
    Column("share_aggregates", Boolean, nullable=False),
    PrimaryKeyConstraint("network_id", "merchant_id", name="pk_network_participation"),
    CheckConstraint("left_at IS NULL OR left_at >= joined_at", name="ck_participation_order"),
)

obligation = Table(
    "obligation",
    metadata,
    Column("obligation_id", String(80), primary_key=True),
    Column("network_id", String(80), ForeignKey("network.network_id"), nullable=False),
    Column("debtor", String(80), ForeignKey(MERCHANT_FK), nullable=False),
    Column("creditor", String(80), ForeignKey(MERCHANT_FK), nullable=False),
    Column("currency", String(3), nullable=False),
    Column("face_cents", BigInteger, nullable=False),
    Column("issued_at", DateTime(timezone=True), nullable=False),
    Column("provenance_source", String(40), nullable=False),
    Column("provenance_reference", Text, nullable=False),
    Column("provenance_recorded_at", DateTime(timezone=True), nullable=False),
    Column("provenance_evidence_ref", Text),
    Column("provider", String(40)),
    Column("provider_environment", String(40)),
    Column("provider_resource_id", String(120)),
    Column("fee_status", String(20), nullable=False),
    Column("payer_fee_cents", BigInteger, nullable=False),
    Column("payee_fee_cents", BigInteger, nullable=False),
    Column("fee_source", String(200), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    CheckConstraint("debtor <> creditor", name="ck_obligation_parties"),
    CheckConstraint("face_cents > 0", name="ck_obligation_face"),
    CheckConstraint("currency IN ('USD', 'CAD')", name="ck_obligation_currency"),
    CheckConstraint(
        "fee_status IN ('known', 'conditional', 'unknown')", name="ck_obligation_fee_status"
    ),
    CheckConstraint(
        "payer_fee_cents >= 0 AND payee_fee_cents >= 0 AND (fee_status <> 'unknown' "
        "OR (payer_fee_cents = 0 AND payee_fee_cents = 0))",
        name="ck_obligation_fees",
    ),
    CheckConstraint(
        "(provider IS NULL) = (provider_resource_id IS NULL) "
        "AND (provider IS NULL) = (provider_environment IS NULL)",
        name="ck_obligation_provider_ref",
    ),
)
Index(
    "uq_obligation_provider_ref",
    obligation.c.provider,
    obligation.c.provider_environment,
    obligation.c.provider_resource_id,
    unique=True,
    postgresql_where=text("provider IS NOT NULL"),
)
Index("ix_obligation_debtor", obligation.c.debtor)
Index("ix_obligation_creditor", obligation.c.creditor)

obligation_revision = Table(
    "obligation_revision",
    metadata,
    Column("obligation_id", String(80), ForeignKey("obligation.obligation_id"), nullable=False),
    Column("revision_no", Integer, nullable=False),
    Column("status", String(20), nullable=False),
    Column("due_at", DateTime(timezone=True), nullable=False),
    Column("business_tz", String(64), nullable=False),
    Column("concession_cents", BigInteger, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True)),
    Column(
        "authorized_by",
        ARRAY(String(80)),
        nullable=False,
        server_default=text("'{}'::varchar(80)[]"),
    ),
    Column("supersedes", Integer),
    Column("dispute", String(20), nullable=False),
    Column("provenance_source", String(40), nullable=False),
    Column("provenance_reference", Text, nullable=False),
    Column("provenance_recorded_at", DateTime(timezone=True), nullable=False),
    Column("provenance_evidence_ref", Text),
    Column("content_sha256", String(64), nullable=False),
    PrimaryKeyConstraint("obligation_id", "revision_no", name="pk_obligation_revision"),
    ForeignKeyConstraint(
        ["obligation_id", "supersedes"],
        ["obligation_revision.obligation_id", "obligation_revision.revision_no"],
        name="fk_obligation_revision_supersedes",
    ),
    CheckConstraint("revision_no >= 1", name="ck_revision_no"),
    CheckConstraint("status IN ('draft', 'authorized', 'rejected')", name="ck_revision_status"),
    CheckConstraint("dispute IN ('none', 'open', 'resolved')", name="ck_revision_dispute"),
    CheckConstraint("concession_cents >= 0", name="ck_revision_concession"),
    CheckConstraint(
        "expires_at IS NULL OR (status = 'draft' AND expires_at > created_at)",
        name="ck_revision_expiry",
    ),
    CheckConstraint(
        "supersedes IS NULL OR supersedes < revision_no", name="ck_revision_supersedes_order"
    ),
    CheckConstraint(
        "revision_no > 1 OR (status = 'authorized' AND concession_cents = 0 "
        "AND supersedes IS NULL)",
        name="ck_revision_original_terms",
    ),
    CheckConstraint(
        "array_position(authorized_by, NULL) IS NULL", name="ck_revision_authorized_by"
    ),
)

policy_draft = Table(
    "policy_draft",
    metadata,
    Column("draft_id", String(40), primary_key=True),
    Column("merchant_id", String(80), ForeignKey(MERCHANT_FK), nullable=False),
    Column("created_by", String(40), ForeignKey(USER_FK), nullable=False),
    Column("session_id", String(40), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("source", String(40), nullable=False),
    Column("source_text", Text, nullable=False),
    Column("status", String(40), nullable=False),
    Column("report", JSONB, nullable=False),
    Column("proposed", JSONB),
    Column("digest", String(64)),
    Column("model_call", JSONB),
    CheckConstraint("length(source_text) BETWEEN 1 AND 2000", name="ck_policy_draft_text"),
    CheckConstraint(
        "(status = 'READY_FOR_CONFIRMATION') = (digest IS NOT NULL AND proposed IS NOT NULL)",
        name="ck_policy_draft_ready",
    ),
)
Index("ix_policy_draft_merchant", policy_draft.c.merchant_id, policy_draft.c.created_at)

policy_revision = Table(
    "policy_revision",
    metadata,
    Column("merchant_id", String(80), ForeignKey(MERCHANT_FK), nullable=False),
    Column("revision_no", Integer, nullable=False),
    Column(
        "draft_id", String(40), ForeignKey("policy_draft.draft_id"), nullable=False, unique=True
    ),
    Column("rules", JSONB, nullable=False),
    Column("digest", String(64), nullable=False),
    Column("confirmed_by", String(40), ForeignKey(USER_FK), nullable=False),
    Column("session_id", String(40), nullable=False),
    Column("confirmed_at", DateTime(timezone=True), nullable=False),
    PrimaryKeyConstraint("merchant_id", "revision_no", name="pk_policy_revision"),
    CheckConstraint("revision_no >= 1", name="ck_policy_revision_no"),
)

offer = Table(
    "offer",
    metadata,
    Column("offer_id", String(40), primary_key=True),
    Column("obligation_id", String(80), ForeignKey("obligation.obligation_id"), nullable=False),
    Column("revision_no", Integer, nullable=False),
    Column("base_revision_no", Integer, nullable=False),
    Column("proposer_merchant", String(80), ForeignKey(MERCHANT_FK), nullable=False),
    Column("counterparty_merchant", String(80), ForeignKey(MERCHANT_FK), nullable=False),
    Column("cash_cents", BigInteger, nullable=False),
    Column("concession_cents", BigInteger, nullable=False),
    Column("currency", String(3), nullable=False),
    Column("due_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("terms_digest", String(64), nullable=False, unique=True),
    Column("parent_offer_id", String(40), ForeignKey("offer.offer_id")),
    Column("source", String(30), nullable=False),
    Column("ai_reference", JSONB),
    Column("message", Text),
    Column("status", String(20), nullable=False),
    Column("created_by", String(40), ForeignKey(USER_FK), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("closed_at", DateTime(timezone=True)),
    Column("closed_by_revision", Integer),
    ForeignKeyConstraint(
        ["obligation_id", "revision_no"],
        ["obligation_revision.obligation_id", "obligation_revision.revision_no"],
        name="fk_offer_revision",
    ),
    ForeignKeyConstraint(
        ["obligation_id", "base_revision_no"],
        ["obligation_revision.obligation_id", "obligation_revision.revision_no"],
        name="fk_offer_base_revision",
    ),
    UniqueConstraint("obligation_id", "revision_no", name="uq_offer_revision"),
    CheckConstraint("proposer_merchant <> counterparty_merchant", name="ck_offer_parties"),
    CheckConstraint("cash_cents > 0 AND concession_cents >= 0", name="ck_offer_amounts"),
    CheckConstraint("currency = 'USD'", name="ck_offer_currency"),
    CheckConstraint("expires_at > created_at", name="ck_offer_expiry"),
    CheckConstraint("base_revision_no < revision_no", name="ck_offer_base_order"),
    CheckConstraint("source IN ('manual', 'ai_draft_reviewed')", name="ck_offer_source"),
    CheckConstraint(
        "status IN ('proposed', 'agreed', 'rejected', 'countered', 'withdrawn', 'expired', "
        "'superseded')",
        name="ck_offer_status",
    ),
    CheckConstraint("(status = 'proposed') = (closed_at IS NULL)", name="ck_offer_closed_at"),
)
# One open proposal per invoice: further moves are counters, not parallel offers.
Index(
    "uq_offer_one_open_per_obligation",
    offer.c.obligation_id,
    unique=True,
    postgresql_where=text("status = 'proposed'"),
)

offer_decision = Table(
    "offer_decision",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("offer_id", String(40), ForeignKey("offer.offer_id"), nullable=False),
    Column("merchant_id", String(80), ForeignKey(MERCHANT_FK), nullable=False),
    Column("user_id", String(40), ForeignKey(USER_FK), nullable=False),
    Column("session_id", String(40), nullable=False),
    Column("decision", String(20), nullable=False),
    Column("terms_digest", String(64), nullable=False),
    Column("decided_at", DateTime(timezone=True), nullable=False),
    Column("operation_key", String(100), nullable=False),
    Column("resulting_revision_no", Integer),
    Column("child_offer_id", String(40)),
    UniqueConstraint("offer_id", "merchant_id", name="uq_offer_decision_merchant"),
    CheckConstraint(
        "decision IN ('accept', 'reject', 'counter', 'withdraw')", name="ck_offer_decision"
    ),
)

plan_snapshot = Table(
    "plan_snapshot",
    metadata,
    Column("plan_id", String(80), primary_key=True),
    Column("network_id", String(80), ForeignKey("network.network_id"), nullable=False),
    Column("initiator_merchant", String(80), ForeignKey(MERCHANT_FK), nullable=False),
    Column("created_by", String(40), ForeignKey(USER_FK), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("as_of", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True)),
    Column("input_digest", String(64), nullable=False),
    Column("network_digest", String(64), nullable=False),
    Column("record", JSONB, nullable=False),
    Column("verifier_issues", JSONB, nullable=False),
    Column("solver_status", String(40), nullable=False),
    # Digest of the economic inputs only (no clock): unchanged inputs reuse one snapshot.
    Column("economic_digest", String(64), nullable=False, server_default=text("'legacy'")),
    # Only plans that the independent replay verifier accepted are ever stored.
    CheckConstraint("jsonb_array_length(verifier_issues) = 0", name="ck_plan_snapshot_verified"),
)
Index("ix_plan_snapshot_initiator", plan_snapshot.c.initiator_merchant, plan_snapshot.c.created_at)
Index(
    "ix_plan_snapshot_economic",
    plan_snapshot.c.network_id,
    plan_snapshot.c.initiator_merchant,
    plan_snapshot.c.economic_digest,
)

payment_authorization = Table(
    "payment_authorization",
    metadata,
    Column("authorization_id", String(40), primary_key=True),
    Column("plan_id", String(80), ForeignKey("plan_snapshot.plan_id"), nullable=False),
    Column("option_id", String(120), nullable=False),
    Column("leg_id", String(120), nullable=False),
    Column("obligation_id", String(80), ForeignKey("obligation.obligation_id"), nullable=False),
    Column("revision_no", Integer, nullable=False),
    Column("payer_merchant", String(80), ForeignKey(MERCHANT_FK), nullable=False),
    Column("payee_merchant", String(80), ForeignKey(MERCHANT_FK), nullable=False),
    Column("amount_cents", BigInteger, nullable=False),
    Column("fee_ceiling_cents", BigInteger, nullable=False),
    Column("currency", String(3), nullable=False),
    Column("plan_input_digest", String(64), nullable=False),
    Column("binding_digest", String(64), nullable=False, unique=True),
    Column("decision", String(10), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("user_id", String(40), ForeignKey(USER_FK), nullable=False),
    Column("session_id", String(40), nullable=False),
    Column("decided_at", DateTime(timezone=True), nullable=False),
    Column("operation_key", String(100), nullable=False),
    # The material conditions the approval was given under (ADR-0011 addendum 2). Rows
    # from before 0005 carry 'legacy-unbound', which never matches: they need re-approval.
    Column("conditions", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column(
        "conditions_digest", String(64), nullable=False, server_default=text("'legacy-unbound'")
    ),
    ForeignKeyConstraint(
        ["obligation_id", "revision_no"],
        ["obligation_revision.obligation_id", "obligation_revision.revision_no"],
        name="fk_payment_authorization_revision",
    ),
    UniqueConstraint("plan_id", "option_id", name="uq_payment_authorization_leg"),
    CheckConstraint("payer_merchant <> payee_merchant", name="ck_payment_authorization_parties"),
    CheckConstraint(
        "amount_cents > 0 AND fee_ceiling_cents >= 0", name="ck_payment_authorization_amounts"
    ),
    CheckConstraint("currency = 'USD'", name="ck_payment_authorization_currency"),
    CheckConstraint("decision IN ('approve', 'decline')", name="ck_payment_authorization_decision"),
    CheckConstraint("expires_at > decided_at", name="ck_payment_authorization_expiry"),
)

payment_intent = Table(
    "payment_intent",
    metadata,
    Column("intent_id", String(40), primary_key=True),
    Column(
        "authorization_id",
        String(40),
        ForeignKey("payment_authorization.authorization_id"),
        nullable=False,
        unique=True,
    ),
    Column("plan_id", String(80), ForeignKey("plan_snapshot.plan_id"), nullable=False),
    Column("payer_merchant", String(80), ForeignKey(MERCHANT_FK), nullable=False),
    Column("payee_merchant", String(80), ForeignKey(MERCHANT_FK), nullable=False),
    Column("obligation_id", String(80), ForeignKey("obligation.obligation_id"), nullable=False),
    Column("revision_no", Integer, nullable=False),
    Column("amount_cents", BigInteger, nullable=False),
    Column("fee_ceiling_cents", BigInteger, nullable=False),
    Column("currency", String(3), nullable=False),
    Column("operation_key", String(100), nullable=False),
    Column("request_sha256", String(64), nullable=False),
    Column("commitment_id", String(80), nullable=False, unique=True),
    Column("status", String(30), nullable=False),
    Column("created_by", String(40), ForeignKey(USER_FK), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    # Evidence basis of the reserved cash. A synthetic intent can never leave its initial
    # state, so simulation data cannot become payment authority (also enforced on outbox).
    Column("evidence_kind", String(20), nullable=False, server_default=text("'synthetic'")),
    # P5 (ADR-0012 §4.1): why the intent is in its current state, and since when. The
    # transition itself is guarded by a trigger and logged in payment_state_event.
    Column("status_reason", String(80)),
    Column("status_updated_at", DateTime(timezone=True)),
    UniqueConstraint("payer_merchant", "operation_key", name="uq_payment_intent_operation"),
    CheckConstraint(
        "evidence_kind IN ('provider_verified', 'merchant_attested', 'synthetic')",
        name="ck_payment_intent_evidence_kind",
    ),
    CheckConstraint(
        "evidence_kind <> 'synthetic' OR status IN ('approved', 'cancelled')",
        name="ck_payment_intent_synthetic_never_executes",
    ),
    CheckConstraint(
        "amount_cents > 0 AND fee_ceiling_cents >= 0", name="ck_payment_intent_amounts"
    ),
    CheckConstraint("currency = 'USD'", name="ck_payment_intent_currency"),
    CheckConstraint(
        "status IN ('approved', 'session_pending', 'payer_action_pending', "
        "'provider_pending', 'provider_unknown', 'provider_confirmed', 'reconciled', "
        "'failed', 'expired', 'cancelled', 'exception')",
        name="ck_payment_intent_status",
    ),
)

outbox_message = Table(
    "outbox_message",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("dedupe_key", String(200), nullable=False, unique=True),
    Column("kind", String(40), nullable=False),
    Column("intent_id", String(40), ForeignKey("payment_intent.intent_id")),
    Column("payload", JSONB, nullable=False),
    Column("payload_sha256", String(64), nullable=False),
    Column("status", String(20), nullable=False),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("lease_until", DateTime(timezone=True)),
    Column("available_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    # P5 worker (ADR-0012 §4.3): lease owner + fencing token, error, dead letter.
    Column("lease_owner", String(80)),
    Column("lease_token", Integer, nullable=False, server_default=text("0")),
    Column("max_attempts", Integer, nullable=False, server_default=text("8")),
    Column("last_error", String(200)),
    Column("last_attempt_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
    CheckConstraint(
        "kind IN ('create_payment_session', 'provider_fetch', 'reconcile_payment', "
        "'cancel_payment_session')",
        name="ck_outbox_kind",
    ),
    CheckConstraint(
        "status IN ('held', 'pending', 'leased', 'done', 'failed', 'dead')",
        name="ck_outbox_status",
    ),
    CheckConstraint("attempts >= 0", name="ck_outbox_attempts"),
    CheckConstraint("max_attempts BETWEEN 1 AND 50", name="ck_outbox_max_attempts"),
    CheckConstraint(
        "status <> 'leased' OR (lease_owner IS NOT NULL AND lease_until IS NOT NULL)",
        name="ck_outbox_lease",
    ),
)
Index(
    "ix_outbox_due",
    outbox_message.c.available_at,
    postgresql_where=text("status IN ('pending', 'leased')"),
)

audit_event = Table(
    "audit_event",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("actor_user_id", String(40)),
    Column("session_id", String(40)),
    # NULL = security log (denied or anonymous events), never shown to merchants.
    Column("merchant_id", String(80)),
    Column("action", String(60), nullable=False),
    Column("object_type", String(40), nullable=False),
    Column("object_id", String(120), nullable=False),
    Column("outcome", String(20), nullable=False),
    Column("detail", JSONB, nullable=False),
    Column("prev_sha256", String(64), nullable=False),
    Column("event_sha256", String(64), nullable=False, unique=True),
    CheckConstraint(
        "outcome IN ('ok', 'denied', 'conflict', 'error')", name="ck_audit_event_outcome"
    ),
)
Index("ix_audit_event_merchant", audit_event.c.merchant_id, audit_event.c.id)

idempotency_record = Table(
    "idempotency_record",
    metadata,
    Column("merchant_id", String(80), nullable=False),
    Column("operation_key", String(100), nullable=False),
    Column("user_id", String(40), nullable=False),
    Column("route", String(120), nullable=False),
    Column("request_sha256", String(64), nullable=False),
    Column("status_code", Integer, nullable=False),
    Column("response", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    PrimaryKeyConstraint("merchant_id", "operation_key", name="pk_idempotency_record"),
    CheckConstraint("length(operation_key) BETWEEN 8 AND 100", name="ck_idempotency_key"),
)

APPEND_ONLY = (
    "obligation",
    "obligation_revision",
    "policy_draft",
    "policy_revision",
    "offer_decision",
    "plan_snapshot",
    "payment_authorization",
    "audit_event",
    "idempotency_record",
)
