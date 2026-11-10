# 12 — Independent Verification, Benchmarking and Impact Proof

## Four evidence levels
1. **Design claim:** specified behavior in docs, no implementation proof.
2. **Synthetic test:** measured outcome on invented financial data, labeled synthetic.
3. **Sandbox provider proof:** actual documented payment event and independently fetched PayPal resource.
4. **Merchant field evidence:** appropriately authorized real invoices/outcomes, valid counterfactual comparison and operator review.

Do not present one evidence level as another.

## Baselines to compare under identical inputs
B0: no accelerated payment.
B1: earliest due invoice first.
B2: buyer maximizes net discount alone, subject to same reserve and fees.
B3: buyer-only rule with random tie resolution; use expected outcome for ties, not just worst comparison.
B4: Elide graph-aware deterministic optimizer.
B5: Elide with accepted negotiated change (different feasible set; state this explicitly).
B6 (later): risk-aware planner using validated forecasts.

Fair comparison fixes visible invoices, consents, fees, source-of-cash, due dates, availability lag, decision cutoff and uncertainty realization.

## Example tie trap
If two equal-price early payments exist and one yields $1,700 downstream face and the other yields $0, a blind random choice already gets **$850 expected downstream face**. Do not advertise $1,700 average improvement merely because Elide finds the best branch in a cherry-picked graph. Report outcome vs B3 and on-time incremental value from a baseline schedule.

## Experiment design
Generate seeded synthetic networks across disconnected, chains, cycles, overlapping obligations, shallow/deep graphs, partial participation, scarce and abundant cash, high and low fees, realistic fund lags, disputed invoices, competing reserves and refusals. Keep seeds and generator version fixed per baseline. Pair outcomes by network. Report median, interquartile range, 5th–95th percentiles and zero/negative lift fraction, not only best-case examples.

## Core outcome metrics
- `additional_on_time_face_cents`: plan − fixed baseline for original invoices paid by due time.
- `net_incremental_private_value_cents`: per-party benefit accounting for discount, financing/opportunity costs and fees.
- `seed_cash_cents`: minimum existing committed cash or explicitly hypothetical funding, not conflated.
- `liquidity_dollar_days`: time-weighted early deployment.
- `gross_payment_face_cents`: sum of distinct transfer legs with repeat-cash warning.
- `completion_rate`, `consent_rate`, `privacy_refusal_rate`, `provider_reconciliation_error_rate`.
- `solving_latency_ms`, infeasible rate, bound/optimality gap if solver supports.
- If ML exists: reliability/calibration, error by merchant segment and realized incremental vs baseline.

## Independent domain test matrix
| Test | Pass condition |
|---|---|
| Currency cents and fees | Conservation/journal balances hold; no floating rounding |
| Tiny exact optimality | Planner matches exhaustive enumerator objective and constraints |
| Reserve and same-slot reuse | No leg may spend reserve or funds not yet available |
| Discount concession | Original face = cash applied + valid concession + residual |
| Double invoice / split | One original obligation is not counted or paid twice |
| Decline and version change | Dependent unexecuted legs invalidated |
| Parallel sessions | Same cash not reserved twice; conflicting plan returns 409 |
| Provider duplicate | One economic posting despite duplicate events |
| Reversed receipt | Compensating journal and exception, no historical rewrite |
| Cross-tenant | No restricted row, aggregate leak or resource enumeration |
| AI injected document | No policy relaxation without human confirmation |
| Installation from clean clone | Reproducible demo with documented and optional sandbox secrets |

## AI evaluation specifics
Write human-labeled policy golden cases and adversarial cases in advance. Measure unsafe relaxations (zero tolerance for release cases), exact rule accuracy, abstentions and user edit rate. For negotiation measure proposal feasibility and participant approval. For synthetic ML demonstrate algorithm functionality only, not commercial forecast validity.

## Provider evidence procedure
Each run records the specific Sandbox invoice/payment ID, fetched provider status, exact amount/currency, fees when available, webhook ID, local journal digest, actor approvals and time. Force duplicate event replay and one mid-chain failure. Mark next leg 'simulated' if spendable-funds source is fictional.

## Business falsification gates
Stop claiming commercial value if consent is low, accounting reconciliation is unsupported, data visibility is too sparse, discount economics fail on both sides, actual payment availability breaks most chains, or blinded historical replay shows no material improvement after costs. Publish negative and neutral outcomes alongside wins.
