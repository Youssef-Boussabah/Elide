# Development environment audit — 2026-11-10

Detected with PowerShell/Git Bash on the owner's machine. Versions are as observed. Nothing
was upgraded or installed system-wide.

## Operating system and shells

| Item | Detected |
|---|---|
| Windows | Windows 11 Home, 10.0.26300 (build 26300) |
| PowerShell | Windows PowerShell 5.1.26100.9818 (no PowerShell 7 required) |
| Git Bash | bundled with Git for Windows |

## Tools

| Tool | Status | Version / path | Notes |
|---|---|---|---|
| Git | OK | 2.51.0.windows.1 | global user.name/email set; `core.autocrlf` unset globally, set to `false` in this repo + `.gitattributes` (LF) |
| GitHub CLI (`gh`) | **Missing** | — | Only needed to create the remote repo/PRs. System install (winget) needs owner approval |
| Python 3.12 | OK | 3.12.6 (`C:\Users\Youss\AppData\Local\Programs\Python\Python312`) | Pinned via `.python-version` |
| Other Pythons | present | 3.13.14, 3.13.12, 3.10.20, 3.9.25, 3.8.20 (uv-managed) | not used |
| `python` on PATH | **Store alias stub** | `WindowsApps\python.exe` | Prints "Python was not found". Use `uv run` / `py -3.12`. Optional fix: Settings → Apps → Advanced app settings → App execution aliases (owner decision) |
| uv | OK | 0.11.26 | |
| Node.js | OK | v24.13.0 | |
| npm | OK | 11.6.2 | |
| pnpm | OK | 11.18.0 (corepack shim) | |
| Docker Desktop | OK (was stopped; launched) | Engine 29.1.2 | WSL2 backend |
| Docker Compose | OK | v2.40.3-desktop.1 | |
| WSL | OK | default version 2; distros `Ubuntu`, `docker-desktop` (v2) | |
| Claude Code | OK | 2.1.322 | |
| Codex CLI | OK | codex-cli 0.167.0 | |
| VS Code | OK | 1.145.2 (`code` on PATH) | recommended extensions in `.vscode/extensions.json` |
| `psql` client | not installed | — | Not needed: `docker exec elide-db psql -U elide -d elide` |

## Ports

| Port | Use | Status |
|---|---|---|
| 8000 | FastAPI | free |
| 5173 | Vite | free |
| 5432 | (PostgreSQL default) | **taken** by `wslrelay.exe` (WSL distro service forwarded to Windows) once WSL started. Elide uses **5433** instead (ADR-0003) |
| 5433 | Elide PostgreSQL | free → now used by `elide-db` |
| 3000, 8080 | spare | free |

Windows excluded TCP port ranges: 50000–50059 and 63353–63933 (none conflict).

## OneDrive check

- OneDrive is installed (`C:\Users\Youss\OneDrive`), but Known Folder redirection is **off**:
  Documents → `C:\Users\Youss\Documents`, Downloads → `C:\Users\Youss\Downloads`,
  Desktop → `C:\Users\Youss\Desktop` (registry `User Shell Folders`).
- The project `C:\Users\Youss\Documents\Projects\Elide` is therefore **not synchronized** by
  OneDrive. No current risk.
- If OneDrive folder backup is ever turned on for Documents, move the repo outside
  OneDrive first (for example `C:\dev\Elide`). Otherwise OneDrive would upload `.env`
  (secrets), churn `.venv`/`node_modules`, and can corrupt Git index/lock files during
  sync. The PostgreSQL data lives in a Docker volume, not in the project folder, so it
  is not exposed either way.

## Install classification for missing items

| Item | Class | Action |
|---|---|---|
| GitHub CLI | System install (winget/MSI), optional | Ask owner when a remote repo is wanted |
| Python Store alias | Windows setting | Optional, owner decision. Not needed for this project |
| PayPal Sandbox app credentials | Requires owner sign-in | Owner action (see README → PayPal Sandbox setup) |
| LLM provider API key | Requires owner account / possibly paid | Phase 3 decision (open question 9) |
| Project-local packages | Safe, installed | `uv sync`, `pnpm install` (locked) |
