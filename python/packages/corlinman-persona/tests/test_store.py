"""Tests for :mod:`corlinman_persona.store`.

Covers the round-trip path (upsert / get), the dedup + cap invariant on
``recent_topics``, JSON serialisation of ``state_json``, the clamped
``update_fatigue`` arithmetic, and the ``delete`` happy / sad paths.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from corlinman_persona.state import RECENT_TOPICS_CAP, PersonaState
from corlinman_persona.store import PersonaStore


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "agent_state.sqlite"


async def test_open_or_create_creates_schema(db_path: Path) -> None:
    """Opening a fresh path materialises both the file and the table.

    Phase 3.1 added ``tenant_id`` so multi-tenant rollout in Phase 4 is
    a single-line call-site change. This test pins the column set so
    the next migration can't silently drop the column.
    """
    store = await PersonaStore.open_or_create(db_path)
    try:
        assert db_path.exists()
        # Table must exist with the documented columns.
        conn = sqlite3.connect(db_path)
        try:
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(agent_persona_state)")
            }
        finally:
            conn.close()
        assert cols == {
            "agent_id",
            "tenant_id",
            "mood",
            "fatigue",
            "recent_topics",
            "updated_at",
            "state_json",
        }
    finally:
        await store.close()


async def test_get_missing_returns_none(db_path: Path) -> None:
    async with PersonaStore(db_path) as store:
        assert await store.get("nobody") is None


async def test_upsert_then_get_round_trips_all_fields(db_path: Path) -> None:
    state = PersonaState(
        agent_id="mentor",
        mood="focused",
        fatigue=0.42,
        recent_topics=["alpha", "beta"],
        updated_at_ms=1_700_000_000_000,
        state_json={"trust": 0.9, "tone": "warm"},
    )
    async with PersonaStore(db_path) as store:
        await store.upsert(state)
        got = await store.get("mentor")
    assert got is not None
    assert got.agent_id == "mentor"
    assert got.mood == "focused"
    assert got.fatigue == pytest.approx(0.42)
    assert got.recent_topics == ["alpha", "beta"]
    assert got.updated_at_ms == 1_700_000_000_000
    assert got.state_json == {"trust": 0.9, "tone": "warm"}


async def test_upsert_replaces_existing_row(db_path: Path) -> None:
    """Re-upsert with the same agent_id must not duplicate or merge."""
    async with PersonaStore(db_path) as store:
        await store.upsert(
            PersonaState(agent_id="mentor", mood="neutral", fatigue=0.1, updated_at_ms=1)
        )
        await store.upsert(
            PersonaState(agent_id="mentor", mood="tired", fatigue=0.8, updated_at_ms=2)
        )
        rows = await store.list_all()
    assert len(rows) == 1
    assert rows[0].mood == "tired"
    assert rows[0].fatigue == pytest.approx(0.8)


async def test_push_recent_topic_dedup_moves_to_tail(db_path: Path) -> None:
    """Same topic seen twice keeps the freshest position."""
    async with PersonaStore(db_path) as store:
        await store.upsert(
            PersonaState(
                agent_id="mentor",
                recent_topics=["a", "b", "c"],
                updated_at_ms=1,
            )
        )
        await store.push_recent_topic("mentor", "a")
        got = await store.get("mentor")
    assert got is not None
    assert got.recent_topics == ["b", "c", "a"]


async def test_push_recent_topic_caps_at_twenty(db_path: Path) -> None:
    """The 21st distinct topic must evict the oldest entry."""
    async with PersonaStore(db_path) as store:
        seed = [f"t{i}" for i in range(RECENT_TOPICS_CAP)]
        await store.upsert(
            PersonaState(agent_id="mentor", recent_topics=seed, updated_at_ms=1)
        )
        await store.push_recent_topic("mentor", "fresh")
        got = await store.get("mentor")
    assert got is not None
    assert len(got.recent_topics) == RECENT_TOPICS_CAP
    assert got.recent_topics[-1] == "fresh"
    # Oldest ("t0") must have been evicted.
    assert "t0" not in got.recent_topics


async def test_push_recent_topic_no_op_for_missing_agent(db_path: Path) -> None:
    """The seeder is the only path that creates rows; push must not."""
    async with PersonaStore(db_path) as store:
        await store.push_recent_topic("ghost", "topic")
        assert await store.get("ghost") is None


async def test_upsert_dedups_and_caps_on_write(db_path: Path) -> None:
    """A caller passing duplicates / overflow gets the invariant enforced."""
    over = [f"t{i}" for i in range(RECENT_TOPICS_CAP + 5)]
    async with PersonaStore(db_path) as store:
        await store.upsert(
            PersonaState(
                agent_id="mentor",
                recent_topics=[*over, "t3"],  # duplicate "t3"
                updated_at_ms=1,
            )
        )
        got = await store.get("mentor")
    assert got is not None
    assert len(got.recent_topics) == RECENT_TOPICS_CAP
    # Duplicate "t3" must keep its freshest position (the appended copy).
    assert got.recent_topics.count("t3") == 1
    assert got.recent_topics[-1] == "t3"


async def test_update_fatigue_clamps_high_and_low(db_path: Path) -> None:
    async with PersonaStore(db_path) as store:
        await store.upsert(
            PersonaState(agent_id="mentor", fatigue=0.5, updated_at_ms=1)
        )
        await store.update_fatigue("mentor", 5.0)  # would overflow 1.0
        high = await store.get("mentor")
        assert high is not None
        assert high.fatigue == pytest.approx(1.0)

        await store.update_fatigue("mentor", -10.0)  # would underflow 0.0
        low = await store.get("mentor")
        assert low is not None
        assert low.fatigue == pytest.approx(0.0)


async def test_update_mood_changes_only_mood(db_path: Path) -> None:
    async with PersonaStore(db_path) as store:
        await store.upsert(
            PersonaState(
                agent_id="mentor",
                mood="neutral",
                fatigue=0.3,
                recent_topics=["x"],
                updated_at_ms=1,
                state_json={"k": "v"},
            )
        )
        await store.update_mood("mentor", "curious")
        got = await store.get("mentor")
    assert got is not None
    assert got.mood == "curious"
    # Other fields untouched.
    assert got.fatigue == pytest.approx(0.3)
    assert got.recent_topics == ["x"]
    assert got.state_json == {"k": "v"}


async def test_recent_topics_serialised_as_json_array(db_path: Path) -> None:
    """The wire format on disk must be a JSON array — confirms callers
    using sqlite3 directly can decode the column without bespoke logic."""
    async with PersonaStore(db_path) as store:
        await store.upsert(
            PersonaState(
                agent_id="mentor", recent_topics=["a", "b"], updated_at_ms=1
            )
        )
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT recent_topics, state_json FROM agent_persona_state WHERE agent_id = ?",
            ("mentor",),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert json.loads(row[0]) == ["a", "b"]
    assert json.loads(row[1]) == {}


async def test_list_all_sorted_by_agent_id(db_path: Path) -> None:
    async with PersonaStore(db_path) as store:
        await store.upsert(PersonaState(agent_id="zeta", updated_at_ms=1))
        await store.upsert(PersonaState(agent_id="alpha", updated_at_ms=1))
        await store.upsert(PersonaState(agent_id="mu", updated_at_ms=1))
        rows = await store.list_all()
    assert [r.agent_id for r in rows] == ["alpha", "mu", "zeta"]


async def test_delete_removes_row_and_returns_flag(db_path: Path) -> None:
    async with PersonaStore(db_path) as store:
        await store.upsert(PersonaState(agent_id="mentor", updated_at_ms=1))
        assert await store.delete("mentor") is True
        assert await store.get("mentor") is None
        assert await store.delete("mentor") is False


async def test_decode_corrupt_topics_yields_empty_list(db_path: Path) -> None:
    """If the column gets corrupted, the row still loads (degraded)."""
    async with PersonaStore(db_path) as store:
        await store.upsert(PersonaState(agent_id="mentor", updated_at_ms=1))
    # Stomp the column out of band.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE agent_persona_state SET recent_topics = ? WHERE agent_id = ?",
            ("not json", "mentor"),
        )
        conn.commit()
    finally:
        conn.close()
    async with PersonaStore(db_path) as store:
        got = await store.get("mentor")
    assert got is not None
    assert got.recent_topics == []


async def test_use_outside_context_raises(db_path: Path) -> None:
    store = PersonaStore(db_path)
    with pytest.raises(RuntimeError, match="outside async context"):
        _ = store.conn


# ---------------------------------------------------------------------------
# Phase 3.1: tenant_id scoping
#
# v0 schema kept ``agent_id`` as PRIMARY KEY; Phase 3.1 adds the
# ``tenant_id`` column so Phase 4's multi-tenant fan-out has a place to
# write to without a heavyweight ALTER TABLE then. The PK rewrite to a
# composite key is deferred to Phase 4 (SQLite can't change a PK in
# place). Practical implication: today every row's tenant_id is
# ``'default'``; the read path filter still scopes correctly because
# every row matches the same value.
# ---------------------------------------------------------------------------


async def test_list_all_default_tenant_skips_other_tenant_rows(db_path: Path) -> None:
    """A row whose ``tenant_id`` got rewritten out of band must not be
    returned by the default-tenant read. Pins the WHERE-clause scope
    so Phase 4 can flip tenant_id at the call site and trust the
    isolation."""
    import sqlite3

    async with PersonaStore(db_path) as store:
        await store.upsert(PersonaState(agent_id="alpha", updated_at_ms=1))
        await store.upsert(PersonaState(agent_id="beta", updated_at_ms=1))
    # Re-tag one row's tenant_id without going through the
    # store API — simulates the Phase 4 multi-tenant world where
    # the same DB carries rows from many tenants.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE agent_persona_state SET tenant_id = 'other-tenant' WHERE agent_id = 'beta'"
        )
        conn.commit()
    finally:
        conn.close()

    async with PersonaStore(db_path) as store:
        default_rows = await store.list_all()
        other_rows = await store.list_all(tenant_id="other-tenant")
    assert [r.agent_id for r in default_rows] == ["alpha"]
    assert [r.agent_id for r in other_rows] == ["beta"]


async def test_get_filters_by_tenant_id(db_path: Path) -> None:
    """Same shape as the list test — direct ``get`` lookups must
    respect tenant scoping too."""
    import sqlite3

    async with PersonaStore(db_path) as store:
        await store.upsert(PersonaState(agent_id="mentor", mood="curious", updated_at_ms=1))
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE agent_persona_state SET tenant_id = 'other-tenant' WHERE agent_id = 'mentor'"
        )
        conn.commit()
    finally:
        conn.close()
    async with PersonaStore(db_path) as store:
        # Row exists, but not in the default tenant.
        assert await store.get("mentor") is None
        got = await store.get("mentor", tenant_id="other-tenant")
        assert got is not None
        assert got.mood == "curious"
