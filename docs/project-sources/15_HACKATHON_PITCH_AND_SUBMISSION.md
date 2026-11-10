# 15 — PayPal AI Hackathon: Winning Case, Submission and Demo

## Official requirements (source of truth: Devpost rules)
Deadline **Thursday November 12, 2026, 12:00 p.m. PST / 3:00 p.m. Toronto EST**. Need an application meaningfully using the PayPal developer platform **and** AI; functional demo (judges can actually run/try it); public repository with full code/assets/setup and visible open-source license; public YouTube video under 3 minutes; written project explanation; identified AI tools. Official rules: https://paypalaihackathon.devpost.com/rules . Confirm live site before submission.

The five criteria are **equally weighted** after viability screening: technological implementation, design, potential impact, innovation/idea, and presentation. Great architecture PDFs alone do not satisfy viability.

## Main pitch
"A supplier invoice doesn't live in isolation. It may hold up the money another business needs to pay its own bills. Elide finds the best eligible early-payment opportunity, helps counterparties agree on acceptable terms, verifies the cash and constraints, and executes approved payments through PayPal. It makes the financial reasoning transparent and quantifies additional on-time settlement instead of treating gross flow as money created."

## Why the entry could stand out
- Real research-driven SMB problem: overdue receivables and delays spreading from incoming to outgoing obligations.
- Nontrivial graph/time/cash optimization beyond selecting the cheapest invoice.
- AI does useful work interpreting merchant policies, drafting negotiated offers and explaining solver results.
- Payments are linked to a real PayPal Sandbox receipt and independent human approvals.
- Failure handling is explicit; demonstrate blocked held-funds route instead of fictional autonomous transfer.
- Credible business case and competitor acknowledgment show commercial maturity.

## Most likely objections / responses
**"Isn't this dynamic discounting?"** Early discounts are existing practice. Distinctive hypothesis: using permissioned downstream obligations and capacity constraints can improve outcomes beyond buyer-only choices; test with baselines.
**"You are just counting the same cash three times."** Correct: $2,690 gross flow reuses an initial $990. We separately show cash committed, downstream face, net incremental on-time effect and individual merchant benefit.
**"How can you see every business's balances?"** We cannot and do not claim that. Merchants consent to limited data disclosure/attestation, and we show network coverage and unknown edges.
**"What if funds are held?"** No further leg executes. Provider-confirmed receipt is distinct from spendability. Elide freezes, reconciles and replans.
**"Does PayPal let you orchestrate everyone's accounts?"** Selected-partner multiparty restrictions apply. Hackathon uses owned/permissioned sandbox accounts; never claim arbitrary partner access.
**"What is the AI actually doing?"** Bounded typed constraint interpretation and negotiated proposal generation, tested against valid and invalid policies; deterministic financial verifier remains authoritative.
**"Will PayPal earn more?"** Potential merchant retention/service fees and incremental activity, not proved. Some netting could cannibalize processing volume; pilot economics needed.

## Submission acceptance checklist
- [ ] Functional build runs from clean clone or deployed URL
- [ ] PayPal developer Sandbox API is central and a real payment is demonstrated
- [ ] AI operation is live/meaningful; input affects a verified plan
- [ ] Public git repository with LICENSE at root, source, assets, setup/env and reproducible synthetic demo
- [ ] YouTube demo publicly accessible, length <3:00, screen footage of working product, rights-safe media
- [ ] All depicted merchants and balances are labeled synthetic unless independently evidenced
- [ ] Project description includes problem, intended customer, how it works, real PayPal API used, AI model/tool used, benchmarks and limitations
- [ ] Provider receipts/release SHA and known gaps recorded in safe evidence manifest
- [ ] No leaked credentials or personal merchant information
- [ ] Links checked while signed out; submission recorded before official cutoff

## Demonstration decision
Do not waste time on advanced ML, actual netting, credit/loans, or fancy effects at the expense of the real payment. Sequence: business dilemma → clear two-choice comparison → AI counteroffer → independent verifier → consent → Sandbox receipt → safe block/recovery → measured takeaway.

## Measurement language
Safe: 'In this synthetic fixture, selecting the food supplier permits two additional projected downstream payments, conditional on consent and usable funds.'
Unsafe: 'Elide creates $1,700 of liquidity, earns PayPal $1,700, and guarantees the chain completes.'

## Probability of winning
No defensible numerical odds from current evidence. Elide may be competitive if judges observe the full functioning loop, sophisticated safety and genuine PayPal/AI depth; these are conditional strengths, not a promise of award.
