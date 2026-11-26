# AGENTS.md — Robbie Engineering Guidelines

This file provides repository context and strict rules for AI coding agents and contributors working on **Robbie**.

## Project Overview

Robbie is a Windows desktop AI agent built with:
- **Desktop shell & UI:** Electron, React, TypeScript, Vite (`apps/desktop/`)
- **Local backend:** Python 3.11, FastAPI, Uvicorn, Pydantic (`backend/robbie/`)
- **GUI agent engine:** `Agent-S` (`gui-agents` S3) mounted as a third-party Git submodule (`vendor/Agent-S/`)

See `docs/ARCHITECTURE.md`, `docs/PRODUCT.md`, `docs/ROADMAP.md`, and `docs/DEVELOPMENT.md` for full details.

## Strict Rules

1. **Do NOT modify `vendor/Agent-S`**:
   - Treat `vendor/Agent-S` as a read-only third-party dependency.
   - Never edit files inside `vendor/Agent-S`, never alter `.gitmodules`, and never bump the submodule commit without explicit approval.
   - All Windows fixes, safety guards, and custom behavior must live in `backend/robbie/` via wrappers or subclasses.
2. **Keep Dependencies Clean and Portable**:
   - Declare Robbie's direct Python dependencies in `backend/pyproject.toml`.
   - Never commit hardcoded local file paths (e.g., `C:\Users\...`) or raw `pip freeze` dumps into `backend/requirements.txt`.
   - Do not upgrade Electron or major frontend/backend dependencies without explicit technical justification and approval.
3. **Never Commit Secrets**:
   - Keep API keys out of Git. Only document variable names in `.env.example`.
4. **Respect Windows Constraints**:
   - All scripts and runtime code must work natively on Windows (PowerShell, Windows paths, DPI scaling, no `/bin/bash` assumptions).
5. **Security & Safety Guardrails**:
   - Never pass untrusted LLM output directly to `exec()` or `eval()` in Robbie code.
   - Keep `Agent-S` `LocalEnv` disabled by default.
   - Localhost FastAPI endpoints must bind to `127.0.0.1` and require authentication once implemented (Milestone 1+).
   - Never run synchronous `Agent-S` prediction loops directly on FastAPI's `asyncio` event loop.

## Code & Style Conventions

- **Python (`backend/`):** Python 3.11+, Ruff linting/formatting (`line-length = 100`, rules `E, F, I, B, UP`), Pytest in `backend/tests/`.
- **TypeScript (`apps/desktop/`):** Strict TypeScript (`tsconfig.json`), ESLint with `--max-warnings 0`.
- **Line Endings:** LF for source files, CRLF for Windows scripts (`.ps1`, `.bat`, `.cmd`), enforced by `.editorconfig` and `.gitattributes`.
- **Language:** All code, comments, commit messages, and documentation must be written in English.

## Verification Checklist Before Finishing Changes

Run the relevant verification scripts from the repository root:

- `.\scripts\typecheck-frontend.ps1` (TypeScript + ESLint)
- `Push-Location apps\desktop; npm run build:app; Pop-Location` (Vite + Electron bundle build)
- `.\scripts\test-backend.ps1` (Ruff check, Ruff format check, Pytest)
- Confirm `vendor/Agent-S` has no modified source files and its commit hash is unchanged.
