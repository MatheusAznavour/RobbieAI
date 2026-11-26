"""Unit and Windows subprocess regression tests for the non-blocking stdin watcher."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import httpx
import pytest

from robbie.main import (
    _WIN32_ERROR_BROKEN_PIPE,
    _WIN32_FILE_TYPE_PIPE,
    _start_stdin_shutdown_watcher,
)


class _DummyServer:
    def __init__(self) -> None:
        self.should_exit = False


class _FakeLoop:
    """Minimal asyncio-loop stand-in recording call_soon_threadsafe invocations."""

    def __init__(self) -> None:
        self._closed = False
        self.calls: list[tuple[Any, tuple[Any, ...]]] = []

    def is_closed(self) -> bool:
        return self._closed

    def call_soon_threadsafe(self, callback: Any, *args: Any) -> None:
        self.calls.append((callback, args))
        callback(*args)


class _FakeWindowsPipeAPI:
    """Scripted Win32 pipe API for deterministic unit testing of the Windows watcher."""

    def __init__(
        self,
        *,
        file_type: int = _WIN32_FILE_TYPE_PIPE,
        file_type_err: int = 0,
        peeks: list[tuple[bool, int, int]] | None = None,
        reads: list[tuple[bool, bytes, int]] | None = None,
        handle_error: Exception | None = None,
    ) -> None:
        self._file_type = file_type
        self._file_type_err = file_type_err
        self._peeks: deque[tuple[bool, int, int]] = deque(peeks or [])
        self._reads: deque[tuple[bool, bytes, int]] = deque(reads or [])
        self._handle_error = handle_error
        self.peek_count = 0
        self.read_requests: list[int] = []

    def get_stdin_handle(self, stdin_stream: Any) -> int:
        if self._handle_error is not None:
            raise self._handle_error
        return 1234

    def get_file_type(self, handle: int) -> tuple[int, int]:
        return self._file_type, self._file_type_err

    def peek_named_pipe(self, handle: int) -> tuple[bool, int, int]:
        self.peek_count += 1
        if self._peeks:
            return self._peeks.popleft()
        return True, 0, 0

    def read_file(self, handle: int, max_bytes: int) -> tuple[bool, bytes, int]:
        self.read_requests.append(max_bytes)
        if self._reads:
            return self._reads.popleft()
        return True, b"", 0


def test_windows_watcher_empty_pipe_does_not_busy_loop_or_read() -> None:
    server = _DummyServer()
    loop = _FakeLoop()
    stop_event = threading.Event()
    pipe_api = _FakeWindowsPipeAPI()

    thread = _start_stdin_shutdown_watcher(
        server,
        loop,  # type: ignore[arg-type]
        poll_interval_seconds=0.05,
        stop_event=stop_event,
        stdin_stream=object(),
        platform_name="win32",
        pipe_api=pipe_api,
    )
    time.sleep(0.22)
    stop_event.set()
    thread.join(timeout=2.0)

    assert not thread.is_alive()
    assert pipe_api.read_requests == []
    # Polling at ~50ms for ~220ms should run a handful of polls, never a tight spin loop.
    assert 1 <= pipe_api.peek_count <= 15
    assert server.should_exit is False
    assert loop.calls == []


def test_windows_watcher_detects_shutdown_command_and_bounds_read() -> None:
    server = _DummyServer()
    loop = _FakeLoop()
    stop_event = threading.Event()
    payload = b"shutdown\n"
    pipe_api = _FakeWindowsPipeAPI(
        peeks=[(True, len(payload), 0)],
        reads=[(True, payload, 0)],
    )

    thread = _start_stdin_shutdown_watcher(
        server,
        loop,  # type: ignore[arg-type]
        poll_interval_seconds=0.02,
        stop_event=stop_event,
        stdin_stream=object(),
        platform_name="win32",
        pipe_api=pipe_api,
    )
    thread.join(timeout=2.0)

    assert not thread.is_alive()
    assert pipe_api.read_requests == [len(payload)]
    assert server.should_exit is True
    assert len(loop.calls) == 1


def test_windows_watcher_handles_split_shutdown_across_multiple_writes() -> None:
    server = _DummyServer()
    loop = _FakeLoop()
    stop_event = threading.Event()
    pipe_api = _FakeWindowsPipeAPI(
        peeks=[
            (True, 2, 0),
            (True, 0, 0),
            (True, 3, 0),
            (True, 0, 0),
            (True, 5, 0),
        ],
        reads=[
            (True, b"sh", 0),
            (True, b"utd", 0),
            (True, b"own\r\n", 0),
        ],
    )

    thread = _start_stdin_shutdown_watcher(
        server,
        loop,  # type: ignore[arg-type]
        poll_interval_seconds=0.01,
        stop_event=stop_event,
        stdin_stream=object(),
        platform_name="win32",
        pipe_api=pipe_api,
    )
    thread.join(timeout=2.0)

    assert not thread.is_alive()
    assert pipe_api.read_requests == [2, 3, 5]
    assert server.should_exit is True
    assert len(loop.calls) == 1


def test_windows_watcher_handles_broken_pipe_eof() -> None:
    server = _DummyServer()
    loop = _FakeLoop()
    stop_event = threading.Event()
    pipe_api = _FakeWindowsPipeAPI(
        peeks=[
            (True, 0, 0),
            (False, 0, _WIN32_ERROR_BROKEN_PIPE),
        ],
    )

    thread = _start_stdin_shutdown_watcher(
        server,
        loop,  # type: ignore[arg-type]
        poll_interval_seconds=0.01,
        stop_event=stop_event,
        stdin_stream=object(),
        platform_name="win32",
        pipe_api=pipe_api,
    )
    thread.join(timeout=2.0)

    assert not thread.is_alive()
    assert pipe_api.read_requests == []
    assert server.should_exit is True
    assert len(loop.calls) == 1


def test_windows_watcher_handles_unexpected_win32_error_without_treating_as_eof(
    caplog: pytest.LogCaptureFixture,
) -> None:
    server = _DummyServer()
    loop = _FakeLoop()
    stop_event = threading.Event()
    # ERROR_ACCESS_DENIED (5) is not an EOF / broken-pipe code.
    pipe_api = _FakeWindowsPipeAPI(peeks=[(False, 0, 5)])

    with caplog.at_level(logging.WARNING, logger="robbie.main"):
        thread = _start_stdin_shutdown_watcher(
            server,
            loop,  # type: ignore[arg-type]
            poll_interval_seconds=0.01,
            stop_event=stop_event,
            stdin_stream=object(),
            platform_name="win32",
            pipe_api=pipe_api,
        )
        thread.join(timeout=2.0)

    assert not thread.is_alive()
    assert server.should_exit is False
    assert loop.calls == []
    assert any("winerror=5" in record.getMessage() for record in caplog.records)


def test_windows_watcher_skips_non_pipe_stdin_without_blocking_readline(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _ForbidReadline:
        def readline(self) -> str:
            raise AssertionError("readline() must never be called on Windows")

    server = _DummyServer()
    loop = _FakeLoop()
    stop_event = threading.Event()
    pipe_api = _FakeWindowsPipeAPI(file_type=2)  # FILE_TYPE_CHAR

    with caplog.at_level(logging.WARNING, logger="robbie.main"):
        thread = _start_stdin_shutdown_watcher(
            server,
            loop,  # type: ignore[arg-type]
            poll_interval_seconds=0.01,
            stop_event=stop_event,
            stdin_stream=_ForbidReadline(),
            platform_name="win32",
            pipe_api=pipe_api,
        )
        thread.join(timeout=2.0)

    assert not thread.is_alive()
    assert pipe_api.peek_count == 0
    assert pipe_api.read_requests == []
    assert server.should_exit is False
    assert any("not a pipe" in record.getMessage() for record in caplog.records)


def test_windows_watcher_discards_oversized_line_and_still_detects_shutdown() -> None:
    server = _DummyServer()
    loop = _FakeLoop()
    stop_event = threading.Event()
    huge = b"a" * 5000
    tail = b"\nshutdown\n"
    pipe_api = _FakeWindowsPipeAPI(
        peeks=[
            (True, len(huge), 0),
            (True, 904, 0),
            (True, len(tail), 0),
        ],
        reads=[
            (True, huge[:4096], 0),
            (True, huge[4096:], 0),
            (True, tail, 0),
        ],
    )

    thread = _start_stdin_shutdown_watcher(
        server,
        loop,  # type: ignore[arg-type]
        poll_interval_seconds=0.01,
        stop_event=stop_event,
        stdin_stream=object(),
        platform_name="win32",
        pipe_api=pipe_api,
    )
    thread.join(timeout=2.0)

    assert not thread.is_alive()
    # Read size is capped to _STDIN_MAX_READ_BYTES (4096) and never exceeds avail.
    assert pipe_api.read_requests == [4096, 904, len(tail)]
    assert server.should_exit is True


@pytest.mark.parametrize("payload", ["shutdown\n", "hello\nshutdown\n", ""])
def test_posix_stdin_watcher_preserves_readline_behavior(payload: str) -> None:
    server = _DummyServer()
    loop = _FakeLoop()
    stop_event = threading.Event()

    thread = _start_stdin_shutdown_watcher(
        server,
        loop,  # type: ignore[arg-type]
        stop_event=stop_event,
        stdin_stream=io.StringIO(payload),
        platform_name="linux",
    )
    thread.join(timeout=2.0)

    assert not thread.is_alive()
    assert server.should_exit is True
    assert len(loop.calls) == 1


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only pipe/DLL regression test")
def test_windows_empty_stdin_pipe_does_not_block_numpy_import() -> None:
    """Verify the real Win32 PeekNamedPipe watcher never blocks lazy NumPy DLL loading."""
    script = (
        "import asyncio, threading, time\n"
        "from robbie.main import _start_stdin_shutdown_watcher\n"
        "class _Srv:\n"
        "    should_exit = False\n"
        "async def _run():\n"
        "    srv = _Srv()\n"
        "    stop = threading.Event()\n"
        "    t = _start_stdin_shutdown_watcher(\n"
        "        srv, asyncio.get_running_loop(), poll_interval_seconds=0.05, stop_event=stop\n"
        "    )\n"
        "    await asyncio.sleep(0.15)\n"
        "    t0 = time.perf_counter()\n"
        "    import numpy  # noqa: F401\n"
        "    print(f'NUMPY_IMPORTED {time.perf_counter() - t0:.3f}', flush=True)\n"
        "    for _ in range(100):\n"
        "        if srv.should_exit:\n"
        "            break\n"
        "        await asyncio.sleep(0.05)\n"
        "    print(f'SHUTDOWN_FLAG {srv.should_exit}', flush=True)\n"
        "    stop.set()\n"
        "    t.join(timeout=2.0)\n"
        "asyncio.run(_run())\n"
    )

    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    assert proc.stderr is not None

    try:
        # Keep stdin open and empty while the child runs the watcher + imports numpy.
        first_line = ""
        read_done = threading.Event()

        def _read_first() -> None:
            nonlocal first_line
            first_line = proc.stdout.readline().strip()  # type: ignore[union-attr]
            read_done.set()

        reader = threading.Thread(target=_read_first, daemon=True)
        reader.start()
        assert read_done.wait(timeout=10.0), "import numpy deadlocked with empty Windows stdin pipe"
        assert first_line.startswith("NUMPY_IMPORTED "), f"Unexpected stdout: {first_line!r}"

        # Now close stdin and verify the watcher sets should_exit = True.
        proc.stdin.close()
        second_line = proc.stdout.readline().strip()
        assert second_line == "SHUTDOWN_FLAG True"
        assert proc.wait(timeout=10.0) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5.0)


def _spawn_windows_backend(tmp_path: Path, token: str) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env.update(
        {
            "ROBBIE_BACKEND_HOST": "127.0.0.1",
            "ROBBIE_BACKEND_PORT": "0",
            "ROBBIE_API_TOKEN": token,
            "ROBBIE_ALLOWED_ORIGINS": "robbie://desktop",
            "ROBBIE_WATCH_STDIN": "1",
            "ROBBIE_DATA_DIR": str(tmp_path),
            "ROBBIE_LOG_LEVEL": "INFO",
            "PYTHONUNBUFFERED": "1",
        }
    )
    return subprocess.Popen(
        [sys.executable, "-m", "robbie"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def _wait_for_ready_event(proc: subprocess.Popen[str], timeout: float = 15.0) -> dict[str, Any]:
    assert proc.stdout is not None
    result: dict[str, Any] = {}
    ready_event = threading.Event()
    stdout_lines: list[str] = []

    def _drain_stdout() -> None:
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            line = raw_line.strip()
            if not line:
                continue
            stdout_lines.append(line)
            if not ready_event.is_set() and line.startswith("{"):
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if parsed.get("event") == "robbie_backend_ready":
                    result.update(parsed)
                    ready_event.set()

    def _drain_stderr() -> None:
        assert proc.stderr is not None
        for _ in proc.stderr:
            pass

    threading.Thread(target=_drain_stdout, daemon=True).start()
    threading.Thread(target=_drain_stderr, daemon=True).start()

    assert ready_event.wait(timeout=timeout), (
        f"Backend did not emit robbie_backend_ready within {timeout}s "
        f"(rc={proc.poll()}, stdout={stdout_lines})"
    )
    return result


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows-specific sidecar stdin pipe + lazy Agent-S/NumPy import regression test",
)
def test_windows_backend_subprocess_no_deadlock_during_lazy_adapter_import(
    tmp_path: Path,
) -> None:
    """Reproduce the exact M3.2A.1 deadlock conditions on Windows and assert zero stalls."""
    token = secrets.token_urlsafe(32)
    proc = _spawn_windows_backend(tmp_path, token)
    assert proc.stdin is not None

    try:
        ready = _wait_for_ready_event(proc, timeout=15.0)
        port = int(ready["port"])
        assert port > 0

        base_url = f"http://127.0.0.1:{port}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Origin": "robbie://desktop",
        }

        async def _exercise_concurrent_requests_during_adapter_init() -> dict[str, Any]:
            async with httpx.AsyncClient(
                base_url=base_url,
                headers=headers,
                timeout=5.0,
            ) as client:
                conv_resp = await client.post("/conversations", json={"title": "WinDeadlockTest"})
                assert conv_resp.status_code == 201
                conv_id = conv_resp.json()["conversation_id"]

                # Trigger _default_adapter_factory -> robbie.agents.adapter -> gui_agents -> numpy.
                # Uses dry_run=True and unreachable loopback base_url so zero external calls
                # or real desktop actions occur.
                task_resp = await client.post(
                    "/tasks",
                    json={
                        "instruction": "Open Notepad.",
                        "conversation_id": conv_id,
                        "config": {
                            "worker": {
                                "provider": "openai",
                                "model": "test-model",
                                "base_url": "http://127.0.0.1:1/v1",
                                "api_key": "sk-local-test-only",
                            },
                            "grounding": {
                                "provider": "openai",
                                "model": "test-model",
                                "base_url": "http://127.0.0.1:1/v1",
                                "api_key": "sk-local-test-only",
                                "grounding_width": 1920,
                                "grounding_height": 1080,
                            },
                            "dry_run": True,
                            "max_steps": 1,
                            "init_timeout_seconds": 30.0,
                            "step_timeout_seconds": 10.0,
                        },
                    },
                )
                assert task_resp.status_code == 202
                task_id = task_resp.json()["task_id"]

                # Keep stdin open & empty while probing all 4 endpoints concurrently across the
                # entire initialization window until adapter init finishes and step >= 1 begins.
                deadline = time.monotonic() + 28.0
                final_task: dict[str, Any] = task_resp.json()
                wave_count = 0

                while time.monotonic() < deadline:
                    wave_start = time.monotonic()
                    health_r, convs_r, conv_r, task_r = await asyncio.gather(
                        client.get("/health"),
                        client.get("/conversations"),
                        client.get(f"/conversations/{conv_id}"),
                        client.get(f"/tasks/{task_id}"),
                    )
                    wave_elapsed = time.monotonic() - wave_start
                    wave_count += 1

                    assert health_r.status_code == 200
                    assert convs_r.status_code == 200
                    assert conv_r.status_code == 200
                    assert task_r.status_code == 200
                    assert wave_elapsed < 4.5, (
                        f"Concurrent wave {wave_count} stalled: {wave_elapsed:.2f}s"
                    )

                    final_task = task_r.json()
                    if final_task["current_step"] >= 1 or final_task["state"] in (
                        "failed",
                        "completed",
                    ):
                        break
                    await asyncio.sleep(0.25)

                assert wave_count >= 1
                return final_task

        final_task = asyncio.run(_exercise_concurrent_requests_during_adapter_init())
        last_error = str(final_task.get("last_error") or "")
        assert "initialization timed out" not in last_error.lower(), (
            f"Adapter initialization timed out unexpectedly: {final_task}"
        )
        assert final_task["current_step"] >= 1, (
            f"Adapter initialization never progressed past step 0: {final_task}"
        )

        # Send shutdown command in two split writes while keeping pipe open until exit.
        proc.stdin.write("shut")
        proc.stdin.flush()
        time.sleep(0.05)
        proc.stdin.write("down\n")
        proc.stdin.flush()

        exit_code = proc.wait(timeout=10.0)
        assert exit_code == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5.0)


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows-specific sidecar stdin EOF graceful shutdown regression test",
)
def test_windows_backend_subprocess_exits_cleanly_on_stdin_close(tmp_path: Path) -> None:
    """Verify closing stdin without sending 'shutdown' terminates the Windows backend cleanly."""
    token = secrets.token_urlsafe(32)
    proc = _spawn_windows_backend(tmp_path, token)
    assert proc.stdin is not None

    try:
        ready = _wait_for_ready_event(proc, timeout=15.0)
        port = int(ready["port"])

        with httpx.Client(timeout=5.0) as client:
            resp = client.get(
                f"http://127.0.0.1:{port}/health",
                headers={"Authorization": f"Bearer {token}", "Origin": "robbie://desktop"},
            )
            assert resp.status_code == 200

        # Close stdin directly (simulating parent crash or EOF-only shutdown).
        proc.stdin.close()
        exit_code = proc.wait(timeout=10.0)
        assert exit_code == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5.0)
