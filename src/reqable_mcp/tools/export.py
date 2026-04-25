"""Phase 3 — body decoding, pretty-printing, and dumping to local files.

Three closely-related tools that share the body-fetch + decode plumbing:

* ``decode_body`` — fetch raw bytes, walk the ``Content-Encoding``
  chain (gzip / deflate / br / zstd), return decoded text.
* ``prettify``   — decode, then indent JSON / XML / HTML.
* ``dump_body``  — write the decoded body to a local file (with a
  guard against writing into Reqable's own data directory).

Brotli and zstd codecs require optional dependencies — install
``reqable-mcp[export]`` to enable them. Without those packages the
respective codecs return a clean error rather than crashing.
"""

from __future__ import annotations

import base64
import gzip
import html
import json
import logging
import re  # used by _pretty_html
import zlib
from pathlib import Path
from typing import Any, Literal

from ..mcp_server import get_daemon, mcp
from ..sources.body_source import lookup_from_record

log = logging.getLogger(__name__)

BodySide = Literal["request", "response"]
DEFAULT_DUMP_LIMIT_BYTES: int = 16 * 1024 * 1024  # 16 MiB cap for safety

# Directories we refuse to write into. Reqable's own data must never be
# touched by us — even an accidental dump file in there could confuse
# Reqable's cleanup or be mistaken for a capture artifact.
_REFUSED_WRITE_PREFIXES: tuple[Path, ...] = (
    Path.home() / "Library" / "Application Support" / "com.reqable.macosx",
)


# ---------------------------------------------------------------- decoding


def _try_import_brotli():  # pragma: no cover - import path
    try:
        import brotli

        return brotli
    except ImportError:
        return None


def _try_import_zstd():  # pragma: no cover - import path
    try:
        import zstandard

        return zstandard
    except ImportError:
        return None


def _decode_one(data: bytes, codec: str) -> tuple[bytes | None, str | None]:
    """Apply one Content-Encoding step. Returns ``(bytes, error)``."""
    codec = codec.strip().lower()
    if codec in ("identity", ""):
        return data, None
    if codec == "gzip" or codec == "x-gzip":
        try:
            return gzip.decompress(data), None
        except OSError as e:
            return None, f"gzip decompress failed: {e}"
    if codec == "deflate":
        # Per RFC 7230, "deflate" can be either zlib-wrapped or raw —
        # try both.
        try:
            return zlib.decompress(data), None
        except zlib.error:
            try:
                return zlib.decompress(data, -zlib.MAX_WBITS), None
            except zlib.error as e:
                return None, f"deflate decompress failed: {e}"
    if codec == "br":
        brotli = _try_import_brotli()
        if brotli is None:
            return None, (
                "br codec missing — install with `pip install brotli` "
                "or `pip install 'reqable-mcp[export]'`"
            )
        try:
            return brotli.decompress(data), None
        except Exception as e:  # noqa: BLE001 - 3rd-party exc shapes vary
            return None, f"br decompress failed: {e}"
    if codec == "zstd":
        zstd = _try_import_zstd()
        if zstd is None:
            return None, (
                "zstd codec missing — install with `pip install zstandard` "
                "or `pip install 'reqable-mcp[export]'`"
            )
        try:
            return zstd.ZstdDecompressor().decompress(data), None
        except Exception as e:  # noqa: BLE001
            return None, f"zstd decompress failed: {e}"
    return None, f"unsupported codec: {codec!r}"


def _walk_content_encoding(
    raw: bytes, content_encoding: str | None
) -> tuple[bytes, list[str], str | None]:
    """Apply each codec in ``Content-Encoding`` from right to left.

    Returns ``(decoded_bytes, applied_chain, error_or_None)``. The
    chain is the list of codecs we *successfully* applied; if a codec
    fails we stop and return the partial result + an error.
    """
    if not content_encoding:
        return raw, [], None
    # Multiple codings comma-separated, applied last → first.
    codecs = [c.strip() for c in content_encoding.split(",") if c.strip()]
    applied: list[str] = []
    out = raw
    for codec in reversed(codecs):
        decoded, err = _decode_one(out, codec)
        if err is not None:
            return out, applied, err
        if decoded is not None:
            out = decoded
            applied.append(codec)
    return out, applied, None


def _content_encoding_from(headers: list[str]) -> str | None:
    """Find ``Content-Encoding`` (case-insensitive) in a Reqable
    header list. Returns ``None`` if absent."""
    for h in headers:
        if not h or h.startswith(":"):
            continue
        name, sep, value = h.partition(":")
        if not sep:
            continue
        if name.strip().lower() == "content-encoding":
            return value.strip()
    return None


