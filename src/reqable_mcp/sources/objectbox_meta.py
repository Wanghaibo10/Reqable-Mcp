"""ObjectBox internal model schema extractor.

ObjectBox stores its model definition as FlatBuffers blobs in the main
LMDB sub-database, under reserved 8-byte big-endian keys (e.g.
``\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x0b``). Each value is an
``Entity`` table describing one entity's name, internal id, and
properties (id, name, type, vtable slot).

This module extracts just enough to decode user-data tables for the
entities we care about (``CaptureRecordHistoryEntity``).

The Entity / Property table layouts are reverse-engineered by
inspection of the on-disk format on a real Reqable LMDB. They line up
with the public ObjectBox model schema. Importantly, we *don't*
hard-code any field IDs from that schema — we inspect the vtable each
record carries with itself, so a future ObjectBox revision that adds
fields will simply yield extra entries we ignore.

References (corroborating the layout used here):
  * https://github.com/objectbox/objectbox-c
  * https://docs.objectbox.io/advanced/data-model
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass

import lmdb

from . import flatbuffers_reader as fbr

# The LMDB key range reserved for ObjectBox internal metadata.
# All metadata keys are 8 bytes, big-endian, and start with seven \x00
# bytes followed by a small id (the entity / index id). User data keys
# never start with seven zero bytes.
_META_PREFIX = b"\x00" * 7

# ObjectBox property type codes (subset we care about).
# https://github.com/objectbox/objectbox-c/blob/main/include/objectbox.h
PROP_BOOL = 1
PROP_BYTE = 2
PROP_SHORT = 3
PROP_CHAR = 4
PROP_INT = 5
PROP_LONG = 6
PROP_FLOAT = 7
PROP_DOUBLE = 8
PROP_STRING = 9
PROP_DATE = 10
PROP_RELATION = 11
PROP_DATE_NANO = 12
PROP_FLEX = 13
PROP_BYTE_VECTOR = 23
PROP_STRING_VECTOR = 30


@dataclass(frozen=True)
class Property:
    """One property of an entity.

    ``vt_index`` is the index in *user data table* vtables (not in the
    Entity meta vtable). It maps directly to ``vtable[vt_index]``
    when reading actual records.
    """

    pid: int
    vt_index: int
    name: str
    type_code: int


@dataclass(frozen=True)
class Entity:
    eid: int
    name: str
    properties: list[Property]

    def property_by_name(self, name: str) -> Property | None:
        for p in self.properties:
            if p.name == name:
                return p
        return None


# --------------------------------------------------------------------- helpers


def _is_meta_key(key: bytes) -> bool:
    return len(key) == 8 and key.startswith(_META_PREFIX)


def _parse_property(buf: bytes, table_off: int) -> Property | None:
    """Decode one Property meta blob.

    Empirical layout (vtable slots, observed across multiple entities):

    * slot 1 → property id (u32, inline)
    * slot 6 → name (string field — uoffset to length-prefixed UTF-8)
    * slot 7 → packed:  u16 type_code | u16 flags  (low 16 bits = type)
    * slot 8 → packed:  u16 vt_offset_in_user_table | u16 pid_lo
              The vt_offset_in_user_table value, when divided by 2 and
              with the leading two header u16 stripped, is the vtable
              index used by user data records to find this field.

    These positions were validated against
    ``CaptureRecordHistoryEntity`` on a real Reqable 3.0 install. If a
    future ObjectBox version reorders things we'll see name=None and
    skip the property gracefully (the caller logs and continues).
    """
    try:
        t = fbr.parse_table(buf, table_off)
    except ValueError:
        return None

    if 6 not in t.fields:
        return None

    # slot 6 = name (string)
    try:
        name = fbr.read_string_field(buf, t.fields[6])
    except (UnicodeDecodeError, IndexError):
        return None
    if not name or not all(0x20 <= ord(c) < 0x7F or c in "_-" for c in name):
        return None

    # slot 1 = property id (u32 inline)
    pid = fbr.u32(buf, t.fields[1]) if 1 in t.fields else 0

    # slot 7 = type code (low 16 bits of u32)
    type_code = 0
    if 7 in t.fields:
        type_code = fbr.u32(buf, t.fields[7]) & 0xFFFF

    # slot 8 — first u16 is the vtable byte offset within user-data tables.
    # FB vtables start at byte offset 4 (after vtable_size + table_size headers).
    # The u16 we read is the *byte offset in the user table*, e.g. 0x4 / 0x6 / 0x8.
    # Convert to vtable slot index by: (byte_offset - 4) / 2.
    vt_index = -1
    if 8 in t.fields:
        byte_off = fbr.u16(buf, t.fields[8])
        if byte_off >= 4 and byte_off % 2 == 0:
            vt_index = (byte_off - 4) // 2

    return Property(pid=pid, vt_index=vt_index, name=name, type_code=type_code)


def _parse_entity(buf: bytes) -> Entity | None:
    """Decode one Entity meta blob (the value at e.g. key 0x0b).

    Empirical layout of the Entity table:

    * slot 3 → name (string)            — observed: "CaptureRecordHistoryEntity"
    * slot 4 → properties [Property]    — vector of sub-tables
    * slot 1 → entity id (u32 inline)   — best-effort, may be 0

    We scan candidate slots holding a vector-of-tables and pick the
    largest as the property list, in case ObjectBox shuffles slots.
    """
    try:
        root = fbr.root_table_offset(buf)
        t = fbr.parse_table(buf, root)
    except ValueError:
        return None

    name = ""
    if 3 in t.fields:
        try:
            n = fbr.read_string_field(buf, t.fields[3])
            if n.endswith("Entity"):
                name = n
        except (UnicodeDecodeError, IndexError):
            pass

    if not name:
        return None

    # Find the property vector: a uoffset → u32 length → N uoffsets to tables.
    best_props: list[Property] = []
    for _slot, abs_off in t.fields.items():
        try:
            target = fbr.deref_uoffset(buf, abs_off)
            if target + 4 > len(buf):
                continue
            count = fbr.u32(buf, target)
            if count == 0 or count > 200:
                continue
            sub_offs: list[int] = []
            ok = True
            for i in range(count):
                item_pos = target + 4 + i * 4
                if item_pos + 4 > len(buf):
                    ok = False
                    break
                sub_t = item_pos + fbr.u32(buf, item_pos)
                if not fbr.looks_like_table(buf, sub_t):
                    ok = False
                    break
                sub_offs.append(sub_t)
            if not ok:
                continue
            props: list[Property] = []
            for st in sub_offs:
                p = _parse_property(buf, st)
                if p is not None:
                    props.append(p)
            if len(props) > len(best_props):
                best_props = props
        except (ValueError, IndexError):
            continue

    eid = 0
    if 1 in t.fields:
        with contextlib.suppress(IndexError):
            eid = fbr.u32(buf, t.fields[1])

    return Entity(eid=eid, name=name, properties=best_props)


# --------------------------------------------------------------------- public


def load_schema(env: lmdb.Environment) -> dict[str, Entity]:
    """Walk the LMDB metadata key range and decode every Entity meta blob.

    Returns a dict keyed by entity name. Entries that fail to decode are
    silently dropped — callers log if a required entity (e.g.
    ``CaptureRecordHistoryEntity``) is absent and treat the LMDB as
    incompatible.
    """
    out: dict[str, Entity] = {}
    with env.begin() as txn:
        cur = txn.cursor()
        if not cur.set_range(_META_PREFIX):
            return out
        while True:
            try:
                key = cur.key()
                val = cur.value()
            except lmdb.Error:
                break
            if not _is_meta_key(key):
                break
            ent = _parse_entity(bytes(val))
            if ent is not None:
                out[ent.name] = ent
            if not cur.next():
                break
    return out


__all__ = [
    "PROP_BOOL",
    "PROP_BYTE",
    "PROP_BYTE_VECTOR",
    "PROP_DATE",
    "PROP_DATE_NANO",
    "PROP_DOUBLE",
    "PROP_FLEX",
    "PROP_FLOAT",
    "PROP_INT",
    "PROP_LONG",
    "PROP_RELATION",
    "PROP_SHORT",
    "PROP_STRING",
    "PROP_STRING_VECTOR",
    "Entity",
    "Property",
    "load_schema",
]
