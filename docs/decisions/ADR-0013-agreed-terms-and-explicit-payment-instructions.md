# ADR-0013 — Agreed early-payment terms and explicit merchant payment instructions

- **Status:** **Proposed** (2026-11-12). Not implemented; needs owner approval. The pilot does
  not depend on it (see the blocker report, R1).
- **Context:** the P5 pilot produced an empty plan after the buyer accepted a USD 1.00
  early-payment offer (`evidence/phase5/pilot_plan_blocker_2026-11-12.md`). The decisive
  cause was capacity evidence shorter than the payment window. The investigation also
  exposed a semantic defect in the P2 early-payment rule. It also exposed a product gap: a
  merchant cannot explicitly choose to pay an agreed obligation now when the optimizer, rightly,
  does not recommend it.

## Distinctions the model must keep

| Concept | Where it lives today | Use |
|---|---|---|
| Original due date and terms (revision 1) | `obligation_revision` 1 | the economic baseline (B0) and the policy's notion of "early" |
| Agreed pay-by date and total concession | the AUTHORIZED revision in force | what the payee will accept |
| Planned slot | the plan leg | the optimizer's recommendation |
| Actual payment time | chosen by the payer at PayPal, between send and pay-by | where cash really leaves; the reservation spans it |
| Incremental concession | option concession − in-force concession | the objective and time-cost test (never re-counts agreed terms) |
| Merchant instruction vs recommendation | — (missing) | a voluntary decision must never be presented as the optimizer's plan |

## Decision (proposed)

### 1. Early-discount rule against the original terms (solver and verifier, coded separately)

- A leg is *early for the policy* when its slot is before the **original** due slot.
- Then the payer's `min_early_discount` must be met by the option's **total** concession
  relative to the original terms. Agreed terms count, because they are what the merchant
  negotiated.
- The time-cost and net-benefit test (`extra − cost ≥ min_net_benefit`) and the objective
  stay **incremental** against the in-force terms, so an agreed concession is never counted
  twice and accelerating beyond an agreed date still has to pay for itself.
- `ObligationEdge` gains `original_due_at` (and the original concession, always 0) from
  revision 1.
- The P2 equivalence (solver = brute force) and the B0–B5 benchmarks must be re-run. The
  canonical seeds have no negotiated revisions, so their results should not change; that
  must be shown, not assumed.

### 2. Explicit payment instruction (new decision type, distinct from plans)

- **Object.** `payment_instruction`: payer merchant, obligation, in-force revision, amount
  (= remaining open), payee, fee ceiling, execution window `[now, pay-by]`, conditions digest,
  economics disclosure, user, session, expiry (24 h). Created only by the payer, with
  `payment:authorize` and step-up; append-only.
- **Independent check** (a separate function, not the optimizer), at creation and at every
  dispatch revalidation:
  - terms AUTHORIZED by both parties; not disputed; USD; amount equals open;
  - payee not prohibited; fee within the payer's cap;
  - **own verified cash only** (no upstream dependency);
  - current evidence **valid through the whole execution window**;
  - after amount plus fee ceiling, `deployable ≥ 0` with the reserve at
    max(evidence, policy floor);
  - the policy's discount rule (§1) against the original terms;
  - provider bindings as in P5.
- **Disclosure.** Economics against the original terms (concession earned, days earlier) and
  against the agreed date (incremental value and time cost at the merchant's stated hurdle),
  labelled `MERCHANT_INSTRUCTION`. The optimizer's own recommendation (possibly NO_ACTION) is
  shown alongside, unchanged.
- **Execution.** The same P5 payment intent and reservation, the same revalidation, worker,
  reconciliation and booking. `verify_leg_now` is replaced by the instruction check for
  instruction-backed intents.
- **Not allowed.** No instruction for a leg that depends on incoming funds. No instruction
  that would breach the reserve or policy. No instruction from anyone but the payer. An
  instruction never alters terms.

## Alternatives considered

- **Change `extra` to the total concession everywhere:** rejected. It double counts agreed
  terms in the objective and lets the optimizer "gain" a concession it already has.
- **Force the leg into the plan or skip the verifier:** rejected (D005).
- **Treat a 24 h attestation as valid for a multi-day window:** rejected. The reservation
  spans the whole window.
- **Do nothing:** acceptable for the pilot (R1). The product still cannot express "I choose to
  pay now", so the gap remains.

## Finance and privacy impact

No new data is disclosed to counterparties. Voluntary acceleration is labelled with its cost.
Reserves, policy floors and D006/D008 are unchanged.

## Tests (planned)

- Planner and verifier parity for §1.
- Brute-force equivalence and the benchmark re-run.
- For instructions: creation, refusal on every failed condition, evidence shorter than the
  window, reserve and policy changes between approval and dispatch, payee isolation,
  labelling in API and UI, and a Playwright flow against the mock provider.
- Starting point: the strict xfail in `tests/integration/test_p5_plan_blocker.py` must turn
  into a pass.
