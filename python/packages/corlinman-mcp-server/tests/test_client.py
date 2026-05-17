"""Stdio JSON-RPC client smoke tests — mirrors ``src/client/stdio.rs``."""

from __future__ import annotations

import asyncio

import pytest

from corlinman_mcp_server import (
    McpClient,
    McpClientMissingStdioError,
    McpClientServerError,
)


@pytest.mark.asyncio
async def test_spawn_and_close_kills_child():
    # `cat` echoes — perfect for testing spawn + clean shutdown.
    client = await McpClient.connect_stdio("cat", [])
    await client.close()


@pytest.mark.asyncio
async def test_call_resolves_when_child_echoes_well_formed_response():
    """awk responder: take each line, emit a result frame with matching id."""
    awk_script = r"""awk 'BEGIN{FS=","} {
        for (i=1;i<=NF;i++) if ($i ~ /"id"/) idline=$i;
        gsub(/.*"id":/, "", idline);
        gsub(/[}\]].*/, "", idline);
        printf("{\"jsonrpc\":\"2.0\",\"id\":%s,\"result\":{\"pong\":true}}\n", idline);
        fflush();
    }'"""
    process = await asyncio.create_subprocess_exec(
        "sh",
        "-c",
        awk_script,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    client = await McpClient.connect_with_process(process)
    try:
        result = await client.call("ping", None)
        assert result == {"pong": True}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_server_error_response_lifts_to_servererror():
    awk_script = r"""awk 'BEGIN{FS=","} {
        for (i=1;i<=NF;i++) if ($i ~ /"id"/) idline=$i;
        gsub(/.*"id":/, "", idline);
        gsub(/[}\]].*/, "", idline);
        printf("{\"jsonrpc\":\"2.0\",\"id\":%s,\"error\":{\"code\":-32601,\"message\":\"no such method\"}}\n", idline);
        fflush();
    }'"""
    process = await asyncio.create_subprocess_exec(
        "sh",
        "-c",
        awk_script,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    client = await McpClient.connect_with_process(process)
    try:
        with pytest.raises(McpClientServerError) as exc:
            await client.call("nope", None)
        assert exc.value.code == -32601
        assert "no such method" in exc.value.message
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_notify_writes_a_frame_without_blocking():
    client = await McpClient.connect_stdio("cat", [])
    try:
        await client.notify("notifications/cancelled", {"requestId": "x"})
    finally:
        await client.close()


def test_id_key_round_trips_string_and_number():
    from corlinman_mcp_server.client import _id_key

    assert _id_key("abc") == "abc"
    assert _id_key(42) == "42"
    assert _id_key(None) == "null"


@pytest.mark.asyncio
async def test_missing_stdio_returns_error():
    """Spawning without piping stdin/stdout must raise McpClientMissingStdioError.

    Mirrors the Rust ``missing_stdio_returns_error`` test in
    ``rust/crates/corlinman-mcp/src/client/stdio.rs``.
    """
    process = await asyncio.create_subprocess_exec(
        "sh",
        "-c",
        "exit 0",
        stdin=None,
        stdout=None,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        with pytest.raises(McpClientMissingStdioError):
            await McpClient.connect_with_process(process)
    finally:
        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
