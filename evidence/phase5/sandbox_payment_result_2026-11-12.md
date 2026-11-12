# P5 controlled Sandbox payment: result (2026-11-12)

**Evidence class: Sandbox-verified.** This is a real PayPal Sandbox payment, independently
retrieved by provider GETs. It is not real money, and not pilot-validated with real merchants.

- **Branch:** `feat/p5-paypal-execution`. Product code is identical to the reviewed
  `66c95ff`; later commits add only scripts and evidence.
- **Database:** the isolated `elide_sandbox_pilot`.

## What happened

| When (UTC) | Who | What |
|---|---|---|
| 11-12 | Owner | Bound the TEST merchants to the existing Sandbox accounts; attested the payer's balance (merchant-attested, 72 h); accepted the USD 1.00 offer; approved the USD 24.00 leg; prepared the intent; released it in the workspace |
| 18:18:56–59 | Worker, one owner-authorized pass | Create invoice `INV2-HQXZ-SCNE-RKTV-VMMT` (201, request `elide-pin_7452d6099eaf9caf-create`); GET; send (200, request `…-send`); GET (SENT). Mutating execution was disabled immediately afterwards |
| 18:24:35 (capture time) | Owner, as the Sandbox payer | Paid USD 24.00 in PayPal's hosted page (owner action; not initiated or simulated by Elide) |
| 18:26:13 | Worker, mutations disabled | GET invoice (PAID), GET capture: verified, booked once, independently checked; intent `reconciled` |
| 18:27:06 | Replay check | Duplicate notifications, second reconcile, direct second booking, re-posted bundle: nothing added |

Exactly **two** provider-mutating calls were made in the whole pilot: one create and one
send, each on its first attempt with its persisted request id. No cancel, refund, update,
delete, record-payment or capture was made by Elide.

## Provider facts (GETs; `settlement_before-booking_*.json` and `settlement_after-booking_*.json`)

| Field | Value |
|---|---|
| Invoice | `INV2-HQXZ-SCNE-RKTV-VMMT`, number `ELIDE-4E480F60A4F8`, **PAID**; item USD 25.00, discount USD 1.00, total USD 24.00, paid USD 24.00, due 0.00; one SUCCESS transaction. Strict PAID comparison: no problems |
| Capture | `82L451107A123522J`, **COMPLETED**, final; invoice id matches; created 2026-11-12T18:24:35Z; seller protection ELIGIBLE |
| Gross / fee / net | **USD 24.00 / USD 1.00 / USD 23.00** (PayPal-reported fee) |
| Credited | **CAD 31.57** at 1.37241975 USD→CAD (23.00 × 1.37241975 = 31.5656, rounds to 31.57) |
| Payee | capture `payee.merchant_id` fingerprint `redacted:6dbc83888cc3`, which matches the invoicer binding |
| Payer | the invoice recipient is the approved Sandbox payer (`redacted:94281907e2c4`). PayPal's invoice and capture do not identify which PayPal account paid, so this is not claimed |

## Accounting (`api/accounting`; one transaction)

**Payee `pilot_seller`.**
- Invoice issued: Dr receivable 25.00 / Cr revenue 25.00.
- Concession: Dr contra-revenue 1.00 / Cr receivable 1.00.
- Capture settlement:
  - USD: clearing 24.00 against the receivable; fee 1.00; net 23.00 to the FX position;
  - CAD: `paypal_balance` 31.57 against the FX position.
- The receivable nets to **0**.

**Payer `pilot_buyer`.**
- Payable recognized: Dr purchases 25.00 / Cr payable 25.00 (merchant data).
- Payment sent: Dr payable 25.00 / Cr PayPal payments sent 24.00 / Cr discount received 1.00.
- The payable nets to **0**.

**Totals.**
- **Obligation:** face 25.00 = cash applied 24.00 + agreed concession 1.00 + remaining 0.00
  (settled). One settlement event `paypal_capture:82l451107a123522j`.
- **Settlement record:** one, for the capture.
- **Independent checker:** no problems. **Trial balance:** USD 0, CAD 0.

## Reservations and capacity

- **Payer.** The USD 24.00 reservation was converted, not freed: it closed as
  `settled:capture:82L451107A123522J` and a **settled outflow of USD 24.00 remains committed**.
  It is released only by newer balance evidence that lists it. Deployable:
  200.00 − 50.00 − 24.00 = USD 126.00 (merchant-attested).
- **Payee.** The receipt is **PROVIDER_CONFIRMED_NOT_SPENDABLE**; usable USD is **0.00**
  (D006; the credit is CAD, D008). No capacity is inferred from the payment.

## Exactly once (`replay_20261112T182706Z.json`)

Before and after: 5 journal entries, 17 postings, 1 settlement event, 1 settlement record,
2 commitments, 0 exceptions; the trial balance is zero.

- Two duplicate provider-fetch jobs and a second reconcile were processed with mutations
  disabled.
- A direct second booking was a no-op.
- All 5 re-posted entries returned `duplicate`.

## Observations and open items

- **INV-1 reproduced.** The invoice transaction time (`10:24:36Z`) and the capture time
  (`18:24:35Z`) again differ by 7 h 59 m 59 s, as in P0. It is consistent with the invoice
  field reporting Pacific time labelled as UTC, but that is **not confirmed**. Ordering uses
  the capture time, and both raw values are kept.
- **Partial-payment flag.** PayPal's invoice representation does not echo
  `partial_payment.allow_partial_payment`: the field is absent, which is the non-partial
  default. The payment was in full.
- **Webhooks** were not used: there is no public endpoint. Reconciliation ran by polling
  GETs, as designed (A14 still open).
- **Refund posting** is still detection only.
- **ADR-0013** (agreed-terms semantics and explicit payment decisions) is accepted but not
  implemented; its strict xfail remains.
