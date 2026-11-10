# ADR-0003: PostgreSQL host port 5433

- **Date:** 2026-11-10 · **Status:** Accepted
- **Context:** On the development machine, `127.0.0.1:5432` is held by `wslrelay.exe`
  (a PostgreSQL inside the owner's WSL Ubuntu distro, forwarded to Windows). Compose
  failed with "ports are not available" on 5432.
- **Decision:** Publish the container on `127.0.0.1:5433` by default (`ELIDE_DB_PORT`
  overrides). Bind to loopback only. The owner's existing WSL PostgreSQL is left untouched.
- **Consequences:** `DATABASE_URL` defaults to port 5433. CI uses 5432 inside its own runner.
