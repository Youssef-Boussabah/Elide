# ELIDE — Phase Gate Tracker
**State (2026-11-11):** P0 **complete** (owner-reviewed). P1 **complete** (owner-approved for the synthetic simulation and deterministic financial-model scope; merged to `main` at `41c0f9f`, CI run #7 green). P2 **implemented and hardened on `feat/p2-liquidity-optimizer`, pending final approval** (see *P2 detail* and *P2 hardening*); not merged; no P3 work started. Maintain this tracker in Git; update with commit, tests, redacted evidence and blocker notes.

| Phase | Gate | Status | Evidence to attach |
|---|---|---|---|
| P0 | API access + hosted Sandbox invoice payment route proven | **Complete** (owner-reviewed): payment route Sandbox-verified (invoice PAID); public repo + CI green ([run #1](https://github.com/Youssef-Boussabah/Elide/actions/runs/42916020788)) | Redacted API responses + provider IDs/status + code SHA |
| P1 | Money model, reserves, fees, revisions and fixtures pass | **Complete** (owner-approved 2026-11-11; merged `41c0f9f`): 329 tests pass (0 skipped), incl. 16 Hypothesis properties and 39 PostgreSQL integration tests; CI run #7 on `main` green | pytest / property test output |
| P2 | Exact optimizer agrees with separate verifier and fair baselines | **Implemented, pending final approval** on `feat/p2-liquidity-optimizer`: hardened after review; 487 tests pass (0 skipped); solver = brute force on 640/640 checks; B0–B5 benchmark in `evidence/phase2/` | B0–B5 test table + seed/trace |
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

### Remaining limitations / risks
- Whole-invoice legs only: no partial payments or instalments.
- Acceptance is per leg; no benefit sharing between merchants.
- Inputs are deterministic: no risk-aware or probabilistic plans.
- The exact search is exponential in the worst case. It is sized for demo networks of about 10 legs; at the limit the status degrades honestly.
- Plans are not persisted, and there is no API endpoint or UI yet (P4). Approvals are synthetic assumptions. Tenant scoping of the network-level record is P4.
- Plan commitments are not released automatically when a newer balance snapshot reflects the outflow; the release is manual through the P1 store.
- Doc 12 lists more metrics (consent, completion and privacy-refusal rates). These need real or multi-session data and are not computed.

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
