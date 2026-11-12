# P4 architectural review: reproduction on the pre-fix code (`f935fe7`)

These runs use the synthetic `workspace-seed-v1` network on a throwaway PostgreSQL database, over HTTP with separate sessions. They involved no PayPal or model calls.

## Finding 1 (critical): a stale financial authorization still created a payment intent

**Setup.**
1. The buyer has USD 1,490.00 spendable and USD 500.00 protected (synthetic evidence).
2. The buyer counters at $10 and the supplier accepts.
3. The buyer authorizes the USD 990.00 leg `food_001:r4`.
4. The buyer then confirms a policy raising the protected reserve to USD 700.00 (`reserve_floor_cents` 70000).

**Observed.**
- The re-plan no longer contains the leg: the plan view shows `my_legs = []`.
- `POST /payment-intents` with the old authorization returned **200 `awaiting_payer_action`**. One intent was written and USD 990.00 was reserved, leaving USD 0 above the old USD 500 reserve, below the confirmed USD 700 floor.

**Root causes.**
1. `revalidate_leg` checked only that the option id still existed and that payer, payee and amount were equal. Options exist whether or not they are fundable, so this check is not a financial check, and the independent verifier was not re-run.
2. `reserve_capacity_in` used the evidence's protected reserve (USD 500) and ignored the merchant's confirmed policy floor (USD 700) and its maximum early commitment.
3. Neither the authorization binding nor `existing_authorization()` recorded the conditions the approval was given under: policy version, effective reserve, participation, evidence basis, fee terms, the invoice revision's content, due date and open balance. As a result an approval matched any later plan with the same option id and amount.
4. `existing_authorization()` also returned expired approvals for the same plan, so the UI could show them as approved.
5. The actor's and the approver's memberships were checked outside the transaction that writes the authorization or intent. A revocation committing in between was not seen.

## Finding 2 (high): aggregates leaked after disclosure was refused

**Setup.** As above, after the agreement. The carrier sets `share_aggregates = false`.

**Observed.** The buyer's response had `aggregates.status = WITHHELD_NO_CONSENT`, yet still contained:
- `recommendation.reason`: "the plan pays **USD 1,700.00** more invoice face on time than doing nothing …";
- `decision.consent.approvals_required = 3`, which reveals the size of the network;
- `plan.downstream_legs_count = 2`;
- an explanation fact, the input to any model narration, carrying the same USD 1,700.00 total.

The non-initiating supplier also received `plan.downstream_legs_count = 1`.

**Root cause.** The view was the privileged P3 report with a few fields deleted after the fact (`suppress_aggregates`). Free text, counts and the recommendation action were derived from network data before the disclosure decision. The P2 plan view was not covered at all.

## Finding 3: GET wrote durable records

**Observed.** Five `GET /v1/m/buyer/opportunities` requests, 30 s apart, created **5** `plan_snapshot` rows.

**Root cause.** `analyze_for()` stored every analysis. As a result, a read request had durable side effects with no CSRF check, idempotency or rate limit, and immutable plan records grew without bound.
