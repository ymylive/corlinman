"""Shared fixtures for corlinman-wstool tests.

Mirrors the Rust crate's ``tests/common/mod.rs`` helpers: every test
gets a fresh gateway on ``127.0.0.1:0`` so parallel test execution
doesn't compete for a port, plus convenience helpers that dial a runner
and wait for tool registration to settle before returning.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
import pytest_asyncio

from corlinman_hooks.bus import HookBus
from corlinman_wstool import (
    ProgressSink,
    ToolAdvert,
    ToolError,
    ToolHandler,
    WsToolConfig,
    WsToolRunner,
    WsToolServer,
)


@dataclass
class Harness:
    server: WsToolServer
    hook_bus: HookBus
    token: str
    ws_url: str


async def _make_harness(heartbeat_secs: int = 15) -> Harness:
    hook_bus = HookBus(capacity=64)
    token = "test-token"
    cfg = WsToolConfig.loopback(token)
    cfg.heartbeat_secs = heartbeat_secs
    server = WsToolServer(cfg, hook_bus)
    host, port = await server.bind()
    return Harness(
        server=server,
        hook_bus=hook_bus,
        token=token,
        ws_url=f"ws://{host}:{port}",
    )


@pytest_asyncio.fixture
async def harness() -> AsyncIterator[Harness]:
    h = await _make_harness()
    try:
        yield h
    finally:
        await h.server.shutdown()


@pytest_asyncio.fixture
async def harness_fast_heartbeat() -> AsyncIterator[Harness]:
    h = await _make_harness(heartbeat_secs=1)
    try:
        yield h
    finally:
        await h.server.shutdown()


# ---------------------------------------------------------------------------
# Sample handlers.
# ---------------------------------------------------------------------------


class EchoHandler:
    """Test handler — echoes its args back inside ``{"tool", "echo"}``."""

    async def invoke(
        self,
        tool: str,
        args: Any,
        progress: ProgressSink,
        cancel: asyncio.Event,
    ) -> Any:
        del progress, cancel
        return {"tool": tool, "echo": args}


def simple_advert(name: str) -> ToolAdvert:
    return ToolAdvert(
        name=name,
        description=f"{name} tool",
        parameters={"type": "object"},
    )


async def spawn_runner(
    h: Harness,
    runner_id: str,
    tools: list[ToolAdvert],
    handler: ToolHandler,
) -> tuple[WsToolRunner, asyncio.Task[None]]:
    """Dial + register a runner; wait until every advertised tool is in
    the gateway's index before returning.

    Returns ``(runner, serve_task)``. Cancel ``serve_task`` to tear the
    runner down.
    """
    runner = await WsToolRunner.connect(h.ws_url, h.token, runner_id, tools)
    serve = asyncio.create_task(runner.serve_with(handler))
    deadline = asyncio.get_running_loop().time() + 2.0
    while True:
        adv = h.server.advertised_tools()
        if all(t.name in adv for t in tools):
            break
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(
                f"runner tools never registered: {[t.name for t in tools]}"
            )
        await asyncio.sleep(0.01)
    return runner, serve


@pytest.fixture
def make_input() -> Any:
    """Tiny shim to keep tests terse."""

    def _make(tool: str, args: Any, timeout_ms: int = 5000) -> dict[str, Any]:
        return {"tool": tool, "args": args, "timeout_ms": timeout_ms}

    return _make
