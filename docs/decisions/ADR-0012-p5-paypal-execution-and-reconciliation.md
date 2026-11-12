# ADR-0012 — P5 PayPal execution, durable outbox and reconciliation

- **Status:** accepted for implementation (2026-11-11). Pending owner review.
- **Phase:** P5, branch `feat/p5-paypal-execution` from `main` at `68b2268` (P4 merged as
  `1ed309a`, CI run #22 green).
- **Sources read:** CLAUDE.md/AGENTS.md; project sources 00, 03, 08, 09, 10, 11, 12, 14, 16;
  ADR-0005, ADR-0007, ADR-0008, ADR-0011; the P0 Sandbox evidence runs; the PayPal Invoicing
  v2 OpenAPI document (`developer.paypal.com/api/invoicing/v2/schema.json`, fetched
  2026-11-11); the PayPal webhooks guide (`developer.paypal.com/api/rest/webhooks/rest/`) and
  event-name list.
- **Supersedes:** nothing. ADR-0011 §7 ("payment execution boundary") is implemented here.

## 1. Context

P4 ends at a durable, condition-bound `payment_intent` whose cash is reserved and whose
`outbox_message` stays `held`. P5 connects that intent to a real, supported PayPal Sandbox
operation and reconciles the provider outcome into the P1 journal, without weakening D003
(private acceptance), D004 (no model authority), D005 (verifier before execution), D006
(payment-confirmed ≠ spendable) or D008 (USD only).

## 2. What the provider actually permits (verified facts, not assumptions)

| Fact | Source | Class |
|---|---|---|
| The `.env` app credentials belong to **one** Sandbox Business account ("Test Store"), country CA, which receives USD invoice payments and settles them in **CAD** | P0 runs `20261110T221827Z` (capture: fee USD 29.01, receivable CAD 1,306.79) | Sandbox-verified |
| Invoicing v2 with these credentials creates, sends and reads invoices **owned by that account**: it is always the invoicer (payee) | P0 runs; Invoicing v2 schema (no "on behalf of" field) | Sandbox-verified |
| Creating invoices for other merchants needs select-partner onboarding (`INVOICE_READ_WRITE`, `ACCESS_MERCHANT_INFORMATION`) that this project does not have | doc 03, doc 10 | Specified (access boundary) |
| The payer acts in PayPal's hosted page; Elide cannot pay, capture or approve for the payer | Invoicing v2 has no "pay" operation; P0 payment was an owner browser action | Sandbox-verified |
| `GET /v2/invoicing/invoices/{id}` returns `detail.metadata.recipient_view_url` | P0 runs (fingerprinted) | Sandbox-verified |
| Endpoints: create, send, cancel (`POST .../cancel`, 204), show, `POST /v2/invoicing/search-invoices` (filter `invoice_number`) | Invoicing v2 schema | Provider documentation |
| `PayPal-Request-Id` is defined in the schema's components but **not attached** to create or send; invoice-number uniqueness is **not documented** | Invoicing v2 schema | Provider documentation (absence) |
| Invoice status enum: DRAFT, SENT, SCHEDULED, PAID, MARKED_AS_PAID, CANCELLED, REFUNDED, PARTIALLY_PAID, PARTIALLY_REFUNDED, MARKED_AS_REFUNDED, UNPAID, PAYMENT_PENDING, AUTO_CANCELLED, PAID_EXTERNAL, REFUNDED_EXTERNAL, SHARED | Invoicing v2 schema | Provider documentation |
| `configuration.partial_payment.allow_partial_payment`, `configuration.allow_tip` exist | Invoicing v2 schema | Provider documentation |
| `INVOICING.INVOICE.PAID` fires for **full, partial or pending** payments | webhook event-name list | Provider documentation |
| Webhook postback: `POST /v1/notifications/verify-webhook-signature` with `transmission_id`, `transmission_time`, `cert_url`, `auth_algo`, `transmission_sig`, `webhook_id`, `webhook_event`; the event "must be posted back exactly as it was received"; simulator (mock) events **cannot** be verified by postback; failed deliveries are retried up to 25 times over 3 days | webhooks guide | Provider documentation |
| A registered webhook needs a public HTTPS URL; this project has none, and `PAYPAL_WEBHOOK_ID` is empty | `.env.example`, local setup | Observed |

### 2.1 Mapping provider identity to the obligation model

An Elide obligation has a **debtor** (payer merchant) and a **creditor** (payee merchant).
With first-party Invoicing v2 the provider operation is a *payment request issued by the
creditor's PayPal account*, which the debtor then pays in PayPal's hosted page. Therefore:

| Elide concept | PayPal Sandbox object | Binding |
|---|---|---|
| Creditor (payee) merchant | The Business account that owns the app credentials (the **invoicer**) | `provider_account` row, role `invoicer`, `credential_ref = env:PAYPAL_CLIENT_ID`, fingerprint of the provider merchant id. At most **one** active invoicer binding exists per credential |
| Debtor (payer) merchant | A Sandbox Personal account that pays in the hosted page | `provider_account` row, role `payer`, `credential_ref = env:PAYPAL_SANDBOX_PAYER_EMAIL` (the address is never stored; a fingerprint is) |
| Agreed obligation revision (cash `A`, concession `C`) | One PayPal invoice: one line of `A + C`, invoice discount `C`, total `A`, USD, partial payment and tips disabled, deterministic `invoice_number` | `payment_session` row (1:1 with the intent) |
| Payer approval (P4 `payment_authorization`) | Nothing at PayPal. The payer must still log in and pay; PayPal records that action | — |
| Payment | The invoice transaction + the capture it references | Fetched by GET |

Consequences, enforced in code:
- A leg is **executable only if its payee holds an active invoicer binding** whose fingerprint
  matches the configured credentials, and its payer holds an active payer binding. Every other
  leg is `PROVIDER_NOT_CONNECTED`: it stays approved but held, and the UI says why. This is an
  access boundary, not something to simulate.
- One developer-owned account does **not** give Elide authority over arbitrary merchants. The
  bindings are created by an operator script, run by the owner, for Sandbox test identities
  only (`scripts/bind_sandbox_accounts.py`), with the owner's recorded collection consent for
  the invoicer.
- The creditor's consent to *collect* through PayPal is the binding's consent plus its own
  signature on the AUTHORIZED revision (P4). The debtor's consent to *pay* is its P4
  authorization plus its own action at PayPal. The worker issues a payment request; it never
  pays and never impersonates payer approval.
- Anyone holding the hosted link could pay the invoice. The provider resources do not prove
  which PayPal account paid. Elide records the payment against the invoice and states this
  limitation; it never claims payer identity it did not verify.
- The payee settles in CAD. The reconciled receipt is CAD and **never** USD capacity (D008),
  and it is provider-confirmed, not spendable (D006), until separate availability evidence.

## 3. Decision overview

```
 approve (P4) ─► create intent (P4, reserve cash, outbox msg)
                       │  PAYMENTS_EXECUTION_ENABLED, not synthetic, bindings active
                       ▼
 worker: claim (SKIP LOCKED lease + fencing token)
   ├─ revalidate in DB (authority, conditions, verifier, commitment, bindings, kill switch)
   ├─ provider_operation row 'started'  ─► PayPal call (no DB lock held) ─► record result
   ├─ create DRAFT ─► GET verify ─► revalidate ─► send ─► GET verify (SENT)
   └─ schedule reconcile polling
 payer opens hosted page (link fetched on demand by GET, never stored) and pays at PayPal
 webhook (optional accelerator): verify signature ─► inbox dedupe ─► provider_fetch message
 reconcile: GET invoice ─► GET capture ─► strict match ─► ONE transaction:
   settlement events + journal bundle + settlement record + intent 'reconciled'
   + payer commitment becomes 'settled_outflow' (released only by newer balance evidence)
 independent checker re-derives journal / settlement identity from raw rows
```

## 4. State machines

### 4.1 Payment intent (`payment_intent.status`)

| State | Meaning | Cash reservation |
|---|---|---|
| `approved` | Approved by the payer and financially revalidated; cash reserved; no provider session yet. Held while synthetic, not bound, or the kill switch is off. (P4 called this `awaiting_payer_action`; renamed by migration `0006`) | reserved |
| `session_pending` | The worker revalidated and is creating/verifying the PayPal invoice (draft exists or is being created; not yet payable) | reserved |
| `payer_action_pending` | Invoice SENT and verified by GET. Only the payer's action in PayPal's hosted page can move money | reserved |
| `provider_pending` | Provider shows a payment initiated but not complete (`PAYMENT_PENDING`, or capture `PENDING`) | reserved |
| `provider_unknown` | The outcome of a provider-mutating call is unknown (timeout, connection reset, 5xx, crash). Lookup required; never a blind retry, never assumed failed | reserved |
| `provider_confirmed` | Provider GETs show PAID with exactly the expected terms; booking not yet committed | reserved |
| `reconciled` | Settlement booked atomically and independently checked. Terminal success | converted to `settled_outflow` |
| `failed` | The provider definitively did not create a payable request (e.g. 4xx on create, verified absent) | released |
| `expired` | The payer did not pay by the session deadline; provider cancellation confirmed by GET | released |
| `cancelled` | Withdrawn before payment (authority revoked, conditions changed, operator); if an invoice was sent, its provider cancellation is confirmed by GET first | released |
| `exception` | Operator review: amount/currency/payee mismatch, partial payment, more than one transaction, refund before booking, MARKED_AS_PAID, retries exhausted with money state unknown | **kept** |

Allowed transitions (enforced by a PostgreSQL trigger; anything else is refused):

```
approved            -> session_pending | cancelled
session_pending     -> payer_action_pending | provider_unknown | failed | cancelled
provider_unknown    -> session_pending | payer_action_pending | provider_pending
                       | provider_confirmed | failed | cancelled | expired | exception
payer_action_pending-> provider_pending | provider_confirmed | provider_unknown
                       | expired | cancelled | exception
provider_pending    -> provider_confirmed | payer_action_pending | provider_unknown | exception
provider_confirmed  -> reconciled | exception
reconciled, failed, expired, cancelled, exception -> (none)
```

- Late or duplicate observations never regress state: `reconciled` has no exit, and a late
  `PAYMENT_PENDING` after `provider_confirmed` is ignored and recorded.
- A synthetic intent stays in `approved` / `cancelled` (P4 CHECK kept, value renamed).
- A refund or reversal after `reconciled` does not change the intent (history is not
  rewritten): it creates a `payment_exception` row and compensating postings (§7.4).

### 4.2 Funds after payment (receipt side, D006)

`provider_confirmed` (payee receipt recorded, zero capacity) → `available_verified` only through
P1 `mark_available` with separate `AvailabilityEvidence` or a newer balance observation that
lists the receipt in `included_receipt_ids`. A CAD credit is `CURRENCY_BLOCKED` regardless.
Nothing in P5 turns a PayPal `PAID`/`COMPLETED` into spendable cash.

### 4.3 Outbox message

```
held ──(execution enabled, eligible)──► pending ──claim──► leased ──► done
                                          ▲                 │  ├─► pending (retry with backoff, attempts+1)
                                          └─ lease expired ─┘  ├─► failed (permanent, handled outcome)
                                                               └─► dead  (retries exhausted; operator review)
```

- **Claim:** `SELECT … FOR UPDATE SKIP LOCKED` over due `pending` messages and `leased`
  messages whose lease expired (crash recovery), then `status='leased'`, `lease_owner`,
  `lease_until = now + lease`, `lease_token = lease_token + 1`. Committed before any work.
- **Fencing:** before each provider-mutating call the worker re-asserts
  `lease_owner/lease_token` in a short transaction; a worker whose lease was taken over stops
  without calling the provider.
- **Backoff:** `delay = min(base · 2^(attempt−1), cap)` plus deterministic jitter from the
  message id; defaults base 30 s, cap 30 min, max 8 attempts.
- **Kinds:** `create_payment_session`, `reconcile_payment` (polling, re-armed), `provider_fetch`
  (webhook-triggered), `cancel_payment_session`.
- **Kill switch:** with `PAYMENTS_EXECUTION_ENABLED=false` the worker never creates, sends or
  cancels anything, but still runs read-only reconciliation (doc 08, doc 11 incident rule).

### 4.4 Provider operation (`provider_operation`)

One row per provider call, written **before** the call: `started` → `succeeded` | `failed` |
`unknown`. A partial unique index allows at most one `started` mutating operation per
session: it is the per-payment mutex that never holds a database lock during network I/O. A
`started` row found after a crash is treated as `unknown`.

### 4.5 Capacity commitment lifecycle

| Event | Effect on the payer's commitment |
|---|---|
| Intent created (P4) | `reservation` of amount + fee ceiling |
| Session created, sent, pending, unknown, exception | unchanged (reserved) |
| Reconciled | in the same transaction: reservation released with reason `settled`, and a `settled_outflow` commitment of the **actual** outflow inserted. The cash left the account, but the latest balance evidence predates that |
| Newer balance evidence (observed after the confirmation) that lists the payment in `included_outflow_ids` | `settled_outflow` released with reason `reflected_in_evidence:<evidence_id>`. A snapshot that merely changed, or one that does not list the outflow, releases nothing |
| Failed / expired / cancelled (provider absence or cancellation confirmed by GET) | reservation released once, with the reason |
| Unknown / exception / dead letter | never released automatically |

Releases are once-only (P1). The payer pays no PayPal fee on an invoice payment; the fee is
the payee's and is taken from the provider's capture breakdown, never assumed.

## 5. Revalidation before every provider mutation

In one short transaction (no network), before **create** and again before **send**:
1. the outbox lease is still ours (fencing token);
2. the intent is in the expected state; the session's expected terms equal the intent;
3. the payer authorization is unexpired; its approving user still holds `payment:authorize`
   (membership share-locked, as in P4);
4. the payment conditions recomputed now equal the approved `conditions_digest`;
5. the independent P2 verifier replays the leg on current data, with this intent's own
   reservation excluded and its invoice included (`load_network(..., executing_intent=…)`);
6. the commitment is still active and covers amount + fee ceiling;
7. both provider bindings are active, the invoicer binding fingerprint equals the configured
   credential fingerprint (wrong-payee guard), and the environment is Sandbox;
8. execution is enabled and the intent is not synthetic.

A failure before anything was created → `cancelled` (reason code), cash released. A failure
after a draft exists → the draft is left unsent (a draft is not payable), `cancelled`. A
failure after send → a `cancel_payment_session` message; the intent stays reserved until the
provider cancellation is confirmed by GET.

## 6. Idempotency without undocumented guarantees

- Each session has a deterministic `invoice_number` = `ELIDE-` + 12 hex of
  SHA-256(intent id) and `detail.reference` = the session id. `PayPal-Request-Id` is still sent
  (harmless), but correctness does not depend on it.
- **Unknown create:** search by `invoice_number`. Exactly one match whose terms equal the
  session is adopted; more than one → `exception`. No match → search again with backoff; only
  then re-create with the same number. **Why this is safe:** a DRAFT cannot be paid, so a late
  duplicate draft cannot take money; it is detected by the next search and reported. Only
  **send** makes an invoice payable, and send is always preceded by a GET of that exact
  invoice id.
- **Unknown send:** GET the invoice. SENT/UNPAID/PAID… → treat as sent; DRAFT → send again;
  anything else → `exception`.
- **Unknown cancel:** GET; CANCELLED → done; otherwise retry; PAID → reconcile instead.
- Response classes: 2xx success; 401 refresh once; 403 access boundary (permanent, recorded);
  400/404/422 permanent **only if** the call is a create that the provider rejected before
  creating anything; 429 and connect errors (request never sent) retry; 5xx, read timeouts and
  resets on a mutating call → `unknown` → lookup.

## 7. Reconciliation

### 7.1 Webhooks are notifications only

`POST /v1/provider/paypal/webhook`: size and header checks; the `PayPalWebhookVerifier` posts
the original body **verbatim** inside the verify request with the configured `webhook_id`;
`verification_status == "SUCCESS"` required. `cert_url` must be HTTPS on a PayPal Sandbox
host; transmission time may not be in the future beyond 5 minutes. Without `PAYPAL_WEBHOOK_ID`
the endpoint stays `503`. Accepted events are deduplicated by (provider, environment, event
id) in the P1 inbox; a redelivery with a different payload is `409`. The resource identity
(invoice id from `resource.id`/`resource.invoice.id`, or a capture's `invoice_id`) goes into a
`provider_fetch` message. Unknown resources are recorded as unmatched, never acted on.

Because no public endpoint or registered webhook exists, **polling is the primary path**:
a `reconcile_payment` message is re-armed while a session is live. Webhooks only make it
faster. Reconciliation is correct with zero webhooks.

### 7.2 Provider truth and matching

`GET` invoice → classify:

| Provider observation | Result |
|---|---|
| DRAFT/SENT/UNPAID/SCHEDULED, no transactions, before deadline | unchanged; poll again |
| same, after deadline | `cancel_payment_session` → GET CANCELLED → `expired`, release |
| PAYMENT_PENDING | `provider_pending` |
| PAID: currency USD, total = `A`, item total = `A + C`, discount = `C`, paid = `A`, due 0, exactly one SUCCESS transaction of `A` | GET capture: COMPLETED, `invoice_id` matches, gross USD `A`, payee merchant fingerprint = binding → `provider_confirmed` → book (§7.3) |
| PARTIALLY_PAID, >1 transaction, amount/currency/payee mismatch | `exception` (no posting; reservation kept) |
| MARKED_AS_PAID / PAID_EXTERNAL | `exception`: a merchant-recorded payment is not provider payment evidence |
| CANCELLED/AUTO_CANCELLED with no transaction | `cancelled`/`expired`, release |
| REFUNDED/PARTIALLY_REFUNDED before booking | `exception` |

### 7.3 Atomic booking (one transaction)

1. intent `provider_confirmed → reconciled` (compare-and-set under row lock);
2. `obligation_settlement_event` CASH_APPLIED `A` (append-only; P1 `InvoiceRecord.apply`
   re-checks over-settlement and the identity `face = cash_applied + concession + remaining`);
3. payee journal bundle via the existing `settlement_entries` (invoice issued, concession,
   capture settlement with fee and FX), and a payer entry (`provider_payment_sent`:
   Dr `accounts_payable:<obligation>` `A + C` / Cr `paypal_payments_sent` `A` / Cr
   `discount_received` `C`), all through `post_entries_in(conn, …)`;
4. `provider_settlement` record (capture id unique, intent id unique, gross, fee, net, credit,
   rate, provider timestamps, bundle digest);
5. commitment conversion (§4.5); audit events.

Then `reconciliation_check` — coded separately from the posting code — reads raw journal rows
and settlement events and verifies: the invoice receivable nets to zero; capture gross = `A`;
gross − fee = net; credit = rate·net (rounded half-up) or = net; per-currency balance of every
entry is zero; exactly one settlement entry per capture; the obligation identity holds; and the
payer's AP for the obligation nets to zero. Any failure → `payment_exception` row and an
operator-visible incident (the booking is never mutated; corrections are compensating
entries).

### 7.4 Refunds and reversals after booking

A refund observed on a reconciled payment (invoice `REFUNDED`/`PARTIALLY_REFUNDED`, capture
`REFUNDED`/`PARTIALLY_REFUNDED`) creates a `payment_exception` (`REFUND_OBSERVED`) and fetches
each refund resource by GET. Posting refunds automatically is **not** enabled in P5: PayPal's
refund fee treatment must be observed on a real Sandbox refund first (a provider mutation that
needs separate owner authorization). Until then the operator sees the exception, the original
booking stays, and no capacity is created for the payer.

## 8. Hosted payer link

`GET /v1/m/{m}/payment-intents/{id}/payer-link` (payer merchant, `payment:authorize`, recent
step-up): fetches the invoice by GET, checks state and terms again, and returns
`detail.metadata.recipient_view_url` only if it is on `https://www.sandbox.paypal.com/`.
Response `Cache-Control: no-store`. The URL is never stored, logged, audited or put in evidence;
the audit records only that the link was viewed.

## 9. API contract additions

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/v1/m/{m}/payment-intents/{id}` | payer or payee member (`read`) | Intent, session (no link), state history, reasons, provider-confirmed vs spendable labels |
| GET | `/v1/m/{m}/payment-intents/{id}/payer-link` | payer, `payment:authorize`, step-up | On-demand hosted link (§8) |
| POST | `/v1/m/{m}/payment-intents/{id}/cancel` | payer, `payment:authorize`, step-up, Idempotency-Key | Cancel before payment (provider cancellation first if sent) |
| POST | `/v1/m/{m}/capacity-evidence` | owner/approver, step-up, Idempotency-Key | Merchant-attested USD balance with `included_receipt_ids` and `included_outflow_ids` |
| GET | `/v1/m/{m}/settlements` | member | Reconciled settlements of this merchant, receipt state (D006/D008 reasons) |
| POST | `/v1/provider/paypal/webhook` | PayPal signature | §7.1 |

Errors keep the P4 shape. Cross-tenant access stays an identical `404`.

## 10. Invariants (each has a test)

- I1 Money is integer cents; the PayPal boundary uses `money.py` only.
- I2 No provider-mutating call without a committed `provider_operation` row and a successful
  revalidation in the same worker step.
- I3 No database lock is held during a provider call.
- I4 At most one live session and one `started` mutating operation per intent.
- I5 Intent transitions follow §4.1 (DB trigger); `reconciled` is final.
- I6 One capture → at most one settlement record, one settlement entry, one CASH_APPLIED event.
- I7 Every booking satisfies the settlement identity and balances per currency; the independent
  checker agrees.
- I8 Provider-confirmed never adds USD capacity; a CAD credit never does.
- I9 A commitment is released only for a confirmed absence of payment or when newer evidence
  includes the outflow; unknown/exception never releases.
- I10 Webhook bodies never change financial state; only GETs do.
- I11 The LLM, planner and browser have no path to the worker or provider adapter
  (`api/ai` and `api/planning` cannot import `api/integrations` or `api/execution`).
- I12 No credentials, payer links or payer emails in logs, audit, evidence or responses other
  than the on-demand link response.
- I13 Synthetic intents never dispatch; production refuses mock providers.

## 11. Acceptance matrix

| # | Requirement | Test level | Evidence class |
|---|---|---|---|
| A1 | State machine and trigger: allowed/forbidden transitions | unit + PostgreSQL | Synthetic-tested |
| A2 | Revalidation refuses: revoked approver, changed policy/reserve, changed terms, stale verifier, expired authorization, unbound payee, wrong credential fingerprint, kill switch | PostgreSQL integration | Synthetic-tested |
| A3 | Worker: SKIP LOCKED claim, two concurrent workers, lease expiry takeover, fencing, backoff, dead letter | PostgreSQL concurrency | Synthetic-tested |
| A4 | Crash after create/send before recording → lookup, no duplicate payable invoice | fault injection, mock provider | Synthetic-tested |
| A5 | Timeout on create/send/cancel → `provider_unknown`, never `failed` | fault injection | Synthetic-tested |
| A6 | Webhook: forged signature, wrong environment cert host, missing webhook id, duplicate, changed payload, reordered, delayed, unknown resource | contract + PostgreSQL | Synthetic-tested |
| A7 | Reconciliation: exact match books once; amount/currency/payee/partial/multiple/MARKED_AS_PAID → exception, nothing booked | PostgreSQL integration | Synthetic-tested |
| A8 | Independent checker agrees with booking; detects a tampered bundle | unit + PostgreSQL | Synthetic-tested |
| A9 | Commitment lifecycle (§4.5) incl. new evidence with/without the outflow | PostgreSQL | Synthetic-tested |
| A10 | Cross-tenant: other merchants cannot see intents, links, settlements; payee sees no payer reserve | PostgreSQL over HTTP | Synthetic-tested |
| A11 | UI: execution states, payer link action, provider-confirmed vs spendable labels | jsdom + Playwright (mock provider) | Synthetic-tested |
| A12 | Adapter request/response shapes against PayPal's schema; live read-only GETs of the P0 invoice/capture through the P5 reconciler | contract + read-only Sandbox | Sandbox-verified (read only) |
| A13 | One new Sandbox invoice created, sent, paid by the Sandbox payer, reconciled, duplicate replay posts once | owner-authorized operation | Sandbox-verified — **needs owner authorization** |
| A14 | Verified webhook delivery | needs a public HTTPS endpoint and a registered webhook | **Blocked** (owner decision) |

## 12. Alternatives considered

- **Orders v2 checkout instead of Invoicing:** same payee constraint (the app's own account)
  and no invoice identity to reconcile against the obligation. Kept as a documented fallback.
- **Store the payer-view link encrypted:** rejected; fetching it on demand avoids storing a
  bearer link at all.
- **Rely on `PayPal-Request-Id` for create/send:** rejected; not documented for these endpoints.
- **Treat timeouts as failures and retry:** rejected (doc 10: never blind-retry).
- **Release the reservation when the next balance snapshot arrives:** rejected; a snapshot can
  predate or omit the outflow. Release needs explicit inclusion.
- **Celery/Redis/Kafka worker:** rejected (CLAUDE.md §4); PostgreSQL `SKIP LOCKED` is enough.
- **Auto-post refunds as exact reversals:** rejected; PayPal's refund fee handling differs, so
  an exact negation would be wrong. Deferred until a real refund is observed.

## 13. Finance and privacy impact

- No new money semantics: P1 settlement identity, journal and capacity rules apply unchanged.
- New personal data: none stored. Payer email stays in `.env`; DB holds fingerprints and
  secret *references*. The hosted link is never persisted.
- Payees see the payment and their own receipt; they never see the payer's reserve or other
  invoices (P4 disclosure rules unchanged).

## 14. Test changes

New: `tests/unit/execution/*`, `tests/contract/test_paypal_execution_adapter.py`,
`tests/integration/test_p5_*.py`, frontend and Playwright tests for execution states. The P4
assertion that an intent starts in `awaiting_payer_action` changes to `approved` (migration
`0006` renames the value; no other P4 regression changes).
