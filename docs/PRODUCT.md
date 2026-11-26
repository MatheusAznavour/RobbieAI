# Robbie — Product Specification

## 1. Vision

Robbie is a Windows desktop AI agent designed to bridge human intent and everyday computer operations. Rather than forcing users to translate goals into manual sequences of clicks, menus, and scripts, Robbie observes the desktop, plans steps, and operates native Windows and web applications safely under human supervision.

## 2. Target Platform & Audience

- **Primary Platform:** Microsoft Windows (Windows 10 / Windows 11, x64).
- **Target Users:** Power users, developers, analysts, and knowledge workers who want an interactive, transparent desktop assistant capable of automating multi-step GUI workflows with clear oversight and local control.

## 3. Core Capabilities (Target)

1. **Natural-Language Desktop Automation**
   - Accept plain-language instructions from a dedicated desktop application.
   - Visually perceive screen state and execute GUI actions (click, type, scroll, hotkeys, application switching) via an Agent-S S3 runtime adapter.
2. **Human-in-the-Loop Safety & Control**
   - Preview proposed actions and reasoning steps before execution.
   - Require explicit approval for sensitive or high-impact operations.
   - Provide an always-available global emergency stop / pause mechanism.
3. **Flexible Model & Grounding Providers**
   - Support cloud Vision-Language Models (OpenAI, Anthropic, Gemini, OpenRouter, Azure OpenAI) as well as local endpoints (Ollama, vLLM).
   - Configure dedicated visual grounding models (such as UI-TARS) alongside the main reasoning model.
4. **Local Session History & Auditability**
   - Store task sessions, step timelines, and audit logs locally in user-scoped Windows storage (`%LOCALAPPDATA%\Robbie`).

## 4. Design Principles

- **Safety First:** Desktop automation has direct access to user files and applications. Transparency, explicit permissions, and rapid cancellation take priority over unattended autonomy.
- **Clean Separation:** Keep third-party agent research code (`vendor/Agent-S`) isolated behind Robbie-owned adapters so upstream updates remain manageable.
- **Windows-Native Reliability:** Account for Windows-specific realities (DPI scaling, window focus management, UAC boundaries, PowerShell tooling, and NSIS packaging) instead of relying on Linux/macOS assumptions.

## 5. Non-Goals (Initial Milestones)

- Cross-platform macOS or Linux packaging.
- Autonomous, unattended background execution without user supervision.
- Forking or rewriting the upstream `Agent-S` framework.
- Cloud-hosted multi-tenant execution environments.
