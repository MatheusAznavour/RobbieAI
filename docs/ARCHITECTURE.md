# Robbie — Architecture

## 1. System Overview

Robbie uses a **local desktop sidecar architecture** tailored for Windows x64:

```text
+--------------------------------------------------+
|  Electron Desktop Shell (`apps/desktop`)         |
|  - Main Process: window, IPC, backend lifecycle  |
|  - Preload: narrow typed bridge                  |
|  - Renderer: React + TypeScript UI               |
+------------------------+-------------------------+
                         | Localhost HTTP + WebSocket (per-session auth token)
                         v
+--------------------------------------------------+
|  Python Backend (`backend/robbie`)               |
|  - `api/`: FastAPI REST & WebSocket endpoints    |
|  - `core/`: settings, auth, logging, lifecycle   |
|  - `agents/`: Agent-S S3 adapter & step worker   |
|  - `tools/`: validated Windows action executor   |
|  - `providers/`: VLM & grounding config          |
|  - `memory/`: local SQLite session/audit store   |
+------------------------+-------------------------+
                         | Python SDK (unmodified submodule)
                         v
+--------------------------------------------------+
|  Third-Party Dependency (`vendor/Agent-S`)       |
|  - `gui_agents.s3` (`AgentS3`, `OSWorldACI`)     |
+--------------------------------------------------+
```

## 2. Repository Boundaries

- **`apps/desktop/`**: Electron + React + TypeScript desktop application. Owns user interaction, live trajectory rendering, approval prompts, global shortcuts, and (from Milestone 1 onward) spawning/monitoring the local Python backend process.
- **`backend/robbie/`**: Robbie's Python 3.11 FastAPI backend and orchestration layer. Owns API contracts, task state machines, safety policies, Windows-specific ACI subclassing, provider settings, and local persistence.
- **`vendor/Agent-S/`**: Independent third-party Git submodule (`simular-ai/Agent-S`, Apache-2.0) installed editable as `gui-agents`. **Must never be modified directly.** All Robbie customizations must live in `backend/robbie/` via wrappers or subclasses.
- **`scripts/`**: Windows PowerShell developer automation scripts.
- **`docs/`**: Product, architecture, roadmap, and development documentation.

## 3. Dependency Strategy

- `backend/pyproject.toml` declares **only Robbie's direct backend dependencies** (`fastapi`, `uvicorn`, `pydantic`, `httpx`, `python-dotenv`, `websockets`) and `[dev]` tooling (`pytest`, `ruff`).
- `vendor/Agent-S` declares its own runtime dependencies in `vendor/Agent-S/setup.py` (`gui-agents`), which remains unmodified.
- `backend/requirements.txt` serves as a portable editable development manifest installing `../vendor/Agent-S` and `.[dev]` without machine-specific paths.

## 4. Agent-S Integration Architecture (Planned for Milestone 2)

Robbie integrates with **`gui_agents.s3`** (`AgentS3` + `OSWorldACI`) programmatically rather than invoking `gui_agents.s3.cli_app` (which assumes an interactive terminal loop, global logging, and `SIGINT` handlers).

Key architectural constraints for the integration layer:
1. **Worker Isolation**: `AgentS3.predict()` performs synchronous LLM/grounding HTTP calls with retries and `time.sleep()`. It must execute in a background worker thread (`asyncio.to_thread`) or isolated worker subprocess so FastAPI's `asyncio` event loop remains responsive for WebSocket updates, health checks, and emergency cancellation.
2. **Windows ACI Subclass (`RobbieWindowsACI`)**: Subclass `OSWorldACI` inside `backend/robbie/agents/` to override Windows-incompatible behaviors (such as application switching and shell tooling) without touching `vendor/Agent-S`.
3. **Controlled Action Dispatch**: `AgentS3.predict()` returns generated Python/PyAutoGUI action strings. Rather than passing raw strings blindly to `exec()`, Robbie will validate/parse actions, request user approval according to policy, and dispatch through a constrained Windows executor.

## 5. Security Boundaries (Implemented in Milestone 1)

