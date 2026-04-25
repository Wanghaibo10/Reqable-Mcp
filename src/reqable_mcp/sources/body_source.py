"""Read raw request/response bodies from Reqable's ``capture/`` directory.

Discovered on Reqable 3.0.40 (2026-04). Files are named::

    {conn.timestamp}-{conn.id}-{session.id}-req_raw-body.reqable     (request)
    {conn.timestamp}-{conn.id}-{session.id}-res-raw-body.reqable     (response, on-wire bytes)
    {conn.timestamp}-{conn.id}-{session.id}-res-extract-body.reqable (response, decoded plaintext)

These three IDs are surfaced in each ``CaptureRecordHistoryEntity``'s
``dbData`` JSON at:

* ``data.session.connection.timestamp`` (microseconds, ObjectBox-internal)
* ``data.session.connection.id``
* ``data.session.id``

Important quirk: the request body uses ``req_raw`` (underscore) while
the response uses ``res-raw`` / ``res-extract`` (hyphen). Reqable
inconsistency; we paper over it.

If a file is missing, the body either:
  * was never recorded (e.g. zero-byte body, GET request),
  * was a streaming/large response Reqable chose to skip, or
  * was already cleaned up by Reqable.

We always return ``None`` rather than raising in those cases.
"""

from __future__ import annotations

import gzip
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

log = logging.getLogger(__name__)

BodyKind = Literal["req", "res"]


@dataclass(frozen=True)
class BodyLookup:
    """The three IDs that, taken together, identify a body file.

    Caller extracts these from a decoded LMDB record (``data.session
    .connection.timestamp / .id / data.session.id``).
    """

    conn_timestamp: int
    conn_id: int
    session_id: int

    def filename(self, kind: BodyKind, *, prefer_decoded: bool = True) -> str:
        # ``req_raw`` underscore vs ``res-raw`` hyphen — Reqable's choice.
        if kind == "req":
            suffix = "req_raw-body.reqable"
        elif prefer_decoded:
            suffix = "res-extract-body.reqable"
        else:
            suffix = "res-raw-body.reqable"
        return f"{self.conn_timestamp}-{self.conn_id}-{self.session_id}-{suffix}"


class BodySource:
    """Reader for request / response body files in ``capture/``.

    Read-only; absolutely never writes (``capture/`` is Reqable's data).

    Files are usually small; we just read them whole. For >50MB bodies
    callers should stream — but that's not yet a requirement.
    """

    def __init__(self, capture_dir: Path):
        self.capture_dir = Path(capture_dir)

    def get_request_body(self, lookup: BodyLookup) -> bytes | None:
        return self._read(lookup.filename("req"))

    def get_response_body(
        self,
        lookup: BodyLookup,
        *,
        prefer_decoded: bool = True,
    ) -> bytes | None:
        """Return response body bytes.

        ``prefer_decoded=True`` (default) tries the ``-res-extract``
        plaintext file first (gzip already decoded by Reqable); falls
        back to ``-res-raw`` (on-wire bytes).
        """
        if prefer_decoded:
            # Prefer extract (decoded plaintext)
            data = self._read(lookup.filename("res", prefer_decoded=True))
            if data is not None:
                return data
        # Fallback to raw (may be gzipped)
        raw = self._read(lookup.filename("res", prefer_decoded=False))
        if raw is None:
            return None
        # If it looks gzipped, transparently decompress
        if raw[:2] == b"\x1f\x8b":
            try:
                return gzip.decompress(raw)
            except OSError as e:
                log.debug("res-raw-body looked gzip but decompress failed: %s", e)
                return raw
        return raw

    def get_response_raw(self, lookup: BodyLookup) -> bytes | None:
        """Return on-wire response bytes (no decompression). Useful for
        protocol-level analysis."""
        return self._read(lookup.filename("res", prefer_decoded=False))

    def _read(self, filename: str) -> bytes | None:
        path = self.capture_dir / filename
        try:
            with path.open("rb") as f:
                return f.read()
        except FileNotFoundError:
            return None
        except OSError as e:
            log.warning("body read failed for %s: %s", filename, e)
            return None


def lookup_from_record(record: dict) -> BodyLookup | None:
    """Build a :class:`BodyLookup` from a decoded ``dbData`` JSON.

    Returns ``None`` if any of the three required fields is missing
    (which happens for incomplete / errored captures).
    """
    sess = record.get("session") or {}
    conn = sess.get("connection") or {}
    ct = conn.get("timestamp")
    ci = conn.get("id")
    sid = sess.get("id")
    if ct is None or ci is None or sid is None:
        return None
    try:
        return BodyLookup(int(ct), int(ci), int(sid))
    except (TypeError, ValueError):
        return None


__all__ = ["BodyLookup", "BodySource", "lookup_from_record"]
