# 04 — Liquidity Research, Accounting, and Core Financial Definitions

## Terms with precise meanings
- **Cash balance:** ledger cash in merchant's designated account at observation time.
- **Spendable/available funds:** cash verified free of holds and restrictions and usable for the proposed payment rail; may differ from statement or pending balance.
- **Protected reserve:** amount required for payroll, tax, rent, buffers and merchant policy; cannot be deployed.
- **Committed cash:** funds already reserved to other valid operations; prevent double-allocation.
- **Deployable capacity:** `max(0, verified_spendable − protected_reserve − other_committed_cash)` in same currency and within evidence validity.
- **Incoming receivable:** promise or settled-but-not-yet-spendable funds; *zero additional deployable cash until verified available*.
- **Liquidity gap:** `max(0, required_due_outflows + required_reserve + fees − available_cash − already committed usable inflows at that time)` under an explicit schedule.
- **Early cash commitment:** amount brought forward versus baseline; measure both dollars and time outstanding (e.g. dollar-days).
- **Gross throughput:** sum of separate valid payment leg amounts. Cannot be equated with newly created assets, economic profit, or funds released.
- **Incremental on-time settlement:** additional original invoice face amount paid by deadline compared with a specified valid baseline. A payment already due/on-time under baseline does not count as incremental.

## Three distinct mechanisms
### A. Sequencing existing cash
A business with spendable cash pays another; recipient pays once money is usable. A $100 seed may circulate through three $100 obligations for $300 gross transfer volume. This is sequential settlement, not debt cancellation. It is conditional on transfer availability, fees, reserves and each approval.

### B. Negotiated payment acceleration
A buyer voluntarily pays a discounted invoice before its contractual due time. Buyer gains discount but incurs an opportunity cost from earlier cash deployment. Supplier gains earlier liquidity but bears concession, fees and possible risk. Downstream invoices may become feasible. Changes must be consented and accurately reflected in accounting.

### C. Multilateral obligation set-off (research, not PayPal hackathon implementation)
For a directed cycle A owes B, B owes C, C owes A, it may be possible under legally valid agreements to extinguish mutually balancing portions **without moving cash**. Model this separately; it is not PayPal's ordinary payment flow. Requirements include all-party contractual authorization, accounting treatment, rights of set-off, dispute and insolvency considerations, and atomicity. Do not combine noncash cleared face value with PayPal processing volume.

## Canonical USD illustration
Buyer opening $1,490; protected reserve $500; deployable $990. It pays a $1,000 food invoice using $990 cash and approved $10 concession. Supplier receives $990 gross (subject to provider fees) and might pay carrier $900; carrier might pay mechanic $800. Zero fees/holds/reserves downstream gives closing cash Buyer $500, Supplier $90, Carrier $100, Mechanic $800: **$1,490 total conserved across the four merchants**. Cumulative distinct cash transfers = $2,690. Downstream face amount = $1,700. No funding or profit is invented.

## Exact settlement identity
For each invoice, at a snapshot boundary:
`original_open = recognized_cash_applied + authorized_concession + valid_noncash_setoff + remaining_open`.
Any refund or reversal requires a linked compensating entry and revisiting remaining balances. Payment processing fees affect payer debit or creditor receipt depending on fee contract; do not silently deduct them from the invoice's principal accounting.

## Why the liquidity literature matters
Fleischman et al. (2020) analyzed obligation clearing and mutual credit. BIS Working Paper 1318 (2025) analyzed incentive-compatible wholesale liquidity-saving auctions. These motivate **three separate computational objectives**: discover feasible settlement order, minimize cash requirement, and discover acceptable incentives. Their empirical percentages cannot be forecast to PayPal SMBs.

## Additional liquidity metrics
| Metric | Numerator / denominator or method | Caveat |
|---|---|---|
| Gross throughput ratio | sum executed cash payments / opening early cash committed | Counts reused cash; present with warning |
| Net incremental deadline value | achieved on-time face − baseline on-time face | Requires matched and defensible baseline |
| Minimum seed cash | minimum independently deployed existing usable capacity for target | Specify which merchant supplies it and whether hypothetical funding is allowed |
| Liquidity duration | sum over cash commitments of amount × days early | Higher can mean greater economic cost |
| Shortfall avoided | max baseline shortage − observed shortage over due horizon | Never treat simulation as observed |
| Benefit by merchant | buyer discount − cash opportunity cost; supplier avoided cost − concession − fees | Every participant may have different private utility |
| Realized versus projected | provider-reconciled invoices vs solver-proposed invoices | Separate all displays |

## Missing data must stay missing
If a merchant reports $990 incoming but availability is unverified, Elide may show a projected path but cannot mark it executable. If a $150 supplier reserve applies, $990 receipt cannot fund a $900 outflow because only $840 is deployable. This is not a rounding issue; it invalidates the route.
