# ADR-0001: Native Windows development instead of Linux/WSL2

- **Date:** 2026-11-10 · **Proposer:** project owner (via Phase 0 instructions) · **Status:** Accepted
- **Context:** The execution plan (§2) names native Linux or WSL2/Ubuntu as the primary
  environment. The owner develops on Windows 11 with VS Code and asked for native Windows
  (PowerShell + Docker Desktop with the WSL2 backend) unless there is a demonstrated
  technical reason to migrate.
- **Decision:** Develop natively on Windows. PostgreSQL runs in Docker Desktop (WSL2
  backend). CI runs on Linux (`ubuntu-latest`), so the code also works on Linux.
- **Alternatives:** WSL2 Ubuntu checkout (rejected for now: no demonstrated need, and it
  splits the toolchain across two filesystems).
- **Consequences:** Code must not depend on the OS: use `pathlib`, LF line endings
  (`.gitattributes`), and no shell-specific scripts on the critical path. PowerShell 5.1
  `Set-Content -Encoding utf8` writes a BOM that breaks `pyproject.toml`. Write files
  with the editor/agent tools instead.
- **Finance/privacy impact:** none. **Test impact:** CI on Linux plus local Windows runs.
