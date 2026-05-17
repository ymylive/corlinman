"""Tests for ``corlinman_providers.plugins.registry``.

Ported from the inline tests in
``rust/crates/corlinman-plugins/src/registry.rs``.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from corlinman_providers.plugins.discovery import Origin, SearchRoot
from corlinman_providers.plugins.manifest import MANIFEST_FILENAME, PluginManifest
from corlinman_providers.plugins.registry import (
    NameCollisionDiagnostic,
    ParseErrorDiagnostic,
    PluginEntry,
    PluginRegistry,
)


def _body(name: str, version: str) -> str:
    return textwrap.dedent(
        f"""
        name = "{name}"
        version = "{version}"
        plugin_type = "sync"
        [entry_point]
        command = "true"
        """
    ).strip()


def _scratch_manifest(root: Path, plugin: str, body: str) -> Path:
    plugin_dir = root / plugin
    plugin_dir.mkdir(parents=True, exist_ok=True)
    p = plugin_dir / MANIFEST_FILENAME
    p.write_text(body, encoding="utf-8")
    return p


def test_higher_origin_wins_lower_becomes_collision_diag(tmp_path: Path) -> None:
    low = tmp_path / "low"
    high = tmp_path / "high"
    low.mkdir()
    high.mkdir()

    _scratch_manifest(low, "shared", _body("shared", "0.0.1"))
    _scratch_manifest(high, "shared", _body("shared", "9.9.9"))

    reg = PluginRegistry.from_roots(
        [
            SearchRoot(path=low, origin=Origin.BUNDLED),
            SearchRoot(path=high, origin=Origin.CONFIG),
        ]
    )

    entry = reg.get("shared")
    assert entry is not None
    assert entry.manifest.version == "9.9.9"
    assert entry.origin == Origin.CONFIG

    diags = reg.diagnostics()
    name_collisions = [d for d in diags if isinstance(d, NameCollisionDiagnostic)]
    assert len(name_collisions) == 1
    assert name_collisions[0].name == "shared"
    assert name_collisions[0].loser_origin == Origin.BUNDLED


@pytest.mark.asyncio
async def test_upsert_then_remove_round_trips() -> None:
    reg = PluginRegistry()
    assert reg.is_empty()

    import tomllib

    manifest = PluginManifest.model_validate(tomllib.loads(_body("alpha", "0.1.0")))
    manifest.migrate_to_current_in_memory()
    entry = PluginEntry(
        manifest=manifest,
        origin=Origin.WORKSPACE,
        manifest_path=Path("/tmp/alpha/plugin-manifest.toml"),
    )
    await reg.upsert(entry)
    assert len(reg) == 1
    looked_up = reg.get("alpha")
    assert looked_up is not None
    assert looked_up.manifest.version == "0.1.0"

    prev = await reg.remove("alpha")
    assert prev is not None
    assert prev.manifest.name == "alpha"
    assert reg.get("alpha") is None
    assert reg.is_empty()


@pytest.mark.asyncio
async def test_set_diagnostics_replaces_snapshot() -> None:
    reg = PluginRegistry()
    assert reg.diagnostics() == []
    await reg.set_diagnostics(
        [
            ParseErrorDiagnostic(
                path=Path("/tmp/bad/plugin-manifest.toml"),
                origin=Origin.CONFIG,
                message="bad",
            )
        ]
    )
    assert len(reg.diagnostics()) == 1


def test_list_returns_alphabetical_order(tmp_path: Path) -> None:
    _scratch_manifest(tmp_path, "z-last", _body("z-last", "0.1.0"))
    _scratch_manifest(tmp_path, "a-first", _body("a-first", "0.1.0"))
    _scratch_manifest(tmp_path, "m-middle", _body("m-middle", "0.1.0"))

    reg = PluginRegistry.from_roots([SearchRoot(path=tmp_path, origin=Origin.CONFIG)])
    names = [e.manifest.name for e in reg.list()]
    assert names == ["a-first", "m-middle", "z-last"]
