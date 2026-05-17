"""ResourcesAdapter — mirrors ``src/adapters/resources.rs`` tests."""

from __future__ import annotations

import pytest

from corlinman_mcp_server import (
    McpInvalidParamsError,
    McpMethodNotFoundError,
    ResourcesAdapter,
    ResourcesListParams,
    ResourcesListResult,
    ResourcesReadParams,
    SessionContext,
    TextResourceContent,
)

from .conftest import (
    StubMemoryHost,
    StubPersonaProvider,
    StubSkill,
    StubSkillRegistry,
)


def _adapter(hosts: dict | None = None, skills: StubSkillRegistry | None = None):
    return ResourcesAdapter(
        memory_hosts=hosts or {},
        skills=skills or StubSkillRegistry(),
    )


@pytest.mark.asyncio
async def test_list_returns_skills_and_memory_uris():
    hosts = {"kb": StubMemoryHost("kb", {"1": "first", "2": "second"})}
    skills = StubSkillRegistry(
        [StubSkill(name="foo", description="foo desc", body_markdown="Body F")]
    )
    adapter = _adapter(hosts, skills)
    res = await adapter.list_resources(
        ResourcesListParams(cursor=None), SessionContext.permissive()
    )
    uris = [r.uri for r in res.resources]
    assert "corlinman://memory/kb/1" in uris
    assert "corlinman://memory/kb/2" in uris
    assert "corlinman://skill/foo" in uris


@pytest.mark.asyncio
async def test_list_paginates_with_server_issued_cursor():
    seed = {f"{i:03}": f"doc-{i}" for i in range(150)}
    hosts = {"kb": StubMemoryHost("kb", seed)}
    adapter = (
        ResourcesAdapter(memory_hosts=hosts, skills=StubSkillRegistry())
        .with_page_size(50)
        .with_memory_list_limit(200)
    )
    p1 = await adapter.list_resources(
        ResourcesListParams(cursor=None), SessionContext.permissive()
    )
    assert len(p1.resources) == 50
    assert p1.next_cursor == "50"

    p2 = await adapter.list_resources(
        ResourcesListParams(cursor=p1.next_cursor), SessionContext.permissive()
    )
    assert len(p2.resources) == 50
    assert p2.next_cursor == "100"

    p3 = await adapter.list_resources(
        ResourcesListParams(cursor="100"), SessionContext.permissive()
    )
    assert len(p3.resources) == 50
    assert p3.next_cursor is None


@pytest.mark.asyncio
async def test_list_invalid_cursor_returns_invalid_params():
    adapter = _adapter()
    with pytest.raises(McpInvalidParamsError) as exc:
        await adapter.list_resources(
            ResourcesListParams(cursor="not-a-number"),
            SessionContext.permissive(),
        )
    assert exc.value.jsonrpc_code() == -32602


@pytest.mark.asyncio
async def test_list_filters_by_scheme_allowlist():
    hosts = {"kb": StubMemoryHost("kb", {"1": "x"})}
    skills = StubSkillRegistry(
        [StubSkill(name="foo", description="stub desc", body_markdown="body")]
    )
    adapter = _adapter(hosts, skills)
    ctx = SessionContext(resources_allowed=["skill"])
    res = await adapter.list_resources(ResourcesListParams(cursor=None), ctx)
    for r in res.resources:
        assert r.uri.startswith("corlinman://skill/")


@pytest.mark.asyncio
async def test_read_skill_returns_body_markdown_verbatim():
    skills = StubSkillRegistry(
        [StubSkill(name="foo", description="foo desc", body_markdown="Step1.\nStep2.")]
    )
    adapter = _adapter(skills=skills)
    res = await adapter.read_resource(
        ResourcesReadParams(uri="corlinman://skill/foo"),
        SessionContext.permissive(),
    )
    assert isinstance(res.contents[0], TextResourceContent)
    assert res.contents[0].uri == "corlinman://skill/foo"
    assert "Step1." in res.contents[0].text
    assert "Step2." in res.contents[0].text


@pytest.mark.asyncio
async def test_read_memory_routes_to_named_host():
    hosts = {"kb": StubMemoryHost("kb", {"42": "memory body"})}
    adapter = _adapter(hosts)
    res = await adapter.read_resource(
        ResourcesReadParams(uri="corlinman://memory/kb/42"),
        SessionContext.permissive(),
    )
    assert isinstance(res.contents[0], TextResourceContent)
    assert res.contents[0].text == "memory body"


