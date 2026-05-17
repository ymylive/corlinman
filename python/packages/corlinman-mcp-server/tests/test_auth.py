"""Token ACL + ``resolve_token`` — mirrors ``src/server/auth.rs`` tests."""

from __future__ import annotations

from corlinman_mcp_server import DEFAULT_TENANT_ID, TokenAcl, resolve_token


def fixture() -> list[TokenAcl]:
    return [
        TokenAcl(
            token="alpha-token",
            label="alpha-laptop",
            tools_allowlist=["kb:*"],
            resources_allowed=["skill"],
            prompts_allowed=["*"],
            tenant_id="alpha",
        ),
        TokenAcl(
            token="beta-token",
            label="beta-server",
            tools_allowlist=["web_search"],
            resources_allowed=["*"],
            prompts_allowed=[],
            tenant_id=None,  # → default
        ),
    ]


def test_resolve_returns_matching_acl():
    acl = resolve_token(fixture(), "alpha-token")
    assert acl is not None
    assert acl.label == "alpha-laptop"
    assert acl.tenant_id == "alpha"


def test_resolve_returns_none_for_unknown_token():
    assert resolve_token(fixture(), "ghost") is None


def test_empty_string_token_never_resolves():
    assert resolve_token(fixture(), "") is None


def test_empty_acl_list_resolves_nothing_fail_closed():
    assert resolve_token([], "alpha-token") is None


def test_missing_tenant_falls_back_to_default_constant():
    acl = resolve_token(fixture(), "beta-token")
    assert acl is not None
    assert acl.effective_tenant() == DEFAULT_TENANT_ID == "default"


def test_empty_tenant_string_also_falls_back_to_default():
    acl = TokenAcl.permissive("t")
    acl.tenant_id = ""
    assert acl.effective_tenant() == DEFAULT_TENANT_ID


def test_to_session_context_carries_allowlists_and_tenant():
    alpha = resolve_token(fixture(), "alpha-token")
    assert alpha is not None
    ctx = alpha.to_session_context()
    assert ctx.tools_allowlist == ["kb:*"]
    assert ctx.resources_allowed == ["skill"]
    assert ctx.prompts_allowed == ["*"]
    assert ctx.tenant_id == "alpha"

    # Empty prompts list → closed (the adapter denies any name).
    beta = resolve_token(fixture(), "beta-token")
    assert beta is not None
    bctx = beta.to_session_context()
    assert bctx.prompts_allowed == []
    assert not bctx.allows_prompt("any-name")
    assert bctx.tenant_id == "default"


def test_permissive_helper_grants_all_capabilities():
    acl = TokenAcl.permissive("dev")
    ctx = acl.to_session_context()
    assert ctx.allows_tool("anything:any")
    assert ctx.allows_resource_scheme("memory")
    assert ctx.allows_prompt("any-skill")
    assert ctx.tenant_id == "default"
