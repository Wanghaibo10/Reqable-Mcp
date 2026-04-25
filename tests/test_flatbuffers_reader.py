"""Tests for the schema-less FlatBuffers reader.

Strategy: hand-build minimal FB-shaped byte blobs (no flatbuffers
dependency) and verify our reader recovers the values correctly. The
shapes mirror what ObjectBox actually emits.
"""

from __future__ import annotations

import struct

import pytest

from reqable_mcp.sources import flatbuffers_reader as fbr

# ---------------------------------------------------------------- helpers


def _pack_string(s: str) -> bytes:
    data = s.encode("utf-8")
    return struct.pack("<I", len(data)) + data + b"\x00"


def _build_simple_table(
    *, name: str, count: int, root_extra_padding: int = 0
) -> bytes:
    """Build a minimal FB blob with a root table that has:
       slot 0: string ``name``
       slot 1: u32 ``count``

    Layout we emit (offsets all relative to start of buffer):

        [0-3]    uoffset to root table
        [4-...]  vtable
        [.....]  table
        [.....]  string data

    This is intentionally laid out with a trailing buffer style: data
    fields placed *after* the root table.
    """
    # We'll build it bottom-up:
    #   1) decide string position
    #   2) decide table position
    #   3) decide vtable position
    #   4) emit header uoffset
    buf = bytearray()

    # Reserve 4 bytes for the root uoffset header
    buf += b"\x00\x00\x00\x00"

    # Vtable: vt_size (4 + 2*2 = 8), tbl_size (12 = uoffset(4) + u32(4) + i32(4) backptr)
    # field 0 = at byte_offset 8 in table  (after the i32 vtable backptr at 0..3, count at 4..7? 顺序我们自己决定)
    # 我们固定:table layout = [i32 vt_backptr (4B)][u32 count (4B)][uoffset string (4B)]
    # tbl_size = 12; field 0 (string) at offset 8 in table; field 1 (count) at offset 4 in table
    vtable_off = len(buf)
    vt_size = 4 + 2 * 2  # 4 header + 2 fields × 2 bytes = 8
    tbl_size = 12
    buf += struct.pack("<HHHH", vt_size, tbl_size, 8, 4)

    # Table itself
    table_off = len(buf)
    backptr = table_off - vtable_off  # signed positive distance back
    # placeholder bytes; we'll fill after we know string offset
    buf += struct.pack("<i", backptr)  # vtable backptr
    buf += struct.pack("<I", count)  # field 1 (count)
    string_uoff_pos = len(buf)
    buf += b"\x00\x00\x00\x00"  # placeholder for uoffset to string

    # Optional padding before the string (exercises non-trivial offsets)
    buf += b"\x00" * root_extra_padding

    # String data
    string_off = len(buf)
    buf += _pack_string(name)

    # Now patch in offsets:
    # 1) header → table
    struct.pack_into("<I", buf, 0, table_off - 0)
    # 2) field 0 uoffset → string
    struct.pack_into("<I", buf, string_uoff_pos, string_off - string_uoff_pos)

    return bytes(buf)


# ---------------------------------------------------------------- numeric reads


def test_numeric_readers() -> None:
    buf = struct.pack("<BHIiQq", 0xAB, 0x1234, 0xDEADBEEF, -1, 0xCAFEBABE, -100)
    assert fbr.u8(buf, 0) == 0xAB
    assert fbr.u16(buf, 1) == 0x1234
    assert fbr.u32(buf, 3) == 0xDEADBEEF
    assert fbr.i32(buf, 7) == -1
    assert fbr.u64(buf, 11) == 0xCAFEBABE
    assert fbr.i64(buf, 19) == -100


def test_read_uint_byte_sizes() -> None:
    buf = b"\xff\x00\x00\x00\x00\x00\x00\x00"
    assert fbr.read_uint(buf, 0, 1) == 0xFF
    assert fbr.read_uint(buf, 0, 2) == 0x00FF
    assert fbr.read_uint(buf, 0, 4) == 0x000000FF
    assert fbr.read_uint(buf, 0, 8) == 0x00000000_000000FF

    with pytest.raises(ValueError, match="byte_size"):
        fbr.read_uint(buf, 0, 3)


