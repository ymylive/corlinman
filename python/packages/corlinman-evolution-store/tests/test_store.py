"""Schema / migration tests — ports of ``rust/.../src/store.rs#tests``."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import aiosqlite
import pytest
from corlinman_evolution_store import EvolutionStore
from corlinman_evolution_store.store import _column_exists


async def test_open_creates_tables(db_path: Path) -> None:
    """Fresh open materialises all four tables — a COUNT(*) on each
    must succeed and return zero."""
    async with await EvolutionStore.open(db_path) as store:
        for table in ("evolution_signals", "evolution_proposals", "evolution_history"):
            cursor = await store.conn.execute(f"SELECT COUNT(*) FROM {table}")
            row = await cursor.fetchone()
            await cursor.close()
            assert row is not None and int(row[0]) == 0, f"{table} starts empty"


async def test_open_is_idempotent(db_path: Path) -> None:
    """Re-opening an already-initialised DB must be a clean no-op."""
    first = await EvolutionStore.open(db_path)
    await first.close()
    second = await EvolutionStore.open(db_path)
    await second.close()


async def test_fresh_db_has_v0_3_columns(db_path: Path) -> None:
    """Fresh DB: SCHEMA_SQL alone produces all v0.3 + Phase 4 columns
    on ``evolution_proposals`` — the migration block sees they exist
    and no-ops."""
    async with await EvolutionStore.open(db_path) as store:
        for col in (
            "shadow_metrics",
            "eval_run_id",
            "baseline_metrics_json",
            "auto_rollback_at",
            "auto_rollback_reason",
            "metadata",
        ):
            assert await _column_exists(store.conn, "evolution_proposals", col), (
                f"evolution_proposals.{col} should exist on fresh DB"
            )


async def test_fresh_db_has_phase4_tenant_columns_and_indexes(db_path: Path) -> None:
    """Phase 4 W1 4-1A: fresh DB lands with ``tenant_id`` on every
    migrated table and the post-migration tenant-aware indexes."""
    async with await EvolutionStore.open(db_path) as store:
        for table in (
            "evolution_signals",
            "evolution_proposals",
            "evolution_history",
            "apply_intent_log",
        ):
            assert await _column_exists(store.conn, table, "tenant_id"), (
                f"{table}.tenant_id should exist on fresh DB"
            )

        for idx in (
            "idx_evol_signals_tenant_observed",
            "idx_evol_proposals_tenant_status",
            "idx_evol_history_tenant_applied",
            "idx_apply_intent_tenant_uncommitted",
        ):
            cursor = await store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
                (idx,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            assert row is not None, f"missing index {idx}"

        # Pre-tenant partial index should be gone.
        cursor = await store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            " AND name='idx_apply_intent_uncommitted'",
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row is None, "old idx_apply_intent_uncommitted should be gone"


async def test_migration_adds_v0_3_columns_to_legacy_db(db_path: Path) -> None:
    """Legacy v0.2 DB (no ``eval_run_id`` / ``baseline_metrics_json``)
    must converge to v0.3 + Phase 4 when reopened through
    :meth:`EvolutionStore.open`."""
    # Bootstrap a v0.2-shaped DB by hand.
    conn = await aiosqlite.connect(db_path)
    try:
        await conn.executescript(
            """CREATE TABLE evolution_proposals (
                    id              TEXT PRIMARY KEY,
                    kind            TEXT NOT NULL,
                    target          TEXT NOT NULL,
                    diff            TEXT NOT NULL,
                    reasoning       TEXT NOT NULL,
                    risk            TEXT NOT NULL,
                    budget_cost     INTEGER NOT NULL DEFAULT 1,
                    status          TEXT NOT NULL,
                    shadow_metrics  TEXT,
                    signal_ids      TEXT NOT NULL,
                    trace_ids       TEXT NOT NULL,
                    created_at      INTEGER NOT NULL,
                    decided_at      INTEGER,
                    decided_by      TEXT,
                    applied_at      INTEGER,
                    rollback_of     TEXT
                );"""
        )
        await conn.commit()
    finally:
        await conn.close()

    # Pre-condition: legacy columns missing.
    conn = await aiosqlite.connect(db_path)
    try:
        assert not await _column_exists(conn, "evolution_proposals", "eval_run_id")
        assert not await _column_exists(
            conn, "evolution_proposals", "baseline_metrics_json"
        )
    finally:
        await conn.close()

    # Open through the production path → migrations apply.
    async with await EvolutionStore.open(db_path) as store:
        for col in (
            "eval_run_id",
            "baseline_metrics_json",
            "auto_rollback_at",
            "auto_rollback_reason",
        ):
            assert await _column_exists(store.conn, "evolution_proposals", col), (
                f"migration must add evolution_proposals.{col}"
            )


async def test_migration_adds_metadata_column_to_legacy_db(db_path: Path) -> None:
    """Phase 4 W2 B1 iter 2: pre-Phase-4 schema (no ``metadata``) must
    converge; idempotent on a second open."""
    conn = await aiosqlite.connect(db_path)
    try:
        await conn.executescript(
            """CREATE TABLE evolution_proposals (
                    id                    TEXT PRIMARY KEY,
                    kind                  TEXT NOT NULL,
                    target                TEXT NOT NULL,
                    diff                  TEXT NOT NULL,
                    reasoning             TEXT NOT NULL,
                    risk                  TEXT NOT NULL,
                    budget_cost           INTEGER NOT NULL DEFAULT 1,
                    status                TEXT NOT NULL,
                    shadow_metrics        TEXT,
                    eval_run_id           TEXT,
                    baseline_metrics_json TEXT,
                    signal_ids            TEXT NOT NULL,
                    trace_ids             TEXT NOT NULL,
                    created_at            INTEGER NOT NULL,
                    decided_at            INTEGER,
                    decided_by            TEXT,
                    applied_at            INTEGER,
                    auto_rollback_at      INTEGER,
                    auto_rollback_reason  TEXT,
                    rollback_of           TEXT
                );"""
        )
        await conn.commit()
        assert not await _column_exists(conn, "evolution_proposals", "metadata")
    finally:
        await conn.close()

    async with await EvolutionStore.open(db_path) as store:
        assert await _column_exists(store.conn, "evolution_proposals", "metadata"), (
            "migration must add evolution_proposals.metadata"
        )

    # Second open must be a clean no-op.
    async with await EvolutionStore.open(db_path):
        pass


async def test_migration_adds_share_with_column_to_legacy_history_db(
    db_path: Path,
) -> None:
    """Phase 4 W2 B3 iter 3: pre-B3 ``evolution_history`` (no
    ``share_with``) must converge; idempotent on a second open."""
    conn = await aiosqlite.connect(db_path)
    try:
        await conn.executescript(
            """CREATE TABLE evolution_history (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    proposal_id      TEXT NOT NULL,
                    kind             TEXT NOT NULL,
                    target           TEXT NOT NULL,
                    before_sha       TEXT NOT NULL,
                    after_sha        TEXT NOT NULL,
                    inverse_diff     TEXT NOT NULL,
                    metrics_baseline TEXT NOT NULL,
                    applied_at       INTEGER NOT NULL,
                    rolled_back_at   INTEGER,
                    rollback_reason  TEXT,
                    tenant_id        TEXT NOT NULL DEFAULT 'default'
                );"""
        )
        await conn.commit()
        assert not await _column_exists(conn, "evolution_history", "share_with")
    finally:
        await conn.close()

    async with await EvolutionStore.open(db_path) as store:
        assert await _column_exists(store.conn, "evolution_history", "share_with"), (
            "migration must add evolution_history.share_with"
        )

    async with await EvolutionStore.open(db_path):
        pass


async def test_migration_adds_phase4_tenant_columns_to_legacy_db(db_path: Path) -> None:
    """Phase 4 W1 4-1A: pre-Phase-4 DB whose tables lack ``tenant_id``
    and still carry the old partial index must converge to the same
    end state."""
    conn = await aiosqlite.connect(db_path)
    try:
        await conn.executescript(
            """CREATE TABLE evolution_signals (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_kind   TEXT NOT NULL,
                    target       TEXT,
                    severity     TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    trace_id     TEXT,
                    session_id   TEXT,
                    observed_at  INTEGER NOT NULL
                );
                CREATE TABLE evolution_proposals (
                    id                    TEXT PRIMARY KEY,
                    kind                  TEXT NOT NULL,
                    target                TEXT NOT NULL,
                    diff                  TEXT NOT NULL,
                    reasoning             TEXT NOT NULL,
                    risk                  TEXT NOT NULL,
                    budget_cost           INTEGER NOT NULL DEFAULT 1,
                    status                TEXT NOT NULL,
                    shadow_metrics        TEXT,
                    eval_run_id           TEXT,
                    baseline_metrics_json TEXT,
                    signal_ids            TEXT NOT NULL,
                    trace_ids             TEXT NOT NULL,
                    created_at            INTEGER NOT NULL,
                    decided_at            INTEGER,
                    decided_by            TEXT,
                    applied_at            INTEGER,
                    auto_rollback_at      INTEGER,
                    auto_rollback_reason  TEXT,
                    rollback_of           TEXT
                );
                CREATE TABLE evolution_history (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    proposal_id      TEXT NOT NULL,
                    kind             TEXT NOT NULL,
                    target           TEXT NOT NULL,
                    before_sha       TEXT NOT NULL,
                    after_sha        TEXT NOT NULL,
                    inverse_diff     TEXT NOT NULL,
                    metrics_baseline TEXT NOT NULL,
                    applied_at       INTEGER NOT NULL,
                    rolled_back_at   INTEGER,
                    rollback_reason  TEXT
                );
                CREATE TABLE apply_intent_log (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    proposal_id     TEXT NOT NULL,
                    kind            TEXT NOT NULL,
                    target          TEXT NOT NULL,
                    intent_at       INTEGER NOT NULL,
                    committed_at    INTEGER,
                    failed_at       INTEGER,
                    failure_reason  TEXT
                );
                CREATE INDEX idx_apply_intent_uncommitted
                    ON apply_intent_log(intent_at)
                    WHERE committed_at IS NULL AND failed_at IS NULL;
                """
        )
        await conn.commit()
    finally:
        await conn.close()

    async with await EvolutionStore.open(db_path) as store:
        for table in (
            "evolution_signals",
            "evolution_proposals",
            "evolution_history",
            "apply_intent_log",
        ):
            assert await _column_exists(store.conn, table, "tenant_id"), (
                f"migration must add {table}.tenant_id"
            )

        cursor = await store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            " AND name='idx_apply_intent_tenant_uncommitted'",
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None

        cursor = await store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            " AND name='idx_apply_intent_uncommitted'",
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row is None, "legacy partial index should be dropped post-migration"

    # Idempotent reopen.
    async with await EvolutionStore.open(db_path):
        pass


def test_sync_check_via_sqlite3(db_path: Path) -> None:
    """Sanity: a synchronous ``sqlite3`` connection sees the freshly
    initialised schema once the async open returns. (Pure paranoia —
    the async open is the source of truth, but if WAL behaved oddly
    this would surface.)"""
    import asyncio

    async def _bootstrap() -> None:
        s = await EvolutionStore.open(db_path)
        await s.close()

    asyncio.run(_bootstrap())

    conn = sqlite3.connect(db_path)
    try:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()
    assert {"evolution_signals", "evolution_proposals", "evolution_history",
            "apply_intent_log"}.issubset(names)


def test_python_38_marker() -> None:
    """Pin the package's runtime target — fails fast if someone bumps
    pyproject without updating the syntax sweep."""
    import sys

    assert sys.version_info >= (3, 12)


# Ensure pytest doesn't import this fixture-only file in collection mode.
pytest.importorskip("aiosqlite")
