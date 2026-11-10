# 16 — Decision Register, Status, Open Questions and Boundaries
**Decision register snapshot: 2026-11-10.** This is the tie-breaker when project documents conflict.

## Frozen direction (unless consciously amended)
| ID | Decision | Reason |
|---|---|---|
| D001 | Product name **Elide** | One professional identity across UI, pitch and code |
| D002 | Focus on permissioned early-payment liquidity coordination | Clear merchant workflow and plausible PayPal integration |
| D003 | Network benefit cannot override private merchant acceptance | Economic sustainability and consent |
| D004 | No autonomous payment authority for LLM | Payment safety and legal agency |
| D005 | Deterministic verifier before any offer execution | Formal financial control |
| D006 | Payment-confirmed ≠ funds-usable | Avoid impossible cascades |
| D007 | Modular monolith React/TypeScript + FastAPI + PostgreSQL | Practical, testable initial architecture |
| D008 | One currency/USD cents for first demonstration | Prevent complex FX and rounding issues |
| D009 | Active flow is real PayPal Sandbox + separate simulated downstream where necessary | Honest provider capability boundary |
| D010 | Minimum-seed optimization is central research direction, not a claim of live lender funding | Preserves core ambition safely |
| D011 | Noncash obligation clearing is independent research, not ordinary PayPal transfers | Legal/accounting differences |
| D012 | Real ML is postponed until real labeled data or explicitly research-grade synthetic evaluation | No fabricated predictive power |
| D013 | Evidence must compare matched baselines and report negative/zero cases | Avoid misleading liquidity impact claims |

## Status categories
- **Specified:** concept/contract appears in Project Sources. This is the default status for the content of these files.
- **Reference-tested:** independent toy Python cases from Astra's original archive were previously described; do not treat these as the full optimizer.
- **Sandbox-verified:** only after someone records an actual provider request/receipt, exact endpoint status and release commit.
- **Pilot-validated:** only after real consenting merchants and a documented comparison methodology.

At creation of this source set, do **not** assume any new application, ML model, partner integration or production optimizer has been built. The only attached historical package contained design markdown and `reference/verify.py`.

## Known open questions
1. Can the team's actual PayPal Sandbox business account create the exact modified invoice/discount workflow and complete a payer-hosted checkout?
2. Do all relevant developer scopes exist for the participating merchant accounts, and is selected-partner access needed?
3. Can we verify spendable funds before issuing a downstream payment, or must that part remain clearly simulated/attested?
4. Which source of historical invoices can merchants provide lawfully and reliably?
5. What % of connected invoices and counterparties will opt into limited disclosures?
6. Will buyer/supplier concessions exceed financing, fee, liquidity and onboarding costs for each participant?
7. What baseline schedule is fair, especially when bills would be paid on time anyway?
8. Is a full time-slot exact solver necessary for the hackathon, or is a smaller deterministic enumerator adequate?
9. Which AI provider/model is available, and can the typed policy parser meet zero unsafe relaxation on golden cases?
10. If long-term clearing is considered, what is the exact legal/accounting jurisdiction and contracting mechanism?

## Features explicitly not assumed to exist
Instant global PayPal liquidity feed; automatic merchant cross-account invoice reading; deposits guaranteed immediately usable; multilateral debt extinguishment; unapproved side payments; proprietary credit line; trained prediction model on real linked merchant data; statistically proven ROI; legal compliance opinions.

## Change control procedure
Every change should record ID, date, proposer, alternatives, affected files, finance/privacy impacts, changed test assumptions, and explicit rationale. For hackathon, prioritize consistent end-to-end functionality over feature count. Keep an evidence table mapping implemented feature → code release SHA → tests → provider receipt or screenshot → claim allowed.

## Historical materials note
Earlier brainstorming and Astra's original baseline are valuable for explaining design history, but they contain narrower objectives and unimplemented capabilities. These **must not** outrank this frozen set. If a future user asks what was dropped, explain the reason and how it appears in the post-hackathon research backlog.
