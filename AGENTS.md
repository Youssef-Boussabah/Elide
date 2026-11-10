# AGENTS.md — Elide instructions for Codex CLI and other coding agents

Fallback coding agent: Codex CLI. Primary: Claude Code (reads `CLAUDE.md`).
Everything below the marker is identical in `CLAUDE.md` (enforced by
`tests/unit/test_repository_hygiene.py`). Edit both together.

<!-- SHARED-RULES-START -->

## 1. What Elide is

Elide identifies when an authorized change to a business payment (timing, discount or
terms) could make other obligations payable, helps counterparties negotiate acceptable
terms, verifies financial feasibility deterministically, and uses PayPal (Sandbox) to
execute and reconcile **independently approved** payments. Built for the PayPal AI
Hackathon (deadline **Thu 2026-11-12, 12:00 PT / 15:00 Toronto**).

It is **not** a generic chatbot, an invoice reminder, a lender, a wallet or a netting
engine. Do not simplify it into one.

## 2. Source-of-truth hierarchy (highest first)

1. Actual code and verified PayPal API observations — for what is *implemented*.
   Record divergences from the plan; never silently rewrite the plan.
2. `docs/project-sources/00_START_HERE.md` — defines the project.
3. `docs/project-sources/16_DECISIONS_STATUS_AND_OPEN_QUESTIONS.md` — resolves conflicts
   (frozen decisions D001–D013).
4. `docs/project-sources/01`–`17` — intended architecture and behavior.
5. `docs/execution/ELIDE_BUILD_EXECUTION_PLAN_2026-11-10.md` and
   `docs/execution/ELIDE_PHASE_TRACKER_2026-11-10.md` — how and in what order to build.
6. `docs/decisions/` — ADRs recording deviations/choices made during implementation.
7. Older blueprints (not in this repo) are historical only.

`docs/project-sources/` is read-only reference material: its SHA-256 values are pinned in
`ELIDE_SOURCES_SHA256.json` and checked by a test. To amend the design, add an ADR in
`docs/decisions/` (ID, date, alternatives, affected files, finance/privacy impact, test
changes, rationale) — do not edit the source files in place without the owner's approval.

## 3. Phases and gates (do not skip ahead; stop at each gate)

| Phase | Gate (exit criterion) |
|---|---|
| P0 | Env + repo bootstrapped; PayPal Sandbox API access + hosted invoice payment route proven with redacted evidence |
| P1 | Money model (int cents), reserves, fees, revisions, fixtures pass unit/property tests |
| P2 | Exact planner agrees with separately coded verifier; B0–B5 baselines, honest 0/negative lift |
| P3 | Live policy parsing + one bounded counteroffer, schema-validated, human-confirmed, injection-tested |
| P4 | Separate merchant sessions, independent approvals, no cross-tenant leakage |
| P5 | One actual Sandbox payer payment + provider GET + idempotent replay (one posting) |
| P6 | QA: adversarial scenarios, invariants zero violations, CI green, clean-clone install |
| P7 | Public repo + license, working demo, <3 min video, Devpost submission |

Never mark a gate complete without the evidence listed in the tracker. Partial is fine;
say exactly what is missing.

## 4. Stack and layout

- Frontend `web/`: React + TypeScript + Vite + Tailwind CSS v4; React Flow
  (`@xyflow/react`) for the graph; shadcn/ui only when useful; Vitest; ESLint. pnpm.
- Backend `api/` (Python 3.12, uv): FastAPI, Pydantic v2, SQLAlchemy 2, Alembic
  (`migrations/`), PostgreSQL 16 via `compose.yaml` (host port **5433**).
- Tests `tests/{unit,property,contract,integration,sandbox}`: pytest + Hypothesis; Ruff.
- Modular monolith. Backend modules: `app/` (HTTP, auth), `domain/`, `liquidity/`,
  `planning/` (pure, no network), `ai/` (no payment access), `integrations/paypal/`,
  `accounting/`, `workers/` (outbox/inbox). No microservices, Kafka, Redis/Celery, K8s,
  blockchain, vector DB or ML infra.
- Solver: small deterministic exact search first; OR-Tools CP-SAT only if justified.

Common commands (PowerShell, repo root):

```powershell
uv sync --locked                       # backend deps
docker compose up -d --wait db         # PostgreSQL on 127.0.0.1:5433
uv run alembic upgrade head
uv run uvicorn api.app.main:app --reload --port 8000
uv run ruff check . ; uv run ruff format --check . ; uv run pytest
cd web; pnpm install --frozen-lockfile; pnpm typecheck; pnpm lint; pnpm test; pnpm dev
uv run python scripts/paypal_sandbox_check.py   # Sandbox feasibility (needs local .env)
```

## 5. Financial rules (non-negotiable)

