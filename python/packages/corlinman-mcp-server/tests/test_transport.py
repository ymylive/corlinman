"""WebSocket transport — mirrors ``src/server/transport.rs`` tests.

Spins an isolated MCP server on ``127.0.0.1:0`` and dials it with the
``websockets`` client.
"""

from __future__ import annotations

import json

import pytest
import websockets

from corlinman_mcp_server import (
    McpServer,
    McpServerConfig,
    TokenAcl,
    error_codes,
)


async def _spawn(cfg: McpServerConfig):
    server = McpServer.with_stub(cfg)
    s = await server.bind(host="127.0.0.1", port=0)
    sockets = list(s.sockets) if hasattr(s, "sockets") else []
    port = sockets[0].getsockname()[1] if sockets else 0
    return port, s


def _is_unauthorized(err: Exception) -> bool:
    msg = str(err).lower()
    return "401" in msg or "unauthorized" in msg


@pytest.mark.asyncio
async def test_rejects_pre_upgrade_when_token_missing():
    port, s = await _spawn(McpServerConfig.with_token("good"))
    try:
        url = f"ws://127.0.0.1:{port}/mcp"
        with pytest.raises(Exception) as exc:
            async with websockets.connect(url):
                pass
        assert _is_unauthorized(exc.value), f"expected 401, got {exc.value}"
    finally:
        s.close()
        await s.wait_closed()


@pytest.mark.asyncio
async def test_rejects_pre_upgrade_when_token_wrong():
    port, s = await _spawn(McpServerConfig.with_token("good"))
    try:
        url = f"ws://127.0.0.1:{port}/mcp?token=BAD"
        with pytest.raises(Exception) as exc:
            async with websockets.connect(url):
                pass
        assert _is_unauthorized(exc.value), f"expected 401, got {exc.value}"
    finally:
        s.close()
        await s.wait_closed()


@pytest.mark.asyncio
async def test_empty_token_list_rejects_everything():
    port, s = await _spawn(McpServerConfig())
    try:
        url = f"ws://127.0.0.1:{port}/mcp?token=anything"
        with pytest.raises(Exception) as exc:
            async with websockets.connect(url):
                pass
        assert _is_unauthorized(exc.value), f"expected 401, got {exc.value}"
    finally:
        s.close()
        await s.wait_closed()


@pytest.mark.asyncio
async def test_upgrades_with_valid_token_and_stub_returns_method_not_found():
    port, s = await _spawn(McpServerConfig.with_token("good"))
    try:
        url = f"ws://127.0.0.1:{port}/mcp?token=good"
        async with websockets.connect(url) as ws:
            req = {
                "jsonrpc": "2.0",
                "id": "req-1",
                "method": "tools/list",
                "params": None,
            }
            await ws.send(json.dumps(req))
            reply = await ws.recv()
            parsed = json.loads(reply)
            assert "error" in parsed
            assert parsed["id"] == "req-1"
            assert parsed["error"]["code"] == error_codes.METHOD_NOT_FOUND
            assert "tools/list" in parsed["error"]["message"]
    finally:
        s.close()
        await s.wait_closed()


@pytest.mark.asyncio
async def test_notifications_get_no_reply():
    port, s = await _spawn(McpServerConfig.with_token("good"))
    try:
        url = f"ws://127.0.0.1:{port}/mcp?token=good"
        async with websockets.connect(url) as ws:
            notif = {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": None,
            }
            await ws.send(json.dumps(notif))
            req = {
                "jsonrpc": "2.0",
                "id": "after-notif",
                "method": "tools/list",
                "params": None,
            }
            await ws.send(json.dumps(req))
            reply = await ws.recv()
            parsed = json.loads(reply)
            # Must be the reply to "after-notif", not the notification.
            assert parsed["id"] == "after-notif"
    finally:
        s.close()
        await s.wait_closed()


@pytest.mark.asyncio
async def test_malformed_json_replies_with_parse_error_and_null_id():
    port, s = await _spawn(McpServerConfig.with_token("good"))
    try:
        url = f"ws://127.0.0.1:{port}/mcp?token=good"
        async with websockets.connect(url) as ws:
            await ws.send("{not json")
            reply = await ws.recv()
            parsed = json.loads(reply)
            assert parsed["id"] is None
            assert parsed["error"]["code"] == error_codes.PARSE_ERROR
    finally:
        s.close()
        await s.wait_closed()


@pytest.mark.asyncio
async def test_oversize_frame_triggers_close_1009():
    cfg = McpServerConfig.with_token("good")
    cfg.max_frame_bytes = 256
    port, s = await _spawn(cfg)
    try:
        url = f"ws://127.0.0.1:{port}/mcp?token=good"
        async with websockets.connect(url) as ws:
            huge = "x" * 1024
            await ws.send(huge)
            # Expect the server to close with 1009. Read until close.
            with pytest.raises(websockets.exceptions.ConnectionClosed) as exc:
                await ws.recv()
            assert exc.value.code == 1009 or exc.value.rcvd.code == 1009
    finally:
        s.close()
        await s.wait_closed()


@pytest.mark.asyncio
async def test_structured_acl_resolves_through_pre_upgrade():
    acl = TokenAcl(
        token="acl-token",
        label="scoped-laptop",
        tools_allowlist=["kb:*"],
        resources_allowed=["skill"],
        prompts_allowed=["*"],
        tenant_id="alpha",
    )
    port, s = await _spawn(McpServerConfig.with_acl(acl))
    try:
        url = f"ws://127.0.0.1:{port}/mcp?token=acl-token"
        async with websockets.connect(url) as ws:
            # Stub handler still returns MethodNotFound — confirms the
            # upgrade path doesn't drop a structured ACL.
            req = {
                "jsonrpc": "2.0",
                "id": "p",
                "method": "tools/list",
                "params": None,
            }
            await ws.send(json.dumps(req))
            reply = await ws.recv()
            parsed = json.loads(reply)
            assert parsed["error"]["code"] == error_codes.METHOD_NOT_FOUND
    finally:
        s.close()
        await s.wait_closed()
