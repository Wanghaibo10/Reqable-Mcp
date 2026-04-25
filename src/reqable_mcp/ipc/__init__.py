"""Unix-socket IPC between the daemon and Reqable's per-request addons.py.

Reqable forks one Python process per HTTP request to run the user
script (~200ms cold-start). Heavy state — rule definitions, hit logs —
lives in the long-running daemon, and addons.py is a thin shell that
queries us over a Unix socket each time.

Submodules:
  * ``protocol`` — wire format helpers (no network).
  * ``server``   — daemon-side listener (`Daemon.start` wires it up).
"""

from .protocol import (
    PROTOCOL_VERSION,
    InvalidMessage,
    decode_message,
    encode_message,
    error_response,
    ok_response,
)

__all__ = [
    "PROTOCOL_VERSION",
    "InvalidMessage",
    "decode_message",
    "encode_message",
    "error_response",
    "ok_response",
]
