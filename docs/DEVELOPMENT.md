# Robbie — Development Guide

## 1. Prerequisites (Windows)

- **OS:** Windows 10 or Windows 11 (x64)
- **Git:** With submodule support (`git clone --recurse-submodules ...`)
- **Python:** **3.11.x** (required for compatibility with `vendor/Agent-S`, which specifies `>=3.9, <=3.12`)
- **Node.js:** Node.js LTS (tested with Node 24 / npm 11; Electron `^30.0.1` runs its own embedded Node runtime)
- **Optional — Tesseract OCR:** Only required if using Agent-S OCR text-span grounding (`highlight_text_span`). Install Tesseract for Windows and add it to `PATH`, or configure `TESSERACT_CMD` in `.env`.

> **Tip (OneDrive Warning):** Avoid storing active Git repositories containing `node_modules/`, `.venv/`, and local SQLite databases inside OneDrive-synced folders when possible. OneDrive file locking can cause slow builds or transient `EPERM` errors.

## 2. Initial Setup

1. **Clone with submodules** (or initialize submodules in an existing clone):
   ```powershell
   git submodule update --init --recursive
   ```
2. **Create your local environment file:**
   ```powershell
   Copy-Item .env.example .env
   ```
   Edit `.env` with your model provider settings when running agent tasks. Never commit `.env`.
3. **Check your environment:**
   ```powershell
   .\scripts\check-env.ps1
   ```
4. **Install Python backend dependencies** (creates `backend/.venv` if missing, installs `vendor/Agent-S` editable and `backend[dev]` editable):
   ```powershell
   .\scripts\install-backend.ps1
   ```
   Manual equivalent from `backend/`:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```
5. **Install desktop frontend dependencies:**
   ```powershell
   Push-Location apps\desktop
   npm install
   Pop-Location
   ```

## 3. Common Development Commands

All helper scripts are PowerShell scripts in `scripts/` and can be run from the repository root:

| Task | Command |
|---|---|
| Verify local dev environment | `.\scripts\check-env.ps1` |
| Install/update backend venv | `.\scripts\install-backend.ps1` |
| Run backend lint (Ruff) & tests (Pytest) | `.\scripts\test-backend.ps1` |
| Run frontend TypeScript & ESLint checks | `.\scripts\typecheck-frontend.ps1` |
| Start Electron desktop app (dev mode) | `.\scripts\start-desktop.ps1` |

### Manual Frontend Build & Tests (`apps/desktop`)

```powershell
Push-Location apps\desktop
npm run typecheck   # tsc --noEmit
npm run lint        # eslint
npm test            # Node test runner (sidecar lifecycle + preload/renderer tests)
npm run build:app   # tsc && vite build (produces dist/ and dist-electron/)
npm run build       # full build + Windows NSIS installer via electron-builder
Pop-Location
```

## 4. Python Backend Standards

- **Packaging:** Direct backend dependencies live in `backend/pyproject.toml`. Do **not** overwrite `backend/requirements.txt` with raw `pip freeze` output or machine-specific paths.
- **Linting & Formatting:** Configured via Ruff in `backend/pyproject.toml` (target `py311`, line length 100).
  ```powershell
  .\backend\.venv\Scripts\python.exe -m ruff check backend
  .\backend\.venv\Scripts\python.exe -m ruff format --check backend
  ```
- **Testing:** Configured via Pytest in `backend/pyproject.toml`.
  ```powershell
  Push-Location backend
  .\.venv\Scripts\python.exe -m pytest
  Pop-Location
  ```

## 5. Third-Party Submodule Policy (`vendor/Agent-S`)

`vendor/Agent-S` is an independent Apache-2.0 Git submodule (`simular-ai/Agent-S`).

- **Never modify source files inside `vendor/Agent-S`.**
- **Never change the submodule commit** unless explicitly planned as an upstream upgrade.
- Implement all Windows adaptations, action guards, and integrations in `backend/robbie/` by wrapping or subclassing `gui_agents.s3` interfaces.
