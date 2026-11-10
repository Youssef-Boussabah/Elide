# ADR-0008: Phase 1 financial kernel design choices

- **Date:** 2026-11-10 · **Status:** Accepted (implementation choices; no frozen decision changed)
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
