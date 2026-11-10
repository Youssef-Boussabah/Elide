# ELIDE — Project Knowledge Base
**Authoritative project-source set | v1.0 | 2026-11-10**

## Purpose
This folder is the durable technical and commercial brain of **Elide**, an AI-assisted, permissioned coordination system for business invoice payments and liquidity bottlenecks, developed for the **PayPal AI Hackathon**. Treat this set as *design specifications*, not evidence of implemented capabilities. Anything described as proposed, simulated, planned, or a hypothesis must remain labeled that way.

## One-sentence definition
**Elide identifies when an authorized change to a business payment (timing, discount, or terms) could make other obligations payable, helps counterparties negotiate acceptable terms, verifies financial feasibility, and uses PayPal to execute and reconcile independently approved payments.**

## The invariant idea
One merchant's delayed receivable can block several other merchants' outgoing payments. Elide looks beyond one invoice, but never assumes that a network-level benefit justifies imposing losses on an individual participant. It does not create money, guarantee immediately spendable receipts, provide credit, net debt through PayPal, or pay anyone without their authorization.

## Authority hierarchy
1. **Actual code and verified PayPal API observations** outrank any design plan for what is implemented. Record divergences; do not secretly rewrite the plan.
2. **This source set** is the authority for intended product behavior. `16_DECISIONS_STATUS_AND_OPEN_QUESTIONS.md` resolves conflicts.
3. Dated official PayPal/hackathon documentation outranks uncited interpretations of provider capabilities or rules.
4. Original Astra archive and older exploratory brainstorms are *historical*; do not restore superseded assumptions without a design decision.
5. External research supports *motivation* and *method*, not Elide performance claims.

## File map (upload each individual Markdown file to ChatGPT Project Sources)
| File | The question it answers |
|---|---|
| `01_PRODUCT_CHARTER.md` | What is the product, who participates, why would they care? |
| `02_RESEARCH_EVIDENCE_AND_COMPETITORS.md` | Is the problem real? What already exists? |
| `03_PAYPAL_STRATEGY_AND_FEASIBILITY.md` | Why PayPal; which features are technically accessible? |
| `04_LIQUIDITY_RESEARCH_AND_ACCOUNTING.md` | What is liquidity, what gets unlocked, what is merely double-counted? |
| `05_OPTIMIZATION_AND_ALGORITHMS.md` | What exactly does the financial planner solve? |
| `06_MECHANISM_DESIGN_AND_UNIT_ECONOMICS.md` | Why do parties participate and who pays? |
| `07_AI_ML_INTELLIGENCE.md` | What do the LLM and ML actually do; what must they never do? |
| `08_SYSTEM_ARCHITECTURE.md` | Services, control boundaries, data flow, state transitions |
| `09_DATA_MODELS_AND_API_CONTRACTS.md` | Types, storage, API shapes, invariants |
| `10_PAYPAL_INTEGRATION_RUNBOOK.md` | Sandbox implementation, provider reconciliation, permissions |
| `11_SECURITY_PRIVACY_AND_RISK.md` | Confidentiality, consent, abuse prevention, failures |
| `12_TESTING_BENCHMARKS_AND_PROOF.md` | How to prove technical and financial claims |
| `13_UX_JOURNEYS_AND_DEMO.md` | Screens, interaction, example walkthrough, demo narrative |
| `14_BUILD_ROADMAP.md` | Dependencies, release gates, sequencing |
| `15_HACKATHON_PITCH_AND_SUBMISSION.md` | Judging strategy, deliverables and showstoppers |
| `16_DECISIONS_STATUS_AND_OPEN_QUESTIONS.md` | Frozen choices, explicitly unresolved facts, non-goals |
| `17_SOURCE_REGISTER.md` | Source URLs, provenance, limitations |

## Definition of a working release
A merchant can enter or import a real or labeled synthetic obligation, configure a liquidity reserve, receive two competing payment arrangements, review an AI-suggested but human-approved change, see a deterministic explanation of feasibility, complete **at least one actual sandbox payment** through PayPal, and view a correctly reconciled provider receipt. When downstream receipts or capacities are simulated, the UI labels them so.

## Two distinct product horizons
- **Hackathon release**: small permissioned network fixture, valid payment plan, bounded AI policy/negotiation, human approvals, one concrete PayPal sandbox flow, replayable evidence.
- **Long-term research/product**: multiple independent merchant integrations, minimum-seed-liquidity search, verified timing forecasts, consent-preserving optimization at scale, optional legally reviewed multilateral set-off, economic pilots, robust causal impact evaluation.

## Prohibited accidental claims
Never describe gross payment throughput as newly created money, payments 'unlocked' as confirmed without provider evidence, invoice-paid events as proof of spendability, hypothetical probabilities as empirical ML accuracy, or set-off as a PayPal-native debt extinguishment feature.

## Maintenance
When implementing, create an explicit decision record, update the corresponding source file and the status matrix, and attach test evidence to the code repository. Preserve past design in Git history; do not upload competing versions as separate authoritative Project Sources.
