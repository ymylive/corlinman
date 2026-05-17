"""Ports the ``type_tests`` module in
``rust/crates/corlinman-memory-host/src/lib.rs`` plus a couple of
round-trip checks specific to the Python serde shape (no field aliases —
the wire shape is the canonical reference)."""

from __future__ import annotations

import json

from corlinman_memory_host import MemoryFilter, MemoryQuery


def test_memory_filter_serde_snake_case() -> None:
    """Mirror of ``memory_filter_serde_snake_case``: the variant
    discriminator must come out as ``snake_case`` under the ``kind``
    key for wire-compat with the Rust ``#[serde(rename_all =
    "snake_case")]`` derive."""
    f = MemoryFilter.tag_eq("kind", "note")
    s = json.dumps(f.to_json())
    assert '"kind": "tag_eq"' in s, f"got: {s}"


def test_memory_query_default_fields() -> None:
    """Mirror of ``memory_query_default_fields``: the ``filters`` and
    ``namespace`` fields must default sensibly when absent from the
    wire payload."""
    raw = '{"text":"hi","top_k":3}'
    q = MemoryQuery.from_json(json.loads(raw))
    assert q.text == "hi"
    assert q.top_k == 3
    assert q.filters == []
    assert q.namespace is None


def test_memory_filter_tag_in_round_trip() -> None:
    f = MemoryFilter.tag_in("project", ["alpha", "beta"])
    payload = f.to_json()
    assert payload == {
        "kind": "tag_in",
        "tag": "project",
        "values": ["alpha", "beta"],
    }
    decoded = MemoryFilter.from_json(payload)
    assert decoded == f


def test_memory_filter_created_after_round_trip() -> None:
    f = MemoryFilter.created_after(1_700_000_000)
    payload = f.to_json()
    assert payload == {"kind": "created_after", "unix": 1_700_000_000}
    decoded = MemoryFilter.from_json(payload)
    assert decoded == f
