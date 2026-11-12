"""Payer attribution and verification status of a settled payment (ADR-0012 §17.2, §17.3).

PayPal's invoice GET proves the invoice was PAID and the capture GET proves the payee
received the money; **neither names the paying account**. Elide therefore keeps three
separately evidenced facts for each settlement:

- obligation settlement: PROVIDER_CONFIRMED (invoice PAID, capture COMPLETED, to the bound
  payee account, independently checked);
- payer authorization: MERCHANT_APPROVED (the payer merchant approved the leg and, for a held
  intent, released it; recorded in the authorization, state events and audit chain);
- payment source: NOT_VERIFIED until the payer merchant attests it
  (MERCHANT_ATTESTED_OWN_ACCOUNT or MERCHANT_ATTESTED_THIRD_PARTY). PROVIDER_VERIFIED is
  reserved for a provider read that names the paying account (Transaction Search
  `payer_info`, ADR-0012 §17.2) and is not implemented: nothing in Elide produces it.

A third party paying the invoice discharges the debt all the same (the payable clears);
only the payer's own cash outflow is unknown. The settled-outflow commitment stays as a
conservative hold either way: it is released only by newer balance evidence that lists it,
which is correct whoever paid.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Connection, select, text

from api.accounting.payer_postings import PaymentSource, payment_source_entry
from api.accounting.reconciliation_check import PLACEHOLDER_SHA
from api.accounting.store import post_entries_in
from api.domain.money import USD, Money
from api.execution.tables import (
    evidence_correction,
    payment_source_attestation,
    provider_settlement,
    settlement_verification,
)
from api.workflow import audit
from api.workflow.store import ConflictError, NotVisibleError
from api.workflow.tables import payment_intent

STATUS = {
    None: "NOT_VERIFIED",
    PaymentSource.OWN_PAYPAL_ACCOUNT.value: "MERCHANT_ATTESTED_OWN_ACCOUNT",
    PaymentSource.THIRD_PARTY.value: "MERCHANT_ATTESTED_THIRD_PARTY",
}
NOTE = (
    "PayPal confirms the invoice was paid and the payee received the money; it does not "
    "identify the paying account"
)


def _legacy(conn: Connection, capture_id: str) -> bool:
    return bool(
        conn.execute(
            text(
                "SELECT 1 FROM journal_entry WHERE kind = 'provider_payment_sent' "
                "AND provider_resource_type = 'capture' AND provider_resource_id = :c"
            ),
            {"c": capture_id},
        ).first()
    )


def verification_view(conn: Connection, st: Any) -> dict[str, Any]:
    """The newest verification attestation (safe for either party: digests and verdicts,
    never the document, which spans both merchants' books)."""
    rows = conn.execute(
        select(
            settlement_verification.c.purpose,
            settlement_verification.c.verdict,
            settlement_verification.c.checker_version,
            settlement_verification.c.document_sha256,
            settlement_verification.c.verified_at,
        )
        .where(settlement_verification.c.capture_id == st["capture_id"])
        .order_by(settlement_verification.c.id)
    ).all()
    out: dict[str, Any] = {
        "attestations": [
            {
                "purpose": p,
                "verdict": v,
                "checker_version": cv,
                "document_sha256": d,
                "verified_at": at.isoformat(),
            }
            for p, v, cv, d, at in rows
        ],
        "legacy_check_sha256_placeholder": st["check_sha256"] == PLACEHOLDER_SHA,
    }
    out["status"] = rows[-1][1].upper() if rows else "NOT_ATTESTED"
    if out["legacy_check_sha256_placeholder"]:
        out["note"] = "check_sha256 on this pre-0008 record is a placeholder, not a digest"
    return out


def payer_attribution(conn: Connection, st: Any) -> dict[str, Any]:
    att = conn.execute(
        select(payment_source_attestation.c.source, payment_source_attestation.c.attested_at).where(
            payment_source_attestation.c.capture_id == st["capture_id"]
        )
    ).first()
    corrections = conn.execute(
        select(evidence_correction.c.aspect, evidence_correction.c.corrected_to).where(
            evidence_correction.c.subject_table == "provider_settlement",
            evidence_correction.c.subject_id == st["capture_id"],
        )
    ).all()
    return {
        "obligation_settlement": "PROVIDER_CONFIRMED",
        "payer_authorization": "MERCHANT_APPROVED",
        "payment_source": STATUS[att.source if att else None],
        "payment_source_attested_at": att.attested_at.isoformat() if att else None,
        "payment_source_note": NOTE,
        "outflow_commitment_basis": "PRESUMED_OUTFLOW_HELD_CONSERVATIVELY",
        "corrections": [{"aspect": a, "corrected_to": c} for a, c in corrections],
    }


def attest_payment_source(
    conn: Connection,
    *,
    merchant_id: str,
    user_id: str,
    intent_id: str,
    source: PaymentSource,
    at: datetime,
) -> dict[str, Any]:
    """The payer merchant states, once, where a settled payment's money came from.

    Merchant-attested, never provider-verified. Own account: the unverified payment source
    is reclassified as `paypal_payments_sent` (nothing posts for a pre-0008 capture, whose
    books already say so). Third party: it moves to `third_party_payment:<capture>`. It
    never changes the obligation, the payee's books or any cash commitment."""
    intent = (
        conn.execute(
            select(payment_intent).where(payment_intent.c.intent_id == intent_id).with_for_update()
        )
        .mappings()
        .one_or_none()
    )
    if intent is None or intent["payer_merchant"] != merchant_id:
        raise NotVisibleError("not found")
    st = (
        conn.execute(
            select(provider_settlement).where(provider_settlement.c.intent_id == intent_id)
        )
        .mappings()
        .one_or_none()
    )
    if st is None or intent["status"] != "reconciled":
        raise ConflictError("the payment is not reconciled", code="NOT_RECONCILED")
    if conn.execute(
        select(payment_source_attestation.c.id).where(
            payment_source_attestation.c.capture_id == st["capture_id"]
        )
    ).first():
        raise ConflictError("the payment source is already attested", code="ALREADY_ATTESTED")
    legacy = _legacy(conn, st["capture_id"])
    entry = payment_source_entry(
        payer_entity=merchant_id,
        capture_id=st["capture_id"],
        cash=Money(int(st["gross_cents"]), USD),
        source=source,
        legacy=legacy,
        attested_at=at,
        attested_by=f"user {user_id}",
    )
    entry_id = None if entry is None else post_entries_in(conn, (entry,))[0].entry_id
    conn.execute(
        payment_source_attestation.insert().values(
            capture_id=st["capture_id"],
            intent_id=intent_id,
            merchant_id=merchant_id,
            source=source.value,
            attested_by=user_id,
            attested_at=at,
            journal_entry_id=entry_id,
        )
    )
    audit.append(
        conn,
        at=at,
        action="payment.source_attested",
        object_type="payment_intent",
        object_id=intent_id,
        actor_user_id=user_id,
        merchant_id=merchant_id,
        detail={"source": source.value, "journal_entry_posted": entry_id is not None},
    )
    return {
        "intent_id": intent_id,
        "capture_id": st["capture_id"],
        "payment_source": STATUS[source.value],
        "evidence_class": "merchant_attested",
        "journal_entry_posted": entry_id is not None,
        "cash_commitment_changed": False,
    }
