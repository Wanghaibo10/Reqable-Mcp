"""Tests for ObjectBox schema extraction.

These tests run against the user's real Reqable LMDB if available
(see ``conftest.py`` ``real_lmdb_required`` fixture). On CI machines or
fresh installs they skip.

Why no synthetic LMDB fixture?

Building a faithful ObjectBox-compatible LMDB from scratch would
require re-implementing ObjectBox's writer in Python — the very thing
we're avoiding. The reader is small and easy to inspect; integration
tests against a real LMDB give us realistic coverage.
"""

from __future__ import annotations

from pathlib import Path

import lmdb

from reqable_mcp.sources.objectbox_meta import (
    PROP_LONG,
    PROP_STRING,
    Entity,
    load_schema,
)


def test_load_schema_finds_capture_record(real_lmdb_required: Path) -> None:
    env = lmdb.open(
        str(real_lmdb_required),
        readonly=True,
        lock=False,
        max_dbs=64,
        subdir=True,
    )
    try:
        schema = load_schema(env)
    finally:
        env.close()

    assert "CaptureRecordHistoryEntity" in schema, (
        "Reqable LMDB does not contain CaptureRecordHistoryEntity. "
        f"Found entities: {sorted(schema.keys())}"
    )
    ent = schema["CaptureRecordHistoryEntity"]
    assert isinstance(ent, Entity)
    assert ent.name == "CaptureRecordHistoryEntity"

    # Empirical: 5 properties — id / uid / timestamp / dbData / dbUniqueId.
    # If Reqable adds new fields we'd see >5; that's fine.
    names = {p.name for p in ent.properties}
    required = {"id", "uid", "timestamp", "dbData"}
    missing = required - names
    assert not missing, f"missing required properties: {missing} (found {names})"


def test_property_types_make_sense(real_lmdb_required: Path) -> None:
    env = lmdb.open(
        str(real_lmdb_required),
        readonly=True,
        lock=False,
        max_dbs=64,
        subdir=True,
    )
    try:
        schema = load_schema(env)
    finally:
        env.close()

    ent = schema["CaptureRecordHistoryEntity"]
    by_name = {p.name: p for p in ent.properties}

    # ``id`` is ObjectBox's primary key — always Long
    assert by_name["id"].type_code == PROP_LONG
    # ``uid`` is the UUID we display to users — String
    assert by_name["uid"].type_code == PROP_STRING
    # ``timestamp`` is unix ms — Long (we accept either PROP_LONG or PROP_DATE)
    assert by_name["timestamp"].type_code in (PROP_LONG, 10)


def test_vt_indexes_are_consistent(real_lmdb_required: Path) -> None:
    """Every property must have a non-negative vtable slot.

    A negative ``vt_index`` would mean we failed to extract the slot
    info from the meta blob, which would break user-data decoding.
    """
    env = lmdb.open(
        str(real_lmdb_required),
        readonly=True,
        lock=False,
        max_dbs=64,
        subdir=True,
    )
    try:
        schema = load_schema(env)
    finally:
        env.close()

    ent = schema["CaptureRecordHistoryEntity"]
    for p in ent.properties:
        assert p.vt_index >= 0, f"vt_index missing for {p.name}: {p}"
        assert p.pid > 0, f"pid missing for {p.name}: {p}"


def test_schema_includes_other_entities(real_lmdb_required: Path) -> None:
    """Sanity: at least a handful of entities discovered."""
    env = lmdb.open(
        str(real_lmdb_required),
        readonly=True,
        lock=False,
        max_dbs=64,
        subdir=True,
    )
    try:
        schema = load_schema(env)
    finally:
        env.close()

    # Reqable defines roughly a dozen entities (RestApi, Cookie, Cert, ...);
    # we don't need them all, just confirm we discover more than one.
    assert len(schema) >= 2, (
        f"only {len(schema)} entity discovered: {sorted(schema.keys())}"
    )
