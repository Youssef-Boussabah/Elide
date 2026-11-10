# ADR-0004: Toolchain pins and deferred tools

- **Date:** 2026-11-10 · **Status:** Accepted
- **TypeScript pinned to ~6.0** (6.0.3). The latest, 7.0.2, is outside the supported peer
  range of typescript-eslint 8.72 (`>=4.8.4 <6.1.0`). Revisit when typescript-eslint
  supports 7.x.
- **Python pinned to 3.12** (`.python-version`, `requires-python >=3.12,<3.13`).
- **Locked:** `uv.lock` (backend) and `web/pnpm-lock.yaml` (frontend). CI uses
  `uv sync --locked` and `pnpm install --frozen-lockfile`.
- **Deferred until needed:** shadcn/ui (add components when the merchant screens are
  built in P4), OR-Tools (only if the exact enumerator is inadequate; open question 8),
  Playwright (P6 if time allows), LLM provider SDK (P3, after the provider is chosen;
  open question 9).
- **Known warning:** Starlette deprecates using `httpx` with its TestClient. This is
  harmless for now; revisit when FastAPI's TestClient guidance changes.
