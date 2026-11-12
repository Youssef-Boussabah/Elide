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
| *Corrected 2026-11-12 (§16):* PayPal's Invoicing guide documents `PayPal-Request-Id` for retries ("returns the original result" without a duplicate invoice) and that invoice numbers "must be unique"; the endpoint schema does not attach the header to create/send/cancel. The original entry said both were undocumented, which was wrong | Invoicing Core concepts and REST request guide (2026-10-02); Invoicing v2 schema (modified 2026-11-09) | Provider documentation (discrepancy) |
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

## 6. Idempotency without relying on provider guarantees (corrected in §16)

- Each session has a deterministic `invoice_number` = `ELIDE-` + 12 hex of
  SHA-256(intent id) and `detail.reference` = the session id. Each logical create/send/cancel
  has one persisted `PayPal-Request-Id`, reused on every attempt (§16); correctness does not
  depend on PayPal honouring it.
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
| same, after deadline | policy (§16): auto-cancel **disabled** (pilot) → operator review, nothing cancelled or released, polling continues; enabled → `cancel_payment_session` → GET CANCELLED → `expired`, release |
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

> **Corrected in §17 (final review).** The checker as first implemented (v1) did not check
> `credit = rate·net` or any journal account against PayPal's own figures; v2 does (§17.1).
> The payer entry described in step 3 overstated its evidence; from 0008 it is
> `obligation_discharged` to `payment_source_unverified:<capture>` (§17.2). The
> `check_sha256` placeholder is replaced by `settlement_verification` (§17.3).

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
| A8 | Independent checker agrees with booking; detects a tampered bundle (v1 detected only unbalanced or receivable-reopening tampers; superseded by A8′, §17.6) | unit + PostgreSQL | Synthetic-tested |
| A9 | Commitment lifecycle (§4.5) incl. new evidence with/without the outflow | PostgreSQL | Synthetic-tested |
| A10 | Cross-tenant: other merchants cannot see intents, links, settlements; payee sees no payer reserve | PostgreSQL over HTTP | Synthetic-tested |
| A11 | UI: execution states, payer link action, provider-confirmed vs spendable labels | jsdom + Playwright (mock provider) | Synthetic-tested |
| A12 | Adapter request/response shapes against PayPal's schema; live read-only GETs of the P0 invoice/capture through the P5 reconciler | contract + read-only Sandbox | Sandbox-verified (read only) |
| A13 | One new Sandbox invoice created, sent, paid by the Sandbox payer, reconciled, duplicate replay posts once | owner-authorized operation | Sandbox-verified — done 2026-11-12, accepted by the owner (`evidence/phase5/sandbox_payment_result_2026-11-12.md`; payer source account not verified, §17.2) |
| A14 | Verified webhook delivery | needs a public HTTPS endpoint and a registered webhook | **Blocked** (owner decision) |

## 12. Alternatives considered

