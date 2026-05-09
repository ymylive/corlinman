"""Synchronous insert helpers for ``corlinman-episodes`` tests.

Importable as ``from tests._seed import insert_signal`` etc. — kept
out of ``conftest.py`` because pytest only auto-imports fixture
*decorated* names there; raw helpers need a real module path so test
files can ``from`` them directly under ``importlib`` import-mode.
"""

from __future__ import annotations

import json as _json
import sqlite3
from pathlib import Path


def insert_session_message(
    db: Path,
    *,
    session_key: str,
    seq: int,
    role: str = "user",
    content: str = "",
    ts_ms: int,
    tenant_id: str = "default",
) -> None:
    """Seed a single ``sessions`` row.

    ``ts_ms`` is converted to RFC3339 with millisecond precision so
    the collector's parsing path gets exercised — prod ``sessions.ts``
    uses the same format.
    """
    from datetime import UTC, datetime

    ts = (
        datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            """INSERT INTO sessions
                 (session_key, seq, role, content, ts, tenant_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (session_key, seq, role, content, ts, tenant_id),
        )
        conn.commit()
    finally:
        conn.close()


def insert_signal(
    db: Path,
    *,
    event_kind: str,
    target: str | None = None,
    severity: str = "warn",
    payload_json: str = "{}",
    session_id: str | None = None,
    observed_at_ms: int,
    tenant_id: str = "default",
) -> int:
    conn = sqlite3.connect(db)
    try:
        cur = conn.execute(
            """INSERT INTO evolution_signals
                 (event_kind, target, severity, payload_json,
                  session_id, observed_at, tenant_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                event_kind,
                target,
                severity,
                payload_json,
                session_id,
                observed_at_ms,
                tenant_id,
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def insert_proposal_with_history(
    db: Path,
    *,
    proposal_id: str,
    kind: str,
    target: str,
    signal_ids: list[int],
    applied_at_ms: int,
    rolled_back_at_ms: int | None = None,
    rollback_reason: str | None = None,
    tenant_id: str = "default",
) -> int:
    """Insert a proposal + matching history row in one shot.

    Returns the history id. Most tests only care that the join
    surfaces both columns, so the proposal fields are kept boring.
    """
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            """INSERT INTO evolution_proposals
                 (id, kind, target, diff, reasoning, risk, status,
                  signal_ids, trace_ids, created_at, applied_at, tenant_id)
               VALUES (?, ?, ?, '', '', 'medium', 'applied',
                       ?, '[]', ?, ?, ?)""",
            (
                proposal_id,
                kind,
                target,
                _json.dumps(signal_ids),
                applied_at_ms,
                applied_at_ms,
                tenant_id,
            ),
        )
        cur = conn.execute(
            """INSERT INTO evolution_history
                 (proposal_id, kind, target, before_sha, after_sha,
                  inverse_diff, metrics_baseline, applied_at,
                  rolled_back_at, rollback_reason)
               VALUES (?, ?, ?, '0', '0', '', '{}', ?, ?, ?)""",
            (
                proposal_id,
                kind,
                target,
                applied_at_ms,
                rolled_back_at_ms,
                rollback_reason,
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def insert_hook_event(
    db: Path,
    *,
    kind: str,
    payload_json: str = "{}",
    session_key: str | None = None,
    occurred_at_ms: int,
    tenant_id: str = "default",
) -> int:
    conn = sqlite3.connect(db)
    try:
        cur = conn.execute(
            """INSERT INTO hook_events
                 (kind, payload_json, session_key, tenant_id, occurred_at)
               VALUES (?, ?, ?, ?, ?)""",
            (kind, payload_json, session_key, tenant_id, occurred_at_ms),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def insert_identity_merge(
    db: Path,
    *,
    user_a: str,
    user_b: str,
    channel: str | None = None,
    consumed_at_ms: int,
    tenant_id: str = "default",
) -> int:
    conn = sqlite3.connect(db)
    try:
        cur = conn.execute(
            """INSERT INTO verification_phrases
                 (user_a, user_b, channel, tenant_id, consumed_at)
               VALUES (?, ?, ?, ?, ?)""",
            (user_a, user_b, channel, tenant_id, consumed_at_ms),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()
