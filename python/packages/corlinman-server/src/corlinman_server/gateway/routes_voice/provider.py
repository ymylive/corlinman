"""Voice provider adapter trait + mock implementation.

Direct Python port of
``rust/crates/corlinman-gateway/src/routes/voice/provider.rs``. Lifts
the upstream-WebSocket integration behind a pluggable Protocol so a
real OpenAI Realtime adapter and the mock used by tests both implement
the same shape. The WebSocket session driver in :mod:`.mod` never
knows which provider is on the other end of the channels — the
adapter is the single waist point.

Shape:

Each ``/voice`` session spawns one :class:`VoiceProviderSession` via
:meth:`VoiceProvider.open`. The session is a tri-channel object:

* ``audio_in`` — gateway pumps client PCM-16 frames in
* ``control_in`` — gateway forwards client control frames
  (``interrupt``, ``approve_tool``, ``end``) and gateway-side commands
  (e.g. ``Close`` mid-session)
* ``events`` — provider drains :class:`VoiceEvent` back; the gateway
  demultiplexes into binary TTS frames + JSON ``ServerControl``

The provider speaks in semantic events (``AudioOut``,
``TranscriptPartial``, ``ToolCall``, …) rather than provider-shaped
JSON.

The real OpenAI adapter (``provider_openai.rs``, ~29 KB) is not ported
in this iter — only the trait surface + a mock. The mock is sufficient
to drive the framing / cost / approval / persistence integration tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, runtime_checkable

DEFAULT_PROVIDER_CHANNEL_CAPACITY: Final[int] = 64
"""Default channel depth for the audio/event pumps. 64 frames at
~20 ms each = ~1.3 s of headroom — enough to absorb a brief stall
without dropping but small enough that a stuck consumer surfaces
quickly via backpressure."""


# ---------------------------------------------------------------------------
# ProviderCommand — gateway → provider
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderCommand:
    """Inbound from gateway → provider. Small tagged dataclass,
    deliberately not JSON-shaped: it never crosses a wire, only an
    asyncio queue.

    Discriminator is :attr:`kind`. Use the construction helpers
    :meth:`interrupt`, :meth:`approve_tool`, :meth:`close`.
    """

    kind: str
    approval_id: str | None = None
    approve: bool | None = None

    INTERRUPT: Final[str] = "interrupt"
    APPROVE_TOOL: Final[str] = "approve_tool"
    CLOSE: Final[str] = "close"

    @classmethod
    def interrupt(cls) -> ProviderCommand:
        return cls(kind=cls.INTERRUPT)

    @classmethod
    def approve_tool(cls, approval_id: str, *, approve: bool) -> ProviderCommand:
        return cls(kind=cls.APPROVE_TOOL, approval_id=approval_id, approve=approve)

    @classmethod
    def close(cls) -> ProviderCommand:
        return cls(kind=cls.CLOSE)


# ---------------------------------------------------------------------------
# VoiceEvent — provider → gateway
# ---------------------------------------------------------------------------


class ProviderEndReason:
    """Why the provider closed the session. The WebSocket session
    driver maps these to ``voice_sessions.end_reason``."""

    GRACEFUL: Final[str] = "graceful"
    """Session ended in response to a ``Close`` command."""

    PROVIDER_ERROR: Final[str] = "provider_error"
    """Provider terminated unexpectedly (network drop, upstream bug)."""

    START_FAILED: Final[str] = "start_failed"
    """Provider declined to start (auth, quota, etc.). Sent before any
    ``Ready``."""


@dataclass(frozen=True)
class VoiceEvent:
    """Provider → gateway events. Each variant maps to one or more
    outbound WebSocket frames in the route handler.

    Kept separate from the wire-shaped :class:`ServerControl` because
    the provider may emit events the wire never carries (e.g. raw
    ``usage`` deltas the gateway aggregates locally).

    Discriminator is :attr:`kind`. Construction helpers below mirror
    the Rust enum variants.
    """

    kind: str
    # Ready
    provider_session_id: str | None = None
    # AudioOut
    pcm_le_bytes: bytes | None = None
    # TranscriptPartial / TranscriptFinal
    role: str | None = None
    text: str | None = None
    # ToolCall
    call_id: str | None = None
    tool: str | None = None
    args: Any = None
    # Error
    code: str | None = None
    message: str | None = None
    # End
    end_reason: str | None = None

    READY: Final[str] = "ready"
    AUDIO_OUT: Final[str] = "audio_out"
    TRANSCRIPT_PARTIAL: Final[str] = "transcript_partial"
    TRANSCRIPT_FINAL: Final[str] = "transcript_final"
    AGENT_TEXT: Final[str] = "agent_text"
    TOOL_CALL: Final[str] = "tool_call"
    ERROR: Final[str] = "error"
    END: Final[str] = "end"

    @classmethod
    def ready(cls, provider_session_id: str) -> VoiceEvent:
        return cls(kind=cls.READY, provider_session_id=provider_session_id)

    @classmethod
    def audio_out(cls, pcm_le_bytes: bytes) -> VoiceEvent:
        return cls(kind=cls.AUDIO_OUT, pcm_le_bytes=pcm_le_bytes)

    @classmethod
    def transcript_partial(cls, role: str, text: str) -> VoiceEvent:
        return cls(kind=cls.TRANSCRIPT_PARTIAL, role=role, text=text)

    @classmethod
    def transcript_final(cls, role: str, text: str) -> VoiceEvent:
        return cls(kind=cls.TRANSCRIPT_FINAL, role=role, text=text)

    @classmethod
    def agent_text(cls, text: str) -> VoiceEvent:
        return cls(kind=cls.AGENT_TEXT, text=text)

    @classmethod
    def tool_call(cls, call_id: str, tool: str, args: Any) -> VoiceEvent:
        return cls(kind=cls.TOOL_CALL, call_id=call_id, tool=tool, args=args)

    @classmethod
    def error(cls, code: str, message: str) -> VoiceEvent:
        return cls(kind=cls.ERROR, code=code, message=message)

    @classmethod
    def end(cls, end_reason: str) -> VoiceEvent:
        return cls(kind=cls.END, end_reason=end_reason)


# ---------------------------------------------------------------------------
# VoiceSessionStartParams — handed to provider at open
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VoiceSessionStartParams:
    """Configuration handed to the adapter at session start. Pulled
    from ``[voice]`` config at the route handler so the Protocol stays
    decoupled from the config shape.
    """

    session_id: str
    provider_alias: str
    sample_rate_hz_in: int = 16_000
    sample_rate_hz_out: int = 24_000
    voice_id: str | None = None
    agent_id: str | None = None


# ---------------------------------------------------------------------------
# VoiceProvider / VoiceProviderSession Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class VoiceProviderSession(Protocol):
    """One open provider session. The WebSocket session driver:

    * Calls :meth:`push_audio` with each validated PCM-16 frame.
    * Calls :meth:`push_command` with each :class:`ProviderCommand`
      (translated from client control frames or gateway-side actions).
    * Iterates :meth:`events` as an async generator until the provider
      yields ``End``.
    * Calls :meth:`close` from the cleanup path.
    """

    async def push_audio(self, pcm_le_bytes: bytes) -> None: ...

    async def push_command(self, command: ProviderCommand) -> None: ...

    def events(self) -> AsyncIterator[VoiceEvent]: ...

    async def close(self) -> None: ...


@runtime_checkable
class VoiceProvider(Protocol):
    """Trait surface for a voice provider adapter. The WebSocket
    session driver only ever calls :meth:`open` — the returned session
    is the active conversation."""

    async def open(self, params: VoiceSessionStartParams) -> VoiceProviderSession: ...


# ---------------------------------------------------------------------------
# Mock provider — used by tests
# ---------------------------------------------------------------------------


class MockVoiceProviderSession:
    """Mock session that echoes back a single ``Ready`` event then any
    events the test queues via :meth:`emit`. Mirrors the Rust mock used
    by the gateway's voice tests.

    Tests drive it like::

        session = await provider.open(params)
        await session.emit(VoiceEvent.transcript_final(
            role="user", text="hello"
        ))
        await session.emit(VoiceEvent.end(ProviderEndReason.GRACEFUL))
        async for ev in session.events():
            ...
    """

    def __init__(self, *, ready_id: str = "mock-session") -> None:
        self._ready_id = ready_id
        self._events: asyncio.Queue[VoiceEvent | None] = asyncio.Queue()
        self._audio_in: list[bytes] = []
        self._commands_in: list[ProviderCommand] = []
        self._closed = False
        # Synthesise the initial Ready event so callers can `async
        # for` immediately.
        self._events.put_nowait(VoiceEvent.ready(provider_session_id=ready_id))

    async def push_audio(self, pcm_le_bytes: bytes) -> None:
        if self._closed:
            raise RuntimeError("MockVoiceProviderSession is closed")
        self._audio_in.append(pcm_le_bytes)

    async def push_command(self, command: ProviderCommand) -> None:
        if self._closed:
            raise RuntimeError("MockVoiceProviderSession is closed")
        self._commands_in.append(command)
        if command.kind == ProviderCommand.CLOSE:
            await self.emit(VoiceEvent.end(end_reason=ProviderEndReason.GRACEFUL))

    async def events(self) -> AsyncIterator[VoiceEvent]:
        while True:
            ev = await self._events.get()
            if ev is None:
                return
            yield ev
            if ev.kind == VoiceEvent.END:
                return

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Sentinel — unstick any awaiter on `events`.
        await self._events.put(None)

    # Test helpers --------------------------------------------------

    async def emit(self, event: VoiceEvent) -> None:
        """Enqueue a single provider → gateway event."""
        await self._events.put(event)

    @property
    def audio_in(self) -> list[bytes]:
        """Snapshot of audio frames the gateway pushed in, in order."""
        return list(self._audio_in)

    @property
    def commands_in(self) -> list[ProviderCommand]:
        """Snapshot of commands the gateway pushed in, in order."""
        return list(self._commands_in)


class MockVoiceProvider:
    """Mock :class:`VoiceProvider` that yields a fresh
    :class:`MockVoiceProviderSession` on every :meth:`open`."""

    def __init__(self) -> None:
        self.sessions: list[MockVoiceProviderSession] = []

    async def open(
        self, params: VoiceSessionStartParams
    ) -> MockVoiceProviderSession:
        session = MockVoiceProviderSession(ready_id=f"mock-{params.session_id}")
        self.sessions.append(session)
        return session


__all__ = [
    "DEFAULT_PROVIDER_CHANNEL_CAPACITY",
    "ProviderCommand",
    "ProviderEndReason",
    "VoiceEvent",
    "VoiceSessionStartParams",
    "VoiceProvider",
    "VoiceProviderSession",
    "MockVoiceProvider",
    "MockVoiceProviderSession",
]
