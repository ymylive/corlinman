"""Shared fixtures: ephemeral sqlite files + helpers to seed them.

The Rust crate ``corlinman-evolution`` owns the canonical schema, but this
test package can't depend on Rust at test time. We replicate the
``CREATE TABLE`` statements verbatim from
``rust/crates/corlinman-evolution/src/schema.rs`` here. If they drift, the
``test_engine`` round-trip test will fail loudly when an INSERT references
a missing column.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

# Mirrors rust/crates/corlinman-evolution/src/schema.rs SCHEMA_SQL.
EVOLUTION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS evolution_signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_kind   TEXT NOT NULL,
    target       TEXT,
    severity     TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    trace_id     TEXT,
    session_id   TEXT,
    observed_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evol_signals_kind_target
    ON evolution_signals(event_kind, target);
CREATE INDEX IF NOT EXISTS idx_evol_signals_observed
    ON evolution_signals(observed_at);

CREATE TABLE IF NOT EXISTS evolution_proposals (
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
    rollback_of     TEXT REFERENCES evolution_proposals(id)
);
CREATE INDEX IF NOT EXISTS idx_evol_proposals_status
    ON evolution_proposals(status);
CREATE INDEX IF NOT EXISTS idx_evol_proposals_created
    ON evolution_proposals(created_at);

CREATE TABLE IF NOT EXISTS evolution_history (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id      TEXT NOT NULL REFERENCES evolution_proposals(id),
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
"""

# Minimal subset of corlinman-vector kb.sqlite — only what memory_op reads.
KB_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    diary_name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    mtime INTEGER NOT NULL,
    size INTEGER NOT NULL,
    updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    vector BLOB,
    namespace TEXT NOT NULL DEFAULT 'general',
    decay_score REAL NOT NULL DEFAULT 1.0,
    consolidated_at INTEGER,
    last_recalled_at INTEGER,
    FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
);
"""


def _init_db(path: Path, schema: str) -> None:
    """Create + initialise an SQLite file with ``schema``."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(schema)
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def evolution_db(tmp_path: Path) -> Iterator[Path]:
    """Path to a freshly-initialised ``evolution.sqlite`` file."""
    p = tmp_path / "evolution.sqlite"
    _init_db(p, EVOLUTION_SCHEMA_SQL)
    yield p


@pytest.fixture
def kb_db(tmp_path: Path) -> Iterator[Path]:
    """Path to a freshly-initialised ``kb.sqlite`` file."""
    p = tmp_path / "kb.sqlite"
    _init_db(p, KB_SCHEMA_SQL)
    yield p


def insert_signal(
    db_path: Path,
    *,
    event_kind: str,
    target: str | None,
    severity: str = "warn",
    payload_json: str = "{}",
    trace_id: str | None = None,
    session_id: str | None = None,
    observed_at: int = 0,
) -> int:
    """Synchronously insert one ``evolution_signals`` row. Returns its id."""
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            """INSERT INTO evolution_signals
                 (event_kind, target, severity, payload_json,
                  trace_id, session_id, observed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (event_kind, target, severity, payload_json, trace_id, session_id, observed_at),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def insert_chunk(
    db_path: Path,
    *,
    content: str,
    namespace: str = "general",
    file_id: int = 1,
    chunk_index: int = 0,
) -> int:
    """Insert one ``chunks`` row (auto-creates a placeholder ``files`` row).

    Returns the inserted chunk id.
    """
    conn = sqlite3.connect(db_path)
    try:
        # Make sure there's a file row for the FK (chunks.file_id is NOT NULL
        # and has a foreign key to files; SQLite doesn't enforce FKs by
        # default but the value still needs to exist semantically).
        conn.execute(
            """INSERT OR IGNORE INTO files (id, path, diary_name, checksum, mtime, size)
               VALUES (?, ?, 'test', 'cafef00d', 0, 0)""",
            (file_id, f"/tmp/file_{file_id}.md"),
        )
        cur = conn.execute(
            """INSERT INTO chunks (file_id, chunk_index, content, namespace)
               VALUES (?, ?, ?, ?)""",
            (file_id, chunk_index, content, namespace),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()
