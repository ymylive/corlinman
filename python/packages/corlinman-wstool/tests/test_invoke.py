"""Happy-path and negative-path invocation tests.

Mirrors ``rust/crates/corlinman-wstool/tests/invoke.rs``.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from corlinman_wstool import Unsupported

from .conftest import EchoHandler, Harness, simple_advert, spawn_runner


@pytest.mark.asyncio
async def test_invoke_roundtrip_produces_result(harness: Harness) -> None:
    runner, serve = await spawn_runner(
        harness, "runner-A", [simple_advert("echo")], EchoHandler()
    )
    try:
        out = await harness.server.invoke(
            "echo", {"hello": "world"}, timeout_ms=5_000
        )
        assert isinstance(out, dict)
        assert out["tool"] == "echo"
        assert out["echo"] == {"hello": "world"}
    finally:
        await runner.close()
        serve.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await serve


@pytest.mark.asyncio
async def test_invoke_with_unknown_tool_returns_unsupported(harness: Harness) -> None:
    # No runner connected -> every tool is unsupported.
    with pytest.raises(Unsupported) as excinfo:
        await harness.server.invoke("missing.tool", {}, timeout_ms=1000)
    assert excinfo.value.tool == "missing.tool"