@pytest.mark.asyncio
async def test_read_persona_routes_to_provider():
    persona = StubPersonaProvider(
        ids=["alice"], snap={"alice": {"trait": "curious"}}
    )
    adapter = _adapter().with_persona(persona)
    res = await adapter.read_resource(
        ResourcesReadParams(uri="corlinman://persona/alice/snapshot"),
        SessionContext.permissive(),
    )
    assert isinstance(res.contents[0], TextResourceContent)
    import json

    parsed = json.loads(res.contents[0].text)
    assert parsed == {"trait": "curious"}
    assert res.contents[0].mime_type == "application/json"


@pytest.mark.asyncio
async def test_read_unknown_uri_returns_invalid_params():
    adapter = _adapter()
    with pytest.raises(McpInvalidParamsError) as exc:
        await adapter.read_resource(
            ResourcesReadParams(uri="https://example.com/foo"),
            SessionContext.permissive(),
        )
    assert exc.value.jsonrpc_code() == -32602


@pytest.mark.asyncio
async def test_read_unknown_memory_id_returns_invalid_params():
    hosts = {"kb": StubMemoryHost("kb", {"1": "x"})}
    adapter = _adapter(hosts)
    with pytest.raises(McpInvalidParamsError) as exc:
        await adapter.read_resource(
            ResourcesReadParams(uri="corlinman://memory/kb/9999"),
            SessionContext.permissive(),
        )
    assert exc.value.jsonrpc_code() == -32602


@pytest.mark.asyncio
async def test_read_disallowed_scheme_returns_invalid_params():
    hosts = {"kb": StubMemoryHost("kb", {"1": "x"})}
    adapter = _adapter(hosts)
    ctx = SessionContext(resources_allowed=["skill"])
    with pytest.raises(McpInvalidParamsError) as exc:
        await adapter.read_resource(
            ResourcesReadParams(uri="corlinman://memory/kb/1"),
            ctx,
        )
    assert exc.value.jsonrpc_code() == -32602
    assert "not allowed" in exc.value.message


@pytest.mark.asyncio
async def test_read_isolates_hosts_by_name():
    hosts = {
        "alpha": StubMemoryHost("alpha", {"1": "ALPHA"}),
        "beta": StubMemoryHost("beta", {"1": "BETA"}),
    }
    adapter = _adapter(hosts)
    alpha = await adapter.read_resource(
        ResourcesReadParams(uri="corlinman://memory/alpha/1"),
        SessionContext.permissive(),
    )
    beta = await adapter.read_resource(
        ResourcesReadParams(uri="corlinman://memory/beta/1"),
        SessionContext.permissive(),
    )
    assert isinstance(alpha.contents[0], TextResourceContent)
    assert isinstance(beta.contents[0], TextResourceContent)
    assert alpha.contents[0].text == "ALPHA"
    assert beta.contents[0].text == "BETA"


@pytest.mark.asyncio
async def test_handle_routes_through_capability_adapter():
    hosts = {"kb": StubMemoryHost("kb", {"1": "x"})}
    skills = StubSkillRegistry(
        [StubSkill(name="foo", description="stub desc", body_markdown="body")]
    )
    adapter = _adapter(hosts, skills)
    assert adapter.capability_name() == "resources"

    value = await adapter.handle(
        "resources/list", None, SessionContext.permissive()
    )
    parsed = ResourcesListResult.model_validate(value)
    assert parsed.resources

    with pytest.raises(McpMethodNotFoundError):
        await adapter.handle(
            "resources/bogus", None, SessionContext.permissive()
        )


def test_parse_uri_recognises_three_schemes_and_rejects_others():
    from corlinman_mcp_server.resources import _MemoryUri, _ParsedUri, _PersonaUri, _SkillUri, _parse_uri  # noqa: PLC0415

    parsed = _parse_uri("corlinman://memory/kb/abc")
    assert isinstance(parsed, _MemoryUri)
    assert parsed.host == "kb"
    assert parsed.id == "abc"

    parsed = _parse_uri("corlinman://skill/foo")
    assert isinstance(parsed, _SkillUri)
    assert parsed.name == "foo"

    parsed = _parse_uri("corlinman://persona/u1/snapshot")
    assert isinstance(parsed, _PersonaUri)
    assert parsed.user_id == "u1"

    assert _parse_uri("corlinman://persona/u1/other") is None
    assert _parse_uri("corlinman://memory/kb/") is None
    assert _parse_uri("https://example.com/x") is None
