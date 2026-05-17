"""Tests for ``corlinman_providers.plugins.lifecycle``.

Ported from the inline tokio tests in
``rust/crates/corlinman-plugins/src/supervisor.rs``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from corlinman_providers.plugins.lifecycle import (
    PluginSupervisor,
    PluginSupervisorError,
)
from corlinman_providers.plugins.manifest import EntryPoint, PluginManifest, PluginType


def _fake_manifest(name: str, command: str, args: list[str] | None = None) -> PluginManifest:
    return PluginManifest(
        manifest_version=2,
        name=name,
        version="0.1.0",
        plugin_type=PluginType.SERVICE,
        entry_point=EntryPoint(command=command, args=args or []),
    )


@pytest.mark.asyncio
async def test_spawn_missing_binary_returns_plugin_runtime_err(tmp_path: Path) -> None:
    sup = PluginSupervisor(tmp_path)
    m = _fake_manifest("ghost", "/nonexistent/binary/xyz-corlinman-test")
    with pytest.raises(PluginSupervisorError) as exc_info:
        await sup.spawn_service(m)
    assert "ghost" in str(exc_info.value)


@pytest.mark.asyncio
async def test_stop_on_unknown_plugin_is_noop(tmp_path: Path) -> None:
    sup = PluginSupervisor(tmp_path)
    await sup.stop_service("does-not-exist")
    assert sup.child_count() == 0


@pytest.mark.asyncio
async def test_spawn_and_stop_lifecycle(tmp_path: Path) -> None:
    sup = PluginSupervisor(tmp_path)
    # `cat` blocks on stdin so the child stays alive long enough for us to
    # observe + stop it.
    m = _fake_manifest("sleeper", "cat")
    socket_path = await sup.spawn_service(m)
    assert socket_path == tmp_path / "sleeper.sock"
    assert sup.child_count() == 1
    await sup.stop_service("sleeper")
    assert sup.child_count() == 0


@pytest.mark.asyncio
async def test_shutdown_kills_all_children(tmp_path: Path) -> None:
    sup = PluginSupervisor(tmp_path)
    await sup.spawn_service(_fake_manifest("a", "cat"))
    await sup.spawn_service(_fake_manifest("b", "cat"))
    assert sup.child_count() == 2
    await sup.shutdown()
    assert sup.child_count() == 0
    assert sup.is_shutting_down()


@pytest.mark.asyncio
async def test_spawn_after_shutdown_refused(tmp_path: Path) -> None:
    sup = PluginSupervisor(tmp_path)
    await sup.shutdown()
    with pytest.raises(PluginSupervisorError):
        await sup.spawn_service(_fake_manifest("late", "cat"))
