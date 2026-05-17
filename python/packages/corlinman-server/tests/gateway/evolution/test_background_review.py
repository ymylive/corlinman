"""Tests for :mod:`corlinman_server.gateway.evolution.background_review`.

Covers:

* whitelist enforcement — non-whitelisted tools are dropped, no disk
  writes occur;
* skill_manage create/edit/patch/delete happy paths + provenance stamping;
* delete-on-pinned / delete-on-non-agent refused with the right reason;
* memory_write append to MEMORY.md and USER.md with timestamp;
* path-traversal defence — unsafe skill names dropped before any write;
* timeout handling — the report returns ``error="timeout"``;
* provider failure — RuntimeError surfaces as ``error="provider_failure: ..."``;
* user-correction kind — the embedded correction text appears in the
  prompt the fake provider receives.

The tests use a hand-rolled fake provider (``ScriptedProvider``) that
exposes both ``chat`` (the dispatch-friendly path) and ``chat_stream``
(the canonical Protocol path) so we can exercise either branch without
juggling the OpenAI streaming envelope.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from corlinman_providers.base import ProviderChunk
from corlinman_providers.mock import MockProvider
from corlinman_server.gateway.evolution.background_review import (
    WHITELISTED_TOOLS,
    BackgroundReviewReport,
    ReviewWriteRecord,
    _apply_tool_calls,
    load_prompt,
    spawn_background_review,
)
from corlinman_skills_registry import (
    Skill,
    SkillRegistry,
    SkillRequirements,
    write_skill_md,
)

# ─── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def profile_root(tmp_path: Path) -> Path:
    """A fresh profile directory layout under tmp_path."""
    root = tmp_path / "profiles" / "alice"
    (root / "skills").mkdir(parents=True, exist_ok=True)
    return root


def _empty_registry(profile_root: Path) -> SkillRegistry:
    return SkillRegistry.load_from_dir(profile_root / "skills")


def _seed_skill(
    profile_root: Path,
    name: str,
    *,
    body: str = "# Seed\n",
    origin: str = "agent-created",
    pinned: bool = False,
    version: str = "1.0.0",
) -> Path:
    """Drop a SKILL.md into ``profile_root/skills/<name>/`` and return its path."""
    skill_dir = profile_root / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    md_path = skill_dir / "SKILL.md"
    skill = Skill(
        name=name,
        description=f"seed for {name}",
        requires=SkillRequirements(),
        allowed_tools=[],
        body_markdown=body,
        source_path=md_path,
        version=version,
        origin=origin,  # type: ignore[arg-type]
        state="active",
        pinned=pinned,
    )
    write_skill_md(md_path, skill)
    return md_path


# ─── Scripted providers ──────────────────────────────────────────────


class ScriptedProvider:
    """Fake provider that returns a fixed list of tool_calls on every chat call.

    Exposes ``chat`` (the non-streaming shortcut the background review's
    adapter prefers when available). Tests use it to feed deterministic
    tool_calls into the dispatcher.
    """

    def __init__(self, tool_calls: list[dict[str, Any]]) -> None:
        self._tool_calls = tool_calls
        self.last_messages: list[dict[str, Any]] | None = None

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        self.last_messages = list(messages)
        return {"tool_calls": list(self._tool_calls)}


class SlowProvider:
    """Sleeps long enough to trip the spawn_background_review timeout."""

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        await asyncio.sleep(10)
        return {"tool_calls": []}


class RaisingProvider:
    """Raises a RuntimeError on every chat call."""

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        raise RuntimeError("upstream exploded")


class StreamingScriptedProvider:
    """Scripted provider exercising the chat_stream path.

    Emits the canonical ``tool_call_start`` / ``tool_call_delta`` /
    ``tool_call_end`` / ``done`` sequence so we cover the stream-assembly
    branch of ``_invoke_provider`` as well as the ``chat`` branch.
    """

    def __init__(self, tool_calls: list[dict[str, Any]]) -> None:
        self._tool_calls = tool_calls

    def chat_stream(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[ProviderChunk]:
        async def _gen() -> AsyncIterator[ProviderChunk]:
            for idx, call in enumerate(self._tool_calls):
                tcid = f"call_{idx}"
                name = call.pop("tool", "")
                args_json = json.dumps(call)
                yield ProviderChunk(
                    kind="tool_call_start",
                    tool_call_id=tcid,
                    tool_name=name,
                    arguments_delta="",
                )
                yield ProviderChunk(
                    kind="tool_call_delta",
                    tool_call_id=tcid,
                    arguments_delta=args_json,
                )
                yield ProviderChunk(kind="tool_call_end", tool_call_id=tcid)
            yield ProviderChunk(kind="done", finish_reason="tool_calls")

        return _gen()


# ─── Smoke ────────────────────────────────────────────────────────────


def test_whitelist_constant_only_skill_manage_and_memory_write() -> None:
    """Invariant: only the two whitelisted tool names are allowed."""
    assert frozenset({"skill_manage", "memory_write"}) == WHITELISTED_TOOLS


def test_load_prompt_each_kind_produces_non_empty_string() -> None:
    for kind in ("memory", "skill", "combined", "curator", "user-correction"):
        body = load_prompt(kind)  # type: ignore[arg-type]
        assert "Output ONLY tool_calls" in body, kind
        assert "tool_calls" in body, kind


def test_load_prompt_unknown_kind_raises() -> None:
    with pytest.raises(ValueError):
        load_prompt("garbage")  # type: ignore[arg-type]


# ─── Provider-roundtrip tests ────────────────────────────────────────


async def test_empty_provider_response_yields_empty_report(profile_root: Path) -> None:
    """MockProvider emits no tool_calls — report is empty with no error."""
    registry = _empty_registry(profile_root)
    report = await spawn_background_review(
        kind="memory",
        profile_slug="alice",
        profile_root=profile_root,
        recent_messages=[{"role": "user", "content": "hello"}],
        registry=registry,
        provider=MockProvider(),
        model="mock",
    )
    assert isinstance(report, BackgroundReviewReport)
    assert report.error is None
    assert report.writes == []
    assert report.applied_count == 0


async def test_non_whitelisted_tool_is_dropped(profile_root: Path) -> None:
    """A ``terminal`` tool call is dropped; no skills appear on disk."""
    registry = _empty_registry(profile_root)
    provider = ScriptedProvider([
        {"tool": "terminal", "command": "rm -rf /"},
        {"tool": "web_fetch", "url": "http://evil.example"},
    ])
    report = await spawn_background_review(
        kind="combined",
        profile_slug="alice",
        profile_root=profile_root,
        recent_messages=[],
        registry=registry,
        provider=provider,
        model="mock",
    )
    assert report.error is None
    assert len(report.writes) == 2
    for w in report.writes:
        assert w.applied is False
        assert w.skipped_reason == "not_whitelisted"
    # No file leaked onto disk.
    assert list((profile_root / "skills").rglob("*.md")) == []
    assert not (profile_root / "MEMORY.md").exists()
    assert not (profile_root / "USER.md").exists()


async def test_skill_manage_create_writes_agent_created_skill(profile_root: Path) -> None:
    registry = _empty_registry(profile_root)
    provider = ScriptedProvider([
        {
            "tool": "skill_manage",
            "action": "create",
            "name": "test",
            "content": "# Hi\n\nBody body body.\n",
        }
    ])
    report = await spawn_background_review(
        kind="skill",
        profile_slug="alice",
        profile_root=profile_root,
        recent_messages=[],
        registry=registry,
        provider=provider,
        model="mock",
    )
    assert report.error is None
    assert report.applied_count == 1

    md_path = profile_root / "skills" / "test" / "SKILL.md"
    assert md_path.exists()

    fresh = SkillRegistry.load_from_dir(profile_root / "skills")
    skill = fresh.get("test")
    assert skill is not None
    assert skill.origin == "agent-created"
    assert skill.version == "1.0.0"
    assert skill.state == "active"
    assert "Hi" in skill.body_markdown


async def test_skill_manage_patch_bumps_version(profile_root: Path) -> None:
    md_path = _seed_skill(profile_root, "lookup", body="# Lookup\nold-line\n")
    registry = SkillRegistry.load_from_dir(profile_root / "skills")

    provider = ScriptedProvider([
        {
            "tool": "skill_manage",
            "action": "patch",
            "name": "lookup",
            "find": "old-line",
            "replace": "new-line",
        }
    ])
    report = await spawn_background_review(
        kind="skill",
        profile_slug="alice",
        profile_root=profile_root,
        recent_messages=[],
        registry=registry,
        provider=provider,
        model="mock",
    )
    assert report.error is None
    assert report.applied_count == 1

    fresh = SkillRegistry.load_from_dir(profile_root / "skills")
    skill = fresh.get("lookup")
    assert skill is not None
    assert skill.version == "1.0.1"
    assert "new-line" in skill.body_markdown
    assert "old-line" not in skill.body_markdown
    assert md_path.exists()


async def test_skill_manage_edit_replaces_body(profile_root: Path) -> None:
    _seed_skill(profile_root, "abc", body="# Old body\n")
    registry = SkillRegistry.load_from_dir(profile_root / "skills")

    provider = ScriptedProvider([
        {
            "tool": "skill_manage",
            "action": "edit",
            "name": "abc",
            "content": "# Brand new body\n\nFresh content.\n",
        }
    ])
    report = await spawn_background_review(
        kind="skill",
        profile_slug="alice",
        profile_root=profile_root,
        recent_messages=[],
        registry=registry,
        provider=provider,
        model="mock",
    )
    assert report.error is None
    assert report.applied_count == 1

    fresh = SkillRegistry.load_from_dir(profile_root / "skills")
    skill = fresh.get("abc")
    assert skill is not None
    assert "Brand new body" in skill.body_markdown
    assert skill.version == "1.0.1"


async def test_skill_manage_delete_on_pinned_refused(profile_root: Path) -> None:
    md_path = _seed_skill(profile_root, "important", pinned=True)
    registry = SkillRegistry.load_from_dir(profile_root / "skills")

    provider = ScriptedProvider([
        {"tool": "skill_manage", "action": "delete", "name": "important"}
    ])
    report = await spawn_background_review(
        kind="curator",
        profile_slug="alice",
        profile_root=profile_root,
        recent_messages=[],
        registry=registry,
        provider=provider,
        model="mock",
    )
    assert report.error is None
    assert len(report.writes) == 1
    assert report.writes[0].applied is False
    assert report.writes[0].skipped_reason == "protected"
    # File survives the refused delete.
    assert md_path.exists()


async def test_skill_manage_delete_on_user_requested_refused(profile_root: Path) -> None:
    """Non-agent-created skills are protected even when not pinned."""
    md_path = _seed_skill(profile_root, "manual", origin="user-requested")
    registry = SkillRegistry.load_from_dir(profile_root / "skills")

    provider = ScriptedProvider([
        {"tool": "skill_manage", "action": "delete", "name": "manual"}
    ])
    report = await spawn_background_review(
        kind="curator",
        profile_slug="alice",
        profile_root=profile_root,
        recent_messages=[],
        registry=registry,
        provider=provider,
        model="mock",
    )
    assert report.writes[0].skipped_reason == "protected"
    assert md_path.exists()


async def test_skill_manage_delete_on_agent_created_succeeds(profile_root: Path) -> None:
    md_path = _seed_skill(profile_root, "throwaway", origin="agent-created")
    registry = SkillRegistry.load_from_dir(profile_root / "skills")

    provider = ScriptedProvider([
        {"tool": "skill_manage", "action": "delete", "name": "throwaway"}
    ])
    report = await spawn_background_review(
        kind="curator",
        profile_slug="alice",
        profile_root=profile_root,
        recent_messages=[],
        registry=registry,
        provider=provider,
        model="mock",
    )
    assert report.applied_count == 1
    assert not md_path.exists()


async def test_memory_write_append_to_memory_md_includes_timestamp(profile_root: Path) -> None:
    registry = _empty_registry(profile_root)
    provider = ScriptedProvider([
        {
            "tool": "memory_write",
            "target": "MEMORY",
            "action": "append",
            "content": "user prefers concise answers",
        }
    ])
    report = await spawn_background_review(
        kind="memory",
        profile_slug="alice",
        profile_root=profile_root,
        recent_messages=[],
        registry=registry,
        provider=provider,
        model="mock",
    )
    assert report.error is None
    assert report.applied_count == 1

    body = (profile_root / "MEMORY.md").read_text(encoding="utf-8")
    assert "user prefers concise answers" in body
    # Timestamp prefix shape: "- [YYYY-MM-DDTHH:MM:SS...] ..."
    assert body.lstrip().startswith("- [")


async def test_memory_write_append_to_user_md(profile_root: Path) -> None:
    registry = _empty_registry(profile_root)
    provider = ScriptedProvider([
        {
            "tool": "memory_write",
            "target": "USER",
            "action": "append",
            "content": "Ian is an AI engineer",
        }
    ])
    report = await spawn_background_review(
        kind="memory",
        profile_slug="alice",
        profile_root=profile_root,
        recent_messages=[],
        registry=registry,
        provider=provider,
        model="mock",
    )
    assert report.applied_count == 1
    body = (profile_root / "USER.md").read_text(encoding="utf-8")
    assert "Ian is an AI engineer" in body


async def test_memory_write_replace_overwrites(profile_root: Path) -> None:
    (profile_root / "MEMORY.md").write_text("stale junk\n", encoding="utf-8")
    registry = _empty_registry(profile_root)
    provider = ScriptedProvider([
        {
            "tool": "memory_write",
            "target": "MEMORY",
            "action": "replace",
            "content": "# Memory\n\nClean slate.\n",
        }
    ])
    report = await spawn_background_review(
        kind="memory",
        profile_slug="alice",
        profile_root=profile_root,
        recent_messages=[],
        registry=registry,
        provider=provider,
        model="mock",
    )
    assert report.applied_count == 1
    body = (profile_root / "MEMORY.md").read_text(encoding="utf-8")
    assert "stale junk" not in body
    assert "Clean slate." in body


async def test_memory_write_invalid_target_dropped(profile_root: Path) -> None:
    registry = _empty_registry(profile_root)
    provider = ScriptedProvider([
        {
            "tool": "memory_write",
            "target": "SOMETHING_ELSE",
            "action": "append",
            "content": "x",
        }
    ])
    report = await spawn_background_review(
        kind="memory",
        profile_slug="alice",
        profile_root=profile_root,
        recent_messages=[],
        registry=registry,
        provider=provider,
        model="mock",
    )
    assert report.writes[0].applied is False
    assert report.writes[0].skipped_reason == "invalid_target"


async def test_timeout_returns_error_report_no_writes(profile_root: Path) -> None:
    registry = _empty_registry(profile_root)
    report = await spawn_background_review(
        kind="memory",
        profile_slug="alice",
        profile_root=profile_root,
        recent_messages=[],
        registry=registry,
        provider=SlowProvider(),
        model="mock",
        timeout_seconds=0.05,
    )
    assert report.error == "timeout"
    assert report.writes == []


async def test_path_traversal_create_dropped(profile_root: Path) -> None:
    registry = _empty_registry(profile_root)
    provider = ScriptedProvider([
        {
            "tool": "skill_manage",
            "action": "create",
            "name": "../../etc/passwd",
            "content": "x",
        }
    ])
    report = await spawn_background_review(
        kind="skill",
        profile_slug="alice",
        profile_root=profile_root,
        recent_messages=[],
        registry=registry,
        provider=provider,
        model="mock",
    )
    assert report.error is None
    assert report.writes[0].applied is False
    assert report.writes[0].skipped_reason == "unsafe_name"
    # Nothing escaped to the parent dirs.
    assert not (profile_root.parent.parent / "etc" / "passwd").exists()
    # No SKILL.md materialised inside the skills dir either.
    assert list((profile_root / "skills").rglob("*.md")) == []


async def test_path_traversal_via_slash_dropped(profile_root: Path) -> None:
    """A name containing ``/`` is refused even without ``..``."""
    registry = _empty_registry(profile_root)
    provider = ScriptedProvider([
        {
            "tool": "skill_manage",
            "action": "create",
            "name": "sub/dir/sneaky",
            "content": "x",
        }
    ])
    report = await spawn_background_review(
        kind="skill",
        profile_slug="alice",
        profile_root=profile_root,
        recent_messages=[],
        registry=registry,
        provider=provider,
        model="mock",
    )
    assert report.writes[0].skipped_reason == "unsafe_name"


async def test_provider_raises_surfaces_error(profile_root: Path) -> None:
    registry = _empty_registry(profile_root)
    report = await spawn_background_review(
        kind="memory",
        profile_slug="alice",
        profile_root=profile_root,
        recent_messages=[],
        registry=registry,
        provider=RaisingProvider(),
        model="mock",
    )
    assert report.error is not None
    assert report.error.startswith("provider_failure")
    assert "upstream exploded" in report.error
    assert report.writes == []


async def test_user_correction_kind_embeds_text_in_prompt(profile_root: Path) -> None:
    registry = _empty_registry(profile_root)
    provider = ScriptedProvider([])
    correction = "stop using bullet points"
    await spawn_background_review(
        kind="user-correction",
        profile_slug="alice",
        profile_root=profile_root,
        recent_messages=[{"role": "user", "content": correction}],
        registry=registry,
        provider=provider,
        model="mock",
        user_correction_text=correction,
    )
    assert provider.last_messages is not None
    system_msg = next(m for m in provider.last_messages if m["role"] == "system")
    assert "User correction" in system_msg["content"]
    assert correction in system_msg["content"]
    # And the structured user envelope carries it too.
    user_msg = next(m for m in provider.last_messages if m["role"] == "user")
    assert correction in user_msg["content"]


async def test_chat_stream_path_assembles_tool_calls(profile_root: Path) -> None:
    """The stream-based provider path should produce the same writes as chat."""
    registry = _empty_registry(profile_root)
    provider = StreamingScriptedProvider([
        {
            "tool": "memory_write",
            "target": "MEMORY",
            "action": "append",
            "content": "streamed-line",
        }
    ])
    report = await spawn_background_review(
        kind="memory",
        profile_slug="alice",
        profile_root=profile_root,
        recent_messages=[],
        registry=registry,
        provider=provider,
        model="mock",
    )
    assert report.error is None
    assert report.applied_count == 1
    assert "streamed-line" in (profile_root / "MEMORY.md").read_text(encoding="utf-8")


# ─── Direct dispatcher tests ─────────────────────────────────────────


async def test_apply_tool_calls_malformed_returns_record(profile_root: Path) -> None:
    registry = _empty_registry(profile_root)
    records = await _apply_tool_calls(
        tool_calls=[42, "garbage", None],  # type: ignore[list-item]
        profile_root=profile_root,
        registry=registry,
    )
    assert len(records) == 3
    assert all(isinstance(r, ReviewWriteRecord) for r in records)
    assert all(not r.applied for r in records)
    assert all(r.skipped_reason == "malformed_tool_call" for r in records)


async def test_apply_tool_calls_openai_function_envelope(profile_root: Path) -> None:
    """The dispatcher accepts the OpenAI ``{"function": {...}}`` shape too."""
    registry = _empty_registry(profile_root)
    records = await _apply_tool_calls(
        tool_calls=[
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "memory_write",
                    "arguments": json.dumps(
                        {
                            "target": "MEMORY",
                            "action": "append",
                            "content": "openai envelope works",
                        }
                    ),
                },
            }
        ],
        profile_root=profile_root,
        registry=registry,
    )
    assert len(records) == 1
    assert records[0].applied is True
    body = (profile_root / "MEMORY.md").read_text(encoding="utf-8")
    assert "openai envelope works" in body


async def test_skill_patch_find_not_present_skipped(profile_root: Path) -> None:
    _seed_skill(profile_root, "doc", body="# A\nB\n")
    registry = SkillRegistry.load_from_dir(profile_root / "skills")
    provider = ScriptedProvider([
        {
            "tool": "skill_manage",
            "action": "patch",
            "name": "doc",
            "find": "NEVER_PRESENT",
            "replace": "xxx",
        }
    ])
    report = await spawn_background_review(
        kind="skill",
        profile_slug="alice",
        profile_root=profile_root,
        recent_messages=[],
        registry=registry,
        provider=provider,
        model="mock",
    )
    assert report.writes[0].skipped_reason == "find_not_in_body"
    assert report.applied_count == 0


async def test_skill_create_already_exists_skipped(profile_root: Path) -> None:
    _seed_skill(profile_root, "dupe")
    registry = SkillRegistry.load_from_dir(profile_root / "skills")
    provider = ScriptedProvider([
        {
            "tool": "skill_manage",
            "action": "create",
            "name": "dupe",
            "content": "# new",
        }
    ])
    report = await spawn_background_review(
        kind="skill",
        profile_slug="alice",
        profile_root=profile_root,
        recent_messages=[],
        registry=registry,
        provider=provider,
        model="mock",
    )
    assert report.writes[0].skipped_reason == "already_exists"
