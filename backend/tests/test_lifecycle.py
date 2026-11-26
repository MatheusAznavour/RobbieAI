"""Subprocess lifecycle, dynamic port allocation, and real-network tests."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import subprocess
import sys

import httpx
import websockets

from robbie.main import bind_loopback_socket


def test_dynamic_loopback_port_allocation_is_unique() -> None:
    sock1 = bind_loopback_socket("127.0.0.1", 0)
    try:
        sock2 = bind_loopback_socket("127.0.0.1", 0)
        try:
            port1 = sock1.getsockname()[1]
            port2 = sock2.getsockname()[1]
            assert port1 > 0
            assert port2 > 0
            assert port1 != port2
        finally:
            sock2.close()
    finally:
        sock1.close()


def test_backend_subprocess_startup_auth_ws_and_graceful_shutdown() -> None:
    token = secrets.token_urlsafe(32)
    env = os.environ.copy()
    env.update(
        {
            "ROBBIE_BACKEND_HOST": "127.0.0.1",
            "ROBBIE_BACKEND_PORT": "0",
            "ROBBIE_API_TOKEN": token,
            "ROBBIE_ALLOWED_ORIGINS": "robbie://desktop",
            "ROBBIE_WATCH_STDIN": "1",
            "ROBBIE_LOG_LEVEL": "INFO",
        }
    )

    proc = subprocess.Popen(
        [sys.executable, "-m", "robbie"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    assert proc.stderr is not None

    try:
        ready_line = proc.stdout.readline().strip()
        assert ready_line, f"Backend exited early with code {proc.poll()}: {proc.stderr.read()}"
        assert token not in ready_line

        ready_payload = json.loads(ready_line)
        assert ready_payload["event"] == "robbie_backend_ready"
        assert ready_payload["host"] == "127.0.0.1"
        port = ready_payload["port"]
        assert isinstance(port, int) and port > 0

        base_url = f"http://127.0.0.1:{port}"
        with httpx.Client(timeout=5.0) as client:
            unauth = client.get(f"{base_url}/health")
            assert unauth.status_code == 401

            auth_resp = client.get(
                f"{base_url}/health",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert auth_resp.status_code == 200
            health = auth_resp.json()
            assert health["status"] == "ok"
            assert health["host"] == "127.0.0.1"
            assert health["port"] == port

        async def _verify_ws() -> None:
            ws_url = f"ws://127.0.0.1:{port}/ws/status"
            async with websockets.connect(
                ws_url,
                additional_headers={
                    "Authorization": f"Bearer {token}",
                    "Origin": "robbie://desktop",
                },
                open_timeout=5.0,
            ) as ws:
                first_msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                assert first_msg["type"] == "status"
                assert first_msg["status"] == "ready"
                assert first_msg["port"] == port

                await ws.send(json.dumps({"type": "ping", "request_id": "subproc-1"}))
                pong = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                assert pong["type"] == "pong"
                assert pong["request_id"] == "subproc-1"

        asyncio.run(_verify_ws())

        # Trigger graceful shutdown by closing stdin pipe
        proc.stdin.close()
        exit_code = proc.wait(timeout=10)
        assert exit_code == 0

        remaining_stdout = proc.stdout.read()
        stderr_output = proc.stderr.read()
        assert token not in remaining_stdout
        assert token not in stderr_output
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
