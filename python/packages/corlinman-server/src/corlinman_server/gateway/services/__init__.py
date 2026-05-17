"""``corlinman_server.gateway.services`` — gateway-internal service layer.

Mirrors :rust:`corlinman_gateway::services`. These services expose the
chat pipeline as callable Python APIs so other in-process components
(channel adapters, scheduler jobs, admin tasks) can drive it without a
round-trip through HTTP.
"""

from __future__ import annotations

from corlinman_server.gateway.services.chat_service import (
    ChatBackend,
    ChatService,
    GrpcAgentChatBackend,
)

__all__ = [
    "ChatBackend",
    "ChatService",
    "GrpcAgentChatBackend",
]
