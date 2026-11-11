# ADR-0009: Phase 2 planner, verifier and baseline design

- **Date:** 2026-11-11, amended 2026-11-11 (hardening) · **Status:** Proposed (pending owner review of P2; no frozen decision changed)
- **Context:** Phase 2 implements the deterministic planner, a separately coded verifier
  and B0–B5 baselines (execution plan Phase 2; docs 04, 05, 06, 09, 12, 16). The source
  documents leave the time model, the exact objective, the baseline rules and the solver
  method open. Evidence class of everything here: **Synthetic-tested**. No PayPal call,
  no invoice change and no merchant approval is made by any P2 code.
- **Affected files:** `api/planning/*`, `fixtures/planning.py`,
  `scripts/run_planner_benchmarks.py`, `tests/unit/planning/*`,
  `tests/property/test_planner_props.py`, `tests/integration/test_planner_reservations_db.py`,
  `api/domain/invoice.py` (read-only `open_offers`), `api/accounting/tables.py` (index
  declaration only), `.github/workflows/ci.yml` (`alembic check`).

## Decisions

1. **Network model.** Merchants are nodes; each invoice is an immutable edge keyed by its
   obligation id (parallel invoices stay separate; a duplicate id or provider reference is
   rejected). An edge carries face, cash already applied, in-force concession, revision,
   due instant and dispute status. Each edge has candidate *legs*, one per term set:
   `IN_FORCE` (authorized terms), `OPEN_OFFER` (a DRAFT signed by the creditor; the debtor
   must accept before the deadline and pay in the same slot) and `NEGOTIATED_PROPOSAL`
   (a DRAFT not signed by both; B5 only). A leg must discharge the invoice exactly
   (`cash + concession + applied = face`); partial payments are out of P2 scope (doc 05).
2. **Consent is data, never inferred.** Every leg lists the approvals it needs (payer
   `authorize_payment`; `accept_offer` for offers; both parties' `sign_terms` for
   proposals) with a basis: `synthetic_assumption`, `recorded_decision` (needs an evidence
   reference; none exist in P2) or `refused`. Only the payer and payee may appear; a third
   merchant's authorization is rejected (D003). Plans list approvals as *required*; P2
   never creates one.
3. **Time model.** A `SlotClock` of explicit slot starts (canonical: daily, 31 slots from
   `AS_OF + 30 min`). A payment in slot t executes at the slot start; it is on time iff
   the slot starts no later than the due instant. Proceeds are usable from `t + lag`,
   `lag >= 1` per merchant (`AvailabilityModel`); `synthetic_zero_lag` is lag 1. Lag 0 is
   unrepresentable, so a receipt can never fund a payment in its own slot. Already-known
   receipts become `ScheduledInflow`s only with availability evidence, at the first slot
   after `usable_from`; held, provider-confirmed and unverified funds count for zero.
4. **Evidence validity bounds a merchant's payments.** A merchant cannot pay in a slot that
   starts at or after its capacity evidence's `valid_until` (re-verify, then re-plan).
   With the 4-hour canonical evidence the buyer can act only in slot 0. Synthetic
   downstream positions have no expiry and are labelled as assumed current.
5. **Fees.** `payer_fee` is added to the payer's debit; `payee_fee` is deducted from the
   payee's proceeds; `CONDITIONAL` amounts are worst-case ceilings; `UNKNOWN` fees make a
   leg unplannable. The USD 29.01 Sandbox fee appears only as a labelled example
   (`paypal_sandbox_observed_example`), never as a fee model. The reserve must hold after
   every debit including fees.
6. **Merchant acceptance is a hard constraint, per leg.** Paying *before the due slot* is
   accepted by the payer only if the extra concession (vs in-force terms) is at least its
   `min_early_discount` and `extra − time cost >= min_net_benefit`, where time cost =
   `ceil(debit × hurdle_bps × seconds early / (10,000 × 365 days))`. Payees may cap the
   concession they give; either side may set a fee ceiling or prohibit a counterparty;
   non-participants cannot pay (or sign offers). Per-leg rather than aggregate acceptance
   is stricter: a bad leg is never subsidised by a good one. The network objective can
   never override these rules (D003, doc 06).
7. **Objective (lexicographic, larger is better).**
   `(lead0, lead1, on_time_face, surplus, −early_cash, −cent_seconds, −fees)`, then the
   lexicographically smallest schedule. `NETWORK`: lead = (0, 0). `FOCAL`: lead = the
   focal merchant's own net benefit (doc 05 item 4). `MIN_LIQUIDITY`: lead =
   (−hypothetical funding, −existing own-funds draw). `on_time_face` maximises the increment
   over any fixed baseline on the same network. `surplus` = stated early-receipt value −
   time cost (concessions cancel: they are transfers). Hard constraints are never scored.
