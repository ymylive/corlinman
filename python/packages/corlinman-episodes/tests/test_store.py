"""Iter 1 tests — schema bootstrap + idempotent re-open.

The store is async-only; ``conftest`` from the workspace root sets
``asyncio_mode = "auto"`` so test functions can be plain ``async def``
without an explicit ``@pytest.mark.asyncio`` decorator.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from corlinman_episodes import EpisodeKind, EpisodesConfig, EpisodesStore


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Per-test path to a non-existent ``episodes.sqlite``."""
    return tmp_path / "episodes.sqlite"


# ---------------------------------------------------------------------------
# Schema round-trip
# ---------------------------------------------------------------------------


async def test_open_or_create_materialises_schema(db_path: Path) -> None:
    """Opening a fresh path creates the file + both tables + indexes.

    Pins the column set so a future migration that drops a column
    fails loudly here — the design-doc schema is load-bearing for the
    placeholder resolver and the importance ranker.
    """
    store = await EpisodesStore.open_or_create(db_path)
    try:
        assert db_path.exists()

        # Use the sync sqlite3 client for the introspection asserts —
        # PRAGMA round-trips are easier to read than aiosqlite-driven
        # equivalents.
        conn = sqlite3.connect(db_path)
        try:
            episode_cols = {
                row[1] for row in conn.execute("PRAGMA table_info(episodes)")
            }
            run_cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(episode_distillation_runs)")
            }
            indexes = {
                row[1]
                for row in conn.execute(
                    "SELECT type, name FROM sqlite_master WHERE type = 'index'"
                )
            }
        finally:
            conn.close()
    finally:
        await store.close()

    assert episode_cols == {
        "id",
        "tenant_id",
        "started_at",
        "ended_at",
        "kind",
        "summary_text",
        "source_session_keys",
        "source_signal_ids",
        "source_history_ids",
        "embedding",
        "embedding_dim",
        "importance_score",
        "last_referenced_at",
        "distilled_by",
        "distilled_at",
        "schema_version",
    }
    assert run_cols == {
        "run_id",
        "tenant_id",
        "window_start",
        "window_end",
        "started_at",
        "finished_at",
        "episodes_written",
        "status",
        "error_message",
    }
    # All three documented secondary indexes + the unique-window guard.
    assert {
        "idx_episodes_tenant_ended",
        "idx_episodes_tenant_importance",
        "idx_episodes_kind",
        "uq_distillation_window",
    } <= indexes


async def test_reopen_is_idempotent(db_path: Path) -> None:
    """Two ``open_or_create`` calls don't double-create or error.

    The schema script uses ``IF NOT EXISTS`` clauses on every statement
    so re-opening an established DB is a cheap no-op. Without that
    guarantee the persona-style ``async with`` framing would lose its
    "open whenever you need it" ergonomic.
    """
    first = await EpisodesStore.open_or_create(db_path)
    await first.close()
    second = await EpisodesStore.open_or_create(db_path)
    try:
        # Sanity: still responds to a trivial query.
        cursor = await second.conn.execute("SELECT COUNT(*) FROM episodes")
        row = await cursor.fetchone()
        await cursor.close()
        assert row == (0,)
    finally:
        await second.close()


async def test_async_context_manager_closes(db_path: Path) -> None:
    """``async with`` exits with the connection released."""
    async with EpisodesStore(db_path) as store:
        assert store.conn is not None

    # After exit, accessing ``conn`` raises — mirrors PersonaStore so
    # callers that leak the object trip an obvious failure rather than
    # using a stale cursor.
    store_after = EpisodesStore(db_path)
    with pytest.raises(RuntimeError):
        _ = store_after.conn


# ---------------------------------------------------------------------------
# EpisodeKind enum
# ---------------------------------------------------------------------------


def test_episode_kind_values_are_canonical() -> None:
    """The enum exposes the five design-doc kinds in a stable order.

    Order matters for the gateway resolver mirror — ``EpisodeKind.values()``
    feeds the Rust ``strum`` whitelist via a tested-in-Python contract.
    """
    assert EpisodeKind.values() == (
        "conversation",
        "evolution",
        "incident",
        "onboarding",
        "operator",
    )


def test_episode_kind_round_trips_as_str() -> None:
    """``EpisodeKind`` is a ``StrEnum`` — column writes don't need a
    converter and JSON dumps emit the bare value.
    """
    # ``StrEnum.__str__`` returns the value, not ``ClassName.MEMBER``;
    # this is the load-bearing difference vs. plain ``Enum`` and lets
    # the resolver render kind names without ``.value``.
    assert str(EpisodeKind.INCIDENT) == "incident"
    assert EpisodeKind.INCIDENT.value == "incident"
    # Comparison against a raw string works (str-mixin); the resolver
    # does this when filtering ``{{episodes.kind(<k>)}}``.
    assert EpisodeKind.INCIDENT == "incident"


# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------


def test_episodes_config_defaults_match_design_doc() -> None:
    """Defaults from §Configuration in the design doc.

    Tightly-coupled to the doc: a doc edit without a default change is
    fine, but a default change without a doc edit must fail this
    test. Picked the load-bearing values (window, schedule, archival
    horizon, query cap).
    """
    cfg = EpisodesConfig()
    assert cfg.enabled is True
    assert cfg.schedule == "0 6 * * * *"
    assert cfg.distillation_window_hours == 24.0
    assert cfg.min_session_count_per_episode == 1
    assert cfg.min_window_secs == 3600
    assert cfg.max_messages_per_call == 60
    assert cfg.llm_provider_alias == "default-summary"
    assert cfg.embedding_provider_alias == "small"
    assert cfg.max_episodes_per_query == 5
    assert cfg.last_week_top_n == 5
    assert cfg.cold_archive_days == 180
    assert cfg.run_stale_after_secs == 1800
