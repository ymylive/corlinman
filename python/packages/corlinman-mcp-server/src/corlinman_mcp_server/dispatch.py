"""Real :class:`FrameHandler` implementation that drives the
per-session state machine and dispatches by method-prefix to a set of
registered :class:`CapabilityAdapter`\\ s.

Mirrors the Rust ``server::dispatch`` module 1:1.

Routing happens in three stages:

1. **Lifecycle gate.**
   :meth:`SessionState.check_request_allowed` /
   :meth:`SessionState.check_notification_allowed` refuses
   non-``initialize`` requests while the session is still
   ``Connected`` / ``Initializing``.
2. **Built-in methods.** ``initialize`` and
   ``notifications/initialized`` are handled here, not by an adapter:
   they mutate :class:`SessionState` and emit the canonical
   :class:`InitializeResult` reply.
3. **Capability adapters.** Anything else is routed by the prefix
   before the first ``/``. Adapters are stored in a sorted dict so
   lookup is O(1) and iteration order is stable for snapshot tests.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Protocol

import structlog

from .adapters import CapabilityAdapter, SessionContext
from .errors import (
    McpError,
    McpInternalError,
    McpInvalidParamsError,
    McpMethodNotFoundError,
)
from .session import (
    INITIALIZE_METHOD,
    INITIALIZED_NOTIFICATION,
    SessionState,
    initialize_reply,
)
from .types import (
    InitializeParams,
    JsonRpcErrorResponse,
    JsonRpcRequest,
    JsonRpcResponse,
    JsonRpcResultResponse,
    PromptsCapability,
    ResourcesCapability,
    ServerCapabilities,
    ToolsCapability,
)

log = structlog.get_logger(__name__)


@dataclass
class ServerInfo:
    """Server identity surfaced in ``initialize`` replies."""

    name: str = "corlinman"
    version: str = "0.1.0"

    @classmethod
    def default(cls) -> ServerInfo:
        version = os.environ.get("CORLINMAN_MCP_SERVER_VERSION", "0.1.0")
        return cls(name="corlinman", version=version)


class FrameHandler(Protocol):
    """Pluggable JSON-RPC frame handler. The transport calls
    :meth:`handle` for every inbound frame."""

    async def handle(
        self,
        req: JsonRpcRequest,
        session: SessionState,
        session_lock: asyncio.Lock,
        ctx: SessionContext,
    ) -> JsonRpcResultResponse | JsonRpcErrorResponse | None: ...


class StubMethodNotFoundHandler:
    """Frame handler that always returns ``MethodNotFound``. Useful as
    a placeholder when standing up the transport without any
    adapters."""

    async def handle(
        self,
        req: JsonRpcRequest,
        session: SessionState,  # noqa: ARG002
        session_lock: asyncio.Lock,  # noqa: ARG002
        ctx: SessionContext,  # noqa: ARG002
    ) -> JsonRpcResultResponse | JsonRpcErrorResponse | None:
        if req.is_notification():
            return None
        raise McpMethodNotFoundError(req.method)


class AdapterDispatcher:
    """Real :class:`FrameHandler` — built from a set of capability
    adapters. The transport's per-connection :class:`SessionState` +
    :class:`SessionContext` are supplied at call time; this handler
    owns no per-connection state.

    Mirrors the Rust ``AdapterDispatcher`` 1:1.
    """

    def __init__(self, server_info: ServerInfo | None = None) -> None:
        self._server_info: ServerInfo = server_info or ServerInfo.default()
        self._adapters: dict[str, CapabilityAdapter] = {}
        self._capabilities: ServerCapabilities = ServerCapabilities()

    @classmethod
    def from_adapters(
        cls,
        server_info: ServerInfo,
        adapters: list[CapabilityAdapter],
    ) -> AdapterDispatcher:
        d = cls(server_info)
        for a in adapters:
            d.register(a)
        return d

    @property
    def server_info(self) -> ServerInfo:
        return self._server_info

    @property
    def capabilities(self) -> ServerCapabilities:
        return self._capabilities

    def register(self, adapter: CapabilityAdapter) -> None:
        """Register one capability adapter. Last-write-wins on
        duplicates; we log a warning so a typo doesn't silently
        shadow."""
        cap = adapter.capability_name()
        if cap in self._adapters:
            log.warning(
                "mcp dispatcher: duplicate adapter; replacing",
                capability=cap,
            )
        self._adapters[cap] = adapter
        # Refresh advertised capabilities. We use the spec's
        # "object, even if empty" shape — present means supported.
        if cap == "tools":
            self._capabilities.tools = ToolsCapability()
        elif cap == "resources":
            self._capabilities.resources = ResourcesCapability(subscribe=False)
        elif cap == "prompts":
            self._capabilities.prompts = PromptsCapability()
        else:
            log.warning(
                "mcp dispatcher: unknown adapter capability",
                capability=cap,
            )

    @staticmethod
    def capability_for(method: str) -> str | None:
        if "/" not in method:
            return None
        prefix = method.split("/", 1)[0]
        if prefix in ("tools", "resources", "prompts"):
            return prefix
        return None

    async def handle(
        self,
        req: JsonRpcRequest,
        session: SessionState,
        session_lock: asyncio.Lock,
        ctx: SessionContext,
    ) -> JsonRpcResultResponse | JsonRpcErrorResponse | None:
        # Notifications never produce a reply on the wire, even on
        # error.
        if req.is_notification():
            async with session_lock:
                try:
                    session.check_notification_allowed(req.method)
                except McpError as err:
                    log.debug(
                        "mcp: notification rejected by lifecycle gate",
                        method=req.method,
                        err=str(err),
                    )
                    return None
                if req.method == INITIALIZED_NOTIFICATION:
                    try:
                        session.observe_initialized_notification()
                    except McpError:
                        pass
            return None

        request_id = req.id

        async with session_lock:
            session.check_request_allowed(req.method)

        # Built-in `initialize` reply.
        if req.method == INITIALIZE_METHOD:
            try:
                parsed = InitializeParams.model_validate(req.params or {})
            except Exception as e:
                raise McpInvalidParamsError(f"initialize: bad params: {e}") from e
            async with session_lock:
                session.observe_initialize(parsed)
            reply = initialize_reply(
                self._capabilities.model_copy(deep=True),
                self._server_info.name,
                self._server_info.version,
            )
            try:
                value = reply.model_dump()
            except Exception as e:
                raise McpInternalError(f"initialize: serialize reply: {e}") from e
            return JsonRpcResponse.ok(request_id, value)

        # Capability dispatch.
        cap = self.capability_for(req.method)
        if cap is None:
            raise McpMethodNotFoundError(req.method)
        adapter = self._adapters.get(cap)
        if adapter is None:
            raise McpMethodNotFoundError(req.method)
        result_value = await adapter.handle(req.method, req.params, ctx)
        return JsonRpcResponse.ok(request_id, result_value)


__all__ = [
    "AdapterDispatcher",
    "FrameHandler",
    "ServerInfo",
    "StubMethodNotFoundHandler",
]
