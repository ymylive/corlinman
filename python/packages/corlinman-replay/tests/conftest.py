"""Shared fixtures for the corlinman-replay tests.

Mirrors the ``seed()`` helper in the Rust crate's test module: a fresh
``sessions.sqlite`` under a tempdir + tenant dir, returned together with
the tenant so callers can drive the replay against the same data dir
root.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from corlinman_replay import (
    SessionMessage,
    SessionRole,
    SqliteSessionStore,
    TenantId,
    sessions_db_path,
)


# Use the asyncio backend by default for every test in this package.
@pytest.fixture
def anyio_backend() -> str:  # pragma: no cover - fixture wiring
    return "asyncio"


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """Per-test data-dir root."""
    return tmp_path


@pytest.fixture
def legacy_tenant() -> TenantId:
    """Reserved single-tenant slug. Mirrors the Rust
    ``TenantId::legacy_default()`` shortcut."""
    return TenantId.legacy_default()


def make_message(role: SessionRole, content: str, *, offset_seconds: int = 0) -> SessionMessage:
    """Build a :class:`SessionMessage` with a fixed timestamp.

    Mirrors the Rust ``msg()`` helper: pin ``ts`` to a known
    far-future value (UNIX_EPOCH + 1_777_593_600s = 2026-04-30T...)
    so the RFC-3339 round-trip check in the transcript test stays
    stable across runs.
    """
    base = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)
    return SessionMessage(
        role=role,
        content=content,
        tool_call_id=None,
        tool_calls=None,
        ts=base + timedelta(seconds=offset_seconds),
    )


async def seed(
    data_dir: Path, tenant: TenantId, messages: list[SessionMessage]
) -> None:
    """Open the sessions DB under ``<data_dir>/tenants/<tenant>/`` and
    append each message in order under the key ``"test-session"``."""
    path = sessions_db_path(data_dir, tenant)
    path.parent.mkdir(parents=True, exist_ok=True)
    store = await SqliteSessionStore.open(path)
    try:
        for m in messages:
            await store.append("test-session", m)
    finally:
        await store.close()