8. **Solver: exact dynamic programming over slots.** State =
   (slot, obligations paid, per-merchant plan cash flow, receipts in transit; plus peak
   draws for minimum liquidity). Within a slot payment order cannot matter (receipts are
   usable only from the next slot; debits only lower cash), so the search enumerates
   subsets of affordable legs per slot. The objective is additive per leg, so memoising
   the best completion per state is exact. OR-Tools/CP-SAT was not needed: the 10-leg,
   5-slot canonical network and all 320 benchmark networks solve completely
   (`OPTIMAL`). `max_nodes` (default 1,000,000) bounds the search; when reached the solver
   returns `FEASIBLE` with a greedy incumbent (which the plan builder verifies) or `UNKNOWN`,
   never `OPTIMAL`. `INFEASIBLE` only after a complete search.
9. **Independent verifier.** `verifier.verify` replays a schedule from network data only,
   with its own ledger and formulas (it does not import the solver). It rejects unknown,
   duplicate or double-settling legs, out-of-window or stale-evidence slots, proposals
   without B5, non-USD, disputes, unknown or over-ceiling fees, refused/foreign/missing
   consent, payer/payee acceptance failures, reserve or commitment breaches after any
   debit (incl. same-slot reuse and wrong order), conservation errors and unmet required
   obligations, and recomputes every metric. The brute-force reference ranks every
   assignment the verifier accepts; tests require the solver to match it exactly.
10. **Minimum liquidity (inverse mode).** For a target set paid on time, minimise
    hypothetical external funding first, then the peak draw on *funding* merchants' own
    existing cash. Non-funding merchants may only re-spend what the plan pays them.
    Hypothetical funding is reported separately, labelled as non-existent, and verified by
    replaying with it as a slot-0 injection (one cent less fails). Noncash set-off stays
    out of the payment model (D011): the $100 A→B→C→A loop needs a $100 hypothetical
    seed here.
11. **Baselines (doc 12) on one identical network.** B0: pay on in-force terms from the due
    slot. B1: every merchant pays as soon as it can, earliest due first, cheapest
    executable terms. B2: the focal buyer maximises (own net benefit, own on-time face)
    using its own cash; ties to the smallest schedule; others follow B1. B3: B2 with each
    tied buyer choice equally likely; exact expectation (`Fraction`). B4: the exact plan.
    B5: B4 with proposals assumed approved by both parties: a different feasible set,
    stated in its notes. Every baseline schedule is verifier-checked; B4 ≥ B0–B3 on on-time
    face is a tested property, not an assumption.
