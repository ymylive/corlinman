"""Per-connection MCP session state machine.

One WebSocket connection = one :class:`SessionState`. The state
controls which JSON-RPC methods the dispatcher accepts.

State diagram (MCP 2024-11-05 §lifecycle)::

                     initialize          notifications/initialized
    [Connected] ───────────────► [Initializing] ───────────────► [Initialized]
        │                              │
        │ (any non-initialize)         │ (any non-notification request)
        ▼                              ▼
    McpSessionNotInitialized           McpSessionNotInitialized
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

from .errors import McpInvalidRequestError, McpSessionNotInitializedError
from .types import (
    MCP_PROTOCOL_VERSION,
    Implementation,
    InitializeParams,
    InitializeResult,
    ServerCapabilities,
)

if TYPE_CHECKING:
    pass


INITIALIZED_NOTIFICATION: str = "notifications/initialized"
"""Method name the client sends to confirm the handshake completed."""

INITIALIZE_METHOD: str = "initialize"
"""Method name carrying the handshake parameters."""


class SessionPhase(Enum):
    """Lifecycle phases of a single MCP session."""

    CONNECTED = "connected"
    """WebSocket upgraded; no ``initialize`` request seen yet."""

    INITIALIZING = "initializing"
    """Server has replied to ``initialize``; awaiting
    ``notifications/initialized`` from the client."""

    INITIALIZED = "initialized"
    """Handshake complete; ``tools/*``, ``resources/*``, ``prompts/*``
    allowed."""

    def accepts_request(self, method: str) -> bool:
        """True iff the dispatcher should accept the given method *as a
        request* (i.e. with an ``id``)."""
        if self is SessionPhase.CONNECTED:
            return method == INITIALIZE_METHOD
        if self is SessionPhase.INITIALIZING:
            return False
        # Initialized
        return method != INITIALIZE_METHOD

    def accepts_notification(self, method: str) -> bool:
        """True iff the dispatcher should accept the given method as a
        notification. Mirrors the Rust ``accepts_notification`` logic.
        """
        if method == INITIALIZED_NOTIFICATION:
            return self is SessionPhase.INITIALIZING
        # Be liberal with non-handshake notifications; reject only
        # when no session is up yet (Connected). Otherwise some clients
        # race `notifications/cancelled` past boot.
        return self is not SessionPhase.CONNECTED


class SessionState:
    """Session state owned by the dispatcher for a single WS
    connection.

    Mirrors the Rust ``SessionState`` struct + methods 1:1.
    """

    __slots__ = (
        "_phase",
        "_client_protocol_version",
        "_client_name",
        "_client_version",
    )

    def __init__(self) -> None:
        self._phase: SessionPhase = SessionPhase.CONNECTED
        self._client_protocol_version: str | None = None
        self._client_name: str | None = None
        self._client_version: str | None = None

    def phase(self) -> SessionPhase:
        """Current phase. Cheap to read."""
        return self._phase

    def client_protocol_version(self) -> str | None:
        return self._client_protocol_version

    def client_name(self) -> str | None:
        return self._client_name

    def client_version(self) -> str | None:
        return self._client_version

    def observe_initialize(self, params: InitializeParams) -> None:
        """Apply the ``initialize`` request. Captures the client
        metadata and advances ``Connected → Initializing``. Raises
        :class:`McpInvalidRequestError` if called outside ``Connected``."""
        if self._phase is not SessionPhase.CONNECTED:
            raise McpInvalidRequestError(
                f"duplicate `initialize`; session already in {self._phase.name}"
            )
        self._client_protocol_version = params.protocol_version
        self._client_name = params.client_info.name
        self._client_version = params.client_info.version
        self._phase = SessionPhase.INITIALIZING

    def observe_initialized_notification(self) -> None:
        """Apply the client's ``notifications/initialized``. Advances
        ``Initializing → Initialized``. Raises
        :class:`McpSessionNotInitializedError` if called before
        ``initialize``."""
        if self._phase is SessionPhase.CONNECTED:
            raise McpSessionNotInitializedError()
        if self._phase is SessionPhase.INITIALIZING:
            self._phase = SessionPhase.INITIALIZED
            return
        # Already initialized — duplicates are a benign no-op per spec.

    def check_request_allowed(self, method: str) -> None:
        """Pre-flight a request. Raises the right :class:`McpError`
        subclass for the dispatcher to lift onto a JSON-RPC error frame,
        or returns ``None`` if the method is admissible in this phase.
        """
        if self._phase.accepts_request(method):
            return
        if self._phase is SessionPhase.INITIALIZED and method == INITIALIZE_METHOD:
            raise McpInvalidRequestError(
                "session already initialized; duplicate `initialize` not supported"
            )
        raise McpSessionNotInitializedError()

    def check_notification_allowed(self, method: str) -> None:
        """Pre-flight a notification frame."""
        if self._phase.accepts_notification(method):
            return
        raise McpSessionNotInitializedError()


def initialize_reply(
    server_capabilities: ServerCapabilities,
    server_name: str,
    server_version: str,
) -> InitializeResult:
    """Build the canonical ``initialize`` reply payload.

    Mirrors the Rust ``initialize_reply`` helper; the dispatcher calls
    this once it has crafted the server-side advertised capabilities.
    """
    return InitializeResult(
        protocolVersion=MCP_PROTOCOL_VERSION,
        capabilities=server_capabilities,
        serverInfo=Implementation(name=server_name, version=server_version),
    )


__all__ = [
    "INITIALIZED_NOTIFICATION",
    "INITIALIZE_METHOD",
    "SessionPhase",
    "SessionState",
    "initialize_reply",
]
