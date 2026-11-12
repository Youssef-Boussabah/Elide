"""Roles and permission scopes (ADR-0011 §4). Roles are per merchant membership."""

from __future__ import annotations

from enum import StrEnum


class Permission(StrEnum):
    WORKSPACE_READ = "workspace:read"
    POLICY_EDIT = "policy:edit"
    OFFER_PROPOSE = "offer:propose"
    OFFER_DECIDE = "offer:decide"
    PAYMENT_AUTHORIZE = "payment:authorize"
    AUDIT_READ = "audit:read"
    MEMBERS_MANAGE = "members:manage"


P = Permission
ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    "owner": frozenset(P),
    "approver": frozenset(
        {P.WORKSPACE_READ, P.OFFER_PROPOSE, P.OFFER_DECIDE, P.PAYMENT_AUTHORIZE, P.AUDIT_READ}
    ),
    "policy_editor": frozenset({P.WORKSPACE_READ, P.POLICY_EDIT, P.OFFER_PROPOSE}),
    "auditor": frozenset({P.WORKSPACE_READ, P.AUDIT_READ}),
    "viewer": frozenset({P.WORKSPACE_READ}),
}

# Decisions that change money or terms need a recent password check (step-up).
STEP_UP = frozenset({P.POLICY_EDIT, P.OFFER_DECIDE, P.PAYMENT_AUTHORIZE, P.MEMBERS_MANAGE})


def permissions_for(role: str) -> frozenset[Permission]:
    return ROLE_PERMISSIONS.get(role, frozenset())
