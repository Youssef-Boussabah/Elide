# Architecture decision records

Implementation-time decisions and deviations from the execution plan. Format follows the
change-control procedure in `docs/project-sources/16_DECISIONS_STATUS_AND_OPEN_QUESTIONS.md`.
ADRs never override frozen decisions D001–D013. A conflict needs an explicit owner decision.

| ID | Title | Status |
|---|---|---|
| [ADR-0001](ADR-0001-native-windows-development.md) | Native Windows development instead of Linux/WSL2 | Accepted |
| [ADR-0002](ADR-0002-repository-and-package-layout.md) | Root `pyproject.toml`, `api` import package, uv | Accepted |
| [ADR-0003](ADR-0003-postgres-host-port-5433.md) | PostgreSQL host port 5433 | Accepted |
| [ADR-0004](ADR-0004-toolchain-pins-and-deferrals.md) | Toolchain pins (TypeScript 6.0) and deferred tools | Accepted |
| [ADR-0005](ADR-0005-paypal-phase0-spike-design.md) | PayPal Phase 0 spike: Invoicing v2 draft with invoice-level discount | Accepted |
| [ADR-0006](ADR-0006-mit-license.md) | MIT license | Accepted |
| [ADR-0007](ADR-0007-usd-only-with-cad-sandbox-settlement.md) | USD-only optimizer with a CAD-settling Sandbox merchant | Accepted |
| [ADR-0008](ADR-0008-financial-kernel-design.md) | Phase 1 financial kernel design choices | Accepted |
