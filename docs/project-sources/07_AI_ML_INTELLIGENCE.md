# 07 — AI and Machine Learning Architecture

## Separation of duties
**LLM:** interpret natural-language policies and propose negotiated terms in words and typed JSON. **Deterministic financial engine:** calculates legality/consent/cash feasibility and selects candidate plans. **ML predictor (future or explicitly experimental):** estimates uncertain timing/refusal likelihood. **Payment controller:** waits for verified human approvals and PayPal facts. An LLM has *no payment credentials, signing powers or authority to mark funds as available*.

## Immediate AI capability A: merchant policy interpretation
Example user: "Keep $500 for payroll through Friday and don't pay repair bill M-17 before I approve the work." Parse into `reserve`, `valid_to`, `excluded_invoice` or `requires_approval`. Respond with exact extracted fields, linked original spans, ambiguities and a confirmation control. Deterministic validation resolves currency, timezone, date, merchant scope and invoice identity. Ambiguous M-17 or Friday ⇒ ask user; **never default missing thresholds to zero**.

## Immediate AI capability B: bounded negotiation assistant
Input: only merchant-authorized public offer ranges, redacted reason for infeasibility, and permitted actions. Output: typed candidate change `{kind, invoice_id, effective_date, proposed_discount_cents, explanation, references_to_policy}`. Allowed kinds: amend timing, discount, payment threshold, or proposed installment with explicit legal implications. The solver checks every candidate. Merchant individually authorizes amendment, then separately authorizes any payment. AI can help write a polite proposal but cannot impersonate a counterparty.

## Useful additional AI: infeasibility explanation
Convert solver rejection reasons into plain language with cited graph constraints. Example: "Supplier B cannot make its $900 carrier payment because its $990 receipt must leave $150 reserved; the shortfall is $60 before fees." This is trustworthy **only** if calculated by the engine, not generated from model intuition.

## Proposed ML modules in order
| Model | Inputs and label | Method | When appropriate |
|---|---|---|---|
| Payment-timing forecast | Historical invoice terms, payer, amount, prior payment dates; outcome = days to usable paid receipt | Kaplan–Meier/survival baseline, gradient boosted trees | Only with legitimate labeled merchant history; avoid leakage |
| Offer acceptance | Prior offer terms + merchant-authorized attributes; outcome accepted/declined | Logistic regression baseline, CatBoost/XGBoost | Only once enough repeat offers exist |
| Cascade-risk forecast | Node and payment history, predicted availability; outcome = successful timely fulfillment | Calibrated conditional model; graph neural nets later | When graph data outperforms non-graph baseline |
| Risk-aware planner | Probability distributions from validated models | Monte Carlo, CVaR, robust/stochastic optimization | After deterministic plan is correct |
| Causal impact | Comparable interventions/non-interventions | Randomized experiments or defensible observational causal methods | Only with merchant pilots and necessary data |

## Data availability warning
There is **no verified labeled dataset of interconnected consenting PayPal merchant payments** in the current package. Synthetic examples are useful for testing algorithms and UI but *cannot validate real-world predictive accuracy*. If ML uses synthetic labels, present it as a simulation/illustration, not as a deployed commercial predictor. Avoid a gratuitous GNN or reinforcement-learning agent without measured gains over baselines.

## Evaluation protocol
Policy parsing: held-out human-annotated set, exact-match restrictions, unsafe relaxed-rule rate (target zero on golden cases), abstention/clarification rate, human correction effort. Negotiation: valid proposals / total attempts, approvals, private value improvement, no private threshold leaks. Forecast: MAE or survival calibration, Brier score, stratified fairness, temporal holdout; compare simple heuristics. Planning: paired realized incremental on-time value and cost against deterministic baseline, not just predicted expected value. Explicitly separate simulation results from observed PayPal data.

## Threat model for language models
Invoice PDFs and merchant messages may contain prompt injection. Treat all uploaded content as untrusted; isolate tenants, restrict context, schema-validate responses, show source spans, require explicit user confirmation, redact private balances, and reject model instructions that call tools or override reserve/approval rules. Log model name/version, prompt version, sanitized context hash and human decision. A failed model call falls back to confirmed policies/manual entry.

## Example output schema (illustrative)
```json
{
  "proposal_type": "early_payment_discount",
  "invoice_id": "inv_food_1000",
  "proposed_payment_cents": 99000,
  "proposed_concession_cents": 1000,
  "currency": "USD",
  "source_span": "Would $10 off today work?",
  "human_confirmation_required": true,
  "reason_code": "SUPPLIER_CASH_GAP",
  "status": "draft_only"
}
```
This record is a draft, not a modified invoice or payment authorization.