- Money is **integer minor units (USD cents)** everywhere. Never floats. Decimal strings
  only at the PayPal boundary via `api/integrations/paypal/money.py`. One currency (D008).
- Deployable capacity = `max(0, verified_spendable − protected_reserve − committed)`.
  The reserve must hold after **every** outgoing leg, including fees.
- Incoming receivables add **zero** deployable cash until verified/attested available;
  no same-slot reuse of incoming funds.
- Settlement identity per invoice:
  `original_open = cash_applied + authorized_concession + valid_noncash_setoff + remaining_open`.
- Gross throughput is not created liquidity; report incremental on-time value against a
  fixed, matched baseline (B0–B5), including zero and negative results.
- Append-only, balanced journal. Corrections are compensating entries, never mutations.
- Planner output is verified by an **independently coded** replay verifier. Report
  `OPTIMAL` only when exhaustive or solver-proven.
- Plans/approvals bind to invoice revision, counterparty, exact amount, currency, fee
  ceiling, expiry and plan digest. Stale digest → 409.

## 6. Payment, consent and AI boundaries

- **AI never authorizes or executes payments.** The LLM gets no PayPal credentials, no
  tools, no payment endpoints, and cannot record consent or mark funds available.
  `api/ai/` must not import `api/integrations/` (a test enforces this).
- LLM output is untrusted: schema-validate, show source spans, require human
  confirmation; ambiguity → ask, never default a missing threshold to zero.
- **Every participating merchant independently approves its own payment.** One merchant
  can never sign for another; network benefit never overrides private acceptance (D003).
- **PayPal payment confirmation ≠ spendable funds** (D006). A `PAID` invoice or webhook
  is provider confirmation only; downstream legs need separate capacity evidence.
- Provider truth comes from fetching the PayPal resource (GET), not from webhook bodies.
  Verify webhook signatures; dedupe events; never post the same money twice; unknown
  outcome after timeout → look up, never blind-retry.
- **No cross-merchant disclosure** of reserves, invoices, payables or thresholds.
  Scope every query by tenant + membership; inaccessible resources look nonexistent.
- PayPal **Sandbox only**. The client refuses non-sandbox base URLs. No real money.
- Out of scope: production lending/credit, pooled wallets, autonomous or legal netting,
  blockchain/crypto, cross-currency, unapproved side payments, trained-ML claims
  without real labeled data.

## 7. Honesty and evidence rules

- Never invent payment receipts, provider responses, test results or completed
  features. Never simulate a successful PayPal payment and report it as real.
- A test counts as passed only if it was executed and passed in this session; report
  skipped tests as skipped. A `403` is a recorded access boundary, not something to
  work around.
- Label every number and state by evidence class: **Specified**, **Synthetic-tested**,
  **Sandbox-verified**, **Pilot-validated**. Synthetic data stays tagged synthetic.
- Evidence goes in `evidence/` **redacted** (no tokens, emails, names, payer URLs).

## 8. Security

- Secrets live only in the untracked `.env` (see `.env.example`). Never commit, log,
  print or paste them; never ask the owner to paste secrets into chat.
- Before any commit: `git status` and check nothing under `.env*`, `evidence/**/raw/`
  or credential files is staged.
- Do not create/publish a GitHub remote, deploy, or spend money without the owner's
  explicit approval of name, visibility and contents.
- Ask before system-wide installs, Windows settings changes, restarts or paid services.

## 9. Testing requirements

- New domain/finance code ships with unit tests; invariants get Hypothesis property
  tests; API endpoints get contract tests; DB code gets integration tests (marked
  `integration`, skipped—not passed—when PostgreSQL is down); live PayPal calls are
  marked `sandbox` and never run by default.
- Before finishing any task: `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run pytest`, and in `web/`: `pnpm typecheck`, `pnpm lint`, `pnpm test`.

## 10. Phase tracker discipline

Update `docs/execution/ELIDE_PHASE_TRACKER_2026-11-10.md` whenever a gate's status
changes, with: what was implemented, exact commands run and results (pass/fail/skip
counts), commit SHA, redacted evidence paths, blockers and owner actions required.
Append a dated line to its **Session log** at the end of each working session.

## 11. Resuming work (Claude Code ⇄ Codex CLI handoff)

Both agents follow this same file (CLAUDE.md and AGENTS.md are identical below the
marker). On start:

1. Read this file, then the Phase Tracker (current phase, blockers, last session log).
2. `git status` and `git log --oneline -10`; do not discard uncommitted work you did not
   create — ask the owner.
3. Run the full test commands in §9 to confirm the baseline before changing anything.
4. Continue only the current phase. Re-read the relevant `docs/project-sources/` file
   for that phase before designing.
5. Work in small commits (`P<n>: <summary>`), never pushing without approval.
6. Before stopping: run tests, update the tracker (status + session log + next task),
   and leave the tree committed or clearly described in the session log.
