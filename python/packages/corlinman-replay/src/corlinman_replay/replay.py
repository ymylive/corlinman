"""Deterministic session replay primitive — Python port.

Loads a session by key from ``sessions.sqlite`` and reconstructs a
structured transcript ready for diff / dump. Direct 1:1 port of
``rust/crates/corlinman-replay/src/lib.rs``.

Modes
-----
* **Transcript** (default) -- read-only deterministic dump of the
  stored session messages, ordered by ``seq`` ASC. No agent execution.
  Idempotent: same ``(sessions.sqlite, session_key)`` always yields
  the same transcript.
* **Rerun** (Wave 2.5+, stub in v1) -- ships the wire shape with a
  ``not_implemented_yet`` marker so the UI can render the deferral.
  The actual diff renderer ships in Wave 2.5.

Tenant scoping
--------------
Callers pass a :class:`TenantId` and the primitive opens
``<data_dir>/tenants/<tenant>/sessions.sqlite``. Single-tenant
deployments pass :meth:`TenantId.legacy_default` and read from the
reserved-default path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from corlinman_replay.session_store import (
    CorlinmanError,
    SessionMessage,
    SessionSummary,
    SqliteSessionStore,
    _format_rfc3339,
)
from corlinman_replay.tenant import TenantId, tenant_db_path


class ReplayMode(str, Enum):
    """Replay execution mode. See module docs for the deferral note on
    :attr:`RERUN`."""

    TRANSCRIPT = "transcript"
    RERUN = "rerun"

    def as_str(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ReplayMessage:
    """One row in the replay transcript. Mirrors :class:`SessionMessage`
    but with the timestamp pinned to RFC-3339 (UI consumption) and the
    role serialised as a lowercase string to match the JSON wire shape."""

    role: str
    content: str
    ts: str
    """RFC-3339 / ISO-8601 string. Matches the ``tenants.created_at``
    and ``evolution_history.applied_at`` formatting conventions used
    elsewhere in the admin surface."""


@dataclass(slots=True)
class ReplaySummary:
    """Summary block in the replay output. Carries metadata the UI
    needs to render headers without re-querying."""

    message_count: int
    tenant_id: str
    rerun_diff: str | None = None
    """Wave 2.5 deferral marker. Set to ``"not_implemented_yet"`` in v1
    when :attr:`ReplayMode.RERUN` is requested. ``None`` for transcript
    mode and once rerun ships."""


@dataclass(slots=True)
class ReplayOutput:
    """Top-level replay output. Direct serde shape for the
    ``/admin/sessions/:key/replay`` HTTP route and the ``corlinman
    replay`` CLI's ``--output json`` mode."""

    session_key: str
    mode: str
    transcript: list[ReplayMessage] = field(default_factory=list)
    summary: ReplaySummary = field(
        default_factory=lambda: ReplaySummary(message_count=0, tenant_id="default")
    )


@dataclass(frozen=True, slots=True)
class SessionListRow:
    """One row in the admin sessions list. Mirrors the UI's
    ``SessionSummary`` interface in ``ui/lib/api/sessions.ts``. Distinct
    from :class:`SessionSummary` only in the field names — kept
    separate so the wire shape can evolve without touching the store."""

    session_key: str
    last_message_at: int
    """Unix milliseconds of the most-recent message in the session."""
    message_count: int

    @classmethod
    def from_summary(cls, s: SessionSummary) -> SessionListRow:
        return cls(
            session_key=s.session_key,
            last_message_at=s.last_message_at_ms,
            message_count=s.message_count,
        )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ReplayError(Exception):
    """Base class for errors raised by :func:`replay` / :func:`list_sessions`.

    The Rust ``ReplayError`` enum collapses to a class hierarchy here so
    callers can ``except StoreOpen`` / ``except SessionNotFound`` to
    pattern-match the variant.
    """


class StoreOpenError(ReplayError):
    """Raised when the session store cannot be opened (file missing,
    permission denied, malformed schema). Carries the attempted path."""

    def __init__(self, path: Path, source: BaseException) -> None:
        super().__init__(f"session store open failed at {path}: {source}")
        self.path = path
        self.__cause__ = source


class StoreLoadError(ReplayError):
    """Raised when the store fails mid-query. Carries the offending key
    (``"<list>"`` for :func:`list_sessions` failures)."""

    def __init__(self, key: str, source: BaseException) -> None:
        super().__init__(f"session store load failed for key {key!r}: {source}")
        self.key = key
        self.__cause__ = source


