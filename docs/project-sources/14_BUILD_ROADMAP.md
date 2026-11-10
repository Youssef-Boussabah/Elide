# 14 — Implementation Roadmap, Dependencies and Release Gates
**Planning reference: Nov 10, 2026. Official close: Nov 12, 2026 at noon Pacific / 3:00 p.m. Toronto.** This is a recommended sequence, not evidence work has been completed.

## Hackathon implementation dependency graph
`Sandbox credentials → one real invoice and hosted payment → provider GET reconciliation` is the main external integration path.
`Synthetic fixture → exact cash/offer model → independent verifier → API planner → approvals` is the finance path.
`Confirmed typed policy parsing → negotiable candidate proposal → deterministic verifier` is the AI path.
`Buyer interface → comparison/approvals → hosted payment → receipts/exceptions` is the UX path.
Merge paths only once the payment and domain contracts actually agree.

## Milestones
| Phase | Deliverables | Acceptance / evidence |
|---|---|---|
| P0 — Verify provider | Sandbox account/client/merchant, hosted invoice, receipt lookup | Real sandbox request/response and payer observation, no permission assumptions |
| P1 — Domain invariant | USD cents, invoice versions, reserve, discount, fees, missing capacity | Exact fixture and adversarial financial replay pass |
| P2 — Solver | two offers, one pay decision, downstream conditions, minimum-cash analysis | Independent exact verifier agrees on small networks; alternatives explained |
| P3 — AI | typed policy parser, single bounded negotiation turn, human confirmation | Held-out safety cases, one live changed policy changes plan |
| P4 — Reliable execution | consent, idempotent intent, outbox/webhooks, receipt reconciliation | One real sandbox payment, duplicate event yields one posting; hold blocks next leg |
| P5 — Full product surface | overview, compare, consent, execute, books, accessible fallback | Demo works from clean setup / hosted preview |
| P6 — Submit | recorded test results, repo license, setup, under-3-minute video, Devpost | Links public and working, no live secrets, release SHA recorded |

## Practical calendar (Toronto)
**Nov 10:** obtain provider access immediately; freeze scenario schema, implement reference finance model, create repo/CI and first UI shell. Prefer direct PayPal proof before polished design.
**Nov 11:** integrate policies, full route comparison, approvals, actual Sandbox action, inbox/outbox reconciliation, time-based test cases, record first backup video.
**Nov 12:** freeze stable features in the morning, run clean-clone test, final 3-minute video, upload repo/video and submit before **3 p.m. EST**, leaving contingency for submission failures. Verify authoritative Devpost times on the day.

## Parallel lanes
- Finance lane: constraints + independent verifier + benchmark.
- PayPal lane: API evidence + hosted payment + event reconciliation.
- AI lane: typed interpretation + safe negotiation + evaluation.
- Experience lane: compare/approve/run/export + narrative.
- QA/release: integration, docs, measurements, video and setup.

If solo, do work in dependency order, not lane order: *provider feasibility, finance core, simplest UI, AI, reconciliation, demo proof*.

## Quality thresholds
Never bypass reserve, consent, duplicate-payment, merchant isolation or provider-truth rules. Drop decorative UI or speculative ML before dropping correctness. If an optional feature fails, clearly label it unavailable and preserve coherent working core. If **no real PayPal Sandbox payment** can be demonstrated, this is a material judging vulnerability; do not misrepresent a simulation as actual integration.

## Long-term roadmap (not in two-day implementation)
- R1: connected multi-merchant pilot with permissioned invoice visibility, no hidden access assumptions.
- R2: formal minimum-seed liquidity solver, risk-aware capacity and settlement uncertainty, solver scaling.
- R3: real payment-timing forecasting after obtaining consented historical labels; evaluate vs simple baselines.
- R4: participation economics and possible value-sharing, multi-merchant product monetization, rigorous adoption tests.
- R5: legally reviewed noncash clearing research, privacy-enhancing computation and cross-currency extension only as separately justified efforts.

## Definition of done versus promise
Release docs contain exact implemented status, model/provider versions, tested endpoints, credentials/setup steps, replay seed, receipt evidence and known issues. Project knowledge files remain an intended specification until implementation artifacts prove completion.
