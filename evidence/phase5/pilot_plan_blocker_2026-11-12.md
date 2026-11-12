# P5 pilot blocker: empty payment plan after the accepted offer (2026-11-12)

**Status:** reproduced, root cause established, correction proposed (ADR-0013, *Proposed*).
**Not done:** no payment authority was changed. No PayPal call, worker run, cancellation,
reseeding or model call was made. The pilot API was restarted with execution **off** and the
provider `none`. The pilot database and its accepted offer are untouched; the diagnosis read
it in a rolled-back transaction.

## Reproduction

`scripts/diagnose_pilot_plan.py` loads the network as the workspace does and runs the solver
together with its own per-slot leg evaluation. Output:
`evidence/phase5/pilot_plan_diagnosis_20261112T164855Z.json`.

**Invoice `pilot_001`.**
- Revision 3 is AUTHORIZED, signed by `pilot_buyer` and `pilot_seller`.
- Concession USD 1.00, open USD 24.00.
- Pay-by 2026-11-15T13:13Z; the original due date was 2026-11-22.

**Payer `pilot_buyer`.**
- Merchant-attested USD 200.00 with a USD 50.00 reserve.
- **Valid until 2026-11-13T16:34Z** (a 24 h attestation).
- Policy: minimum early discount USD 1.00, hurdle 800 bps, minimum net benefit 0.

**The one option `pilot_001:r3`** (in force, USD 24.00, USD 1.00 concession); the due slot is 2:

| Slot | Start | Early | Refused because |
|---|---|---|---|
| 0 | 11-12 16:48Z | yes | `extra = 100 − 100 = 0 < min_early_discount 100` |
| 1 | 11-13 16:48Z | yes | capacity evidence expired; early rule |
| 2 | 11-14 16:48Z | no | **capacity evidence expired** |
| 3 | 11-15 16:48Z | late | capacity evidence expired; late |

The solver returns OPTIMAL with an empty schedule. That is correct for these constraints.

## Isolating the causes (in-memory what-ifs; nothing persisted)

| What-if | Feasible slots | Plan |
|---|---|---|
| As is | none | empty |
| Minimum-discount rule removed | none: paying at slot 0 costs **2 cents** of time value (USD 24.00 × 8% × 2 days) for 0 incremental benefit, so the net-benefit rule refuses it | empty |
| Evidence valid through the pay-by date (72 h) | **slot 2 only** | **`pilot_001:r3` on the agreed date** |
| ... plus no discount rule and a 5-cent loss accepted | 0, 1, 2 | — |

## Root cause: a combination, one part decisive

1. **Decisive: the capacity evidence expired before the agreed pay-by date.**
   - Elide reserves the payer's cash from the moment the intent is created until settlement,
     and the hosted invoice can be paid at any time up to the pay-by date.
   - So evidence must cover that whole window. A 24 h attestation for a payment due in about
     68 h is not enough, and refusing it is correct conservatism, not a bug.
   - My attestation script defaults to 24 h, which contributed.
2. **Genuine: there is no incremental incentive to pay earlier than the agreed date.**
   - The USD 1.00 concession is already in the terms.
   - Accelerating further gains nothing and costs time value at the buyer's own hurdle.
   - The optimizer is right not to recommend paying before 11-14/15.
3. **Latent P2 defect, real but not decisive here: the early-payment discount rule.**
   - `min_early_discount` is compared with the *incremental* concession relative to the
     *revised* due date. The buyer's rule "pay early only for at least $1 off" therefore
     becomes "at least $1 *more* off to pay before the date I already agreed".
   - It should be judged against the **original** terms: total concession vs the original
     due date.
   - The time-cost and net-benefit test and the objective must stay incremental, so the
     agreed $1 is never counted twice.
   - Pinned by a strict xfail in `tests/integration/test_p5_plan_blocker.py`.

**Not the cause.**
- The agreed revision is treated correctly as in-force terms (cash USD 24.00 plus USD 1.00
  concession = face USD 25.00).
- The verifier was never reached.
- Reserves and policy floors were respected.

## Recommended correction

**R1, for the pilot. No code change, no new payment authority.**
1. The owner re-attests the pilot buyer's balance with validity covering the pay-by window:
   `--valid-hours 72`; any attestation made after 2026-11-12T13:13Z covers 2026-11-15T13:13Z.
2. The existing optimizer and verifier then plan the USD 24.00 payment **on the agreed date**.
3. The owner approves it; the worker sends the invoice.
4. The owner may pay in PayPal's page at any time before the pay-by date.

**Economics.**
- The payment is exactly on the agreed terms; the USD 1.00 concession was already agreed.
- Paying today rather than on 11-15 is about 3 days earlier than necessary: about 2 cents of
  time value at the stated 8% hurdle.
- That is the payer's own choice at PayPal, not an Elide recommendation, and the UI states
  it.

**R2, the model correction (ADR-0013, Proposed; needs approval).**
- (a) Correct the early-discount semantics in **both** the solver and the independent
  verifier, using original terms for the policy rule and incremental terms for time cost and
  objective.
- (b) Add an **explicit merchant payment instruction**, "pay this agreed obligation now".
  It is a separate decision, never shown as the optimizer's plan, and has its own independent
  check over the actual execution window (current evidence, reserve, policy, terms, fees,
  provider permissions). It carries a disclosure of its economics against the original terms
  and against the agreed date, and goes through the same authorization and P5 revalidation.

## Regression tests

`tests/integration/test_p5_plan_blocker.py` (PostgreSQL, real persistence path):
- 24 h evidence: no feasible slot, empty plan.
- Evidence covering the window: the payment is planned on the agreed date only.
- Acceleration has no incremental benefit, even without the discount rule.
- A newer reserve still binds.
- The discount-semantics defect, as a strict xfail.
