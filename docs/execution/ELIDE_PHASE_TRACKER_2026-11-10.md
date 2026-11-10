# ELIDE — Phase Gate Tracker
**State (2026-11-10, 22:20 UTC):** P0 **partial: PayPal payment gate met; only the GitHub CI run remains** (needs an owner-approved remote). Local bootstrap is done and verified. PayPal Sandbox OAuth, Invoicing v2 access, draft with the $10 discount, due-date update, send, owner payment and independent provider retrieval are **Sandbox-verified**: invoice `INV2-4RUV-XPEH-4F3C-T8BW` is **PAID**, US$990.00, one transaction, remaining balance 0. Open finding: the Sandbox merchant receives in **CAD** (see *Open findings*). No financial features are implemented. Maintain this tracker in Git; update with commit, tests, redacted evidence and blocker notes.

| Phase | Gate | Status | Evidence to attach |
|---|---|---|---|
| P0 | API access + hosted Sandbox invoice payment route proven | **Partial**: PayPal Sandbox payment route proven (invoice PAID, provider-verified); CI run on GitHub not yet possible (no remote) | Redacted API responses + provider IDs/status + code SHA |
| P1 | Money model, reserves, fees, revisions and fixtures pass | Not started | pytest / property test output |
| P2 | Exact optimizer agrees with separate verifier and fair baselines | Not started | B0–B5 test table + seed/trace |
| P3 | Live policy parsing and bounded offer proposal safely gated | Not started | Prompt version + schema result + injection tests |
| P4 | Separate merchant sessions, approvals, permissioned UI | Not started | E2E run and auth isolation test |
| P5 | Actual PayPal payer payment + provider GET + safe replay | Not started | Redacted receipt + journal + webhook replay |
| P6 | QA, financial invariants, security and clean clone | Not started | CI, test matrix, known issues |
| P7 | Public license/repo, working demo, video, submission | Not started | Public links, release SHA, submission receipt |

## P0 detail (updated 2026-11-10)

### Implemented (evidence class: tested locally; no Sandbox evidence yet)
- Repository with the documented layout, `CLAUDE.md` and `AGENTS.md` (identical rules,
  enforced by a test), README, MIT LICENSE, `.gitignore`, `.gitattributes`, `.env.example`, ADRs 0001–0006.
- Authoritative docs copied into `docs/project-sources/`. All 18 match `ELIDE_SOURCES_SHA256.json`.
  The execution plan copy is byte-identical to the original.
- Backend: FastAPI app with `GET /health` (liveness) and `GET /health/ready` (DB check +
  config flags, no secrets). Pydantic settings use `SecretStr`, and placeholders count as
  unset. SQLAlchemy engine. Alembic baseline `0001_baseline` applied to PostgreSQL 16.15.
- Domain boundary packages (`domain`, `liquidity`, `planning`, `accounting`, `workers`)
  are **empty stubs** with documented responsibilities. No business logic yet.
- AI: provider-neutral `LLMAdapter` (structured output only, no tools). `UnconfiguredLLMAdapter`
  refuses instead of fabricating. A test checks that `api/ai` does not reference PayPal or integrations.
- PayPal adapter (`api/integrations/paypal/`): Sandbox-only client (OAuth, list/number/create/send/get
  invoice), exact cents ↔ decimal-string conversion, redaction. `scripts/paypal_sandbox_check.py`
  runs opt-in steps and writes redacted evidence. Run against Sandbox 2026-11-10 (see below).
- Frontend: React 19 + TS 6.0 + Vite 8 + Tailwind 4 + `@xyflow/react` (installed, not used yet).
  Status page reads `/api/health/ready` through the Vite proxy and labels the build as a Phase 0 scaffold.
- CI workflow `.github/workflows/ci.yml` (backend + Postgres service, frontend). **Not executed**
  because no GitHub remote exists.

### Tests and checks executed (2026-11-10, Windows 11)
Full log: `evidence/phase0/verification_2026-11-10.txt`, server probe: `evidence/phase0/server_smoke_2026-11-10.txt`.

