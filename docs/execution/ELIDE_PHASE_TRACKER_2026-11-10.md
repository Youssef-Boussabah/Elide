# ELIDE — Phase Gate Tracker
**State (2026-11-11):** P0 **complete**. P1 **complete** (merged `41c0f9f`). P2 **complete**: owner-approved at `04663db` (CI run #11) for the synthetic research scope, merged to `main` at `7bb068c`. P3 **complete**: owner-approved at `dc5949e` (CI run #17) for the tested synthetic and real-LLM-inference scope (not production financial execution), merged to `main` at `3100091` (CI run #18 green). P4 **complete**: owner-approved at `7d971d9` (CI run #21) for the tested synthetic scope, merged to `main` at `1ed309a` (CI run #22 green). P5 **implemented on `feat/p5-paypal-execution` (synthetic-tested; read-only Sandbox-verified), conditionally approved by the owner for the mock-tested architecture (review of `613d72f`, CI run #25); gate-review corrections applied; the controlled Sandbox payment is NOT authorized or performed**. Maintain this tracker in Git; update with commit, tests, redacted evidence and blocker notes.

| Phase | Gate | Status | Evidence to attach |
|---|---|---|---|
| P0 | API access + hosted Sandbox invoice payment route proven | **Complete** (owner-reviewed): payment route Sandbox-verified (invoice PAID); public repo + CI green ([run #1](https://github.com/Youssef-Boussabah/Elide/actions/runs/42916020788)) | Redacted API responses + provider IDs/status + code SHA |
| P1 | Money model, reserves, fees, revisions and fixtures pass | **Complete** (owner-approved 2026-11-11; merged `41c0f9f`): 329 tests pass (0 skipped), incl. 16 Hypothesis properties and 39 PostgreSQL integration tests; CI run #7 on `main` green | pytest / property test output |
| P2 | Exact optimizer agrees with separate verifier and fair baselines | **Complete** (owner-approved `04663db`, CI run #11; merged `7bb068c`): 487 tests pass (0 skipped); solver = brute force on 640/640 checks; B0–B5 benchmark in `evidence/phase2/` | B0–B5 test table + seed/trace |
| P3 | Live policy parsing and bounded offer proposal safely gated | **Complete** (owner-approved `dc5949e`, CI run #17; merged `3100091`, CI run #18): real Nebius inference (Qwen3-235B), 0 unsafe passes on 40 labelled development cases; 633 tests pass | Prompt version + schema result + injection tests |
| P4 | Separate merchant sessions, approvals, permissioned UI | **Complete** (owner-approved `7d971d9`, CI run #21; merged `1ed309a`, CI run #22): 689 backend, 18 frontend, 5 Playwright tests pass; synthetic scope only, outbox held | E2E run and auth isolation test |
| P5 | Actual PayPal payer payment + provider GET + safe replay | **Implemented, gate pending** (see P5 detail): execution state machine, durable outbox worker, PayPal adapter, reconciliation and booking; owner gate review corrected (automatic cancellation off for the pilot; stable `PayPal-Request-Id`); 782 backend / 26 frontend / 9 Playwright pass; read-only Sandbox probe passed. **Missing:** one owner-authorized Sandbox payment (A13) and a verified webhook (A14) | Redacted receipt + journal + webhook replay |
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

**Not executed at bootstrap:** GitHub Actions CI and PayPal calls (both done later, see
below). Playwright/E2E: not set up (P4/P6).

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

Commit SHAs below are pre-rewrite; see `evidence/COMMIT_SHA_MAP_2026-11-10.md`.
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

**Reconciliation (Sandbox-verified, derived only from provider GETs):** invoice total 99000 = paid 99000 = single transaction 99000 = capture gross 99000; remaining 0; gross − fee = 99000 − 2901 = 96099 net; 960.99 × 1.35984225 = 1306.7948 → CAD 1,306.79. **No posting was made**: no ledger exists yet. One provider transaction is a provider observation, **not** duplicate-posting protection: Elide's ledger and its idempotency tests are P1 work and are not implemented. Proposed P1 idempotency key: `paypal_capture:4J016440MV5285528`. Re-running verification only re-reads PayPal and writes a new run file. Not established: that funds are spendable (D006), or that the merchant holds US$990.00 (it was credited US$960.99 net, converted to CAD).

**Browser vs provider:** the hosted page (7a) and the owner's browser confirmation (8) are browser observations. The PAID status, amounts and transaction come only from the provider GETs (9, 10).

**Timestamps (UTC, as returned):** invoice transaction `payment_date_time` = `2026-11-10T14:15:49Z`; capture `create_time` = `2026-11-10T22:15:48Z`; both are labeled UTC and differ by 7 h 59 min 59 s. Cause **not determined** (investigation item INV-1). Both raw values are preserved; neither is altered.

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
4. ~~Decide F1 (CAD settlement)~~ (decided 2026-11-10, see *Owner decisions*).
5. ~~Approve GitHub repository~~ (approved 2026-11-10: public `Elide`, MIT, `main`).

### Remaining P0 gate requirements
- [x] OAuth token obtained server-side (redacted evidence)
- [x] Invoicing v2 accessible for the owned Sandbox merchant
- [x] Draft invoice created; provider-computed total observed (discount behavior recorded: returned negative, total 99000)
- [x] Invoice sent; hosted payer link observed (owner-approved; status `SENT`; link from PayPal's send response)
- [x] Sandbox payer completes payment; provider GET confirms amount/currency/status (PAID, USD 990.00, 1 transaction `4J016440MV5285528`, remaining 0)
- [x] Redacted evidence + commit SHA recorded here (all PayPal steps)
- [x] (if any step returns 403/unsupported) blocker recorded: no 403; one 415 on numbering, cause found and fixed. Orders v2 fallback not needed so far
- [x] CI run green on GitHub: [run #1](https://github.com/Youssef-Boussabah/Elide/actions/runs/42916020788) on `63473d8`, backend + frontend success

The old `--create-draft --send` combination was removed. Sending is only possible with
`--send-invoice-id`, which never creates an invoice (tested in
`tests/unit/test_paypal_send_script.py`).

### P0 exit review (2026-11-10)

**Repository:** https://github.com/Youssef-Boussabah/Elide (public, MIT detected by GitHub,
default branch `main`). Created by the agent in Chrome with no auto-generated files; pushed
with Git. Pre-publication audit: all blobs in all commits scanned for the real client id,
secret, Sandbox payer email/local part, payer link, merchant id, token/key patterns and
email-like strings: **0 real hits** (only synthetic test values and lockfile `name@version`
strings). `.env` and `evidence/**/raw/` never tracked. Author/committer email rewritten to
the GitHub noreply address before the first push (owner decision; trees unchanged; map in
`evidence/COMMIT_SHA_MAP_2026-11-10.md`). Local Windows user paths in the environment audit
replaced with `%USERPROFILE%`. Older commits still contain those paths (username only; no
secret).

**CI:** [run #1](https://github.com/Youssef-Boussabah/Elide/actions/runs/42916020788), commit `63473d8bb2ab0bb8296e8443ee78709f80f6cb67`, push to
`main`, started 2026-11-10T22:35:48Z, conclusion **success** (32 s).

| Job | Steps (all success) | Result |
|---|---|---|
| backend | `uv sync --locked`, `ruff check`, `ruff format --check`, `alembic upgrade head` (Postgres 16 service), `pytest` | **77 passed, 0 skipped**, 1 warning (known Starlette deprecation) |
| frontend | `pnpm install --frozen-lockfile`, `typecheck`, `lint`, `test`, `build` | Vitest **3 passed**; build pass |

**Clean clone (2026-11-10, Windows, `git clone` of the public repo into a scratch dir):**
109 tracked files, no `.env` shipped. Followed README: `uv sync --locked` ✓, `cp .env.example
.env`, `ruff check` / `format --check` ✓, `alembic upgrade head` ✓, `pytest` **77 passed**,
`uvicorn` → `GET /health` 200, `GET /health/ready` 200 (`database: ok`, PostgreSQL 16.15),
`pnpm install --frozen-lockfile` ✓, `typecheck` / `lint` ✓, Vitest 3 passed, `build` ✓,
`pnpm dev` → `GET localhost:5173/` 200 with `#root`, proxy `GET /api/health/ready` 200.
Caveat (resolved in P1: `container_name` removed from `compose.yaml`): the clone used the already-running `elide-db` container on 5433 instead of its own
`docker compose up`, because `compose.yaml` fixes `container_name: elide-db` and two
checkouts on one machine would clash. A fresh machine would run `docker compose up -d --wait
db` as documented (not separately re-tested here).

**Gate evidence (all eight items):**

| # | Gate item | Evidence | Class |
|---|---|---|---|
| 1 | OAuth token server-side | run `20261110T214449Z`, HTTP 200, invoicing scope | Sandbox-verified |
| 2 | Invoicing v2 accessible to the owned merchant | same run, `GET /v2/invoicing/invoices` 200 | Sandbox-verified |
| 3 | Draft created; provider total and discount behavior observed | run `20261110T214515Z` (201) + `20261110T214717Z`; total 99000, discount returned `-10.00` | Sandbox-verified |
| 4 | Invoice sent; hosted payer link observed | run `20261110T220152Z`; send 200, `SENT`, `rel=payer-view` link (git-ignored) | Sandbox-verified |
| 5 | Payer completes payment; provider GET confirms | runs `20261110T221823Z`, `20261110T221827Z`; `PAID`, USD 990.00, 1 transaction, remaining 0 | Sandbox-verified (payment itself: owner browser action) |
| 6 | Redacted evidence + commit SHA recorded | `evidence/paypal-sandbox/runs/*.json`, reconciliation record, SHA map | Repository |
| 7 | 403/unsupported blockers recorded; fallback evaluated | no 403; 415 on numbering fixed; Orders v2 fallback not needed | Sandbox-verified |
| 8 | CI green on GitHub | [run #1](https://github.com/Youssef-Boussabah/Elide/actions/runs/42916020788) | CI |

**Remaining items (not P0 blockers):**
- INV-1 timestamp discrepancy (open investigation).
- `compose.yaml` fixed container name prevents two checkouts running side by side (minor).
- Starlette/httpx `TestClient` deprecation warning (ADR-0004).
- GitHub shows `claude` as a contributor because commits carry a `Co-Authored-By` trailer.
- Local branch `backup/pre-noreply-rewrite` holds the pre-rewrite commits (contains the
  owner's personal email in metadata). **Not pushed.** Delete locally once the owner is
  satisfied.
- Not implemented (by design, P1+): money model, ledger, idempotent posting, webhooks,
  planner, AI, UI. One provider transaction is **not** duplicate-posting protection.

**Recommended P1 starting tasks** (execution plan Phase 1, D006/D008, ADR-0007), each with
tests first:
1. Money value types in `api/domain/`: integer USD cents, currency guard rejecting non-USD
   as capacity, no floats; Hypothesis properties.
2. Invoice identity + immutable revisions (original amount, discount, due date in UTC +
   merchant TZ) and the settlement identity
   `original_open = cash_applied + authorized_concession + valid_noncash_setoff + remaining_open`.
3. Capacity model with observed / held / attested / unknown classes and
   `deployable = max(0, verified_spendable − protected_reserve − committed)`; incoming
   receivables add zero until verified; provider `PAID` never becomes spendable (D006).
4. Provider settlement record type carrying gross, fee, net, settlement currency/amount,
   FX rate and provenance separately (fixture: capture `4J016440MV5285528`).
5. Append-only balanced journal (`api/accounting/`) with Alembic migration and an
   idempotency key per provider event (e.g. `paypal_capture:<id>`): replay posts once.
   Integration tests against PostgreSQL.
6. Seed fixtures: canonical buyer $1,490 / $500 reserve / two $990 choices / $900 + $800
   downstream; AI negotiation scenario $5 → $10 discount; unit cases held $990 receipt,
   supplier $150 reserve shortfall $60, duplicate obligation.

### Owner decisions (2026-11-10)
- **F1 (currency):** keep the existing Canadian Sandbox Business account and its payment
  evidence. The initial optimizer stays USD-only (D008). The CAD receipt is **not**
  US$990 of spendable USD. Settlement data is preserved field by field with provenance
  (see *Settlement record*). A US Sandbox Business account may be created later for
  USD-only integration tests; not created now, and it does not delay P1. See ADR-0007.
- **F2 (GitHub):** create a public repository `Elide` under the owner's authenticated
  personal account (`Youssef-Boussabah`), MIT license, default branch `main`, after a
  security audit of tracked files and history.

### Settlement record: capture `4J016440MV5285528` (Sandbox-verified)
| Field | Value | Provenance |
|---|---|---|
| Gross | USD 990.00 (99000 cents) | capture `seller_receivable_breakdown.gross_amount`, run `20261110T221827Z` |
| PayPal fee | USD 29.01 (2901 cents) | `...paypal_fee`, same run |
| Net (USD-equivalent proceeds) | USD 960.99 (96099 cents) | `...net_amount`, same run |
| Actual settlement credited | CAD 1,306.79 | `...receivable_amount`, same run |
| Exchange rate | 1.35984225 USD→CAD | `...exchange_rate`, same run |
| Invoice paid amount | USD 990.00 | invoice `payments.paid_amount`, run `20261110T221823Z` |
| Spendable USD for downstream legs | **not established** | D006; settlement is CAD |

### Findings
- **FIN-1: fees reduce available proceeds.** US$29.01 on US$990.00 leaves US$960.99.
  P1 fee model and reserve checks must use provider-reported fees, not assumptions.
- **FIN-2: FX settlement creates a currency mismatch.** The payer paid USD, the
  merchant was credited CAD. This invalidates any assumption that a received USD
  payment can immediately fund a downstream USD payment.
- **INV-1 (investigation): timestamp discrepancy.** Invoice transaction time and capture
  time differ by 7 h 59 min 59 s while both are labeled UTC. Cause not determined.
  Until resolved, record both raw values and do not use either for ordering-critical logic
  without noting the source.

## P1 detail (2026-11-10/11, branch `feat/p1-financial-kernel`)

Base: `main` = `21047fecb9f27278891e5dd1831537274ab00e01` (owner-reviewed P0). Design choices:
ADR-0007 (currency), ADR-0008 (kernel). Evidence classes: **Synthetic-tested** for fixtures
and logic; the settlement record and its postings are built from **Sandbox-verified**
evidence (runs `20261110T221823Z`, `20261110T221827Z`). No PayPal call was made in P1.

### Implemented
| Area | Module | What it enforces |
|---|---|---|
| Money | `api/domain/money.py` | `Money` = nonnegative int cents + currency; `SignedAmount` = nonzero journal movement (distinct type); `ExchangeRate` = Decimal, half-up to the cent; USD-only `require_spendable`, CAD allowed as a fact; no floats, no cross-currency arithmetic |
| Invoices | `api/domain/invoice.py` | Immutable `Obligation` identity and face; append-only numbered revisions (no backdating, conflicting revision → error); concession counts only when AUTHORIZED by debtor **and** creditor; drafts and expired drafts never count; cash applied, partials, reversals (exact, never beyond the payment, no reversal of a reversal); over-settlement refused; identical replays idempotent; `original_open = cash_applied + authorized_concession + remaining_open` checked on every position; duplicate original identity (`ObligationRegistry`, incl. provider reference) |
| Capacity | `api/liquidity/capacity.py` | `deployable = max(0, raw)`, `deficit = max(0, −raw)`, `raw = spendable + verified incoming − reserve − committed`; validity window; STALE / NOT_YET_OBSERVED / UNVERIFIED / CURRENCY_BLOCKED deploy zero; held / pending / promised / provider-confirmed (`PAID`) add zero; verified receipt only strictly after it is available; no double counting of receipts or commitments; payment assessment with liquidity gap and explanation |
| Settlement evidence | `api/domain/settlement.py`, `api/integrations/paypal/settlement.py`, `api/liquidity/provider_receipts.py` | Gross, fee, net, CAD credit and rate kept separately and cross-checked; exactly one cash receipt; CAD never USD capacity; disputed timestamp (INV-1) preserved, never used for ordering |
| Simulation | `api/liquidity/simulation.py` | SYNTHETIC zero-lag sequential transfers; reserve after every leg incl. fees; conservation |
| Journal | `api/accounting/journal.py`, `settlement_postings.py` | Per-currency balanced entries; evidence class; immutable provider refs; content digest; exact-negation reversals. Sandbox capture booked as one entry: USD clearing nets to 0, one CAD 1,306.79 balance, USD 29.01 fee expense, USD 10.00 discount |
| Persistence | `api/accounting/store.py`, `api/liquidity/store.py`, migration `0002_financial_kernel` | Append-only tables (DB triggers), deferred per-currency balance check, idempotency-key + provider-event unique index, separate inbox dedup, reversal uniqueness, per-merchant advisory lock for capacity reservations, one transaction per operation |
| Fixtures | `fixtures/canonical.py` (`canonical-v1`) | Scenarios A–D, synthetic; E in tests |

**Not implemented (by design or later phase):** webhook worker / signature verification,
obligation persistence tables, overpayment handling, partial-payment planning, FX handling
of any kind, tenant/membership authorization (P4), planner/verifier (P2), AI (P3).
Duplicate-posting protection exists for the journal; it has not been exercised by real
webhooks (none are received yet).

### Verification (2026-11-11, Windows 11, local PostgreSQL 16.15 at `127.0.0.1:5433`)
| Check | Command | Result |
|---|---|---|
| Deps | `uv sync --locked` | pass (no new dependencies) |
| Lint / format | `uv run ruff check .` / `uv run ruff format --check .` | pass / pass (95 files) |
| Migrations | `alembic upgrade head` on dev DB → `0002_financial_kernel`; down to `0001` and up again on a throwaway DB (test) | pass |
| Unit | `uv run pytest tests/unit` | **211 passed** |
| Property | `uv run pytest tests/property` | **11 passed** (+1 Hypothesis property in `tests/unit/accounting`) |
| Contract (PayPal adapter, mock transport, no network) | `uv run pytest tests/contract` | **11 passed** |
| Integration (PostgreSQL, throwaway DB) | `uv run pytest tests/integration` | **26 passed, 0 skipped** |
| Full backend | `uv run pytest` | **259 passed, 0 failed, 0 skipped**, 1 known warning |
| Agent instructions consistency | `tests/unit/test_repository_hygiene.py` | 3 passed |
| Frontend | `pnpm typecheck`, `lint`, `test`, `build` | pass, 3 passed, build ok |

Ledger / idempotency evidence (integration tests):
- real Sandbox settlement: 3 entries POSTED, replay → 3 DUPLICATE (same ids); trial balance 0
  per currency; AR, USD clearing and USD cash all 0; CAD balance 130679; fee 2901;
- same capture under another key or entity → refused; same key with changed content → conflict;
- 8 concurrent identical posts → 1 POSTED + 7 DUPLICATE, 2 posting rows;
- 6 concurrent posts of one provider event under different keys → 1 posted, 5 refused;
- crash after the entry header → nothing persisted; clean retry posts once;
- unbalanced entry / entry without postings inserted with raw SQL → refused at COMMIT;
- UPDATE / DELETE / TRUNCATE on journal tables → refused;
- reversal compensates (balances 0, both entries kept), second reversal refused, partial or
  missing-target reversal refused;
- inbox: duplicate webhook recorded once (also under 6 concurrent deliveries), changed payload
  → conflict;
- 6 concurrent plans reserving USD 990 from USD 990 deployable → exactly 1 succeeds; release
  frees once; commitments cannot be deleted or edited; CAD evidence cannot be reserved.

### Infrastructure changes
- `compose.yaml`: fixed `container_name` removed. The running container was recreated by
  `docker compose up -d --wait db` as `elide-db-1` on the **same volume** `elide_elide-pgdata`
  (alembic version preserved). No volume was deleted.
- Default DB host `127.0.0.1` (`.env.example`, `api/config.py`, tests). Measured on this
  machine: `127.0.0.1` 0.01 s, `localhost` 4.1 s (tries `::1` first and times out), which
  made Alembic hang without a timeout. `migrations/env.py` now sets `connect_timeout=5`.
  **Owner action (optional):** the untracked local `.env` still has `localhost:5433`;
  change it to `127.0.0.1:5433` to avoid a ~4 s delay per new connection.
- Two leftover throwaway test databases from the hung runs were dropped (the dev `elide`
  database was not touched except for the additive `0002` upgrade).

### P1 acceptance (execution plan Phase 1)
| Criterion | Status | Evidence |
|---|---|---|
| Independently checked invariants | met | settlement identity, capacity partition, conservation, per-currency balance: unit + Hypothesis + DB triggers |
| Cash conservation in fee-free example | met | `test_a_fee_free_chain_conserves_cash_doc04_figures` + property test |
| No reduction below reserve | met | simulation + capacity + reservation tests, adverse cases |
| No unpaid invoice mislabeled paid | met | status derived from `remaining_open`; draft discount leaves $10 open; PAID ≠ spendable |
| No hypothetical incoming balance treated as spendable | met | receipt-status tests, Scenario C, CAD settlement |
| Held $990 receipt; $150 reserve shortfall $60; duplicate obligation | met | Scenarios C, D, E |
| Ledger idempotency / concurrency / rollback | met | integration tests listed above |
| CI green on the branch | met | [run #3](https://github.com/Youssef-Boussabah/Elide/actions/runs/42923590785) on `daabed964dcada9ee7e18c89212a6bf82a9e9e2a`: backend **259 passed, 0 skipped** (migration 0002 applied to the CI PostgreSQL service), frontend 3 passed + build |
| Owner architectural review | **approved** after remediation (2026-11-11), scope: synthetic simulation and deterministic financial model; not production payment execution | *P1 review remediation* and *P1 approval and merge* below |

### P1 review remediation (2026-11-11)

Owner review of `91b8152`: strong implementation, conditional approval. Every finding was
reproduced against that commit before it was fixed; regression tests were written first
and failed (19 failed + 3 modules not importable) before the fixes. Reproduction output:
`evidence/phase1/review_reproduction_2026-11-11.txt`. Design record: ADR-0008 addendum
(decisions 13–20). Migration **`0003_review_remediation`** (new revision; 0002 unchanged).

| # | Finding | Reproduced | Root cause | Fix |
|---|---|---|---|---|
| 1 | Critical: duplicate receipt counted twice | yes: $990 twice → $1,980 deployable | `compute_capacity` summed every record; no receipt identity | receipts keyed by (merchant, id); identical copies collapse; differing records → `ReceiptConflictError`; `mark_available` replaces the held record |
| 2 | High: evidence round trip / older snapshot | yes: included ids lost; identical replay rejected; $900 reserved on an older snapshot after a newer $10 one | no column for `included_receipt_ids`; no selection rule | `0003` adds immutable `included_receipt_ids`; `EvidenceConflictError`; latest observation per (merchant, currency) is authoritative, older ids refused (`SupersededEvidenceError`); one observation per instant |
| 3 | High: status alone proved spendability | yes: synthetic and "PAID" receipts with the status counted $990 | the enum value was the only check | `AvailabilityEvidence` required (SYNTHETIC only in simulation / MERCHANT_ATTESTED by the receiving merchant / PROVIDER_BALANCE), must differ from the payment confirmation; evidence kind must match provenance source |
| 4 | High: offer expiry vs accepted terms | yes: a rejected $5 offer was "accepted" as $90 | `supersedes` only checked draft expiry | acceptance must target an open, unexpired offer with identical terms; rejected/accepted/countered offers closed; accepted terms never expire; `expires_at` only on offers |
| 5 | High: local reversal looked provider-verified | yes: copied `sandbox_verified` and the capture ref | `reversal()` inherited class and ref | explicit `evidence_class`; new `manual_adjustment`; provider-verified reversal needs a refund/dispute ref ≠ original; synthetic ↔ real refused; store re-checks; one refund backs one reversal |
| 6 | Medium: non-atomic settlement import | yes (by construction: three separate transactions) | `post_entry` per entry | `post_entries` (one transaction) + `import_settlement`; status derived (`NOT_IMPORTED`/`PARTIAL`/`COMPLETE`/`CONFLICT`), no stored flag |
| 7 | Terminology | yes: P1 exit report said every account but CAD is zero | wording | docstrings and entry description: illustrative bookkeeping, no revenue-recognition claim, FX position ≠ cash, one receipt; test asserts the exact non-zero balances |
| + | Found during fix 6: concurrent replay race | yes: 2 of 8 runs raised `DuplicateProviderEventError` instead of DUPLICATE | `ON CONFLICT` targeted only the idempotency key | every unique index is an arbiter, conflict classified afterwards; 15/15 stress runs pass |

New regression tests: `tests/unit/liquidity/test_receipt_identity_and_evidence.py` (27 incl.
parametrized cases), `tests/unit/domain/test_revision_lifecycle.py` (15),
`tests/unit/accounting/test_reversal_evidence.py` (11), `tests/property/test_receipt_and_evidence_props.py`
(3 Hypothesis), `tests/property/test_revision_props.py` (1 Hypothesis, 400 examples),
`tests/integration/test_review_remediation_db.py` (13, each in its own throwaway database).
Existing tests adjusted only where they used labels the new rules reject (merchant-attested
evidence with synthetic or PayPal provenance; verified receipts without availability
evidence; reversals without an evidence class).

Verification after remediation (2026-11-11, Windows 11, PostgreSQL 16 at `127.0.0.1:5433`):
| Check | Result |
|---|---|
| `uv sync --locked` | pass (no new dependencies) |
| `ruff check .` / `ruff format --check .` | pass / pass (103 files) |
| Dev DB `alembic upgrade head` | `0002` → `0003_review_remediation` (additive; tables were empty) |
| Migration round trip head → 0001 → head (throwaway DB) | pass |
| `pytest tests/unit` | **264 passed** |
| `pytest tests/property` | **15 passed** (+1 Hypothesis property in `tests/unit/accounting`: 16 total) |
| `pytest tests/contract` | **11 passed** |
| `pytest tests/integration` | **39 passed, 0 skipped** |
| `pytest` (full) | **329 passed, 0 failed, 0 skipped**, 1 known Starlette warning |
| Frontend `typecheck` / `lint` / `test` / `build` | exit 0 / exit 0 / 3 passed / ok |
| GitHub Actions | [run #5](https://github.com/Youssef-Boussabah/Elide/actions/runs/42934762564) on `bf3ad8b`: success; migrations 0001→0002→0003 applied to the CI PostgreSQL service; backend **329 passed, 0 skipped** (1 known warning); frontend success |

Remaining risks after remediation:
- Incoming receipts are not persisted; callers supply them and the engine checks them.
  Receipt lifecycle storage and a real available-balance check (`PROVIDER_BALANCE`) are P5.
- Commitments stay active across newer evidence (conservative: may double-subtract cash
  already spent). P2 must release commitments when an outflow is reflected in a newer
  observation.
- Accepted discounts are unconditional amendments; a lapsing "pay by X" discount is not
  modeled. P2 should only create authorized amendments as part of an approved plan.
- Provider refunds: no partial-refund entry kind and no refund GET verification; no code
  path creates `sandbox_verified` reversals yet.
- Webhook worker, signature verification, tenant authorization: still P4/P5. INV-1
  timestamp cause still unknown. Starlette deprecation warning unchanged.

### P1 approval and merge (2026-11-11)

- Owner approval: P1 approved for the synthetic simulation and deterministic
  financial-model scope. **Not** an authorization for production payment execution.
- Pre-merge checks: `feat/p1-financial-kernel` = `3af748d60f0ae8b6daffdf66a15baec974b4bc1c`
  (local and `origin`), working tree clean, CI run #6 on `3af748d` green.
- Merge: `git merge --no-ff feat/p1-financial-kernel` into `main` →
  **`41c0f9f0af5890a29fd41bcc63f75b469c177fbc`** (no rebase, no history rewrite; feature
  branch kept; Sandbox evidence unchanged). Pushed `21047fe..41c0f9f`.
- CI on `main`: run #7 (id 42936778766) on `41c0f9f`: **success** (same suite as run #5:
  329 passed, 0 skipped; migrations through `0003`).

### Dependencies for P2
Planner and replay verifier consume: `InvoiceRecord.position()` (open amounts, authorized
concessions, due dates), `compute_capacity` / `assess_outflow` (deployable, deficit,
explanations), `Commitment` + `reserve_capacity` (cash reservation per plan),
`simulate` (reference replay for synthetic chains), `fixtures/canonical.py` (B0–B5
scenarios), and the journal for realized outcomes. Since the review: `current_evidence` /
`SupersededEvidenceError` (only the newest balance snapshot may fund a plan),
`IncomingReceipt` + `AvailabilityEvidence` + `mark_available` (one state per receipt),
`ReceiptConflictError`, `post_entries` / `import_settlement` (atomic bundles). Open items
P2 must decide: slot/lag model for verified availability, how plan reservations expire
and are released when a newer observation reflects the outflow, fee model per leg.

## P2 detail (2026-11-11, branch `feat/p2-liquidity-optimizer`)

Evidence class of every P2 number: **Synthetic-tested** (simulation), except two labelled
Sandbox facts reused from P0: the CAD 1,306.79 settlement credit and the USD 29.01 fee (an
example, not a fee model). No PayPal call, no invoice change, no merchant approval, no
credential change. Design record: **ADR-0009**. No database migration (planner is pure).

### Implemented (`api/planning/`)
| Module | What it does |
|---|---|
| `network.py` | Directed multigraph: merchants, immutable obligation edges (parallel invoices kept, duplicate ids / provider refs rejected), candidate legs per terms (in force / open offer / negotiated proposal), slot clock, lag ≥ 1 (same-slot reuse unrepresentable), consent basis (synthetic assumption / recorded decision / refused), fee terms (payer debit, payee deduction, conditional ceiling, unknown), separate available / reserve / committed / inflow / held / pending / unverified / currency-blocked cash |
| `builder.py` | Networks from P1 objects: `compute_capacity` positions (one receipt counts once, held = 0), the Sandbox settlement (CAD blocked, D006/D008), invoice revisions → legs; exclusions with reasons |
| `solver.py` | Exact DP over time slots with memoised states; objectives NETWORK / FOCAL / MIN_LIQUIDITY; status OPTIMAL only for a complete search, FEASIBLE / UNKNOWN on the node limit, INFEASIBLE only after a complete search |
| `verifier.py` | Separately coded replay: per-leg rules, reserve after every debit, availability, invoice identity, deadlines, terms, consent (no merchant signs for another), incentives, conservation, own-funds draw vs hypothetical funding |
| `objective.py` | One lexicographic definition: (lead, on-time face, surplus, −early cash, −cash×time, −fees), then smallest schedule |
| `baselines.py` | B0–B5 on the identical network; B3 exact expectation over tied buyer choices |
| `plan.py` | Deterministic `elide.plan.v1` record (`status: SIMULATED`, `may_execute: false`), per-merchant views, `verify_plan_record`, `plan_commitments` |
| `reference.py`, `generator.py` | Brute-force reference; seeded synthetic families (`p2-gen-1`) |

Also: `fixtures/planning.py` (`canonical-p2-v1`, built from the P1 fixtures),
`scripts/run_planner_benchmarks.py`, read-only `InvoiceRecord.open_offers(at)`,
`ix_journal_posting_entry` declared in metadata (pre-existing `alembic check` drift from
P1; no schema change) and `alembic check` added to CI.

### Canonical results (exact solver, verified by the independent replay)
| Scenario | Result |
|---|---|
| A: buyer $1,490 / reserve $500; two $1,000 invoices with $10-off offers; food → freight $900 → repair $800 | OPTIMAL: food offer @ slot 0, freight @ 28, repair @ 29 (latest slots that keep the mechanic on time). On-time face $2,700; gross flow $2,690; downstream face $1,700; closing cash 500 / 90 / 0 / 100 / 800 = $1,490 conserved. Buyer: $10 discount − $6.30 time cost (8%/yr hurdle, 29 days) = **+$3.70**; supplier −$10 concession |
| A baselines | B0 $0 (cannot pay $1,000 at due) · B1 $2,700 (id order happens to pick food) · B2 $2,700 (deterministic tie-break) · **B3 $1,850** (expected over the 2 tied choices) · B4 $2,700 · B5 $2,700 (no negotiated change). **Honest increment: +$850 vs B3, $0 vs B1/B2** |
| B: $5 offer, buyer needs ≥ $10 | B0–B4: nothing ($995 > $990 deployable and $5 < minimum). B5 with the $10 proposal assumed signed by both: $2,700 on time; increment over B4 +$2,700 from a **different feasible set** |
| C: supplier $990 held, reserve $150 | INFEASIBLE for freight; held funds count for zero; minimum HYPOTHETICAL funding $1,050 |
| D: supplier $990 available, reserve $150, freight $900 | INFEASIBLE: "needs USD 900.00 + USD 150.00 reserve, has USD 990.00 available at slot 0: short by USD 60.00"; existing $840 + hypothetical $60 |
| D′ / F: USD 29.01 fee (hypothetical USD net $960.99, or deducted from the $990 payment) | short by **USD 89.01** |
| Payer-side fee | buyer needs USD 1,019.01 + USD 500.00 reserve: short by USD 29.01 |
| E: real Sandbox CAD 1,306.79 credit | cannot fund USD freight (currency-blocked, D008); hypothetical USD gap $900 |
| Minimum liquidity | food → freight → repair needs **$990** of existing buyer cash; A→B→C→A $100 loop needs a $100 hypothetical seed (noncash set-off would need $0 but is out of scope, D011) |
| Limits | 10 candidate legs / 5 slots solve OPTIMAL; with `max_nodes=50` the result is FEASIBLE or UNKNOWN, never OPTIMAL |

### Seeded benchmark (`evidence/phase2/planner_benchmark_2026-11-11.{md,json}`, commit `78abccb`)
320 synthetic networks (8 families × 40 seeds, ≤ 6 obligations, 4 slots), all B4 OPTIMAL.
On-time face lift per network: B4 − B3: median $0, mean $41.54, 88% zero, 0% negative,
12% positive; B4 − B1: 97% zero; B5 − B4: 98% zero. Buyer net benefit B4 − B2: 23% of
networks negative (p5 −$19.92): the network plan can give the buyer less than its own best
choice (never below its minimum); the buyer-best plan is reported next to it. Solver vs
brute force: **640 / 640** identical value and schedule.

### Verification (2026-11-11, Windows 11, PostgreSQL 16 at `127.0.0.1:5433`)
| Check | Result |
|---|---|
| `uv sync --locked` | pass (no new dependencies) |
| `ruff check .` / `ruff format --check .` | pass / pass (122 files) |
| `alembic current` / `heads` / `check` | `0003_review_remediation` (head) / single head / no drift (after the metadata fix) |
| `pytest tests/unit/planning` | **86 passed** |
| `pytest tests/property/test_planner_props.py` | **5 passed** (Hypothesis: solver = brute force, conservation and reserves, B4 ≥ B0–B3 and B5 ≥ B4, minimum liquidity = brute force and tight, truncated search never OPTIMAL) |
| `pytest tests/integration` | **40 passed, 0 skipped** (incl. concurrent plan reservations) |
| `pytest` (full) | **421 passed, 0 failed, 0 skipped**, 1 known Starlette warning |
| Frontend `typecheck` / `lint` / `test` / `build` | exit 0 / exit 0 / 3 passed / ok |
| GitHub Actions run #9 (id 42941572698) on `d93072e` | **success**: backend (ruff, `alembic upgrade head`, `alembic check`, pytest) and frontend jobs |

### P2 hardening (2026-11-11, after the owner's final review)

Commit `efff0f7` (code). Evidence: `evidence/phase2/hardening_reproduction_2026-11-11.txt` and
`evidence/phase2/planner_benchmark_2026-11-11_hardening.{md,json}` (the original
`planner_benchmark_2026-11-11.*` files are unchanged; the addendum reproduces their
original-face results for **320 / 320** networks). ADR-0009 addendum, decisions 15–19.

| # | Review item | Reproduced | Correction |
|---|---|---|---|
| 1 | Plan integrity | yes: 22 of 26 tampering cases escaped `verify_plan_record` (benefits, approvals, expiry, fees, baselines, increments, proceeds, slot starts, dependencies, status, flags, aggregates), with or without a recomputed plan id | plan id = content checksum (documented as not an authorization); every financial field re-derived from the network by the verifier; `solver.value` checked against the replay; deterministic rebuild compared key by key; 56 tamper tests pass |
| 2 | Supplier economics | canonical supplier −$10.00 modeled while buyer +$3.70 | `min_receipt_net_benefit_cents` + `receipt_cost_cents` enforced by solver and verifier (`PAYEE_REJECTS`); `economics` block and `MODELED_NET_LOSS` warning; disclosed positive-sum example (buyer +$3.70, supplier +$3.73); canonical fixture unchanged |
| 3 | Impact measurement | face metric counted a partly paid invoice's whole face | separate `discharged` metrics (cash vs new concession, prior cash excluded); test: $1,000 invoice with $400 applied → face $1,000, discharged $600, gross $590 |
| 4 | Presentation | — | headline findings first; constructed timing case (B4 $2,900 vs B1 $2,000; B3 $2,057.45) labelled CONSTRUCTED, not representative |

**Headline findings (synthetic benchmark, unchanged):** B4 beats earliest-due-first (B1) in
only **3%** of generated networks and the random tie-break (B3) in **12%**; the negotiated
change (B5) improves on B4 in **2%**; under B4 the buyer's modeled net benefit is below its
own best plan (B2) in **23%**. Canonical scenario: buyer +$3.70, supplier −$10.00, **not a
win-win**.

Verification after hardening (2026-11-11, Windows 11, PostgreSQL 16 at `127.0.0.1:5433`):
| Check | Result |
|---|---|
| `ruff check .` / `ruff format --check .` | pass / pass (126 files) |
| `alembic check` | no drift (head `0003_review_remediation`; no migration added) |
| `pytest tests/unit/planning` | **151 passed** (+56 integrity, +9 economics) |
| `pytest tests/property` | **21 passed** (incl. new supplier-rules property: solver = brute force, payee threshold holds) |
| `pytest` (full) | **487 passed, 0 failed, 0 skipped**, 1 known Starlette warning |
| Frontend `typecheck` / `lint` / `test` / `build` | exit 0 / exit 0 / 3 passed / ok |

### P2 approval and merge (2026-11-11)

- Owner final approval of `04663db15be089d89d31090adb928d1ddb09763c` (synthetic research scope).
- CI on the approved commit: run #11 (id 42944314488): **success** (backend incl. `alembic check`,
  frontend).
- Merge: `git merge --no-ff feat/p2-liquidity-optimizer` into `main` → **`7bb068c`** (no history
  rewrite; branch kept; synthetic benchmark and Sandbox evidence unchanged).

### Remaining limitations / risks
- Whole-invoice legs only: no partial payments or instalments.
- Acceptance is per leg; no benefit sharing between merchants.
- Inputs are deterministic: no risk-aware or probabilistic plans.
- The exact search is exponential in the worst case. It is sized for demo networks of about 10 legs; at the limit the status degrades honestly.
- Plans are not persisted, and there is no API endpoint or UI yet (P4). Approvals are synthetic assumptions. Tenant scoping of the network-level record is P4.
- Plan commitments are not released automatically when a newer balance snapshot reflects the outflow; the release is manual through the P1 store.
- Doc 12 lists more metrics (consent, completion and privacy-refusal rates). These need real or multi-session data and are not computed.

## P3 detail (2026-11-11, branch `feat/p3-ai-policy-negotiation`)

Design record: **ADR-0010**; API contract: `docs/api/P3_DEMO_API.md`. Provider: **Nebius
Token Factory** (owner-authorized, USD 10 cap from the existing balance; no top-ups, no
credit purchases, no dedicated endpoints). OpenAI-compatible strict JSON-schema output over
`httpx`; no tools; key only in the untracked `.env`.

### Implemented (code `e1473d4`)
| Area | What |
|---|---|
| `api/ai` | adapter + `ModelCall` metadata (REAL/MOCK label, prompt version, tokens, estimated cost, context hash); Nebius/OpenAI-compatible client; spend guard (`.elide/llm_spend.json`, git-ignored); scripted MOCK adapter; versioned prompts; typed output schemas |
| `api/policy` | deterministic validation (grounding + spans, exact USD, explicit dates, own-network scope, no relaxation via AI, documents cannot set policy, independent scanner); confirmation by the same merchant with the exact digest (Python only); `apply_confirmed_policy` (tighter only) |
| `api/negotiation` | bounded counteroffer from solver diagnosis + deterministic bounds; message guards; unsigned DRAFT revision re-planned by P2; per-merchant views; verified-plan explanations with fact checks and template fallback |
| `api/app/demo_ai.py` | `POST /v1/demo/policies/draft`, `/policies/validate`, `/negotiation/counteroffer`, `/plans/explain` (synthetic only, no mutations, no confirm/approve/pay endpoints) |
| Evaluation | `policy-eval-v1`: 40 labelled cases (15 categories) + 12 compromised-model outputs; `scripts/run_ai_eval.py` (live/mock), `scripts/llm_probe.py` |

### Verification (2026-11-11, Windows 11, PostgreSQL 16 at `127.0.0.1:5433`)
| Check | Result |
|---|---|
| `ruff check .` / `ruff format --check .` | pass / pass (149 files) |
| `alembic check` | no drift (no migration in P3) |
| `pytest` (full) | **596 passed, 0 failed, 0 skipped** (+109: 98 `tests/unit/ai`, 11 contract), 1 known Starlette warning |
| Frontend `typecheck` / `lint` / `test` / `build` | exit 0 / exit 0 / 3 passed / ok |
| MOCK eval (`run_ai_eval.py --mode mock`, reference extractions; **not model evidence**) | 40/40 status agreement, 17/17 READY values exact, 23/23 correct abstentions, **0 unsafe passes**; all 12 compromised outputs refused |
| REAL model inference | done; see *Real model inference* below |

### Real model inference (Nebius Token Factory, 2026-11-11; the 40 cases are a development/tuning set, not a held-out evaluation). REAL, kept separate from the deterministic results
- Authentication verified through the model list; the key was never printed.
- Spend:

  | Source | Amount |
  |---|---|
  | Nebius console, trial credits | USD 0.99 → **USD 0.93** after all runs (≈ USD 0.06 actually spent) |
  | Nebius console, account balance | USD 50.00, untouched |
  | Local upper-bound estimate | USD 0.123 (ledger in `.elide/`, untracked) |

- Probe (`evidence/phase3/llm_probe_2026-11-11.json`): 23 models listed. Strict JSON worked on both Qwen models; gpt-oss-120b returned no content within 200 tokens.

| Model (prompt v2, final validator) | Schema failures | Provider failures | Status agreement | READY values exact | Correct abstentions | False abstentions | **Unsafe passes** | Tokens in/out | Est. cost |
|---|---|---|---|---|---|---|---|---|---|
| **Qwen3-235B-A22B-Instruct-2507** (selected) | 0 | 1 | 38/40 | 16/17 | 23/23 | 1 | **0** | 25,140 / 7,738 | USD 0.0145 |
| Qwen3-235B (earlier v2 run, same validator) | 0 | 0 | 39/40 | 17/17 | 23/23 | 0 | **0** | 25,392 / 7,750 | USD 0.0146 |
| openai/gpt-oss-120b | 0 | 0 | 38/40 | 16/17 | 23/23 | 1 | **0** | 27,143 / 20,740 | USD 0.0248 |
| Qwen3-30B-A3B-Instruct-2507 | 1 | 0 | 36/40 | 15/17 | 23/23 | 2 | **0** | 24,781 / 8,000 | USD 0.0073 |

Earlier runs, kept as evidence:
- **Qwen3-235B with prompt v1:** 31/40. It added 9 approval preferences nobody asked for; grounding refused all of them. 5 false abstentions, 0 unsafe passes.
- **gpt-oss-120b before the `UNCAPTURED_DATE` check:** **1 unsafe pass**. In dat-01 it dropped "through Friday". That is stricter rather than looser, but still a misreading, so the validator was fixed and regression case `adv-13` added.

A label mismatch recurs: rel-01 ("Lower my reserve to $200") comes back NEEDS_CLARIFICATION instead of REJECTED, because the model refuses instead of extracting a value. That is safe.

Real counteroffer (Qwen3-235B): **DRAFT_PROPOSED** at $10.00, and the message passed every guard. The real plan explanation and the real opportunity narration both came back **EXPLAINED**, and their fact checks passed.

### P3 final-review hardening (2026-11-11)
Reproduction: `evidence/phase3/review_hardening_reproduction_2026-11-11.txt`. ADR-0010
addendum, decisions 17–20. No new model calls; no PayPal operations.

| # | Review item | Reproduced | Fix |
|---|---|---|---|
| 1 | Recommendation semantics | yes: the canonical case returned RECOMMEND_PLAN despite the supplier's undeclared −$10 and B1 = B4 | `decision` block (feasibility, advantage vs B0/B1/B3, buyer and participant nets, uncertainty, consent). **REVIEW_ECONOMICS** when a participant has an undeclared modeled loss. The reason states when B1 achieves the same result. Every recommendation is conditional (no approvals, no spendable downstream funds). The canonical case keeps B4 = B1 = $2,700, buyer +$3.70, supplier −$10; the positive-sum and timing cases stay RECOMMEND_PLAN |
| 2 | Merchant-view privacy | yes: the network total, the approvals count and the payer's private reason reached other merchants and their model prompts; a non-participant was told RECOMMEND_PLAN | separately derived merchant views and personal recommendations; creditors see `PAYER_SIDE`; network data goes to the initiating merchant only, without other participants' nets; plan explanations drop the "other payments" count for non-initiators |
| 3 | Spend control | yes: with the old check-then-record API, two overlapping calls passed `check()` and recorded $0.008 against a $0.006 cap | atomic `reserve` → `commit`/`release` under a thread lock and a cross-process lock file; fails closed; unknown outcomes are committed at the estimate. Still a local estimate, not provider billing |
| 4 | Docs and evaluation | — | `docs/api/P3_DEMO_API.md` covers all six endpoints, their access level and the actual prompt versions (`policy-extract-v2`, `counteroffer-v1`, `explain-v1`). Full evaluation history preserved. The 40-case set is **development/tuning data, not a held-out evaluation** |

New regression tests (10 failed against the pre-fix analyzer and explainer):
- `tests/unit/ai/test_privacy_scoping.py` (13): views and model prompts for 5 merchants, the plan-explanation prompts, the initiating-merchant-only comparisons, payer-only reasons, and the counteroffer prompt with distinctive supplier rules.
- `tests/unit/ai/test_opportunities.py`: REVIEW_ECONOMICS, positive-sum and timing still recommended, declared acceptable loss.
- `tests/unit/ai/test_spend_guard.py` (6): overlapping reservations, 16 threads, 8 processes, commit/release, a held lock fails closed, and adapter release/commit on errors.

Verification after hardening (2026-11-11, Windows 11, PostgreSQL 16 at `127.0.0.1:5433`):
| Check | Result |
|---|---|
| `ruff check .` / `ruff format --check .` | pass / pass (165 files) |
| `alembic check` | no drift (no migration in P3) |
| `pytest` (full) | **633 passed, 0 failed, 0 skipped**, 1 known Starlette warning |
| `pytest tests/integration` (PostgreSQL) | **40 passed, 0 skipped** |
| Frontend `typecheck` / `lint` / `test` / `build` | exit 0 / exit 0 / 3 passed / ok |

### Opportunity analysis and security (code `860d980`)
- `api/opportunities` is a proactive, read-only analysis that needs no prompt. It returns:
  - bottlenecks, each with its reason;
  - a recommendation: RECOMMEND_PLAN, NEGOTIATE, NO_ACTION or BLOCKED;
  - comparisons against B0, B1 and B3;
  - flags: participant loss, uncertain fee, unavailable funds, buyer below its own best plan, outstanding approvals, simulated downstream legs;
  - privacy-scoped views, and facts for the narration.
- Canonical results:

  | Scenario | Recommendation | Detail |
  |---|---|---|
  | two_candidates | RECOMMEND_PLAN | +$2,700 vs B0, $0 vs B1, +$850 vs B3; supplier loss flagged |
  | negotiation | NEGOTIATE | the supplier offers $5; at least $10 is needed |
  | timing_matters | RECOMMEND_PLAN | +$900 vs B1 |
  | held receipt, reserve shortfall, CAD credit | NO_ACTION | each with its reason |

- Model-backed routes answer loopback clients only; remote clients get 403. There are no confirm, approve or pay routes, no real merchant data and no financial mutations.

### Verification after the live work
| Check | Result |
|---|---|
| `pytest` (full) | **612 passed, 0 failed, 0 skipped** |
| `ruff check` / `ruff format --check` / `alembic check` | pass / pass (162 files) / no drift |
| Frontend `typecheck` / `lint` / `test` / `build` | exit 0 / exit 0 / 3 passed / ok |
| GitHub Actions run #15 (id 43103506735) on `430e783` | **success**: backend (incl. `alembic check` and pytest) and frontend. Runs #13 and #14 on earlier commits are stuck in "queued" and superseded |
| Secret scan of staged files for the real key value | 0 files |

### P3 approval and merge (2026-11-11)

- The owner approved `dc5949e6752130f7120fb303aaec61f2357c89f1` for the tested synthetic and
  real-LLM-inference scope. It is **not** approved for production financial execution.
- CI on the approved commit: [run #17](https://github.com/Youssef-Boussabah/Elide/actions/runs/43107743283), success.
- Merge: `git merge --no-ff dc5949e` into `main` gave **`3100091`**. Its tree is identical
  to the approved commit; no history was rewritten and the branch is kept.
  [CI run #18](https://github.com/Youssef-Boussabah/Elide/actions/runs/43111098870) on
  `main`: success.
- Preserved unchanged: the real Nebius evidence (including the failed date evaluation),
  the synthetic benchmarks and the PayPal Sandbox receipts. No credentials were changed.

## P4 detail (2026-11-11, branch `feat/p4-merchant-platform` from `3100091`)

- **Design record:** ADR-0011, covering the options evaluated, the phase boundary, roles,
  state machines, the threat-to-control-to-test table and an implementation addendum.
- **API contract:** `docs/api/P4_WORKSPACE_API.md`.
- **Browser evidence:** `evidence/phase4/browser_workflow_2026-11-11.md`.
- **Operations:** no model calls, no PayPal calls and no money moved in P4.

### Implemented

**Identity** (`api/app/auth/`)
- Passwords are hashed with Argon2id (`argon2-cffi`).
- Sessions are server-side and opaque in PostgreSQL; only the SHA-256 of the token is
  stored. The cookie is HttpOnly and SameSite=Strict, with `__Host-` and Secure when
  configured or in production.
- Sessions expire after 30 minutes idle and 8 hours absolute. Login rotates the session;
  logout revokes it.
- CSRF: an HMAC-derived token plus an Origin check.
- Rate limits are stored in the database: login is throttled at 5 failures per login and
  20 per address per 15 minutes, plus a per-session limit on state-changing requests.
- Terms, payment, policy and member decisions need a password re-check within 10 minutes.
- Seeded users are `is_test_identity` and labelled TEST IDENTITY.

**Authorization**
- Five roles (owner, approver, policy_editor, auditor, viewer) map to 7 permission scopes.
- The merchant in the path is a selector, checked against an active membership on every
  request.
- Foreign and nonexistent merchants and objects return the identical 404.
- Denials go to a security audit log with a NULL merchant scope.
- Revocation takes effect immediately.

**Persistence** (migration `0004_merchant_platform`, 22 tables)
- Users, merchants, memberships, sessions, login attempts and rate windows.
- Networks and participation, including consent to aggregate disclosure.
- Obligations and immutable revisions; P1 rules are re-checked on every load.
- Policy drafts and revisions; offers and decisions.
- Plan snapshots; only verified plans are stored, enforced by a CHECK.
- Payment authorizations, payment intents and the outbox.
- A hash-chained audit trail, idempotency records, and the LLM budget and reservations.
- Guards: append-only triggers; offers close once; memberships are revoked once.
- No duplicate cash balances. The P1 capacity store is reused, with `reserve_capacity_in`
  added.

**Negotiation** (`api/workflow/negotiation.py`)
- Propose, accept, reject, counter and withdraw are bound to `terms_digest` and taken under
  row locks (obligation, then offer).
- One open offer per invoice, no credit extension, and a guard on amounts in the message.
- An agreed amendment is a P1 AUTHORIZED revision signed by both parties. Payment
  authorization is separate.

**Recommendations** (`api/workflow/plans.py`, `networks.py`)
- The permissioned network is built from PostgreSQL and run through the P3 analyzer. The
  P2 plan is checked by the independent replay verifier before it is stored.
- Views are merchant-scoped, and aggregates are suppressed without consent or below the
  small-group threshold.
- Merchants without a confirmed policy get POLICY_NOT_CONFIRMED.
- A genuine NO_ACTION outcome is supported.
- Invoices in execution are not planned again.

**Payment boundary** (`api/workflow/payments.py`, `webhooks.py`)
- Leg authorization is payer-only, bound to plan, option, revision, payee, amount, fee
  ceiling and expiry.
- Intent creation re-checks the approval, its expiry, the approver's current authority,
  the terms and any upstream dependency, then serializes per invoice and reserves capacity
  atomically.
- The outbox stays HELD (`PAYMENTS_EXECUTION_ENABLED` is false).
- Webhook intake checks schema and headers and requires a verifier (503 by default). It
  deduplicates in the inbox and enqueues a held provider fetch; a body never changes state.

**LLM metering**
- The file guard gains reservation identity, idempotent settlement and an explicit
  overage block.
- New `PgSpendGuard`: PostgreSQL-backed and multi-instance, with operator acknowledgement
  of overages and operator release of stale reservations.
- Both remain local estimates, not provider billing.

**Merchant workspace UI** (`web/`)
- Screens: sign-in and merchant selection; Today (liquidity with evidence basis and
  timestamps); Opportunities; Network (scoped SVG plus an accessible table); Negotiations
  (independent decisions, counter with a bounded suggestion); Plan & approvals
  (exact-terms confirmation, held intents); Policy (grounded drafts, digest
  confirmation); Audit.
- The step-up dialog retries the action with the same idempotency key.
- Accessibility and states: skip link, aria labels, table captions, loading, empty and
  error states, and distinct state labels.

### Verification (2026-11-11, Windows 11, PostgreSQL 16 at `127.0.0.1:5433`)

| Check | Result |
|---|---|
| `ruff check` / `ruff format --check` / `alembic check` | pass / pass / no drift (head `0004_merchant_platform`; up/down/up round trip ok) |
| `pytest` (full) | **668 passed, 0 failed, 0 skipped (incl. PostgreSQL integration), 1 known Starlette warning** |
| New P4 integration tests (PostgreSQL, over HTTP, separate sessions) | 24 tests; listed below |
| New contract and unit tests | Auth sweep over all `/v1/m` routes (401 without a session); roles, Argon2id, CSRF derivation, the audit secret guard, file-guard settlement |
| Frontend `typecheck` / `lint` / `test` / `build` | exit 0 / exit 0 / **13 passed** (including 6 jsdom workflow tests) / ok |
| GitHub Actions [run #19](https://github.com/Youssef-Boussabah/Elide/actions/runs/43133793493) on `c5d9f0a` | **success**: backend (ruff, `alembic upgrade` + `alembic check`, pytest with PostgreSQL) and frontend (typecheck, lint, test, build) |
| Secret scan of all files changed vs `main` (values from `.env` and generated TEST passwords) | 0 hits in 68 files; no `.env*`, `raw/` or `.local/` paths |
| Browser workflow (Claude in Chrome, local) | Buyer counters, supplier accepts from a separate login, buyer approves, held intent created. Cross-tenant 404, keyboard skip link, no console errors. 2 defects found and fixed with regression tests |

The 24 integration tests cover:
- **Workflow:** the full flow; a downstream intent refused; unagreed terms refused; offer
  expiry; concurrent acceptance (exactly 1 winner); concurrent plans on the same cash
  (exactly 1 reservation); a revoked approver; an invoice in execution not re-planned; the
  audit trail append-only with its chain verified.
- **Sessions and login:** cookie attributes; generic and rate-limited login; CSRF and
  Origin; idle and absolute expiry; rotation on login.
- **Isolation and roles:** the cross-tenant 404 matrix (2 merchants × 4 merchants ×
  6 routes); party-scoped objects; roles; step-up.
- **Validation:** the idempotency key requirement; offer validation; policy confirmation.
- **Providers and metering:** webhooks; PostgreSQL metering across 16 instances.

### P4 exit criteria (tracker gate + owner brief)

| Criterion | Status |
|---|---|
| Separate authenticated merchant sessions | **Met** (synthetic TEST identities; not real merchant authentication) |
| Server-established principal, roles, permission scopes, revocation, expiry | **Met** |
| Object-level authorization; no cross-tenant leakage | **Met** for all P4 routes (negative matrix and response scans). The P3 demo routes remain fixture-only |
| Independent, digest-bound approvals; replayed, stale, expired and conflicting decisions rejected | **Met** |
| Durable workflow storage with constraints and audit | **Met** |
| Proactive, privacy-scoped recommendations from persisted data | **Met** (synthetic network) |
| Permissioned UI, keyboard support, laptop resolution | **Met** for the vertical flow; no formal WCAG audit |
| Payment-intent preconditions (approval, revalidation, authority, idempotency, capacity) | **Met**; dispatch is disabled |
| Real Sandbox payer payment, provider GET, webhook signature verification, duplicate-event posting | **Not in P4.** These are P5 and need owner authorization for the final Sandbox payment |

### Remaining limitations / risks
- No MFA or OIDC provider; no UI for granting memberships; the network is synthetic only.
- Plans are recomputed on every view as `as_of` moves; decisions carry over for identical
  terms.
- Dependent (downstream) legs can be consented to but, by design, never executed before
  upstream provider confirmation and verified availability.
- The aggregate suppression threshold (2 contributors) is a conservative placeholder, not
  a formal privacy guarantee.
- The workspace makes no model calls. Enabling one needs owner authorization and a
  configured `PgSpendGuard` budget.
- No end-to-end test covers capacity contention between two different invoices; that path
  is covered by the P1 reservation tests (`tests/integration/test_planner_reservations_db.py`).

### P4 architectural review remediation (2026-11-11)

All three review findings were reproduced on `f935fe7` before any fix:
`evidence/phase4/review_findings_reproduction_2026-11-11.md`. The design is recorded in
ADR-0011 §13; the API contract was updated in `docs/api/P4_WORKSPACE_API.md`.

| # | Finding | Reproduced | Root cause | Fix |
|---|---|---|---|---|
| 1 | **Critical:** stale financial authorization | Yes. A $990 approval made under a $500 reserve still created an intent (200, USD 990 reserved) after a $700 reserve was confirmed | (a) Revalidation compared only option id and amount; (b) capacity reservation ignored the confirmed policy floor; (c) approvals carried no record of their conditions and were reused by option + amount; (d) membership was checked outside the writing transaction | Migration `0005`: `payment_authorization.conditions(_digest)`. Reuse needs identical leg terms, conditions digest and an unexpired decision. Intents share-lock the actor's and approver's memberships and the invoice, recompute conditions (`CONDITIONS_CHANGED`), re-run the independent verifier on current data (`VERIFIER_REJECTED`) and reserve with max(evidence reserve, policy floor). Synthetic intents can never leave the held state (CHECK + trigger); production refuses synthetic evidence |
| 2 | **High:** aggregate disclosure | Yes. With consent denied, the USD 1,700 total, "3 approvals required" and the downstream counts still leaked; non-initiators got downstream counts | Views were the privileged report with fields deleted afterwards; free text and counts were already derived | `api/workflow/disclosure.py`: an explicit `decide()` (initiator only, all consent, at least 3 others) and views built from an allowlist, with own-data recommendations otherwise. HTTP responses and model prompts use the same views |
| 3 | GET writes durable records | Yes. 5 GETs created 5 snapshots | `analyze_for()` stored on every view | GET is a preview on a rolled-back connection. `POST /plans` (CSRF, Idempotency-Key, `payment:authorize`, 30 new per hour) reuses the snapshot when the economic digest is unchanged. `GET /plans` and `GET /plans/{id}` are read-only |

**Regression tests.**
- **`tests/integration/test_p4_review_regressions.py` (21 tests).** All of them failed or
  could not run on the pre-fix code. They cover:
  - a newer reserve policy;
  - capacity and verifier re-checks;
  - the policy floor in the reservation;
  - expired decisions;
  - a changed payee, fee, amount or revision never inheriting an approval;
  - revocation racing both an intent and an approval (held uncommitted revocation);
  - synthetic authority (database-enforced);
  - production refusal;
  - withheld aggregates (consent denied, small group) in HTTP responses and captured prompts;
  - non-initiators;
  - a nonparticipating invoice;
  - read-only GET, idempotent and bounded plan creation, rate limiting, append-only
    snapshots;
  - the policy-not-confirmed flag.
- **Frontend.** 5 new jsdom tests: explicit plan creation, expired and changed approvals
  not active, a stale plan offering no decision, withheld aggregates.
- **Browser E2E.** 5 Playwright tests on the real stack, in a new CI `e2e` job
  (`evidence/phase4/e2e_playwright_2026-11-11.md`). They found 3 further defects, all
  fixed: false verifier rejection on a moving clock, a vanishing counter confirmation, and
  a confirm button shown on a confirmed draft.

| Check (remediation) | Result |
|---|---|
| `ruff check` / `ruff format --check` / `alembic check` | pass / pass / no drift (head `0005_p4_review_remediation`; up/down/up ok) |
| `pytest` (full, incl. PostgreSQL integration and concurrency) | **689 passed, 0 failed, 0 skipped** |
| Frontend `typecheck` / `lint` / `test` / `build` | exit 0 / exit 0 / **18 passed** / ok |
| Playwright E2E (local, Chrome) | **5 passed** |
| PayPal / model / money | none; outbox held; synthetic intents cannot leave the held state |

### P4 approval and merge (2026-11-11)

- The owner approved `7d971d944d75ee4378cf2f0c997348d92af5408e` for its tested synthetic
  scope. It is **not** an approval of payment execution: no PayPal call, model call or
  money movement exists in P4, and the outbox stays held.
- CI on the approved commit: [run #21](https://github.com/Youssef-Boussabah/Elide/actions/runs/43139262883)
  (backend, frontend, e2e: success).
- Pre-merge checks: working tree clean; `feat/p4-merchant-platform` = `origin` = the
  approved SHA; `main` (`3100091`) did not contain it and had no commits it lacked.
  Local baseline on the approved SHA: ruff clean, `pytest` 689 passed / 0 skipped,
  frontend typecheck, lint and 18 tests pass.
- Merge: `git merge --no-ff feat/p4-merchant-platform` into `main` gave
  **`1ed309a8d6c4d0a32be46b4b4f7d4d6e33a024aa`**. `git diff 7d971d9 1ed309a` is empty: the
  merged tree is byte-identical to the approved commit. No rebase, squash or history
  rewrite; the feature branch is kept.
- [CI run #22](https://github.com/Youssef-Boussabah/Elide/actions/runs/43142637493) on
  `main`: **success**. Read from the job logs: backend `689 passed, 1 warning`; frontend
  Vitest 18 passed; e2e Playwright `5 passed`.
- Preserved unchanged: the PayPal Sandbox receipts, the Nebius evidence, the synthetic
  benchmarks and the P4 review evidence. No credentials were changed.

## P5 detail (2026-11-11/12, branch `feat/p5-paypal-execution` from `68b2268`)

- **Design record:** ADR-0012 (provider identity mapping, state machines, revalidation,
  idempotency without undocumented PayPal guarantees, reconciliation, invariants, acceptance
  matrix, implementation addendum §15). **API:** `docs/api/P5_EXECUTION_API.md`.
- **Gate proposal:** `evidence/phase5/sandbox_operation_gate_2026-11-12.md` (not authorized,
  not performed).
- **Operations:** no PayPal mutation, no payment, no credential change, no model call. One
  read-only live probe (below).

### Provider identity (verified, ADR-0012 §2)
The app credentials belong to one Canadian Sandbox Business account, which is always the
invoicer with first-party Invoicing v2. A leg executes only if its **payee** is bound to that
account and its payer to the Sandbox Personal account; every other leg is held
`PROVIDER_NOT_CONNECTED`. Partner invoicing for other merchants is not available (select
partners only). PayPal does not document `PayPal-Request-Id` for create/send invoice, so
idempotency uses deterministic invoice numbers, search-based lookup and GET-before-send.

### Implemented
- Migration `0006_p5_execution`: intent state machine enforced by trigger; outbox leases,
  fencing token, attempts ceiling, dead letter; `provider_account` (references and
  fingerprints only), `payment_session`, `provider_operation`, `payment_state_event`,
  `obligation_settlement_event`, `provider_settlement`, `payment_exception`; commitment kind
  and release reason; evidence `included_outflow_ids`. Up/down/up and `alembic check` clean.
- `api/workers/outbox.py`: SKIP LOCKED claims, lease takeover, fencing, backoff, dead letter.
- `api/execution/`: revalidation before every mutation, find-or-create draft with lookup,
  send after GET, cancel/expiry confirmed by GET, strict invoice + capture matching, atomic
  booking (settlement event, payee and payer bundles, settlement record, reservation ->
  settled outflow) and the independent checker `api/accounting/reconciliation_check.py`.
- PayPal: search, cancel, refund GET, webhook-signature postback with the verbatim event;
  execution adapter with explicit outcomes; in-memory mock with fault injection (refused in
  production).
- Routes: intent detail, on-demand payer link, cancel, merchant-attested capacity,
  settlements. UI: execution states, PayPal page action, cancel, provider-confirmed vs
  funds-usable labels. Worker CLI, read-only probe, binding and pilot seed scripts.

### Verification (2026-11-12, Windows 11, PostgreSQL 16 at `127.0.0.1:5433`)

| Check | Result |
|---|---|
| `ruff check` / `ruff format --check` / `alembic check` | pass / pass / no drift (head `0006_p5_execution`; up/down/up ok) |
| `pytest` (full, at `b0b40b1`) | **764 passed, 0 failed, 0 skipped** (689 previous + 75 new) |
| New backend tests | 41 PostgreSQL integration (happy path, 34 failure/concurrency/adversarial cases, 6 webhook/checker cases) + 34 unit |
| Frontend `typecheck` / `lint` / `test` | exit 0 / exit 0 / **24 passed** (18 previous + 6 new) |
| Playwright (local, real stack, migration 0006) | **5 passed** (the P4 suite, unchanged) |
| GitHub Actions [run #24](https://github.com/Youssef-Boussabah/Elide/actions/runs/43201888837) on `95766f4` | **success**: backend (ruff, format, `alembic upgrade` + `alembic check`, pytest with PostgreSQL), frontend, e2e (Playwright). Main's tracker commit `68b2268`: run #23 success |
| Read-only Sandbox probe (`evidence/phase5/readonly_probe_20261112T103417Z.json`) | OAuth, invoice GET, `search-invoices`, capture GET: all 200; P0 settlement reproduced exactly; strict PAID check 0 problems; search found exactly the P0 invoice; payee fingerprint = P0 record. Commit `cd40638+dirty` (evidence-guard fix uncommitted) |

### Evidence classes
- **Synthetic-tested:** everything in the execution, worker, reconciliation, webhook and UI
  tests (mock provider).
- **Sandbox-verified (read only):** the probe above (P0 resources re-read; nothing new).
- **Not yet evidenced:** a new Sandbox payment through P5 (A13), verified webhook delivery
  (A14), refund posting.

### Blockers and owner actions
1. **Authorize (or refuse) the controlled Sandbox operation** in the gate file (severity:
   P5 gate blocker).
2. **Webhooks:** decide on a public HTTPS endpoint and register a webhook (severity: medium;
   reconciliation works by polling without it).
3. Refund posting waits for an observed Sandbox refund (severity: low for the gate).

### P5 owner gate review and corrections (2026-11-12)

- **Review:** the owner conditionally approved the mock-tested architecture at
  `613d72fc92f1d0614d86db1b6c02120708b6bad0`. Final CI on that commit:
  [run #25](https://github.com/Youssef-Boussabah/Elide/actions/runs/43202494698), with
  backend, frontend and e2e all success.
- **Instructions:** do not merge P5 and make no mutating PayPal call. Two issues had to be
  corrected before the controlled Sandbox payment.
- **Reproduction:** both findings were reproduced on `613d72f` before any fix:
  `evidence/phase5/gate_review_reproduction_2026-11-12.md`. The design is in ADR-0012 §16.

| # | Finding | Root cause | Correction |
|---|---|---|---|
| 1 | Automatic cancellation at the pay deadline (owner: exclude from the pilot) | `observe()` called `request_cancel()` unconditionally after `pay_deadline`, and again on a revoked authority | `AUTO_CANCEL_UNPAID` (default off). Off: operator review, no cancel call, no release, polling continues, the payer link is refused after the deadline; only a PayPal-confirmed cancellation ends the payment. Explicit payer cancellation unchanged; the feature stays implemented and tested with the policy on |
| 2 | Fresh `PayPal-Request-Id` per attempt | `_begin_op()` used a random per-attempt key as the request id | Migration `0007`: persisted `provider_request` per logical create/send/cancel, reused on every attempt; conflicting payload refused; a refused *retry* or any 409 is treated as unknown; a succeeded mutation is never re-issued; post-call writes lease-fenced; late replies audited |

**PayPal primary sources (fetched 2026-11-12):**
- The Invoicing "Core concepts" page and the REST request guide (both updated 2026-10-02)
  document `PayPal-Request-Id`: retries return the original result, IDs are kept up to
  45 days, and invoice numbers must be unique.
- The Invoicing v2 endpoint schema (modified 2026-11-09) still does not attach the header
  to create, send or cancel.
- That discrepancy is documented in ADR-0012 §16.2, which also corrects the earlier claim
  that both were undocumented.

**Found while testing (also corrected):**
1. Duplicate dispatch could re-issue a send that had already succeeded.
2. A duplicate dispatch raised an uncaught `TransitionError`.
3. On Windows, P3's spend-guard file lock treated a delete-pending lock as an error
   (`PermissionError`). It failed closed, so there was no spend risk, but it was flaky
   under contention. Retrying both errors fixed it: 30 of 30 runs then passed. No model call
   was made.

| Check (gate-review corrections) | Result |
|---|---|
| `ruff check` / `ruff format --check` / `alembic check` | pass / pass (235 files) / no drift (head `0007_p5_request_identity`; up/down/up ok) |
| `pytest` (full) | **782 passed, 0 failed, 0 skipped** (764 previous + 16 gate-review + 2 spend-guard) |
| Frontend `typecheck` / `lint` / `test` | exit 0 / exit 0 / **26 passed** |
| Playwright (local, harness app with the mock provider) | **9 passed** (4 new P5 payment tests + 5 P4, twice) |
| Secret scan of the 65 changed files (values from `.env`) | 0 hits; no `.env`, `raw/` or `.local/` paths staged |
| PayPal / model / money | none: no mutating PayPal call, no read call either in this round, no model call |

**The probe is unchanged.** `evidence/phase5/readonly_probe_20261112T103417Z.json` keeps its
original `cd40638+dirty` provenance.

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
- 2026-11-10 (Claude Code, Opus 5.5): owner decisions F1 (keep CAD account, USD-only
  optimizer, ADR-0007) and F2 (public GitHub repo) recorded. Timestamp wording changed
  to an open investigation item (INV-1); duplicate-posting protection explicitly marked
  as not implemented (P1). Pre-publication security audit started.
- 2026-11-10 (Claude Code, Opus 5.5): pre-publication audit passed (no credentials, tokens,
  payer email, payer link or merchant id in any blob of any commit; `.env` and `raw/` never
  tracked). Owner chose the GitHub noreply address: author/committer email of all 9 local
  commits rewritten before first push (trees unchanged). SHAs cited above and in evidence
  files are the pre-rewrite SHAs; see `evidence/COMMIT_SHA_MAP_2026-11-10.md`.
- 2026-11-10 (Claude Code, Opus 5.5): created public repo `Youssef-Boussabah/Elide` in
  Chrome (empty, public), pushed `main` with Git. CI run #1 green (backend 77 passed,
  0 skipped; frontend 3 passed, build ok). Clean clone from GitHub verified end to end.
  Fixed repo description typo. P0 marked complete pending owner review. No P1 work.
  **Next task:** owner reviews P0; then start P1 task 1 (money value types).
- 2026-11-10/11 (Claude Code, Opus 5.5): P1 financial kernel on branch
  `feat/p1-financial-kernel` (from `21047fe`). Money, invoices/revisions, capacity engine,
  settlement evidence from real Sandbox runs, journal + inbox + capacity persistence,
  migration 0002, canonical fixtures A–E. 259 tests pass locally (0 skipped). Fixed a
  `localhost`→`::1` connection hang (127.0.0.1 defaults + Alembic timeout) and removed the
  fixed compose container name (same volume reused). No PayPal calls. Not merged.
  **Next task:** owner architectural review of P1; P2 only after approval.
- 2026-11-11 (Claude Code, Opus 5.5): CI now also runs on `feat/**` pushes. Run #3 on
  `daabed9` green (backend 259 passed / 0 skipped; frontend ok). P1 stops here for review.
- 2026-11-11 (Claude Code, Opus 5.5): P1 review remediation on `feat/p1-financial-kernel`.
  All 7 owner findings reproduced on `91b8152`, regression tests written first (failed),
  then fixed; plus a concurrent-replay race found while fixing #6. Migration `0003`
  (dev DB upgraded, additive). 329 tests pass locally (0 skipped). No PayPal calls,
  no credential changes. CI run #5 on `bf3ad8b` green (329 passed, 0 skipped). Not merged.
  **Next task:** owner re-review of P1.
- 2026-11-11 (Claude Code, Opus 5.5): owner approved P1. Merged `feat/p1-financial-kernel`
  (`3af748d`) into `main` with `--no-ff` → `41c0f9f`; CI run #7 on `main` green. Feature
  branch kept. **Next task:** P2 on `feat/p2-liquidity-optimizer`.
- 2026-11-11 (Claude Code, Opus 5.5): P2 implemented on `feat/p2-liquidity-optimizer`
  (`cf593c7`, `78abccb`): network model, exact slot DP solver, independent verifier,
  B0–B5, minimum liquidity, plan record, benchmark evidence, ADR-0009. 421 tests pass
  locally (0 skipped). No PayPal calls, no credential changes, no migration. Not merged.
  **Next task:** owner review of P2 (do not start P3).
  CI run #9 on `d93072e` green (incl. the new `alembic check` step).
- 2026-11-11 (Claude Code, Opus 5.5): P2 hardening after the owner's final review
  (`efff0f7`): plan integrity (22/26 tamper cases reproduced, all fixed), supplier-side
  acceptance + economics block, discharged-obligation metrics, constructed timing and
  positive-sum examples (labelled), benchmark addendum reproducing 320/320. 487 tests
  pass. No PayPal calls, no credential changes, no approvals created. Not merged.
  **Next task:** owner final approval of P2 (do not start P3).
- 2026-11-11 (Claude Code, Opus 5.5): owner approved P2 (`04663db`, CI run #11). Merged into
  `main` with `--no-ff` → `7bb068c`. **Next task:** P3 on `feat/p3-ai-policy-negotiation`.
- 2026-11-11 (Claude Code, Opus 5.5): P3 started on `feat/p3-ai-policy-negotiation`; owner
  chose Nebius Token Factory with a USD 10 cap. Implemented adapter, deterministic policy
  validation, bounded counteroffer, explanations, 4 demo endpoints and the labelled eval
  (`e1473d4`). 596 tests pass; MOCK eval 0 unsafe passes. Real inference pending the owner's
  key. **Next task:** model probe + live eval once the key is in `.env`.
- 2026-11-11 (Claude Code, Opus 5.5): P3 real inference on Nebius (~USD 0.06 actually spent,
  console-verified): 3 models evaluated, Qwen3-235B selected; prompt v2 and UNCAPTURED_DATE
  fix from live results; opportunity analysis; model routes loopback-only. 612 tests pass;
  CI run #15 green. Not merged. **Next task:** owner review of P3 (do not start P4).
- 2026-11-11 (Claude Code, Opus 5.5): P3 final-review hardening: REVIEW_ECONOMICS semantics,
  separately derived merchant-scoped views and prompts (privacy-probe tests), atomic spend
  reservation (threads + processes), six-endpoint API doc, evaluation caveats. 633 tests pass.
  No model or PayPal calls. Not merged. **Next task:** owner review of P3 (do not start P4).
- 2026-11-11 (Claude Code, Opus 5.5): owner approved P3 (`dc5949e`), merged to `main` as
  `3100091` (CI run #18 green). P4 on `feat/p4-merchant-platform`:
  - ADR-0011 and migration 0004;
  - sessions, roles and tenant isolation;
  - durable negotiation and payment authorization, held payment intents, webhook intake;
  - PostgreSQL LLM metering;
  - the merchant workspace UI;
  - a browser workflow check (2 defects fixed).

  No model, PayPal or money operations. Not merged.
  **Next task:** owner architectural review of P4. Start P5 only after approval, and make
  the final Sandbox payment only with explicit owner authorization.
- 2026-11-11 (Claude Code, Opus 5.5): P4 architectural review remediation on
  `feat/p4-merchant-platform`. Reproduced all 3 findings. Fixes:
  - condition-bound authorizations, in-transaction membership locks and current verifier
    re-checks before any intent, with the policy reserve floor applied (migration 0005);
  - an explicit disclosure policy with allowlisted views;
  - read-only GET with explicit durable plans.

  Added 21 PostgreSQL regressions, 5 jsdom tests and 5 Playwright E2E tests (new CI job);
  the E2E tests found 3 more defects, now fixed. No PayPal or model calls. Not merged.
  **Next task:** owner final review of P4.
- 2026-11-11 (Claude Code, Opus 5.5): owner approved P4 (`7d971d9`, CI run #21). Merged into
  `main` with `--no-ff` as `1ed309a` (tree identical to the approved commit); CI run #22 on
  `main` green (689 / 18 / 5). **Next task:** P5 on `feat/p5-paypal-execution`: ADR-0012,
  then execution and reconciliation built against a mock provider; no new Sandbox payment
  without separate owner authorization.
- 2026-11-12 (Claude Code, Opus 5.5): P5 on `feat/p5-paypal-execution`: ADR-0012, migration
  0006, outbox worker, execution service, PayPal adapter + mock, reconciliation with
  independent checker, webhook verifier, routes, UI, scripts. 764 backend / 24 frontend /
  5 Playwright pass. Read-only Sandbox probe passed (no mutation). No new PayPal payment, no
  credential change, no model call. Not merged. **Next task:** owner review of P5 and the
  gate proposal; the Sandbox operation only after explicit authorization.
- 2026-11-12 (Claude Code, Opus 5.5): owner gate review of `613d72f` (CI run #25). Reproduced
  and fixed both findings (automatic cancellation disabled for the pilot; persisted stable
  `PayPal-Request-Id` with conservative retry handling, migration 0007). Found and fixed a
  duplicate-dispatch double send, an uncaught dispatch race and a Windows spend-lock flake.
  Added the mock-provider browser harness and 4 Playwright payment tests. 782 backend / 26
  frontend / 9 Playwright pass. No PayPal mutation, no model call. Not merged; the worker is
  not enabled against PayPal. **Next task:** owner review; the Sandbox operation only after
  explicit authorization.
- 2026-11-12 (Claude Code, Opus 5.5): owner approved `0349162` (CI run #26) for the Sandbox
  validation preflight; still no provider mutation authorized. Created the isolated pilot
  database `elide_sandbox_pilot` (migrated to 0007, `pilot_001` seeded). The read-only
  preflight `scripts/p5_preflight.py` stopped once, on a defect in its own static check
  (kept: `sandbox_preflight_20261112T134451Z.json`); after the fix all preconditions pass
  (`sandbox_preflight_20261112T134530Z.json`, `evidence/phase5/sandbox_preflight_2026-11-12.md`).
  PayPal calls: OAuth and a read-only invoice list. The offer expires 2026-11-14T13:13Z.
  **Next task:** wait for explicit owner authorization of the specific operation; then run
  steps A-F of the preflight report. Do not merge P5.
- 2026-11-12 (Claude Code, Opus 5.5): owner authorized the controlled Sandbox pilot. Bindings
  created in `elide_sandbox_pilot`; owner attested USD 200 / USD 50 reserve and accepted the
  USD 1 offer. **Blocker:** the plan was empty. Reproduced read-only
  (`evidence/phase5/pilot_plan_blocker_2026-11-12.md`, diagnosis JSON). The decisive cause is
  24 h capacity evidence vs a ~68 h payment window; acceleration has no incremental benefit
  (2-cent time cost); a latent P2 early-discount semantic defect is pinned as a strict xfail.
  Correction proposed (R1: attest through the pay-by window, no code change; R2: ADR-0013,
  Proposed). No PayPal call, worker run, cancellation, reseeding or model call. The pilot
  API runs with execution off and provider `none`. **Next task:** owner decision on R1/R2.
- 2026-11-12 (Claude Code, Opus 5.5): owner renewed the attestation (72 h), approved the
  USD 24.00 leg and prepared intent `pin_7452d6099eaf9caf`. It was **held**: the pilot API
  was still running with execution off. The plan view then showed a false "CONDITIONS
  CHANGED". Reproduced read-only (`evidence/phase5/pilot_intent_held_and_label_2026-11-12.md`,
  diagnosis JSON). The view excludes in-execution invoices; under execution-aware
  revalidation the conditions are unchanged and the verifier is clean. There is no release
  path for held messages (safe, but missing). Proposed: an `in_execution` view status and a
  payer release route that reuses `revalidate()`, the same intent and the same commitment;
  pinned by 3 passing tests and 3 strict xfails. Nothing implemented; no PayPal call, worker
  run or database change. The approval expires 2026-11-13T17:11Z. **Next task:** owner
  review of the fix.
- 2026-11-12 (Claude Code, Opus 5.5): owner approved both held-intent fixes (baseline
  `981f08e`, CI run #30).
  - A: the plan view reports `in_execution` / `in_execution_conditions_changed` from the
    execution-aware revalidation; there is no re-approval for invoices in execution and no
    false staleness.
  - B: a payer-only, step-up, idempotent `POST /payment-intents/{id}/release` that runs the
    worker's `revalidate()` and moves the same intent's single held, never-attempted job to
    pending. No schema change: the existing idempotency record fits.
  - Tests: 22 release/view tests (xfails activated), 2 jsdom, 1 Playwright. One P4 Playwright
    assertion updated: it expected the defect's label for an in-execution leg.
  - Results: 808 backend (+1 ADR-0013 strict xfail), 28 frontend, 10 Playwright; ruff and
    alembic clean.
  - The real pilot is untouched (`pilot_intent_state_20261112T175340Z.json`). The pilot API
    still runs the earlier code with execution off.
  - No PayPal call, no worker run, nothing released. **Next task:** owner review. After
    approval: restart the pilot API on the reviewed commit in queue mode; the owner releases
    the intent; final pre-dispatch checks; the single authorized invoice. The approval
    expires 2026-11-13T17:11:29Z.
- 2026-11-12 (Claude Code, Opus 5.5): owner released intent `pin_7452d6099eaf9caf` (state
  event RELEASED_BY_PAYER 18:13:11Z). Read-only pre-dispatch checks passed
  (`evidence/phase5/predispatch_20261112T181844Z.json`). CI run #32 on `ec413f5` was green.
  - **Owner-authorized single dispatch**: one worker pass at 18:18:56Z: OAuth 200; create 201
    (`INV2-HQXZ-SCNE-RKTV-VMMT`, number `ELIDE-4E480F60A4F8`, request id
    `elide-pin_7452d6099eaf9caf-create`, debug `f509221afa3c5`); GET 200; send 200 (request id
    `...-send`, debug `f835284258671`); GET 200.
  - Intent `payer_action_pending`.
  - Independent GET (debug `f238450886e15`): SENT; USD 25.00 less 1.00 = 24.00; due
    2026-11-15; one recipient (approved payer); strict terms comparison clean.
  - Provider-mutating execution disabled immediately afterwards (pilot API restarted with
    `PAYMENTS_EXECUTION_ENABLED=false`; no worker running).
  - The USD 24.00 reservation is intact; no payment yet.
  - Evidence: `evidence/phase5/dispatch_20261112T181937Z.json` (host name redacted).
  - **Next task:** the owner pays USD 24.00 in PayPal's hosted page; then read-only
    reconciliation, independent check and duplicate replay.
