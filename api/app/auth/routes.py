"""`/v1/auth`: login, session, re-authentication (step-up), logout."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from api.app.auth import service
from api.app.auth.deps import (
    authenticated,
    check_origin,
    client_ip,
    cookie_name,
    current_principal,
    engine_of,
    error,
    now_of,
    policy_of,
)
from api.app.auth.service import Principal
from api.config import get_settings

router = APIRouter(prefix="/v1/auth", tags=["auth"])


class LoginBody(BaseModel):
    login: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=1024)


class ReauthBody(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


def _session_body(request: Request, p: Principal, csrf: str) -> dict:
    with engine_of(request).begin() as conn:
        ms = service.memberships(conn, p.user_id)
    policy = policy_of(request)
    return {
        "user": {
            "user_id": p.user_id,
            "login": p.login,
            "display_name": p.display_name,
            "is_test_identity": p.is_test_identity,
            "identity_label": "TEST IDENTITY" if p.is_test_identity else None,
        },
        "session": {
            "session_id": p.session_id,
            "authenticated_at": p.authenticated_at.isoformat(),
            "absolute_expires_at": p.absolute_expires_at.isoformat(),
            "step_up_valid": p.recently_authenticated(now_of(request), policy),
        },
        "csrf_token": csrf,
        "memberships": [
            {
                "merchant_id": m.merchant_id,
                "display_name": m.display_name,
                "is_synthetic": m.is_synthetic,
                "role": m.role,
                "permissions": sorted(x.value for x in m.permissions),
            }
            for m in ms
        ],
    }


@router.post("/login")
def login(body: LoginBody, request: Request, response: Response) -> dict:
    check_origin(request)
    old = request.cookies.get(cookie_name())
    try:
        new = service.login(
            engine_of(request),
            body.login,
            body.password,
            client_ip=client_ip(request),
            user_agent=request.headers.get("user-agent", ""),
            now=now_of(request),
            policy=policy_of(request),
            replaces_token=old,
        )
    except service.RateLimitedError as exc:
        raise error(429, exc.code, str(exc)) from None
    except service.InvalidCredentialsError as exc:
        raise error(401, exc.code, str(exc)) from None
    policy = policy_of(request)
    response.set_cookie(
        cookie_name(),
        new.token,
        max_age=int(policy.absolute.total_seconds()),
        httponly=True,
        secure=get_settings().cookie_secure,
        samesite="strict",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return _session_body(request, new.principal, new.csrf_token)


@router.get("/session")
def session(
    request: Request, response: Response, p: Annotated[Principal, Depends(current_principal)]
):
    response.headers["Cache-Control"] = "no-store"
    token = request.cookies.get(cookie_name(), "")
    return _session_body(request, p, service.csrf_for(token))


@router.post("/reauth")
def reauth(
    body: ReauthBody, request: Request, p: Annotated[Principal, Depends(authenticated)]
) -> dict:
    try:
        service.reauthenticate(
            engine_of(request), p, body.password, now=now_of(request), policy=policy_of(request)
        )
    except service.RateLimitedError as exc:
        raise error(429, exc.code, str(exc)) from None
    except service.InvalidCredentialsError as exc:
        raise error(401, exc.code, str(exc)) from None
    return {"step_up_valid": True}


@router.post("/logout")
def logout(
    request: Request, response: Response, p: Annotated[Principal, Depends(authenticated)]
) -> dict:
    service.logout(engine_of(request), p, now=now_of(request))
    response.delete_cookie(cookie_name(), path="/", secure=get_settings().cookie_secure)
    return {"signed_out": True}
