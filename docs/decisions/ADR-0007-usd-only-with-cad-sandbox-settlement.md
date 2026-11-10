# ADR-0007: USD-only optimizer with a CAD-settling Sandbox merchant

- **Date:** 2026-11-10 · **Status:** Accepted (owner decision F1)
- **Context:** The P0 Sandbox payment of `INV2-4RUV-XPEH-4F3C-T8BW` was invoiced and paid
  in USD (US$990.00), but the Sandbox Business account is Canadian: PayPal took a
  US$29.01 fee and credited CAD 1,306.79 (net US$960.99 at 1.35984225). D008 fixes one
  currency (USD cents) for the first demonstration; cross-currency is deferred
  (14_BUILD_ROADMAP R5).
- **Decision:** Keep the existing Canadian Sandbox Business account and its verified
  payment evidence. The initial optimizer and money model stay USD-only. A CAD receipt is
  never counted as spendable USD. Provider settlement data (gross, fee, net, receivable
  currency and amount, exchange rate) is stored as separate fields with provenance, never
  collapsed into one "received" number.
- **Alternatives:** (a) create a US Sandbox Business account now: deferred, may be added
  later for USD-only integration tests without delaying P1; (b) add FX handling: rejected
  for now (D008, R5).
- **Affected files:** tracker (*Owner decisions*, *Settlement record*), evidence
  `RECONCILIATION_INV2-4RUV-XPEH-4F3C-T8BW.json`. P1 money model must carry
  `settlement_currency` and fee fields.
- **Finance impact:** fees reduce available proceeds (FIN-1); FX settlement means a received
  USD payment cannot be assumed to fund an immediate downstream USD payment (FIN-2).
  Capacity from this merchant's PayPal receipts is not USD-deployable.
- **Privacy impact:** none; no new data collected.
- **Test changes (P1):** money-model tests must reject using a non-USD settlement as USD
  capacity and must apply provider-reported fees.
