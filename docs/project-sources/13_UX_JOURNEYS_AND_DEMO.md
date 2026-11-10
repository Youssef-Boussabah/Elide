# 13 — User Experience, Storyboard and Demonstration

## Design objective
A financial product should feel clear and trustworthy, not like an autonomous chatbot. Merchants should always know **what is simulated, what they approved, what PayPal verified, and what money is usable**. Avoid glowing money lines that imply confirmed settlement when only a graph prediction exists.

## Primary screens
1. **Today / Liquidity overview:** observed spendable cash (source and timestamp), protected reserves, committed cash, deployable amount, known invoice due dates and data coverage.
2. **Opportunities:** comparison of buyer-only result versus network-aware result; each card shows buyer net benefit, cash/timing cost, downstream potential, uncertainty and binding constraints.
3. **Graph Explorer:** nodes as merchants; directional edges as invoices with due date, allowed visibility, status (proposed/approved/provider-confirmed/usable). Accessible table equivalent is mandatory.
4. **AI Policy and Terms Review:** original merchant sentence, highlighted evidence, typed policy revision and approve/edit controls. Unknown date/currency triggers clarification.
5. **Negotiation Room:** proposed discount/date, why it helps, each merchant's private economics, no exposure of hidden supplier thresholds. Accept/decline independently.
6. **Execution:** explicit payer confirmation, hosted PayPal action, plan state, receipt fetched from provider, blocked downstream leg reason, replay trace.
7. **Books / Evidence:** journal by invoice, cash applied, concession, provider fees, remaining amount, receipts, exceptions and export.

## Canonical demo fixture
Caterer has $1,490 cash, must keep $500 protected. Packaging invoice and food invoice each $1,000 with $990 early settlement offer. Food supplier has authorized $900 carrier due soon and carrier has $800 mechanic payable. Candidate A (food) and B (packaging) have same buyer discount. In zero-fee, known-availability synthetic case A potentially permits $1,700 downstream face while B does not. The UI must say **projected** until receipts and capacity evidence confirm it.

## Stronger AI demo fixture
Supplier originally offers only $5 discount; buyer minimum $10; initial arrangement fails private buyer constraint. AI drafts a $10 counteroffer (not acceptance). Supplier may approve, solver re-verifies buyer reserve, supplier proceeds after fees, and dependent payment feasibility. Demonstrate a refusal that yields a blocked route or a smaller feasible alternative. This shows AI creating a new viable *offer*, not creating money.

## Compelling fault demonstration
After first sandbox payment, inject a held-funds capacity attestation or refuse the second leg. Show prior PayPal-confirmed first transfer persists, rest of plan halts, journal remains balanced, and new plan is requested. A financial-grade failure is more impressive than an impossible seamless cascade.

## Suggested video structure (<3:00)
- 0:00–0:20: business problem, three linked invoices.
- 0:20–0:45: choose between equal private offers; network-aware difference and math.
- 0:45–1:15: user states reserve/constraint; AI extracts it and human confirms.
- 1:15–1:45: AI proposes acceptable change, merchant approval and deterministic verifier result.
- 1:45–2:20: actual hosted PayPal Sandbox payment; provider-confirmed receipt and reconciliation.
- 2:20–2:40: blocked downstream event or duplicate webhook safety; label simulated inputs.
- 2:40–2:55: measured baseline result, value to merchant and PayPal, precise limitations.

## UX rules
Give amounts/currency/fees/timestamps on every payment. Avoid conflating labels like `ready`, `paid`, and `spendable`. Never show private unrelated invoices to other merchants. Design for 1280px desktop, contrast/keyboard accessibility, loading/error states and action confirmation. Synthetic merchant aliases only in public materials.
