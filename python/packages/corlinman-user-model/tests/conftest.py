"""Shared test fixtures.

We mirror the Rust gateway's ``sessions`` schema here so the distiller
tests can read from a synthetic SQLite file. If the Rust schema drifts,
``test_distiller`` will fail loudly when the SELECT returns 0 rows.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

# Mirrors rust/crates/corlinman-core/src/session_sqlite.rs SCHEMA_SQL.
_SESSIONS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_key TEXT NOT NULL,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    tool_call_id TEXT,
    tool_calls_json TEXT,
    ts TEXT NOT NULL,
    PRIMARY KEY (session_key, seq)
);
CREATE INDEX IF NOT EXISTS idx_sessions_key ON sessions(session_key);
"""


@pytest.fixture
def sessions_db(tmp_path: Path) -> Iterator[Path]:
    """Path to a freshly-initialised ``sessions.sqlite`` file."""
    p = tmp_path / "sessions.sqlite"
    conn = sqlite3.connect(p)
    try:
        conn.executescript(_SESSIONS_SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()
    yield p


def insert_turn(
    db_path: Path,
    *,
    session_key: str,
    seq: int,
    role: str,
    content: str,
    ts: str = "2026-04-28T09:00:00Z",
) -> None:
    """Synchronously insert one ``sessions`` row."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """INSERT INTO sessions
                 (session_key, seq, role, content, tool_call_id, tool_calls_json, ts)
               VALUES (?, ?, ?, ?, NULL, NULL, ?)""",
            (session_key, seq, role, content, ts),
        )
        conn.commit()
    finally:
        conn.close()
