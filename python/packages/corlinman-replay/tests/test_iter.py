"""Async-iterator API tests.

The Rust crate exposes only an eager ``Vec``-returning ``replay``. The
Python port adds :func:`iter_replay_messages` for callers that want to
stream the transcript out one row at a time (the brief explicitly
asks for an async-iterator surface).
"""

from __future__ import annotations

from pathlib import Path

from corlinman_replay import (
    ReplayMessage,
    SessionRole,
    TenantId,
    iter_replay_messages,
)

from .conftest import make_message, seed


async def test_iter_replay_messages_yields_in_seq_order(
    data_dir: Path, legacy_tenant: TenantId
) -> None:
    await seed(
        data_dir,
        legacy_tenant,
        [
            make_message(SessionRole.USER, "alpha", offset_seconds=0),
            make_message(SessionRole.ASSISTANT, "beta", offset_seconds=1),
            make_message(SessionRole.USER, "gamma", offset_seconds=2),
        ],
    )

    out: list[ReplayMessage] = []
    async for m in iter_replay_messages(data_dir, legacy_tenant, "test-session"):
        out.append(m)

    assert [m.content for m in out] == ["alpha", "beta", "gamma"]
    assert [m.role for m in out] == ["user", "assistant", "user"]
    # Wire-shape timestamp must be RFC-3339.
    assert out[0].ts.startswith("2026-")


async def test_iter_replay_messages_empty_when_session_missing(
    data_dir: Path, legacy_tenant: TenantId
) -> None:
    # No seed -- the store gets opened lazily so the empty case is just
    # an empty async iteration. Mirrors ``async for row in cursor`` over
    # an empty SQL result.
    from corlinman_replay import SqliteSessionStore, sessions_db_path

    path = sessions_db_path(data_dir, legacy_tenant)
    path.parent.mkdir(parents=True, exist_ok=True)
    s = await SqliteSessionStore.open(path)
    await s.close()

    out = [m async for m in iter_replay_messages(data_dir, legacy_tenant, "ghost")]
    assert out == []
