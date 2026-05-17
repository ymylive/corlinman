"""Tests for ``corlinman_providers.plugins.discovery``.

Ported from the inline tests in
``rust/crates/corlinman-plugins/src/discovery.rs``.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from corlinman_providers.plugins.discovery import (
    Origin,
    SearchRoot,
    discover,
    roots_from_env_var,
)
from corlinman_providers.plugins.manifest import MANIFEST_FILENAME


def _write_manifest(root: Path, name: str, body: str) -> Path:
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    path = plugin_dir / MANIFEST_FILENAME
    path.write_text(body, encoding="utf-8")
    return path


def _minimal(name: str) -> str:
    return textwrap.dedent(
        f"""
        name = "{name}"
        version = "0.1.0"
        plugin_type = "sync"
        [entry_point]
        command = "true"
        """
    ).strip()


def test_discovers_well_formed_manifests(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "alpha", _minimal("alpha"))
    _write_manifest(tmp_path, "beta", _minimal("beta"))

    result = discover([SearchRoot(path=tmp_path, origin=Origin.WORKSPACE)])

    assert len(result.plugins) == 2
    assert result.diagnostics == []
    names = sorted(p.manifest.name for p in result.plugins)
    assert names == ["alpha", "beta"]


def test_bad_manifest_becomes_diagnostic_not_panic(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "good", _minimal("good"))
    _write_manifest(tmp_path, "bad", "not = valid = toml")

    result = discover([SearchRoot(path=tmp_path, origin=Origin.CONFIG)])

    assert len(result.plugins) == 1
    assert result.plugins[0].manifest.name == "good"
    assert len(result.diagnostics) == 1
    assert "parse" in result.diagnostics[0].message.lower() or (
        "toml" in result.diagnostics[0].message.lower()
    )


def test_missing_search_root_is_silent() -> None:
    result = discover(
        [SearchRoot(path=Path("/tmp/definitely-does-not-exist-corlinman"), origin=Origin.GLOBAL)]
    )
    assert result.plugins == []
    assert result.diagnostics == []


def test_origin_rank_matches_precedence_order() -> None:
    assert Origin.BUNDLED.rank < Origin.GLOBAL.rank
    assert Origin.GLOBAL.rank < Origin.WORKSPACE.rank
    assert Origin.WORKSPACE.rank < Origin.CONFIG.rank


def test_plugin_dir_is_manifest_parent(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "alpha", _minimal("alpha"))
    result = discover([SearchRoot(path=tmp_path, origin=Origin.WORKSPACE)])
    assert result.plugins[0].plugin_dir() == tmp_path / "alpha"


def test_roots_from_env_var(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CORLINMAN_TEST_PLUGINS", f"/a/b:{tmp_path}::/c/d")
    roots = roots_from_env_var("CORLINMAN_TEST_PLUGINS", Origin.CONFIG)
    assert len(roots) == 3
    for r in roots:
        assert r.origin == Origin.CONFIG


def test_roots_from_env_var_unset_returns_empty(monkeypatch) -> None:
    monkeypatch.delenv("CORLINMAN_TEST_PLUGINS_NOT_SET", raising=False)
    assert roots_from_env_var("CORLINMAN_TEST_PLUGINS_NOT_SET", Origin.CONFIG) == []
