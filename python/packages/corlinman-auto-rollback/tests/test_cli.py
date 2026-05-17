"""Smoke tests for the ``corlinman-auto-rollback`` CLI.

The Rust binary's tests live in the integration crate; the Python CLI
has no separate crate, so we exercise the argparse surface + the
disabled-config short-circuit + a happy ``run-once`` against a real
TOML file pointing at a fresh ``evolution.sqlite``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, ClassVar

import pytest
from corlinman_auto_rollback import cli
from corlinman_auto_rollback.cli import (
    _load_applier_factory,
    _load_auto_rollback_config,
    _resolve_evolution_db_path,
    main,
)
from corlinman_evolution_store import EvolutionStore


def test_help_smoke(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "corlinman-auto-rollback" in out
    assert "run-once" in out


def test_run_once_help_smoke(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["run-once", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--config" in out
    assert "--max-proposals" in out
    assert "--applier" in out


def test_load_auto_rollback_config_defaults_on_missing_section() -> None:
    cfg = _load_auto_rollback_config({})
    assert cfg.enabled is False
    assert cfg.grace_window_hours == 72
    assert cfg.thresholds.default_err_rate_delta_pct == 50.0


def test_load_auto_rollback_config_reads_full_block() -> None:
    raw: dict[str, Any] = {
        "evolution": {
            "auto_rollback": {
                "enabled": True,
                "grace_window_hours": 24,
                "thresholds": {
                    "default_err_rate_delta_pct": 33.0,
                    "default_p95_latency_delta_pct": 10.0,
                    "signal_window_secs": 900,
                    "min_baseline_signals": 9,
                },
            }
        }
    }
    cfg = _load_auto_rollback_config(raw)
    assert cfg.enabled is True
    assert cfg.grace_window_hours == 24
    assert cfg.thresholds.default_err_rate_delta_pct == 33.0
    assert cfg.thresholds.signal_window_secs == 900
    assert cfg.thresholds.min_baseline_signals == 9


def test_resolve_evolution_db_path_prefers_explicit_override(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit.sqlite"
    raw = {"evolution": {"observer": {"db_path": "/etc/should_be_ignored.sqlite"}}}
    assert _resolve_evolution_db_path(raw, explicit) == explicit


def test_resolve_evolution_db_path_falls_back_to_observer() -> None:
    raw = {"evolution": {"observer": {"db_path": "/srv/evolution.sqlite"}}}
    assert _resolve_evolution_db_path(raw, None) == Path("/srv/evolution.sqlite")


def test_resolve_evolution_db_path_falls_back_to_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("CORLINMAN_DATA_DIR", raising=False)
    raw = {"server": {"data_dir": str(tmp_path)}}
    assert _resolve_evolution_db_path(raw, None) == tmp_path / "evolution.sqlite"


def test_resolve_evolution_db_path_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    assert _resolve_evolution_db_path({}, None) == tmp_path / "evolution.sqlite"


def test_run_once_requires_enabled(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """When [evolution.auto_rollback].enabled = false the CLI must
    refuse rather than silently no-op — mirrors the Rust binary."""
    config = tmp_path / "corlinman.toml"
    config.write_text(
        "[evolution.auto_rollback]\nenabled = false\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit) as exc:
        main(["run-once", "--config", str(config)])
    assert exc.value.code == 2


def test_run_once_requires_applier_when_enabled(
    tmp_path: Path,
) -> None:
    config = tmp_path / "corlinman.toml"
    config.write_text(
        "[evolution.auto_rollback]\nenabled = true\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit) as exc:
        main(["run-once", "--config", str(config)])
    assert exc.value.code == 2


def test_run_once_missing_config_file_exits_2(tmp_path: Path) -> None:
    rc = main(["run-once", "--config", str(tmp_path / "nope.toml")])
    assert rc == 2


def test_load_applier_factory_rejects_bad_spec() -> None:
    with pytest.raises(ValueError):
        _load_applier_factory("no_colon_here")


# ---------------------------------------------------------------------------
# Happy-path end-to-end: real config file + real evolution.sqlite +
# applier factory imported from this very test module. Mirrors the
# Rust binary's "wires everything up and runs one pass" shape.
# ---------------------------------------------------------------------------


class _NoopApplier:
    """Records calls, never raises — used by the happy-path CLI test
    to prove the wiring works end-to-end. ``calls`` is a class-level
    ClassVar so the ``--applier`` factory can hand the test a fresh
    instance per run while still letting the test inspect call history
    via the class itself."""

    calls: ClassVar[list[tuple[str, str]]] = []

    async def revert(self, proposal_id: object, reason: str) -> None:
        type(self).calls.append((str(proposal_id), reason))


def make_noop_applier(store: EvolutionStore, raw: dict[str, Any]) -> _NoopApplier:
    # Exported with a stable name so --applier 'tests.test_cli:make_noop_applier'
    # resolves via the package import path.
    return _NoopApplier()


def test_run_once_end_to_end_empty_db_emits_zero_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = tmp_path / "evolution.sqlite"

    # Create the schema so the CLI's EvolutionStore.open finds a valid
    # DB. ``main`` calls ``asyncio.run`` itself, so this test must be
    # synchronous (otherwise pytest-asyncio's auto mode starts an outer
    # event loop and ``asyncio.run`` raises RuntimeError).
    async def _seed() -> None:
        store = await EvolutionStore.open(db)
        await store.close()

    asyncio.run(_seed())

    config = tmp_path / "corlinman.toml"
    config.write_text(
        f"""
[evolution.auto_rollback]
enabled = true
grace_window_hours = 72

[evolution.observer]
db_path = "{db.as_posix()}"
""",
        encoding="utf-8",
    )

    rc = main(
        [
            "run-once",
            "--config",
            str(config),
            "--applier",
            f"{__name__}:make_noop_applier",
            "--json",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    summary = json.loads(out)
    assert summary == {
        "proposals_inspected": 0,
        "thresholds_breached": 0,
        "rollbacks_triggered": 0,
        "rollbacks_succeeded": 0,
        "rollbacks_failed": 0,
        "errors": 0,
    }


def test_module_exports_main_for_console_script() -> None:
    """``[project.scripts]`` points at ``corlinman_auto_rollback.cli:main``.
    Smoke that the attribute resolves."""
    assert callable(cli.main)
