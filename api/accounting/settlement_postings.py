"""Journal entries for one provider-verified invoice settlement (creditor's books).

**Illustrative bookkeeping, not an accounting-standards determination.** The entries model
a receivable, sales revenue, an authorized discount and the provider settlement so that
cash, fees and FX can be reconciled. Booking revenue when the invoice is issued is a
modeling simplification: a PayPal invoice on its own does not establish when revenue must
be recognized under the applicable accounting standards.

For the Phase 0 Sandbox payment this produces three entries, each with its own
idempotency key and provider reference. They are one accounting bundle and are imported
together (`api.accounting.settlement_import`):

1. invoice issued      Dr accounts_receivable 1,000.00 USD / Cr revenue (illustrative)
2. discount authorized Dr contra_revenue (discount) 10.00 USD / Cr accounts_receivable
3. capture settlement  (one entry, balanced per currency)
     USD: Dr paypal_settlement_clearing 990.00   Cr accounts_receivable 990.00
          Dr expense:paypal_fees          29.01  Cr paypal_settlement_clearing 29.01
          Dr fx_conversion_position      960.99  Cr paypal_settlement_clearing 960.99
     CAD: Dr paypal_balance            1,306.79  Cr fx_conversion_position 1,306.79

What remains afterwards: the receivable and the USD clearing account are zero. Revenue,
the discount, the fee expense and the FX conversion position legitimately keep balances.
`fx_conversion_position` (USD +960.99 / CAD -1,306.79) records the conversion PayPal
performed; it is **not** merchant cash. The USD-equivalent net proceeds (960.99) and the
CAD credit (1,306.79) are two views of one receipt, not two cash receipts: the only cash
asset is the CAD `paypal_balance`. That balance is provider-reported, not verified
spendable capacity (D006); capacity is decided by the liquidity layer, and CAD can never be
USD capacity (D008).
"""

from __future__ import annotations

from api.accounting.journal import (
    EntryKind,
    EvidenceClass,
    JournalEntry,
    Posting,
    ProviderEventRef,
)
from api.domain.money import Money, SignedAmount
from api.domain.settlement import ProviderSettlement


def _dr(account: str, m: Money) -> Posting:
    return Posting(account, SignedAmount.debit(m))


def _cr(account: str, m: Money) -> Posting:
    return Posting(account, SignedAmount.credit(m))


def settlement_entries(s: ProviderSettlement, entity_id: str) -> tuple[JournalEntry, ...]:
    inv_key = f"paypal:{s.environment}:invoice:{s.invoice_id.lower()}"
    cap_key = f"paypal:{s.environment}:capture:{s.capture_id.lower()}"
    ar = f"accounts_receivable:{s.invoice_id}"
    inv_ref = ProviderEventRef("paypal", s.environment, "invoice", s.invoice_id)
    cap_ref = ProviderEventRef("paypal", s.environment, "capture", s.capture_id)
    ordered_at = s.ordering_time()  # never the disputed timestamp (INV-1)
    issued_at = s.invoice_created_at or ordered_at

    entries = [
        JournalEntry(
            entity_id=entity_id,
            idempotency_key=f"{inv_key}:issued",
            kind=EntryKind.INVOICE_ISSUED,
            evidence_class=EvidenceClass.SANDBOX_VERIFIED,
            description=(
                f"Invoice {s.invoice_id} issued for {s.invoice_face} (illustrative receivable; "
                "not a revenue-recognition determination)"
            ),
            occurred_at=issued_at,
            postings=(_dr(ar, s.invoice_face), _cr("revenue:sales", s.invoice_face)),
            provider_ref=inv_ref,
        )
    ]
    if s.authorized_discount.cents:
        entries.append(
            JournalEntry(
                entity_id=entity_id,
                idempotency_key=f"{inv_key}:concession",
                kind=EntryKind.CONCESSION_AUTHORIZED,
                evidence_class=EvidenceClass.SANDBOX_VERIFIED,
                description=f"Authorized invoice discount {s.authorized_discount}",
                occurred_at=issued_at,
                postings=(
                    _dr("contra_revenue:early_payment_discount", s.authorized_discount),
                    _cr(ar, s.authorized_discount),
                ),
                provider_ref=inv_ref,
            )
        )

    clearing = "paypal_settlement_clearing"
    postings = [
        _dr(clearing, s.gross),
        _cr(ar, s.gross),
    ]
    if s.provider_fee.cents:
        postings += [_dr("expense:paypal_fees", s.provider_fee), _cr(clearing, s.provider_fee)]
    if s.is_fx_settlement:
        postings += [
            _dr("fx_conversion_position", s.net),
            _cr(clearing, s.net),
            _dr("paypal_balance", s.settlement_credit),
            _cr("fx_conversion_position", s.settlement_credit),
        ]
    else:
        postings += [_dr("paypal_balance", s.net), _cr(clearing, s.net)]
    rate = f" at {s.exchange_rate.rate}" if s.exchange_rate else ""
    entries.append(
        JournalEntry(
            entity_id=entity_id,
            idempotency_key=f"{cap_key}:settlement",
            kind=EntryKind.PROVIDER_SETTLEMENT,
            evidence_class=EvidenceClass.SANDBOX_VERIFIED,
            description=(
                f"Capture {s.capture_id}: gross {s.gross}, fee {s.provider_fee}, net {s.net}, "
                f"credited {s.settlement_credit}{rate}"
            ),
            occurred_at=ordered_at,
            postings=tuple(postings),
            provider_ref=cap_ref,
        )
    )
    return tuple(entries)
