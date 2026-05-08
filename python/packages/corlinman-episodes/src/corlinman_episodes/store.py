"""Async SQLite store for ``episodes.sqlite``.

The schema is documented in ``docs/design/phase4-w4-d1-design.md``
§"Episode shape — schema". Two tables:

- ``episodes`` — one row per distilled narrative slice, carrying
  source-key joins, a frozen importance score, optional embedding,
  and a ``last_referenced_at`` marker that drives the cold-archive
  pruner.
- ``episode_distillation_runs`` — idempotency log; one row per pass.
  ``UNIQUE(tenant_id, window_start, window_end)`` guards re-runs.

This module owns iter 1: bootstrap + schema only. Iter 2 layers on
``insert_episode`` + the run-log CRUD; later iters wire the join,
classifier, distiller, and runner.

All times are unix milliseconds.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

import aiosqlite

from corlinman_episodes.config import DEFAULT_TENANT_ID

# ---------------------------------------------------------------------------
# Episode kind enum
# ---------------------------------------------------------------------------


class EpisodeKind(StrEnum):
    """Narrative shape of an episode.

    The kind biases the LLM prompt segment selected at distillation
    time and is also the read-side filter for ``{{episodes.kind(<k>)}}``.
    Adding a new kind is purely additive (register a prompt segment +
    a classifier rule) so this enum is the canonical list — the Rust
    gateway resolver mirrors it.

    ``StrEnum`` (Python 3.11+) means raw column writes round-trip
    without an explicit converter and string comparisons just work.
    """

    CONVERSATION = "conversation"
    EVOLUTION = "evolution"
    INCIDENT = "incident"
    ONBOARDING = "onboarding"
    OPERATOR = "operator"

    @classmethod
    def values(cls) -> tuple[str, ...]:
        """Return the canonical ordered tuple of valid kind strings.

        Convenience for argparse ``choices=`` and the gateway resolver
        whitelist.
        """
        return tuple(k.value for k in cls)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

#: Idempotent ``CREATE … IF NOT EXISTS`` DDL applied on every open.
#: Matches §"Episode shape — schema" verbatim. Indexes are spelled out
#: rather than synthesised from a config so a SQLite ``EXPLAIN QUERY
#: PLAN`` against a checked-in DB matches the design doc grep output.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS episodes (
    id                  TEXT PRIMARY KEY,
    tenant_id           TEXT NOT NULL DEFAULT 'default',
    started_at          INTEGER NOT NULL,
    ended_at            INTEGER NOT NULL,
    kind                TEXT NOT NULL,
    summary_text        TEXT NOT NULL,
    source_session_keys TEXT NOT NULL DEFAULT '[]',
    source_signal_ids   TEXT NOT NULL DEFAULT '[]',
    source_history_ids  TEXT NOT NULL DEFAULT '[]',
    embedding           BLOB,
    embedding_dim       INTEGER,
    importance_score    REAL NOT NULL DEFAULT 0.5,
    last_referenced_at  INTEGER,
    distilled_by        TEXT NOT NULL,
    distilled_at        INTEGER NOT NULL,
    schema_version      INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_episodes_tenant_ended
    ON episodes(tenant_id, ended_at DESC);
CREATE INDEX IF NOT EXISTS idx_episodes_tenant_importance
    ON episodes(tenant_id, importance_score DESC, ended_at DESC);
CREATE INDEX IF NOT EXISTS idx_episodes_kind
    ON episodes(tenant_id, kind, ended_at DESC);

CREATE TABLE IF NOT EXISTS episode_distillation_runs (
    run_id           TEXT PRIMARY KEY,
    tenant_id        TEXT NOT NULL,
    window_start     INTEGER NOT NULL,
    window_end       INTEGER NOT NULL,
    started_at       INTEGER NOT NULL,
    finished_at      INTEGER,
    episodes_written INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL,
    error_message    TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_distillation_window
    ON episode_distillation_runs(tenant_id, window_start, window_end);
"""


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class EpisodesStore:
    """Async wrapper around ``episodes.sqlite``.

    The class is intentionally minimal at iter 1 — open / close /
    schema-bootstrap only. The CRUD surface lands in iter 2.

    Use as an async context manager so the connection closes cleanly
    even if the caller aborts mid-task.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    @classmethod
    async def open_or_create(cls, path: Path) -> EpisodesStore:
        """Open the DB (creating the file + schema if absent) and
        return an *entered* store.

        Convenience for callers that don't need ``async with`` framing
        (CLI subcommands, single-shot tests). The caller is responsible
        for ``await store.close()``.
        """
        store = cls(path)
        await store._open()
        return store

    async def __aenter__(self) -> EpisodesStore:
        await self._open()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def _open(self) -> None:
        # ``aiosqlite.connect`` will create the file on demand; the
        # schema script's ``IF NOT EXISTS`` guards make second opens
        # cheap. Foreign keys aren't used yet, but we toggle them on so
        # a future iter that adds an FK doesn't silently no-op.
        self._conn = await aiosqlite.connect(self._path)
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.executescript(SCHEMA_SQL)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("EpisodesStore used outside async context")
        return self._conn

    @property
    def path(self) -> Path:
        return self._path


__all__ = [
    "DEFAULT_TENANT_ID",
    "SCHEMA_SQL",
    "EpisodeKind",
    "EpisodesStore",
]
