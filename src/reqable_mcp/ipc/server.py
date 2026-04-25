"""Unix-socket IPC server.

The server speaks the line-delimited JSON protocol from
:mod:`reqable_mcp.ipc.protocol`. One connection = one round trip:
addons.py opens, sends one request, reads one response, closes.

Threading model:

* one *acceptor* thread runs ``socket.accept()`` in a loop;
* every accepted connection spawns a short-lived *handler* thread.

Handler threads have a small (5s) read timeout so a misbehaving
addons process can't pile up against the daemon. The acceptor
respects ``stop()`` via a self-pipe-style wakeup using socket timeout.

Socket location is owned by the caller (typically
``~/.reqable-mcp/daemon.sock``). The server creates the file with
0600 perms and unlinks it on stop.

Why short-lived connections, not a persistent one? Reqable spawns a
fresh Python interpreter for *every* request. A per-call socket is
the simplest, debuggable answer; pooling is unnecessary at this scale.
"""

from __future__ import annotations

import contextlib
import logging
import os
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .protocol import (
    MAX_MESSAGE_BYTES,
    InvalidMessage,
    Request,
    decode_message,
    error_response,
)

log = logging.getLogger(__name__)

# Socket-level read timeout for one client. 5s is generous — a healthy
# round trip is sub-millisecond. Beyond this it's a bug or a stuck
# addons process and we'd rather drop the connection.
READ_TIMEOUT_S: float = 5.0
# How often the acceptor wakes up to check ``_stop``. The socket is in
# blocking mode otherwise; we use a timeout so stop() is responsive.
ACCEPT_POLL_S: float = 0.5

Handler = Callable[[Request], bytes]
"""Function the server calls per request.

Returns the raw response bytes (the framing helpers
``ok_response`` / ``error_response`` produce these).
"""


class IpcServer:
    """A simple per-connection thread-pool over a Unix socket.

    Use :meth:`start` once after wiring up the handler; the thread
    runs as a daemon so a crashed parent doesn't leave it stranded.
    Always call :meth:`stop` on graceful shutdown to unlink the
    socket file.
    """

    def __init__(self, socket_path: Path, handler: Handler):
        self.socket_path = Path(socket_path)
        self.handler = handler
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._acceptor: threading.Thread | None = None
        # Tracks live handler threads; stop() joins them.
        self._handlers: list[threading.Thread] = []
        self._handlers_lock = threading.Lock()
        # Diagnostic counters surfaced via ``stats``.
        self._connections_total = 0
        self._invalid_total = 0
        self._error_total = 0

    # ------------------------------------------------------------------ public

    def start(self) -> None:
        """Bind, listen, and start the acceptor thread."""
        if self._acceptor is not None and self._acceptor.is_alive():
            return
        self._stop.clear()
        # Best-effort ensure parent dir exists with strict perms.
        self.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Stale socket from a prior crash? Remove it so bind succeeds.
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.socket_path)

        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(self.socket_path))
        # 0600 — only the running user can talk to us. addons.py runs
        # under the same user (Reqable spawns it locally).
        os.chmod(self.socket_path, 0o600)
        s.listen(32)
        s.settimeout(ACCEPT_POLL_S)
        self._sock = s

        self._acceptor = threading.Thread(
            target=self._accept_loop, name="reqable-mcp-ipc-acceptor", daemon=True
        )
        self._acceptor.start()
        log.info("ipc server listening on %s", self.socket_path)

    def stop(self, *, timeout: float = 2.0) -> None:
        """Stop accepting, join handlers (best effort), unlink socket."""
        self._stop.set()
        if self._acceptor is not None:
            self._acceptor.join(timeout=timeout)
            self._acceptor = None
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
            self._sock = None
        # Don't block forever on stuck handlers; they have READ_TIMEOUT_S.
        deadline = time.time() + timeout
        with self._handlers_lock:
            handlers = list(self._handlers)
        for t in handlers:
            remaining = max(0.0, deadline - time.time())
            t.join(timeout=remaining)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.socket_path)
        log.info("ipc server stopped")

    def stats(self) -> dict[str, int]:
        return {
            "connections_total": self._connections_total,
            "invalid_total": self._invalid_total,
            "error_total": self._error_total,
            "live_handlers": sum(1 for t in self._handlers if t.is_alive()),
        }

    # ------------------------------------------------------------------ internals

    def _accept_loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                self._reap_dead_handlers()
                continue
            except OSError as e:
                # Socket was closed during stop(); leave the loop.
                if self._stop.is_set():
                    return
                log.warning("accept failed: %s", e)
                continue
            self._connections_total += 1
            t = threading.Thread(
                target=self._handle_one,
                args=(conn,),
                name="reqable-mcp-ipc-handler",
                daemon=True,
            )
            with self._handlers_lock:
                self._handlers.append(t)
            t.start()

    def _reap_dead_handlers(self) -> None:
        """Trim the handler-tracking list. O(n); n stays small."""
        with self._handlers_lock:
            self._handlers[:] = [t for t in self._handlers if t.is_alive()]

    def _handle_one(self, conn: socket.socket) -> None:
        """One connection's full lifecycle: read frame → handler → write."""
        try:
            conn.settimeout(READ_TIMEOUT_S)
            line = self._read_line(conn)
            if line is None:
                return
            try:
                req = decode_message(line)
            except InvalidMessage as e:
                self._invalid_total += 1
                with contextlib.suppress(OSError):
                    conn.sendall(error_response(f"protocol error: {e}"))
                return

            try:
                resp = self.handler(req)
            except Exception as e:  # noqa: BLE001 — handlers must not crash the server
                self._error_total += 1
                log.exception("ipc handler raised on op=%s", req.op)
                with contextlib.suppress(OSError):
                    conn.sendall(error_response(f"handler raised: {type(e).__name__}"))
                return

            with contextlib.suppress(OSError):
                conn.sendall(resp)
        finally:
            with contextlib.suppress(OSError):
                conn.close()

    @staticmethod
    def _read_line(conn: socket.socket) -> bytes | None:
        """Read one ``\\n``-terminated frame. None on EOF / oversized."""
        buf = bytearray()
        while True:
            try:
                chunk = conn.recv(4096)
            except (TimeoutError, OSError):
                return None
            if not chunk:
                return bytes(buf) if buf else None
            buf.extend(chunk)
            if b"\n" in chunk:
                return bytes(buf)
            if len(buf) > MAX_MESSAGE_BYTES:
                return None  # decode_message will reject if we returned it


__all__ = ["IpcServer", "Handler", "READ_TIMEOUT_S"]
