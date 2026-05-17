"""Wire-format primitives for the ``corlinman.voice.v1`` subprotocol.

Direct Python port of
``rust/crates/corlinman-gateway/src/routes/voice/framing.rs``. Pure
parsing helpers — no FastAPI, no asyncio, no I/O.

Wire layout (unchanged from Rust):

* **Subprotocol**: a single token, ``corlinman.voice.v1``. Anything
  else is a hard-fail (close code 1002).
* **Audio frames**: WebSocket binary messages carrying raw
  little-endian PCM-16. Each frame must be at most ~200 ms; framing
  itself is just byte concatenation, so the parser only checks that
  the length is a multiple of 2 (one PCM-16 sample = 2 bytes) and
  refuses pathological short / long frames.
* **Control frames**: WebSocket text messages carrying a JSON object
  with a discriminating ``type`` field. The set is fixed; unknown
  types are rejected so a misbehaving client can't smuggle future
  shapes through an old gateway.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import Any, ClassVar, Final, Union

# ---------------------------------------------------------------------------
# Subprotocol negotiation
# ---------------------------------------------------------------------------

SUBPROTOCOL: Final[str] = "corlinman.voice.v1"
"""Canonical subprotocol token."""

SUBPROTOCOLS: Final[tuple[str, ...]] = (SUBPROTOCOL,)
"""Subprotocol whitelist — exactly one entry today, but kept as a
constant tuple so a future ``corlinman.voice.v2`` (e.g. Opus payloads)
can be added without touching the upgrade handler."""

MAX_AUDIO_FRAME_BYTES: Final[int] = 8_192
"""Maximum binary audio frame size we accept from a client. 16 kHz ×
16-bit × 0.2 s = 6_400 bytes; pad to 8 KiB to absorb 24 kHz client
streams + header padding from any future encapsulation."""

MIN_AUDIO_FRAME_BYTES: Final[int] = 2
"""Smallest meaningful audio frame: one PCM-16 sample (2 bytes)."""


@dataclass(frozen=True)
class SubprotocolDecision:
    """Outcome of a subprotocol negotiation.

    Mirrors the Rust ``SubprotocolDecision`` enum:

    * ``accepted is not None`` means the client offered a supported
      token; the gateway replies with ``Sec-WebSocket-Protocol:
      <accepted>`` on the upgrade response.
    * ``reason is not None`` means rejection; the gateway maps to
      pre-upgrade HTTP 400 (or post-upgrade close code 1002) and
      includes ``reason`` in telemetry / close-frame reason text.

    Exactly one of the two fields is populated.
    """

    accepted: str | None
    reason: str | None

    @classmethod
    def accept(cls, token: str) -> SubprotocolDecision:
        return cls(accepted=token, reason=None)

    @classmethod
    def reject(cls, reason: str) -> SubprotocolDecision:
        return cls(accepted=None, reason=reason)

    @property
    def is_accept(self) -> bool:
        return self.accepted is not None


def accept_subprotocol(header: str | None) -> SubprotocolDecision:
    """Negotiate against the comma-separated value of the
    ``Sec-WebSocket-Protocol`` request header.

    ``None`` (header absent) is *rejected* — the design contract says a
    ``/voice`` upgrade without an explicit subprotocol is ambiguous and
    must be refused so future v2 clients aren't silently downgraded.

    Multiple protocols separated by ``,`` (the RFC 6455 shape) are
    scanned in order; the first match wins.
    """
    if header is None or not header.strip():
        return SubprotocolDecision.reject(
            "missing Sec-WebSocket-Protocol header; expected " + SUBPROTOCOL
        )
    for raw_token in header.split(","):
        token = raw_token.strip()
        if not token:
            continue
        if token in SUBPROTOCOLS:
            # Return the canonical reference rather than the
            # user-supplied slice so the upgrade response uses our
            # spelling (case & whitespace canonicalised).
            return SubprotocolDecision.accept(SUBPROTOCOL)
    return SubprotocolDecision.reject(
        f"no supported subprotocol in offered set; offered=[{header}], "
        f"expected=[{SUBPROTOCOL}]"
    )


# ---------------------------------------------------------------------------
# PCM-16 binary frame parsing
# ---------------------------------------------------------------------------


class AudioFrameError(Exception):
    """Reasons a binary frame is rejected. Mapped to a ``close`` frame
    on repeated offences; the design's "drop if > 100 frames/sec"
    defence piggybacks on this same path.

    The Rust enum is flattened here into a single exception subclass
    with a discriminating ``kind`` string plus context fields.
    """

    __slots__ = ("kind", "got", "minimum", "maximum")

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        got: int | None = None,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.got = got
        self.minimum = minimum
        self.maximum = maximum

    @classmethod
    def empty(cls) -> AudioFrameError:
        return cls("empty", "audio frame is empty")

    @classmethod
    def too_small(cls, got: int) -> AudioFrameError:
        return cls(
            "too_small",
            f"audio frame too small: got {got} bytes, minimum {MIN_AUDIO_FRAME_BYTES}",
            got=got,
            minimum=MIN_AUDIO_FRAME_BYTES,
        )

    @classmethod
    def too_large(cls, got: int) -> AudioFrameError:
        return cls(
            "too_large",
            f"audio frame too large: got {got} bytes, max {MAX_AUDIO_FRAME_BYTES}",
            got=got,
            maximum=MAX_AUDIO_FRAME_BYTES,
        )

    @classmethod
    def odd_length(cls, got: int) -> AudioFrameError:
        return cls(
            "odd_length",
            f"audio frame length must be even (PCM-16 = 2 bytes per sample); got {got}",
            got=got,
        )


@dataclass(frozen=True)
class AudioFrame:
    """What the framing layer understood from a binary frame.

    ``pcm_le_bytes`` — the raw little-endian PCM-16 bytes. The
    WebSocket session driver hands these to the provider adapter
    without per-sample copies.

    ``sample_count`` — number of ``int16`` samples = ``len(bytes) / 2``.
    """

    pcm_le_bytes: bytes
    sample_count: int

    def samples(self) -> tuple[int, ...]:
        """Decode the PCM-16 LE byte buffer into a tuple of ``int16``
        samples. Convenience for tests / callers that need numeric
        access; the hot path uses ``pcm_le_bytes`` directly.

        Uses :mod:`struct` so the decode honours the wire byte order
        regardless of host endianness.
        """
        return struct.unpack(f"<{self.sample_count}h", self.pcm_le_bytes)


def parse_audio_frame(payload: bytes | bytearray | memoryview) -> AudioFrame:
    """Validate a binary frame and return a frozen view of the PCM-16
    payload. Pure: no allocation beyond a single bytes copy at the
    boundary, no I/O.
    """
    n = len(payload)
    if n == 0:
        raise AudioFrameError.empty()
    if n < MIN_AUDIO_FRAME_BYTES:
        raise AudioFrameError.too_small(n)
    if n > MAX_AUDIO_FRAME_BYTES:
        raise AudioFrameError.too_large(n)
    if n % 2 != 0:
        raise AudioFrameError.odd_length(n)
    # Normalise to immutable bytes so downstream consumers can't mutate
    # the view out from under us. The Rust version borrows, but Python
    # bytes are already cheap to share.
    return AudioFrame(pcm_le_bytes=bytes(payload), sample_count=n // 2)


# ---------------------------------------------------------------------------
# Control frame JSON
# ---------------------------------------------------------------------------


def _default_sample_rate_in() -> int:
    return 16_000


def _default_format() -> str:
    return "pcm16"


@dataclass(frozen=True)
class ClientControl:
    """Client → server control frame.

    Tagged on the ``"type"`` field per the design's wire matrix. Adding
    a new variant is intentionally a breaking change — old gateways must
    reject new types until the upgrade handler explicitly maps them.

    The Rust enum is flattened here into a discriminated dataclass with
    a single ``type`` string and per-variant optional fields. The
    :meth:`from_obj` factory is the parser entry point.
    """

    type: str
    # Start
    session_key: str | None = None
    agent_id: str | None = None
    sample_rate_hz: int | None = None
    format: str | None = None
    # ApproveTool
    approval_id: str | None = None
    approve: bool | None = None

    # Variants. Mirror the Rust serde discriminants verbatim.
    # ClassVar annotations keep these as class-level constants instead of
    # dataclass fields (avoids "mutable default" error on Python 3.13).
    START: ClassVar[str] = "start"
    INTERRUPT: ClassVar[str] = "interrupt"
    APPROVE_TOOL: ClassVar[str] = "approve_tool"
    END: ClassVar[str] = "end"

    _KNOWN_TYPES: ClassVar[frozenset[str]] = frozenset(
        {"start", "interrupt", "approve_tool", "end"}
    )

    # Per-variant allowed JSON keys (beyond the discriminator "type").
    # Used to enforce serde's ``deny_unknown_fields`` semantics on the
    # struct variants.
    _ALLOWED_FIELDS: ClassVar[dict[str, frozenset[str]]] = {
        "start": frozenset({"type", "session_key", "agent_id", "sample_rate_hz", "format"}),
        "approve_tool": frozenset({"type", "approval_id", "approve"}),
        "interrupt": frozenset({"type"}),
        "end": frozenset({"type"}),
    }


class ControlParseError(Exception):
    """Parse error wrapper that doesn't leak the underlying JSON
    decoder internals into upstream telemetry. Mirrors the Rust
    ``ControlParseError`` shape (single ``message`` field)."""

    __slots__ = ("message",)

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def parse_client_control(text: str) -> ClientControl:
    """Parse a text control frame. Returns a :class:`ClientControl`
    dataclass or raises :class:`ControlParseError` with a
    human-readable message for telemetry."""
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ControlParseError(f"invalid control frame: {exc}") from exc

    if not isinstance(obj, dict):
        raise ControlParseError(
            "invalid control frame: top-level JSON must be an object"
        )

    ty = obj.get("type")
    if not isinstance(ty, str):
        raise ControlParseError(
            "invalid control frame: missing or non-string 'type' discriminator"
        )

    if ty not in ClientControl._KNOWN_TYPES:
        raise ControlParseError(
            f"invalid control frame: unknown type '{ty}' (expected one of "
            + ", ".join(sorted(ClientControl._KNOWN_TYPES))
            + ")"
        )

    allowed = ClientControl._ALLOWED_FIELDS[ty]
    extra = set(obj.keys()) - allowed
    if extra:
        raise ControlParseError(
            f"invalid control frame: unknown field(s) for type '{ty}': "
            + ", ".join(sorted(extra))
        )

    if ty == ClientControl.START:
        session_key = obj.get("session_key")
        if not isinstance(session_key, str) or not session_key:
            raise ControlParseError(
                "invalid control frame: 'start' requires non-empty string 'session_key'"
            )
        agent_id = obj.get("agent_id")
        if agent_id is not None and not isinstance(agent_id, str):
            raise ControlParseError(
                "invalid control frame: 'agent_id' must be a string when present"
            )
        sample_rate_hz = obj.get("sample_rate_hz", _default_sample_rate_in())
        if not isinstance(sample_rate_hz, int) or isinstance(sample_rate_hz, bool):
            raise ControlParseError(
                "invalid control frame: 'sample_rate_hz' must be an integer"
            )
        if sample_rate_hz <= 0:
            raise ControlParseError(
                "invalid control frame: 'sample_rate_hz' must be positive"
            )
        fmt = obj.get("format", _default_format())
        if not isinstance(fmt, str) or not fmt:
            raise ControlParseError(
                "invalid control frame: 'format' must be a non-empty string"
            )
        return ClientControl(
            type=ClientControl.START,
            session_key=session_key,
            agent_id=agent_id,
            sample_rate_hz=sample_rate_hz,
            format=fmt,
        )

    if ty == ClientControl.INTERRUPT:
        return ClientControl(type=ClientControl.INTERRUPT)

    if ty == ClientControl.APPROVE_TOOL:
        approval_id = obj.get("approval_id")
        if not isinstance(approval_id, str) or not approval_id:
            raise ControlParseError(
                "invalid control frame: 'approve_tool' requires non-empty string 'approval_id'"
            )
        approve = obj.get("approve", False)
        if not isinstance(approve, bool):
            raise ControlParseError(
                "invalid control frame: 'approve' must be a boolean"
            )
        return ClientControl(
            type=ClientControl.APPROVE_TOOL,
            approval_id=approval_id,
            approve=approve,
        )

    # ty == ClientControl.END
    return ClientControl(type=ClientControl.END)


@dataclass(frozen=True)
class ServerControl:
    """Server → client control frame.

    Mirrors the design matrix. The Rust enum is flattened here into a
    discriminated dataclass with a single ``type`` string and per-
    variant optional fields. :func:`encode_server_control` serialises
    one of these into the JSON shape the wire expects.
    """

    type: str
    # Started
    session_id: str | None = None
    provider: str | None = None
    # TranscriptPartial / TranscriptFinal
    role: str | None = None
    text: str | None = None
    # ToolApprovalRequired
    approval_id: str | None = None
    tool: str | None = None
    args: Any = None
    # BudgetWarning
    minutes_remaining: int | None = None
    # Error
    code: str | None = None
    message: str | None = None

    STARTED: Final[str] = "started"
    TRANSCRIPT_PARTIAL: Final[str] = "transcript_partial"
    TRANSCRIPT_FINAL: Final[str] = "transcript_final"
    AGENT_TEXT: Final[str] = "agent_text"
    TOOL_APPROVAL_REQUIRED: Final[str] = "tool_approval_required"
    BUDGET_WARNING: Final[str] = "budget_warning"
    ERROR: Final[str] = "error"


def encode_server_control(event: ServerControl) -> str:
    """Serialise a server-side control event for an outbound text
    frame. Infallible by construction (every variant's payload is
    JSON-safe); returning a string rather than ``str | None`` keeps the
    call sites that emit dozens of these per session noise-free.
    """
    ty = event.type
    if ty == ServerControl.STARTED:
        return json.dumps(
            {"type": ty, "session_id": event.session_id, "provider": event.provider}
        )
    if ty in (ServerControl.TRANSCRIPT_PARTIAL, ServerControl.TRANSCRIPT_FINAL):
        return json.dumps({"type": ty, "role": event.role, "text": event.text})
    if ty == ServerControl.AGENT_TEXT:
        return json.dumps({"type": ty, "text": event.text})
    if ty == ServerControl.TOOL_APPROVAL_REQUIRED:
        return json.dumps(
            {
                "type": ty,
                "approval_id": event.approval_id,
                "tool": event.tool,
                "args": event.args if event.args is not None else {},
            }
        )
    if ty == ServerControl.BUDGET_WARNING:
        return json.dumps(
            {"type": ty, "minutes_remaining": event.minutes_remaining}
        )
    if ty == ServerControl.ERROR:
        return json.dumps({"type": ty, "code": event.code, "message": event.message})
    raise ValueError(f"unknown ServerControl type '{ty}'")


# Public union type alias mirroring the Rust ``ClientControl`` /
# ``ServerControl`` enums for callers that want a typing hook.
ControlFrame = Union[ClientControl, ServerControl]


__all__ = [
    "SUBPROTOCOL",
    "SUBPROTOCOLS",
    "MAX_AUDIO_FRAME_BYTES",
    "MIN_AUDIO_FRAME_BYTES",
    "SubprotocolDecision",
    "accept_subprotocol",
    "AudioFrame",
    "AudioFrameError",
    "parse_audio_frame",
    "ClientControl",
    "ControlFrame",
    "ControlParseError",
    "ServerControl",
    "encode_server_control",
    "parse_client_control",
]
