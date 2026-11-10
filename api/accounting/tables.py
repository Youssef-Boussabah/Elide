"""SQLAlchemy Core tables for the journal and the provider event inbox.

Schema is created by migration `0002_financial_kernel`, which also installs the database
guards that do not appear here: append-only triggers and the deferred per-currency
balance check on postings.
"""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    SmallInteger,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)

from api.db import Base

metadata = Base.metadata

CURRENCY_CHECK = "currency IN ('USD', 'CAD')"

journal_entry = Table(
    "journal_entry",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("entity_id", String(80), nullable=False),
    Column("idempotency_key", String(200), nullable=False, unique=True),
    Column("kind", String(40), nullable=False),
    Column("evidence_class", String(40), nullable=False),
    Column("description", Text, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("provider", String(40)),
    Column("provider_environment", String(40)),
    Column("provider_resource_type", String(40)),
    Column("provider_resource_id", String(120)),
    Column("reverses_entry_id", BigInteger, ForeignKey("journal_entry.id"), unique=True),
    Column("content_sha256", String(64), nullable=False),
    CheckConstraint(
        "evidence_class IN ('sandbox_verified', 'merchant_attested', 'synthetic')",
        name="ck_journal_entry_evidence_class",
    ),
    CheckConstraint(
        "(provider IS NULL) = (provider_resource_id IS NULL)",
        name="ck_journal_entry_provider_ref",
    ),
    CheckConstraint(
        "(kind = 'reversal') = (reverses_entry_id IS NOT NULL)",
        name="ck_journal_entry_reversal_link",
    ),
)
# One economic posting per provider resource and entry kind, whatever key is used.
Index(
    "uq_journal_entry_provider_event",
    journal_entry.c.provider,
    journal_entry.c.provider_environment,
    journal_entry.c.provider_resource_type,
    journal_entry.c.provider_resource_id,
    journal_entry.c.kind,
    unique=True,
    postgresql_where=text("provider IS NOT NULL AND kind <> 'reversal'"),
)

journal_posting = Table(
    "journal_posting",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("entry_id", BigInteger, ForeignKey("journal_entry.id"), nullable=False),
    Column("line_no", SmallInteger, nullable=False),
    Column("account", String(160), nullable=False),
    Column("currency", String(3), nullable=False),
    Column("amount_cents", BigInteger, nullable=False),
    UniqueConstraint("entry_id", "line_no", name="uq_journal_posting_line"),
    CheckConstraint(CURRENCY_CHECK, name="ck_journal_posting_currency"),
    CheckConstraint("amount_cents <> 0", name="ck_journal_posting_nonzero"),
)
Index("ix_journal_posting_account", journal_posting.c.account, journal_posting.c.currency)

provider_event_inbox = Table(
    "provider_event_inbox",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("provider", String(40), nullable=False),
    Column("environment", String(40), nullable=False),
    Column("provider_event_id", String(120), nullable=False),
    Column("event_type", String(120), nullable=False),
    Column("payload_sha256", String(64), nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint(
        "provider", "environment", "provider_event_id", name="uq_provider_event_inbox_event"
    ),
)
