# ELIDE — Phase Gate Tracker
**State (2026-11-10):** P0 **partial**. Local bootstrap is done and verified. PayPal Sandbox feasibility is **blocked on owner credentials**. No financial features are implemented, and no PayPal API call has been made yet. Maintain this tracker in Git; update with commit, tests, redacted evidence and blocker notes.

| Phase | Gate | Status | Evidence to attach |
|---|---|---|---|
| P0 | API access + hosted Sandbox invoice payment route proven | **Partial**: local bootstrap done; PayPal steps blocked on owner Sandbox credentials | Redacted API responses + provider IDs/status + code SHA |
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
  runs opt-in steps and writes redacted evidence. **Not yet run against PayPal.**
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
| Backend tests | `uv run pytest` | **42 passed, 0 failed, 0 skipped** (incl. 2 live-PostgreSQL integration tests) |
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

### PayPal Sandbox status
| Step | Status |
|---|---|
| 1. OAuth token against Sandbox | **Blocked**: owner must create a Sandbox app and put credentials in local `.env` |
| 2. Invoicing API accessibility (`GET /v2/invoicing/invoices`) | Blocked (depends on 1) |
| 3. Draft invoice creation with $10 invoice-level discount | Blocked (depends on 1) |
| 4. Hosted payer workflow (send → `payer-view` link) | Blocked (depends on 3) |
| 5. Provider resource retrieval + amount comparison | Blocked (depends on 3) |
| Actual payer payment | Not attempted. Needs an owner action in the hosted page, then step 5 with `--invoice-id` |
| Orders v2 fallback | Not built. Only needed if Invoicing is blocked |

### Missing dependencies / environment notes
- GitHub CLI not installed (only needed for remote creation; system install needs approval).
- `python` on PATH is the Microsoft Store alias stub. Use `uv run` (no action needed).
- Port 5432 is held by a WSL PostgreSQL relay, so Elide uses 5433 (ADR-0003).
- LLM provider/model/key not chosen (open question 9, Phase 3).

### Owner actions required
1. Create the PayPal Sandbox Business and Personal accounts and a Sandbox REST app. Fill
   `.env` locally (README → "PayPal Sandbox setup"). Do not paste secrets in chat.
2. After the hosted payer link is produced, pay it as the Sandbox Personal account.
3. Later: approve GitHub repository name/visibility before any remote is created.

### Remaining P0 gate requirements
- [ ] OAuth token obtained server-side (redacted evidence)
- [ ] Invoicing v2 accessible for the owned Sandbox merchant
- [ ] Draft invoice created; provider-computed total observed (discount behavior recorded)
- [ ] Invoice sent; hosted payer link observed
- [ ] Sandbox payer completes payment; provider GET confirms amount/currency/status
- [ ] Redacted evidence + commit SHA recorded here
- [ ] (if any step returns 403/unsupported) blocker recorded and Orders v2 fallback evaluated
- [ ] CI run green on GitHub (after the remote exists)

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
  (see table). Bootstrap commit: see `git log` (first commit on `main`, "P0: bootstrap
  repository"). Stopped at the P0 gate, blocked on owner PayPal Sandbox credentials.
  **Next task:** once `.env` has Sandbox credentials, run `scripts/paypal_sandbox_check.py`
  steps 1→5, record results here, then have the owner pay the hosted invoice.
