"""Session input reader for the Memory Curator.

Reads raw session material from sessions.sqlite and distilled
episodes from episodes.sqlite, producing unified SessionBundle
instances ready for the candidate-extraction phase.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from corlinman_agent_brain.models import BundleMessage, SessionBundle

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(sk-[a-zA-Z0-9]{20,})"),
    re.compile(r"(ghp_[a-zA-Z0-9]{36,})"),
    re.compile(r"(xox[bprs]-[a-zA-Z0-9-]+)"),
    re.compile(r"(AKIA[0-9A-Z]{16})"),
    re.compile(r"(Bearer\s+[a-zA-Z0-9._-]{20,})"),
    re.compile(r"(://[^:]+:)[^@]+(@)"),
]

_SECRET_REPLACEMENT = "[REDACTED]"
MAX_MESSAGE_CONTENT_LEN = 12_000
DEFAULT_TENANT_ID = "default"


# ---------------------------------------------------------------------------
# Read-only connection helper
# ---------------------------------------------------------------------------


@contextmanager
def _ro_connect(path: Path) -> Iterator[sqlite3.Connection]:
    """Open path read-only; yield in-memory stub if file missing."""
    if not path.exists():
        conn = sqlite3.connect(":memory:")
        try:
            yield conn
        finally:
            conn.close()
        return
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        yield conn
    finally:
        conn.close()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    found = cur.fetchone() is not None
    cur.close()
    return found


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------


def _ts_to_ms(value: object) -> int:
    """Best-effort timestamp to unix-ms conversion."""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return 0
    try:
        return int(text)
    except ValueError:
        pass
    from datetime import datetime

    cleaned = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(cleaned)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# Sanitization
# ---------------------------------------------------------------------------


def sanitize_content(text: str) -> str:
    """Strip likely secrets and truncate oversized content."""
    if not text:
        return text
    for pat in _SECRET_PATTERNS:
        text = pat.sub(_SECRET_REPLACEMENT, text)
    if len(text) > MAX_MESSAGE_CONTENT_LEN:
        text = text[:MAX_MESSAGE_CONTENT_LEN] + "\n[...truncated]"
    return text


# ---------------------------------------------------------------------------
# Internal raw message (pre-bundle)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RawMessage:
    """Internal row from sessions.sqlite before bundling."""

    session_key: str
    seq: int
    role: str
    content: str
    ts_ms: int
    tenant_id: str = DEFAULT_TENANT_ID
    agent_id: str = ""
    tool_call_id: str | None = None
    tool_name: str | None = None


# ---------------------------------------------------------------------------
# Session readers
# ---------------------------------------------------------------------------


def read_session_by_id(
    *,
    sessions_db: Path,
    session_key: str,
    sanitize: bool = True,
) -> SessionBundle | None:
    """Read a single session by its key, returning a SessionBundle.

    Returns None if the session does not exist or has zero messages.
    """
    messages = _fetch_messages(
        sessions_db=sessions_db,
        where_clause="session_key = ?",
        params=(session_key,),
    )
    if not messages:
        return None
    return _messages_to_bundle(
        messages=messages,
        session_key=session_key,
        sanitize=sanitize,
    )


def read_sessions_by_range(
    *,
    sessions_db: Path,
    tenant_id: str = DEFAULT_TENANT_ID,
    agent_id: str | None = None,
    window_start_ms: int | None = None,
    window_end_ms: int | None = None,
    sanitize: bool = True,
) -> list[SessionBundle]:
    """Read all sessions matching the filter criteria.

    Filters:
    - tenant_id: required (defaults to "default").
    - agent_id: optional; if set, only sessions tagged with this agent.
    - window_start_ms / window_end_ms: half-open time range [start, end).

    Returns one SessionBundle per distinct session_key, sorted by
    earliest message timestamp ascending.
    """
    all_messages = _fetch_messages_ranged(
        sessions_db=sessions_db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        window_start_ms=window_start_ms,
        window_end_ms=window_end_ms,
    )
    if not all_messages:
        return []

    groups: dict[str, list[_RawMessage]] = {}
    for msg in all_messages:
        groups.setdefault(msg.session_key, []).append(msg)

    bundles: list[SessionBundle] = []
    for skey, msgs in groups.items():
        bundle = _messages_to_bundle(
            messages=msgs,
            session_key=skey,
            sanitize=sanitize,
        )
        if bundle is not None:
            bundles.append(bundle)

    bundles.sort(key=lambda b: b.started_at_ms)
    return bundles


def read_episodes_as_context(
    *,
    episodes_db: Path,
    tenant_id: str = DEFAULT_TENANT_ID,
    window_start_ms: int | None = None,
    window_end_ms: int | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Read already-distilled episodes as supplementary context.

    Returns lightweight dicts containing:
    - id, kind, summary_text, started_at, ended_at, importance_score
    """
    out: list[dict[str, Any]] = []
    with _ro_connect(episodes_db) as conn:
        if not _table_exists(conn, "episodes"):
            return out
        where_parts = ["tenant_id = ?"]
        params: list[Any] = [tenant_id]
        if window_start_ms is not None:
            where_parts.append("ended_at >= ?")
            params.append(int(window_start_ms))
        if window_end_ms is not None:
            where_parts.append("started_at < ?")
            params.append(int(window_end_ms))
        where_sql = " AND ".join(where_parts)
        cur = conn.execute(
            f"""SELECT id, kind, summary_text, started_at, ended_at,
                       importance_score
                FROM episodes
                WHERE {where_sql}
                ORDER BY importance_score DESC, ended_at DESC
                LIMIT ?""",
            (*params, int(limit)),
        )
        for row in cur.fetchall():
            out.append(
                {
                    "id": str(row[0]),
                    "kind": str(row[1]),
                    "summary_text": str(row[2]),
                    "started_at": int(row[3]),
                    "ended_at": int(row[4]),
                    "importance_score": float(row[5]),
                }
            )
        cur.close()
    return out


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _fetch_messages(
    *,
    sessions_db: Path,
    where_clause: str,
    params: tuple[Any, ...],
) -> list[_RawMessage]:
    """Low-level message fetch with arbitrary WHERE clause."""
    out: list[_RawMessage] = []
    with _ro_connect(sessions_db) as conn:
        if not _table_exists(conn, "sessions"):
            return out
        cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        tenant_col = "tenant_id" if "tenant_id" in cols else f"'{DEFAULT_TENANT_ID}'"
        agent_col = "agent_id" if "agent_id" in cols else "''"
        tool_call_col = "tool_call_id" if "tool_call_id" in cols else "NULL"
        tool_name_col = "tool_name" if "tool_name" in cols else "NULL"
        cur = conn.execute(
            f"""SELECT session_key, seq, role, content, ts,
                     {tenant_col}, {agent_col},
                     {tool_call_col}, {tool_name_col}
                FROM sessions
                WHERE {where_clause}
                ORDER BY session_key ASC, seq ASC""",
            params,
        )
        for row in cur.fetchall():
            ts_ms = _ts_to_ms(row[4])
            out.append(
                _RawMessage(
                    session_key=str(row[0]),
                    seq=int(row[1]),
                    role=str(row[2]),
                    content=str(row[3]) if row[3] is not None else "",
                    ts_ms=ts_ms,
                    tenant_id=str(row[5]) if row[5] is not None else DEFAULT_TENANT_ID,
                    agent_id=str(row[6]) if row[6] is not None else "",
                    tool_call_id=str(row[7]) if row[7] is not None else None,
                    tool_name=str(row[8]) if row[8] is not None else None,
                )
            )
        cur.close()
    return out


