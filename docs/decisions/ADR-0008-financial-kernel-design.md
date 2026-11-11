# ADR-0008: Phase 1 financial kernel design choices

- **Date:** 2026-11-10, amended 2026-11-11 (review remediation) · **Status:** Accepted (implementation choices; no frozen decision changed)
- **Context:** Phase 1 implements money, invoices, capacity, provider settlement and the
  journal (execution plan Phase 1; docs 04, 05, 08, 09, 10, 11). Several details were
  left open by the source documents.

## Decisions

1. **Two amount types.** `Money` is nonnegative integer minor units with a currency;
   `SignedAmount` is a nonzero signed journal movement. They do not mix, so a debit/credit
   can never be used as a payable amount. Rates are `Decimal`; conversion rounds half-up to
   the cent, which reproduces the observed PayPal value (960.99 × 1.35984225 → 1,306.79).
2. **Known vs spendable currencies.** `USD` and `CAD` are known (they can appear as facts);
   only `USD` is spendable (`require_spendable`). Capacity evidence in CAD is
   `CURRENCY_BLOCKED` (D008, ADR-0007).
3. **Settlement identity without set-off.** `original_open = cash_applied +
   authorized_concession + remaining_open`. Doc 04's `valid_noncash_setoff` term is omitted
   because set-off is out of scope (D011); there is no code path that could introduce it.
4. **Revisions.** Revision 1 is the original issued terms (no concession). Later revisions
   are append-only, numbered contiguously and cannot be backdated. A concession counts only
   in an AUTHORIZED revision signed by **both** debtor and creditor; DRAFT (e.g.
   AI-proposed) and REJECTED revisions never count; an expired draft cannot be authorized.
5. **Over-settlement is refused.** A payment or concession that would make
   `remaining_open` negative is rejected rather than absorbed. Overpayments must be
   recorded separately later (not implemented).
6. **Capacity.** `raw = verified_spendable + verified_incoming − reserve − committed`;
   `deployable = max(0, raw)` and `deficit = max(0, −raw)` are both reported. Evidence is
   valid on `[observed_at, valid_until)`. Held, pending, promised and provider-confirmed
   (`PAID`) receipts add zero. A verified receipt counts only if available **strictly
   before** the evaluated instant (no same-slot reuse) and not already included in the
   observed balance. Synthetic evidence deploys cash only with `allow_synthetic=True`.
7. **One cash receipt per settlement.** A cross-currency capture is one journal entry,
   balanced separately in each currency through `fx_conversion_position`. The USD clearing
   account nets to zero, so the only cash asset is the CAD `paypal_balance`. That balance
   is provider-reported, not spendable capacity.
8. **Two dedup layers.** `provider_event_inbox` dedupes notifications by provider event
   id. `journal_entry` dedupes economic events by idempotency key **and** by a partial
   unique index on (provider resource, kind), so the same capture cannot be booked under a
   second key. Same key with different content is a conflict (error), not a duplicate.
9. **Database guards.** Journal, inbox and evidence tables are append-only (triggers refuse
   UPDATE/DELETE/TRUNCATE). A deferred constraint trigger requires ≥ 2 postings balanced
   per currency at COMMIT. Commitments may only be released once.
10. **No double-counted cash across plans.** `reserve_capacity` takes a per-merchant
    transaction-scoped advisory lock, re-reads evidence and active commitments, checks with
    the pure function and inserts, all in one transaction.
11. **Disputed timestamps.** The settlement record keeps every provider timestamp with its
    source field and reliability. The invoice transaction time (INV-1) is DISPUTED and is
    never used for ordering; journal dates use the capture `create_time` and the invoice
    `metadata.create_time`.
12. **Infrastructure.** Default DB host is `127.0.0.1`: on this machine `localhost`
    resolved to `::1`, where a connection to port 5433 hung until timeout (measured: 0.01 s
    vs 4.1 s). The Alembic engine now has a connect timeout. `compose.yaml` no longer fixes
    `container_name`, so separate checkouts get separate containers (the existing volume
    `elide_elide-pgdata` was reused; data preserved).

## Alternatives considered

- Floats or `Decimal` for amounts: rejected (D008, no inexact arithmetic).
- Storing CAD credit and USD net as two receipts: rejected (double counting).
- `SELECT ... FOR UPDATE` on one evidence row instead of an advisory lock: rejected because
  two plans could reserve against two different evidence rows of the same merchant.
- Mutable `status` on journal rows: rejected; corrections are reversal entries.

## Finance / privacy impact

Prevents spending reserves, held funds, provider-confirmed-but-unverified receipts and
non-USD balances; prevents double posting. No new personal data is stored; journal and
evidence tables hold amounts, opaque ids and provider resource ids only.

## Test changes

`tests/unit/domain`, `tests/unit/liquidity`, `tests/unit/accounting`, `tests/unit/scenarios`,
`tests/property/*`, `tests/integration/test_financial_kernel_db.py` (throwaway database per
session, real migrations).

## Addendum 2026-11-11: Phase 1 architectural review remediation