- **Renderer Isolation**: Electron enforces `contextIsolation: true`, `nodeIntegration: false`, `sandbox: true`, `webSecurity: true`, and `allowRunningInsecureContent: false`. Preload compiles to CommonJS (`dist-electron/preload.cjs`) for full Electron 30 sandbox compatibility and exposes only the minimal typed `window.robbie` facade (never raw `ipcRenderer` or Node APIs). Navigation outside the app origin and popups are denied, and `vite-plugin-electron-renderer` has been removed.
- **Strict Content Security Policy (CSP)**: Enforced via Electron `session.defaultSession.webRequest.onHeadersReceived` (scoped to Vite HMR origin in dev; `connect-src 'none'` in production because all backend communication passes through the Main-process IPC bridge) plus an injected production `<meta http-equiv="Content-Security-Policy">` tag in `dist/index.html`.
- **Zero-Token-in-Renderer Sidecar Auth**: Electron Main generates a fresh 256-bit cryptographically random bearer token (`crypto.randomBytes(32)`) on every backend launch and passes it to Python exclusively via the child process environment (`ROBBIE_API_TOKEN`, never CLI argv). Electron Main owns the authenticated HTTP (`/health`) and RFC 6455 WebSocket (`/ws/status`) client (`Origin: robbie://desktop`, `Authorization: Bearer <token>`), keeping the bearer token out of Renderer memory and logs. Both Python logging (`TokenRedactingFilter`) and Electron Main sanitize any accidental token appearance.
- **Loopback Binding & Windows Process Management**: FastAPI validates `host == "127.0.0.1"` (rejecting `0.0.0.0`) and binds an ephemeral port (`port=0`) with `SO_EXCLUSIVEADDRUSE` on Windows to prevent loopback port hijacking. Because Windows lacks POSIX `SIGTERM` for child processes, Electron spawns Python with `ROBBIE_WATCH_STDIN=1` and initiates graceful shutdown by closing the child's stdin pipe (also preventing orphans if Electron exits abruptly), falling back to `taskkill.exe /PID <pid> /T /F` via `execFile` (no shell interpolation) if the grace period expires.
- **Packaged vs. Development Executable Resolution**: `electron/backend/paths.ts` separates monorepo development (`<repo>/backend/.venv/Scripts/python.exe -m robbie`) from packaged `extraResources` paths (`<resourcesPath>/backend/robbie-backend.exe` or `<resourcesPath>/backend/venv/Scripts/python.exe`). Actual Python runtime bundling into the Windows NSIS installer remains scoped for Milestone 5.
- **Credential Storage (Planned for Milestone 4)**: Provider API keys are kept out of source control and will be persisted using OS-backed encryption (Electron `safeStorage` / Windows Credential Manager).

## 6. Known Technical Risks

The following risks were identified during repository analysis and are explicitly tracked for upcoming milestones:

1. **Dynamic Python Code Execution in Agent-S S3**
   - `gui_agents/s3/utils/common_utils.py` invokes `eval()` on LLM-generated action calls during `predict()`, and `AgentS3.predict()` emits executable Python/PyAutoGUI code strings intended for `exec()`. Enabling `LocalEnv` also executes arbitrary Python/shell snippets with user privileges.
   - *Mitigation target (Milestones 2–3):* Keep `LocalEnv` disabled by default, wrap/validate action dispatch with strict allowlisting, enforce human-in-the-loop approvals, and add a global emergency-stop hotkey.
2. **Linux-Specific Execution Paths in Agent-S**
   - `LocalController.run_bash_script` (`gui_agents/s3/utils/local_env.py`) hardcodes `/bin/bash`, `CODE_AGENT_PROMPT` and `PROCEDURAL_MEMORY` contain OSWorld Linux `sudo` instructions, and `OSWorldACI.type` includes an `apt-get install xclip xsel` fallback.
   - *Mitigation target (Milestone 2):* Keep `LocalEnv` off by default and override incompatible methods in Robbie's `OSWorldACI` subclass.
3. **Windows Application Switching Requires Adaptation**
   - `OSWorldACI.switch_applications` on Windows sends `win+d` (Show Desktop) followed by typing the app name, which minimizes open windows instead of focusing the target application.
   - *Mitigation target (Milestone 2):* Override `switch_applications` in Robbie's Windows ACI using `pywinauto`/`win32gui` window enumeration and focus activation.
4. **OCR Requires Native Tesseract Installation**
   - `OSWorldACI.get_ocr_elements` (used by `highlight_text_span`) depends on `pytesseract`, which requires the native Tesseract OCR binary installed and accessible on `PATH` (or configured via `TESSERACT_CMD`).
   - *Mitigation target (Milestones 2 & 5):* Detect Tesseract availability gracefully at startup/runtime, degrade cleanly when unavailable, and document optional installation.
5. **Separate Worker and Grounding Model Configurations**
   - Agent-S S3 requires both a main VLM (`worker_engine_params`) and a visual grounding endpoint (`engine_params_for_grounding`, e.g., UI-TARS with explicit coordinate dimensions), increasing setup complexity, latency, and cost.
   - *Mitigation target (Milestones 2 & 4):* Provide validated provider schemas, clear UI setup flows, and local/cloud presets.
6. **Synchronous Model Calls Blocking FastAPI's Event Loop**
   - All `gui_agents.s3.core.engine` clients are synchronous (`OpenAI`, `Anthropic`, `AzureOpenAI`) with synchronous `@backoff` retries (up to 60s) and `time.sleep()` loops.
   - *Mitigation target (Milestone 2):* Execute Agent-S turns outside the asyncio loop in a managed worker thread/process communicating via thread-safe queues.
7. **Localhost API Communication Requires Authentication**
   - An unauthenticated localhost HTTP/WebSocket server on a developer or end-user machine exposes desktop control to cross-origin/local attacks.
   - *Mitigation target (Milestone 1):* Bind exclusively to `127.0.0.1`, enforce strict CORS, and require a per-session secret token on every HTTP and WebSocket request.

Additional environmental considerations to track:
- **Windows DPI Scaling, Multi-Monitor & UAC**: Ensure explicit DPI awareness so screenshot pixels match logical input coordinates, handle Robbie's own window visibility during screen capture, and surface clear errors when targeting UAC-elevated windows.
- **Packaging Footprint**: `gui-agents` pulls heavy transitive packages (`paddlepaddle`, `paddleocr`, `opencv-contrib-python`, `selenium`), which will require careful packaging evaluation in Milestone 5.
- **OneDrive-Synced Workspaces**: Keeping `.git`, `node_modules`, `.venv`, and local SQLite databases inside a OneDrive-synced folder can cause file locks (`EPERM`) and sync conflicts during development.
