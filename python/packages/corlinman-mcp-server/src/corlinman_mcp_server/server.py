"""Server-side facade — re-exports the dispatcher + transport + session
+ auth surfaces used to stand up a `/mcp` WebSocket endpoint.

Mirrors the Rust ``server`` module's ``pub use`` surface so callers can
write ``from corlinman_mcp_server.server import McpServer`` instead of
hunting through every module.
"""

from __future__ import annotations

from .auth import DEFAULT_TENANT_ID, TokenAcl, resolve_token
from .dispatch import (
    AdapterDispatcher,
    FrameHandler,
    ServerInfo,
    StubMethodNotFoundHandler,
)
from .session import (
    INITIALIZED_NOTIFICATION,
    INITIALIZE_METHOD,
    SessionPhase,
    SessionState,
    initialize_reply,
)
from .transport import (
    CLOSE_CODE_MESSAGE_TOO_BIG,
    DEFAULT_MAX_FRAME_BYTES,
    McpServer,
    McpServerConfig,
    connection_loop,
)

__all__ = [
    "AdapterDispatcher",
    "CLOSE_CODE_MESSAGE_TOO_BIG",
    "DEFAULT_MAX_FRAME_BYTES",
    "DEFAULT_TENANT_ID",
    "FrameHandler",
    "INITIALIZE_METHOD",
    "INITIALIZED_NOTIFICATION",
    "McpServer",
    "McpServerConfig",
    "ServerInfo",
    "SessionPhase",
    "SessionState",
    "StubMethodNotFoundHandler",
    "TokenAcl",
    "connection_loop",
    "initialize_reply",
    "resolve_token",
]
