"""Tests for ``corlinman_providers.plugins.manifest``.

1:1 port of the inline ``#[cfg(test)] mod tests`` in
``rust/crates/corlinman-plugins/src/manifest.rs``.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from corlinman_providers.plugins.manifest import (
    MANIFEST_FILENAME,
    AllowlistMode,
    ManifestParseError,
    ManifestTomlError,
    ManifestValidationError,
    PluginManifest,
    PluginType,
    RestartPolicy,
    parse_manifest_file,
)

SAMPLE = textwrap.dedent(
    """
    name = "greeter"
    version = "0.1.0"
    description = "Says hello"
    author = "ada"
    plugin_type = "sync"

    [entry_point]
    command = "python"
    args = ["main.py"]

    [communication]
    timeout_ms = 5000

    [[capabilities.tools]]
    name = "greet"
    description = "Greet someone"

    [capabilities.tools.parameters]
    type = "object"
    required = ["name"]

    [capabilities.tools.parameters.properties.name]
    type = "string"

    [capabilities]
    disable_model_invocation = false

    [sandbox]
    memory = "256m"
    cpus = 0.5
    read_only_root = true
    cap_drop = ["ALL"]
    network = "none"
    binds = []
    """
).strip()


def _write_manifest(tmp_path: Path, body: str) -> Path:
    path = tmp_path / MANIFEST_FILENAME
    path.write_text(body, encoding="utf-8")
    return path


# ---------- baseline parse ----------


def test_sample_manifest_parses(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, SAMPLE)
    m = parse_manifest_file(path)
    assert m.name == "greeter"
    assert m.version == "0.1.0"
    assert m.plugin_type == PluginType.SYNC
    assert m.entry_point.command == "python"
    assert m.entry_point.args == ["main.py"]
    assert m.communication.timeout_ms == 5000
    assert len(m.capabilities.tools) == 1
    assert m.capabilities.tools[0].name == "greet"
    assert m.sandbox.memory == "256m"


def test_empty_name_fails_validation(tmp_path: Path) -> None:
    body = textwrap.dedent(
        """
        name = ""
        version = "0.1.0"
        plugin_type = "sync"
        [entry_point]
        command = "true"
        """
    )
    path = _write_manifest(tmp_path, body)
    with pytest.raises(ManifestValidationError):
        parse_manifest_file(path)


def test_unknown_fields_rejected(tmp_path: Path) -> None:
    body = textwrap.dedent(
        """
        name = "x"
        version = "0.1.0"
        plugin_type = "sync"
        mystery_field = 42
        [entry_point]
        command = "true"
        """
    )
    path = _write_manifest(tmp_path, body)
    with pytest.raises(ManifestValidationError):
        parse_manifest_file(path)


def test_plugin_type_async_and_service_parse(tmp_path: Path) -> None:
    for t, expected in (("async", PluginType.ASYNC), ("service", PluginType.SERVICE)):
        body = textwrap.dedent(
            f"""
            name = "x"
            version = "0.1.0"
            plugin_type = "{t}"
            [entry_point]
            command = "true"
            """
        )
        path = _write_manifest(tmp_path, body)
        m = parse_manifest_file(path)
        assert m.plugin_type == expected


def test_parse_manifest_file_round_trip(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, SAMPLE)
    m = parse_manifest_file(path)
    assert m.name == "greeter"


# ---------- v1 / v2 / v3 migration ----------


def test_v1_manifest_loads_as_current(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, SAMPLE)
    m = parse_manifest_file(path)
    assert m.manifest_version == 3
    assert m.protocols == ["openai_function"]
    assert m.hooks == []
    assert m.skill_refs == []
    assert m.mcp is None


def test_v2_manifest_migrates_to_v3(tmp_path: Path) -> None:
    body = textwrap.dedent(
        """
        manifest_version = 2
        name = "full"
        version = "0.2.0"
        plugin_type = "sync"
        protocols = ["openai_function", "block"]
        hooks = ["message.received", "session.patch"]
        skill_refs = ["skill.core", "skill.search"]

        [entry_point]
        command = "python"
        args = ["main.py"]
        """
    )
    path = _write_manifest(tmp_path, body)
    m = parse_manifest_file(path)
    assert m.manifest_version == 3
    assert m.protocols == ["openai_function", "block"]
    assert m.hooks == ["message.received", "session.patch"]
    assert m.skill_refs == ["skill.core", "skill.search"]
    assert m.mcp is None


def test_invalid_protocol_rejected(tmp_path: Path) -> None:
    body = textwrap.dedent(
        """
        manifest_version = 2
        name = "bad"
        version = "0.1.0"
        plugin_type = "sync"
        protocols = ["custom"]
        [entry_point]
        command = "true"
        """
    )
    path = _write_manifest(tmp_path, body)
    with pytest.raises(ManifestValidationError, match="unknown protocol"):
        parse_manifest_file(path)


def test_future_version_rejected(tmp_path: Path) -> None:
    body = textwrap.dedent(
        """
        manifest_version = 99
        name = "future"
        version = "0.1.0"
        plugin_type = "sync"
        [entry_point]
        command = "true"
        """
    )
    path = _write_manifest(tmp_path, body)
    with pytest.raises(ManifestValidationError, match="not supported"):
        parse_manifest_file(path)


def test_unknown_hook_is_not_an_error(tmp_path: Path) -> None:
    body = textwrap.dedent(
        """
        manifest_version = 2
        name = "hooky"
        version = "0.1.0"
        plugin_type = "sync"
        hooks = ["message.weird"]
        [entry_point]
        command = "true"
        """
    )
    path = _write_manifest(tmp_path, body)
    m = parse_manifest_file(path)
    assert m.hooks == ["message.weird"]


# ---------- channel manifests ----------


def test_smoke_load_qq_manifest_migrates_to_current(tmp_path: Path) -> None:
    body = textwrap.dedent(
        """
        name = "qq"
        version = "0.1.0"
        description = "QQ (OneBot v11) channel adapter"
        plugin_type = "service"

        [entry_point]
        command = "corlinman-channel-qq"
        """
    )
    path = _write_manifest(tmp_path, body)
    m = parse_manifest_file(path)
    assert m.name == "qq"
    assert m.manifest_version == 3
    assert m.protocols == ["openai_function"]


def test_smoke_load_telegram_manifest_migrates_to_current(tmp_path: Path) -> None:
    body = textwrap.dedent(
        """
        name = "telegram"
        version = "0.1.0"
        description = "Telegram Bot API channel adapter"
        plugin_type = "service"

        [entry_point]
        command = "corlinman-channel-telegram"
        """
    )
    path = _write_manifest(tmp_path, body)
    m = parse_manifest_file(path)
    assert m.name == "telegram"
    assert m.manifest_version == 3
    assert m.protocols == ["openai_function"]


# ---------- v3 / MCP rules ----------


def test_mcp_kind_parses_under_v3(tmp_path: Path) -> None:
    body = textwrap.dedent(
        """
        manifest_version = 3
        name = "fs"
        version = "0.1.0"
        plugin_type = "mcp"

        [entry_point]
        command = "npx"
        args = ["-y", "@modelcontextprotocol/server-filesystem", "/data"]

        [mcp]
        """
    )
    path = _write_manifest(tmp_path, body)
    m = parse_manifest_file(path)
    assert m.plugin_type == PluginType.MCP
    assert m.mcp is not None


def test_mcp_kind_on_v2_rejected(tmp_path: Path) -> None:
    body = textwrap.dedent(
        """
        manifest_version = 2
        name = "fs"
        version = "0.1.0"
        plugin_type = "mcp"
        [entry_point]
        command = "npx"
        """
    )
    path = _write_manifest(tmp_path, body)
    with pytest.raises(ManifestValidationError, match=r"manifest_version >= 3"):
        parse_manifest_file(path)


def test_mcp_table_unknown_field_rejected(tmp_path: Path) -> None:
    body = textwrap.dedent(
        """
        manifest_version = 3
        name = "fs"
        version = "0.1.0"
        plugin_type = "mcp"
        [entry_point]
        command = "npx"
        [mcp]
        mystery = 1
        """
    )
    path = _write_manifest(tmp_path, body)
    with pytest.raises(ManifestValidationError):
        parse_manifest_file(path)


def test_tools_allowlist_default_is_fail_closed(tmp_path: Path) -> None:
    body = textwrap.dedent(
        """
        manifest_version = 3
        name = "fs"
        version = "0.1.0"
        plugin_type = "mcp"
        [entry_point]
        command = "npx"
        [mcp]
        """
    )
    path = _write_manifest(tmp_path, body)
    m = parse_manifest_file(path)
    assert m.mcp is not None
    assert m.mcp.tools_allowlist.mode == AllowlistMode.ALLOW
    assert m.mcp.tools_allowlist.names == []


def test_v3_only_field_on_v2_manifest_rejected(tmp_path: Path) -> None:
    body = textwrap.dedent(
        """
        manifest_version = 2
        name = "x"
        version = "0.1.0"
        plugin_type = "service"
        [entry_point]
        command = "true"
        [mcp]
        """
    )
    path = _write_manifest(tmp_path, body)
    with pytest.raises(ManifestValidationError, match=r"manifest_version >= 3"):
        parse_manifest_file(path)


def test_mcp_table_on_non_mcp_plugin_rejected(tmp_path: Path) -> None:
    body = textwrap.dedent(
        """
        manifest_version = 3
        name = "x"
        version = "0.1.0"
        plugin_type = "service"
        [entry_point]
        command = "true"
        [mcp]
        """
    )
    path = _write_manifest(tmp_path, body)
    with pytest.raises(ManifestValidationError, match=r'plugin_type = "mcp"'):
        parse_manifest_file(path)


def test_mcp_restart_policy_defaults_and_parses(tmp_path: Path) -> None:
    body = textwrap.dedent(
        """
        manifest_version = 3
        name = "fs"
        version = "0.1.0"
        plugin_type = "mcp"
        [entry_point]
        command = "npx"
        [mcp]
        restart_policy = "always"
        crash_loop_max = 7
        crash_loop_window_secs = 90
        handshake_timeout_ms = 1234
        idle_shutdown_secs = 30
        autostart = true
        """
    )
    path = _write_manifest(tmp_path, body)
    m = parse_manifest_file(path)
    assert m.mcp is not None
    assert m.mcp.autostart is True
    assert m.mcp.restart_policy == RestartPolicy.ALWAYS
    assert m.mcp.crash_loop_max == 7
    assert m.mcp.crash_loop_window_secs == 90
    assert m.mcp.handshake_timeout_ms == 1234
    assert m.mcp.idle_shutdown_secs == 30


def test_mcp_defaults_applied_when_table_empty(tmp_path: Path) -> None:
    body = textwrap.dedent(
        """
        manifest_version = 3
        name = "fs"
        version = "0.1.0"
        plugin_type = "mcp"
        [entry_point]
        command = "npx"
        [mcp]
        """
    )
    path = _write_manifest(tmp_path, body)
    m = parse_manifest_file(path)
    assert m.mcp is not None
    assert m.mcp.autostart is False
    assert m.mcp.restart_policy == RestartPolicy.ON_CRASH
    assert m.mcp.crash_loop_max == 3
    assert m.mcp.crash_loop_window_secs == 60
    assert m.mcp.handshake_timeout_ms == 5000
    assert m.mcp.idle_shutdown_secs == 0
    assert m.mcp.env_passthrough.allow == []
    assert m.mcp.env_passthrough.deny == []
    assert m.mcp.tools_allowlist.mode == AllowlistMode.ALLOW
    assert m.mcp.tools_allowlist.names == []
    assert m.mcp.resources_allowlist.mode == AllowlistMode.ALLOW
    assert m.mcp.resources_allowlist.patterns == []


# ---------- migration polish ----------


def test_migrate_is_idempotent() -> None:
    body = SAMPLE
    m1 = PluginManifest.model_validate(_loads_inline_toml(body))
    m1.migrate_to_current_in_memory()
    v_after_first = m1.manifest_version
    m1.migrate_to_current_in_memory()
    assert m1.manifest_version == v_after_first == 3


def test_parse_manifest_file_does_not_rewrite_v1_on_disk(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, SAMPLE)
    before = path.read_text(encoding="utf-8")
    m = parse_manifest_file(path)
    assert m.manifest_version == 3
    after = path.read_text(encoding="utf-8")
    assert before == after


def test_parse_manifest_file_does_not_rewrite_v2_on_disk(tmp_path: Path) -> None:
    body = textwrap.dedent(
        """
        manifest_version = 2
        name = "preserve"
        version = "0.1.0"
        plugin_type = "service"
        protocols = ["openai_function"]

        [entry_point]
        command = "true"
        """
    )
    path = _write_manifest(tmp_path, body)
    before = path.read_text(encoding="utf-8")
    m = parse_manifest_file(path)
    assert m.manifest_version == 3
    after = path.read_text(encoding="utf-8")
    assert before == after


def test_parse_manifest_file_repeated_loads_are_stable(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, SAMPLE)
    m1 = parse_manifest_file(path)
    m2 = parse_manifest_file(path)
    assert m1.model_dump() == m2.model_dump()


def test_v1_mcp_flavoured_manifest_keeps_version_for_validation_error(
    tmp_path: Path,
) -> None:
    body = textwrap.dedent(
        """
        name = "fs"
        version = "0.1.0"
        plugin_type = "mcp"
        [entry_point]
        command = "npx"
        """
    )
    path = _write_manifest(tmp_path, body)
    with pytest.raises(ManifestValidationError, match=r"manifest_version >= 3"):
        parse_manifest_file(path)


def test_v3_manifest_round_trip_through_migration_is_noop() -> None:
    body = textwrap.dedent(
        """
        manifest_version = 3
        name = "fs"
        version = "0.1.0"
        plugin_type = "mcp"

        [entry_point]
        command = "npx"

        [mcp]
        autostart = true
        """
    )
    m = PluginManifest.model_validate(_loads_inline_toml(body))
    m.migrate_to_current_in_memory()
    m.migrate_to_current_in_memory()
    m.migrate_to_current_in_memory()
    m.validate_all()
    assert m.manifest_version == 3
    assert m.mcp is not None
    assert m.mcp.autostart is True


def test_v2_to_v3_migration_round_trip_e2e(tmp_path: Path) -> None:
    body = textwrap.dedent(
        """
        manifest_version = 2
        name = "rt"
        version = "0.1.0"
        plugin_type = "service"
        protocols = ["openai_function", "block"]
        hooks = ["message.received"]
        skill_refs = ["skill.alpha"]

        [entry_point]
        command = "corlinman-channel-rt"

        [capabilities]
        disable_model_invocation = false
        """
    )
    path = _write_manifest(tmp_path, body)
    on_disk_before = path.read_text(encoding="utf-8")
    m = parse_manifest_file(path)
    assert m.manifest_version == 3
    assert m.protocols == ["openai_function", "block"]
    assert m.hooks == ["message.received"]
    assert m.skill_refs == ["skill.alpha"]
    assert m.mcp is None
    on_disk_after = path.read_text(encoding="utf-8")
    assert on_disk_before == on_disk_after


# ---------- error types ----------


def test_io_error_for_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ManifestParseError):
        parse_manifest_file(tmp_path / "missing.toml")


def test_toml_error_for_broken_toml(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, "not = valid = toml")
    with pytest.raises(ManifestTomlError):
        parse_manifest_file(path)


# Helper used by a couple of cases above.
def _loads_inline_toml(body: str) -> dict:
    import tomllib

    return tomllib.loads(body)
