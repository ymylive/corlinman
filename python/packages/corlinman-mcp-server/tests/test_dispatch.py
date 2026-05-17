"""AdapterDispatcher — mirrors ``src/server/dispatch.rs`` tests."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from corlinman_mcp_server import (
    AdapterDispatcher,
    JsonRpcRequest,
    JsonRpcResponse,
    JsonRpcResultResponse,
    ServerInfo,
    SessionContext,
    SessionState,
)


class DummyAdapter:
    """Dummy adapter used to exercise dispatch routing."""

    def __init__(self, name: str) -> None:
        self._name = name
        self.last_method: str | None = None

    def capability_name(self) -> str:
        return self._name

    async def handle(self, method: str, params: Any, ctx: SessionContext) -> Any:  # noqa: ARG002
        self.last_method = method
        return {"adapter": self._name, "method": method}


@pytest.mark.asyncio
async def test_initialize_advertises_registered_capabilities_only():
    d = AdapterDispatcher.from_adapters(ServerInfo(), [DummyAdapter("tools")])
    session = SessionState()
    lock = asyncio.Lock()
    ctx = SessionContext.permissive()

    req = JsonRpcRequest(
        jsonrpc="2.0",
        id="init-1",
        method="initialize",
        params={
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0.1"},
        },
    )
    reply = await d.handle(req, session, lock, ctx)
    assert isinstance(reply, JsonRpcResultResponse)
    result = reply.result
    assert result["capabilities"]["tools"] == {}
    assert "resources" not in result["capabilities"]
    assert "prompts" not in result["capabilities"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["serverInfo"]["name"] == "corlinman"


@pytest.mark.asyncio
async def test_pre_initialize_request_returns_session_not_initialized():
    d = AdapterDispatcher.from_adapters(ServerInfo(), [DummyAdapter("tools")])
    session = SessionState()
    lock = asyncio.Lock()
    ctx = SessionContext.permissive()

    req = JsonRpcRequest(
        jsonrpc="2.0",
        id=1,
        method="tools/list",
        params=None,
    )
    from corlinman_mcp_server import McpSessionNotInitializedError

    with pytest.raises(McpSessionNotInitializedError) as exc:
        await d.handle(req, session, lock, ctx)
    assert exc.value.jsonrpc_code() == -32002


@pytest.mark.asyncio
async def test_post_handshake_dispatch_routes_to_capability_adapter():
    tools = DummyAdapter("tools")
    d = AdapterDispatcher.from_adapters(ServerInfo(), [tools])
    session = SessionState()
    lock = asyncio.Lock()
    ctx = SessionContext.permissive()

    init_req = JsonRpcRequest(
        jsonrpc="2.0",
        id="i",
        method="initialize",
        params={
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"},
        },
    )
    await d.handle(init_req, session, lock, ctx)

    initd = JsonRpcRequest(
        jsonrpc="2.0",
        id=None,
        method="notifications/initialized",
        params=None,
    )
    await d.handle(initd, session, lock, ctx)

    req = JsonRpcRequest(
        jsonrpc="2.0",
        id=7,
        method="tools/list",
        params=None,
    )
    reply = await d.handle(req, session, lock, ctx)
    assert isinstance(reply, JsonRpcResultResponse)
    assert reply.id == 7
    assert reply.result["adapter"] == "tools"
    assert reply.result["method"] == "tools/list"
    assert tools.last_method == "tools/list"


@pytest.mark.asyncio
async def test_unknown_capability_returns_method_not_found():
    d = AdapterDispatcher.from_adapters(ServerInfo(), [DummyAdapter("tools")])
    session = SessionState()
    lock = asyncio.Lock()
    ctx = SessionContext.permissive()

    # Walk handshake.
    await d.handle(
        JsonRpcRequest(
            jsonrpc="2.0",
            id="i",
            method="initialize",
            params={
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        ),
        session,
        lock,
        ctx,
    )
    await d.handle(
        JsonRpcRequest(
            jsonrpc="2.0",
            id=None,
            method="notifications/initialized",
            params=None,
        ),
        session,
        lock,
        ctx,
    )

    from corlinman_mcp_server import McpMethodNotFoundError

    with pytest.raises(McpMethodNotFoundError) as exc:
        await d.handle(
            JsonRpcRequest(
                jsonrpc="2.0",
                id=1,
                method="resources/list",
                params=None,
            ),
            session,
            lock,
            ctx,
        )
    assert exc.value.jsonrpc_code() == -32601
