# ADR-0013 implementation plan (proposed, 2026-11-12)

- **Branch:** `feat/adr-0013-agreed-terms`, from `main` at `b027276`, after P5 was merged at
  `f7e7974` (CI #36).
- **Status:** **proposed, not started.** No product code has changed on this branch yet.

**Sources read**
- ADR-0013 (accepted by the owner);
- ADR-0008, ADR-0009, ADR-0011, ADR-0012;
- doc 05 (optimization), doc 12 (baselines and proof), doc 16 (D003, D005, D006, D008, D013).

**Current code that carries the defect**
- `api/planning/solver.py` (`_eval`, `_admissible`);
- `api/planning/verifier.py` (early-payment rule);
- `api/planning/network.py` (`ObligationEdge`, `PolicyParams`);
- `api/planning/builder.py`;
- `api/negotiation/counteroffer.py` and `api/opportunities/analysis.py`, which also apply
  `min_early_discount` to the **incremental** concession;
- `api/workflow/payments.py` and `api/execution/service.revalidate` (execution validity).

**Operating limits (unchanged)**
- No PayPal call of any kind.
- No model call.
- The pilot database is not touched.
- PayPal-mutating execution stays disabled.
- The mock provider only.
- The strict xfail `test_min_early_discount_should_count_the_already_agreed_concession`
  changes to a pass only when the corrected solver **and** verifier make it pass.

## Owner decisions requested before step 3

1. **Counteroffer and opportunity analysis.** ADR-0013 §2 states the rule for the planner.
   `counteroffer.py` and `opportunities/analysis.py` apply the same `min_early_discount`
   against the incremental concession.
   - **Recommendation:** apply ADR-0013 §2 there too:
     - total concession against the original terms for the merchant's early-payment rule;
     - incremental concession for the acceleration test and for reported benefit.

     Otherwise the negotiation layer would propose terms that the planner then judges
     differently.
   - The plan assumes this, recorded as an ADR-0013 addendum. Reply if you prefer to keep
     negotiation unchanged for now.
2. **Two review checkpoints instead of one.**
   - **Checkpoint 1:** after the semantics and deadlines (steps 0–6), before any new payment
     authority.
   - **Checkpoint 2:** after the payment instruction (step 7), before merge.
   - **Recommendation:** yes. Step 7 adds a second way, besides a plan leg, for a payment
     intent to exist, so it deserves its own review.

## Steps

### 0. Baseline and characterization (no product change)
- Re-run on `main`:
  - `scripts/run_planner_benchmarks.py` (B0–B5, fixed seeds);
  - the brute-force equivalence corpus (`api/planning/reference.brute_force`);
  - the 5 `test_p5_plan_blocker.py` cases.
- Store outputs under `evidence/adr0013/baseline_*`, labelled Synthetic-tested.
- **Test first:** add the failing tests that pin ADR-0013 §2 and §4 behaviour, beside the
  existing strict xfail. Record their failure output before any fix, as the P5 reviews did.

### 1. Original versus agreed terms in the planning model
- Add `original_due_at` and `original_concession` to `ObligationEdge`, built from revision 1
  in `builder.py`. They are already persisted, so no schema change is needed. The existing
  `due_at` and `in_force_concession` remain the agreed terms.
- Validation:
  - `original_concession` is 0;
  - the agreed due date is no later than the original;
  - the agreed concession is not negative.
- Unit and property tests for the builder and the edge invariants.

### 2. Discount semantics in the solver and, separately, the verifier
- **Policy rule:** if paying before `D0`, the total concession must be at least M.
- **Acceleration test:** if paying before `D_agreed`, incremental concession − time cost to
  `D_agreed` must be at least `min_net_benefit`.
- **Objective and reported benefit:** incremental only.
- The solver and the verifier implement these **independently**. The verifier still imports
  nothing from the solver (the existing boundary test keeps enforcing that).
- **Tests:**
  - the ADR's worked pilot example (11-14 allowed with gain 0; 11-12 refused);
  - unit cases at each boundary (equal to M, one cent below, the agreed date itself);
  - Hypothesis parity: on generated networks with negotiated revisions, the solver's chosen
    plan always passes the verifier, and every plan the verifier accepts is admissible to
    the solver's rules.
- `generator.py` gains a `negotiated_revisions` option, **off** for the canonical corpus, so
  the canonical results must stay byte-identical.

### 3. Negotiation and opportunity layer
Subject to owner decision 1:
- align `counteroffer.py` and `opportunities/analysis.py` with §2;
- add regression tests for the existing counteroffer schema and its human-confirmation flow,
  which stay unchanged.

### 4. Exact deadlines versus daily slots (§4)
- A pure function computes the payment window `[dispatch, deadline]`:
  - the deadline is `D_agreed` for an on-time leg, or the planned slot start for an
    accelerated leg.
- Evidence must satisfy `observed_at ≤ dispatch` and `valid_until > deadline`:
  - at intent creation (`api/workflow/payments.py`);
  - at every dispatch revalidation (`api/execution/service.revalidate`).
- Offer expiry stays exact.
- **Tests:** evidence lapsing inside the window, exactly at its end and after it; a
  revalidation that fails because evidence lapsed between creation and dispatch (the intent
  is cancelled before anything payable exists); the pilot's original 24 h case.

### 5. Benchmarks and equivalence
- Re-run brute-force equivalence on the seeded corpus, extended with negotiated revisions.
- Re-run B0–B5 with the same seeds.
- Canonical results must be identical to step 0, or every difference must be explained in
  the evidence file, including zero and negative cases (D013).

### 6. Resolve the strict xfail
Remove the `xfail` marker only after the test passes without it. Record the before and after
output.

**→ Checkpoint 1: owner review of steps 0–6.**

### 7. Merchant-directed payment instruction (§5, §6)
- **Migration 0009:**
  - append-only `payment_instruction` table, with guards;
  - `payment_intent.basis` (`plan_leg` | `instruction`) and `instruction_id`;
  - one open instruction per obligation.
- **An independent `instruction_check`**, outside the planner and importing nothing from the
  solver or verifier, checks all seven §6 conditions at creation and at every dispatch:
  - terms and content hash;
  - consent and conditions digest, with the user's authority share-locked;
  - original-terms policy;
  - own verified cash only, with reserve and fee ceiling (D006/D008);
  - an evidence window through `window_end`;
  - bindings, Sandbox only, kill switch;
  - no upstream dependency.
- **API:**
  - preview: the economics disclosure and its digest, with the optimizer's current
    recommendation shown verbatim beside it;
  - confirm: payer only, `payment:authorize`, step-up, Idempotency-Key, and the disclosure
    digest must match;
  - cancel before dispatch.
- **Execution reuses the P5 path unchanged:** intent, reservation, outbox, revalidation,
  worker, reconciliation, checker v2 and the settlement identity.
- **UI:** the instruction is shown separately from recommendations and labelled "your
  decision", never "recommended".
- **Tests:**
  - each §6 condition refused;
  - changes between creation and dispatch: reserve, policy, revocation, revision, evidence
    lapse;
  - payee isolation and other-tenant 404;
  - concurrency (two instructions; instruction against plan leg);
  - an instruction-backed booking passing checker v2;
  - jsdom tests and a Playwright flow against the mock provider.

### 8. Close
- ADR-0013 implementation addendum (what changed, deviations, evidence);
- tracker update;
- full checks: ruff, `alembic check`, pytest, frontend, Playwright, secret scan, CI.

**→ Checkpoint 2: owner review. No merge without approval.**

## Invariants that must not weaken
- D003, D004, D005: the verifier always wins.
- D006, D008.
- Reserve after every leg including fees.
- Consent is bound to the exact terms and digest.
- No cross-tenant disclosure.
- Append-only journal and settlement identity.
- Checker v2 as the booking guard.
- No model access to payment paths.

## Risks
- **Benchmark drift.** Mitigated by keeping negotiated revisions off for the canonical corpus
  and comparing byte for byte.
- **Two semantics active during migration.** Avoided by doing step 2 as one change in solver
  and verifier together, with parity tests.
- **The instruction path widens payment authority.** Mitigated by an independent checker,
  step-up, digest-bound disclosure, the unchanged P5 execution path, and a separate review
  checkpoint.
