# 05 — Optimization, Graph Algorithms and Settlement Mathematics

## Domain representation
Permissioned directed multigraph `G=(V,E)`. Each merchant is a node; each invoice obligation is a directed edge `(debtor → creditor)`. Parallel invoices are separate by immutable ID. Every edge has original face amount, open cents, due instant, currency, dispute status, legal owner, revision, earliest legally allowable payment instant and evidence provenance. Buyer-funded offers are separate candidate modifications, not hidden invoice fields. Availability is a function of time and evidence source, not a graph edge alone.

## Mathematical planning modes
**Mode 1: buyer decision.** Choose one or more early supplier payments within its verified deployable cash and approval policy, compare private discounts and downstream outcomes. This is the hackathon product nucleus.

**Mode 2: network feasibility.** Given candidate consenting participants and time horizon, find executable payment ordering meeting every due date, reserve and availability constraint. Every downstream payment requires payer's own consent and usable cash at execution, and all legs may be recomputed after any observed event.

**Mode 3: minimum-liquidity target.** Given a target set/value of obligations, minimize additional committed **existing** liquidity or hypothetical seed financing, with those two modes reported separately. Do not silently introduce borrowable funds.

**Mode 4: multilateral noncash clearing (research).** Find circulation `f_e` on eligible edges with `0 ≤ f_e ≤ open_e` and for each vertex `sum_in f_e = sum_out f_e`. Maximize valid extinguished face, subject to accepted legal setoff conditions. Use independently signed atomic batch and proper journals, **no PayPal payment claims**.

## Constraints (per currency and plan version)
- Financial amounts are nonnegative integer minor units; never IEEE float for transaction amounts.
- An invoice cannot be settled twice; original invoice ID remains stable across revisions/splits.
- Include contractually accepted concessions; a buyer's discount floor and supplier's minimum receipt must both be satisfied.
- Preserve merchant reserve after **every** outgoing leg, net of fees and other reserved commitments.
- Incoming cash is spendable only after a verified or explicitly simulated availability event in a later slot; no simultaneous assumed reuse.
- Respect invoice due time, discount expiry, terms updates, disputes, rejections, payer-scope and merchant opt-outs.
- Conditional transfers have independent payer authorization; one merchant cannot sign for another.
- No plan can consume the same verified balance attestation twice across concurrent plans.
- Cross-currency netting/FX is outside initial scope.

## Baseline-aware objective
Do not default to maximizing total invoice face paid by a network, which rewards payments that were already scheduled. Let `B_e` be 1 when baseline would pay invoice e by deadline; `Y_e` be 1 when plan does; impact `I_e = max(0,Y_e−B_e)` *only in a paired scenario with the same inputs*. Define a multiobjective or Pareto frontier:
1. Hard: legality, consent, verified cash, reserves, deadlines, private merchant acceptance constraints.
2. Maximize **additional** on-time face/count relative to baseline, with clearly labeled assumptions.
3. Within comparable impact, improve net participant surplus and minimize liquidity-dollar-days, fees and worst-case shortfall.
4. Show buyer-facing alternatives, including the plan maximizing the buyer's own return; never force network optimum over merchant preference.
5. Tie-break deterministically by stable invoice identifiers, and report why the order won.

A synthetic weighted score can be used for interface ranking only if its weights are openly chosen by merchant; hard safety constraints cannot be turned into soft score penalties.

## Time-indexed feasibility model
For small networks enumerate invoice/slot payment variables `x[e,t]∈{0,1}` and offer-choice variables. Each merchant has `cash[m,t]` and `reserved[m,t]`. An outgoing settled payment at t reduces payer cash including payer fees; a prior incoming payment increases cash only at a verified availability time `t+lag`, not t automatically. Require `cash[m,t]−reserve[m,t]−committed[m,t]≥0` for all merchants and time prefixes. Invoice constraints restrict sum of chosen complete-settlement actions. Model partials as a separate later-release problem.

**Exact search:** Enumerate orderings for up to ~10 candidate legs in the demo, compare independent reference solver. **Scale-up:** MILP or OR-Tools CP-SAT (integer cents/time slots). Return solver status OPTIMAL/FEASIBLE/INFEASIBLE/UNKNOWN, solve time, best bound when supported, model version, and normalized replay trace. Never call a non-proven feasible solution 'optimal'.

## Minimum seed liquidity
For a fixed target T and permitted funding merchants M, solve:
`minimize Σ_m seed_m` subject to every targeted obligation fulfilled before deadline and every cash/consent constraint. Seed must be explicitly tagged as either merchant **existing spare cash** or **hypothetical external funding**. For zero-fee A→B→C→A loop of three $100 bills with no starting usable funds, sequential cash payment may require $100 seed, whereas legally valid netting might require zero. These are separate modes.

## Risk-aware optimization (after validated data)
Optimize expected incremental benefit subject to constraints such as a maximum probability of liquidity shortfall or tail-loss metric (CVaR). Correlated branch failures matter: multiply probabilities only with justified conditional independence. If distributions are synthetic, label them. A deterministic verifier still gates every real operation.

## Planner result contract
Include `plan_id`, `snapshot_digest`, `solver_type/version`, `status`, `objective_definition`, `baseline_id`, `currency`, `cash_reservations`, `forecast_legs`, `binding_constraints`, `required_approvals`, `critical_dependencies`, `availability_assumptions`, `merchant_benefits`, `downstream_face`, `gross_flow`, `incremental_on_time_face`, `warnings`, and `expires_at`.

## Failure and re-optimization
A stale policy, changed invoice, withdrawal, hold, reversal, unexpected payment or missed deadline invalidates **future** legs. Do not erase completed transfers. Freeze dependent intent, reconcile observed facts, compute residual graph and ask merchants for fresh approvals. Provide explainable infeasibility: 'Supplier needs $900 + $150 reserve, but only $990 verified available, so short by $60.'
