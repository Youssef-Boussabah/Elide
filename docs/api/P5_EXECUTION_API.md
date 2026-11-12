# P5 execution API (ADR-0012)

All routes are under the authenticated, merchant-scoped workspace (`/v1/m/{merchant}`) and
follow the P4 contract: session cookie, CSRF header and Origin check on state changes,
`Idempotency-Key` on mutations, identical `404` for anything outside the merchant's scope,
errors as `{"detail": {"code", "message"}}`. Amounts are integer USD cents.

## Intent creation (changed from P4)

`POST /v1/m/{m}/payment-intents` — unchanged request. The intent now starts in `approved`
(P4: `awaiting_payer_action`) with `status_reason`:

| `status_reason` | Meaning |
|---|---|
| `QUEUED_FOR_PROVIDER` | Dispatchable; the worker revalidates before any PayPal call |
| `SYNTHETIC_EVIDENCE_HELD` | Synthetic cash; can never execute (DB-enforced) |
| `EXECUTION_DISABLED` | Kill switch off |
| `PROVIDER_NOT_CONNECTED` | Payee or payer has no active PayPal Sandbox binding |

Every intent response carries `money_moved`, `provider_confirmed` and `funds_usable`
(always `false`: D006).

## New routes

| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `/payment-intents/{id}` | `workspace:read` (payer or payee) | State, reason, history, exceptions (codes), session summary (no link). Payer also sees its reservation; payee sees its settlement (fee, net, credit, receipt status) |
| GET | `/payment-intents/{id}/payer-link` | `payment:authorize` + step-up, payer only | Fetches the invoice by GET, re-checks terms, returns `recipient_view_url` (Sandbox host only). `Cache-Control: no-store`. `409 NOT_AWAITING_PAYER`, `409 PAY_DEADLINE_PASSED`, `409 NEEDS_REVIEW`, `409 PROVIDER_TERMS_MISMATCH`, `409 PROVIDER_UNAVAILABLE` |
| POST | `/payment-intents/{id}/release` | `payment:authorize` + step-up, payer only, Idempotency-Key | Release a HELD intent (held for `EXECUTION_DISABLED` / `PROVIDER_NOT_CONNECTED`): exactly one held, never-attempted job; the worker's `revalidate()`; held → pending; same intent, authorization and reservation. `409`: `EXECUTION_DISABLED`, `ALREADY_QUEUED`, `NOT_HELD`, `NOT_RELEASABLE`, `PREVIOUSLY_ATTEMPTED`, `CONFLICTING_JOB`, `COMPETING_INTENT`, `SYNTHETIC_EVIDENCE`, `PROVIDER_UNAVAILABLE`, or any revalidation code (`AUTHORIZATION_EXPIRED`, `AUTHORIZATION_REVOKED`, `CONDITIONS_CHANGED`, `VERIFIER_REJECTED`, `COMMITMENT_MISSING`, `PROVIDER_NOT_CONNECTED`, ...) |
| POST | `/payment-intents/{id}/cancel` | `payment:authorize` + step-up, payer only, Idempotency-Key | `approved`: cancelled now, cash released. `session_pending`/`payer_action_pending`: cancellation requested; cash released only after PayPal's cancellation is confirmed by GET. Otherwise `409 CANNOT_CANCEL` |
| POST | `/capacity-evidence` | `payment:authorize` + step-up, Idempotency-Key | Merchant-attested USD balance: `verified_spendable_cents`, `protected_reserve_cents`, `valid_hours` (1-72), `included_receipt_ids`, `included_outflow_ids`. Listing a settled outflow releases exactly that commitment. Refused for synthetic merchants (`409 SYNTHETIC_MERCHANT`); foreign outflow ids `404` |
| GET | `/settlements` | `workspace:read` | Reconciled captures where the merchant is payer or payee, scoped as above |

## Provider webhook (changed from P4)

`POST /v1/provider/paypal/webhook`: `503 VERIFIER_NOT_CONFIGURED` without
`PAYPAL_WEBHOOK_ID`; `400 SIGNATURE_INVALID`; `503 VERIFIER_UNAVAILABLE` if PayPal's
verification could not be asked (PayPal retries); `409 PAYLOAD_CHANGED`; `200 duplicate`;
`202 accepted`. An accepted event enqueues a fetch carrying only `invoice_id` / `capture_id`.
The body never changes state.

## Execution states (`status`)

`approved`, `session_pending`, `payer_action_pending`, `provider_pending`,
`provider_unknown`, `provider_confirmed`, `reconciled`, `failed`, `expired`, `cancelled`,
`exception`. Transitions are enforced by the database (ADR-0012 §4.1).

## Cancellation policy and review codes (gate review, ADR-0012 §16)

`AUTO_CANCEL_UNPAID=false` (default; the initial pilot). An unpaid invoice past its pay-by
time, or one whose approver lost authority after it was sent, stays in
`payer_action_pending` with an open review item in `exceptions`
(`PAY_DEADLINE_PASSED_REVIEW`, `AUTHORIZATION_REVOKED_REVIEW`); no cancel call is made and the
cash stays reserved. Other review codes: `PROVIDER_REQUEST_CONFLICT`, `CAPTURE_MISMATCH`,
`PARTIAL_PAYMENT`, `MARKED_AS_PAID_NOT_PROVIDER_EVIDENCE`, `PAID_TERMS_MISMATCH`,
`DEAD_LETTER`, `REFUND_OBSERVED`.

## Plan view for invoices in execution

`GET /plans/{id}`: a leg whose invoice has a non-terminal intent has `payment_authorization`
of `{"status": "in_execution", "intent_id", "intent_status"}`. The payer also gets
`authorization_id` and, before dispatch, `expires_at`. If the execution-aware
revalidation fails, the status is `in_execution_conditions_changed` with `problems`. No
`authorize` block is offered for such a leg.
