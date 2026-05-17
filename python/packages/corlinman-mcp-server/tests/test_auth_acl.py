"""Integration tests for per-token ACL + tenant scoping. Mirrors
``tests/auth_acl.rs`` 1:1 — exercises the full path from
``TokenAcl.to_session_context()`` through the adapters' ``handle()``
methods.
"""

from __future__ import annotations

import pytest

from corlinman_mcp_server import (
    DEFAULT_TENANT_ID,
    McpToolNotAllowedError,
    PluginOutputSuccess,
    ResourcesAdapter,
    ResourcesListResult,
    TokenAcl,
    ToolsAdapter,
    ToolsCallResult,
    ToolsListResult,
)

from .conftest import (
    StubMemoryHost,
    StubPluginRegistry,
    StubPluginRuntime,
    StubSkill,
    StubSkillRegistry,
    make_plugin_entry,
)


def _two_tool_registry() -> StubPluginRegistry:
    reg = StubPluginRegistry()
    reg.add(make_plugin_entry("kb", [("search", "search kb"), ("get", "fetch by id")]))
    reg.add(make_plugin_entry("web", [("fetch", "fetch URL")]))
    return reg


@pytest.mark.asyncio
async def test_tools_list_filters_by_acl_allowlist_end_to_end():
    reg = _two_tool_registry()
    runtime = StubPluginRuntime(PluginOutputSuccess(content=b'"ok"', duration_ms=1))
    adapter = ToolsAdapter.with_runtime(reg, runtime)

    acl = TokenAcl(
        token="t",
        label="limited",
        tools_allowlist=["kb:*"],
        resources_allowed=["*"],
        prompts_allowed=["*"],
        tenant_id="alpha",
    )
    ctx = acl.to_session_context()

    value = await adapter.handle("tools/list", None, ctx)
    parsed = ToolsListResult.model_validate(value)
    names = [t.name for t in parsed.tools]
    assert names == ["kb:get", "kb:search"]  # web:fetch filtered out


@pytest.mark.asyncio
async def test_tools_call_rejected_when_disallowed_by_acl():
    reg = _two_tool_registry()
    runtime = StubPluginRuntime(PluginOutputSuccess(content=b'"ok"', duration_ms=1))
    adapter = ToolsAdapter.with_runtime(reg, runtime)

    acl = TokenAcl(
        token="t",
        label="limited",
        tools_allowlist=["kb:*"],
        resources_allowed=["*"],
        prompts_allowed=["*"],
        tenant_id=None,
    )
    ctx = acl.to_session_context()

    with pytest.raises(McpToolNotAllowedError) as exc:
        await adapter.handle(
            "tools/call",
            {"name": "web:fetch", "arguments": {}},
            ctx,
        )
    assert exc.value.tool_name == "web:fetch"
    assert exc.value.jsonrpc_code() == -32001

    # And the allowed branch still works.
    ok = await adapter.handle(
        "tools/call",
        {"name": "kb:search", "arguments": {}},
        ctx,
    )
    parsed = ToolsCallResult.model_validate(ok)
    assert not parsed.is_error


@pytest.mark.asyncio
async def test_resources_list_filters_by_scheme_allowlist_end_to_end():
    hosts = {"alpha": StubMemoryHost("alpha", {"1": "ALPHA-1"})}
    skills = StubSkillRegistry(
        [StubSkill(name="foo", description="stub", body_markdown="body")]
    )
    adapter = ResourcesAdapter(memory_hosts=hosts, skills=skills)

    acl = TokenAcl(
        token="t",
        label="skills-only",
        tools_allowlist=["*"],
        resources_allowed=["skill"],
        prompts_allowed=["*"],
        tenant_id="alpha",
    )
    ctx = acl.to_session_context()
    value = await adapter.handle("resources/list", None, ctx)
    parsed = ResourcesListResult.model_validate(value)
    for r in parsed.resources:
        assert r.uri.startswith("corlinman://skill/")
    assert parsed.resources, "skill list must be non-empty"

    # Read of a memory uri is rejected with -32602 + 'not allowed'.
    from corlinman_mcp_server import McpInvalidParamsError

    with pytest.raises(McpInvalidParamsError) as exc:
        await adapter.handle(
            "resources/read",
            {"uri": "corlinman://memory/alpha/1"},
            ctx,
        )
    assert exc.value.jsonrpc_code() == -32602


@pytest.mark.asyncio
async def test_cross_tenant_read_returns_empty_or_unknown_host():
    # Only alpha host visible — the gateway integration prunes others
    # by tenant before passing the host map in. We simulate that here.
    alpha_only = {"alpha": StubMemoryHost("alpha", {"1": "ALPHA-1"})}
    adapter = ResourcesAdapter(memory_hosts=alpha_only, skills=StubSkillRegistry())

    acl = TokenAcl(
        token="t",
        label="alpha-token",
        tools_allowlist=["*"],
        resources_allowed=["*"],
        prompts_allowed=["*"],
        tenant_id="alpha",
    )
    ctx = acl.to_session_context()
    assert ctx.tenant_id == "alpha"

    from corlinman_mcp_server import McpInvalidParamsError

    with pytest.raises(McpInvalidParamsError) as exc:
        await adapter.handle(
            "resources/read",
            {"uri": "corlinman://memory/beta/1"},
            ctx,
        )
    assert exc.value.jsonrpc_code() == -32602

    lvalue = await adapter.handle("resources/list", None, ctx)
    lparsed = ResourcesListResult.model_validate(lvalue)
    uris = [r.uri for r in lparsed.resources]
    assert any(u.startswith("corlinman://memory/alpha/") for u in uris)
    assert not any(u.startswith("corlinman://memory/beta/") for u in uris)


@pytest.mark.asyncio
async def test_missing_tenant_falls_back_to_default_constant():
    acl = TokenAcl(
        token="t",
        label="no-tenant",
        tools_allowlist=["*"],
        resources_allowed=["*"],
        prompts_allowed=["*"],
        tenant_id=None,
    )
    ctx = acl.to_session_context()
    assert ctx.tenant_id == DEFAULT_TENANT_ID == "default"


@pytest.mark.asyncio
async def test_empty_acl_lists_fail_closed_at_adapter_layer():
    acl = TokenAcl(
        token="t",
        label="empty",
        tools_allowlist=[],
        resources_allowed=[],
        prompts_allowed=[],
        tenant_id=None,
    )
    ctx = acl.to_session_context()

    reg = _two_tool_registry()
    runtime = StubPluginRuntime(PluginOutputSuccess(content=b'"ok"', duration_ms=1))
    adapter = ToolsAdapter.with_runtime(reg, runtime)
    value = await adapter.handle("tools/list", None, ctx)
    parsed = ToolsListResult.model_validate(value)
    assert parsed.tools == []

    with pytest.raises(McpToolNotAllowedError) as exc:
        await adapter.handle(
            "tools/call",
            {"name": "kb:search", "arguments": {}},
            ctx,
        )
    assert exc.value.jsonrpc_code() == -32001

    hosts = {"kb": StubMemoryHost("kb", {"1": "x"})}
    res_adapter = ResourcesAdapter(
        memory_hosts=hosts,
        skills=StubSkillRegistry([StubSkill(name="foo", description="x", body_markdown="x")]),
    )
    lvalue = await res_adapter.handle("resources/list", None, ctx)
    lparsed = ResourcesListResult.model_validate(lvalue)
    assert lparsed.resources == []
