"""Identity services: users, memberships, server-side sessions, rate limits (ADR-0011).

The principal is resolved only from the session cookie on the server. Nothing in a
request body or header can name a user or a merchant role. Every function takes an
explicit `now` so expiry and throttling are testable without sleeping.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, Engine, and_, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from api.app.auth import passwords
from api.app.auth.permissions import Permission, permissions_for
from api.app.auth.tables import (
    ROLES,
    app_user,
    auth_attempt,
    auth_session,
    membership,
    merchant_org,
    rate_limit_window,
)
from api.workflow import audit


class AuthError(Exception):
    code = "AUTH_ERROR"


class InvalidCredentialsError(AuthError):
    code = "INVALID_CREDENTIALS"


class RateLimitedError(AuthError):
    code = "RATE_LIMITED"


class NotFoundError(AuthError):
    """Missing OR not permitted: the two are indistinguishable to the caller."""

    code = "NOT_FOUND"


class ForbiddenError(AuthError):
    code = "FORBIDDEN"


@dataclass(frozen=True, slots=True)
class SessionPolicy:
    idle: timedelta = timedelta(minutes=30)
    absolute: timedelta = timedelta(hours=8)
    step_up: timedelta = timedelta(minutes=10)
    login_failures_per_login: int = 5
    login_failures_per_ip: int = 20
    login_window: timedelta = timedelta(minutes=15)
    mutations_per_minute: int = 60
    touch_every: timedelta = timedelta(seconds=30)


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: str
    login: str
    display_name: str
    is_test_identity: bool
    session_id: str
    token_sha256: str
    csrf_sha256: str
    authenticated_at: datetime
    absolute_expires_at: datetime

    def recently_authenticated(self, now: datetime, policy: SessionPolicy) -> bool:
        return now - self.authenticated_at <= policy.step_up


@dataclass(frozen=True, slots=True)
class NewSession:
    token: str  # goes only into the Set-Cookie header
    csrf_token: str  # returned to the page once; only its hash is stored
    principal: Principal


@dataclass(frozen=True, slots=True)
class Membership:
    merchant_id: str
    role: str
    permissions: frozenset[Permission]
    display_name: str
    is_synthetic: bool


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def csrf_for(token: str) -> str:
    """Synchronizer token derived from the (secret, HttpOnly) session token with HMAC, so
    every tab of one session gets the same value and a cross-site page cannot compute it
    (OWASP "HMAC-based token" pattern). Only its SHA-256 is stored."""
    return hmac.new(token.encode(), b"elide-csrf-v1", hashlib.sha256).hexdigest()


def normalize_login(login: str) -> str:
    return (login or "").strip().lower()


# --- provisioning ------------------------------------------------------------------------
def create_merchant(
    conn: Connection, merchant_id: str, display_name: str, business_tz: str, *, is_synthetic: bool
) -> None:
    conn.execute(
        pg_insert(merchant_org)
        .values(
            merchant_id=merchant_id,
            display_name=display_name,
            business_tz=business_tz,
            is_synthetic=is_synthetic,
        )
        .on_conflict_do_nothing()
    )


def create_user(
    conn: Connection,
    login: str,
    display_name: str,
    password: str,
    *,
    is_test_identity: bool,
) -> str:
    user_id = "usr_" + secrets.token_hex(8)
    conn.execute(
        insert(app_user).values(
            user_id=user_id,
            login=normalize_login(login),
            display_name=display_name,
            password_hash=passwords.hash_password(password),
            is_test_identity=is_test_identity,
        )
    )
    return user_id


def grant(
    conn: Connection,
    user_id: str,
    merchant_id: str,
    role: str,
    *,
    at: datetime,
    granted_by: str | None = None,
) -> int:
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}")
    mid = conn.execute(
        insert(membership)
        .values(
            user_id=user_id,
            merchant_id=merchant_id,
            role=role,
            granted_at=at,
            granted_by=granted_by,
        )
        .returning(membership.c.id)
    ).scalar_one()
    audit.append(
        conn,
        at=at,
        action="membership.granted",
        object_type="membership",
        object_id=str(mid),
        actor_user_id=granted_by,
        merchant_id=merchant_id,
        detail={"user_id": user_id, "role": role},
    )
    return mid


def revoke_membership(
    conn: Connection,
    user_id: str,
    merchant_id: str,
    *,
    at: datetime,
    revoked_by: str | None,
    reason: str,
) -> bool:
    row = conn.execute(
        update(membership)
        .where(
            membership.c.user_id == user_id,
            membership.c.merchant_id == merchant_id,
            membership.c.revoked_at.is_(None),
        )
        .values(revoked_at=at, revoked_by=revoked_by, revoke_reason=reason)
        .returning(membership.c.id)
    ).scalar_one_or_none()
    if row is not None:
        audit.append(
            conn,
            at=at,
            action="membership.revoked",
            object_type="membership",
            object_id=str(row),
            actor_user_id=revoked_by,
            merchant_id=merchant_id,
            detail={"user_id": user_id, "reason": reason[:200]},
        )
    return row is not None


# --- rate limiting -----------------------------------------------------------------------
def _recent_failures(conn: Connection, column, value: str, since: datetime) -> int:
    return conn.execute(
        select(func.count())
        .select_from(auth_attempt)
        .where(column == value, auth_attempt.c.succeeded.is_(False))
        .where(auth_attempt.c.attempted_at >= since)
    ).scalar_one()


def hit_rate_limit(
    conn: Connection, bucket: str, *, limit: int, window: timedelta, now: datetime
) -> bool:
    """Fixed-window counter in PostgreSQL (consistent across app instances).

    Returns True when this request exceeds the limit.
    """
    seconds = int(window.total_seconds())
    start = datetime.fromtimestamp(int(now.timestamp()) // seconds * seconds, tz=now.tzinfo)
    count = conn.execute(
        pg_insert(rate_limit_window)
        .values(bucket=bucket[:200], window_start=start, count=1)
        .on_conflict_do_update(
            index_elements=["bucket", "window_start"],
            set_={"count": rate_limit_window.c.count + 1},
        )
        .returning(rate_limit_window.c.count)
    ).scalar_one()
    return count > limit


# --- sessions ----------------------------------------------------------------------------
def _principal(row: Any) -> Principal:
    return Principal(
        user_id=row["user_id"],
        login=row["login"],
        display_name=row["display_name"],
        is_test_identity=row["is_test_identity"],
        session_id=row["session_id"],
        token_sha256=row["token_sha256"],
        csrf_sha256=row["csrf_sha256"],
        authenticated_at=row["authenticated_at"],
        absolute_expires_at=row["absolute_expires_at"],
    )


def login(
    engine: Engine,
    login_name: str,
    password: str,
    *,
    client_ip: str,
    user_agent: str,
    now: datetime,
    policy: SessionPolicy,
    replaces_token: str | None = None,
) -> NewSession:
    norm = normalize_login(login_name)
    login_hash = sha256(norm)
    since = now - policy.login_window
    with engine.begin() as conn:
        throttled = (
            _recent_failures(conn, auth_attempt.c.login_sha256, login_hash, since)
            >= policy.login_failures_per_login
            or _recent_failures(conn, auth_attempt.c.client_ip, client_ip, since)
            >= policy.login_failures_per_ip
        )
    if throttled:
        audit.append_separately(
            engine,
            at=now,
            action="auth.login",
            object_type="login",
            object_id=login_hash[:16],
            outcome="denied",
            detail={"reason": "RATE_LIMITED"},
        )
        raise RateLimitedError("too many failed attempts; try again later")

    with engine.begin() as conn:
        user = (
            conn.execute(select(app_user).where(app_user.c.login == norm)).mappings().one_or_none()
        )
        ok, rehash = passwords.verify(user["password_hash"] if user else None, password)
        if ok and user["disabled_at"] is not None:
            ok = False
        conn.execute(
            insert(auth_attempt).values(
                attempted_at=now, login_sha256=login_hash, client_ip=client_ip, succeeded=ok
            )
        )
        if not ok:
            audit.append(
                conn,
                at=now,
                action="auth.login",
                object_type="login",
                object_id=login_hash[:16],
                outcome="denied",
                detail={"reason": "INVALID_CREDENTIALS"},
            )
    if not ok:
        raise InvalidCredentialsError("invalid login or password")

    token = secrets.token_urlsafe(32)
    csrf = csrf_for(token)
    session_id = "ses_" + secrets.token_hex(8)
    absolute = now + policy.absolute
    with engine.begin() as conn:
        if rehash:
            conn.execute(
                update(app_user)
                .where(app_user.c.user_id == user["user_id"])
                .values(password_hash=rehash)
            )
        if replaces_token:  # a new login never keeps an older cookie alive (fixation)
            conn.execute(
                update(auth_session)
                .where(
                    auth_session.c.token_sha256 == sha256(replaces_token),
                    auth_session.c.revoked_at.is_(None),
                )
                .values(revoked_at=now, revoke_reason="replaced_by_login")
            )
        conn.execute(
            insert(auth_session).values(
                token_sha256=sha256(token),
                session_id=session_id,
                user_id=user["user_id"],
                csrf_sha256=sha256(csrf),
                created_at=now,
                authenticated_at=now,
                last_seen_at=now,
                idle_expires_at=min(now + policy.idle, absolute),
                absolute_expires_at=absolute,
                client_ip=client_ip[:64],
                user_agent_sha256=sha256(user_agent or ""),
            )
        )
        audit.append(
            conn,
            at=now,
            action="auth.login",
            object_type="session",
            object_id=session_id,
            actor_user_id=user["user_id"],
            session_id=session_id,
            detail={"test_identity": bool(user["is_test_identity"])},
        )
    row = {
        **user,
        "session_id": session_id,
        "token_sha256": sha256(token),
        "csrf_sha256": sha256(csrf),
        "authenticated_at": now,
        "absolute_expires_at": absolute,
    }
    return NewSession(token, csrf, _principal(row))


def resolve(
    engine: Engine, token: str | None, *, now: datetime, policy: SessionPolicy
) -> Principal | None:
    """The principal for a cookie token, or None (missing, unknown, expired, revoked,
    disabled). Extends the idle window, never beyond the absolute expiry."""
    if not token or len(token) > 200:
        return None
    h = sha256(token)
    with engine.begin() as conn:
        row = (
            conn.execute(
                select(
                    auth_session,
                    app_user.c.login,
                    app_user.c.display_name,
                    app_user.c.is_test_identity,
                    app_user.c.disabled_at,
                )
                .join(app_user, app_user.c.user_id == auth_session.c.user_id)
                .where(auth_session.c.token_sha256 == h)
            )
            .mappings()
            .one_or_none()
        )
        if row is None or row["revoked_at"] is not None:
            return None
        reason = None
        if row["disabled_at"] is not None:
            reason = "user_disabled"
        elif now >= row["absolute_expires_at"]:
            reason = "absolute_timeout"
        elif now >= row["idle_expires_at"]:
            reason = "idle_timeout"
        if reason:
            conn.execute(
                update(auth_session)
                .where(auth_session.c.token_sha256 == h, auth_session.c.revoked_at.is_(None))
                .values(revoked_at=now, revoke_reason=reason)
            )
            return None
        if now - row["last_seen_at"] >= policy.touch_every:
            conn.execute(
                update(auth_session)
                .where(auth_session.c.token_sha256 == h)
                .values(
                    last_seen_at=now,
                    idle_expires_at=min(now + policy.idle, row["absolute_expires_at"]),
                )
            )
    return _principal(row)


def reauthenticate(
    engine: Engine, principal: Principal, password: str, *, now: datetime, policy: SessionPolicy
) -> None:
    with engine.begin() as conn:
        failures = _recent_failures(
            conn, auth_attempt.c.login_sha256, sha256(principal.login), now - policy.login_window
        )
        if failures >= policy.login_failures_per_login:
            raise RateLimitedError("too many failed attempts; try again later")
        stored = conn.execute(
            select(app_user.c.password_hash).where(app_user.c.user_id == principal.user_id)
        ).scalar_one()
        ok, _ = passwords.verify(stored, password)
        conn.execute(
            insert(auth_attempt).values(
                attempted_at=now,
                login_sha256=sha256(principal.login),
                client_ip="reauth",
                succeeded=ok,
            )
        )
        if ok:
            conn.execute(
                update(auth_session)
                .where(auth_session.c.token_sha256 == principal.token_sha256)
                .values(authenticated_at=now)
            )
        audit.append(
            conn,
            at=now,
            action="auth.reauth",
            object_type="session",
            object_id=principal.session_id,
            actor_user_id=principal.user_id,
            session_id=principal.session_id,
            outcome="ok" if ok else "denied",
        )
    if not ok:
        raise InvalidCredentialsError("password check failed")


def logout(engine: Engine, principal: Principal, *, now: datetime) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(auth_session)
            .where(
                auth_session.c.token_sha256 == principal.token_sha256,
                auth_session.c.revoked_at.is_(None),
            )
            .values(revoked_at=now, revoke_reason="logout")
        )
        audit.append(
            conn,
            at=now,
            action="auth.logout",
            object_type="session",
            object_id=principal.session_id,
            actor_user_id=principal.user_id,
            session_id=principal.session_id,
        )


# --- memberships -------------------------------------------------------------------------
def memberships(conn: Connection, user_id: str) -> list[Membership]:
    rows = conn.execute(
        select(
            membership.c.merchant_id,
            membership.c.role,
            merchant_org.c.display_name,
            merchant_org.c.is_synthetic,
        )
        .join(merchant_org, merchant_org.c.merchant_id == membership.c.merchant_id)
        .where(membership.c.user_id == user_id, membership.c.revoked_at.is_(None))
        .order_by(membership.c.merchant_id)
    ).mappings()
    return [
        Membership(
            r["merchant_id"],
            r["role"],
            permissions_for(r["role"]),
            r["display_name"],
            r["is_synthetic"],
        )
        for r in rows
    ]


def require_membership(
    conn: Connection, principal: Principal, merchant_id: str, permission: Permission
) -> Membership:
    """Active membership with `permission`, else NotFound (no membership: the merchant
    looks nonexistent) or Forbidden (member without that permission)."""
    row = (
        conn.execute(
            select(
                membership.c.role,
                merchant_org.c.display_name,
                merchant_org.c.is_synthetic,
            )
            .join(merchant_org, merchant_org.c.merchant_id == membership.c.merchant_id)
            .join(app_user, app_user.c.user_id == membership.c.user_id)
            .where(
                and_(
                    membership.c.user_id == principal.user_id,
                    membership.c.merchant_id == merchant_id,
                    membership.c.revoked_at.is_(None),
                    app_user.c.disabled_at.is_(None),
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise NotFoundError("not found")
    perms = permissions_for(row["role"])
    if permission not in perms:
        raise ForbiddenError(f"role {row['role']} lacks {permission.value}")
    return Membership(merchant_id, row["role"], perms, row["display_name"], row["is_synthetic"])