def _fetch_messages_ranged(
    *,
    sessions_db: Path,
    tenant_id: str,
    agent_id: str | None,
    window_start_ms: int | None,
    window_end_ms: int | None,
) -> list[_RawMessage]:
    """Fetch messages with tenant/agent/time filtering.

    Because the ts column may be TEXT (RFC3339) we cannot always do
    server-side numeric filtering. Strategy: fetch all for the tenant
    and filter client-side on timestamps.
    """
    out: list[_RawMessage] = []
    with _ro_connect(sessions_db) as conn:
        if not _table_exists(conn, "sessions"):
            return out
        cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        has_tenant = "tenant_id" in cols
        has_agent = "agent_id" in cols
        tenant_col = "tenant_id" if has_tenant else f"'{DEFAULT_TENANT_ID}'"
        agent_col = "agent_id" if has_agent else "''"
        tool_call_col = "tool_call_id" if "tool_call_id" in cols else "NULL"
        tool_name_col = "tool_name" if "tool_name" in cols else "NULL"

        where_parts: list[str] = []
        params: list[Any] = []

        if has_tenant:
            where_parts.append("tenant_id = ?")
            params.append(tenant_id)
        if has_agent and agent_id is not None:
            where_parts.append("agent_id = ?")
            params.append(agent_id)

        where_sql = " AND ".join(where_parts) if where_parts else "1=1"

        cur = conn.execute(
            f"""SELECT session_key, seq, role, content, ts,
              {tenant_col}, {agent_col},
              {tool_call_col}, {tool_name_col}
                FROM sessions
                WHERE {where_sql}
                ORDER BY session_key ASC, seq ASC""",
            tuple(params),
        )
        for row in cur.fetchall():
            ts_ms = _ts_to_ms(row[4])
            if window_start_ms is not None and ts_ms < window_start_ms:
                continue
            if window_end_ms is not None and ts_ms >= window_end_ms:
                continue
            out.append(
                _RawMessage(
                    session_key=str(row[0]),
                    seq=int(row[1]),
                    role=str(row[2]),
                    content=str(row[3]) if row[3] is not None else "",
                    ts_ms=ts_ms,
                    tenant_id=str(row[5]) if row[5] is not None else DEFAULT_TENANT_ID,
                    agent_id=str(row[6]) if row[6] is not None else "",
                    tool_call_id=str(row[7]) if row[7] is not None else None,
                    tool_name=str(row[8]) if row[8] is not None else None,
                )
            )
        cur.close()
    return out


