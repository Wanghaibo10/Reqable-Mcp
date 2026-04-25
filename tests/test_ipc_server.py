"""Tests for the daemon-side IPC server.

Uses ``/tmp`` for socket paths because macOS caps AF_UNIX paths at
104 bytes; pytest's default ``tmp_path`` (under ``/private/var/...``)
overflows that.
"""

from __future__ import annotations

import json
import shutil
import socket
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from reqable_mcp.ipc.protocol import (
    PROTOCOL_VERSION,
    Request,
    encode_message,
    ok_response,
)
from reqable_mcp.ipc.server import IpcServer


@pytest.fixture
def short_tmp() -> Iterator[Path]:
    """A short-path tmp dir suitable for AF_UNIX paths."""
    p = Path("/tmp") / f"rmcp-test-{uuid.uuid4().hex[:8]}"
    p.mkdir(mode=0o700, exist_ok=False)
    try:
        yield p
    finally:
        shutil.rmtree(p, ignore_errors=True)


def _client_round_trip(socket_path: Path, request: dict, timeout: float = 2.0) -> dict:
    """Open a client socket, send ``request``, read one response frame."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(str(socket_path))
    s.sendall(encode_message(request))
    buf = b""
    while b"\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk
    s.close()
    return json.loads(buf.split(b"\n", 1)[0])


@pytest.fixture
def echo_server(short_tmp: Path) -> Iterator[IpcServer]:
    """A server that echoes the args back as response data."""

    def handler(req: Request) -> bytes:
        return ok_response({"echo_op": req.op, "args": req.args})

    server = IpcServer(short_tmp / "test.sock", handler)
    server.start()
    yield server
    server.stop()


# ---------------------------------------------------------------- happy path


class TestRoundTrip:
    def test_basic_echo(self, echo_server: IpcServer) -> None:
        resp = _client_round_trip(
            echo_server.socket_path,
            {"v": PROTOCOL_VERSION, "op": "ping", "args": {"x": 1}},
        )
        assert resp == {"ok": True, "data": {"echo_op": "ping", "args": {"x": 1}}}

    def test_socket_perms_0600(self, echo_server: IpcServer) -> None:
        assert oct(echo_server.socket_path.stat().st_mode)[-3:] == "600"

    def test_stats_increments_connections(self, echo_server: IpcServer) -> None:
        before = echo_server.stats()["connections_total"]
        _client_round_trip(
            echo_server.socket_path,
            {"v": PROTOCOL_VERSION, "op": "ping", "args": {}},
        )
        time.sleep(0.05)  # let counter settle
        assert echo_server.stats()["connections_total"] == before + 1


# ---------------------------------------------------------------- protocol errors


class TestProtocolErrors:
    def test_invalid_json_returns_error(self, echo_server: IpcServer) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect(str(echo_server.socket_path))
        s.sendall(b"not json at all\n")
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        s.close()
        resp = json.loads(buf.split(b"\n", 1)[0])
        assert resp["ok"] is False
        assert "protocol error" in resp["error"]

    def test_wrong_version(self, echo_server: IpcServer) -> None:
        resp = _client_round_trip(
            echo_server.socket_path, {"v": 99, "op": "x", "args": {}}
        )
        assert resp["ok"] is False
        assert "protocol error" in resp["error"]

    def test_invalid_total_counter(self, echo_server: IpcServer) -> None:
        before = echo_server.stats()["invalid_total"]
        _client_round_trip(
            echo_server.socket_path, {"v": 99, "op": "x", "args": {}}
        )
        time.sleep(0.1)
        assert echo_server.stats()["invalid_total"] == before + 1

    def test_oversized_frame_returns_error_not_silent_close(
        self, echo_server: IpcServer
    ) -> None:
        # A frame just over the 256KB cap should produce a structured
        # error response rather than a silent EOF — the previous
        # behavior left the peer guessing why their request vanished.
        from reqable_mcp.ipc.protocol import MAX_MESSAGE_BYTES

        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect(str(echo_server.socket_path))
        # Big single chunk, no newline, intentionally bigger than the cap.
        s.sendall(b"x" * (MAX_MESSAGE_BYTES + 100) + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        s.close()
        resp = json.loads(buf.split(b"\n", 1)[0])
        assert resp["ok"] is False
        assert "exceeds" in resp["error"] or "protocol error" in resp["error"]


# ---------------------------------------------------------------- handler errors


class TestHandlerCrash:
    def test_handler_exception_returns_error(self, short_tmp: Path) -> None:
        def crashy(req: Request) -> bytes:
            raise RuntimeError("boom")

        server = IpcServer(short_tmp / "crash.sock", crashy)
        server.start()
        try:
            resp = _client_round_trip(
                server.socket_path,
                {"v": PROTOCOL_VERSION, "op": "x", "args": {}},
            )
            assert resp["ok"] is False
            assert "handler raised" in resp["error"]
            time.sleep(0.05)
            assert server.stats()["error_total"] >= 1
        finally:
            server.stop()


# ---------------------------------------------------------------- lifecycle


class TestLifecycle:
    def test_socket_unlinked_on_stop(self, short_tmp: Path) -> None:
        server = IpcServer(
            short_tmp / "x.sock", lambda req: ok_response({})
        )
        server.start()
        assert server.socket_path.exists()
        server.stop()
        assert not server.socket_path.exists()

    def test_start_idempotent(self, short_tmp: Path) -> None:
        server = IpcServer(
            short_tmp / "y.sock", lambda req: ok_response({})
        )
        server.start()
        try:
            server.start()  # second call should be a no-op
            # Should still respond.
            _client_round_trip(
                server.socket_path,
                {"v": PROTOCOL_VERSION, "op": "x", "args": {}},
            )
        finally:
            server.stop()

    def test_stale_socket_replaced(self, short_tmp: Path) -> None:
        # Pre-create a leftover socket file from a "crashed" prior run.
        sock_path = short_tmp / "stale.sock"
        old = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old.bind(str(sock_path))
        old.close()
        # New server should bind cleanly.
        server = IpcServer(sock_path, lambda req: ok_response({}))
        server.start()
        try:
            resp = _client_round_trip(
                sock_path, {"v": PROTOCOL_VERSION, "op": "x", "args": {}}
            )
            assert resp["ok"] is True
        finally:
            server.stop()


# ---------------------------------------------------------------- concurrency


class TestConcurrency:
    def test_concurrent_clients(self, short_tmp: Path) -> None:
        # Handler that sleeps a bit so concurrency actually matters.
        def slow(req: Request) -> bytes:
            time.sleep(0.05)
            return ok_response({"n": req.args.get("n")})

        server = IpcServer(short_tmp / "conc.sock", slow)
        server.start()
        try:
            results: list[int] = []
            lock = threading.Lock()

            def worker(n: int) -> None:
                resp = _client_round_trip(
                    server.socket_path,
                    {"v": PROTOCOL_VERSION, "op": "x", "args": {"n": n}},
                )
                with lock:
                    results.append(resp["data"]["n"])

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
            t0 = time.time()
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            elapsed = time.time() - t0
            assert sorted(results) == list(range(8))
            # Sequential would be 8 * 0.05 = 0.4s. Concurrent should be much less.
            assert elapsed < 0.3, f"too slow: {elapsed:.3f}s"
        finally:
            server.stop()
