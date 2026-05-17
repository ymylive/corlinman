"""ToolsAdapter — mirrors ``src/adapters/tools.rs`` tests."""

from __future__ import annotations

import pytest

from corlinman_mcp_server import (
    CollectingProgressBridge,
    McpMethodNotFoundError,
    McpToolNotAllowedError,
    PluginOutputAcceptedForLater,
    PluginOutputError,
    PluginOutputSuccess,
    SessionContext,
    TextContent,
    ToolsAdapter,
    ToolsCallParams,
    ToolsCallResult,
    ToolsListResult,
    decode_tool_name,
    encode_tool_name,
)

from .conftest import (
    StubPluginRegistry,
    StubPluginRuntime,
    make_plugin_entry,
)


@pytest.fixture
def kb_registry():
    reg = StubPluginRegistry()
    reg.add(make_plugin_entry("kb", [("search", "find stuff"), ("get", "fetch by id")]))
    return reg


@pytest.mark.asyncio
async def test_list_returns_one_descriptor_per_manifest_tool(kb_registry):
    runtime = StubPluginRuntime(PluginOutputSuccess(content=b"{}", duration_ms=1))
    adapter = ToolsAdapter.with_runtime(kb_registry, runtime)
    result = adapter.list_tools(SessionContext.permissive())
    names = [t.name for t in result.tools]
    assert names == ["kb:get", "kb:search"]
    assert result.tools[0].description == "fetch by id"
    assert result.tools[0].input_schema == {"type": "object"}


@pytest.mark.asyncio
async def test_list_filters_by_allowlist(kb_registry):
    runtime = StubPluginRuntime(PluginOutputSuccess(content=b"{}", duration_ms=1))
    adapter = ToolsAdapter.with_runtime(kb_registry, runtime)
    ctx = SessionContext(tools_allowlist=["kb:s*"])
    result = adapter.list_tools(ctx)
    names = [t.name for t in result.tools]
    assert names == ["kb:search"]


@pytest.mark.asyncio
async def test_call_success_returns_text_block_with_no_is_error(kb_registry):
    runtime = StubPluginRuntime(
        PluginOutputSuccess(content=b'{"ok":1}', duration_ms=5)
    )
    adapter = ToolsAdapter.with_runtime(kb_registry, runtime)
    res = await adapter.call_tool(
        ToolsCallParams(name="kb:search", arguments={"q": "hi"}),
        SessionContext.permissive(),
        None,
    )
    assert not res.is_error
    assert isinstance(res.content[0], TextContent)
    assert res.content[0].text == '{"ok":1}'


@pytest.mark.asyncio
async def test_call_runtime_error_surfaces_as_is_error_not_jsonrpc_error(kb_registry):
    runtime = StubPluginRuntime(PluginOutputError(code=7, message="boom", duration_ms=5))
    adapter = ToolsAdapter.with_runtime(kb_registry, runtime)
    res = await adapter.call_tool(
        ToolsCallParams(name="kb:search", arguments={}),
        SessionContext.permissive(),
        None,
    )
    assert res.is_error
    assert isinstance(res.content[0], TextContent)
    assert "boom" in res.content[0].text
    assert "[code 7]" in res.content[0].text


@pytest.mark.asyncio
async def test_call_accepted_for_later_collapses_to_text(kb_registry):
    runtime = StubPluginRuntime(
        PluginOutputAcceptedForLater(task_id="t-1", duration_ms=1)
    )
    adapter = ToolsAdapter.with_runtime(kb_registry, runtime)
    res = await adapter.call_tool(
        ToolsCallParams(name="kb:search", arguments=None),
        SessionContext.permissive(),
        None,
    )
    assert not res.is_error
    assert isinstance(res.content[0], TextContent)
    assert "accepted-for-later" in res.content[0].text
    assert "task_id=t-1" in res.content[0].text


@pytest.mark.asyncio
async def test_call_unknown_plugin_returns_method_not_found():
    reg = StubPluginRegistry()
    reg.add(make_plugin_entry("kb", [("search", "")]))
    runtime = StubPluginRuntime(PluginOutputSuccess(content=b"", duration_ms=0))
    adapter = ToolsAdapter.with_runtime(reg, runtime)
    with pytest.raises(McpMethodNotFoundError) as exc:
        await adapter.call_tool(
            ToolsCallParams(name="ghost:do", arguments=None),
            SessionContext.permissive(),
            None,
        )
    assert exc.value.jsonrpc_code() == -32601


