"""PromptsAdapter — mirrors ``src/adapters/prompts.rs`` tests."""

from __future__ import annotations

import pytest

from corlinman_mcp_server import (
    McpInvalidParamsError,
    McpMethodNotFoundError,
    PromptRole,
    PromptsAdapter,
    PromptsGetParams,
    PromptsListResult,
    SessionContext,
)
from corlinman_mcp_server.types import PromptTextContent

from .conftest import StubSkill, StubSkillRegistry


@pytest.mark.asyncio
async def test_list_returns_one_prompt_per_skill_sorted():
    reg = StubSkillRegistry(
        [
            StubSkill(name="zeta-skill", description="z desc", body_markdown="Z body"),
            StubSkill(name="alpha-skill", description="a desc", body_markdown="A body"),
        ]
    )
    adapter = PromptsAdapter(reg)
    result = adapter.list_prompts(SessionContext.permissive())
    names = [p.name for p in result.prompts]
    assert names == ["alpha-skill", "zeta-skill"]
    assert result.prompts[0].arguments == []
    assert result.prompts[0].description == "a desc"


@pytest.mark.asyncio
async def test_list_filters_by_allowlist():
    reg = StubSkillRegistry(
        [
            StubSkill(name="kb-search", description="x", body_markdown="x"),
            StubSkill(name="kb-summary", description="x", body_markdown="x"),
            StubSkill(name="other-thing", description="x", body_markdown="x"),
        ]
    )
    adapter = PromptsAdapter(reg)
    ctx = SessionContext(prompts_allowed=["kb-*"])
    result = adapter.list_prompts(ctx)
    names = [p.name for p in result.prompts]
    assert names == ["kb-search", "kb-summary"]


def test_get_returns_skill_body_as_user_message():
    reg = StubSkillRegistry(
        [
            StubSkill(
                name="foo",
                description="foo desc",
                body_markdown="Step 1.\nStep 2.",
            )
        ]
    )
    adapter = PromptsAdapter(reg)
    result = adapter.get_prompt(
        PromptsGetParams(name="foo", arguments=None),
        SessionContext.permissive(),
    )
    assert result.description == "foo desc"
    assert len(result.messages) == 1
    assert result.messages[0].role is PromptRole.USER
    assert isinstance(result.messages[0].content, PromptTextContent)
    assert "Step 1." in result.messages[0].content.text
    assert "Step 2." in result.messages[0].content.text


def test_get_unknown_name_returns_invalid_params_with_name_echoed():
    reg = StubSkillRegistry([StubSkill(name="foo", description="x", body_markdown="x")])
    adapter = PromptsAdapter(reg)
    with pytest.raises(McpInvalidParamsError) as exc:
        adapter.get_prompt(
            PromptsGetParams(name="ghost", arguments=None),
            SessionContext.permissive(),
        )
    assert exc.value.jsonrpc_code() == -32602
    assert "ghost" in exc.value.message
    assert exc.value.data == {"name": "ghost"}


def test_get_disallowed_name_returns_invalid_params_with_distinct_message():
    reg = StubSkillRegistry([StubSkill(name="foo", description="x", body_markdown="x")])
    adapter = PromptsAdapter(reg)
    ctx = SessionContext(prompts_allowed=["other-*"])
    with pytest.raises(McpInvalidParamsError) as exc:
        adapter.get_prompt(
            PromptsGetParams(name="foo", arguments=None),
            ctx,
        )
    assert exc.value.jsonrpc_code() == -32602
    assert "not allowed" in exc.value.message
    assert exc.value.data == {"name": "foo"}


@pytest.mark.asyncio
async def test_handle_routes_through_capability_adapter():
    reg = StubSkillRegistry(
        [StubSkill(name="foo", description="desc", body_markdown="body")]
    )
    adapter = PromptsAdapter(reg)
    assert adapter.capability_name() == "prompts"

    value = await adapter.handle("prompts/list", None, SessionContext.permissive())
    parsed = PromptsListResult.model_validate(value)
    assert len(parsed.prompts) == 1
    assert parsed.prompts[0].name == "foo"

    with pytest.raises(McpMethodNotFoundError):
        await adapter.handle("prompts/bogus", None, SessionContext.permissive())
