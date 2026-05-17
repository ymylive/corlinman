"""Shared fixtures for corlinman-mcp-server tests."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def fixtures_dir() -> Path:
    """Directory containing the shared MCP fixtures (copied verbatim
    from the Rust crate so both ports exercise the same files).
    """
    return Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------
# In-memory test doubles for the plugin/memory/skill protocols
# ---------------------------------------------------------------------


@dataclass
class StubSkill:
    """In-memory skill matching the ``SkillEntry`` protocol shape."""

    name: str
    description: str = ""
    body_markdown: str = ""


class StubSkillRegistry:
    """Minimal registry implementing the slice of the
    ``corlinman_skills_registry.SkillRegistry`` surface the MCP adapters
    consume."""

    def __init__(self, skills: list[StubSkill] | None = None) -> None:
        self._skills: dict[str, StubSkill] = {}
        for s in skills or []:
            self._skills[s.name] = s

    def add(self, skill: StubSkill) -> None:
        self._skills[skill.name] = skill

    def get(self, name: str) -> StubSkill | None:
        return self._skills.get(name)

    def iter(self) -> Iterator[StubSkill]:
        return iter(self._skills.values())

    def __iter__(self) -> Iterator[StubSkill]:
        return iter(self._skills.values())

    def __len__(self) -> int:
        return len(self._skills)


@dataclass
class StubPluginTool:
    name: str
    description: str = ""
    parameters: Any = field(default_factory=lambda: {"type": "object"})


def make_plugin_entry(name: str, tools: list[tuple[str, str]]):
    """Build a :class:`PluginEntry` from a name + (tool_name, desc)
    pairs. Returns the entry value directly to avoid extra imports in
    test files."""
    from corlinman_mcp_server.bridges import (
        PluginCapabilities,
        PluginCommunication,
        PluginEntry,
        PluginEntryPoint,
        PluginManifest,
        PluginTool,
    )

    entry = PluginEntry(
        manifest=PluginManifest(
            name=name,
            version="0.1.0",
            description="stub",
            entry_point=PluginEntryPoint(command="true", args=[], env={}),
            communication=PluginCommunication(timeout_ms=2_000),
            capabilities=PluginCapabilities(
                tools=[
                    PluginTool(
                        name=tn,
                        description=desc,
                        parameters={"type": "object"},
                    )
                    for tn, desc in tools
                ]
            ),
        ),
        manifest_path=Path("/tmp/stub/plugin-manifest.toml"),
    )
    return entry


class StubPluginRegistry:
    """Read-only registry mirroring the slice of the Rust
    ``PluginRegistry`` surface the MCP server consumes."""

    def __init__(self) -> None:
        self._entries: dict[str, Any] = {}

    def add(self, entry: Any) -> None:
        self._entries[entry.manifest.name] = entry

    def list(self) -> list[Any]:
        return list(self._entries.values())

    def get(self, name: str) -> Any:
        return self._entries.get(name)


class StubMemoryHost:
    """In-memory :class:`MemoryHost` keyed by id → content."""

    def __init__(self, name: str, seed: dict[str, str] | None = None) -> None:
        self._name = name
        self._rows: dict[str, str] = dict(seed) if seed else {}

    def name(self) -> str:
        return self._name

    async def query(self, req):  # noqa: ARG002
        from corlinman_mcp_server.bridges import MemoryHit

        return [
            MemoryHit(
                id=k,
                content=v,
                score=0.5,
                source=self._name,
                metadata=None,
            )
            for k, v in self._rows.items()
        ]

    async def upsert(self, doc) -> str:  # noqa: ARG002
        raise NotImplementedError

    async def delete(self, id: str) -> None:  # noqa: ARG002
        raise NotImplementedError

    async def get(self, id: str):
        from corlinman_mcp_server.bridges import MemoryHit

        content = self._rows.get(id)
        if content is None:
            return None
        return MemoryHit(
            id=id,
            content=content,
            score=1.0,
            source=self._name,
            metadata=None,
        )


class StubPluginRuntime:
    """Stub runtime that records its inputs and returns a configured
    :class:`PluginOutput`."""

    def __init__(self, outcome, progress_emit: tuple[str, float | None] | None = None) -> None:
        self.outcome = outcome
        self.seen: list[Any] = []
        self.progress_emit = progress_emit

    async def execute(self, input_, progress, cancel):  # noqa: ARG002
        self.seen.append(input_)
        if self.progress_emit is not None and progress is not None:
            msg, frac = self.progress_emit
            await progress.emit(msg, frac)
        return self.outcome

    def kind(self) -> str:
        return "stub"


class StubPersonaProvider:
    def __init__(self, ids: list[str] | None = None, snap: dict[str, Any] | None = None) -> None:
        self._ids = list(ids) if ids else []
        self._snap = dict(snap) if snap else {}

    async def list_user_ids(self) -> list[str]:
        return list(self._ids)

    async def read_snapshot(self, user_id: str):
        return self._snap.get(user_id)
