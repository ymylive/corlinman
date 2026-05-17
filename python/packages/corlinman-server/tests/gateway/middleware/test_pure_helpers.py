"""Pure-helper tests for the gateway middleware port.

These mirror the Rust ``#[test]`` (non-`tokio::test`) helpers — they
exercise the byte-shapes of the parsing / matching layer without
needing a live AdminDb or queue. The end-to-end FastAPI-driven tests
live in sibling ``test_*_middleware.py`` files.
"""

from __future__ import annotations

import base64

from corlinman_server.gateway.middleware import (
    SESSION_COOKIE_NAME,
    ApprovalMode,
    ApprovalRule,
    RuleMatchKind,
    extract_cookie,
    extract_tenant_query,
    match_rule,
    parse_basic,
)


# ---------------------------------------------------------------------------
# admin_auth.parse_basic / extract_cookie — mirror the Rust unit tests.
# ---------------------------------------------------------------------------


def test_parse_basic_accepts_well_formed() -> None:
    raw = base64.b64encode(b"alice:hunter2").decode()
    assert parse_basic(f"Basic {raw}") == ("alice", "hunter2")


def test_parse_basic_is_case_insensitive_scheme() -> None:
    raw = base64.b64encode(b"alice:hunter2").decode()
    assert parse_basic(f"basic {raw}") == ("alice", "hunter2")


def test_parse_basic_rejects_non_basic() -> None:
    assert parse_basic("Bearer xyz") is None


def test_parse_basic_rejects_malformed_base64() -> None:
    assert parse_basic("Basic @@@not-base64@@@") is None


def test_parse_basic_rejects_missing_colon() -> None:
    raw = base64.b64encode(b"nocolon").decode()
    assert parse_basic(f"Basic {raw}") is None


def test_extract_cookie_finds_named_value() -> None:
    header = f"foo=bar; {SESSION_COOKIE_NAME}=abc123"
    assert extract_cookie(header, SESSION_COOKIE_NAME) == "abc123"


def test_extract_cookie_returns_none_when_absent() -> None:
    assert extract_cookie("foo=bar", SESSION_COOKIE_NAME) is None


def test_extract_cookie_handles_single_pair() -> None:
    assert extract_cookie(f"{SESSION_COOKIE_NAME}=xyz", SESSION_COOKIE_NAME) == "xyz"


# ---------------------------------------------------------------------------
# tenant_scope.extract_tenant_query — mirror the Rust unit test.
# ---------------------------------------------------------------------------


def test_extract_tenant_query_finds_first_match() -> None:
    assert extract_tenant_query("tenant=acme") == "acme"
    assert extract_tenant_query("foo=1&tenant=bravo&bar=2") == "bravo"
    assert extract_tenant_query("foo=1&bar=2") is None
    assert extract_tenant_query("") is None
    # Tolerates over-encoded `-` even though slugs don't strictly need it.
    assert extract_tenant_query("tenant=ac%2Dme") == "ac-me"


# ---------------------------------------------------------------------------
# approval.match_rule — mirror the Rust ``match_rule_impl`` table tests.
# ---------------------------------------------------------------------------


def test_match_rule_exact_beats_plugin_wide() -> None:
    rules = [
        ApprovalRule(plugin="file-ops", tool=None, mode=ApprovalMode.AUTO),
        ApprovalRule(plugin="file-ops", tool="write", mode=ApprovalMode.DENY),
    ]
    got = match_rule(rules, "file-ops", "write", "s1")
    assert got.kind is RuleMatchKind.MATCHED_DENY

    got = match_rule(rules, "file-ops", "read", "s1")
    assert got.kind is RuleMatchKind.MATCHED_AUTO


def test_match_rule_plugin_only_applies_to_all_tools() -> None:
    rules = [ApprovalRule(plugin="shell", tool=None, mode=ApprovalMode.PROMPT)]
    for tool in ("exec", "spawn", "whatever"):
        assert match_rule(rules, "shell", tool, "s1").kind is RuleMatchKind.MATCHED_PROMPT


def test_match_rule_allow_session_keys_short_circuits_prompt() -> None:
    rules = [
        ApprovalRule(
            plugin="shell",
            tool=None,
            mode=ApprovalMode.PROMPT,
            allow_session_keys=("trusted-session",),
        )
    ]
    assert (
        match_rule(rules, "shell", "exec", "trusted-session").kind
        is RuleMatchKind.MATCHED_WHITELIST
    )
    assert (
        match_rule(rules, "shell", "exec", "random-session").kind
        is RuleMatchKind.MATCHED_PROMPT
    )


def test_match_rule_no_match_when_plugin_absent() -> None:
    rules = [ApprovalRule(plugin="file-ops", tool=None, mode=ApprovalMode.DENY)]
    assert match_rule(rules, "calendar", "add", "s1").kind is RuleMatchKind.NO_MATCH


def test_match_rule_allow_session_keys_ignored_for_non_prompt_modes() -> None:
    rules = [
        ApprovalRule(
            plugin="shell",
            tool=None,
            mode=ApprovalMode.DENY,
            allow_session_keys=("trusted-session",),
        )
    ]
    got = match_rule(rules, "shell", "exec", "trusted-session")
    assert got.kind is RuleMatchKind.MATCHED_DENY
