"""Value types + error taxonomy for ``corlinman-wstool``.

Python port of ``rust/crates/corlinman-wstool/src/error.rs`` and the
small config struct from ``server.rs``.

Two error tiers:

* :class:`ToolError` — what a :class:`~corlinman_wstool.client.ToolHandler`
  returns. Carries a stable ``code`` so callers can branch on it.
* :class:`WsToolError` — higher-level errors at the crate boundary
  (server-side ``invoke_once``, runner-side connect/serve loops). The
  Rust enum variants map onto subclasses here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "AcceptInfo",
    "AuthRejected",
    "Disconnected",
    "InternalError",
    "InvokeOutcome",
    "ProtocolError",
    "ResultErrorReply",
    "TimeoutError_",
    "ToolError",
    "ToolFailed",
    "Unsupported",
    "WsToolConfig",
    "WsToolError",
]


# ---------------------------------------------------------------------------
# Tool-handler return error.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolError(Exception):
    """Returned by :class:`ToolHandler.invoke`.

    Carries a stable ``code`` string so callers can branch on it without
    string-matching messages.
    """

    code: str
    message: str

    def __str__(self) -> str:  # pragma: no cover — trivial
        return f"tool error [{self.code}]: {self.message}"

    @classmethod
    def new(cls, code: str, message: str) -> ToolError:
        return cls(code=code, message=message)

    @classmethod
    def cancelled(cls) -> ToolError:
        """Handlers raise/return this when the gateway cancelled their call."""
        return cls(code="cancelled", message="handler observed cancellation")


# ---------------------------------------------------------------------------
# Higher-level errors used at the crate boundary.
# ---------------------------------------------------------------------------


class WsToolError(Exception):
    """Base class for higher-level errors from the WS tool bus."""


@dataclass
class AuthRejected(WsToolError):
    """``auth rejected: {message}``."""

    message: str

    def __str__(self) -> str:  # pragma: no cover
        return f"auth rejected: {self.message}"


@dataclass
class Unsupported(WsToolError):
    """``unsupported tool: {tool}``."""

    tool: str

    def __str__(self) -> str:  # pragma: no cover
        return f"unsupported tool: {self.tool}"


@dataclass
class Disconnected(WsToolError):
    """The runner disconnected before reply."""

    def __str__(self) -> str:  # pragma: no cover
        return "runner disconnected"


@dataclass
class TimeoutError_(WsToolError):
    """``timed out after {millis}ms``.

    Trailing underscore avoids clashing with built-in ``TimeoutError``;
    re-exported as ``Timeout`` for convenience by the package init module.
    """

    millis: int

    def __str__(self) -> str:  # pragma: no cover
        return f"timed out after {self.millis}ms"


@dataclass
class ToolFailed(WsToolError):
    """``tool {code}: {message}`` — the runner returned a structured fail."""

    code: str
    message: str

    def __str__(self) -> str:  # pragma: no cover
        return f"tool {self.code}: {self.message}"


@dataclass
class ProtocolError(WsToolError):
    """Wire-level protocol violation."""

    message: str

    def __str__(self) -> str:  # pragma: no cover
        return f"protocol: {self.message}"


@dataclass
class InternalError(WsToolError):
    """Catch-all unexpected error inside the bus."""

    message: str

    def __str__(self) -> str:  # pragma: no cover
        return f"internal: {self.message}"


# ---------------------------------------------------------------------------
# Server config + handshake info.
# ---------------------------------------------------------------------------


@dataclass
class WsToolConfig:
    """Wire-level configuration handed to the server.

    Mirrors ``rust/crates/corlinman-wstool/src/server.rs::WsToolConfig``.
    """

    bind_host: str = "127.0.0.1"
    bind_port: int = 0
    auth_token: str = ""
    heartbeat_secs: int = 15
    max_missed_pings: int = 3
    server_version: str = "0.1.0"

    @classmethod
    def loopback(cls, token: str) -> WsToolConfig:
        return cls(bind_host="127.0.0.1", bind_port=0, auth_token=token)


@dataclass
class AcceptInfo:
    """Server-side handshake info exposed to the runner."""

    server_version: str = "unknown"
    heartbeat_secs: int = 15


# ---------------------------------------------------------------------------
# Internal invocation reply (server-side waiter outcome).
# ---------------------------------------------------------------------------


@dataclass
class InvokeOutcome:
    """Terminal outcome for one ``invoke`` request_id.

    Mirrors ``InvokeReply`` in the Rust crate. Exactly one of the
    payload fields is populated depending on the variant; callers
    dispatch on :attr:`kind`.
    """

    kind: str  # one of: ok | tool_failed | result_error | disconnected
    payload: Any = None
    code: str = ""
    message: str = ""


@dataclass
class ResultErrorReply:
    """Payload for an ``ok=False`` ``result`` frame; see ``InvokeOutcome``."""

    payload: Any = None


# ---------------------------------------------------------------------------
# File-fetcher value type (used by file-fetcher port).
# ---------------------------------------------------------------------------


@dataclass
class FetchedBlob:
    """A successfully fetched blob with content metadata.

    Mirrors ``rust/crates/corlinman-wstool/src/file_fetcher.rs::FetchedBlob``.
    """

    data: bytes
    mime: str | None
    sha256: str  # lowercase hex sha256
    total_bytes: int

    def __post_init__(self) -> None:
        # Defensive copy so callers can't mutate the bytes via the original.
        if not isinstance(self.data, (bytes, bytearray, memoryview)):
            raise TypeError(f"FetchedBlob.data must be bytes-like, got {type(self.data).__name__}")
        # Force to immutable bytes.
        if not isinstance(self.data, bytes):
            object.__setattr__(self, "data", bytes(self.data))


# Re-export under a short alias for ``__init__``.
Timeout = TimeoutError_


# Forward-declared field for future expansion (kept for parity / typing). The
# ``field`` import is used here only so type checkers don't complain about
# unused imports if downstream picks up new optional fields.
_ = field
