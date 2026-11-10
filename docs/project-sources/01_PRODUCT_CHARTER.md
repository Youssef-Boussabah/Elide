# 01 — Product Charter and Commercial Thesis

## Core problem
Businesses manage invoices locally, but their ability to pay can depend on other businesses' receipts. A buyer may be able to make one early payment. A supplier may accept a small discount because early funds allow it to satisfy its own obligations. A carrier or contractor further downstream may have another urgent bill. Local optimization based on one merchant's books can miss cross-business effects.

These effects are limited by actual payment availability, consent, reserves, fees, disputes, due dates, and data visibility. They are **not guaranteed** merely because a graph edge exists.

## Target first customer and user
First buyer persona: an SMB owner, finance manager, or accounts-payable approver with enough surplus cash to bring forward *one* supplier invoice, but not enough to accelerate all invoices. Second persona: that supplier's authorized accounts-receivable approver, who can consent to a concession and optionally disclose a downstream payable. Third persona: another supplier participating in a limited, permissioned chain. A bookkeeper/auditor needs trustworthy posting and exports.

## Primary user job
"I have finite cash. Show me whether paying this invoice early is worthwhile to me, and whether that decision can help connected businesses pay their own bills without exposing everyone's finances or compromising payroll."

## Value proposition
- Buyer: earns a mutually agreed discount, maintains required cash reserve, and sees why one supplier-payment option may create additional value.
- Supplier: can receive funds earlier, with transparent concessions and without surrendering authority over its outgoing payments.
- Downstream supplier: may receive an otherwise delayed payment, but independently decides whether and when to send funds onward.
- PayPal: a plausible merchant workflow enhancement and potential source of genuinely incremental payment and SaaS engagement, **not yet a proven commercial product**.

## The core flow
1. Permissioned invoice/merchant records enter Elide (manual, imported, or appropriately authorized PayPal data).
2. Merchant sets protected cash reserves, spend limits, acceptance criteria, date rules and exclusions.
3. Elide constructs only the **consented, visible subgraph**, displays unknown edges explicitly.
4. The deterministic solver identifies feasible early-payment choices or minimum-seed-cash alternatives.
5. An LLM drafts human-readable proposals: "Could you accept $990 now for this $1,000 invoice?" It cannot independently change the legal obligation.
6. Every affected merchant reviews a version-bound proposal; refusal causes an alternate plan or no action.
7. Each payer separately completes an approved PayPal action. Confirmed receipts and verified availability unlock dependencies, not forecasts.
8. Bookkeeping records paid amount, concession, fees, remaining principal, and exceptions. Elide reports incremental and projected outcomes separately.

## Canonical example (synthetic, USD, ignoring fees)
Caterer has $1,490, of which $500 is a protected reserve. There are two independent $1,000 supplier invoices; each supplier offers to discharge its invoice for $990. The food supplier has an authorized $900 payable to a carrier; the carrier has an authorized $800 payable to a mechanic, both due soon. The packaging supplier has no currently disclosed downstream payable. The buyer can pay only one $990 invoice early and receives the same $10 discount either way. If downstream capacities remain available, choosing food permits a possible $990 + $900 + $800 = **$2,690 gross flow** and $1,700 face value in downstream payments. It *does not* create $1,700 in liquidity, and the true incremental on-time benefit could be zero if those bills would have been paid anyway.

## Differentiation
Elide does **not** claim invention of early-payment discounts, obligation clearing, graph optimization, or autonomous commerce. Its proposed differentiation is **buyer-and-network-aware selection, incentive-compatible AI-assisted negotiation, verified execution, and transparent incremental-impact accounting within PayPal-centric merchant workflows**. This differentiation is a testable thesis rather than established market superiority.

## Non-goals for the hackathon
No pooled wallet, lending or credit underwriting, unsecured advances, automatic transfers between unaffiliated merchants, legal debt cancellation, regulated payment custody, crypto settlement, purported real-time merchant balance aggregation, autonomous side payments or unverified ML financial forecasts.

## Success and stop conditions
Must have: one PayPal Sandbox payment with provider evidence; no unapproved spending; correct reserve enforcement; tested blocked/failed chain behavior; demonstrable AI interpretation that changes the verified plan; reproducible comparison with single-buyer or due-date baseline. Do not claim success from a polished animation alone.

## Product validation hypotheses
- H1: Linked invoice chains with actionable slack and permissioned visibility exist frequently enough for customer value.
- H2: The buyer's discount/flexibility compensates them for earlier cash deployment and associated risk.
- H3: Suppliers' avoided penalties or working-capital costs justify early-payment concessions after fees.
- H4: Merchant data permissions and PayPal integration are feasible at acceptable cost.
- H5: Network-aware decisions outperform realistic buyer-only baselines *on a meaningful portion* of observed cases.

Interview one anchor buyer and at least two connected suppliers. Collect redacted prior invoice histories and actual decisions; conduct a blinded historical replay. Record **refusals and non-participation** in the denominator. This is a discovery protocol, not research already completed.
