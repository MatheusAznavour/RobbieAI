# Robbie — Development Roadmap

## Milestone 0 — Foundation Hygiene & Toolchain Stabilization *(Completed)*

**Goal:** Stabilize repository configuration, dependency management, build health, documentation, and developer scripts without adding application features.

- [x] Create `backend/pyproject.toml` separating Robbie's direct dependencies from `Agent-S` transitive dependencies.
- [x] Establish a portable installation strategy for `vendor/Agent-S` and remove hardcoded machine paths.
- [x] Configure Ruff and Pytest and add Python `__init__.py` package markers + foundation smoke tests.
- [x] Fix Electron TypeScript/ESLint compilation error, apply Robbie branding, and update `.gitignore` rules.
- [x] Populate repository configuration (`.editorconfig`, `.gitattributes`, `.env.example`, `THIRD_PARTY_NOTICES.md`) and core `docs/` + `AGENTS.md`.
- [x] Add Windows PowerShell development scripts in `scripts/`.

**Exit Criteria:** Clean venv installation succeeds; `tsc`, ESLint, Vite frontend build, Ruff, and Pytest all pass; `vendor/Agent-S` submodule remains completely unmodified.

---

## Milestone 1 — FastAPI Core Skeleton & Electron Sidecar Bridge *(Completed)*

**Goal:** Establish secure, reliable local communication and lifecycle management between Electron and FastAPI.

- [x] Implement FastAPI application factory (`backend/robbie/main.py`, `python -m robbie`), validated loopback-only configuration (`127.0.0.1`, dynamic port `0`), authenticated `GET /health`, authenticated `WS /ws/status` with Origin validation, and stdin-EOF graceful shutdown.
- [x] Harden Electron security (`contextIsolation: true`, `nodeIntegration: false`, `sandbox: true`, CommonJS `preload.cjs`, strict CSP, scoped `window.robbie` preload API, removed `vite-plugin-electron-renderer`).
- [x] Add Electron main-process sidecar lifecycle manager (`BackendSidecarManager`) with per-launch 256-bit bearer token kept out of the Renderer, authenticated REST/WebSocket client, Windows process-tree cleanup, and dev vs. packaged path resolution.
- [x] Add automated backend and Electron/Renderer tests covering auth, health, WebSockets, dynamic port binding, and sidecar lifecycle.

---

## Milestone 2 — Agent-S S3 Adapter & Safe Windows ACI

**Goal:** Integrate `gui_agents.s3` behind a clean, Windows-adapted, non-blocking backend layer.

- Implement `backend/robbie/agents/` runner executing `AgentS3` turns off FastAPI's event loop with cooperative pause/cancel support and structured step events.
- Implement `RobbieWindowsACI` subclassing `OSWorldACI` to fix Windows application switching (`pywinauto`/`win32gui`), enforce Windows DPI awareness, detect optional Tesseract availability gracefully, and keep unsafe `LocalEnv` disabled by default.
- Replace raw `exec()` with a constrained, allowlisted PyAutoGUI action parser and dispatcher.

---

## Milestone 3 — Human-in-the-Loop Safety & Desktop Control UX

**Goal:** Deliver an interactive, safe Windows desktop experience for running and supervising agent tasks.

- Build the Robbie desktop UI: task composer, live step/reasoning timeline, screenshot previews, and status indicators.
- Implement action approval workflow (Approve / Deny / Policy tiers) over WebSockets.
- Add a global emergency-stop shortcut (`globalShortcut`) and non-intrusive window minimize/overlay handling during screen capture.

---

## Milestone 4 — Provider Configuration, Credentials & Persistence

**Goal:** Make model setup straightforward and persist user sessions safely on Windows.

- Support configuring worker VLM and visual grounding endpoints (OpenAI, Anthropic, Gemini, OpenRouter, Ollama, vLLM, HuggingFace) via UI and validated backend schemas.
- Store API keys securely using OS-backed encryption (Electron `safeStorage` / Windows Credential Manager).
- Add SQLite persistence (`backend/robbie/memory/`) under `%LOCALAPPDATA%\Robbie` for task history, trajectories, and audit logs.

---

## Milestone 5 — Automated Testing, CI & Windows Packaging

**Goal:** Productionize quality checks and Windows distribution.

- Expand backend `pytest` coverage (mocked VLM/grounding/ACI integration tests) and add frontend tests.
- Add GitHub Actions CI workflows for linting, typechecking, and tests.
- Finalize `electron-builder.json5` (Windows `.ico` icon, NSIS installer, bundled Python backend `extraResources`) and evaluate strategies to minimize packaged Python dependency size.
