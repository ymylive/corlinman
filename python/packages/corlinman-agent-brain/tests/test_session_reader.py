"""Tests for corlinman_agent_brain.session_reader module.

Covers:
- sanitize_content: secret redaction, truncation, empty input
- _ts_to_ms: int, float, ISO string, None, empty string
- read_session_by_id: happy path, missing session, sanitization toggle
- read_sessions_by_range: time filtering, agent filtering, ordering
- read_episodes_as_context: happy path, missing table, limit, time window
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from corlinman_agent_brain.session_reader import (
    MAX_MESSAGE_CONTENT_LEN,
    _ts_to_ms,
    read_episodes_as_context,
    read_session_by_id,
    read_sessions_by_range,
    sanitize_content,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sessions_db(tmp_path: Path) -> Path:
    """Create a sessions.sqlite with the standard schema and sample data."""
    db_path = tmp_path / "sessions.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE sessions ("
        "session_key TEXT NOT NULL, seq INTEGER NOT NULL, role TEXT NOT NULL, "
        "content TEXT, ts INTEGER, tenant_id TEXT DEFAULT 'default', "
        "agent_id TEXT DEFAULT '', tool_call_id TEXT, tool_name TEXT)"
    )
    # Session A: 3 messages
    conn.executemany(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("sess-A", 1, "user", "Hello, can you help me set up pytest?",
             1000, "default", "agent-1", None, None),
            ("sess-A", 2, "assistant", "Sure! Let me help you configure pytest.",
             2000, "default", "agent-1", None, None),
            ("sess-A", 3, "user", "I want to use fixtures and parametrize.",
             3000, "default", "agent-1", None, None),
        ],
    )
    # Session B: 2 messages (different agent, later time)
    conn.executemany(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("sess-B", 1, "user", "Deploy the app to production please.",
             5000, "default", "agent-2", None, None),
            ("sess-B", 2, "assistant", "Deploying now.",
             6000, "default", "agent-2", None, None),
        ],
    )
    # Session C: with tool calls
    conn.executemany(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("sess-C", 1, "user", "Run the linter on the project files.",
             7000, "default", "agent-1", None, None),
            ("sess-C", 2, "assistant", "Running linter...",
             7500, "default", "agent-1", "tc-001", "run_lint"),
            ("sess-C", 3, "tool", "Lint passed with 0 errors.",
             8000, "default", "agent-1", "tc-001", "run_lint"),
            ("sess-C", 4, "assistant", "Linting complete, no errors found.",
             8500, "default", "agent-1", None, None),
        ],
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def episodes_db(tmp_path: Path) -> Path:
    """Create an episodes.sqlite with sample episode data."""
    db_path = tmp_path / "episodes.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE episodes ("
        "id TEXT NOT NULL, kind TEXT NOT NULL, summary_text TEXT, "
        "started_at INTEGER, ended_at INTEGER, importance_score REAL, "
        "tenant_id TEXT DEFAULT 'default')"
    )
    conn.executemany(
        "INSERT INTO episodes VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("ep-1", "coding", "Set up pytest framework", 1000, 4000, 0.9, "default"),
            ("ep-2", "deploy", "Production deployment", 5000, 7000, 0.7, "default"),
            ("ep-3", "review", "Code review session", 8000, 9000, 0.5, "default"),
            ("ep-4", "coding", "Other tenant work", 1000, 2000, 0.8, "other-tenant"),
        ],
    )
    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# Tests: sanitize_content
# ---------------------------------------------------------------------------


class TestSanitizeContent:
    def test_empty_string_unchanged(self) -> None:
        assert sanitize_content("") == ""

    def test_normal_text_unchanged(self) -> None:
        text = "Use pytest for testing Python code."
        assert sanitize_content(text) == text

    def test_redacts_sk_key(self) -> None:
        text = "My key is sk-abcdefghijklmnopqrstuvwxyz1234"
        result = sanitize_content(text)
        assert "sk-abcdefghijklmnopqrstuvwxyz1234" not in result
        assert "[REDACTED]" in result

    def test_redacts_github_pat(self) -> None:
        text = "Token: ghp_abcdefghijklmnopqrstuvwxyz1234567890"
        result = sanitize_content(text)
        assert "ghp_" not in result
        assert "[REDACTED]" in result

    def test_redacts_slack_token(self) -> None:
        text = "Slack: xoxb-123456789-abcdefgh"
        result = sanitize_content(text)
        assert "xoxb-" not in result
        assert "[REDACTED]" in result

    def test_redacts_aws_key(self) -> None:
        text = "AWS: AKIAIOSFODNN7EXAMPLE"
        result = sanitize_content(text)
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert "[REDACTED]" in result

    def test_redacts_bearer_token(self) -> None:
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload"
        result = sanitize_content(text)
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in result
        assert "[REDACTED]" in result

    def test_redacts_url_credentials(self) -> None:
        text = "postgres://admin:secretpass@db.host.com/mydb"
        result = sanitize_content(text)
        assert "secretpass" not in result
        assert "[REDACTED]" in result

    def test_truncates_long_content(self) -> None:
        text = "x" * (MAX_MESSAGE_CONTENT_LEN + 500)
        result = sanitize_content(text)
        assert len(result) < len(text)
        assert result.endswith("[...truncated]")

    def test_content_at_max_length_not_truncated(self) -> None:
        text = "a" * MAX_MESSAGE_CONTENT_LEN
        result = sanitize_content(text)
        assert result == text
        assert "[...truncated]" not in result

    def test_multiple_secrets_all_redacted(self) -> None:
        text = "key1=sk-aaaabbbbccccddddeeeefffff key2=AKIAIOSFODNN7EXAMPLE"
        result = sanitize_content(text)
        assert "sk-aaaa" not in result
        assert "AKIAIOSFODNN7EXAMPLE" not in result


# ---------------------------------------------------------------------------
# Tests: _ts_to_ms
# ---------------------------------------------------------------------------


class TestTsToMs:
    def test_none_returns_zero(self) -> None:
        assert _ts_to_ms(None) == 0

    def test_int_passthrough(self) -> None:
        assert _ts_to_ms(1700000000000) == 1700000000000

    def test_float_truncated(self) -> None:
        assert _ts_to_ms(1700000000000.5) == 1700000000000

    def test_string_int(self) -> None:
        assert _ts_to_ms("1700000000000") == 1700000000000

    def test_iso_string_utc(self) -> None:
        # 2023-11-14T22:13:20Z -> 1700000000 seconds -> 1700000000000 ms
        result = _ts_to_ms("2023-11-14T22:13:20Z")
        assert result == 1700000000000

    def test_iso_string_with_offset(self) -> None:
        result = _ts_to_ms("2023-11-14T22:13:20+00:00")
        assert result == 1700000000000

    def test_empty_string_returns_zero(self) -> None:
        assert _ts_to_ms("") == 0

    def test_garbage_string_returns_zero(self) -> None:
        assert _ts_to_ms("not-a-timestamp") == 0

    def test_zero_int(self) -> None:
        assert _ts_to_ms(0) == 0


# ---------------------------------------------------------------------------
# Tests: read_session_by_id
# ---------------------------------------------------------------------------


class TestReadSessionById:
    def test_reads_existing_session(self, sessions_db: Path) -> None:
        bundle = read_session_by_id(sessions_db=sessions_db, session_key="sess-A")
        assert bundle is not None
        assert bundle.session_id == "sess-A"
        assert len(bundle.messages) == 3
        assert bundle.started_at_ms == 1000
        assert bundle.ended_at_ms == 3000

    def test_returns_none_for_missing_session(self, sessions_db: Path) -> None:
        bundle = read_session_by_id(sessions_db=sessions_db, session_key="nonexistent")
        assert bundle is None

    def test_messages_ordered_by_seq(self, sessions_db: Path) -> None:
        bundle = read_session_by_id(sessions_db=sessions_db, session_key="sess-A")
        assert bundle is not None
        seqs = [m.seq for m in bundle.messages]
        assert seqs == sorted(seqs)

    def test_sanitize_true_redacts_secrets(self, tmp_path: Path) -> None:
        db_path = tmp_path / "secret_sessions.sqlite"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE sessions ("
            "session_key TEXT, seq INTEGER, role TEXT, content TEXT, "
            "ts INTEGER, tenant_id TEXT, agent_id TEXT, "
            "tool_call_id TEXT, tool_name TEXT)"
        )
        conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("sess-secret", 1, "user",
             "My key is sk-abcdefghijklmnopqrstuvwxyz1234",
             1000, "default", "", None, None),
        )
        conn.commit()
        conn.close()

        bundle = read_session_by_id(
            sessions_db=db_path, session_key="sess-secret", sanitize=True
        )
        assert bundle is not None
        assert "sk-abcdefghijklmnopqrstuvwxyz1234" not in bundle.messages[0].content
        assert "[REDACTED]" in bundle.messages[0].content

    def test_sanitize_false_preserves_secrets(self, tmp_path: Path) -> None:
        db_path = tmp_path / "secret_sessions2.sqlite"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE sessions ("
            "session_key TEXT, seq INTEGER, role TEXT, content TEXT, "
            "ts INTEGER, tenant_id TEXT, agent_id TEXT, "
            "tool_call_id TEXT, tool_name TEXT)"
        )
        conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("sess-secret", 1, "user",
             "My key is sk-abcdefghijklmnopqrstuvwxyz1234",
             1000, "default", "", None, None),
        )
        conn.commit()
        conn.close()

        bundle = read_session_by_id(
            sessions_db=db_path, session_key="sess-secret", sanitize=False
        )
        assert bundle is not None
        assert "sk-abcdefghijklmnopqrstuvwxyz1234" in bundle.messages[0].content

    def test_tool_calls_populated(self, sessions_db: Path) -> None:
        bundle = read_session_by_id(sessions_db=sessions_db, session_key="sess-C")
        assert bundle is not None
        # Message at seq=2 has a tool_call_id
        msg_with_tool = next(m for m in bundle.messages if m.seq == 2)
        assert msg_with_tool.tool_call_id == "tc-001"
        assert msg_with_tool.tool_calls is not None
        assert msg_with_tool.tool_calls[0]["tool_name"] == "run_lint"

    def test_missing_db_file_returns_none(self, tmp_path: Path) -> None:
        missing_path = tmp_path / "nonexistent.sqlite"
        bundle = read_session_by_id(sessions_db=missing_path, session_key="sess-A")
        assert bundle is None

    def test_db_without_sessions_table_returns_none(self, tmp_path: Path) -> None:
        db_path = tmp_path / "empty.sqlite"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE other_table (id TEXT)")
        conn.commit()
        conn.close()
        bundle = read_session_by_id(sessions_db=db_path, session_key="sess-A")
        assert bundle is None


# ---------------------------------------------------------------------------
# Tests: read_sessions_by_range
# ---------------------------------------------------------------------------


class TestReadSessionsByRange:
    def test_reads_all_sessions_for_tenant(self, sessions_db: Path) -> None:
        bundles = read_sessions_by_range(sessions_db=sessions_db, tenant_id="default")
        assert len(bundles) == 3  # sess-A, sess-B, sess-C

    def test_filters_by_agent_id(self, sessions_db: Path) -> None:
        bundles = read_sessions_by_range(
            sessions_db=sessions_db, tenant_id="default", agent_id="agent-1"
        )
        session_ids = {b.session_id for b in bundles}
        assert "sess-A" in session_ids
        assert "sess-C" in session_ids
        assert "sess-B" not in session_ids

    def test_filters_by_time_window(self, sessions_db: Path) -> None:
        # Only messages with ts in [4000, 7000)
        bundles = read_sessions_by_range(
            sessions_db=sessions_db,
            tenant_id="default",
            window_start_ms=4000,
            window_end_ms=7000,
        )
        session_ids = {b.session_id for b in bundles}
        assert "sess-B" in session_ids
        # sess-A messages are all < 4000, sess-C messages are >= 7000
        assert "sess-A" not in session_ids

    def test_sorted_by_started_at_ms(self, sessions_db: Path) -> None:
        bundles = read_sessions_by_range(sessions_db=sessions_db, tenant_id="default")
        started_times = [b.started_at_ms for b in bundles]
        assert started_times == sorted(started_times)

    def test_empty_result_for_nonexistent_tenant(self, sessions_db: Path) -> None:
        bundles = read_sessions_by_range(
            sessions_db=sessions_db, tenant_id="no-such-tenant"
        )
        assert bundles == []

    def test_window_start_only(self, sessions_db: Path) -> None:
        bundles = read_sessions_by_range(
            sessions_db=sessions_db, tenant_id="default", window_start_ms=6000
        )
        # Only messages with ts >= 6000: sess-B(6000), sess-C(7000,7500,8000,8500)
        session_ids = {b.session_id for b in bundles}
        assert "sess-A" not in session_ids

    def test_window_end_only(self, sessions_db: Path) -> None:
        bundles = read_sessions_by_range(
            sessions_db=sessions_db, tenant_id="default", window_end_ms=4000
        )
        # Only messages with ts < 4000: sess-A(1000,2000,3000)
        session_ids = {b.session_id for b in bundles}
        assert "sess-A" in session_ids
        assert "sess-B" not in session_ids
        assert "sess-C" not in session_ids

    def test_missing_db_returns_empty(self, tmp_path: Path) -> None:
        missing_path = tmp_path / "nonexistent.sqlite"
        bundles = read_sessions_by_range(sessions_db=missing_path, tenant_id="default")
        assert bundles == []


# ---------------------------------------------------------------------------
# Tests: read_episodes_as_context
# ---------------------------------------------------------------------------


class TestReadEpisodesAsContext:
    def test_reads_episodes_for_tenant(self, episodes_db: Path) -> None:
        episodes = read_episodes_as_context(episodes_db=episodes_db, tenant_id="default")
        assert len(episodes) == 3
        # Should be ordered by importance_score DESC
        scores = [ep["importance_score"] for ep in episodes]
        assert scores == sorted(scores, reverse=True)

    def test_respects_limit(self, episodes_db: Path) -> None:
        episodes = read_episodes_as_context(
            episodes_db=episodes_db, tenant_id="default", limit=2
        )
        assert len(episodes) == 2

    def test_filters_by_time_window(self, episodes_db: Path) -> None:
        # ended_at >= 4000 AND started_at < 7000
        episodes = read_episodes_as_context(
            episodes_db=episodes_db,
            tenant_id="default",
            window_start_ms=4000,
            window_end_ms=7000,
        )
        ids = {ep["id"] for ep in episodes}
        # ep-1: ended_at=4000 >= 4000, started_at=1000 < 7000 -> included
        assert "ep-1" in ids
        # ep-2: ended_at=7000 >= 4000, started_at=5000 < 7000 -> included
        assert "ep-2" in ids
        # ep-3: ended_at=9000 >= 4000, started_at=8000 >= 7000 -> excluded
        assert "ep-3" not in ids

    def test_filters_by_tenant(self, episodes_db: Path) -> None:
        episodes = read_episodes_as_context(
            episodes_db=episodes_db, tenant_id="other-tenant"
        )
        assert len(episodes) == 1
        assert episodes[0]["id"] == "ep-4"

    def test_missing_db_returns_empty(self, tmp_path: Path) -> None:
        missing_path = tmp_path / "nonexistent.sqlite"
        episodes = read_episodes_as_context(
            episodes_db=missing_path, tenant_id="default"
        )
        assert episodes == []

    def test_missing_table_returns_empty(self, tmp_path: Path) -> None:
        db_path = tmp_path / "no_episodes.sqlite"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE other (id TEXT)")
        conn.commit()
        conn.close()
        episodes = read_episodes_as_context(
            episodes_db=db_path, tenant_id="default"
        )
        assert episodes == []

    def test_episode_dict_structure(self, episodes_db: Path) -> None:
        episodes = read_episodes_as_context(
            episodes_db=episodes_db, tenant_id="default", limit=1
        )
        ep = episodes[0]
        assert "id" in ep
        assert "kind" in ep
        assert "summary_text" in ep
        assert "started_at" in ep
        assert "ended_at" in ep
        assert "importance_score" in ep
        assert isinstance(ep["started_at"], int)
        assert isinstance(ep["importance_score"], float)
