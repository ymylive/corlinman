"""Contract tests for the NodeBridge v1 stub server.

Port of ``rust/crates/corlinman-nodebridge/tests/contract.rs``. The Rust
tests use ``tokio-tungstenite`` directly so an iOS/Android engineer
reading them can copy the exact JSON shapes; we use the asyncio
``websockets`` client for the same reason — every frame on the wire
matches the Rust test verbatim.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable

import pytest
from corlinman_hooks import HookEvent, HookPriority
from corlinman_nodebridge import (
    SPEC_VERSION,
    Capability,
    Register,
    Registered,
    Telemetry,
    decode_message,
    encode_message,
)
from corlinman_nodebridge.protocol import (
    DispatchJob,
    JobResult,
    RegisterRejected,
)
from corlinman_nodebridge.types import NodeBridgeNoCapableNode
from websockets.asyncio.client import connect

from tests.conftest import Harness

# Type alias for the harness factory fixture.
HarnessFactory = Callable[[bool, int], Awaitable[Harness]]


def _sample_capability(name: str) -> Capability:
    return Capability.new(name, "1.0", {"type": "object"})


async def _register_node(
    ws_url: str,
    node_id: str,
    caps: list[Capability],
    signature: str | None,
):
    """Dial + register with the supplied policy knobs. Returns the WS
    after the ``Registered`` / ``RegisterRejected`` handshake has been
    drained — mirrors the ``register_node`` helper in the Rust tests.
    """
    ws = await connect(ws_url)
    reg = Register(
        node_id=node_id,
        node_type="ios",
        capabilities=caps,
        auth_token="tok",
        version="0.1.0",
        signature=signature,
    )
    await ws.send(encode_message(reg))
    reply_text = await ws.recv()
    assert isinstance(reply_text, str), f"expected text frame, got {type(reply_text)}"
    decoded = decode_message(reply_text)
    return ws, decoded


async def _wait_for(condition: Callable[[], bool], timeout_s: float, label: str) -> None:
    """Poll ``condition`` every event-loop tick until true or timeout."""
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for: {label}")
        await asyncio.sleep(0)


async def test_register_accepted_produces_registered_frame(
    harness: HarnessFactory,
) -> None:
    h = await harness(True, 15)
    ws, ack = await _register_node(h.ws_url, "ios-1", [_sample_capability("camera")], None)
    try:
        assert isinstance(ack, Registered)
        assert ack.node_id == "ios-1"
        assert ack.server_version == SPEC_VERSION
        assert ack.heartbeat_secs == 15

        await _wait_for(lambda: h.server.connected_count() == 1, 2.0, "server to register node")
        assert h.server.connected_count() == 1
    finally:
        await ws.close()


async def test_register_without_signature_rejected_when_unsigned_disabled(
    harness: HarnessFactory,
) -> None:
    h = await harness(False, 15)
    ws, ack = await _register_node(h.ws_url, "ios-2", [], None)
    try:
        assert isinstance(ack, RegisterRejected)
        assert ack.code == "unsigned_registration"
    finally:
        await ws.close()
    # Give the server a tick to finish cleanup.
    await asyncio.sleep(0)
    assert h.server.connected_count() == 0


async def test_register_without_signature_accepted_when_unsigned_enabled(
    harness: HarnessFactory,
) -> None:
    h = await harness(True, 15)
    ws, ack = await _register_node(h.ws_url, "ios-3", [], None)
    try:
        assert isinstance(ack, Registered), f"expected Registered, got {ack}"
    finally:
        await ws.close()


async def test_dispatch_job_routes_to_capable_node_and_returns_result(
    harness: HarnessFactory,
) -> None:
    h = await harness(True, 15)
    ws, ack = await _register_node(
        h.ws_url, "ios-dispatch", [_sample_capability("system.notify")], None
    )
    assert isinstance(ack, Registered)

    async def responder() -> None:
        # Read frames; when a DispatchJob arrives, echo a successful
        # JobResult back.
        try:
            async for raw in ws:
                if not isinstance(raw, str):
                    continue
                try:
                    parsed = decode_message(raw)
                except Exception:
                    continue
                if isinstance(parsed, DispatchJob):
                    result = JobResult(job_id=parsed.job_id, ok=True, payload={"delivered": True})
                    await ws.send(encode_message(result))
                    return
        except Exception:
            return

    responder_task = asyncio.create_task(responder())

    # Wait for the capability to be indexed.
    await _wait_for(lambda: h.server.connected_count() > 0, 2.0, "capability to be indexed")

    try:
        result = await h.server.dispatch_job("system.notify", {"title": "hi"}, 2_000)
    finally:
        await responder_task
        await ws.close()

    assert isinstance(result, JobResult)
    assert result.ok is True
    assert result.payload == {"delivered": True}


async def test_dispatch_job_unknown_capability_returns_not_found_error(
    harness: HarnessFactory,
) -> None:
    h = await harness(True, 15)
    # Register one node with capability "camera"; then ask for "missing".
    ws, ack = await _register_node(h.ws_url, "ios-cam", [_sample_capability("camera")], None)
    try:
        assert isinstance(ack, Registered)
        with pytest.raises(NodeBridgeNoCapableNode) as info:
            await h.server.dispatch_job("missing.kind", {}, 500)
        assert "no capable node" in str(info.value)
    finally:
        await ws.close()


async def test_telemetry_forwarded_to_hook_bus(harness: HarnessFactory) -> None:
    h = await harness(True, 15)
    sub = h.hook_bus.subscribe(HookPriority.NORMAL)

    ws, ack = await _register_node(h.ws_url, "ios-tele", [], None)
    try:
        assert isinstance(ack, Registered)

        tele = Telemetry(
            node_id="ios-tele",
            metric="battery.level",
            value=0.73,
            tags={"build": "dev"},
        )
        await ws.send(encode_message(tele))

        # Generous timeout: hook bus emit is async.
        event = await asyncio.wait_for(sub.recv(), timeout=2.0)
    finally:
        await ws.close()

    assert isinstance(event, HookEvent.Telemetry)
    assert event.node_id == "ios-tele"
    assert event.metric == "battery.level"
    assert abs(event.value - 0.73) < 1e-9
    assert event.tags.get("build") == "dev"


async def test_dispatch_job_times_out_when_node_never_replies(
    harness: HarnessFactory,
) -> None:
    """Bonus coverage beyond the Rust contract suite: a connected node
    that ignores ``DispatchJob`` should produce
    :class:`NodeBridgeTimeout`, not a hang.

    Rust covers this implicitly via ``Timeout`` returns inside
    ``dispatch_job``; we exercise it explicitly because Python's
    ``asyncio.wait_for`` plumbing differs enough from
    ``tokio::time::timeout`` to warrant a regression test.
    """
    from corlinman_nodebridge.types import NodeBridgeTimeout

    h = await harness(True, 15)
    ws, ack = await _register_node(h.ws_url, "ios-silent", [_sample_capability("camera")], None)
    try:
        assert isinstance(ack, Registered)

        # Drain frames the server sends us (the DispatchJob, etc.) so
        # the server's writer doesn't block.
        async def drain() -> None:
            try:
                async for _ in ws:
                    pass
            except Exception:
                pass

        drain_task = asyncio.create_task(drain())
        try:
            with pytest.raises(NodeBridgeTimeout):
                await h.server.dispatch_job("camera", {}, 200)
        finally:
            drain_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await drain_task
    finally:
        await ws.close()
