# P5 Sandbox pilot preflight (2026-11-12): ALL PRECONDITIONS PASS. NOT AUTHORIZED. NOT EXECUTED

> **Status update (2026-11-12, appended; the text below is the historical record as written).** The owner later authorized exactly this one operation. It was performed and verified (`evidence/phase5/sandbox_payment_result_2026-11-12.md`), corrected and re-verified in the final review (ADR-0012 §17), and P5 was approved at `f26e536` and merged to `main` at `f7e7974` (CI run #36). PayPal-mutating execution remains disabled.

Owner instruction: "Phase 5 approved for Sandbox validation preflight". No provider-mutating
call was made. `PAYMENTS_EXECUTION_ENABLED` stays `false` and `PAYMENT_PROVIDER` stays
`none` until the owner explicitly authorizes this specific operation.

- **Machine-checked report:** `evidence/phase5/sandbox_preflight_20261112T134530Z.json`
  (`scripts/p5_preflight.py`, read-only).
- **First run:** `sandbox_preflight_20261112T134451Z.json` is kept. It **stopped** on a
  defect in the preflight script itself: its static check missed a local attribute and
  matched direct calls only. The check was corrected in `10a4211` and the run repeated.
  No system code changed.
- **Code:** identical to the approved `0349162091196bd712620ca02b50f181903b4911` (CI run #26).
  The branch head adds only the preflight script and evidence (`12032b3`, `10a4211`, and
  this commit).

## Results

| # | Precondition | Result |
|---|---|---|
| 1 | Approved commit, clean tree | PASS: no file under `api/`, `migrations/`, `web/src/`, `fixtures/` or the worker differs from `0349162`; tree clean |
| 2 | Migrations through 0007 | PASS: `0007_p5_request_identity (head)` in the pilot database and in the dev database |
| 3 | Payee and payer identities | PASS. Payee credential `redacted:2b2f310e3f08` (= the 2026-11-12 probe). Payee account (capture `payee.merchant_id`) `redacted:6dbc83888cc3` (= the P0 capture record and the probe). Payer `redacted:94281907e2c4` (= the recipient PayPal recorded on the P0 invoice, 17 occurrences in the P0 runs). Bind script dry run prints exactly these; nothing written |
| 4 | `AUTO_CANCEL_UNPAID=false` | PASS (default; not set in `.env`) |
| 5 | `PAYMENTS_EXECUTION_ENABLED=false` | PASS; `PAYMENT_PROVIDER=none`; no webhook id configured |
| 6 | Isolated database | PASS: new database `elide_sandbox_pilot`, migrated from empty, containing only `pilot_buyer` and `pilot_seller` (non-synthetic TEST identities). The dev (`elide`) and e2e (`elide_e2e`) databases are not used by the pilot |
| 7 | Outbox | PASS: no message of any kind or status (pending, leased, held, cancel) |
| 8 | No conflicting records | PASS: no intent, session, provider request/operation, settlement or exception. PayPal (read-only `GET /v2/invoicing/invoices`, debug id `f699303e53d74`): `total_items` 1, the P0 invoice `INV2-4RUV-XPEH-4F3C-T8BW`, number `0001`, status `PAID`; no `ELIDE-*` number; no unpaid invoice |
| 9 | Terms | PASS. `pilot_001`: `pilot_buyer` owes `pilot_seller` **USD 25.00**, due 2026-11-22T13:13Z. The seller's open offer is **USD 1.00** off, so **USD 24.00** is payable by **2026-11-15T13:13Z** (the PayPal due date is 2026-11-15, Toronto). The offer expires **2026-11-14T13:13Z**. No payment authorization exists yet: it is the owner's step and is valid 24 h |
| 10 | No paid LLM inference | PASS. The workspace uses `workspace_llm = None` or the refusing adapter; the pilot flow makes no model call. A model key is configured in `.env` for P3; only the P3 demo routes (loopback, capped) could use it, and the pilot does not call them |
| 11 | No automated cancellation, refund, capture, update or production operation | PASS. Auto-cancel is off. The client has no capture, refund or record-payment operation. Execution references only create/send/cancel/get/search plus two local attributes. No invoice update or delete. The base URL is Sandbox only. `POST .../cancel` is reachable **only** through an explicit payer "Cancel payment", which the owner must not use in this run |

## The exact intended operation (when, and only when, the owner authorizes it)

| Step | Who | Action | PayPal call |
|---|---|---|---|
| A | Owner | `scripts/bind_sandbox_accounts.py --payee pilot_seller --payer pilot_buyer --payee-fingerprint redacted:6dbc83888cc3 --consent "<owner text>" --confirm` with `DATABASE_URL` = the pilot database | none |
| B | Owner | `.env`: `PAYMENT_PROVIDER=paypal_sandbox`, `PAYMENTS_EXECUTION_ENABLED=true`, keep `AUTO_CANCEL_UNPAID=false`; start the API on the pilot database | none |
| C | Owner, signed in as `pilot_buyer` (password in git-ignored `.local/pilot_credentials.json`) | Attest own balance (merchant-attested); accept the USD 1.00 offer **before 2026-11-14T13:13Z**; create the plan; approve the USD 24.00 leg; prepare the intent | none |
| D | Worker, one pass, within 24 h of C | Revalidate, then create one invoice: 1 × USD 25.00, discount USD 1.00, total USD 24.00, due 2026-11-15, recipient = the Sandbox payer, partial payment and tips off, number `ELIDE-<12 hex>`, `PayPal-Request-Id: elide-<intent>-create`. Verify by GET, revalidate, send with `PayPal-Request-Id: elide-<intent>-send`, verify by GET | `POST /v1/oauth2/token`, `POST /v2/invoicing/invoices`, `GET`, `POST /v2/invoicing/invoices/{id}/send`, `GET` (plus a read-only search only if a reply is lost) |
| E | Owner, as the Sandbox Personal payer | "Open PayPal payment page" (link fetched by GET, never stored), then pay **USD 24.00** in PayPal's page | `GET` (link) |
| F | Worker | Reconcile, book once, independent check; then a replay (second reconcile and fetch) that must book nothing | `GET` invoice, `GET` capture |

**Excluded:** cancel (automatic or manual), refund, capture, invoice update, deletion, record
payment, webhooks and production.

**After payment:**
- the payee's receipt is provider-confirmed, not spendable (D006), and a CAD credit is
  never available USD (D008);
- the payer's USD 24.00 stays committed until a newer attestation lists it.

**Timing.** The offer was seeded 2026-11-12T13:43Z and expires 2026-11-14T13:13Z. After that,
the pilot database must be recreated and seeded again.
