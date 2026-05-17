"""Shared fixtures for corlinman-nodebridge tests.

The workspace runs with ``asyncio_mode = "auto"`` (see root
``pyproject.toml``), so async tests don't need an explicit mark and
async fixtures don't need ``@pytest_asyncio.fixture``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

import pytest_asyncio
from corlinman_hooks import HookBus
from corlinman_nodebridge import (
    NodeBridgeServer,
    NodeBridgeServerConfig,
)

HarnessFactory = Callable[[bool, int], Awaitable["Harness"]]


@dataclass
class Harness:
    """Bind a server with the supplied flags and expose a
    ``ws://…/nodebridge/connect`` URL ready to dial.

    Mirrors the ``Harness`` struct in ``tests/contract.rs``.
    """

    server: NodeBridgeServer
    hook_bus: HookBus
    ws_url: str


@pytest_asyncio.fixture
async def harness() -> AsyncIterator[HarnessFactory]:
    """Factory fixture: ``await make_harness(accept_unsigned, hb_secs)``.

    Every harness produced during a test is torn down on fixture
    teardown, so individual tests don't need to call
    ``server.shutdown()`` themselves.
    """
    produced: list[Harness] = []

    async def factory(accept_unsigned: bool, heartbeat_secs: int) -> Harness:
        hook_bus = HookBus(64)
        cfg = NodeBridgeServerConfig.loopback(accept_unsigned)
        cfg.heartbeat_secs = heartbeat_secs
        server = NodeBridgeServer(cfg, hook_bus)
        host, port = await server.bind()
        h = Harness(
            server=server,
            hook_bus=hook_bus,
            ws_url=f"ws://{host}:{port}/nodebridge/connect",
        )
        produced.append(h)
        return h

    try:
        yield factory  # type: ignore[misc]
    finally:
        for h in produced:
            await h.server.shutdown()
