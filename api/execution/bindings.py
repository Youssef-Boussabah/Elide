"""Provider account bindings (ADR-0012 §2.1).

A binding maps an Elide merchant to a Sandbox account *by reference*. Credentials stay in
the untracked `.env`; the database holds only the reference name and fingerprints. Only
the references below can be resolved, so a database row can never point the worker at an
arbitrary secret.

Bindings are created by the operator script `scripts/bind_sandbox_accounts.py` (owner
action, Sandbox test accounts only), never from the merchant UI or an API request.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Connection, select, update

from api.config import Settings
from api.execution.tables import provider_account
from api.integrations.paypal.redaction import fingerprint
from api.workflow import audit

INVOICER_REF = "env:PAYPAL_CLIENT_ID"
PAYER_REF = "env:PAYPAL_SANDBOX_PAYER_EMAIL"
_RESOLVERS = {
    INVOICER_REF: lambda s: s.paypal_client_id,
    PAYER_REF: lambda s: s.paypal_sandbox_payer_email,
}
ROLE_REFS = {"invoicer": INVOICER_REF, "payer": PAYER_REF}


class BindingError(RuntimeError):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class Binding:
    account_id: str
    merchant_id: str
    role: str
    credential_ref: str
    credential_fingerprint: str
    account_fingerprint: str
    settlement_currency: str | None


def resolve(settings: Settings, ref: str) -> str:
    """The secret value behind an allowlisted reference. Never log the result."""
    fn = _RESOLVERS.get(ref)
    value = fn(settings) if fn else None
    if value is None:
        raise BindingError(f"{ref} is not configured", "CREDENTIAL_NOT_CONFIGURED")
    return value.get_secret_value()


def bind(
    conn: Connection,
    *,
    merchant_id: str,
    role: str,
    credential_fingerprint: str,
    account_fingerprint: str,
    consent_note: str,
    bound_by: str,
    at: datetime,
    settlement_currency: str | None = None,
    country: str | None = None,
    evidence_ref: str | None = None,
) -> str:
    if role not in ROLE_REFS:
        raise BindingError(f"unknown role {role!r}", "BAD_ROLE")
    if not consent_note.strip():
        raise BindingError("a binding needs the owner's recorded consent", "NO_CONSENT")
    account_id = "pa_" + secrets.token_hex(8)
    conn.execute(
        provider_account.insert().values(
            account_id=account_id,
            merchant_id=merchant_id,
            provider="paypal",
            environment="sandbox",
            role=role,
            credential_ref=ROLE_REFS[role],
            credential_fingerprint=credential_fingerprint,
            account_fingerprint=account_fingerprint,
            settlement_currency=settlement_currency,
            country=country,
            is_test_account=True,
            consent_note=consent_note,
            bound_by=bound_by,
            bound_at=at,
            evidence_ref=evidence_ref,
        )
    )
    audit.append(
        conn,
        at=at,
        action="provider_account.bound",
        object_type="provider_account",
        object_id=account_id,
        merchant_id=merchant_id,
        detail={"role": role, "environment": "sandbox", "fingerprint": account_fingerprint},
    )
    return account_id


def revoke(conn: Connection, account_id: str, *, reason: str, at: datetime) -> bool:
    res = conn.execute(
        update(provider_account)
        .where(provider_account.c.account_id == account_id, provider_account.c.revoked_at.is_(None))
        .values(revoked_at=at, revoke_reason=reason[:200])
    )
    return res.rowcount == 1


def active(conn: Connection, merchant_id: str, role: str) -> Binding | None:
    r = (
        conn.execute(
            select(provider_account).where(
                provider_account.c.merchant_id == merchant_id,
                provider_account.c.role == role,
                provider_account.c.revoked_at.is_(None),
            )
        )
        .mappings()
        .one_or_none()
    )
    if r is None:
        return None
    return Binding(
        r["account_id"],
        r["merchant_id"],
        r["role"],
        r["credential_ref"],
        r["credential_fingerprint"],
        r["account_fingerprint"],
        r["settlement_currency"],
    )


def by_id(conn: Connection, account_id: str) -> Binding:
    r = (
        conn.execute(select(provider_account).where(provider_account.c.account_id == account_id))
        .mappings()
        .one()
    )
    return Binding(
        r["account_id"],
        r["merchant_id"],
        r["role"],
        r["credential_ref"],
        r["credential_fingerprint"],
        r["account_fingerprint"],
        r["settlement_currency"],
    )


def check_pair(
    conn: Connection,
    *,
    payer_merchant: str,
    payee_merchant: str,
    provider_credential_fingerprint: str,
    payer_email: str | None,
) -> tuple[Binding, Binding]:
    """Both parties bound, the invoicer binding matches the credentials the provider adapter
    actually uses (wrong-payee guard) and the payer binding matches the configured payer."""
    inv = active(conn, payee_merchant, "invoicer")
    pay = active(conn, payer_merchant, "payer")
    if inv is None or pay is None:
        raise BindingError(
            "the payee or the payer has no active PayPal Sandbox binding",
            "PROVIDER_NOT_CONNECTED",
        )
    if inv.credential_fingerprint != provider_credential_fingerprint:
        raise BindingError(
            "the configured PayPal credentials do not belong to the payee's binding",
            "PAYEE_CREDENTIAL_MISMATCH",
        )
    if (
        payer_email is None
        or fingerprint(payer_email.strip().lower()) != pay.credential_fingerprint
    ):
        raise BindingError(
            "the configured Sandbox payer does not match the payer's binding",
            "PAYER_BINDING_MISMATCH",
        )
    return inv, pay
