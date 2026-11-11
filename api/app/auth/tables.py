"""Identity and tenancy tables (migration `0004_merchant_platform`).

Secrets are never stored: sessions keep only the SHA-256 of the cookie token and of the
CSRF token; passwords keep only an Argon2id hash. Memberships are revoked, never deleted
(a database trigger allows only the one-time revocation update).
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    text,
)

from api.db import Base

metadata = Base.metadata

ROLES = ("owner", "approver", "policy_editor", "auditor", "viewer")

app_user = Table(
    "app_user",
    metadata,
    Column("user_id", String(40), primary_key=True),
    Column("login", String(254), nullable=False, unique=True),
    Column("display_name", String(120), nullable=False),
    Column("password_hash", Text, nullable=False),
    Column("is_test_identity", Boolean, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("disabled_at", DateTime(timezone=True)),
    CheckConstraint("login = lower(login) AND length(login) >= 3", name="ck_app_user_login"),
)

merchant_org = Table(
    "merchant_org",
    metadata,
    Column("merchant_id", String(80), primary_key=True),
    Column("display_name", String(120), nullable=False),
    Column("business_tz", String(64), nullable=False),
    Column("is_synthetic", Boolean, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)

membership = Table(
    "membership",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("user_id", String(40), ForeignKey("app_user.user_id"), nullable=False),
    Column("merchant_id", String(80), ForeignKey("merchant_org.merchant_id"), nullable=False),
    Column("role", String(20), nullable=False),
    Column("granted_at", DateTime(timezone=True), nullable=False),
    Column("granted_by", String(40), ForeignKey("app_user.user_id")),
    Column("revoked_at", DateTime(timezone=True)),
    Column("revoked_by", String(40), ForeignKey("app_user.user_id")),
    Column("revoke_reason", Text),
    CheckConstraint(
        "role IN ('owner', 'approver', 'policy_editor', 'auditor', 'viewer')",
        name="ck_membership_role",
    ),
    CheckConstraint(
        "revoked_at IS NULL OR revoked_at >= granted_at", name="ck_membership_revoke_order"
    ),
    CheckConstraint(
        "(revoked_at IS NULL) = (revoke_reason IS NULL)", name="ck_membership_revoke_reason"
    ),
)
# At most one active membership per user and merchant.
Index(
    "uq_membership_active",
    membership.c.user_id,
    membership.c.merchant_id,
    unique=True,
    postgresql_where=text("revoked_at IS NULL"),
)

auth_session = Table(
    "auth_session",
    metadata,
    Column("token_sha256", String(64), primary_key=True),
    Column("session_id", String(40), nullable=False, unique=True),
    Column("user_id", String(40), ForeignKey("app_user.user_id"), nullable=False),
    Column("csrf_sha256", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("authenticated_at", DateTime(timezone=True), nullable=False),
    Column("last_seen_at", DateTime(timezone=True), nullable=False),
    Column("idle_expires_at", DateTime(timezone=True), nullable=False),
    Column("absolute_expires_at", DateTime(timezone=True), nullable=False),
    Column("revoked_at", DateTime(timezone=True)),
    Column("revoke_reason", String(40)),
    Column("client_ip", String(64)),
    Column("user_agent_sha256", String(64)),
    CheckConstraint("absolute_expires_at > created_at", name="ck_auth_session_absolute"),
    CheckConstraint(
        "idle_expires_at <= absolute_expires_at", name="ck_auth_session_idle_within_absolute"
    ),
    CheckConstraint(
        "(revoked_at IS NULL) = (revoke_reason IS NULL)", name="ck_auth_session_revoke_reason"
    ),
)
Index(
    "ix_auth_session_user_active",
    auth_session.c.user_id,
    postgresql_where=text("revoked_at IS NULL"),
)

auth_attempt = Table(
    "auth_attempt",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("attempted_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("login_sha256", String(64), nullable=False),
    Column("client_ip", String(64), nullable=False),
    Column("succeeded", Boolean, nullable=False),
)
Index("ix_auth_attempt_login", auth_attempt.c.login_sha256, auth_attempt.c.attempted_at)
Index("ix_auth_attempt_ip", auth_attempt.c.client_ip, auth_attempt.c.attempted_at)

rate_limit_window = Table(
    "rate_limit_window",
    metadata,
    Column("bucket", String(200), nullable=False),
    Column("window_start", DateTime(timezone=True), nullable=False),
    Column("count", Integer, nullable=False),
    PrimaryKeyConstraint("bucket", "window_start", name="pk_rate_limit_window"),
    CheckConstraint("count >= 0", name="ck_rate_limit_window_count"),
)
