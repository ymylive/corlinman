"""corlinman-server.gateway_api — Python mirror of ``corlinman-gateway-api``.

Contract-only submodule that defines the shared chat-pipeline surface
used by in-process callers (channels, scheduler, admin tasks) without
pulling the full gateway implementation. Mirrors the Rust trait
``ChatService`` and its request / response data types.

Dependency topology (Python side, mirroring the Rust diagram in
``rust/crates/corlinman-gateway-api/src/lib.rs``)::

    corlinman_providers (CorlinmanError / failover reasons)
            ^
            |
    corlinman_server.gateway_api   (this module — protocol only)
            ^
            +-- corlinman_server.<gateway impl>     (impl protocol)
            +-- corlinman channel / scheduler subs  (depend on protocol)

This module is I/O-free: data types, an async ``Protocol`` and one
``ABC`` convenience base. No business logic lives here.

Public API (see ``__all__`` below) is re-exported from
:mod:`corlinman_server` once the parent ``__init__.py`` is updated —
see the integration report attached to the port for the exact diff.
"""

from __future__ import annotations

from corlinman_server.gateway_api.protocol import (
    ChatEventStream,
    ChatService,
    ChatServiceBase,
    SharedChatService,
)
from corlinman_server.gateway_api.types import (
    Attachment,
    AttachmentKind,
    ChannelBinding,
    DoneEvent,
    ErrorEvent,
    InternalChatError,
    InternalChatEvent,
    InternalChatRequest,
    Message,
    Role,
    TokenDeltaEvent,
    ToolCallEvent,
    Usage,
    internal_chat_error_from_corlinman_error,
)

__all__ = [
    # Data types
    "Attachment",
    "AttachmentKind",
    "ChannelBinding",
    "InternalChatError",
    "InternalChatRequest",
    "Message",
    "Role",
    "Usage",
    # Event variants (sum type — discriminate by isinstance / .kind)
    "InternalChatEvent",
    "TokenDeltaEvent",
    "ToolCallEvent",
    "DoneEvent",
    "ErrorEvent",
    # Protocol / trait surface
    "ChatEventStream",
    "ChatService",
    "ChatServiceBase",
    "SharedChatService",
    # Helpers
    "internal_chat_error_from_corlinman_error",
]