@pytest.mark.asyncio
async def test_call_unknown_tool_on_known_plugin_returns_method_not_found(kb_registry):
    runtime = StubPluginRuntime(PluginOutputSuccess(content=b"", duration_ms=0))
    adapter = ToolsAdapter.with_runtime(kb_registry, runtime)
    with pytest.raises(McpMethodNotFoundError):
        await adapter.call_tool(
            ToolsCallParams(name="kb:nope", arguments=None),
            SessionContext.permissive(),
            None,
        )


@pytest.mark.asyncio
async def test_call_with_disallowed_tool_returns_tool_not_allowed(kb_registry):
    runtime = StubPluginRuntime(PluginOutputSuccess(content=b"", duration_ms=0))
    adapter = ToolsAdapter.with_runtime(kb_registry, runtime)
    ctx = SessionContext(tools_allowlist=["other:*"])
    with pytest.raises(McpToolNotAllowedError) as exc:
        await adapter.call_tool(
            ToolsCallParams(name="kb:search", arguments=None),
            ctx,
            None,
        )
    assert exc.value.tool_name == "kb:search"


@pytest.mark.asyncio
async def test_call_progress_events_forward_to_bridge_when_token_supplied(kb_registry):
    runtime = StubPluginRuntime(
        PluginOutputSuccess(content=b"done", duration_ms=1),
        progress_emit=("halfway", 0.5),
    )
    bridge = CollectingProgressBridge()
    adapter = ToolsAdapter(kb_registry, runtime, bridge)
    res = await adapter.call_tool(
        ToolsCallParams(name="kb:search", arguments=None),
        SessionContext.permissive(),
        "p-token-1",
    )
    assert not res.is_error
    events = bridge.drain()
    assert len(events) == 1
    assert events[0].progress_token == "p-token-1"
    assert events[0].message == "halfway"
    assert events[0].fraction == 0.5
    params = events[0].to_progress_params()
    assert params["progressToken"] == "p-token-1"
    assert params["message"] == "halfway"
    assert abs(float(params["progress"]) - 0.5) < 1e-6


@pytest.mark.asyncio
async def test_handle_routes_unknown_method_to_method_not_found():
    reg = StubPluginRegistry()
    reg.add(make_plugin_entry("kb", []))
    runtime = StubPluginRuntime(PluginOutputSuccess(content=b"", duration_ms=0))
    adapter = ToolsAdapter.with_runtime(reg, runtime)
    with pytest.raises(McpMethodNotFoundError):
        await adapter.handle("tools/bogus", None, SessionContext.permissive())


@pytest.mark.asyncio
async def test_handle_routes_list_through_capability_adapter():
    reg = StubPluginRegistry()
    reg.add(make_plugin_entry("kb", [("search", "")]))
    runtime = StubPluginRuntime(PluginOutputSuccess(content=b"", duration_ms=0))
    adapter = ToolsAdapter.with_runtime(reg, runtime)
    assert adapter.capability_name() == "tools"
    value = await adapter.handle(
        "tools/list", None, SessionContext.permissive()
    )
    parsed = ToolsListResult.model_validate(value)
    assert len(parsed.tools) == 1
    assert parsed.tools[0].name == "kb:search"


@pytest.mark.asyncio
async def test_handle_routes_call_through_capability_adapter_with_meta_progress():
    reg = StubPluginRegistry()
    reg.add(make_plugin_entry("kb", [("search", "")]))
    runtime = StubPluginRuntime(
        PluginOutputSuccess(content=b"ok", duration_ms=1),
        progress_emit=("step", 0.25),
    )
    bridge = CollectingProgressBridge()
    adapter = ToolsAdapter(reg, runtime, bridge)
    value = await adapter.handle(
        "tools/call",
        {
            "name": "kb:search",
            "arguments": {"q": "x"},
            "_meta": {"progressToken": "tok-1"},
        },
        SessionContext.permissive(),
    )
    parsed = ToolsCallResult.model_validate(value)
    assert not parsed.is_error
    events = bridge.drain()
    assert events and events[0].progress_token == "tok-1"


def test_encode_decode_round_trip():
    n = encode_tool_name("kb", "search")
    assert n == "kb:search"
    assert decode_tool_name(n) == ("kb", "search")
    assert decode_tool_name("noop") is None
    assert decode_tool_name(":x") is None
    assert decode_tool_name("x:") is None
