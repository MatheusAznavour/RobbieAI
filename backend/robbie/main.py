"""FastAPI application factory and Uvicorn sidecar entrypoint for Robbie."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import robbie
from robbie.agents.conversation import ChatModelClientProtocol, ConversationOrchestrator
from robbie.agents.runner import TaskRunner
from robbie.api.routes import router as api_router
from robbie.core.config import BackendSettings
from robbie.core.runtime import BackendRuntime
from robbie.core.security import configure_logging
from robbie.memory.conversations import ConversationStore, resolve_data_dir
from robbie.providers.settings import ProviderSettingsManager

logger = logging.getLogger("robbie.main")


def create_app(
    settings: BackendSettings | None = None,
    *,
    task_runner: TaskRunner | None = None,
    conversation_store: ConversationStore | None = None,
    settings_manager: ProviderSettingsManager | None = None,
    chat_client: ChatModelClientProtocol | None = None,
) -> FastAPI:
    """Create and configure the Robbie FastAPI application."""
    resolved_settings = settings or BackendSettings.from_env()
    configure_logging(
        resolved_settings.log_level,
        resolved_settings.api_token.get_secret_value(),
    )

    data_dir = resolve_data_dir()
    runtime = BackendRuntime(host=resolved_settings.host, port=resolved_settings.port)
    resolved_runner = task_runner or TaskRunner(broadcaster=runtime.connections.broadcast_json)
    resolved_store = conversation_store or ConversationStore(
        storage_path=data_dir / "conversations.json"
    )
    resolved_settings_mgr = settings_manager or ProviderSettingsManager(
        storage_path=data_dir / "settings.json"
    )
    orchestrator = ConversationOrchestrator(
        store=resolved_store,
        settings_manager=resolved_settings_mgr,
        task_runner=resolved_runner,
        broadcaster=runtime.connections.broadcast_json,
        chat_client=chat_client,
    )
    resolved_runner.set_on_task_finished(orchestrator.handle_task_terminal_state)

    runtime.task_runner = resolved_runner
    runtime.conversation_store = resolved_store
    runtime.settings_manager = resolved_settings_mgr
    runtime.orchestrator = orchestrator

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.runtime.mark_ready()
        logger.info(
            "Robbie backend starting on %s:%s (pid=%s)",
            app.state.runtime.host,
            app.state.runtime.port,
            app.state.runtime.pid,
        )
        try:
            yield
        finally:
            app.state.runtime.mark_shutting_down()
            if app.state.runtime.task_runner is not None:
                await app.state.runtime.task_runner.shutdown()
            await app.state.runtime.connections.close_all()
            logger.info("Robbie backend shutdown complete.")

    app = FastAPI(
        title="Robbie Local Backend",
        version=robbie.__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.runtime = runtime

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(resolved_settings.allowed_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )

    app.include_router(api_router)
    return app


def bind_loopback_socket(host: str, port: int) -> socket.socket:
    """Bind a TCP socket strictly on the loopback interface.

    Passing port=0 lets the OS atomically select an available ephemeral port.
    On Windows, SO_EXCLUSIVEADDRUSE is enabled to prevent loopback port hijacking.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
        sock.listen(128)
        sock.setblocking(False)
    except Exception:
        sock.close()
        raise
    return sock


class RobbieUvicornServer(uvicorn.Server):
    """Uvicorn server subclass that emits a structured readiness line once listening."""

    def __init__(self, config: uvicorn.Config, app: FastAPI, bound_port: int) -> None:
        super().__init__(config)
        self._robbie_app = app
        self._bound_port = bound_port

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        self._robbie_app.state.runtime.set_bound_port(self._bound_port)
        await super().startup(sockets=sockets)
        if not self.should_exit:
            ready_event = {
                "event": "robbie_backend_ready",
                "host": self._robbie_app.state.runtime.host,
                "port": self._bound_port,
                "pid": os.getpid(),
                "version": robbie.__version__,
            }
            # Never emit the bearer token to stdout/stderr.
            sys.stdout.write(json.dumps(ready_event) + "\n")
            sys.stdout.flush()


def _start_stdin_shutdown_watcher(
    server: uvicorn.Server,
    loop: asyncio.AbstractEventLoop,
) -> threading.Thread:
    """Watch stdin in a daemon thread so pipe closure triggers graceful shutdown.

    On Windows, child process termination does not deliver POSIX SIGTERM. Closing
    the parent-owned stdin pipe (either explicitly during shutdown or by the OS if
    Electron exits unexpectedly) signals the backend to shut down cleanly and
    prevents orphaned processes.
    """

    def _watch() -> None:
        try:
            while True:
                line = sys.stdin.readline()
                if line == "" or line.strip().lower() == "shutdown":
                    break
        except Exception:
            pass
        finally:
            if not loop.is_closed():
                loop.call_soon_threadsafe(setattr, server, "should_exit", True)

    thread = threading.Thread(target=_watch, name="robbie-stdin-watcher", daemon=True)
    thread.start()
    return thread


async def serve_backend(settings: BackendSettings | None = None) -> None:
    """Bind a loopback socket and run Uvicorn cleanly until shutdown."""
    resolved_settings = settings or BackendSettings.from_env()
    app = create_app(resolved_settings)
    sock = bind_loopback_socket(resolved_settings.host, resolved_settings.port)
    bound_port = int(sock.getsockname()[1])
    app.state.runtime.set_bound_port(bound_port)

    config = uvicorn.Config(
        app=app,
        host=resolved_settings.host,
        port=bound_port,
        log_level=resolved_settings.log_level.lower(),
        access_log=False,
    )
    server = RobbieUvicornServer(config=config, app=app, bound_port=bound_port)

    if os.environ.get("ROBBIE_WATCH_STDIN", "").strip().lower() in {"1", "true", "yes"}:
        _start_stdin_shutdown_watcher(server, asyncio.get_running_loop())

    try:
        await server.serve(sockets=[sock])
    finally:
        sock.close()


def main() -> None:
    """CLI entrypoint for launching the Robbie backend sidecar."""
    from dotenv import load_dotenv

    load_dotenv(override=False)
    settings = BackendSettings.from_env()
    asyncio.run(serve_backend(settings))


if __name__ == "__main__":
    main()
