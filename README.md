# Elide

**Permissioned, network-aware invoice payment and liquidity coordination**, built for the
PayPal AI Hackathon.

Elide identifies when an authorized change to a business payment (timing, discount or
terms) could make other obligations payable. It helps counterparties negotiate acceptable
terms, verifies feasibility with deterministic code, and uses PayPal (Sandbox) to execute
and reconcile payments that **each merchant approves independently**. AI interprets
policies and drafts offers. It never authorizes or executes payments.

> **Status: Phase 0 complete; Phase 1 financial kernel (money, invoices, capacity,
> journal) implemented on a review branch; review findings remediated, re-review
> pending.** One synthetic Sandbox
> invoice was created, sent and paid by a Sandbox payer, and verified through the PayPal
> API. Planning, approvals, webhooks and the product UI are not implemented
> yet. See [`docs/execution/ELIDE_PHASE_TRACKER_2026-11-10.md`](docs/execution/ELIDE_PHASE_TRACKER_2026-11-10.md)
> for verified progress.

## Repository layout

```text
api/                 FastAPI modular monolith (Python 3.12)
  app/               HTTP boundary, health endpoints
  domain/ liquidity/ planning/ accounting/ workers/   (Phase 1+)
  ai/                LLM adapter: structured output only, no payment access
  integrations/paypal/  Sandbox-only PayPal Invoicing v2 client
migrations/          Alembic migrations
web/                 React + TypeScript + Vite + Tailwind CSS v4
tests/               unit / property / contract / integration / sandbox
scripts/             paypal_sandbox_check.py (PayPal feasibility check)
fixtures/            synthetic demo networks (Phase 1+)
evidence/            redacted proof (Sandbox runs, test logs)
docs/project-sources/  authoritative design documents (read-only, hash-pinned)
docs/execution/      build plan and phase tracker
docs/decisions/      architecture decision records
```

## Prerequisites

| Tool | Tested version |
|---|---|
| Python | 3.12 (via [uv](https://docs.astral.sh/uv/)) |
| uv | 0.11 |
| Node.js | 24 |
| pnpm | 11 (`corepack enable`) |
| Docker Desktop / Compose | 29 / v2.40 |

Native Windows (PowerShell) and Linux/macOS both work. Commands below are the same in
PowerShell and bash unless noted.

## Quickstart (no credentials needed)

```powershell
uv sync --locked
Copy-Item .env.example .env          # bash: cp .env.example .env
docker compose up -d --wait db       # PostgreSQL 16 on 127.0.0.1:5433
uv run alembic upgrade head
uv run uvicorn api.app.main:app --reload --port 8000
```

In a second terminal:

```powershell
cd web
pnpm install --frozen-lockfile
pnpm dev                              # http://localhost:5173
```

Health endpoints: `GET http://127.0.0.1:8000/health` (liveness) and
`GET http://127.0.0.1:8000/health/ready` (database + configuration flags; no secrets).

The database uses host port **5433**, because 5432 is often already taken by a local or
WSL PostgreSQL. Override it with `ELIDE_DB_PORT` and `DATABASE_URL` in `.env`.

## Tests and checks

```powershell
uv run ruff check . ; uv run ruff format --check .
uv run pytest                         # integration tests skip if the DB is down
cd web; pnpm typecheck; pnpm lint; pnpm test; pnpm build
```

## PayPal Sandbox setup

Elide uses the **PayPal Sandbox only**. The client refuses any non-sandbox URL. Never
commit `.env`, and never paste credentials into chats, issues or screenshots.

1. Sign in at <https://developer.paypal.com/dashboard/>.
2. **Testing Tools → Sandbox Accounts**: you need one **Business** account (merchant,
   invoice sender) and one **Personal** account (payer). PayPal usually creates both by
   default. Otherwise use *Create account* (country United States).
3. **Apps & Credentials**: switch the toggle to **Sandbox**, then *Create App*
   (type *Merchant*) linked to the Sandbox **Business** account. Copy the
   **Client ID** and **Secret**.
4. In the app's settings, make sure the **Invoicing** feature is enabled, if the
   dashboard lists it.
5. Open your local `.env` in your editor and fill in `PAYPAL_CLIENT_ID`,
   `PAYPAL_CLIENT_SECRET` and `PAYPAL_SANDBOX_PAYER_EMAIL` (the Personal account's
   email).
6. Run the feasibility check step by step:

   ```powershell
   uv run python scripts/paypal_sandbox_check.py                  # auth + invoice API access
   uv run python scripts/paypal_sandbox_check.py --create-draft   # one draft ($1,000 - $10 discount)
   uv run python scripts/paypal_sandbox_check.py --invoice-id INV2-XXXX --set-due-date 2026-12-01
   uv run python scripts/paypal_sandbox_check.py --send-invoice-id INV2-XXXX --expected-due-date 2026-12-01
   ```

   `--send-invoice-id` checks the draft first (status, $990 total, discount, due date,
   recipient) and never creates an invoice. The hosted payer link is saved to the
   git-ignored `evidence/paypal-sandbox/raw/payer_link_<id>.txt`, not printed.

7. Open that link in a private browser window, log in as the Sandbox **Personal**
   account, and pay. Then verify the payment against PayPal:

   ```powershell
   uv run python scripts/paypal_sandbox_check.py --invoice-id INV2-XXXX --expected-status PAID
   ```

Each run writes redacted evidence to `evidence/paypal-sandbox/runs/`. A `PAID` status
confirms the payment with the provider. It does **not** prove the funds are spendable.

## License

[MIT](LICENSE)