# ---------------------------------------------------------------- table parsing


def test_parse_simple_table() -> None:
    buf = _build_simple_table(name="HelloEntity", count=42)

    root = fbr.root_table_offset(buf)
    assert root > 0

    table = fbr.parse_table(buf, root)
    assert table.vtable_size == 8
    assert table.table_size == 12
    assert sorted(table.fields.keys()) == [0, 1]

    # Field 0 = string
    s = fbr.read_string_field(buf, table.fields[0])
    assert s == "HelloEntity"

    # Field 1 = u32 count
    n = fbr.u32(buf, table.fields[1])
    assert n == 42


def test_parse_table_with_padding() -> None:
    """Make sure offsets still resolve when there's padding inside the buffer."""
    buf = _build_simple_table(name="WithGap", count=7, root_extra_padding=11)
    root = fbr.root_table_offset(buf)
    t = fbr.parse_table(buf, root)
    assert fbr.read_string_field(buf, t.fields[0]) == "WithGap"
    assert fbr.u32(buf, t.fields[1]) == 7


def test_parse_table_invalid_vtable_raises() -> None:
    # Buffer too small for a vtable backref
    bad = b"\x00\x00\x00"
    with pytest.raises((ValueError, struct.error)):
        fbr.parse_table(bad, 0)


# ---------------------------------------------------------------- vector


def test_read_vector_of_offsets() -> None:
    """Build a vector of 3 strings, verify read_vector_of_offsets walks it.

    FB uoffsets are always forward-pointing, so the layout is:
      [field uoffset] -> [vector header + item uoffsets] -> [strings].
    We build the buffer with placeholders, then patch in real offsets
    once everything's been emitted.
    """
    buf = bytearray()
    buf += b"\x00\x00\x00\x00"  # placeholder for root uoffset

    # Field uoffset position (the "field" we'll pass to the reader).
    field_off = len(buf)
    buf += b"\x00\x00\x00\x00"  # placeholder uoffset

    # Vector header + 3 item-uoffset slots — patch later.
    vector_off = len(buf)
    buf += struct.pack("<I", 3)  # length
    item_positions = []
    for _ in range(3):
        item_positions.append(len(buf))
        buf += b"\x00\x00\x00\x00"  # placeholder per item

    # Now actual string data, after all uoffsets.
    str_offs = []
    for s in ("alpha", "beta", "gamma"):
        str_offs.append(len(buf))
        buf += _pack_string(s)

    # Patch each item uoffset to its corresponding string.
    for pos, so in zip(item_positions, str_offs, strict=True):
        struct.pack_into("<I", buf, pos, so - pos)

    # Patch field uoffset → vector
    struct.pack_into("<I", buf, field_off, vector_off - field_off)
    # Root uoffset (not really used by this test, just satisfies invariant)
    struct.pack_into("<I", buf, 0, field_off)

    out = fbr.read_vector_of_offsets(bytes(buf), field_off)
    assert len(out) == 3
    assert [fbr.read_string(bytes(buf), o) for o in out] == ["alpha", "beta", "gamma"]


# ---------------------------------------------------------------- looks_like_table


def test_looks_like_table_true() -> None:
    buf = _build_simple_table(name="Probe", count=1)
    root = fbr.root_table_offset(buf)
    assert fbr.looks_like_table(buf, root) is True


def test_looks_like_table_false_at_garbage() -> None:
    # A random middle byte position usually doesn't look like a valid vtable.
    buf = b"\xff" * 64
    assert fbr.looks_like_table(buf, 32) is False


def test_looks_like_table_false_oob() -> None:
    buf = b"\x00\x00"
    assert fbr.looks_like_table(buf, 100) is False
    assert fbr.looks_like_table(buf, -1) is False
