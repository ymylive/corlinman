"""``NodeBridgeClient`` — an asyncio reference client for the v1 protocol.

The Rust crate intentionally ships no client (the wire contract is the
deliverable). The Python port adds a small client helper anyway because
asyncio test code and integration scripts benefit from a typed wrapper
that mirrors the server's connection-handshake state machine. Mobile
clients should still implement the protocol from scratch against
:mod:`corlinman_nodebridge.protocol`.

Usage::

    async with NodeBridgeClient.connect(
        "ws://127.0.0.1:9000/nodebridge/connect",
        node_id="ios-dev-1",
        node_type="ios",
        capabilities=[Capability(name="camera", version="1.0", params_schema={})],
        auth_token="tok",
        version="0.1.0",
    ) as client:
        # client.registered carries the server's Registered frame.
        async for frame in client:
            if isinstance(frame, DispatchJob):
                await client.send(
                    JobResult(job_id=frame.job_id, ok=True, payload={})
                )
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any

try:
    from websockets.asyncio.client import ClientConnection, connect
except ImportError as exc:  # pragma: no cover
    raise ImportError("corlinman-nodebridge requires websockets>=13 with the asyncio API") from exc
from websockets.exceptions import ConnectionClosed

from corlinman_nodebridge.protocol import (
    Capability,
    Heartbeat,
    NodeBridgeMessage,
    Pong,
    Register,
    Registered,
    RegisterRejected,
    decode_message,
    encode_message,
)
from corlinman_nodebridge.types import NodeBridgeProtocolError, NodeBridgeRegisterRejected

__all__ = ["NodeBridgeClient"]


def _now_ms() -> int:
    return int(time.time() * 1000)


class NodeBridgeClient:
    """Thin asyncio wrapper around a single NodeBridge WebSocket session.

    Construct via :meth:`connect` (which performs the register handshake
    and returns once the server has acknowledged with :class:`Registered`
    or raised on :class:`RegisterRejected`). The instance is then an
    async context manager and an async iterator of inbound frames.
    """

    def __init__(self, ws: ClientConnection, registered: Registered) -> None:
        self._ws = ws
        self.registered = registered

    @property
    def node_id(self) -> str:
        return self.registered.node_id

    @property
    def server_version(self) -> str:
        return self.registered.server_version

    @property
    def heartbeat_secs(self) -> int:
        return self.registered.heartbeat_secs

    # ----- handshake -----

    @classmethod
    async def connect(
        cls,
        url: str,
        *,
        node_id: str,
        node_type: str,
        capabilities: list[Capability] | None = None,
        auth_token: str,
        version: str,
        signature: str | None = None,
        **connect_kwargs: Any,
    ) -> NodeBridgeClient:
        """Dial ``url``, send :class:`Register`, return after the server
        responds.

        Raises :class:`NodeBridgeRegisterRejected` if the server replies
        with :class:`RegisterRejected`. Raises
        :class:`NodeBridgeProtocolError` if the server hangs up before
        replying or replies with an unexpected frame.
        """
        ws = await connect(url, **connect_kwargs)
        reg = Register(
            node_id=node_id,
            node_type=node_type,
            capabilities=list(capabilities or []),
            auth_token=auth_token,
            version=version,
            signature=signature,
        )
        try:
            await ws.send(encode_message(reg))
        except ConnectionClosed as exc:
            raise NodeBridgeProtocolError("server closed connection before register reply") from exc
        try:
            raw = await ws.recv()
        except ConnectionClosed as exc:
            raise NodeBridgeProtocolError("server closed connection before register reply") from exc
        if isinstance(raw, (bytes, bytearray)):
            await ws.close()
            raise NodeBridgeProtocolError("server sent binary frame in handshake")
        reply = decode_message(raw)
        if isinstance(reply, Registered):
            return cls(ws, reply)
        if isinstance(reply, RegisterRejected):
            with suppress(Exception):
                await ws.close()
            raise NodeBridgeRegisterRejected(reply.code, reply.message)
        with suppress(Exception):
            await ws.close()
        raise NodeBridgeProtocolError(f"unexpected first frame from server: {reply.kind!r}")

    # ----- per-frame I/O -----

    async def send(self, msg: NodeBridgeMessage) -> None:
        """Send a single frame. Raises :class:`ConnectionClosed` if the
        socket is gone."""
        await self._ws.send(encode_message(msg))

    async def recv(self) -> NodeBridgeMessage:
        """Receive a single frame. Raises :class:`ConnectionClosed` if
        the socket is gone."""
        raw = await self._ws.recv()
        if isinstance(raw, (bytes, bytearray)):
            raise NodeBridgeProtocolError("server sent binary frame")
        return decode_message(raw)

    async def heartbeat(self) -> None:
        """Convenience helper: send a :class:`Heartbeat` for this node id."""
        await self.send(Heartbeat(node_id=self.node_id, at_ms=_now_ms()))

    async def pong(self) -> None:
        """Convenience helper: send a :class:`Pong` (reply to ``Ping``)."""
        await self.send(Pong())

    async def close(self) -> None:
        with suppress(Exception):
            await self._ws.close()

    # ----- async context + iterator sugar -----

    async def __aenter__(self) -> NodeBridgeClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    def __aiter__(self) -> AsyncIterator[NodeBridgeMessage]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[NodeBridgeMessage]:
        try:
            while True:
                yield await self.recv()
        except ConnectionClosed:
            return
