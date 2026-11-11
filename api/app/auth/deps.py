"""FastAPI dependencies: principal from the session cookie, CSRF, merchant scope, step-up.

Errors use `{"detail": {"code": ..., "message": ...}}`. Unknown merchants and merchants
the principal is not a member of both return the same 404.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy import Engine

from api.app.auth import service
from api.app.auth.permissions import Permission
from api.app.auth.service import Membership, Principal, SessionPolicy
from api.config import get_settings
from api.db import get_engine
from api.workflow import audit

COOKIE = "elide_session"
SECURE_COOKIE = "__Host-elide_session"
UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def cookie_name() -> str:
    return SECURE_COOKIE if get_settings().cookie_secure else COOKIE


def engine_of(request: Request) -> Engine:
    engine = getattr(request.app.state, "engine", None)
    return engine if engine is not None else get_engine()


def now_of(request: Request) -> datetime:
    clock = getattr(request.app.state, "clock", None)
    return clock() if clock is not None else datetime.now(UTC)


def policy_of(request: Request) -> SessionPolicy:
    policy = getattr(request.app.state, "session_policy", None)
    if policy is not None:
        return policy
    s = get_settings()
    from datetime import timedelta

    return SessionPolicy(
        idle=timedelta(minutes=s.session_idle_minutes),
        absolute=timedelta(hours=s.session_absolute_hours),
        step_up=timedelta(minutes=s.step_up_minutes),
    )


def client_ip(request: Request) -> str:
    return (request.client.host if request.client else "unknown")[:64]


def check_origin(request: Request) -> None:
    """A browser-sent Origin must be one of ours. Requests without Origin (non-browser
    clients) still need the CSRF header, which a cross-site page cannot obtain."""
    origin = request.headers.get("origin")
    if origin is not None and origin not in get_settings().app_origins:
        raise error(403, "ORIGIN_REJECTED", "cross-origin request refused")


def current_principal(request: Request) -> Principal:
    token = request.cookies.get(cookie_name())
    p = service.resolve(engine_of(request), token, now=now_of(request), policy=policy_of(request))
    if p is None:
        raise error(401, "UNAUTHENTICATED", "sign in required (session missing or expired)")
    return p


def csrf_guard(request: Request, principal: Principal) -> None:
    if request.method not in UNSAFE:
        return
    check_origin(request)
    sent = request.headers.get("x-csrf-token", "")
    if not sent or not hmac.compare_digest(service.sha256(sent), principal.csrf_sha256):
        raise error(403, "CSRF_FAILED", "missing or invalid CSRF token")
    engine, now = engine_of(request), now_of(request)
    from datetime import timedelta

    with engine.begin() as conn:
        limited = service.hit_rate_limit(
            conn,
            f"mut:{principal.session_id}",
            limit=policy_of(request).mutations_per_minute,
            window=timedelta(minutes=1),
            now=now,
        )
    if limited:
        raise error(429, "RATE_LIMITED", "too many requests; slow down")


def authenticated(request: Request) -> Principal:
    """Principal + CSRF protection for unsafe methods (routes without merchant scope)."""
    p = current_principal(request)
    csrf_guard(request, p)
    return p


@dataclass(frozen=True, slots=True)
class MerchantCtx:
    principal: Principal
    membership: Membership
    merchant_id: str
    engine: Engine
    now: datetime
    policy: SessionPolicy
    request: Request


def merchant_scope(permission: Permission, *, step_up: bool = False) -> Callable[..., MerchantCtx]:
    """Membership with `permission` in the path merchant; `step_up` additionally needs a
    password check within the step-up window (money- or terms-changing decisions)."""

    def dep(merchant_id: str, request: Request, p: Annotated[Principal, Depends(authenticated)]):
        engine, now = engine_of(request), now_of(request)
        try:
            with engine.begin() as conn:
                m = service.require_membership(conn, p, merchant_id, permission)
        except service.NotFoundError:
            audit.append_separately(
                engine,
                at=now,
                action="access.denied",
                object_type="merchant",
                object_id=merchant_id[:120],
                outcome="denied",
                actor_user_id=p.user_id,
                session_id=p.session_id,
                detail={"reason": "NO_ACTIVE_MEMBERSHIP", "permission": permission.value},
            )
            raise error(404, "NOT_FOUND", "not found") from None
        except service.ForbiddenError as exc:
            audit.append_separately(
                engine,
                at=now,
                action="access.denied",
                object_type="merchant",
                object_id=merchant_id,
                outcome="denied",
                actor_user_id=p.user_id,
                session_id=p.session_id,
                merchant_id=merchant_id,
                detail={"reason": "PERMISSION", "permission": permission.value},
            )
            raise error(403, "FORBIDDEN", str(exc)) from None
        if step_up and not p.recently_authenticated(now, policy_of(request)):
            raise error(401, "REAUTH_REQUIRED", "confirm your password to continue")
        return MerchantCtx(p, m, merchant_id, engine, now, policy_of(request), request)

    return dep
