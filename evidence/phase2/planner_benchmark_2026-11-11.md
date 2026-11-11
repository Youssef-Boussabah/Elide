# P2 planner benchmark (SYNTHETIC)

Generated 2026-11-11T01:45:22+00:00 by `scripts/run_planner_benchmarks.py` at commit `78abccb`; solver `p2-solver-1`, verifier `p2-verifier-1`, generator `p2-gen-1`. Evidence class: **Synthetic-tested**. No PayPal call.

## Canonical scenarios

| Scenario | Solver | Plan | On-time face | Gross flow | B0/B1/B2/B3/B4/B5 on-time |
|---|---|---|---|---|---|
| A two candidates | OPTIMAL | food_001:r2:offer@0, freight_001:r1@28, repair_001:r1@29 | $2,700.00 | $2,690.00 | $0.00 / $2,700.00 / $2,700.00 / $1,850.00 / $2,700.00 / $2,700.00 |
| B negotiation (B4: no change) | OPTIMAL | (none) | $0.00 | $0.00 | $0.00 / $0.00 / $0.00 / $0.00 / $0.00 / $2,700.00 |
| B negotiation (B5: $10 assumed approved) | OPTIMAL | food_001:r3:proposal@0, freight_001:r1@28, repair_001:r1@29 | $2,700.00 | $2,690.00 | $0.00 / $0.00 / $0.00 / $0.00 / $0.00 / $2,700.00 |
| C held receipt | INFEASIBLE | (none) | $0.00 | $0.00 | - |
| D reserve shortfall | INFEASIBLE | (none) | $0.00 | $0.00 | - |
| D' hypothetical USD net after the Sandbox fee | INFEASIBLE | (none) | $0.00 | $0.00 | - |
| F payee fee USD 29.01 in the cascade | INFEASIBLE | (none) | $0.00 | $0.00 | - |
| E CAD settlement credit | INFEASIBLE | (none) | $0.00 | $0.00 | - |

Infeasible targets (explained, with HYPOTHETICAL gap sizing):

- C held receipt: food_supplier needs USD 900.00 + USD 150.00 reserve, has USD 0.00 available at slot 0: short by USD 1050.00 (hypothetical: food_supplier $1,050.00)
- D reserve shortfall: food_supplier needs USD 900.00 + USD 150.00 reserve, has USD 990.00 available at slot 0: short by USD 60.00 (hypothetical: food_supplier $60.00)
- D' hypothetical USD net after the Sandbox fee: food_supplier needs USD 900.00 + USD 150.00 reserve, has USD 960.99 available at slot 0: short by USD 89.01 (hypothetical: food_supplier $89.01)
- F payee fee USD 29.01 in the cascade: food_supplier needs USD 900.00 + USD 150.00 reserve, has USD 960.99 available at slot 28: short by USD 89.01 (hypothetical: food_supplier $89.01)
- E CAD settlement credit: food_supplier needs USD 900.00 + USD 0.00 reserve, has USD 0.00 available at slot 28: short by USD 900.00 (hypothetical: food_supplier $900.00)
- minimum existing seed for food -> freight -> repair: OPTIMAL, existing {'buyer': 99000}, hypothetical {}
- A->B->C->A $100 loop, no cash (doc 05): OPTIMAL, existing {}, hypothetical {'a': 10000}

## Seeded synthetic networks (320 networks: 8 families x 40 seeds, <= 6 obligations, 4 slots)

Lift in on-time invoice face (cents) per network, paired on identical inputs:

| Comparison | median | p25 | p75 | p5 | p95 | mean | zero | negative | positive |
|---|---|---|---|---|---|---|---|---|---|
| B4_minus_B0 | 0 | 0 | 0 | 0 | 30000 | 4062 | 87% | 0% | 13% |
| B4_minus_B1 | 0 | 0 | 0 | 0 | 0 | 1406 | 97% | 0% | 3% |
| B4_minus_B2 | 0 | 0 | 0 | 0 | 30000 | 3531 | 91% | 0% | 9% |
| B4_minus_B3 | 0 | 0 | 0 | 0 | 30000 | 4154 | 88% | 0% | 12% |
| B5_minus_B4 | 0 | 0 | 0 | 0 | 0 | 312 | 98% | 0% | 2% |
| buyer net B4_minus_B2 | 0 | 0 | 0 | -1992 | 0 | -209 | 76% | 23% | 1% |
| buyer net B5_minus_B2 | 0 | 0 | 0 | -1990 | 515 | -134 | 72% | 22% | 5% |

Solver vs brute force (<= 4 obligations, 3 slots, with and without proposals): 640 / 640 identical value and schedule.
B4 solver statuses: OPTIMAL.
Median time for all six baselines per network: 0.005 s (max 0.053 s; machine-specific).

Reading: B4 >= B0..B3 on on-time face by construction (every baseline schedule is in the
planner's feasible set). Zero lift is the common case; positive lift needs a real
choice between candidate payments. B5 changes the feasible set (assumed approvals).
Buyer net benefit can be lower under B4 than under B2: the network plan is shown
next to the buyer's own best plan and the buyer's minimum benefit is still enforced.
