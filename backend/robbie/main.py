"""FastAPI application factory and Uvicorn sidecar entrypoint for Robbie."""

from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
import socket
import sys
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final, Protocol

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


_STDIN_POLL_INTERVAL_SECONDS: Final[float] = 0.15
_STDIN_MAX_READ_BYTES: Final[int] = 4096
_STDIN_MAX_LINE_BYTES: Final[int] = 4096

_WIN32_FILE_TYPE_PIPE: Final[int] = 0x0003
_WIN32_ERROR_HANDLE_EOF: Final[int] = 38
_WIN32_ERROR_BROKEN_PIPE: Final[int] = 109
_WIN32_ERROR_NO_DATA: Final[int] = 232
_WIN32_ERROR_PIPE_NOT_CONNECTED: Final[int] = 233
_WIN32_PIPE_EOF_ERRORS: Final[frozenset[int]] = frozenset(
    {
        _WIN32_ERROR_HANDLE_EOF,
        _WIN32_ERROR_BROKEN_PIPE,
        _WIN32_ERROR_NO_DATA,
        _WIN32_ERROR_PIPE_NOT_CONNECTED,
    }
)


class _WindowsStdinPipeAPI(Protocol):
    """Minimal Win32 pipe inspection/read surface for non-blocking stdin watching."""

    def get_stdin_handle(self, stdin_stream: Any) -> int: ...

    def get_file_type(self, handle: int) -> tuple[int, int]: ...

    def peek_named_pipe(self, handle: int) -> tuple[bool, int, int]: ...

    def read_file(self, handle: int, max_bytes: int) -> tuple[bool, bytes, int]: ...


class _Win32Kernel32PipeAPI:
    """ctypes wrapper around kernel32 GetFileType, PeekNamedPipe, and ReadFile."""

    def __init__(self) -> None:
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        kernel32.GetFileType.argtypes = [wintypes.HANDLE]
        kernel32.GetFileType.restype = wintypes.DWORD

        kernel32.PeekNamedPipe.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.PeekNamedPipe.restype = wintypes.BOOL

        kernel32.ReadFile.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        kernel32.ReadFile.restype = wintypes.BOOL

        self._kernel32 = kernel32
        self._dword = wintypes.DWORD

    def get_stdin_handle(self, stdin_stream: Any) -> int:
        import msvcrt

        fd = stdin_stream.fileno()
        handle = int(msvcrt.get_osfhandle(fd))
        if handle in (0, -1):
            raise OSError("Invalid Windows stdin handle.")
        return handle

    def get_file_type(self, handle: int) -> tuple[int, int]:
        ctypes.set_last_error(0)
        file_type = int(self._kernel32.GetFileType(handle))
        return file_type, int(ctypes.get_last_error())

    def peek_named_pipe(self, handle: int) -> tuple[bool, int, int]:
        total_bytes_avail = self._dword(0)
        ctypes.set_last_error(0)
        ok = bool(
            self._kernel32.PeekNamedPipe(
                handle,
                None,
                0,
                None,
                ctypes.byref(total_bytes_avail),
                None,
            )
        )
        return ok, int(total_bytes_avail.value), int(ctypes.get_last_error())

    def read_file(self, handle: int, max_bytes: int) -> tuple[bool, bytes, int]:
        buf = ctypes.create_string_buffer(max_bytes)
        bytes_read = self._dword(0)
        ctypes.set_last_error(0)
        ok = bool(
            self._kernel32.ReadFile(
                handle,
                buf,
                max_bytes,
                ctypes.byref(bytes_read),
                None,
            )
        )
        count = int(bytes_read.value)
        return ok, bytes(buf.raw[:count]), int(ctypes.get_last_error())


def _signal_server_shutdown(server: Any, loop: asyncio.AbstractEventLoop) -> None:
    """Thread-safely signal the Uvicorn server to exit on its asyncio event loop."""
    if not loop.is_closed():
        try:
            loop.call_soon_threadsafe(setattr, server, "should_exit", True)
        except RuntimeError:
            # Event loop closed between check and scheduling.
            pass


def _consume_stdin_lines(
    pending: bytearray,
    chunk: bytes,
    *,
    discarding_oversized_line: bool,
) -> tuple[bool, bool]:
    """Append incoming bytes and return (shutdown_requested, discarding_oversized_line)."""
    data = chunk
    if discarding_oversized_line:
        newline_idx = data.find(b"\n")
        if newline_idx == -1:
            return False, True
        data = data[newline_idx + 1 :]
        discarding_oversized_line = False

    pending.extend(data)
    while True:
        newline_idx = pending.find(b"\n")
        if newline_idx == -1:
            if len(pending) > _STDIN_MAX_LINE_BYTES:
                pending.clear()
                return False, True
            return False, False

        raw_line = bytes(pending[:newline_idx])
        del pending[: newline_idx + 1]
        if raw_line.decode("utf-8", errors="replace").strip().lower() == "shutdown":
            return True, False


