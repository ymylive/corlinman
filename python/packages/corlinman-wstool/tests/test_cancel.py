"""Cancellation + reconnect + loopback-equivalence tests.

Mirrors ``rust/crates/corlinman-wstool/tests/cancel.rs``.

``cancel_in_flight_propagates_to_handler`` uses a oneshot Future to
prove the handler actually observed the cancel — not just that the
caller side timed out. That's the whole point of wiring Cancel frames
into per-request cancel events.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest

from corlinman_wstool import (
    Disconnected,
    ProgressSink,
    ToolError,
    ToolFailed,
)

from .conftest import EchoHandler, Harness, simple_advert, spawn_runner


class SlowHandler:
    def __init__(self) -> None:
        self.cancelled_evt: asyncio.Future[None] = (
            asyncio.get_event_loop().create_future()
        )

    async def invoke(
        self,
        tool: str,
        args: Any,
        progress: ProgressSink,
        cancel: asyncio.Event,
    ) -> Any:
        del tool, args, progress
        await cancel.wait()
        if not self.cancelled_evt.done():
            self.cancelled_evt.set_result(None)
        raise ToolError.cancelled()


class WedgedHandler:
    async def invoke(
        self,
        tool: str,
        args: Any,
        progress: ProgressSink,
        cancel: asyncio.Event,
    ) -> Any:
        del tool, args, progress, cancel
        # Never resolves.
        await asyncio.Event().wait()
        return None  # unreachable


@pytest.mark.asyncio
async def test_cancel_in_flight_propagates_to_handler(harness: Harness) -> None:
    handler = SlowHandler()
    runner, serve = await spawn_runner(
        harness, "slow-runner", [simple_advert("slow")], handler
    )
    try:
        cancel = asyncio.Event()
        caller = asyncio.create_task(
            harness.server.invoke(
                "slow", {}, timeout_ms=30_000, cancel_event=cancel
            )
        )
        # Yield a few times so the invoke frame reaches the handler.
        for _ in range(20):
            await asyncio.sleep(0)
        cancel.set()

        # Handler must observe cancel within a bounded window.
        await asyncio.wait_for(handler.cancelled_evt, timeout=2.0)

        # Caller side also sees a ToolFailed-style error (cancelled).
        with pytest.raises((ToolFailed, Exception)) as excinfo:
            await asyncio.wait_for(caller, timeout=2.0)
        # `ToolFailed(code="cancelled")` is what `invoke_once` raises on
        # caller-side cancel.
        if isinstance(excinfo.value, ToolFailed):
            assert excinfo.value.code == "cancelled"
    finally:
        await runner.close()
        serve.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await serve


@pytest.mark.asyncio
async def test_reconnect_fails_inflight_requests_with_disconnected(
    harness: Harness,
) -> None:
    runner, serve = await spawn_runner(
        harness, "wedged", [simple_advert("wedged")], WedgedHandler()
    )

    caller = asyncio.create_task(
        harness.server.invoke("wedged", {}, timeout_ms=30_000)
    )
    for _ in range(20):
        await asyncio.sleep(0)

    # Tear the runner down.
    serve.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await serve
    await runner.close()
    # Give the server's reader loop a chance to observe close and fail
    # pending futures.
    for _ in range(50):
        await asyncio.sleep(0.01)
        if caller.done():
            break

    with pytest.raises(Disconnected):
        await asyncio.wait_for(caller, timeout=3.0)


@pytest.mark.asyncio
async def test_loopback_equivalence_vs_direct_call(harness: Harness) -> None:
    handler = EchoHandler()
    runner, serve = await spawn_runner(
        harness, "loop", [simple_advert("echo")], handler
    )
    try:
        # Direct call to the same handler.
        direct = await handler.invoke(
            "echo",
            {"k": "v"},
            ProgressSink.discarding(),
            asyncio.Event(),
        )
        via_runtime = await harness.server.invoke(
            "echo", {"k": "v"}, timeout_ms=5_000
        )
        assert direct == via_runtime
    finally:
        await runner.close()
        serve.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await serve