def _content_type_from(headers: list[str]) -> str | None:
    for h in headers:
        if not h or h.startswith(":"):
            continue
        name, sep, value = h.partition(":")
        if not sep:
            continue
        if name.strip().lower() == "content-type":
            return value.strip().split(";")[0].strip().lower()
    return None


def _decode_text(payload: bytes) -> tuple[str, str]:
    """UTF-8 with base64 fallback. Mirrors query._decode_body_text."""
    if not payload:
        return "", "empty"
    try:
        return payload.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return base64.b64encode(payload).decode("ascii"), "base64"


def _fetch_raw_body(
    uid: str, side: BodySide
) -> tuple[bytes | None, str | None, str | None, dict | None]:
    """Returns ``(raw_bytes, content_encoding, content_type, full_record)``
    or ``(None, None, None, None)`` for unknown / unfetchable captures.
    """
    daemon = get_daemon()
    if daemon.db is None:
        return None, None, None, None
    row = daemon.db.get_capture(uid)
    if row is None or not row.get("ob_id"):
        return None, None, None, None
    if daemon.lmdb_source is None or daemon.body_source is None:
        return None, None, None, None
    full = daemon.lmdb_source.fetch_record(int(row["ob_id"]))
    if full is None:
        return None, None, None, None

    sess = full.get("session") or {}
    headers_block = (sess.get("request") if side == "request" else sess.get("response")) or {}
    headers = headers_block.get("headers") or []
    ce = _content_encoding_from(headers)
    ct = _content_type_from(headers)

    lookup = lookup_from_record(full)
    if lookup is None:
        return None, ce, ct, full

    if side == "request":
        raw = daemon.body_source.get_request_body(lookup)
    else:
        # We want the on-wire bytes for decode_body so the user can
        # inspect the Content-Encoding chain themselves. The
        # ``-extract`` plaintext file is for ``get_request``'s
        # convenience.
        raw = daemon.body_source.get_response_raw(lookup)
        if raw is None:
            # Fallback to whatever we can find (might be already-decoded).
            raw = daemon.body_source.get_response_body(lookup)
    return raw, ce, ct, full


# ---------------------------------------------------------------- decode_body


@mcp.tool()
def decode_body(
    uid: str, side: BodySide = "response"
) -> dict[str, Any]:
    """Fetch the body and decode its ``Content-Encoding`` chain.

    For responses we read the on-wire bytes (``-res-raw-body.reqable``)
    and walk every codec listed in ``Content-Encoding`` from right to
    left (RFC 9110 §8.4). If a codec is missing (``brotli`` /
    ``zstandard`` not installed) we surface a clean error; partial
    decodes still return what we managed to undo.

    Returns ``{decoded, decoded_encoding ('utf-8'|'base64'|'empty'),
    original_size, decoded_size, encoding_chain, content_type}`` or
    ``{error}``.
    """
    raw, ce, ct, _ = _fetch_raw_body(uid, side)
    if raw is None:
        return {"error": f"body unavailable for uid={uid!r}, side={side}"}
    decoded, applied, err = _walk_content_encoding(raw, ce)
    text, enc = _decode_text(decoded)
    out: dict[str, Any] = {
        "decoded": text,
        "decoded_encoding": enc,
        "original_size": len(raw),
        "decoded_size": len(decoded),
        "encoding_chain": applied,
        "content_type": ct,
    }
    if err is not None:
        out["error"] = err
    return out


# ---------------------------------------------------------------- prettify


_FormatHint = Literal["json", "xml", "html", "auto"]

def _detect_format(content_type: str | None, sample: str) -> str:
    """Pick a formatter for ``prettify``.

    Strategy: trust ``Content-Type`` if it names json/xml/html; otherwise
    sniff the leading bytes. HTML beats XML when ``<!doctype html`` or
    ``<html`` is in the first 256 chars (``<!doctype xxx`` for non-HTML
    document types still falls through to xml).
    """
    if content_type:
        if "json" in content_type:
            return "json"
        if "xml" in content_type:
            return "xml"
        if "html" in content_type:
            return "html"
    s = sample.lstrip()
    if not s:
        return "text"
    if s[0] in "{[":
        return "json"
    head = s[:256].lower()
    if "<!doctype html" in head or "<html" in head:
        return "html"
    if s.startswith("<?xml") or (
        s.startswith("<") and len(s) > 1 and (s[1].isalpha() or s[1] == "!")
    ):
        return "xml"
    return "text"


def _pretty_json(text: str) -> tuple[str, str | None]:
    try:
        return json.dumps(json.loads(text), indent=2, ensure_ascii=False), None
    except (ValueError, TypeError) as e:
        return text, f"json parse failed: {e}"


