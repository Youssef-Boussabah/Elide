"""Payer-side journal entries for a provider-verified invoice payment (ADR-0012 §7.3).

**Illustrative bookkeeping**, like `settlement_postings` for the payee. Two entries:

1. payable recognized (merchant data, not a provider fact):
     Dr purchases            A + C
     Cr accounts_payable:<obligation>   A + C
2. payment sent (cites the PayPal capture):
     Dr accounts_payable:<obligation>   A + C
     Cr paypal_payments_sent  A          (cash that left via PayPal)
     Cr discount_received     C          (the authorized concession; omitted if 0)

Afterwards the obligation's payable is zero. `paypal_payments_sent` records the outflow;
the payer's spendable capacity is decided by the liquidity layer from balance evidence,
never from this journal.
"""

from __future__ import annotations

from datetime import datetime

from api.accounting.journal import EntryKind, EvidenceClass, JournalEntry, Posting, ProviderEventRef
from api.domain.money import Money, SignedAmount


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
    sent = [
        Posting(ap, SignedAmount.debit(owed)),
        Posting("paypal_payments_sent", SignedAmount.credit(cash)),
    ]
    if concession.cents:
        sent.append(Posting("discount_received", SignedAmount.credit(concession)))
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
            idempotency_key=f"paypal:sandbox:capture:{capture_id.lower()}:payment_sent",
            kind=EntryKind.PROVIDER_PAYMENT_SENT,
            evidence_class=EvidenceClass.SANDBOX_VERIFIED,
            description=(
                f"Paid {cash} via PayPal capture {capture_id} for obligation {obligation_id}"
                + (f"; authorized concession {concession}" if concession.cents else "")
            ),
            occurred_at=paid_at,
            postings=tuple(sent),
            provider_ref=cap_ref,
        ),
    )