| Check | Command | Result |
|---|---|---|
| Backend deps reproducible | `uv sync --locked` | pass |
| Lint / format | `uv run ruff check .` / `uv run ruff format --check .` | pass / pass |
| Backend tests | `uv run pytest` | **42 passed** at bootstrap; **52 passed, 0 failed, 0 skipped** after the Sandbox session (incl. 2 live-PostgreSQL integration tests) |
| Migrations | `uv run alembic upgrade head` → `0001_baseline (head)` | pass |
| PostgreSQL | `docker compose up -d --wait db` | healthy, 16.15 on 127.0.0.1:5433 |
| Backend starts | uvicorn :8000, `GET /health`, `GET /health/ready` | 200 / 200 (`database: ok`) |
| Frontend starts | `pnpm dev` :5173, `GET /`, `GET /api/health/ready` via proxy | 200 / 200 |
| Frontend deps reproducible | `pnpm install --frozen-lockfile` | pass |
| Typecheck / lint | `pnpm typecheck` / `pnpm lint` | pass / pass |
| Frontend tests | `pnpm test` (Vitest) | 3 passed |
| Frontend build | `pnpm build` | pass |
| Secrets ignored | `git check-ignore -v` on `.env`, `.env.local`, `evidence/**/raw/`, `secrets/`, `client_secret*` | all ignored; `.env.example` tracked |
| Docs intact | SHA-256 vs manifest | 18/18 OK |
| PayPal check without creds | `uv run python scripts/paypal_sandbox_check.py` | exits 2 "BLOCKED", no network call |

**Not executed:** GitHub Actions CI (no remote); any PayPal Sandbox call (no credentials);
Playwright/E2E (not set up).

### PayPal Sandbox status (runs 2026-11-10, Sandbox only, environment `api-m.sandbox.paypal.com`)
Owner configured a dedicated Sandbox Merchant app with Invoicing enabled. Env check
(values not printed): `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`,
`PAYPAL_SANDBOX_PAYER_EMAIL` present, no whitespace/quote issues, email-shaped.