def _pretty_xml(text: str) -> tuple[str, str | None]:
    try:
        from xml.dom.minidom import (
            parseString,  # noqa: S408 — input is captured traffic, not user-supplied.
        )

        return parseString(text).toprettyxml(indent="  "), None  # noqa: S318
    except Exception as e:  # noqa: BLE001 - parser exc shapes vary
        return text, f"xml parse failed: {e}"


def _pretty_html(text: str) -> tuple[str, str | None]:
    """Lightweight HTML pretty-print using stdlib only.

    We don't pull in BeautifulSoup; this is a pragmatic indenter that
    inserts newlines around block-level tags. Good enough for skim
    reading; not a full DOM round-trip.
    """
    # Decode HTML entities once so the user reads &quot; as ".
    decoded = html.unescape(text)
    # Insert newlines around tag boundaries; collapse repeats.
    out = re.sub(r">\s*<", ">\n<", decoded)
    return out.strip(), None


@mcp.tool()
def prettify(
    uid: str,
    side: BodySide = "response",
    format: _FormatHint = "auto",
) -> dict[str, Any]:
    """Decode + pretty-print a body.

    ``format="auto"`` picks JSON / XML / HTML based on Content-Type
    (and a content sniff fallback). Pass ``format="json"`` etc. to
    force a specific formatter.

    Returns ``{pretty, format, content_type, encoding_chain, error?}``.
    """
    raw, ce, ct, _ = _fetch_raw_body(uid, side)
    if raw is None:
        return {"error": f"body unavailable for uid={uid!r}, side={side}"}
    decoded, applied, decode_err = _walk_content_encoding(raw, ce)
    text, text_enc = _decode_text(decoded)
    if text_enc == "base64":
        return {
            "error": "body is binary; prettify cannot format base64",
            "content_type": ct,
            "encoding_chain": applied,
        }

    chosen = format if format != "auto" else _detect_format(ct, text)
    if chosen == "json":
        pretty, err = _pretty_json(text)
    elif chosen == "xml":
        pretty, err = _pretty_xml(text)
    elif chosen == "html":
        pretty, err = _pretty_html(text)
    else:
        pretty, err = text, None

    out: dict[str, Any] = {
        "pretty": pretty,
        "format": chosen,
        "content_type": ct,
        "encoding_chain": applied,
    }
    if decode_err is not None:
        out["decode_error"] = decode_err
    if err is not None:
        out["format_error"] = err
    return out


# ---------------------------------------------------------------- dump_body


def _validate_dump_path(path: str) -> tuple[Path | None, str | None]:
    """Reject relative paths and writes into Reqable's data directory."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        return None, f"path must be absolute, got {path!r}"
    try:
        resolved = p.resolve(strict=False)
    except OSError as e:
        return None, f"cannot resolve path: {e}"
    for refused in _REFUSED_WRITE_PREFIXES:
        try:
            resolved.relative_to(refused.resolve(strict=False))
        except ValueError:
            continue
        return None, (
            f"refusing to write under {refused} — that's Reqable's own "
            "data directory; pick another location"
        )
    return resolved, None


@mcp.tool()
def dump_body(
    uid: str,
    side: BodySide,
    path: str,
    decoded: bool = True,
) -> dict[str, Any]:
    """Write a captured body to a local file.

    ``decoded=True`` (default) writes after walking ``Content-Encoding``
    so the file on disk is the plaintext payload. ``decoded=False``
    writes the on-wire bytes verbatim (still gzip / br / zstd / etc.).

    ``path`` must be absolute. Writes under Reqable's own data
    directory are refused — that's not our space to touch.

    Returns ``{path, size, encoding_chain, content_type}`` or ``{error}``.
    """
    target, err = _validate_dump_path(path)
    if err is not None:
        return {"error": err}
    raw, ce, ct, _ = _fetch_raw_body(uid, side)
    if raw is None:
        return {"error": f"body unavailable for uid={uid!r}, side={side}"}

    if decoded and ce:
        body, applied, decode_err = _walk_content_encoding(raw, ce)
    else:
        body, applied, decode_err = raw, [], None

    if len(body) > DEFAULT_DUMP_LIMIT_BYTES:
        return {
            "error": (
                f"body size {len(body)} exceeds dump limit "
                f"{DEFAULT_DUMP_LIMIT_BYTES}"
            )
        }

    assert target is not None  # _validate_dump_path returned no error
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("wb") as f:
            f.write(body)
    except OSError as e:
        return {"error": f"write failed: {e}"}

    out: dict[str, Any] = {
        "path": str(target),
        "size": len(body),
        "encoding_chain": applied,
        "content_type": ct,
    }
    if decode_err is not None:
        out["decode_error"] = decode_err
    return out


__all__: list[str] = []  # tools register via @mcp.tool
