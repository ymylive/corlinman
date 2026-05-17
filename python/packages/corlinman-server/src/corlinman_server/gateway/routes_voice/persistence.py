"""Voice session persistence.

Direct Python port of
``rust/crates/corlinman-gateway/src/routes/voice/persistence.rs``.
Holds the data shapes the WebSocket session driver writes when a
session opens / closes, plus a path-resolution helper for retained
audio.

This Python iter ships:

* The :class:`VoiceEndReason` closed-set enum (mirroring the Rust
  variants exactly, including the snake_case string forms persisted
  into ``voice_sessions.end_reason``).
* The :class:`VoiceSessionStart` / :class:`VoiceSessionEnd` /
  :class:`VoiceSessionRow` dataclasses.
* The :class:`VoiceSessionStore` Protocol + an in-memory implementation
  (:class:`MemoryVoiceSessionStore`) suitable for tests. The SQLite
  swap mirrors the Rust ``SqliteVoiceSessionStore`` and is left as a
  follow-up (the schema string :data:`VOICE_SCHEMA_SQL` is included so
  the iter-7+ SQLite adapter has a one-stop drop-in).
* :func:`audio_path_for` / :func:`tts_audio_path_for` for opt-in audio
  retention path resolution.
* :class:`VoiceTranscriptSink` Protocol + :class:`MemoryTranscriptSink`
  for the chat-session bridge that exposes voice turns to the agent
  loop.

Audio retention: default ``[voice] retain_audio = false`` means audio
is dropped at session end and ``voice_sessions.audio_path`` is NULL.
When ``retain_audio = true``, the gateway writes raw PCM-16 to
``<data_dir>/tenants/<tenant>/voice/<session_id>.pcm``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol, runtime_checkable


VOICE_SCHEMA_SQL: Final[str] = """
CREATE TABLE IF NOT EXISTS voice_sessions (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL DEFAULT 'default',
    session_key     TEXT NOT NULL,
    agent_id        TEXT,
    provider_alias  TEXT NOT NULL,
    started_at      INTEGER NOT NULL,
    ended_at        INTEGER,
    duration_secs   INTEGER,
    audio_path      TEXT,
    transcript_text TEXT,
    end_reason      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_voice_sessions_tenant_session
    ON voice_sessions(tenant_id, session_key, started_at);
"""
"""Schema applied on first open of the SQLite store. Idempotent via
``IF NOT EXISTS``. Mirrors the Rust ``VOICE_SCHEMA_SQL`` byte-for-byte
so the same physical sessions.sqlite file is interoperable across both
gateways."""


class VoiceEndReason(StrEnum):
    """Closed set of session-end reasons. The string value is what
    lands in the ``end_reason`` column; spelt out so a casual operator
    query like ``SELECT end_reason, COUNT(*) FROM voice_sessions GROUP
    BY end_reason`` shows readable buckets.
    """

    GRACEFUL = "graceful"
    BUDGET = "budget"
    MAX_SESSION = "max_session"
    PROVIDER_ERROR = "provider_error"
    CLIENT_DISCONNECT = "client_disconnect"
    START_FAILED = "start_failed"


@dataclass(frozen=True)
class VoiceSessionStart:
    """Insert-time payload — fields known when the session opens
    (before any audio flows). The row is updated in-place on session
    end with the duration / transcript / end_reason columns."""

    id: str
    tenant_id: str
    session_key: str
    agent_id: str | None
    provider_alias: str
    started_at: int  # Unix seconds


@dataclass(frozen=True)
class VoiceSessionEnd:
    """Update-time payload — fields known at session close."""

    id: str
    ended_at: int
    duration_secs: int
    audio_path: str | None
    transcript_text: str | None
    end_reason: VoiceEndReason


@dataclass(frozen=True)
class VoiceSessionRow:
    """Read shape — used by tests + (later) the admin UI's
    voice-session-history view. Keeps the column → field mapping in
    one place."""

    id: str
    tenant_id: str
    session_key: str
    agent_id: str | None
    provider_alias: str
    started_at: int
    ended_at: int | None
    duration_secs: int | None
    audio_path: str | None
    transcript_text: str | None
    end_reason: str


class VoiceStoreError(Exception):
    """Errors raised by :class:`VoiceSessionStore` implementations.

    The Rust enum splits these into ``Sql`` / ``RowMissing``; the
    Python side uses subclasses so callers can ``except RowMissing``.
    """

    __slots__ = ()


class VoiceStoreSqlError(VoiceStoreError):
    """Underlying SQL error."""


class VoiceStoreRowMissingError(VoiceStoreError):
    """Update target row not found — defends against
    double-finalisation."""

    __slots__ = ("row_id",)

    def __init__(self, row_id: str) -> None:
        super().__init__(f"voice store row missing: {row_id}")
        self.row_id = row_id


@runtime_checkable
class VoiceSessionStore(Protocol):
    """Trait surface so tests can drive a pure in-memory store and
    production uses the SQLite-backed adapter (TODO; see file
    docstring).
    """

    async def record_start(self, start: VoiceSessionStart) -> None: ...

    async def record_end(self, end: VoiceSessionEnd) -> None: ...

    async def fetch(self, id: str) -> VoiceSessionRow | None: ...

    async def list_for_session(
        self, tenant_id: str, session_key: str
    ) -> list[VoiceSessionRow]: ...


class MemoryVoiceSessionStore:
    """Pure in-memory :class:`VoiceSessionStore` for tests. Honours the
    same insert / update / fetch contract as the SQLite adapter."""

    def __init__(self) -> None:
        self._rows: dict[str, VoiceSessionRow] = {}
        self._lock = asyncio.Lock()

    async def record_start(self, start: VoiceSessionStart) -> None:
        async with self._lock:
            self._rows[start.id] = VoiceSessionRow(
                id=start.id,
                tenant_id=start.tenant_id,
                session_key=start.session_key,
                agent_id=start.agent_id,
                provider_alias=start.provider_alias,
                started_at=start.started_at,
                ended_at=None,
                duration_secs=None,
                audio_path=None,
                transcript_text=None,
                # Placeholder; overwritten by record_end. Using
                # "graceful" as the default so a row that's never
                # finalised (gateway crash) still has a valid
                # end_reason.
                end_reason=VoiceEndReason.GRACEFUL.value,
            )

    async def record_end(self, end: VoiceSessionEnd) -> None:
        async with self._lock:
            existing = self._rows.get(end.id)
            if existing is None:
                raise VoiceStoreRowMissingError(end.id)
            self._rows[end.id] = replace(
                existing,
                ended_at=end.ended_at,
                duration_secs=end.duration_secs,
                audio_path=end.audio_path,
                transcript_text=end.transcript_text,
                end_reason=end.end_reason.value,
            )

    async def fetch(self, id: str) -> VoiceSessionRow | None:
        async with self._lock:
            return self._rows.get(id)

    async def list_for_session(
        self, tenant_id: str, session_key: str
    ) -> list[VoiceSessionRow]:
        async with self._lock:
            matching = [
                row
                for row in self._rows.values()
                if row.tenant_id == tenant_id and row.session_key == session_key
            ]
        # Most-recent first to mirror the SQLite `ORDER BY started_at
        # DESC` semantics.
        matching.sort(key=lambda r: r.started_at, reverse=True)
        return matching


# ---------------------------------------------------------------------------
# Audio retention path helpers — pure, no I/O
# ---------------------------------------------------------------------------


def audio_path_for(data_dir: Path | str, tenant_id: str, session_id: str) -> Path:
    """Per-session inbound PCM-16 path under the per-tenant tree.

    Pure: returns a :class:`Path`. The caller (audio writer or
    retention sweeper) is responsible for ``mkdir`` and per-session
    file handles.
    """
    return Path(data_dir) / "tenants" / tenant_id / "voice" / f"{session_id}.pcm"


def tts_audio_path_for(
    data_dir: Path | str, tenant_id: str, session_id: str
) -> Path:
    """TTS sibling path for retained assistant audio. Lives next to
    the inbound PCM under the same per-tenant tree so the retention
    sweeper can match both with one glob.
    """
    return Path(data_dir) / "tenants" / tenant_id / "voice" / f"{session_id}.tts.pcm"


# ---------------------------------------------------------------------------
# Transcript bridge — voice turns → chat sessions table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TranscriptedTurn:
    """One voice turn the bridge has appended to a chat session.
    Surfaces what the in-memory sink captured so tests can assert
    ordering / content."""

    tenant_id: str
    session_key: str
    role: str
    text: str


@runtime_checkable
class VoiceTranscriptSink(Protocol):
    """Trait so a route-handler-side bridge (the real Python
    ``SessionStore`` adapter) can be wired without the voice route
    importing the agent loop. Tests use :class:`MemoryTranscriptSink`.
    """

    async def append_turn(
        self,
        tenant_id: str,
        session_key: str,
        role: str,
        text: str,
    ) -> None: ...


class MemoryTranscriptSink:
    """In-memory :class:`VoiceTranscriptSink` for tests + a default
    no-op deployment path while the production wiring lands.
    """

    def __init__(self) -> None:
        self._turns: list[TranscriptedTurn] = []
        self._lock = asyncio.Lock()

    async def append_turn(
        self,
        tenant_id: str,
        session_key: str,
        role: str,
        text: str,
    ) -> None:
        async with self._lock:
            self._turns.append(
                TranscriptedTurn(
                    tenant_id=tenant_id,
                    session_key=session_key,
                    role=role,
                    text=text,
                )
            )

    async def snapshot(self) -> list[TranscriptedTurn]:
        """Cloned snapshot of the appended turns. Cloned out so the
        caller doesn't hold a lock across awaits."""
        async with self._lock:
            return list(self._turns)


__all__ = [
    "VOICE_SCHEMA_SQL",
    "VoiceEndReason",
    "VoiceSessionStart",
    "VoiceSessionEnd",
    "VoiceSessionRow",
    "VoiceStoreError",
    "VoiceStoreRowMissingError",
    "VoiceStoreSqlError",
    "VoiceSessionStore",
    "MemoryVoiceSessionStore",
    "audio_path_for",
    "tts_audio_path_for",
    "TranscriptedTurn",
    "VoiceTranscriptSink",
    "MemoryTranscriptSink",
]
