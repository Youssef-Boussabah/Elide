# P4 authenticated workspace API

Design record: **ADR-0011**. These are application endpoints, not PayPal endpoints. All amounts are integer USD cents; times are ISO 8601 with offset.

The P3 demo routes (`/v1/demo/...`, `docs/api/P3_DEMO_API.md`) remain unchanged: unauthenticated, fixture-only and loopback-only for model use. They can never confirm, approve or pay; a contract test enforces this.

## Conventions

- **Session.** The session is an `HttpOnly`, `SameSite=Strict` cookie: `elide_session`, or `__Host-elide_session` with `Secure` when `SESSION_COOKIE_SECURE=true` or in production. No request body or header names a user.
- **CSRF.** Every `POST` needs `X-CSRF-Token` set to the `csrf_token` returned by login or `GET /v1/auth/session`. A browser `Origin`, if sent, must be in `APP_ORIGINS`.
- **Merchant scope.** In `/v1/m/{merchant_id}/...`, the merchant is a selector: the principal must hold an active membership with the route's permission. No membership, an unknown merchant, or an object of another merchant all return the identical `404 {"detail":{"code":"NOT_FOUND"}}`. Lacking the permission returns `403 FORBIDDEN`.
- **Step-up.** Routes marked *step-up* need a password check within 10 minutes, otherwise `401 REAUTH_REQUIRED`. Re-check with `POST /v1/auth/reauth`.
- **Idempotency.** Routes marked *idempotent* need an `Idempotency-Key` header of 8–100 characters from `[A-Za-z0-9_.:-]`. The same key with the same body returns the stored response with `"replayed": true`. The same key with a different body returns `409 IDEMPOTENCY_CONFLICT`. A failed operation is not stored.
- **Errors.** Errors have the shape `{"detail": {"code", "message"}}`. Status codes:

  | Status | Meaning |
  |---|---|
  | 401 | Unauthenticated |
  | 403 | Permission, CSRF or origin |
  | 404 | Not visible |
  | 409 | Stale, conflicting, expired or duplicate |
  | 422 | Invalid request |
  | 429 | Rate limited |

## Authentication `/v1/auth`

| Method | Path | Notes |
|---|---|---|
| POST | `/login` `{login, password}` | Requires an allowed `Origin` if one is sent. Always creates a new session and revokes the old cookie. The same `401 INVALID_CREDENTIALS` is returned for an unknown login or a wrong password. After 5 failures per login or 20 per address in 15 minutes the response is `429`. Returns the user (with `identity_label: "TEST IDENTITY"` for seeded users), the session, `csrf_token` and memberships with permissions |
| GET | `/session` | Same body as login; `401` when the session is missing, expired (30 minutes idle, 8 hours absolute) or revoked |
| POST | `/reauth` `{password}` | Step-up |
| POST | `/logout` | Revokes the session server-side |

## Workspace `/v1/m/{merchant_id}`

Permissions per role are listed in ADR-0011 §4.

| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `/overview` | `workspace:read` | The merchant's own capacity, its invoices and pending counts. Capacity basis is `PROVIDER_VERIFIED`, `MERCHANT_ATTESTED`, `SYNTHETIC`, `UNVERIFIED` or `NONE`, with `observed_at` and `valid_until`. Each invoice has a `payment_state` |
| GET | `/opportunities` | `workspace:read` | **Read-only preview; writes nothing.** Builds the permissioned network from PostgreSQL at request time, runs the P3 analyzer and the P2 planner, and checks the plan with the independent replay verifier. Returns `opportunity`, `plan_preview` (`durable: false`, with no approval actions) and a deterministic template `explanation`. Every field comes from an allowlist after an explicit disclosure decision (see *Disclosure*). Without a confirmed policy, the merchant gets the `POLICY_NOT_CONFIRMED` flag |
| GET | `/network` | `workspace:read` | Only edges this merchant is a party to, and only its direct counterparties |
| GET | `/invoices/{obligation_id}` | `workspace:read` | Parties only; includes the immutable revision history |
| GET | `/offers` | `workspace:read` | Offers on this merchant's invoices, with `can_decide`, `can_withdraw` and the decisions recorded per merchant |
| POST | `/offers` | `offer:propose` and `offer:decide`; step-up; idempotent | `{obligation_id, concession_cents, pay_by, expires_at, message?, source}`. Signs the merchant's own offer. Requirements: one open offer per invoice, `expires_at ≤ pay_by ≤ current due date` (no credit extension), expiry within 14 days, and the message may mention only this offer's amounts |
| POST | `/offers/{offer_id}/decision` | `offer:decide`; step-up; idempotent | `{decision: accept\|reject\|counter\|withdraw, terms_digest, counter?}`. Accept, reject and counter are counterparty-only (otherwise `NOT_COUNTERPARTY`); withdraw is proposer-only. Rejection codes: `STALE_DIGEST`, `OFFER_EXPIRED`, `STALE_REVISION`, `OFFER_CLOSED` and `ALREADY_DECIDED`. Acceptance writes the AUTHORIZED revision signed by both parties. `payment_authorized` is always false |
| POST | `/offers/draft` | `offer:propose` | `{obligation_id, max_concession_cents}`, payer only. Returns deterministic bounds from the P3 counteroffer engine; there is no model call unless a workspace model is explicitly configured. Creates nothing |
| POST | `/plans` `{network?}` | `payment:authorize`; CSRF; `Idempotency-Key` required | Explicitly creates a durable, independently verified plan snapshot for review and approval. If an unexpired snapshot with the same **economic digest** (inputs without the clock) exists, it is reused (`reused: true`) and nothing is written. At most 30 new snapshots per merchant per hour (`429 RATE_LIMITED`). Returns the allowlisted durable plan view |
| GET | `/plans` | `workspace:read` | The 10 newest durable plans this merchant initiated |
| GET | `/plans/{plan_id}` | `workspace:read` | Participants or the initiator only. Allowlisted legs with each leg's **current** authorization status: `pending`, `approved`, `declined`, `expired` or `needs_reapproval`. A payee sees only `approved` or `pending`. The `authorize` binding includes `plan_current` (the economic inputs are unchanged) |
| POST | `/plans/{plan_id}/authorizations` | `payment:authorize`; step-up; idempotent | `{leg_id, decision: approve\|decline, binding_digest}`, payer only (otherwise `NOT_PAYER`). The actor's membership is re-checked under a row lock inside the writing transaction. The plan's economic inputs must be current (otherwise `STALE_PLAN`). The independent verifier replays the leg with its upstream legs on current data (otherwise `VERIFIER_REJECTED`); `TERMS_NOT_AGREED` is returned for an unsigned offer or proposal. The **material conditions** are stored with the decision (see *Authorization reuse*). `money_moved` is always false |
| POST | `/payment-intents` | `payment:authorize`; step-up; `Idempotency-Key` required | `{authorization_id}`. One transaction, with the actor's and the approver's memberships share-locked. Refusals, in order: `AUTHORIZATION_EXPIRED`, `AUTHORIZATION_REVOKED`, `DUPLICATE`, `ALREADY_IN_EXECUTION` (per-invoice lock); `UPSTREAM_NOT_CONFIRMED`, `TERMS_NOT_AGREED`; `CONDITIONS_CHANGED`, when the current material conditions differ from the approved ones; `VERIFIER_REJECTED`, from the independent verifier on current data; `INSUFFICIENT_CAPACITY`, from the atomic reservation, which uses max(evidence reserve, confirmed policy floor); `SYNTHETIC_EVIDENCE` in production. Writes the intent (`awaiting_payer_action`, with `evidence_kind`) and a HELD outbox message. A synthetic intent can never leave that state; the database enforces it on both tables. **No PayPal call is made** |
| GET | `/payment-intents` | `workspace:read` | |
| GET | `/policy` | `workspace:read` | The confirmed revision plus recent drafts (never active) |
| POST | `/policy/drafts` | `policy:edit` | `{text, source, extraction?}`. With `extraction`, deterministic P3 validation of merchant-entered fields. Without it, a model draft (`MODEL_UNAVAILABLE` unless a workspace model is configured) |
| POST | `/policy/drafts/{draft_id}/confirm` | `policy:edit`; step-up | `{digest}`. Requires a READY draft and the exact digest; otherwise `STALE_DIGEST` or `DRAFT_NOT_READY` |
| GET | `/audit` | `audit:read` | This merchant's events only; hash-chained and append-only |
| GET | `/members` | `members:manage` | |
| POST | `/members/{user_id}/revoke` | `members:manage`; step-up | `{reason}`. Effective immediately for every session of that user |

## Disclosure (server-side policy, `api/workflow/disclosure.py`)

Network-level information (totals, baseline comparisons, counts of legs, approvals,
bottlenecks or participants, and recommendation wording derived from other merchants'
data) is added to a view only when all three hold:
- the viewer is the initiating merchant of that analysis or plan;
- **every** other active participant has `share_aggregates = true`;
- at least **3** other active participants exist.

Otherwise the view carries `aggregates.status`, one of `WITHHELD_NOT_INITIATOR`,
`WITHHELD_NO_CONSENT` or `WITHHELD_SMALL_GROUP`, and a recommendation computed from the
merchant's own data only. Views are built from allowlisted fields, never by deleting
fields from the privileged report. Model prompts (when a workspace model is configured)
are built from the same views.

## Authorization reuse

A payment decision applies only to the identical leg terms: option, invoice revision,
payer, payee, amount and fee ceiling. It also requires identical **material conditions**,
and it must be unexpired. The material conditions are:
- the in-force revision's stored content hash, currency, due date and outstanding balance;
- the payee, and both parties' participation;
- the fee terms;
- the payer's confirmed policy version and digest, and the effective reserve
  max(evidence reserve, policy floor);
- the fee cap and any prohibited counterparty;
- the evidence basis.

Cash levels and other commitments are re-checked by capacity reservation and the verifier
at intent time. A decision recorded before migration 0005 never matches and needs
re-approval.

## Provider webhook

`POST /v1/provider/paypal/webhook` validates the PayPal envelope and transmission headers.

| Response | Meaning |
|---|---|
| `422 MALFORMED` | Bad envelope or missing transmission headers |
| `503 VERIFIER_NOT_CONFIGURED` | The P4 default; nothing is recorded |
| `400 SIGNATURE_INVALID` | Verifier rejected the signature |
| `202` | First delivery: one inbox row and a HELD `provider_fetch` outbox message |
| `200 duplicate` | Same event delivered again |
| `409 PAYLOAD_CHANGED` | Same event id with a different payload |

A webhook body never changes financial state; P5 fetches provider truth by GET.

## Labels the UI must keep distinct

`SIMULATED`, `PROPOSED`, `APPROVED`, `INTENT_CREATED_NOT_SENT`, `PAYPAL_CONFIRMED`, `FUNDS_USABLE_ATTESTED`, `BLOCKED`.

No P4 code path can reach `PAYPAL_CONFIRMED` or `FUNDS_USABLE_ATTESTED`.

## Local setup

```powershell
docker compose up -d --wait db; uv run alembic upgrade head
uv run python scripts/seed_workspace.py      # TEST identities -> .local/demo_credentials.json (git-ignored)
uv run uvicorn api.app.main:app --port 8000
cd web; pnpm dev                              # http://127.0.0.1:5173
uv run python scripts/seed_workspace.py --refresh   # new synthetic evidence after 7 days
```