class SessionNotFoundError(ReplayError):
    """Raised when the session key has no stored messages.

    Distinguishes "session was pruned / never existed" from "session
    exists but is empty"; the latter case returns ``Ok`` with
    ``transcript=[]`` in the Rust crate. v1 treats both as 404 at the
    HTTP layer.
    """

    def __init__(self, key: str) -> None:
        super().__init__(f"session not found: {key!r}")
        self.key = key


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def sessions_db_path(data_dir: Path, tenant: TenantId) -> Path:
    """Resolve the per-tenant ``sessions.sqlite`` path under ``data_dir``
    using the same convention the gateway uses
    (``<data_dir>/tenants/<tenant>/sessions.sqlite``). When the tenant
    is ``"default"`` this collapses to the legacy single-tenant path
    segment."""
    return tenant_db_path(data_dir, tenant, "sessions")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def list_sessions(data_dir: Path, tenant: TenantId) -> list[SessionListRow]:
    """List all sessions stored under
    ``<data_dir>/tenants/<tenant>/sessions.sqlite``.

    Returns an empty list when the file exists but holds no sessions.
    :class:`StoreOpenError` propagates if the file cannot be opened
    (e.g. tenant dir missing — caller decides whether to treat as
    empty list or 503).
    """
    path = sessions_db_path(data_dir, tenant)
    try:
        store = await SqliteSessionStore.open(path)
    except CorlinmanError as exc:
        raise StoreOpenError(path, exc) from exc

    try:
        try:
            rows = await store.list_sessions()
        except CorlinmanError as exc:
            raise StoreLoadError("<list>", exc) from exc
    finally:
        await store.close()

    return [SessionListRow.from_summary(s) for s in rows]


async def replay(
    data_dir: Path,
    tenant: TenantId,
    session_key: str,
    mode: ReplayMode = ReplayMode.TRANSCRIPT,
) -> ReplayOutput:
    """Load a session and reconstruct the deterministic replay output.

    Raises :class:`SessionNotFoundError` when the key has no stored
    messages.
    """
    path = sessions_db_path(data_dir, tenant)
    try:
        store = await SqliteSessionStore.open(path)
    except CorlinmanError as exc:
        raise StoreOpenError(path, exc) from exc

    try:
        try:
            messages = await store.load(session_key)
        except CorlinmanError as exc:
            raise StoreLoadError(session_key, exc) from exc
    finally:
        await store.close()

    return replay_from_messages(tenant, session_key, mode, messages)


async def iter_replay_messages(
    data_dir: Path, tenant: TenantId, session_key: str
) -> AsyncIterator[ReplayMessage]:
    """Async-iterate the replay transcript without buffering the whole
    list in memory.

    Streams from SQLite one row at a time. Raises
    :class:`SessionNotFoundError` only if the *first* row is missing
    AND the caller wants the same semantics as :func:`replay`; for
    streaming consumers we instead yield nothing and let the caller
    decide — same shape as ``async for`` over an empty SQL result.
    """
    path = sessions_db_path(data_dir, tenant)
    try:
        store = await SqliteSessionStore.open(path)
    except CorlinmanError as exc:
        raise StoreOpenError(path, exc) from exc

    try:
        async for msg in store.iter_messages(session_key):
            yield _to_replay_message(msg)
    finally:
        await store.close()


def replay_from_messages(
    tenant: TenantId,
    session_key: str,
    mode: ReplayMode,
    messages: list[SessionMessage],
) -> ReplayOutput:
    """Build replay output from already-loaded session messages.

    Keeps alternate storage layouts (for example a legacy flat
    ``sessions.sqlite`` used by single-tenant deployments) on the same
    wire contract as the tenant-path replay primitive.
    """
    if len(messages) == 0:
        raise SessionNotFoundError(session_key)

    transcript = [_to_replay_message(m) for m in messages]

    summary = ReplaySummary(
        message_count=len(transcript),
        tenant_id=tenant.as_str(),
        rerun_diff=("not_implemented_yet" if mode == ReplayMode.RERUN else None),
    )

    return ReplayOutput(
        session_key=session_key,
        mode=mode.as_str(),
        transcript=transcript,
        summary=summary,
    )


def _to_replay_message(m: SessionMessage) -> ReplayMessage:
    """Convert a stored :class:`SessionMessage` into the wire-shape
    :class:`ReplayMessage`. Mirrors the closure in the Rust
    ``replay_from_messages`` map step."""
    return ReplayMessage(
        role=m.role.as_str(),
        content=m.content,
        ts=_format_rfc3339(m.ts),
    )


__all__ = [
    "ReplayError",
    "ReplayMessage",
    "ReplayMode",
    "ReplayOutput",
    "ReplaySummary",
    "SessionListRow",
    "SessionNotFoundError",
    "StoreLoadError",
    "StoreOpenError",
    "iter_replay_messages",
    "list_sessions",
    "replay",
    "replay_from_messages",
    "sessions_db_path",
]
