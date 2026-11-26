<p align="center">
  <img src="assets/Robbie.jpg">
</p>

<h1 align="center">Robbie</h1>

<p align="center">
  <strong>A new way to interact with your computer.</strong>
</p>

**An AI-powered Windows desktop assistant built with Electron, Python, and Agent-S.**

Robbie is a desktop AI agent designed to understand natural language instructions, interact with Windows applications, and execute computer-based tasks through a conversational interface.

Unlike a traditional chatbot, Robbie can interact with the desktop environment, observe the screen, locate interface elements, and perform actions such as clicking, typing, scrolling, and opening applications.

## Overview

Robbie combines a conversational AI interface with an autonomous desktop interaction engine.

The project uses **[Agent-S](https://github.com/simular-ai/Agent-S)** as its core desktop interaction framework. Agent-S provides the visual computer-use capabilities that allow Robbie to understand and interact with graphical user interfaces.

Robbie builds on top of Agent-S with its own application architecture, orchestration layer, Windows integration, configuration system, and safety controls.

The main goals are:

- Provide a natural conversational interface for interacting with an AI assistant.
- Execute desktop tasks using natural language instructions.
- Support configurable AI providers and OpenAI-compatible APIs.
- Separate conversational reasoning from desktop execution.
- Provide task monitoring, cancellation, and emergency stopping.
- Maintain a modular architecture that can evolve into a more autonomous desktop agent.

## Architecture

Robbie is built around three main components:

### 1. Desktop Application — Electron

The desktop application is built with Electron, React, and TypeScript.

It provides:

- Conversational chat interface.
- Conversation history.
- Desktop task visualization.
- Human approval prompts.
- Task execution controls.
- Emergency Stop functionality.
- Application settings and model configuration.

The Electron Main process manages the Python backend lifecycle and communicates with it through a restricted IPC bridge.

The renderer does not receive the backend authentication token.

### 2. Backend — Python and FastAPI

The backend is responsible for application orchestration and communication between the desktop interface and the AI execution engine.

Its responsibilities include:

- API endpoints and authentication.
- Conversation management.
- AI provider configuration.
- Desktop task orchestration.
- Action validation and safety policies.
- Approval management.
- Task lifecycle management.
- WebSocket event streaming.
- Conversation persistence.

The backend runs as a local sidecar process managed by Electron.

### 3. Desktop Interaction Engine — Agent-S

Robbie uses **Agent-S (Simular AI)** as its desktop interaction engine.

Agent-S is responsible for the visual computer-use workflow, including:

- Understanding screenshots.
- Identifying interface elements.
- Grounding visual references into screen coordinates.
- Planning desktop interactions.
- Clicking and typing.
- Scrolling and dragging.
- Interacting with Windows applications.

Robbie integrates Agent-S through its own adapter rather than replacing or directly modifying its upstream implementation.

The Agent-S repository is maintained as a Git submodule under:

`vendor/Agent-S`

This keeps the upstream framework separate from Robbie's application-specific code.

## AI Provider Configuration

Robbie separates conversational AI from desktop execution.

The architecture supports three model roles:

| Component | Responsibility |
|---|---|
| Conversational Model | Understands user messages and generates responses. |
| Desktop Worker Model | Plans and executes desktop interactions through Agent-S. |
| Visual Grounding Model | Identifies interface elements and their screen positions. |

The backend supports OpenAI-compatible providers and configurable API endpoints.

This makes it possible to use different providers and models for each role, depending on their capabilities and performance.

Provider configuration is managed through environment variables and application settings.

## Safety and Execution Controls

Robbie includes an independent safety layer between AI-generated actions and actual desktop execution.

Its purpose is to prevent the AI model from directly bypassing application-level execution policies.

Current safety-related capabilities include:

- Structured desktop action validation.
- Restricted action parsing.
- Sensitive application launch classification.
- Human approval workflows.
- Action-bound approval validation.
- Emergency Stop with a persistent stop latch.
- Authenticated backend endpoints.
- Secret redaction in relevant application events and errors.

The safety layer is implemented by Robbie and is separate from the underlying AI model.

Changing the model or provider does not automatically change Robbie's execution policies.

## Technology Stack

| Component | Technology |
|---|---|
| Desktop Framework | Electron |
| Frontend | React |
| Language | TypeScript |
| Backend | Python |
| API Framework | FastAPI |
| Desktop Interaction | Agent-S |
| AI Integration | OpenAI-compatible APIs |
| Communication | HTTP and WebSockets |
| Testing | Pytest, TypeScript, ESLint |
| Version Control | Git and Git Submodules |

## Getting Started

### Requirements

- Windows 10 or Windows 11.
- Python 3.11.
- Node.js and npm.
- Git.
- Git support for submodules.
- An API key from a compatible AI provider.

Additional dependencies required by Agent-S may depend on the selected desktop execution configuration.

### Clone the Repository

```bash
git clone --recurse-submodules https://github.com/MatheusAznavour/RobbieAI.git

cd RobbieAI
```

If the repository was cloned without submodules:

```bash
git submodule update --init --recursive
```

### Configure the Backend

Create a Python virtual environment:

```powershell
cd backend

py -3.11 -m venv .venv

.\.venv\Scripts\Activate.ps1

pip install -e ".[dev]"
```

Return to the project root:

```powershell
cd ..
```

Create your local environment file using `.env.example` as a reference:

```powershell
Copy-Item .env.example .env
```

Configure your provider credentials, model names, and API endpoints in `.env`.

**Never commit your `.env` file or expose API keys in source control.**

### Install Desktop Dependencies

```powershell
cd apps/desktop

npm install
```

Use the project's development scripts to launch Robbie.

Refer to the development documentation and scripts included in the repository for the current startup workflow.

## Development and Testing

Robbie uses automated tests to validate backend behavior, desktop integration, and frontend consistency.

Backend tests:

```powershell
cd backend

.\.venv\Scripts\Activate.ps1

pytest
```

Backend linting:

```powershell
ruff check .
ruff format --check .
```

Frontend validation:

```powershell
cd apps/desktop

npm run typecheck
npm run lint
npm run build
```

Run the Electron desktop lifecycle tests using the test scripts configured in the desktop project.

## Current Capabilities

The current implementation includes:

- Conversational desktop interface.
- Configurable AI providers.
- Separate conversational, worker, and grounding model roles.
- Agent-S desktop execution integration.
- Windows application launching.
- Human approval workflows.
- Task execution and event tracking.
- Pause, resume, cancellation, and Emergency Stop foundations.
- Authenticated local backend communication.
- Persistent conversation storage.
- Secret sanitization.
- Automated backend and frontend tests.

## Current Limitations

Robbie is an actively developed project. Some capabilities are still being improved.

Known limitations include:

- Desktop execution may take several seconds to initialize.
- Long-running model requests cannot always be interrupted immediately.
- Multi-monitor support is currently limited.
- Interaction with elevated Windows applications is restricted by Windows security boundaries.
- OCR availability depends on optional local tooling.
- Conversation persistence currently uses bounded JSON storage.
- File and image attachments are not yet fully integrated into the conversational workflow.
- Live token streaming is not yet implemented.
- Human approval requirements may interrupt otherwise straightforward workflows.

## Roadmap

Planned development areas include:

- Improved autonomous task execution.
- Configurable autonomy and approval profiles.
- Better task recovery and failure handling.
- Enhanced visual execution observability.
- Persistent memory independent of conversation history.
- Multimodal input and file attachments.
- Improved adversarial security testing.
- SQLite-based persistence.
- Windows installer and distribution workflow.
- Improved diagnostics and application logging.

## Design Philosophy

Robbie is intended to become a flexible desktop AI assistant rather than a simple chatbot with a collection of automation scripts.

Its architecture separates the conversational interface, AI orchestration, desktop interaction engine, and execution safety layer.

This separation allows the project to evolve independently across its different components while keeping Agent-S as the foundation for visual desktop interaction.

## Acknowledgements

Robbie uses **[Agent-S](https://github.com/simular-ai/Agent-S)** by Simular AI as its desktop interaction framework.

Agent-S is an upstream open-source project and remains maintained separately from Robbie.
