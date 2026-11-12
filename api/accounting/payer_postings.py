"""Payer-side journal entries for a provider-verified invoice payment (ADR-0012 §7.3, §17.2).

**Illustrative bookkeeping**, like `settlement_postings` for the payee.

What PayPal proves, and what it does not: the invoice GET shows the invoice PAID and the
capture GET shows the payee received the money. Neither identifies **which PayPal account
paid**. So the payer's books separate three things:

1. payable recognized (merchant data, `merchant_attested`):
     Dr purchases                          A + C
     Cr accounts_payable:<obligation>      A + C
2. obligation discharged (provider-confirmed, `sandbox_verified`, cites the capture):
     Dr accounts_payable:<obligation>      A + C
     Cr payment_source_unverified:<capture> A   (paid by an account PayPal does not name)
     Cr discount_received                  C   (the authorized concession; omitted if 0)
3. payment source (only when the payer merchant attests it, `merchant_attested`):
     own account:  Dr payment_source_unverified:<capture> A / Cr paypal_payments_sent A
     third party:  Dr payment_source_unverified:<capture> A / Cr third_party_payment:<capture> A

Afterwards the payable is zero (the debt is discharged whoever paid). `paypal_payments_sent`
is credited only on the payer's own attestation, never on provider evidence alone. The
payer's spendable capacity is decided by the liquidity layer from balance evidence, never
from this journal; the settled outflow stays committed until newer balance evidence (a
conservative hold that is correct whoever paid).

Pilot entries booked before migration 0008 used one `provider_payment_sent` entry that
credited `paypal_payments_sent` directly as `sandbox_verified`; they are immutable and
carry an append-only `evidence_correction` (`payer_source_debit` -> `unverified`).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from api.accounting.journal import EntryKind, EvidenceClass, JournalEntry, Posting, ProviderEventRef
from api.domain.money import Money, SignedAmount

UNVERIFIED = "payment_source_unverified:{capture}"
THIRD_PARTY = "third_party_payment:{capture}"
SENT = "paypal_payments_sent"


class PaymentSource(StrEnum):
    OWN_PAYPAL_ACCOUNT = "own_paypal_account"
    THIRD_PARTY = "third_party"


def payer_entries(
    *,
    payer_entity: str,
    obligation_id: str,
    capture_id: str,
    cash: Money,
    concession: Money,
    recognized_at: datetime,
    paid_at: datetime,
) -> tuple[JournalEntry, ...]:
    ap = f"accounts_payable:{obligation_id}"
    owed = cash + concession
    cap_ref = ProviderEventRef("paypal", "sandbox", "capture", capture_id)
    discharged = [
        Posting(ap, SignedAmount.debit(owed)),
        Posting(UNVERIFIED.format(capture=capture_id), SignedAmount.credit(cash)),
    ]
    if concession.cents:
        discharged.append(Posting("discount_received", SignedAmount.credit(concession)))
    return (
        JournalEntry(
            entity_id=payer_entity,
            idempotency_key=f"elide:obligation:{obligation_id.lower()}:payable_recognized",
            kind=EntryKind.PAYABLE_RECOGNIZED,
            evidence_class=EvidenceClass.MERCHANT_ATTESTED,
            description=f"Payable for obligation {obligation_id}: {owed} (illustrative)",
            occurred_at=recognized_at,
            postings=(
                Posting("purchases", SignedAmount.debit(owed)),
                Posting(ap, SignedAmount.credit(owed)),
            ),
        ),
        JournalEntry(
            entity_id=payer_entity,
            idempotency_key=f"paypal:sandbox:capture:{capture_id.lower()}:obligation_discharged",
            kind=EntryKind.OBLIGATION_DISCHARGED,
            evidence_class=EvidenceClass.SANDBOX_VERIFIED,
            description=(
                f"Obligation {obligation_id} discharged by PayPal capture {capture_id} "
                f"({cash} received by the payee"
                + (f"; authorized concession {concession}" if concession.cents else "")
                + "); the paying account is not identified by PayPal"
            ),
            occurred_at=paid_at,
            postings=tuple(discharged),
            provider_ref=cap_ref,
        ),
    )


def payment_source_entry(
    *,
    payer_entity: str,
    capture_id: str,
    cash: Money,
    source: PaymentSource,
    legacy: bool,
    attested_at: datetime,
    attested_by: str,
) -> JournalEntry | None:
    """The payer merchant's own statement of where the money came from.

    `legacy`: the capture was booked before 0008, when `paypal_payments_sent` was already
    credited. An own-account attestation then posts nothing (the books already say so); a
    third-party one moves the credit to `third_party_payment:<capture>`."""
    unverified, third = (
        UNVERIFIED.format(capture=capture_id),
        THIRD_PARTY.format(capture=capture_id),
    )
    if legacy and source is PaymentSource.OWN_PAYPAL_ACCOUNT:
        return None
    debit = SENT if legacy else unverified
    credit = SENT if source is PaymentSource.OWN_PAYPAL_ACCOUNT else third
    what = (
        "paid from its own PayPal account"
        if source is PaymentSource.OWN_PAYPAL_ACCOUNT
        else "paid by a third party, not by this merchant"
    )
    return JournalEntry(
        entity_id=payer_entity,
        idempotency_key=f"paypal:sandbox:capture:{capture_id.lower()}:payment_source",
        kind=EntryKind.PAYMENT_SOURCE_ATTESTED,
        evidence_class=EvidenceClass.MERCHANT_ATTESTED,
        description=f"Payer attests capture {capture_id} ({cash}) was {what} (by {attested_by})",
        occurred_at=attested_at,
        postings=(
            Posting(debit, SignedAmount.debit(cash)),
            Posting(credit, SignedAmount.credit(cash)),
        ),
        provider_ref=ProviderEventRef("paypal", "sandbox", "capture", capture_id),
    )