def _messages_to_bundle(
    *,
    messages: list[_RawMessage],
    session_key: str,
    sanitize: bool = True,
) -> SessionBundle | None:
    """Convert raw messages into a SessionBundle.

    Handles:
    - Empty sessions (returns None).
    - Message ordering by seq.
    - Tool call extraction.
    - Content sanitization.
    """
    if not messages:
        return None

    messages = sorted(messages, key=lambda m: m.seq)

    bundle_messages: list[BundleMessage] = []

    for msg in messages:
        content = msg.content
        if sanitize:
            content = sanitize_content(content)

        tool_calls: list[dict] | None = None
        if msg.tool_call_id is not None:
            tool_calls = [
                {
                    "tool_call_id": msg.tool_call_id,
                    "tool_name": msg.tool_name or "unknown",
                }
            ]

        bundle_messages.append(
            BundleMessage(
                seq=msg.seq,
                role=msg.role,
                content=content,
                ts_ms=msg.ts_ms,
                tool_call_id=msg.tool_call_id,
                tool_calls=tool_calls,
            )
        )

    timestamps = [m.ts_ms for m in messages if m.ts_ms > 0]
    started_at = min(timestamps) if timestamps else 0
    ended_at = max(timestamps) if timestamps else 0
    tenant_id = next((m.tenant_id for m in messages if m.tenant_id), DEFAULT_TENANT_ID)
    agent_id = next((m.agent_id for m in messages if m.agent_id), "")

    return SessionBundle(
        session_id=session_key,
        tenant_id=tenant_id,
        user_id="",
        agent_id=agent_id,
        messages=bundle_messages,
        started_at_ms=started_at,
        ended_at_ms=ended_at,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "DEFAULT_TENANT_ID",
    "MAX_MESSAGE_CONTENT_LEN",
    "read_episodes_as_context",
    "read_session_by_id",
    "read_sessions_by_range",
    "sanitize_content",
]

