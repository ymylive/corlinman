"""Tests for ``corlinman_providers.plugins.sandbox``.

Unit tests cover ``parse_bytes``, ``host_config_from``, ``is_enabled``, and
``parse_response_line`` — none require the Docker daemon. The Docker
integration tests at the bottom are guarded by ``no_docker`` so this file
runs cleanly on machines without a daemon.
"""

from __future__ import annotations

import pytest
from corlinman_providers.plugins.manifest import (
    EntryPoint,
    PluginManifest,
    PluginType,
    SandboxConfig,
)
from corlinman_providers.plugins.sandbox import (
    OOM_ERROR_CODE,
    DockerSandbox,
    PluginOutput,
    SandboxConfigError,
    SandboxRuntimeError,
    host_config_from,
    is_enabled,
    parse_bytes,
    parse_response_line,
)

from .conftest import no_docker


def _fixture_manifest(sandbox: SandboxConfig | None = None) -> PluginManifest:
    return PluginManifest(
        manifest_version=2,
        name="fixture",
        version="0.1.0",
        plugin_type=PluginType.SYNC,
        entry_point=EntryPoint(command="python3", args=["main.py"]),
        sandbox=sandbox or SandboxConfig(),
    )


# ---------- parse_bytes ----------


def test_parse_bytes_bare_bytes() -> None:
    assert parse_bytes("1024") == 1024


def test_parse_bytes_kilobytes() -> None:
    assert parse_bytes("512k") == 512 * 1024
    assert parse_bytes("512KB") == 512 * 1024


def test_parse_bytes_megabytes() -> None:
    assert parse_bytes("256m") == 256 * 1024 * 1024


def test_parse_bytes_gigabytes() -> None:
    assert parse_bytes("1g") == 1 << 30
    assert parse_bytes("2G") == 2 << 30


def test_parse_bytes_terabytes() -> None:
    assert parse_bytes("1t") == 1 << 40


def test_parse_bytes_fractional_megabytes() -> None:
    assert parse_bytes("1.5m") == int(1.5 * (1 << 20))


def test_parse_bytes_empty_string_errors() -> None:
    with pytest.raises(SandboxConfigError):
        parse_bytes("")
    with pytest.raises(SandboxConfigError):
        parse_bytes("   ")


def test_parse_bytes_unknown_unit_errors() -> None:
    with pytest.raises(SandboxConfigError):
        parse_bytes("10zz")


def test_parse_bytes_missing_number_errors() -> None:
    with pytest.raises(SandboxConfigError):
        parse_bytes("m")


# ---------- is_enabled ----------


def test_default_sandbox_is_disabled() -> None:
    assert is_enabled(SandboxConfig()) is False


def test_any_field_enables_sandbox() -> None:
    assert is_enabled(SandboxConfig(memory="64m")) is True
    assert is_enabled(SandboxConfig(read_only_root=True)) is True
    assert is_enabled(SandboxConfig(cap_drop=["ALL"])) is True
    assert is_enabled(SandboxConfig(cpus=0.25)) is True
    assert is_enabled(SandboxConfig(network="bridge")) is True
    assert is_enabled(SandboxConfig(binds=["/a:/b"])) is True


# ---------- host_config_from ----------


def test_host_config_from_maps_memory_and_cpus() -> None:
    sb = SandboxConfig(
        memory="64m",
        cpus=0.5,
        read_only_root=True,
        cap_drop=["ALL"],
        network="none",
        binds=["/tmp/x:/mnt/x:ro"],
    )
    host = host_config_from(_fixture_manifest(sb))
    assert host["mem_limit"] == 64 * 1024 * 1024
    assert host["nano_cpus"] == 500_000_000
    assert host["read_only"] is True
    assert host["cap_drop"] == ["ALL"]
    assert host["network_mode"] == "none"
    assert host["volumes"] == ["/tmp/x:/mnt/x:ro"]
    assert host["auto_remove"] is True


def test_host_config_defaults_network_to_none() -> None:
    host = host_config_from(_fixture_manifest(SandboxConfig(memory="32m")))
    assert host["network_mode"] == "none"


def test_host_config_bad_memory_errors() -> None:
    with pytest.raises(SandboxConfigError):
        host_config_from(_fixture_manifest(SandboxConfig(memory="not-a-size")))


# ---------- parse_response_line ----------


def test_parse_response_line_success() -> None:
    out = parse_response_line(b'{"jsonrpc":"2.0","result":{"value":1}}', 42)
    assert out.kind == "success"
    assert out.duration_ms == 42
    assert out.content is not None
    assert b'"value":1' in out.content


def test_parse_response_line_error() -> None:
    out = parse_response_line(
        b'{"jsonrpc":"2.0","error":{"code":-32603,"message":"boom"}}', 12
    )
    assert out.kind == "error"
    assert out.code == -32603
    assert out.message == "boom"


def test_parse_response_line_accepted_for_later() -> None:
    out = parse_response_line(
        b'{"jsonrpc":"2.0","result":{"task_id":"tsk_42"}}', 7
    )
    assert out.kind == "accepted_for_later"
    assert out.task_id == "tsk_42"


def test_parse_response_line_wrong_jsonrpc_rejected() -> None:
    with pytest.raises(SandboxRuntimeError):
        parse_response_line(b'{"jsonrpc":"3.0","result":{}}', 0)


def test_parse_response_line_invalid_json_rejected() -> None:
    with pytest.raises(SandboxRuntimeError):
        parse_response_line(b"not json", 0)


# ---------- PluginOutput factories ----------


def test_plugin_output_factories() -> None:
    s = PluginOutput.success(b"x", 1)
    assert s.kind == "success" and s.content == b"x"
    e = PluginOutput.error(OOM_ERROR_CODE, "oom", 2)
    assert e.kind == "error" and e.code == OOM_ERROR_CODE
    a = PluginOutput.accepted_for_later("t", 3)
    assert a.kind == "accepted_for_later" and a.task_id == "t"


# ---------- DockerSandbox guard rails ----------


@pytest.mark.asyncio
async def test_docker_sandbox_run_without_sandbox_config_raises() -> None:
    sandbox = DockerSandbox(client=object())
    with pytest.raises(SandboxConfigError):
        await sandbox.run(_fixture_manifest(), b"{}\n", 1000)


@pytest.mark.asyncio
async def test_docker_sandbox_run_without_client_raises() -> None:
    sandbox = DockerSandbox(client=None)
    with pytest.raises(SandboxRuntimeError):
        await sandbox.run(_fixture_manifest(SandboxConfig(memory="32m")), b"{}\n", 1000)


# ---------- Docker integration (skipped without daemon) ----------


@pytest.mark.skipif(no_docker, reason="docker daemon not available")
@pytest.mark.asyncio
async def test_docker_connect_pings_daemon() -> None:
    sandbox = await DockerSandbox.connect()
    assert sandbox.default_image