def _watch_windows_stdin_pipe(
    server: Any,
    loop: asyncio.AbstractEventLoop,
    *,
    stdin_stream: Any,
    pipe_api: _WindowsStdinPipeAPI,
    poll_interval_seconds: float,
    stop_event: threading.Event,
) -> None:
    """Non-blocking Windows stdin pipe watcher using PeekNamedPipe + bounded ReadFile.

    Avoids holding a synchronous blocking ReadFile on Windows' stdin pipe handle,
    which would otherwise deadlock native DLL loads (such as NumPy/OpenBLAS) that
    inspect standard handles while holding the Windows Loader Lock.
    """
    if stdin_stream is None:
        logger.warning("Windows stdin watcher disabled: stdin stream is unavailable.")
        return

    try:
        handle = pipe_api.get_stdin_handle(stdin_stream)
    except Exception as exc:
        logger.warning(
            "Windows stdin watcher could not resolve stdin handle (%s); "
            "stopping watcher without triggering shutdown.",
            exc,
        )
        return

    file_type, type_err = pipe_api.get_file_type(handle)
    if file_type != _WIN32_FILE_TYPE_PIPE:
        logger.warning(
            "Windows stdin watcher disabled: stdin is not a pipe (file_type=%d, winerror=%d).",
            file_type,
            type_err,
        )
        return

    pending = bytearray()
    discarding_oversized_line = False

    while not stop_event.is_set() and not getattr(server, "should_exit", False):
        ok, avail, err_code = pipe_api.peek_named_pipe(handle)
        if not ok:
            if err_code in _WIN32_PIPE_EOF_ERRORS:
                logger.info(
                    "Stdin pipe closed (winerror=%d); requesting graceful shutdown.",
                    err_code,
                )
                _signal_server_shutdown(server, loop)
                return
            logger.warning(
                "Unexpected Win32 error from PeekNamedPipe (winerror=%d); "
                "stopping stdin watcher without treating as normal EOF.",
                err_code,
            )
            return

        if avail <= 0:
            stop_event.wait(timeout=poll_interval_seconds)
            continue

        to_read = min(avail, _STDIN_MAX_READ_BYTES)
        read_ok, chunk, read_err = pipe_api.read_file(handle, to_read)
        if not read_ok:
            if read_err in _WIN32_PIPE_EOF_ERRORS:
                logger.info(
                    "Stdin pipe closed during read (winerror=%d); requesting graceful shutdown.",
                    read_err,
                )
                _signal_server_shutdown(server, loop)
                return
            logger.warning(
                "Unexpected Win32 error from ReadFile on stdin pipe (winerror=%d); "
                "stopping stdin watcher without treating as normal EOF.",
                read_err,
            )
            return

        if not chunk:
            logger.info("Stdin pipe reached EOF; requesting graceful shutdown.")
            _signal_server_shutdown(server, loop)
            return

        shutdown_requested, discarding_oversized_line = _consume_stdin_lines(
            pending,
            chunk,
            discarding_oversized_line=discarding_oversized_line,
        )
        if shutdown_requested:
            logger.info("Received shutdown command on stdin pipe; requesting graceful shutdown.")
            _signal_server_shutdown(server, loop)
            return


def _watch_posix_stdin(
    server: Any,
    loop: asyncio.AbstractEventLoop,
    *,
    stdin_stream: Any,
    stop_event: threading.Event,
) -> None:
    """Line-oriented stdin watcher for non-Windows platforms."""
    if stdin_stream is None:
        return
    try:
        while not stop_event.is_set() and not getattr(server, "should_exit", False):
            line = stdin_stream.readline()
            if line == "" or line.strip().lower() == "shutdown":
                _signal_server_shutdown(server, loop)
                return
    except Exception as exc:
        logger.warning("Unexpected error reading stdin on non-Windows platform: %s", exc)


def _start_stdin_shutdown_watcher(
    server: Any,
    loop: asyncio.AbstractEventLoop,
    *,
    poll_interval_seconds: float = _STDIN_POLL_INTERVAL_SECONDS,
    stop_event: threading.Event | None = None,
    stdin_stream: Any = None,
    platform_name: str | None = None,
    pipe_api: _WindowsStdinPipeAPI | None = None,
) -> threading.Thread:
    """Watch stdin in a daemon thread so pipe closure or 'shutdown' triggers graceful shutdown.

    On Windows, child process termination does not deliver POSIX SIGTERM. Closing
    the parent-owned stdin pipe (either explicitly during shutdown or by the OS if
    Electron exits unexpectedly) signals the backend to shut down cleanly and
    prevents orphaned processes.

    On Windows, this uses non-blocking Win32 ``PeekNamedPipe`` polling plus bounded
    ``ReadFile`` calls strictly on the underlying OS handle (never mixing buffered
    ``sys.stdin.readline()`` calls) so no synchronous read locks the stdin handle
    during lazy native DLL loads.
    """
    resolved_stop_event = stop_event or threading.Event()
    resolved_stdin = sys.stdin if stdin_stream is None else stdin_stream
    resolved_platform = sys.platform if platform_name is None else platform_name

    if resolved_platform == "win32":
        resolved_pipe_api = pipe_api or _Win32Kernel32PipeAPI()

        def _watch() -> None:
            _watch_windows_stdin_pipe(
                server,
                loop,
                stdin_stream=resolved_stdin,
                pipe_api=resolved_pipe_api,
                poll_interval_seconds=poll_interval_seconds,
                stop_event=resolved_stop_event,
            )
    else:

        def _watch() -> None:
            _watch_posix_stdin(
                server,
                loop,
                stdin_stream=resolved_stdin,
                stop_event=resolved_stop_event,
            )

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

    watcher_stop_event = threading.Event()
    if os.environ.get("ROBBIE_WATCH_STDIN", "").strip().lower() in {"1", "true", "yes"}:
        _start_stdin_shutdown_watcher(
            server,
            asyncio.get_running_loop(),
            stop_event=watcher_stop_event,
        )

    try:
        await server.serve(sockets=[sock])
    finally:
        watcher_stop_event.set()
        sock.close()


def main() -> None:
    """CLI entrypoint for launching the Robbie backend sidecar."""
    from dotenv import load_dotenv

    load_dotenv(override=False)
    settings = BackendSettings.from_env()
    asyncio.run(serve_backend(settings))


if __name__ == "__main__":
    main()
