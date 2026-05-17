"""Smoke tests for the ``corlinman`` CLI.

Covers:

* ``corlinman --help`` lists every top-level subcommand from the
  Rust binary (full + stub ports).
* ``corlinman doctor --json`` succeeds and emits valid JSON.
* ``corlinman dev watch`` (a STUB) exits ``2`` with the canonical
  "not yet ported" message.
* ``corlinman config init`` writes a starter ``config.toml`` to a
  custom path.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from corlinman_server.cli.main import cli


# Every subcommand the Rust binary exposes. We assert each one is in
# the help output so a future regression on dispatch wiring is caught.
_EXPECTED_SUBCOMMANDS = (
    "onboard",
    "doctor",
    "plugins",
    "config",
    "dev",
    "qa",
    "vector",
    "tenant",
    "replay",
    # extras promised by the cli/__init__.py docstring
    "migrate",
    "rollback",
    "skills",
    "identity",
)


def test_root_help_lists_every_subcommand() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0, result.output
    for name in _EXPECTED_SUBCOMMANDS:
        assert name in result.output, f"missing subcommand {name!r} in help output"


def test_doctor_json_emits_valid_json(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["doctor", "--json", "--data-dir", str(tmp_path)],
    )
    # Exit may be 0 (all checks pass) or 1 (some fail in CI env); both
    # are acceptable — what we care about is the JSON shape.
    assert result.exit_code in (0, 1), result.output
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    assert payload, "doctor should always emit at least one report"
    for item in payload:
        assert "name" in item
        assert "status" in item
        assert item["status"] in ("ok", "warn", "fail")
        assert "message" in item


def test_stub_subcommand_exits_two() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["dev", "watch"])
    assert result.exit_code == 2, (result.exit_code, result.output)
    # The ``todo_stub`` message goes to stderr; click's CliRunner mixes
    # stderr into ``result.output`` by default.
    assert "TODO: not yet ported" in result.output


def test_config_init_writes_default_file(tmp_path: Path) -> None:
    runner = CliRunner()
    cfg_path = tmp_path / "config.toml"
    result = runner.invoke(cli, ["config", "init", "--path", str(cfg_path)])
    assert result.exit_code == 0, result.output
    assert cfg_path.is_file()
    body = cfg_path.read_text(encoding="utf-8")
    assert "[server]" in body
    assert "[admin]" in body


def test_config_init_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    runner = CliRunner()
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("# pre-existing\n", encoding="utf-8")
    result = runner.invoke(cli, ["config", "init", "--path", str(cfg_path)])
    assert result.exit_code == 1, result.output
    assert "already exists" in result.output


def test_config_migrate_sub2api_dry_run_prints_diff(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[providers.subhub]\nkind = "sub2api"\nbase_url = "http://x"\n',
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["config", "migrate-sub2api", "--path", str(cfg_path)],
    )
    assert result.exit_code == 0, result.output
    assert 'kind = "newapi"' in result.output
    # Dry-run leaves the file untouched.
    assert 'kind = "sub2api"' in cfg_path.read_text(encoding="utf-8")
