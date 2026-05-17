"""Capability-adapter substrate tests — ``glob_match`` +
``SessionContext`` (mirrors ``src/adapters/mod.rs``)."""

from __future__ import annotations

from corlinman_mcp_server import SessionContext, glob_match


def test_glob_star_matches_anything():
    assert glob_match("*", "anything")
    assert glob_match("*", "")


def test_glob_exact_requires_exact():
    assert glob_match("kb.search", "kb.search")
    assert not glob_match("kb.search", "kb.searcher")


def test_glob_prefix_star_matches_prefix():
    assert glob_match("kb.*", "kb.search")
    assert glob_match("kb.*", "kb.")
    assert not glob_match("kb.*", "other.search")


def test_glob_star_suffix_matches_suffix():
    assert glob_match("*.json", "doc.json")
    assert not glob_match("*.json", "doc.json.bak")


def test_glob_middle_star_threads_substring():
    assert glob_match("foo*bar", "foozzbar")
    assert glob_match("foo*bar", "foobar")
    assert not glob_match("foo*bar", "foobaz")


def test_empty_allowlist_denies_everything():
    ctx = SessionContext()
    assert not ctx.allows_tool("anything")
    assert not ctx.allows_resource_scheme("memory")
    assert not ctx.allows_prompt("any")


def test_permissive_allows_everything():
    ctx = SessionContext.permissive()
    assert ctx.allows_tool("kb:search")
    assert ctx.allows_resource_scheme("memory")
    assert ctx.allows_prompt("any-skill")