The owner's review of `91b8152` found six problems. Each was reproduced against that
commit before it was fixed (`evidence/phase1/review_reproduction_2026-11-11.txt`).
Decisions 1–12 still hold; the following refine them.

13. **One receipt counts once (review finding 1, critical).** `compute_capacity` keys
    receipts by (merchant, `receipt_id`). Identical copies collapse. Two different records
    for one receipt (amount, currency, status, availability) raise `ReceiptConflictError`:
    the caller supplies the receipt's current state once. `mark_available` produces the
    verified state as a *replacement* of the held record, never a second receipt.
    Receipts of other merchants are ignored, not merged. `included_receipt_ids` still
    excludes receipts already in the observed balance.
14. **Availability needs its own evidence (finding 3).** `AVAILABLE_VERIFIED` requires an
    `AvailabilityEvidence` with a basis: `SYNTHETIC` (synthetic receipts only; counts only
    with `allow_synthetic=True`), `MERCHANT_ATTESTED` (only the receiving merchant may
    attest), or `PROVIDER_BALANCE` (an independently fetched available-balance check, none
    implemented yet). The evidence reference must differ from the payment confirmation
    that created the receipt, so a PayPal `PAID` invoice or `COMPLETED` capture cannot
    vouch for its own spendability. A receipt counts from `max(available_at, verified_at)`.
    `CapacityEvidence.kind` must match its provenance source (synthetic ⇔ synthetic
    fixture; merchant-attested ⇐ merchant attestation; provider-verified ⇐ PayPal Sandbox).
15. **Evidence persistence and supersession (finding 2).** Migration `0003` stores
    `included_receipt_ids` (immutable with the append-only row), so evidence round-trips
    exactly; identical replays are no-ops and conflicting ids raise
    `EvidenceConflictError`. **Selection rule:** for one merchant and currency, the latest
    observation with `observed_at <= at` is authoritative; older snapshots are superseded
    even while their own validity window is open, and an expired newest snapshot does not
    fall back to an older one. `reserve_capacity` and `capacity_now` refuse a superseded
    evidence id (`SupersededEvidenceError`); `current_evidence` returns the current one. A
    unique index allows one observation per (merchant, currency, instant).
16. **Offer expiry vs accepted terms (finding 4).** `expires_at` exists only on offers
    (DRAFT). An acceptance (AUTHORIZED with `supersedes` = the offer) must be made before
    the deadline, by both parties, with exactly the offered concession, due date and
    timezone. Rejected, accepted and countered offers are closed. Accepted terms have no
    expiry and stay in force after the offer deadline (and after a refund) until later
    authorized terms replace them. A rejection must reference an offer. Payment
    authorization and plan expiry are not invoice properties (P2 commitments, P4
    approvals). Two parties may still sign fresh terms without `supersedes`; that is a new
    agreement, not an acceptance of an expired offer. Conditional (lapsing) early-payment
    discounts are not modeled.
17. **Reversal evidence (finding 5).** `JournalEntry.reversal` requires an explicit
    `evidence_class`; nothing is inherited. Local corrections are `MANUAL_ADJUSTMENT` (new
    class, no provider resource). `SANDBOX_VERIFIED` reversals must cite a provider
    `refund` or `dispute` resource different from the original. Synthetic and real books
    never reverse each other. The store re-checks this against the persisted original, and
    since `0003` one provider refund can back only one reversal. Partial provider refunds
    and refund verification (GET) are not implemented.
18. **Atomic settlement import (finding 6).** `post_entries` books a bundle in one
    transaction; `import_settlement` uses it for the invoice, discount and capture entries.
    Reconciliation is derived (`bundle_status`: NOT_IMPORTED / PARTIAL / COMPLETE /
    CONFLICT) from the booked rows and content digests; there is no stored "reconciled"
    flag. A bundle partly booked by single-entry posting reports PARTIAL and can be
    completed idempotently.
19. **Concurrent replay race (found while fixing 6).** `ON CONFLICT` targeted only the
    idempotency key, so a concurrent replay of a provider-referenced entry could hit the
    provider-event index first and raise instead of returning DUPLICATE (seen in 2 of 8
    runs). Nothing was double-booked, but replays were not reliably idempotent. The insert
    now treats every unique index as an arbiter and then classifies the conflict.
20. **Accounting terminology (finding 7).** The seller-side entries are illustrative
    bookkeeping. Booking revenue at invoice issue is a simplification, not a
    revenue-recognition determination. After the Sandbox settlement only the receivable
    and USD clearing are zero; revenue, discount, fee and the FX conversion position keep
    balances. `fx_conversion_position` is not merchant cash; the USD-equivalent net and the
    CAD credit are one receipt.

Remediation tests: `tests/unit/liquidity/test_receipt_identity_and_evidence.py`,
`tests/unit/domain/test_revision_lifecycle.py`, `tests/unit/accounting/test_reversal_evidence.py`,
`tests/property/test_receipt_and_evidence_props.py`, `tests/property/test_revision_props.py`,
`tests/integration/test_review_remediation_db.py`.
