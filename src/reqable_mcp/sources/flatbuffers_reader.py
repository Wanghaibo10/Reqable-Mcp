"""Schema-less FlatBuffers reader.

We deliberately do **not** depend on the ``flatbuffers`` Python package
(which requires generated bindings from a ``.fbs`` file). Instead, we
parse the raw wire format ourselves. This gives us:

  * Zero external schema files. The ObjectBox internal model is parsed
    directly from bytes stored in Reqable's LMDB metadata keys.
  * Forward compatibility. A new field appended to a table by a future
    Reqable version is simply ignored if we never look it up; vtable
    handles the indirection for us.
  * Tiny attack surface. ~80 lines of stdlib ``struct`` reads.

FlatBuffers wire format (only the parts we touch):
  * uoffset_t (u32, little-endian) — relative offset, **always forward**.
    To dereference: ``abs = position_of_uoffset + read_u32(buf, position)``.
  * Table   = at the table's start, an int32 (signed) gives the *relative*
    distance back to the vtable: ``vtable_abs = table_off - i32(buf, table_off)``.
  * Vtable  = ``[u16 vtable_size, u16 table_size, u16 field_offsets...]``.
    Each field offset is the position *inside the table* where that field
    lives (or 0 if the field is absent).
  * String  = ``[u32 length, bytes (UTF-8), 0 terminator]``.
  * Vector  = ``[u32 length, items...]`` (each item per its declared type).

Reference: https://flatbuffers.dev/internals.html
"""

from __future__ import annotations

import struct
from dataclasses import dataclass


def u8(buf: bytes, off: int) -> int:
    return buf[off]


def u16(buf: bytes, off: int) -> int:
    return int(struct.unpack_from("<H", buf, off)[0])


def u32(buf: bytes, off: int) -> int:
    return int(struct.unpack_from("<I", buf, off)[0])


def i32(buf: bytes, off: int) -> int:
    return int(struct.unpack_from("<i", buf, off)[0])


def u64(buf: bytes, off: int) -> int:
    return int(struct.unpack_from("<Q", buf, off)[0])


def i64(buf: bytes, off: int) -> int:
    return int(struct.unpack_from("<q", buf, off)[0])


def deref_uoffset(buf: bytes, off: int) -> int:
    """Read a uoffset at *off* and return the absolute target offset."""
    return off + u32(buf, off)


def root_table_offset(buf: bytes) -> int:
    """First 4 bytes of any FB buffer point to the root table."""
    return u32(buf, 0)


@dataclass(frozen=True)
class Table:
    """A parsed table with its vtable laid open.

    ``fields`` maps the vtable slot index (0 .. n_fields-1) to the
    *absolute byte offset inside ``buf``* where that field's value lives,
    or ``None`` if the field is absent.

    Note: vtable slot index ≠ ObjectBox property id. ObjectBox does its
    own mapping; we surface only the raw slots, callers translate.
    """

    table_off: int
    vtable_off: int
    vtable_size: int
    table_size: int
    fields: dict[int, int]


def parse_table(buf: bytes, table_off: int) -> Table:
    """Parse the vtable for the table at ``table_off``."""
    vt_off_diff = i32(buf, table_off)
    vt = table_off - vt_off_diff
    if vt < 0 or vt + 4 > len(buf):
        raise ValueError(f"vtable at {vt} out of bounds (buf size {len(buf)})")
    vt_size = u16(buf, vt)
    tbl_size = u16(buf, vt + 2)
    if vt_size < 4 or vt + vt_size > len(buf):
        raise ValueError(f"vtable size {vt_size} invalid at {vt}")
    n_fields = (vt_size - 4) // 2
    fields: dict[int, int] = {}
    for fid in range(n_fields):
        off_in_table = u16(buf, vt + 4 + fid * 2)
        if off_in_table != 0:
            fields[fid] = table_off + off_in_table
    return Table(table_off, vt, vt_size, tbl_size, fields)


def read_string(buf: bytes, abs_off: int) -> str:
    """Read a string whose data starts at *abs_off* (length prefix first)."""
    n = u32(buf, abs_off)
    return buf[abs_off + 4 : abs_off + 4 + n].decode("utf-8", errors="replace")


def read_bytes(buf: bytes, abs_off: int) -> bytes:
    """Read a byte vector whose data starts at *abs_off*."""
    n = u32(buf, abs_off)
    return buf[abs_off + 4 : abs_off + 4 + n]


def read_string_field(buf: bytes, abs_off: int) -> str:
    """Read a string field at *abs_off*: a uoffset to the actual string."""
    return read_string(buf, deref_uoffset(buf, abs_off))


def read_bytes_field(buf: bytes, abs_off: int) -> bytes:
    """Read a [byte] field at *abs_off*: a uoffset to the byte vector."""
    return read_bytes(buf, deref_uoffset(buf, abs_off))


def read_vector_of_offsets(buf: bytes, abs_off: int) -> list[int]:
    """Read a vector-of-tables/strings field.

    Returns a list of absolute offsets to each sub-element.
    """
    target = deref_uoffset(buf, abs_off)
    n = u32(buf, target)
    out: list[int] = []
    for i in range(n):
        item_off = target + 4 + i * 4
        out.append(item_off + u32(buf, item_off))
    return out


def read_uint(buf: bytes, abs_off: int, byte_size: int) -> int:
    """Read an inline unsigned integer of 1, 2, 4, or 8 bytes."""
    if byte_size == 1:
        return u8(buf, abs_off)
    if byte_size == 2:
        return u16(buf, abs_off)
    if byte_size == 4:
        return u32(buf, abs_off)
    if byte_size == 8:
        return u64(buf, abs_off)
    raise ValueError(f"unsupported uint byte_size={byte_size}")


def looks_like_table(buf: bytes, off: int) -> bool:
    """Heuristic: does ``off`` look like the start of a valid table?

    Used to discover sub-tables inside a vector when probing.
    """
    if off < 0 or off + 4 > len(buf):
        return False
    try:
        diff = i32(buf, off)
        vt = off - diff
        if vt < 0 or vt + 4 > len(buf):
            return False
        vt_size = u16(buf, vt)
        if vt_size < 4 or vt_size > 1024:  # sanity
            return False
        return vt + vt_size <= len(buf)
    except struct.error:
        return False