12. **Plan record `elide.plan.v1`.** Deterministic JSON (no wall-clock time):
    - identity: plan id from the record digest, input digest (network digest + config + solver version)
    - solver and verifier status
    - legs: selected legs (terms, slot, amounts, fees, usable slot, on-time/accelerated, dependencies, `proposed` vs `conditional`) and rejected legs with codes and explanations
    - cash: required initial capacity (peak own-funds draw per merchant with its evidence id), merchant benefits, gross flow, downstream face, on-time face, liquidity-dollar-days
    - comparison: B0–B5 and the increments over each, alternatives (network, focal merchant, no action) with a Pareto flag
    - constraints and approvals: binding constraints (reserve, offer expiry, deadline), infeasibility reasons with minimum-liquidity sizing, required approvals
    - status: expiry (earliest offer deadline or evidence expiry), warnings, `status: SIMULATED` and `execution.may_execute: false`
    
    `merchant_view` gives a merchant only its own legs, approvals, benefit and reservation (other merchants' cash, reserves, invoices and benefits are not included; upstream dependencies are a flag, not an id). `verify_plan_record` re-checks a stored record (digest, legs, amounts, dependencies, declared capacity).
13. **Concurrent plans.** A plan's cash reservations are P1 `Commitment`s
    (`plan_commitments`); reserving them goes through the P1 store's locked
    `reserve_capacity`, so two plans cannot use the same verified balance (PostgreSQL
    test), and re-planning sees the commitment.
14. **Canonical fixtures (`canonical-p2-v1`).** Built from P1 fixtures. Supplier discounts
    are open offers (conditional on acceptance before a 60-minute deadline), not the
    unconditional authorized amendment of P1 scenario A, because an unconditional
    discount gives the buyer no reason to pay early (the planner correctly refuses).

## Alternatives considered

- **Permutation enumeration of legs** (doc 05 "enumerate orderings"): factorial and blind to
  slot timing; the slot DP covers ordering *and* timing exactly with far fewer states.
- **OR-Tools CP-SAT / MILP:** not justified at ≤ 10 legs; adds a dependency and makes
  optimality proofs less transparent. Remains the scale-up path (doc 05).
- **Weighted single score:** rejected; doc 05 forbids turning hard constraints into
  penalties and requires transparent tiers.
- **Aggregate (per-merchant) acceptance:** rejected for P2; it would let one unattractive
  early payment be subsidised by another.

## Finance / privacy impact

- No money moves; plans are simulations with explicit assumptions and expiry.
- Hypothetical funding and synthetic approvals are labelled everywhere they appear.
- Merchant views exclude other merchants' private data; the network-level record is an
  internal artefact (tenant scoping is P4).

## Test changes

Added 92 tests (329 → 421): 86 unit tests (22 network/builder, 2 boundary, 21
scenario, 25 verifier, 9 solver, 7 plan-record); 5 Hypothesis properties (solver = brute force, verifier conservation,
baseline dominance, minimum-liquidity tightness, truncated search never OPTIMAL); 1
PostgreSQL concurrency test. Existing P0/P1 tests unchanged.

## Limitations (open for later phases)

- Full-settlement legs only (no partial payments or instalments).
- Per-leg acceptance; no benefit sharing between merchants (doc 06, research).
- Deterministic inputs; no risk-aware or probabilistic planning (doc 05 B6).
- Exact search is exponential in the worst case; sized for demo networks (≤ ~10 legs).
- Plans are not persisted and approvals are not recorded (P4); no endpoint yet.

## Addendum (2026-11-11): hardening after the owner's P2 review

15. **Plan integrity.** `plan_id` = `plan_` + the first 16 hex digits of SHA-256 over the
    canonical JSON of every other field: a content checksum, stated in the record's
    `integrity` block to be **not an authorization or signature**. `verify_plan_record`:
    - re-derives every financial field from the immutable network with the independent verifier;
    - checks `solver.value` against the replayed objective;
    - rebuilds the record deterministically and compares every key.

    Before this, 22 of 26 tampering cases escaped detection: plan id, merchant benefits, approvals, expiry, fees, baselines, increments, leg proceeds, slot starts, dependencies, leg status, flags and aggregates (`evidence/phase2/hardening_reproduction_2026-11-11.txt`). All are now detected, including after an attacker recomputes `plan_id`. `execution.may_execute` stays `false` for every P2 plan. Future execution must reload current state and validate real approvals.
16. **Supplier-side acceptance.** `MerchantPolicy.min_receipt_net_benefit_cents` (None =
    no declared threshold) and `receipt_cost_cents`. For a leg that changes the payee's
    outcome (extra concession or early payment): stated early-receipt value − extra
    concession − payee fee − declared cost ≥ threshold, enforced by both solver and
    verifier (`PAYEE_REJECTS`), alongside the existing concession cap, fee ceiling and
    prohibited counterparties. Defaults leave `canonical-p2-v1` and the 320-network
    benchmark unchanged (reproduced 320/320). The declared cost also enters surplus and
    the payee's net benefit.
17. **Economics are reported, not asserted.** Plans carry `economics` (participants,
    `modeled_losses`, a note that values are modeled from declared synthetic inputs) and a
    `MODELED_NET_LOSS:<merchant>` warning. The canonical plan is **not** a win-win: buyer
    +$3.70, food supplier −$10.00 (it signed the offer but declared no receipt value).
    A separate `network_positive_sum` fixture, labelled `CONSTRUCTED_EXAMPLE` with its
    assumptions disclosed, gives buyer +$3.70 and supplier +$3.73. Its stated assumptions: early cash is worth 20%/yr to the supplier, a $2.00 declared cost, a $10.00 concession cap and a $0.00 threshold.
18. **Measurement.** The published metric stays: `on_time_face_cents` = original face of
    invoices paid by their due slot (B0–B5 comparable). New, separate metrics:
    - `discharged.open_discharged_cents`: outstanding value the legs discharge, split into cash and new concessions;
    - `discharged.prior_cash_applied_excluded_cents`: cash applied before planning, which is never counted as new movement;
    - `on_time_open_discharged_cents`, with matching baseline expectations and increments.

    `gross_flow_cents` counts re-spent cash again; `incremental_*` is plan − baseline on the same network. For an invoice of $1,000 with $400 already applied and a $10 offer, the figures are: face on time $1,000, discharged $600 (cash $590 + $10 concession), gross flow $590.
19. **Presentation.** Benchmark summaries open with the headline findings:
    - B4 beats B1 in 3% of generated networks and B3 in 12%;
    - B5 improves on B4 in 2%;
    - under B4 the buyer's modeled net benefit is below B2 in 23%.

    `network_timing_matters` is a constructed (non-representative) case where earliest-due-first pays the earlier-due invoice with the only cash and makes a downstream carrier late. B4 pays the other invoice first, and every invoice stays on time ($2,900 vs B1 $2,000; B2's tie-break happens to find the same; B3 $2,057.45).
