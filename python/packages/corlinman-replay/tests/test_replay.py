"""Port of ``rust/crates/corlinman-replay/src/lib.rs#tests``.

Each test maps 1:1 to a ``#[tokio::test]`` in the Rust source.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from corlinman_replay import (
    ReplayMode,
    SessionRole,
    SqliteSessionStore,
    TenantId,
    list_sessions,
    replay,
    sessions_db_path,
)
from corlinman_replay.replay import SessionNotFoundError

from .conftest import make_message, seed


async def test_transcript_mode_returns_messages_in_seq_order(
    data_dir: Path, legacy_tenant: TenantId
) -> None:
    await seed(
        data_dir,
        legacy_tenant,
        [
            make_message(SessionRole.USER, "hello", offset_seconds=0),
            make_message(SessionRole.ASSISTANT, "hi there", offset_seconds=1),
            make_message(SessionRole.USER, "how are you", offset_seconds=2),
        ],
    )

    out = await replay(data_dir, legacy_tenant, "test-session", ReplayMode.TRANSCRIPT)

    assert out.session_key == "test-session"
    assert out.mode == "transcript"
    assert len(out.transcript) == 3
    assert out.transcript[0].role == "user"
    assert out.transcript[0].content == "hello"
    assert out.transcript[1].role == "assistant"
    assert out.transcript[2].content == "how are you"
    # RFC-3339 round-trip — must produce a parseable timestamp starting
    # with the seeded year.
    assert out.transcript[0].ts.startswith("2026-")
    assert out.summary.message_count == 3
    assert out.summary.tenant_id == "default"
    assert out.summary.rerun_diff is None


async def test_rerun_mode_emits_not_implemented_marker(
    data_dir: Path, legacy_tenant: TenantId
) -> None:
    await seed(data_dir, legacy_tenant, [make_message(SessionRole.USER, "ping")])

    out = await replay(data_dir, legacy_tenant, "test-session", ReplayMode.RERUN)

    assert out.mode == "rerun"
    assert len(out.transcript) == 1
    # v1 ships the wire shape with a placeholder; Wave 2.5 swaps in the
    # diff renderer.
    assert out.summary.rerun_diff == "not_implemented_yet"


async def test_missing_session_returns_session_not_found(
    data_dir: Path, legacy_tenant: TenantId
) -> None:
    path = sessions_db_path(data_dir, legacy_tenant)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Open the store so the SQLite file exists but no messages are seeded.
    store = await SqliteSessionStore.open(path)
    await store.close()

    with pytest.raises(SessionNotFoundError) as info:
        await replay(data_dir, legacy_tenant, "ghost-session", ReplayMode.TRANSCRIPT)
    assert info.value.key == "ghost-session"


async def test_list_sessions_groups_by_key_ordered_by_last_ts_desc(
    data_dir: Path, legacy_tenant: TenantId
) -> None:
    path = sessions_db_path(data_dir, legacy_tenant)
    path.parent.mkdir(parents=True, exist_ok=True)
    store = await SqliteSessionStore.open(path)
    try:
        # Older session: two messages.
        older = make_message(SessionRole.USER, "old-1", offset_seconds=0)
        older2 = make_message(SessionRole.ASSISTANT, "old-2", offset_seconds=1)
        # Force "older" timestamps by shifting back from the base.
        from datetime import datetime, timezone

        older.ts = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        older2.ts = datetime(2024, 1, 1, 0, 0, 1, tzinfo=timezone.utc)
        await store.append("session-old", older)
        await store.append("session-old", older2)

        # Newer session: one message.
        newer = make_message(SessionRole.USER, "new-1", offset_seconds=0)
        newer.ts = datetime(2030, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        await store.append("session-new", newer)
    finally:
        await store.close()

    rows = await list_sessions(data_dir, legacy_tenant)
    assert len(rows) == 2
    assert rows[0].session_key == "session-new", "newest first"
    assert rows[0].message_count == 1
    assert rows[1].session_key == "session-old"
    assert rows[1].message_count == 2
    # Unix-ms conversion sanity: 2030-01-01T00:00:00Z == 1893456000000 ms.
    assert rows[0].last_message_at == 1893456000000


async def test_list_sessions_empty_when_no_messages(
    data_dir: Path, legacy_tenant: TenantId
) -> None:
    path = sessions_db_path(data_dir, legacy_tenant)
    path.parent.mkdir(parents=True, exist_ok=True)
    store = await SqliteSessionStore.open(path)
    await store.close()

    rows = await list_sessions(data_dir, legacy_tenant)
    assert rows == []


async def test_non_default_tenant_routes_to_per_tenant_path(data_dir: Path) -> None:
    acme = TenantId.new("acme")
    path = sessions_db_path(data_dir, acme)
    # Per-tenant path resolution must place the file under
    # ``<root>/tenants/acme/sessions.sqlite``.
    assert "/tenants/acme/" in str(path).replace("\\", "/")
    path.parent.mkdir(parents=True, exist_ok=True)
    store = await SqliteSessionStore.open(path)
    try:
        await store.append("acme-session", make_message(SessionRole.USER, "moin"))
    finally:
        await store.close()

    out = await replay(data_dir, acme, "acme-session", ReplayMode.TRANSCRIPT)
    assert out.summary.tenant_id == "acme"