| Step | HTTP | Debug id | Result | Evidence class |
|---|---|---|---|---|
| 1. OAuth token (`POST /v1/oauth2/token`) | 200 | `f62629410eab7` | Bearer token, `expires_in` 32400; scope includes `https://uri.paypal.com/services/invoicing`. Token not stored | **Sandbox-verified** |
| 2. Invoicing access (`GET /v2/invoicing/invoices`) | 200 | `f626294a2fc40` | Merchant-owned access works; `total_items=0` before the test | **Sandbox-verified** |
| 2a. `POST /v2/invoicing/generate-next-invoice-number` | **415** → 200 | `f81253945e66d` (415) | First call: `UNSUPPORTED_MEDIA_TYPE` "The request payload is not supported" (our request lacked `Content-Type: application/json`). Fixed in `client.py`; re-run returned 200, `0002`. Non-blocking: PayPal auto-numbered the draft `0001` | **Sandbox-verified** |
| 3. Create draft (`POST /v2/invoicing/invoices`, `PayPal-Request-Id`, `Prefer: return=representation`) | 201 | `f4434203103cb` | **Exactly one** draft: `INV2-4RUV-XPEH-4F3C-T8BW`, status `DRAFT`, number `0001` | **Sandbox-verified** |
| 5. Independent GET (same run) | 200 | `f705628b8d281` | Amounts correct (see below). Run flagged `MISMATCH` only because our parser rejected PayPal's negative discount sign; parser corrected | **Sandbox-verified** |
| 5. Independent GET (separate process, corrected parser) | 200 | see run file | `status=DRAFT`, all requested terms match | **Sandbox-verified** |
| 3a. Owner-approved due-date update: GET before (`PUT /v2/invoicing/invoices/{id}?send_to_recipient=false&send_to_invoicer=false`, full replace built from PayPal's own representation) | 200 / 200 / 200 | see run `20261110T220139Z` | Before: `NO_DUE_DATE`. After: `DUE_ON_DATE_SPECIFIED`, `due_date=2026-12-01`; status still `DRAFT`; total, discount, line item, invoice number, recipient unchanged | **Sandbox-verified** |
| 4. Pre-send GET checks | 200 | see run `20261110T220152Z` | `DRAFT`, 99000 cents, due 2026-12-01, sole recipient = configured Sandbox personal payer. All pass | **Sandbox-verified** |
| 4. Send (`POST .../send`, `send_to_recipient=true`, `PayPal-Request-Id=elide-8a59efe9-8618-40e7-80bf-83d5373455c7`) | 200 | see run `20261110T220152Z` | Owner-approved. Exactly this invoice sent; no new invoice created (`total_items=1` throughout) | **Sandbox-verified** |
| 6. Post-send GET | 200 | see run `20261110T220152Z` | Status **`SENT`**; all terms unchanged; `due_amount` 99000 | **Sandbox-verified** |
| 7. Hosted payer link | — | — | Returned by PayPal in the send response (`rel=payer-view`), host `www.sandbox.paypal.com`; not constructed. Stored only in git-ignored `evidence/paypal-sandbox/raw/`; fingerprint `redacted:f77a6cd3d419` | **Sandbox-verified** |
| 7a. Hosted page inspected in Chrome (Claude in Chrome, no buttons clicked) | — | — | Page on `www.sandbox.paypal.com`, seller "Test Store": Invoice #0001, 1 × US$1,000.00, discount −US$10.00, total/amount due US$990.00, "Due by 01/12/2026" (day-first = 1 Dec 2026), issued Nov 10 2026, billed to the Sandbox personal payer. Consistent with the API record. No screenshot saved (page shows payer email) | **Browser observation** (not provider evidence) |
| 8. Owner payment in hosted page | — | — | Owner logged in as the Sandbox personal account and paid in the browser (2026-11-10). Not initiated, captured or simulated by Elide | Owner action (browser) |
| 9. Independent provider GET after payment (`GET /v2/invoicing/invoices/{id}`; runs `20261110T221647Z` and `20261110T221823Z` with strict checks, commit `265726e`) | 200 | `f6275773b2b44` (first) | Status **`PAID`**; `paid_amount` USD 990.00 (99000); `due_amount` USD 0.00; exactly **1** transaction: `payment_id` **`4J016440MV5285528`**, type `PAYPAL`, `SALE`, `SUCCESS`, USD 990.00; terms unchanged (total 99000, discount 1000, due 2026-12-01, sole recipient = configured payer) | **Sandbox-verified** |
| 10. Payment resource (`GET /v2/payments/captures/4J016440MV5285528`, run `20261110T221827Z`) | 200 | `f310218d8914a` | `COMPLETED`, `final_capture=true`, `invoice_id` matches, order `7YW741992S8159210`, created 2026-11-10T22:15:48Z. Gross USD 990.00, **PayPal fee USD 29.01**, net USD 960.99, **receivable CAD 1,306.79** at USD→CAD **1.35984225**. Seller protection `ELIGIBLE` | **Sandbox-verified** |
| Orders v2 fallback | — | — | Not needed so far: Invoicing access works | Specified |

**Provider-recorded terms of `INV2-4RUV-XPEH-4F3C-T8BW`** (from GET, in cents):

| Field | Requested | PayPal recorded | Match |
|---|---|---|---|
| Currency | USD | USD | yes |
| Line item | 1 × 100000 | 1 × 100000 (`"1000.00"`) | yes |
| `item_total` | 100000 | 100000 | yes |
| `invoice_discount.amount` | 1000 (sent `"10.00"`) | 1000, returned as **`"-10.00"`** (negative sign) | yes (sign convention recorded) |
| `tax_total` | 0 | 0 | yes |
| Total (`amount.value`) | 99000 | 99000 (`"990.00"`) | yes |
| `due_amount` | 99000 | 99000 | yes |
| Status | DRAFT | DRAFT | yes |
| Payment term | not sent | `NO_DUE_DATE` (PayPal default) | n/a; recorded |
| Recipient | Sandbox personal payer | set (fingerprinted in evidence) | yes |

Redacted evidence (`evidence/paypal-sandbox/runs/`, scanned: no client id, secret,
payer email, `@` or access token present):
- `20261110T214449Z.json`: steps 1–2
- `20261110T214515Z.json`: steps 1–3, 2a (415), 5
- `20261110T214717Z.json`: separate-process GET, all terms match
- `20261110T214721Z.json`: numbering re-check, 200
- `20261110T220139Z.json`: due-date update (before GET, PUT, after GET), commit `c80173d`
- `20261110T220152Z.json`: pre-send GET, send, post-send GET (`SENT`), commit `c80173d`
  (recorded `+dirty` only because the previous run's evidence file was untracked)
- `20261110T221647Z.json`: first post-payment GET (`PAID`), commit `1316f79`
- `20261110T221823Z.json`: post-payment GET with strict checks (one SUCCESS transaction), commit `265726e`
- `20261110T221827Z.json`: capture lookup. `payee.merchant_id` was saved unredacted by that run and
  was fingerprinted afterwards (`redacted:6dbc83888cc3`); `merchant_id` is now a redacted key
- `RECONCILIATION_INV2-4RUV-XPEH-4F3C-T8BW.json`: derived reconciliation of the runs above

**Reconciliation (Sandbox-verified, derived only from provider GETs):** invoice total 99000 = paid 99000 = single transaction 99000 = capture gross 99000; remaining 0; gross − fee = 99000 − 2901 = 96099 net; 960.99 × 1.35984225 = 1306.7948 → CAD 1,306.79. Dedupe key `paypal_capture:4J016440MV5285528`. **No posting was made**: no ledger exists yet (P1), and re-running verification only re-reads PayPal and writes a new run file. Not established: that funds are spendable (D006), or that the merchant holds US$990.00 (it was credited US$960.99 net, converted to CAD).

**Browser vs provider:** the hosted page (7a) and the owner's browser confirmation (8) are browser observations. The PAID status, amounts and transaction come only from the provider GETs (9, 10).

**Timestamp note:** the invoice transaction's `payment_date_time` is `2026-11-10T14:15:49Z`; the capture's `create_time` is `2026-11-10T22:15:48Z`. The invoice value is labeled UTC but is 8 h behind (consistent with Pacific time). Use the capture timestamp as UTC.

Code changes made from these observations (ADR-0005 "Sandbox observations"): new
`api/integrations/paypal/invoice_terms.py` (exact parse of provider terms; negative sign
tolerated on the invoice discount field only); `generate_invoice_number` sends a JSON
content type; script compares every term, adds `--expected-status` and `--check-number`.

### Missing dependencies / environment notes
- GitHub CLI not installed (only needed for remote creation; system install needs approval).
- `python` on PATH is the Microsoft Store alias stub. Use `uv run` (no action needed).
- Port 5432 is held by a WSL PostgreSQL relay, so Elide uses 5433 (ADR-0003).
- LLM provider/model/key not chosen (open question 9, Phase 3).

### Owner actions required
1. ~~Create Sandbox accounts + app, fill `.env`~~ (done by owner 2026-11-10).
2. ~~Approve sending the existing draft `INV2-4RUV-XPEH-4F3C-T8BW`~~ (approved and sent 2026-11-10).
3. ~~Pay the hosted invoice as the Sandbox Personal account~~ (paid 2026-11-10).
4. **Decide finding F1 (CAD settlement)** before P1 (see *Open findings*).
5. Approve GitHub repository name/visibility so CI can run (last P0 item).

### Remaining P0 gate requirements
- [x] OAuth token obtained server-side (redacted evidence)
- [x] Invoicing v2 accessible for the owned Sandbox merchant
- [x] Draft invoice created; provider-computed total observed (discount behavior recorded: returned negative, total 99000)
- [x] Invoice sent; hosted payer link observed (owner-approved; status `SENT`; link from PayPal's send response)
- [x] Sandbox payer completes payment; provider GET confirms amount/currency/status (PAID, USD 990.00, 1 transaction `4J016440MV5285528`, remaining 0)
- [x] Redacted evidence + commit SHA recorded here (all PayPal steps)
- [x] (if any step returns 403/unsupported) blocker recorded: no 403; one 415 on numbering, cause found and fixed. Orders v2 fallback not needed so far
- [ ] CI run green on GitHub (after the remote exists)

The old `--create-draft --send` combination was removed. Sending is only possible with
`--send-invoice-id`, which never creates an invoice (tested in
`tests/unit/test_paypal_send_script.py`).

### Open findings (owner decision; no design change made)
- **F1: Sandbox merchant settles in CAD.** The USD invoice was paid in USD, but PayPal
  converted the net (US$960.99) to CAD 1,306.79 at 1.35984225. D008 fixes one currency
  (USD cents) for the demo, and cross-currency is deferred (14_BUILD_ROADMAP R5). If
  Elide models this merchant's spendable capacity, it would not be USD. Options:
  (a) create a Sandbox Business account with country **United States** (USD default)
  and a new Sandbox app linked to it, then re-run the auth/access checks; (b) keep this
  account and treat its capacity as non-USD/unsupported; (c) check whether the existing
  Sandbox account can hold a USD balance (not verified). Recommended: (a), as it matches
  D008 without FX logic. Not a P0 blocker: the invoice payment route itself is proven.
- **F2: PayPal fee is charged to the merchant.** US$29.01 on US$990.00 (net US$960.99).
  P1's fee model and reserve checks must use provider-reported fees, not assumptions.

## Release claim categories
- **Specified**: exists in architecture/docs only.
- **Synthetic-tested**: program ran on invented data; not proof of merchant results.
- **Sandbox-verified**: independently fetched PayPal resource from actual sandbox operation.
- **Pilot-validated**: real consenting merchant evidence with appropriate comparison (outside current scope).

## Critical blockers to monitor
- PayPal Invoicing account scope, hosted payer completion and discount updates.
- Sufficient time for independent tests and public proof.
- Safe data and merchant authorizations; no cross-tenant disclosure.
- Stable access for judges through December 15 (free Render database expiration issue).

## Session log
- 2026-11-10 (Claude Code, Opus 5.5): P0 bootstrap. Scaffolded repo, backend, frontend,
  DB, PayPal adapter + check script, and agent instructions. All local checks green
  (see table). Bootstrap commit: `16c7276` ("P0: bootstrap repository", local only,
  not pushed). Stopped at the P0 gate, blocked on owner PayPal Sandbox credentials.
  **Next task:** once `.env` has Sandbox credentials, run `scripts/paypal_sandbox_check.py`
  steps 1→5, record results here, then have the owner pay the hosted invoice.
- 2026-11-10 (Claude Code, Opus 5.5): PayPal Sandbox steps 1–3 and 5 run against the
  owner's Sandbox Merchant app. OAuth 200, Invoicing list 200, one draft created (201,
  `INV2-4RUV-XPEH-4F3C-T8BW`), independent GET 200 with all terms matching (total 99000
  cents). Found and fixed: discount returned with negative sign (parser), 415 on
  generate-next-invoice-number (missing JSON content type). Tests: ruff clean, pytest
  52 passed. Stopped before send/payment as instructed. Commit: see `git log` ("P0:
  Sandbox-verify OAuth, Invoicing access and draft invoice").
  **Next task:** with owner approval, add a send-by-id option and send
  `INV2-4RUV-XPEH-4F3C-T8BW`; owner pays in the hosted page; verify with
  `--invoice-id INV2-4RUV-XPEH-4F3C-T8BW --expected-cents 99000 --expected-status PAID`.
- 2026-11-10 (Claude Code, Opus 5.5): owner approved sending `INV2-4RUV-XPEH-4F3C-T8BW`.
  Added `invoice_ops.py` (due-date full update, pre-send checks, payer-link extraction)
  and script modes `--set-due-date` / `--send-invoice-id` (commit `c80173d`; pytest 70
  passed, ruff clean). Due date set to 2026-12-01 via PUT (200), re-fetched: all terms
  unchanged. Pre-send checks passed, sent (200), re-fetched: `SENT`. Payer link from
  PayPal's send response saved to git-ignored `evidence/paypal-sandbox/raw/`. Stopped for
  the owner's manual Sandbox payment.
  **Next task:** after the owner pays, run
  `uv run python scripts/paypal_sandbox_check.py --invoice-id INV2-4RUV-XPEH-4F3C-T8BW --expected-status PAID --expected-due-date 2026-12-01`
  and record status, paid amount, currency and remaining balance here.
- 2026-11-10 (Claude Code, Opus 5.5): owner paid the Sandbox invoice in the browser.
  Independent provider GETs: invoice `PAID`, US$990.00 paid, 1 SUCCESS transaction
  `4J016440MV5285528`, remaining 0; capture `COMPLETED`, fee US$29.01, net US$960.99,
  receivable CAD 1,306.79 (rate 1.35984225). Added strict PAID checks (exactly one
  SUCCESS transaction = total), read-only `--payment-id` capture lookup, `merchant_id`
  redaction and a reconciliation record. No ledger posting (P1 not built). PayPal
  payment gate met; P0 remains partial only for the GitHub CI run.
  **Next task:** owner decides F1 (CAD settlement) and approves a GitHub remote; run CI
  and hold the P0 review before any P1 work.
