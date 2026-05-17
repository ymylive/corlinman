"""Smoke tests for :mod:`corlinman_server.gateway.lifecycle.entrypoint`.

The sibling submodules (``gateway.core`` / ``gateway.routes`` / ...) are
not present yet, so these tests only assert the entrypoint's
degraded-mode behaviour: the FastAPI app builds, the ``/health`` route
returns 200, and the CLI parser accepts the documented flags.

Once the sibling agents land, additional integration tests will be
added (or these will be promoted) to cover the full wired app.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from corlinman_server.gateway.lifecycle.entrypoint import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    SIGTERM_EXIT_CODE,
    _build_parser,
    _resolve_bind,
    _resolve_config_path,
    _resolve_data_dir,
    _should_run_legacy_migration,
    build_app,
)


def test_parser_accepts_documented_flags() -> None:
    parser = _build_parser()
    args = parser.parse_args(
        [
            "--config",
            "/tmp/cfg.toml",
            "--host",
            "0.0.0.0",
            "--port",
            "9999",
            "--data-dir",
            "/tmp/data",
            "--log-level",
            "debug",
        ]
    )
    assert args.config == "/tmp/cfg.toml"
    assert args.host == "0.0.0.0"
    assert args.port == 9999
    assert args.data_dir == "/tmp/data"
    assert args.log_level == "debug"


def test_resolve_bind_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BIND", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    assert _resolve_bind(None, None) == (DEFAULT_HOST, DEFAULT_PORT)

    monkeypatch.setenv("BIND", "0.0.0.0")
    monkeypatch.setenv("PORT", "7000")
    assert _resolve_bind(None, None) == ("0.0.0.0", 7000)

    # CLI overrides env.
    assert _resolve_bind("127.0.0.2", 6006) == ("127.0.0.2", 6006)


def test_resolve_config_path_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORLINMAN_CONFIG", raising=False)
    assert _resolve_config_path(None) is None

    monkeypatch.setenv("CORLINMAN_CONFIG", "/etc/corlinman.toml")
    assert _resolve_config_path(None) == Path("/etc/corlinman.toml")

    # CLI wins.
    assert _resolve_config_path("/tmp/x.toml") == Path("/tmp/x.toml")


def test_resolve_data_dir_uses_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    assert _resolve_data_dir(None) == tmp_path


def test_should_run_legacy_migration_gates() -> None:
    from types import SimpleNamespace

    # No tenants section → don't run.
    assert _should_run_legacy_migration(None) is False
    assert _should_run_legacy_migration(SimpleNamespace()) is False

    # Both flags must be true.
    only_enabled = SimpleNamespace(
        tenants=SimpleNamespace(enabled=True, migrate_legacy_paths=False)
    )
    assert _should_run_legacy_migration(only_enabled) is False

    both = SimpleNamespace(
        tenants=SimpleNamespace(enabled=True, migrate_legacy_paths=True)
    )
    assert _should_run_legacy_migration(both) is True

    # Dict-shaped config also works.
    dict_cfg = {"tenants": {"enabled": True, "migrate_legacy_paths": True}}
    assert _should_run_legacy_migration(dict_cfg) is True


def test_build_app_degraded_mode_serves_health(tmp_path: Path) -> None:
    """With no sibling modules present, the app should still expose
    ``/health`` so liveness probes succeed."""
    app = build_app(config_path=None, data_dir=tmp_path)

    # State is plumbed through.
    assert app.state.corlinman_data_dir == tmp_path
    assert app.state.corlinman_config is None
    assert app.state.corlinman_state is not None

    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        # Either degraded or ok depending on what siblings landed; both
        # are acceptable here.
        assert body["mode"] in {"degraded", "ok"}


def test_build_app_runs_legacy_migration_when_gated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the (synthetic) config opts in, the legacy migration runs
    against ``data_dir`` during ``build_app``."""
    from types import SimpleNamespace

    # Pre-seed a legacy file we expect to be moved.
    legacy = tmp_path / "evolution.sqlite"
    legacy.write_bytes(b"legacy")

    # Stub a config loader by injecting via the lazy-import path: monkey
    # patch the entrypoint module's ``_load_config`` to return our cfg
    # without writing a real TOML.
    cfg = SimpleNamespace(
        tenants=SimpleNamespace(enabled=True, migrate_legacy_paths=True)
    )
    monkeypatch.setattr(
        "corlinman_server.gateway.lifecycle.entrypoint._load_config",
        lambda path: cfg,
    )

    # The fake config_path needs to be truthy to traverse the load path.
    app = build_app(config_path=tmp_path / "cfg.toml", data_dir=tmp_path)
    assert app is not None
    assert not legacy.exists()
    migrated = tmp_path / "tenants" / "default" / "evolution.sqlite"
    assert migrated.exists()
    assert migrated.read_bytes() == b"legacy"


def test_sigterm_exit_code_matches_rust_contract() -> None:
    """Both runtimes return 128 + SIGTERM = 143 on a clean signal-driven
    shutdown."""
    assert SIGTERM_EXIT_CODE == 143
