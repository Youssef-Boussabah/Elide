# 09 — Data Model, API Contracts, Typed Boundaries

## Data invariants
Use integer currency minor units, ISO 4217 currency codes, UTC instants plus original business timezone, opaque IDs and immutable revisions. Scope **every** resource by tenant and merchant authorization. Add version, source evidence and effective times to any mutable business fact. Never use floats for dollars or trust a UI-supplied `merchant_id` without membership validation.

## Suggested persistence entities
| Table/entity | Essential fields / uniqueness |
|---|---|
| `merchant`, `membership` | tenant, authenticated principal, scoped roles, revocation |
| `obligation` | stable original ID, debtor, creditor, currency, face/open cents, source/provider identity |
| `obligation_revision` | revision, due timestamp, dispute, open balance, source digest, created_at |
| `offer` | invoice revision, proposed payment/concession cents, limits, signed terms, expiry |
| `capacity_evidence` | merchant, spendable/held/reserve/committed cents, provenance, observed_at, expires_at |
| `policy_revision` | creator, typed rules, original text, evidence spans, confirmation status |
| `plan` | snapshot hash, objective, baseline, solver, assumptions, expiry, status |
| `plan_leg` | payer/payee, invoice revision, amount, fee ceiling, dependencies, status |
| `decision` | actor, role, digest, accept/decline, signed_at, time validity |
| `payment_intent` | leg, operation key, request hash, status, provider correlation |
| `outbox`, `inbox` | durable operation/events, lease, retry count, unique provider event ID |
| `receipt` | provider resource ID, environment/app scope, amount, state, fetched_at |
| `journal_entry`, `posting` | balanced immutable per-currency debit/credit and reversal reference |

## Application endpoints (proposed, not PayPal endpoints)
| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/invoices/import` | Import a scope-approved invoice record, return revision/provenance |
| POST | `/v1/policies/interpret` | Obtain model-suggested typed rules + quoted evidence |
| POST | `/v1/policies/{id}/confirm` | Owner confirms policy fields and digest |
| POST | `/v1/plans` | Compute plan for specific snapshot/horizon, return asynchronous job or plan |
| GET | `/v1/plans/{id}` | Read permissioned plan, private benefit and safe aggregates |
| POST | `/v1/plans/{id}/decisions` | Version-bound approval/refusal |
| POST | `/v1/legs/{id}/payment-session` | Create/return approved payer-hosted action or current status |
| POST | `/v1/provider/paypal/webhook` | Receive PayPal event, verify + persist; no blind trust |
| GET | `/v1/runs/{id}/reconciliation` | Per-invoice audit and exceptions (tenant-scoped) |

## Request integrity
Mutations require a client operation key. Replaying the key with identical body returns the stored result; different request body → HTTP 409. Stale plan digest/terms/availability → HTTP 409 with fresh-action requirement. Unresolved currency/date or invalid state → 422. Inaccessible resources should not reveal cross-tenant existence. All payment initiations require recent role/authorization checks and amount/counterparty re-confirmation.

## Example plan contract
```json
{
  "plan_id": "plan_demo_01",
  "currency": "USD",
  "status": "awaiting_consent",
  "snapshot_digest": "demo-snapshot-sha256-placeholder",
  "solver": {"name": "exact_reference", "status": "OPTIMAL", "mode": "synthetic"},
  "buyer_required_cents": 99000,
  "buyer_reserve_cents": 50000,
  "projected_gross_flow_cents": 269000,
  "projected_downstream_face_cents": 170000,
  "realized_incremental_face_cents": null,
  "availability_model": "synthetic_zero_lag",
  "legs": [
    {"id":"leg_food", "invoice_id":"food_001", "payer":"buyer", "payee":"food_supplier", "payment_cents":99000, "status":"proposed", "approval_required":true},
    {"id":"leg_freight", "invoice_id":"freight_001", "payer":"food_supplier", "payee":"carrier", "payment_cents":90000, "status":"conditional", "depends_on":["leg_food"]},
    {"id":"leg_repair", "invoice_id":"repair_001", "payer":"carrier", "payee":"mechanic", "payment_cents":80000, "status":"conditional", "depends_on":["leg_freight"]}
  ],
  "warnings": ["SYNTHETIC_NETWORK", "DOWNSTREAM_NOT_PROVIDER_VERIFIED"]
}
```

## Contract tests
Schema validation, wrong-tenant request, reused idempotency key with changed body, stale policy digest, expired offer, mismatched currency, disputed invoice, duplicate provider event, partial invoice payment, available-vs-held balance discrepancy, and manual payer payment outside expected path. Always test with simple CLI requests and synthetic fixtures independent of UI.
