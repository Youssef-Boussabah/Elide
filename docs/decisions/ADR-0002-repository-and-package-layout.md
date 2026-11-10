# ADR-0002: Root `pyproject.toml`, `api` import package, uv

- **Date:** 2026-11-10 · **Status:** Accepted
- **Decision:** One `pyproject.toml` and `uv.lock` at the repository root (execution plan §4).
  The backend import package is `api/` with sub-packages matching 08_SYSTEM_ARCHITECTURE
  (`app`, `domain`, `liquidity`, `planning`, `ai`, `integrations/paypal`, `accounting`,
  `workers`). uv runs it as a non-packaged project (`tool.uv.package = false`). pytest
  adds the root to `sys.path`. The frontend is a separate pnpm project in `web/` with
  its own lockfile. No monorepo tool.
- **Alternatives:** `api/pyproject.toml` with a `src/elide` package (rejected: diverges
  from the documented layout and complicates Alembic/tests paths).
- **Consequences:** Run backend commands from the repo root.
