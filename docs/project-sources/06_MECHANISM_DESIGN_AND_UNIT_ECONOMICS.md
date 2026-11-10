# 06 — Incentives, Liquidity Pricing and Merchant Economics

## Essential principle
A network-optimal transfer that makes one participant worse off is not a sustainable product. Elide should propose **individually acceptable terms**, not treat buyer cash as a public resource. Network benefit is an explanatory quantity; explicit merchant consent and minimum personal return are binding.

## Buyer economics
Buyer advantage from paying an invoice early includes approved discount/price benefit plus any independently valued supplier-service benefit, minus earlier deployment of cash, financing/opportunity cost, payable processing fees, liquidity buffer risk and operational costs. For a simple cash-cost approximation:
`early_cash_cost ≈ cash_paid × annual_cash_hurdle × days_advanced / 365`.
Buyer requires `net_private_benefit ≥ privately selected hurdle` and reserve protected at every prefix. In many cases the rational answer is **do not pay early**.

## Supplier economics
Supplier receives earlier net available cash but forgoes discount and may pay fees. Its gain may include avoided late fees, lower financing cost, avoided stockout, improved supplier terms, reduced collection uncertainty, and operating cash-flow resilience. These are **merchant inputs or researched estimates**, not facts Elide can infer from an invoice alone. Supplier approves only if expected private value meets threshold.

## Annualized discount comparison
For a $1,000 nominal payable settled for $980 20 days early, the buyer saves $20. Simple annualized rate on $980 advanced is `($20 / $980) × (365 / 20) ≈ 37.2%`. This is a **price of accelerated cash** measure, not an interest contract or guaranteed ROI. Compare with alternatives and actual contractual timing. Present the supplier's view explicitly.

## AI negotiation procedure
1. Each merchant sets hidden/private threshold fields: minimum net receipt, maximum discount, protected cash, deadline preferences, prohibited counterparties and fees ceiling.
2. Solver computes current arrangement and reason it fails.
3. AI suggests one small change—discount, payment date or permissible installment—using only disclosed ranges. It must not reveal private thresholds to other merchants.
4. Deterministic verifier checks new proposal; both sides receive exact disclosed terms and privately calculated impact.
5. If everyone consents, plan version advances. If not, Elide searches an allowed alternative or reports no feasible deal.
6. Never use LLM-generated 'acceptance' as a signature or bank mandate.

## Fairness and allocation
If a coordination benefit accrues downstream but the initiating buyer bears costs, evaluate **voluntary** benefit-sharing as a future mechanism, with legal and fee review. Do not create unapproved side payments. Show participants their own benefit, avoid exposing other merchants' dollar cost structures, and prevent asymmetric pressure or coercive defaults.

## Incentive mechanism research
BIS Working Paper 1318 considered eliciting participants' willingness to provide liquidity, with side payments supporting incentive alignment in wholesale systems. It motivates the problem formulation, not a ready-made SMB auction. The hackathon product should use simple posted offers + owner negotiation; more sophisticated mechanism design is a research track requiring strategic-manipulation tests and regulatory review.

## PayPal economics
Potential business model choices: subscription for planning, tiered financial analytics, merchant-led coordination workflow, optional performance-based price **only if attribution and legality can be established**. Model contribution: `SaaS recurring net + incremental payment contribution − service costs − compliance/privacy costs − cannibalized processing margin`. Gross face paid on time is not provider profit; amount settled early may only be shifted in time and might have moved through PayPal anyway. Explicitly test whether set-off removes payment volume that PayPal would otherwise process.

## Unit economics pilot ledger
Per participating network/month record: active merchants, consent rate, invoices visible, eligible opportunities, accepted offers, paid payments, genuinely incremental volume, avoided penalties (if substantiated), total discounts, fees, merchant dollar-days committed, setup/support hours, retention and willingness to pay. Analyze results by merchant, not just aggregate throughput.

## Anti-gaming considerations
A supplier could overstate liquidity need or invent downstream obligations. Require invoice provenance, confirmation and participant scope; no credit from unauthenticated graph edges. A buyer could understate capacity; treat merchant's capacity as binding but do not promise optimum network welfare. To avoid leakage, only expose approved aggregation and coarse conditions. Synthetic simulations can stress false signals; product must not promise fraud detection without data and testing.
