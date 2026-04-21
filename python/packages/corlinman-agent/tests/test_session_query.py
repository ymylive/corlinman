"""Unit tests for :mod:`corlinman_agent.session_query`.

We seed a throwaway SQLite file with the same schema the Rust session
store writes, then exercise :class:`SessionQueryClient`. No gateway is
involved — the Rust side is mocked via a fixture DB.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from corlinman_agent.session_query import (
    SessionMessage,
    SessionQueryClient,
    SessionQueryError,
)

# The DDL below must match `corlinman_core::session_sqlite::SCHEMA_SQL`
# byte-for-byte (sans the trailing newline) — the point of this fixture
# is to mirror the Rust store exactly so the read-only Python client
# sees exactly what the gateway writes.
_RUST_SCHEMA = """
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


def _rfc3339_z(dt: datetime) -> str:
    """Match what `time::OffsetDateTime::format(Rfc3339)` emits at offset 0."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


@pytest.fixture()
def seeded_sessions(tmp_path: Path) -> Path:
    """Build a sessions.sqlite with two sessions' worth of canned rows.

    Returns the path so the caller can hand it to :class:`SessionQueryClient`.
    """
    db_path = tmp_path / "sessions.sqlite"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_RUST_SCHEMA)
        base = datetime(2026, 4, 21, 9, 0, 0, tzinfo=UTC)
        rows = [
            ("s1", 0, "user", "hello", None, None, _rfc3339_z(base)),
            (
                "s1",
                1,
                "assistant",
                "hi there",
                None,
                None,
                _rfc3339_z(base + timedelta(seconds=10)),
            ),
            (
                "s1",
                2,
                "assistant",
                "",
                None,
                json.dumps(
                    [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": "{}"},
                        }
                    ]
                ),
                _rfc3339_z(base + timedelta(seconds=20)),
            ),
            (
                "s1",
                3,
                "tool",
                "{}",
                "call_1",
                None,
                _rfc3339_z(base + timedelta(seconds=30)),
            ),
            (
                "s1",
                4,
                "user",
                "thanks",
                None,
                None,
                _rfc3339_z(base + timedelta(seconds=40)),
            ),
            # Second session so isolation can be verified.
            ("s2", 0, "user", "other", None, None, _rfc3339_z(base)),
        ]
        conn.executemany(
            "INSERT INTO sessions(session_key, seq, role, content, tool_call_id, tool_calls_json, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def test_list_messages_returns_rows_in_seq_order(seeded_sessions: Path) -> None:
    client = SessionQueryClient(seeded_sessions)
    msgs = client.list_messages("s1")
    assert len(msgs) == 5
    assert [m.seq for m in msgs] == [0, 1, 2, 3, 4]
    assert [m.role for m in msgs] == [
        "user",
        "assistant",
        "assistant",
        "tool",
        "user",
    ]
    # Pydantic decoded the tool_calls JSON into a list of dicts.
    assert msgs[2].tool_calls == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "lookup", "arguments": "{}"},
        }
    ]
    assert msgs[3].tool_call_id == "call_1"


def test_list_messages_isolates_sessions(seeded_sessions: Path) -> None:
    client = SessionQueryClient(seeded_sessions)
    s1 = client.list_messages("s1")
    s2 = client.list_messages("s2")
    assert len(s1) == 5
    assert len(s2) == 1
    assert s2[0].content == "other"
    # Unknown session → empty list, not an error.
    assert client.list_messages("ghost") == []


def test_list_messages_limit_without_before_keeps_oldest(
    seeded_sessions: Path,
) -> None:
    client = SessionQueryClient(seeded_sessions)
    msgs = client.list_messages("s1", limit=2)
    assert [m.seq for m in msgs] == [0, 1]


def test_list_messages_before_cutoff_excludes_later_rows(
    seeded_sessions: Path,
) -> None:
    client = SessionQueryClient(seeded_sessions)
    base = datetime(2026, 4, 21, 9, 0, 0, tzinfo=UTC)
    cutoff = base + timedelta(seconds=20)  # strictly before seq=2.
    msgs = client.list_messages("s1", before=cutoff)
    assert [m.seq for m in msgs] == [0, 1]


def test_list_messages_limit_with_before_returns_newest_n(
    seeded_sessions: Path,
) -> None:
    client = SessionQueryClient(seeded_sessions)
    base = datetime(2026, 4, 21, 9, 0, 0, tzinfo=UTC)
    cutoff = base + timedelta(seconds=40)
    msgs = client.list_messages("s1", before=cutoff, limit=2)
    # Newest 2 rows strictly before t=40s → seq=2, seq=3; output is
    # re-ordered chronologically.
    assert [m.seq for m in msgs] == [2, 3]


def test_list_messages_after_cutoff_excludes_earlier_rows(
    seeded_sessions: Path,
) -> None:
    client = SessionQueryClient(seeded_sessions)
    base = datetime(2026, 4, 21, 9, 0, 0, tzinfo=UTC)
    after = base + timedelta(seconds=15)
    msgs = client.list_messages("s1", after=after)
    assert [m.seq for m in msgs] == [2, 3, 4]


def test_missing_db_raises_session_query_error(tmp_path: Path) -> None:
    client = SessionQueryClient(tmp_path / "does_not_exist.sqlite")
    with pytest.raises(SessionQueryError):
        client.list_messages("s1")


def test_negative_limit_rejected(seeded_sessions: Path) -> None:
    client = SessionQueryClient(seeded_sessions)
    with pytest.raises(ValueError):
        client.list_messages("s1", limit=-1)


def test_session_message_is_frozen(seeded_sessions: Path) -> None:
    # Pydantic `frozen=True` guard — prevents accidental mutation of
    # returned rows by S12 / S16 callers.
    client = SessionQueryClient(seeded_sessions)
    msgs = client.list_messages("s1", limit=1)
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        msgs[0].content = "mutated"  # type: ignore[misc]
    # Sanity check that SessionMessage is importable + round-trips via
    # `model_dump` for callers that want to serialise it.
    dumped = msgs[0].model_dump()
    assert dumped["session_key"] == "s1"
    SessionMessage.model_validate(dumped)