- **Orders v2 checkout instead of Invoicing:** same payee constraint (the app's own account)
  and no invoice identity to reconcile against the obligation. Kept as a documented fallback.
- **Store the payer-view link encrypted:** rejected; fetching it on demand avoids storing a
  bearer link at all.
- **Rely on `PayPal-Request-Id` alone for create/send:** rejected. It is sent and persisted, but the endpoint schema does not list it, the generic guide says "not all APIs support this header" and that a concurrent duplicate "might fail"; search and GET stay authoritative (§16).
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

## 15. Implementation addendum (2026-11-12)

Implemented as designed, with these decisions and divergences:

1. **P4 behaviour preserved where P5 did not need to change it.** The webhook-triggered
   fetch stays `held` unless a payment provider is configured (nothing could fetch it); the
   UI keeps the P4 labels `INTENT_CREATED_NOT_SENT` and "held: no PayPal call" (now followed
   by the reason). The only P4 assertion changed is the stored status value
   (`awaiting_payer_action` → `approved`, renamed by migration 0006 with a reversible
   downgrade).
2. **Payer-side books.** Two illustrative entries per payment: the payable recognized
   (merchant data, `merchant_attested`) and the payment sent (cites the capture). Both are
   in the same atomic booking as the payee bundle; the independent checker requires the
   obligation's payable and the invoice receivable to net to zero.
3. **Settlement events persisted.** `obligation_settlement_event` (append-only) is loaded by
   `load_record` and applied in time order with revisions, so every P1 rule (no
   over-settlement, the settlement identity) is re-checked on every load.
4. **Two fingerprints per binding.** `credential_fingerprint` (the client id / payer email
   actually configured, checked before every call) and `account_fingerprint` (the provider
   merchant id, checked against the capture's payee).
5. **Merchant-attested capacity route.** `POST /capacity-evidence` lets a non-synthetic
   merchant attest its own balance; this is the only way the pilot payer gets non-synthetic
   capacity, and it is labelled `MERCHANT_ATTESTED` everywhere.
6. **Crash recovery.** A mutating `provider_operation` left `started` longer than
   `stale_op_s` (default 300 s, above the 120 s lease) is reclassified `unknown` and resolved
   by lookup; a fresher one makes other workers back off.
7. **Read-only live verification (A12).** `scripts/p5_readonly_probe.py` re-read the P0
   invoice and capture, ran a read-only `search-invoices`, and passed them through the P5
   parsers: settlement identity, strict PAID terms, payee fingerprint, Sandbox payer-view host
   all confirmed (`evidence/phase5/readonly_probe_20261112T103417Z.json`).

**Not done (blockers and gaps):**
- A13 (one new Sandbox payment) needs owner authorization:
  `evidence/phase5/sandbox_operation_gate_2026-11-12.md`. *(Done later the same day under owner authorization and accepted; see A13 in §11 and §17.)*
- A14 (verified webhook delivery) needs a public HTTPS endpoint and a registered webhook.
- Refund posting is detection only (`REFUND_OBSERVED` exception), pending a real refund.
- No Playwright test drives the mock provider end to end (the mock lives in-process; a
  browser test would need a payer-simulation route, which was deliberately not added). The
  P5 browser surface is covered by jsdom tests; the P4 Playwright suite passes unchanged.
- Operator tooling for resolving `exception` / dead letters is data-only (rows and audit);
  there is no resolution UI.

## 16. Gate review corrections (2026-11-12)

Owner review of `613d72f` (CI run #25). Both findings were reproduced before any fix
(`evidence/phase5/gate_review_reproduction_2026-11-12.md`).

### 16.1 Cancellation policy (finding 1)

- **Problem:** `observe()` scheduled `POST .../cancel` once the pay-by time passed, and again
  when the approver's authority was revoked after send. Automatic cancellation is excluded
  from the initial pilot (owner decision).
- **Policy:** `AUTO_CANCEL_UNPAID` (`Runtime.auto_cancel`), default **false**.
- **Disabled (pilot):** the deadline or a revoked authority opens one operator-review
  exception (`PAY_DEADLINE_PASSED_REVIEW` / `AUTHORIZATION_REVOKED_REVIEW`). It makes no
  cancel call, releases no cash and changes no local state. Read-only reconciliation goes on
  at a slower poll, so a late payment is still booked once. Only a PayPal-confirmed
  cancellation (by GET) ends the payment as expired/cancelled. The payer link is refused
  after the deadline (`PAY_DEADLINE_PASSED`) and while a review is open (`NEEDS_REVIEW`).
- **Unchanged:** explicit payer cancellation (`POST /payment-intents/{id}/cancel`), the kill
  switch (no provider mutation at all when off) and the payment-races-cancellation path.
  With the policy enabled, the previous behaviour is kept and tested.

### 16.2 Provider request identity (finding 2)

Primary sources (fetched 2026-11-12):

| Source | Says |
|---|---|
| Invoicing "Core concepts" (`developer.paypal.com/invoicing/core-concepts`, updated 2026-10-02) | "Use the `PayPal-Request-Id` header to make requests idempotent"; retry with the same id after a network error; PayPal returns the original result "without creating a duplicate invoice"; invoice numbers "must be unique within your PayPal invoices" |
| REST request guide (`developer.paypal.com/docsai/developer/how-to/api/make-api-requests`, updated 2026-10-02) | recommended for all POST and PUT calls; stored "for up to 45 days"; a retry returns the result of the original call |
| Idempotency guidelines (`developer.paypal.com/reference/guidelines/idempotency`, updated 2026-10-02) | "Not all APIs support this header"; a reuse returns the *latest* status; unique per API call type; of two concurrent requests with the same id PayPal "might fail the second" |
| Invoicing v2 schema / endpoint reference (`developer.paypal.com/api/invoicing/v2/schema.json`, modified 2026-11-09) | defines the header in components but does not attach it to create, send or cancel |

**Discrepancy:** the guides document the header for invoicing, but the endpoint reference does
not list it, and the generic guide qualifies its behaviour (latest status, possible failure
of concurrent duplicates). **Conservative behaviour implemented:**

- **Stable identity.** Migration `0007` adds `provider_request`, one append-only row per
  (intent, create/send/cancel). Its id is deterministic (`elide-<intent>-<op>`), and it records
  the digest of the request it was first used for. Every attempt links to it
  (`provider_operation.request_id`, `attempt_no`) and sends that id.
- **Conflicting payloads.** A changed payload under the same logical operation is refused
  before any call (`PROVIDER_REQUEST_CONFLICT`: operator review, cash kept).
- **No re-issue after success.** A mutation whose logical request already succeeded is never
  re-issued (`REQUEST_ALREADY_SUCCEEDED`): the worker re-reads PayPal instead. This was found
  by the duplicate-dispatch test.
- **Refusals of retries are unknown, not failures.** A refusal (400/404/409/422) of a
  *retried* mutation, and any 409, is treated as an unknown outcome and looked up. It may
  mean the first attempt succeeded: the number is taken, or PayPal failed a duplicate id.
- **Search and GET stay authoritative.** Lookup by invoice number and the GET before every
  send are kept; nothing trusts idempotency alone.
- **Lease fencing.** Post-call writes (adopting the invoice, state changes after send or
  failure) require the worker's lease. A late reply from a worker that was taken over is
  recorded (`provider.late_reply`) and changes nothing.

### 16.3 Browser coverage

`scripts/e2e_app.py` runs the real API with an in-process mock provider and loopback-only
test controls (run the worker, simulate the payer at "PayPal", list mock calls). It is not
part of the product. `web/e2e/payments.spec.ts` (4 tests) covers:
- the payer prepares an intent and the worker creates and sends;
- the hosted link is available to the payer only;
- PAID reconciles;
- scoped settlement views, with the receipt labelled not spendable;
- a wrong-payee capture goes to review;
- a stale authorization never reaches the provider.

Requests to the Sandbox host are aborted in those tests.

### 16.4 Tests

- 16 new PostgreSQL cases in `tests/integration/test_p5_gate_review.py` (the 2
  reproductions and 14 regressions).
- The two auto-cancel tests in `test_p5_failures.py` now enable the policy explicitly.
- 2 jsdom tests for the settlements view and 4 Playwright tests.

## 17. Final architectural review corrections (2026-11-12)

The owner accepted the controlled Sandbox payment (invoice `INV2-HQXZ-SCNE-RKTV-VMMT`, capture
`82L451107A123522J`, evidence commit `85ebae1`, CI #34). Three accounting and evidence defects
were found. They are corrected here **without rebooking the pilot payment or changing any
recorded provider fact**. Migration `0008_p5_verification_attribution`.

Reproduction: `tests/integration/test_p5_verification_review.py`, run against the reviewed
code and recorded in `evidence/phase5/review_reproduction_20261112.txt`.
- 8 balanced-but-wrong bookings all reconciled.
- The payer outflow was labelled `sandbox_verified`.
- `check_sha256` was `0`×64.

### 17.1 Independent financial verification (finding 1)

**Root cause.** Checker v1 (`check_capture`) verified only ledger identities:
- the receivable and payable net to zero;
- clearing received the gross;
- the settlement record's `gross − fee = net`;
- the obligation identity.

Its expectations came from the session terms and the stored record, which the same booking
had written. It never compared any journal account with PayPal's own figures. §7.3 above
already required `credit = rate·net`, but v1 did not implement it.

So these all passed, and it would have missed the same defects on the pilot:
- a balanced shift between `paypal_balance` and `fx_conversion_position`;
- a fee moved against the FX position or the balance;
- an internally consistent wrong rate, credit or net;
- issuance booked at the paid amount with the concession dropped;
- a concession booked to `revenue:sales`;
- an inflated payable offset as extra discount.

**Checker v2** (`api/accounting/reconciliation_check.py`, `CHECKER_VERSION =
elide-reconciliation-check/2`). It imports none of the builders, the PayPal settlement
parser, `invoice_terms`, the PayPal money boundary or `api.domain.money`; a unit test
enforces this. It works in five steps.

1. **Parse PayPal's raw GET bodies strictly**, with its own parser. Amounts must be at most
   two decimals in one currency. It checks:
   - the invoice is PAID with exactly one SUCCESS transaction, and that transaction is this
     capture;
   - the capture is COMPLETED and final, and belongs to this invoice;
   - item − discount = total = paid; due is 0;
   - there is no tax, shipping, custom or item discount;
   - capture amount = gross; gross − fee = net;
   - credit = net with no rate, or credit is within ½ minor unit of net × PayPal's rate in
     the stated direction. At an exact half-cent tie either neighbour is accepted, because
     PayPal does not document its tie rule (a Hypothesis property pins this).
2. **Compare with the approved terms**, read from raw rows:
   - obligation face = session item = provider item total;
   - approved concession = provider discount;
   - intent amount = session total = provider total = gross;
   - the capture payee is the bound invoicer account.
3. **Compare the settlement record**, every amount, currency, rate, status, time and party,
   with the provider facts.
4. **Check the journal.** Every entry that cites the invoice or the capture, or touches
   `accounts_receivable:<invoice>` or `accounts_payable:<obligation>`, must:
   - be an expected kind, in the expected entity, citing the expected resource, with the
     expected evidence class;
   - have **debit and credit totals per account and currency** equal to those derived from
     the provider facts.

   Any extra, missing or duplicate entry is a problem. The chart-of-accounts contract
   verified:

   | Entry | Account / currency | Debit | Credit |
   |---|---|---|---|
   | invoice_issued | `accounts_receivable:<inv>` USD | face | 0 |
   | invoice_issued | `revenue:sales` USD | 0 | face |
   | concession_authorized | `contra_revenue:early_payment_discount` USD | discount | 0 |
   | concession_authorized | `accounts_receivable:<inv>` USD | 0 | discount |
   | provider_settlement | `paypal_settlement_clearing` USD | gross | fee + net |
   | provider_settlement | `accounts_receivable:<inv>` USD | 0 | gross |
   | provider_settlement | `expense:paypal_fees` USD | fee | 0 |
   | provider_settlement (FX) | `fx_conversion_position` USD | net | 0 |
   | provider_settlement (FX) | `paypal_balance` CAD | credit | 0 |
   | provider_settlement (FX) | `fx_conversion_position` CAD | 0 | credit |
   | provider_settlement (same currency) | `paypal_balance` USD | net | 0 |
   | payable_recognized | `purchases` USD | face | 0 |
   | payable_recognized | `accounts_payable:<obl>` USD | 0 | face |
   | obligation_discharged | `accounts_payable:<obl>` USD | face | 0 |
   | obligation_discharged | `payment_source_unverified:<cap>` USD | 0 | cash |
   | obligation_discharged | `discount_received` USD | 0 | concession |
   | payment_source_attested | see §17.2 | | |

5. **Check settlement events and commitments.**
   - Settlement events: exactly one cash_applied event for the intent, equal to gross, and
     face = applied + concession + remaining, with remaining 0.
   - Commitments: the reservation was closed as `settled:capture:<id>`, and there is one
     settled outflow equal to the cash in the payer's books.

The checker is a **booking guard**. It runs inside the booking transaction after every row
is written; any problem rolls the whole booking back and opens `INDEPENDENT_CHECK_FAILED`.
At booking, the bodies must also be the ones Elide's own GETs recorded
(`provider_operation.response_sha256`).

`check_capture` (v1) is kept for the historical scripts and now accepts the corrected payer
structure; v2 supersedes it.

### 17.2 Payer-side attribution (finding 2)

**Root cause.** The single `provider_payment_sent` entry credited `paypal_payments_sent`
(the payer's own cash outflow) as `sandbox_verified`. PayPal's invoice and capture GETs
confirm only that the invoice was paid and that the payee received the money. The capture
has a `payee` but no payer field (Payments v2 capture schema), and the invoice transaction
does not name the paying account. Related claims went beyond the evidence in the same way:
- `provider_settlement.payer_merchant`;
- the settled-outflow comment ("the cash has left the account");
- the UI text "You paid … paid via PayPal".

**Three separately evidenced facts now:**

| Fact | Evidence | Where |
|---|---|---|
| Obligation settlement | **Provider-confirmed** (invoice PAID, capture COMPLETED to the bound payee, checker v2) | payee bundle; payer `obligation_discharged` (`sandbox_verified`) |
| Payer authorization of the payment | **Merchant-approved** (leg authorization, release, state events, audit chain) | P4/P5 records |
| Payer source-account debit | **Not verified** until the payer merchant attests it; *provider-verified* is not available (below) | `payment_source_unverified:<cap>` → `payment_source_attested` |

**Payer books from 0008:**
- `payable_recognized` (merchant-attested).
- `obligation_discharged` (`sandbox_verified`, cites the capture): Dr payable / Cr
  `payment_source_unverified:<cap>` (cash) / Cr `discount_received`. The payable clears,
  because the debt is discharged whoever paid it.
- `POST /payment-intents/{id}/payment-source` records `payment_source_attested`
  (`merchant_attested`, payer only, `payment:authorize`, step-up, Idempotency-Key, at most
  once per capture; the DB unique key and a row lock enforce this):
  - `own_paypal_account`: Dr unverified / Cr `paypal_payments_sent`;
  - `third_party`: Dr unverified / Cr `third_party_payment:<cap>`.

  It never changes the obligation, the payee's books or any cash commitment.

**A third party pays.** The obligation is still discharged and booked once. The payer's
outflow is not invented: it stays unverified until the payer attests "third party". The
settled-outflow commitment remains a conservative hold in every case, released only by newer
balance evidence that lists it, so a payer who did not pay is at worst under-counted, never
over-counted.

**Pre-0008 records (the pilot)** are immutable and keep their recorded values. The migration
appends `evidence_correction` rows (append-only, unique per subject and aspect):
- each `provider_payment_sent` entry: `payer_source_debit` → `unverified`;
- each settlement: `payer_merchant` → `debtor_of_discharged_obligation`;
- each settlement: `check_sha256` → `placeholder_not_a_digest`;
- each settled outflow: `outflow_basis` → `presumed_outflow_unverified_source`.

The v2 checker accepts the legacy payer entry **only** with its correction. A legacy
own-account attestation posts nothing; a legacy third-party attestation moves the credit
from `paypal_payments_sent` to `third_party_payment:<cap>`.

**Stronger payer identity: research.** PayPal Transaction Search,
`GET /v1/reporting/transactions` with `transaction_id` and `fields=all`, returns
`payer_info.account_id` and `email_address` for transactions on the caller's own account. It
needs the `https://uri.paypal.com/services/reporting/search/read` scope, and
"It takes a maximum of three hours" for transactions to appear.

That is a supported, authorized read for the payee. It could establish **which PayPal account
paid**, by fingerprint against the payer binding. It still would not prove a debit in the
payer merchant's own books, and the binding itself is merchant-attested. It is **not
implemented and was not called**. Enabling it needs the owner's authorization, the scope on
the REST app and an ADR addendum. Until then nothing in Elide produces `PROVIDER_VERIFIED`
for a payer.

### 17.3 Verification attestation replaces the placeholder (finding 3)

**Root cause.** `check_sha256` was declared as "the digest of the independent check" but was
written as the constant `"0" * 64`. No digest was ever computed, so the field asserted
nothing.

**Design.** `settlement_verification` (append-only) stores, per run:
- the canonical verification document (sorted keys, no whitespace, ASCII);
- `document_sha256` = SHA-256 of that document;
- `checker_version`, purpose (`booking` | `reverification`), verdict and problems;
- both body hashes, the time and the actor.

The document identifies:
- the checker version;
- the input bodies, by `body_sha256` (the same canonical form as `response_sha256`);
- the provider facts extracted;
- every record examined: the settlement row digest; the intent, session, obligation and
  binding digests over their stable columns; each journal entry's id, kind, entity,
  `content_sha256` and a digest of its postings; settlement events; commitments; the
  attestation and the corrections;
- the problems.

The same rows and bodies reproduce the same digest (tested).

Database guards:
- `(verdict='passed') = (problems='[]')`;
- the digest format;
- one booking verification per capture;
- the verification's intent must equal the settlement's;
- a **deferred constraint trigger** rejects any `provider_settlement` committed without a
  passing booking verification;
- a NOT VALID `CHECK (check_sha256 IS NULL)` forbids the placeholder on every new row, while
  pre-0008 rows keep it, labelled by their correction.

API (both parties) `verification`:
- `status`;
- `attestations[]`, each with purpose, verdict, version, digest and time; never the document,
  which spans both merchants' books;
- `legacy_check_sha256_placeholder`.

### 17.4 API contract changes

| Method | Path | Change |
|---|---|---|
| GET | `/v1/m/{m}/settlements`, `/payment-intents/{id}` (payer) | `paid_cents` → `settled_cents` (amount PayPal applied to the obligation); adds `obligation_settlement`, `payer_authorization`, `payment_source`, `payment_source_attested_at`, `payment_source_note`, `outflow_commitment_basis`, `corrections`, `verification` |
| GET | same (payee) | adds `paying_account: NOT_IDENTIFIED_BY_PROVIDER`, `verification`; never the payer's attestation |
| POST | `/v1/m/{m}/payment-intents/{id}/payment-source` | new (§17.2); `409 NOT_RECONCILED` / `ALREADY_ATTESTED`; payee and other tenants get `404` |

### 17.5 The real pilot

The pilot database was migrated to 0008. The recorded booking (5 entries, 17 postings, 1
settlement, the placeholder) is unchanged. `scripts/p5_reverify_settlement.py` then:
- made two read-only GETs;
- ran checker v2 against the raw rows;
- appended one `reverification` attestation;
- wrote `evidence/phase5/reverification_<UTC>.json`.

The payer's payment source remains **NOT_VERIFIED**. Only the payer merchant, as the owner,
can attest it; Elide did not attest it.

### 17.6 Acceptance additions

| # | Requirement | Test level | Evidence class |
|---|---|---|---|
| A8′ | Checker v2: 8 balanced-but-wrong tampers refused atomically (CAD credit offset in FX, fee misallocation ×2, wrong rate, wrong net, issuance without concession, concession to revenue, inflated payable) | PostgreSQL, mock PayPal | Synthetic-tested |
| A15 | Strict provider parser, honest-figure and wrong-credit properties, checker independence | unit + Hypothesis | Synthetic-tested |
| A16 | Verification attestation reproducible; append-only; deferred trigger rejects an unverified settlement; placeholder refused on new rows | PostgreSQL | Synthetic-tested |
| A17 | Payer attribution: discharge provider-confirmed, source not verified; own-account and third-party attestation once (concurrency); payee cannot attest; payee view private | PostgreSQL over HTTP, jsdom, Playwright | Synthetic-tested |
| A18 | Legacy (pre-0008) booking: migration appends corrections, immutable facts unchanged, v2 verifies with the correction, legacy attestations | PostgreSQL (0007 → book → 0008) | Synthetic-tested |
| A19 | Real pilot re-verified by checker v2 from fresh GETs, appended attestation, no financial row changed | read-only Sandbox | Sandbox-verified (read only) |

Still open, not changed by this review:
- INV-1 (invoice transaction time about 8 h from capture time);
- A14 (verified webhooks);
- refund posting (detection only);
- ADR-0013 (strict xfail unchanged);
- Transaction Search payer identity (§17.2, needs authorization).

### 17.7 Approval and accepted scope (2026-11-12)

The owner approved P5 at `f26e536` (CI run #35) and merged it to `main` at `f7e7974`
(CI run #36).

**Demonstrated:**
- independent merchant consent;
- a verified payment plan;
- one real PayPal **Sandbox** invoice, created and delivered;
- a human payment on PayPal's hosted page;
- independently retrieved provider confirmation;
- accounting reconciliation with independent verification (checker v2);
- idempotent replay for the tested scenarios.

**Not demonstrated, and not to be claimed:**
- production merchant payments;
- arbitrary third-party merchant PayPal integration;
- verified source-account identity of the payer;
- spendable downstream liquidity;
- live webhook delivery;
- complete refund, reversal and dispute accounting;
- real merchant financial performance or commercial impact.
