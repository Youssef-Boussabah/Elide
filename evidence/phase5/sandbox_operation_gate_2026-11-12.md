# P5 controlled Sandbox operation: proposal for owner authorization

**Status: NOT AUTHORIZED, NOT PERFORMED.** Nothing in this file has been executed against
PayPal. It describes the one Sandbox operation that would produce the P5 gate evidence
(ADR-0012 acceptance A13), so the owner can authorize it, change it, or refuse it.

Branch `feat/p5-paypal-execution`. Everything before this gate is **Synthetic-tested**
(mock provider), except the read-only probe below (**Sandbox-verified, read only**).

## 1. What is already verified without any mutation

`evidence/phase5/readonly_probe_20261112T103417Z.json` (2026-11-12, existing credentials,
calls: OAuth, `GET /v2/invoicing/invoices/{P0 invoice}`, `POST /v2/invoicing/search-invoices`
(read-only search), `GET /v2/payments/captures/{P0 capture}`; all HTTP 200):

- the P5 strict parsers reproduce the P0 settlement exactly (gross USD 990.00, fee 29.01,
  net 960.99, credit CAD 1,306.79 at 1.35984225); the strict PAID comparison reports 0 problems;
- search by invoice number returns exactly the P0 invoice (the lookup used after a lost
  create response works on real Sandbox data);
- the capture's payee fingerprint equals the P0 record (`redacted:6dbc83888cc3`): the
  wrong-payee guard compares against real provider data;
- the invoice's `recipient_view_url` is on the Sandbox host (on-demand payer link).
- The run is labelled `+dirty`: the evidence-writer guard had been tightened (a false
  positive on the key name `http_status`) and was not yet committed; no code used by the
  calls changed.

## 2. The proposed operation

| Item | Value |
|---|---|
| Payee (invoicer) | The Sandbox Business account that owns the app credentials ("Test Store", CA; settles in CAD). Credential fingerprint `redacted:2b2f310e3f08`; merchant fingerprint `redacted:6dbc83888cc3` |
| Payer | The Sandbox Personal account in `PAYPAL_SANDBOX_PAYER_EMAIL` (address never written to evidence) |
| Elide merchants | `pilot_seller` (bound to the payee) and `pilot_buyer` (bound to the payer); TEST identities, non-synthetic network `sandbox_pilot_network` |
| Obligation | `pilot_001`: USD 25.00, due in 10 days; seller offers USD 1.00 off if paid within 3 days |
| Payment | **USD 24.00** (one line USD 25.00, invoice discount USD 1.00), USD only, partial payments and tips disabled |
| Invoice number | `ELIDE-` + 12 hex of SHA-256(intent id) (deterministic; used for lookup) |
| Money | Sandbox only; no real money. PayPal sets the fee; the CAD credit is recorded as reported and never counted as USD capacity |

### Steps and exact PayPal calls

| # | Who | Action | PayPal call |
|---|---|---|---|
| 1 | Owner | `uv run python scripts/seed_sandbox_pilot.py` (local DB) | none |
| 2 | Owner | `uv run python scripts/bind_sandbox_accounts.py --payee pilot_seller --payer pilot_buyer --payee-fingerprint redacted:6dbc83888cc3 --consent "..." --confirm` (local DB) | none |
| 3 | Owner | In `.env`: `PAYMENT_PROVIDER=paypal_sandbox`, `PAYMENTS_EXECUTION_ENABLED=true`; restart the API | none |
| 4 | Owner (as `pilot_buyer`) | In the workspace: attest own balance (merchant-attested, e.g. USD 200.00 with USD 50.00 reserve), accept the USD 1.00 offer, create the plan, approve the USD 24.00 leg (step-up), prepare the payment intent | none |
| 5 | Worker (`scripts/run_worker.py --once`) | Revalidate; create the draft | `POST /v1/oauth2/token`; `POST /v2/invoicing/invoices` (DRAFT) |
| 6 | Worker | Verify the draft; revalidate; send; verify | `GET /v2/invoicing/invoices/{id}`; `POST /v2/invoicing/invoices/{id}/send` (`send_to_recipient=true`: PayPal emails the Sandbox payer, as in P0); `GET` |
| 7 | Owner (as the Sandbox payer) | "Open PayPal payment page" (link fetched by GET, never stored); log in as the Sandbox Personal account and pay in PayPal's page | `GET /v2/invoicing/invoices/{id}` (link); the payment itself is the owner's action at PayPal |
| 8 | Worker | Reconcile and book | `GET /v2/invoicing/invoices/{id}` (PAID); `GET /v2/payments/captures/{capture}` |
| 9 | Worker / script | Duplicate replay: run reconciliation again and a second provider fetch | `GET` only; must book nothing new |
| (only if unpaid by the deadline) | Worker | Cancel the unpaid invoice, confirm by GET | `POST /v2/invoicing/invoices/{id}/cancel` — **listed so the owner can include or exclude it** |

No refund, capture, "record payment", invoice update or deletion is part of this operation.

## 3. Financial and privacy implications

- Sandbox only. The payer's capacity is **merchant-attested** (owner-entered), not
  provider-verified; it is labelled so everywhere.
- After booking: the payee's receipt is `PROVIDER_CONFIRMED_NOT_SPENDABLE` (D006) and, being
  CAD, never USD capacity (D008). The payer's USD 24.00 stays committed as a settled outflow
  until the owner attests a newer balance that lists it.
- Data sent to PayPal Sandbox: the payer email as the invoice recipient (as in P0), the
  obligation id and revision in the item name and note, the session id as the invoice
  reference. No other merchant data.

## 4. Evidence that would be recorded (redacted)

- A run file under `evidence/paypal-sandbox/runs/` with status codes, debug ids, provider ids
  of the invoice and capture, amounts and fingerprints (no tokens, emails, names or URLs).
- `provider_operation` rows for every call (state, HTTP status, debug id, response hash).
- The intent's state history (`approved -> session_pending -> payer_action_pending ->
  provider_confirmed -> reconciled`).
- The journal bundle (payee: invoice issued, concession, capture settlement with fee and FX;
  payer: payable recognized, payment sent), the settlement record and the independent
  checker result (expected: no problems), plus a trial balance of zero per currency.
- The duplicate replay: one settlement record, two journal entries citing the capture.
- Clearly separated: browser observations (the hosted page) vs provider GETs.

## 5. Not covered even after this operation

- **Webhooks (A14):** needs a public HTTPS endpoint and a webhook registered for this app
  (an owner decision: a tunnel or a deployment). Until then, reconciliation polls by GET;
  signature verification is unit-tested against PayPal's documented contract only.
- Refund posting: a real Sandbox refund (another mutation) must be observed first.
- Payer identity: PayPal's resources do not prove which PayPal account paid the hosted
  invoice; Elide records the payment against the invoice and says so.
