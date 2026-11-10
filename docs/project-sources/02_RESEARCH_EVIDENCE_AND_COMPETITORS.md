# 02 — Research, Problem Evidence, Competitive Landscape
**Research snapshot: November 10, 2026.** All figures are external facts or third-party claims, **not Elide results**. Details and original URLs in `17_SOURCE_REGISTER.md`.

## Why the underlying pain is credible
Intuit QuickBooks' 2026 Small Business Late Payments Report says 59% of surveyed small businesses had invoices at least 30 days overdue; 49% said processing times produced critical/moderate cash-flow gaps, and 42% reported external pressures delaying payments to suppliers or contractors. Nearly three in four were not fully automated in bill management. This supports the existence of receivables timing pain and operational friction, **not** the frequency of viable three-hop payment chains. Source: https://quickbooks.intuit.com/r/small-business-data/small-business-late-payments-report-2026/

## Academic foundations (different mechanisms, do not conflate)
1. **Fleischman, Dini & Littera (2020)**, *Liquidity-Saving through Obligation-Clearing and Mutual Credit*. Sardex network data suggested around 25% reduction of internal debt through obligation clearing alone and around 50% in combination with mutual credit. It is about **netting obligations and a complementary credit mechanism**, not simply paying a supplier early. Do not port these numbers into Elide's promised outcome. https://doi.org/10.3390/jrfm13120295
2. **Garratt, Bech, Nanut Petrič & Ates (BIS Working Paper No. 1318, Dec. 2025)** investigated auction-based liquidity-saving in wholesale RTGS systems. Simulation average auction success of 79%; liquidity savings versus conventional RTGS of roughly 2.6–12.2%. The lesson is preference elicitation and compensation for liquidity providers, not that SMB invoice payments achieve those results. https://www.bis.org/publ/work1318.htm
3. Classic payment-systems research distinguishes queue sequencing, multilateral offset, and deadlock resolution. A time-critical invoice network also needs receivable **availability**, not just transfer confirmation. The mathematical analogy is useful but differences in legal settlement, access, and participants are material.

## Existing products and precedents
| Category | Example | Existing strength | Elide must establish |
|---|---|---|---|
| Early-payment/dynamic discounting | C2FO, SAP Taulia, J.P. Morgan payables solutions | Buyers and suppliers negotiate early-payment economics | Incremental network-aware effects beyond individual discount offers |
| AP/AR automation | Traditional invoice and payments systems | Reminders, records, approvals and settlement operations | Why payers need permissioned downstream coordination |
| Clearing network | Cycles | Publicly describes privacy-preserving graph clearing/netting, and announced $8.7m raised in May 2026 | Differentiation in PayPal merchant workflow, explainable AI negotiation, explicitly verified cash settlement |
| Payment providers | PayPal, others | Established funds movement and payment acceptance | Elide is **application-layer decision intelligence**, not a new payment rail |

**Do not claim a completely untapped 'first ever' market.** Cycles' company-reported materials discuss business payment services and multilateral clearing. Its funding is company-reported and does not itself establish scale or merchant product-market fit. https://www.cycles.money/blog/cycles-raises-8-7-million

## Evidence versus hypothesis
**Supported:** Late invoice and processing delays burden SMBs; obligation-clearing/netting has serious literature; dynamic-discounting and adjacent products already exist; PayPal has relevant invoicing developer capabilities subject to permissions.

**Not proven:** Real-merchant graph visibility, reliable immediate downstream spendability, statistically significant additional on-time settlements, viable adoption economics, proposed cross-merchant SaaS willingness to pay, or predictive model performance. A three-node synthetic scenario demonstrates mechanism only.

## Commercial research plan
- Interview 10–15 buyer AP decision-makers and 10–15 suppliers across linked relationships, if accessible; question timing, reserve, cost of funds, penalty, consent and sharing scope.
- Ask each separately whether the buyer can rationally accelerate and whether the supplier prefers discount, installment, deferral, or no intervention.
- Historical replay: collect decision-time fields **as they were known then**, not look-ahead outcomes. Compare with actual schedule and private buyer optimization.
- Stratify by participation rate, degree of network connectivity, merchant size, delay distribution, processing fees and required savings.
- Capture privacy refusals, disputed invoices, incomplete links and failed offers. A solution that requires 100% invoice disclosure is not commercially credible.

## Honest impact statement
"Elide investigates whether permissioned downstream payment information can make existing early-payment decisions more valuable to participants, while maintaining consent and financial controls." Evidence must follow implementation; large gross flow numbers are **not** proof of causal benefit.
