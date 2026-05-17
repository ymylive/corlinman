"""``NodeBridgeServer`` — the gateway-side stub for the NodeBridge v1 protocol.

Mirrors ``rust/crates/corlinman-nodebridge/src/server.rs``.

This package ships **no** native device client. Per the project
philosophy, the useful artefact is the *wire contract*: a future
Swift/Kotlin/Electron client can read :mod:`corlinman_nodebridge.protocol`
and ``docs/protocols/nodebridge.md``, implement the
Register/Heartbeat/JobResult/Telemetry side, and talk to this server
with no code shared.

Connection lifecycle:

1. Client dials ``ws://host:port/nodebridge/connect``.
2. First frame **must** be :class:`Register`. Anything else, or a
   :class:`Register` with ``signature = None`` when
   ``accept_unsigned = False``, produces a :class:`RegisterRejected`
   frame followed by a close.
3. Server replies :class:`Registered` ``{ server_version: "1.0.0-alpha",
   heartbeat_secs }`` and stores the session.
4. Reader loop dispatches inbound frames (:class:`Heartbeat`,
   :class:`JobResult`, :class:`Telemetry`, :class:`Pong`). Heartbeat
   misses are counted; after three the session is removed and the
   socket closed.
5. :meth:`NodeBridgeServer.dispatch_job` fans out to the first
   registered session whose capabilities contain ``kind``. The returned
   coroutine resolves when that session posts a matching
   :class:`JobResult`, or raises on timeout / no-capable-node.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

try:
    # ``websockets.asyncio.server`` is the v13+ asyncio API the project
    # standardises on. The legacy ``websockets.server`` API is **not**
    # used here.
    from websockets.asyncio.server import ServerConnection, serve
except ImportError as exc:  # pragma: no cover — guard for very old installs
    raise ImportError("corlinman-nodebridge requires websockets>=13 with the asyncio API") from exc
from corlinman_hooks import HookBus, HookEvent
from websockets.exceptions import ConnectionClosed

from corlinman_nodebridge.protocol import (
    Capability,
    DispatchJob,
    Heartbeat,
    JobResult,
    NodeBridgeMessage,
    Ping,
    Pong,
    Register,
    Registered,
    RegisterRejected,
    Shutdown,
    Telemetry,
    decode_message,
    encode_message,
)
from corlinman_nodebridge.session import NodeSession
from corlinman_nodebridge.types import (
    NodeBridgeBindError,
    NodeBridgeError,
    NodeBridgeNoCapableNode,
    NodeBridgeProtocolError,
    NodeBridgeRegisterRejected,
    NodeBridgeTimeout,
)

__all__ = [
    "DEFAULT_HEARTBEAT_SECS",
    "MAX_MISSED_HEARTBEATS",
    "NODEBRIDGE_PATH",
    "SPEC_VERSION",
    "NodeBridgeServer",
    "NodeBridgeServerConfig",
]

_log = logging.getLogger(__name__)

# Protocol spec version reported in the Registered frame. Bumped on any
# breaking change to the NodeBridgeMessage union.
SPEC_VERSION: str = "1.0.0-alpha"

# Default heartbeat cadence, in seconds. Matches WsTool (the adjacent
# crate) so a client team supporting both bridges can share timer logic.
# Three consecutive misses -> disconnect.
DEFAULT_HEARTBEAT_SECS: int = 15

# Number of consecutive missed heartbeats before the server drops the
# session. Matches the spec.
MAX_MISSED_HEARTBEATS: int = 3

# The single WebSocket path the server exposes. Clients must dial this
# path; any other path is rejected with HTTP 404 by the underlying
# ``websockets`` server.
NODEBRIDGE_PATH: str = "/nodebridge/connect"


def _now_ms() -> int:
    """Wall-clock millis since the Unix epoch."""
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Configuration.
# ---------------------------------------------------------------------------


@dataclass
class NodeBridgeServerConfig:
    """Runtime configuration for the stub.

    Kept independent of any gateway-side config plumbing — a caller
    destructures their config once and passes the relevant fields here.
    """

    host: str = "127.0.0.1"
    port: int = 0
    # Mirror of ``[nodebridge].accept_unsigned``. When ``False``, a
    # ``Register`` without ``signature`` is refused.
    accept_unsigned: bool = True
    heartbeat_secs: int = DEFAULT_HEARTBEAT_SECS

    @classmethod
    def loopback(cls, accept_unsigned: bool) -> NodeBridgeServerConfig:
        """Bind to ``127.0.0.1:0`` (port allocated by the OS).

        Matches ``NodeBridgeServerConfig::loopback`` in Rust.
        """
        return cls(
            host="127.0.0.1",
            port=0,
            accept_unsigned=accept_unsigned,
            heartbeat_secs=DEFAULT_HEARTBEAT_SECS,
        )


# ---------------------------------------------------------------------------
# Internal state.
# ---------------------------------------------------------------------------


@dataclass
class _PendingJob:
    """Tracks one in-flight ``DispatchJob`` waiting on its ``JobResult``."""

    future: asyncio.Future[NodeBridgeMessage]


class _ServerState:
    """Shared server state.

    Public-ish (used by the connection-handler closure) but not part of
    the package's external API — external callers should use
    :class:`NodeBridgeServer`.
    """

    def __init__(self, cfg: NodeBridgeServerConfig, hook_bus: HookBus) -> None:
        self.cfg = cfg
        self.hook_bus = hook_bus
        # node_id -> session. First registration with a given id wins;
        # re-registration from a live id is refused with
        # ``duplicate_node_id``.
        self.sessions: dict[str, NodeSession] = {}
        # capability name -> list of node ids advertising it (insertion
        # order). Mirrors the Rust capability_index which uses Vec for
        # the same first-wins semantics.
        self.capability_index: dict[str, list[str]] = {}
        # job_id -> pending waiter.
        self.pending_jobs: dict[str, _PendingJob] = {}
        self._job_seq: int = 0

    def next_job_id(self) -> str:
        n = self._job_seq
        self._job_seq += 1
        return f"job-{n}"

    def find_capable_node(self, kind: str) -> NodeSession | None:
        """Find the first session that advertises ``kind``.

        "First" means "first in the capability_index list", which in
        practice is insertion order.
        """
        ids = self.capability_index.get(kind)
        if not ids:
            return None
        for node_id in ids:
            sess = self.sessions.get(node_id)
            if sess is not None:
                return sess
        return None

    def register_session(self, session: NodeSession) -> None:
        """Insert a session; update capability_index.

        Raises :class:`NodeBridgeRegisterRejected` with code
        ``duplicate_node_id`` if the id is already connected.
        """
        if session.id in self.sessions:
            raise NodeBridgeRegisterRejected(
                "duplicate_node_id",
                f"node_id {session.id} already connected",
            )
        for cap in session.capabilities:
            self.capability_index.setdefault(cap.name, []).append(session.id)
        self.sessions[session.id] = session

    def remove_session(self, node_id: str) -> None:
        if self.sessions.pop(node_id, None) is None:
            return
        # Drop the id from every capability list; prune empty entries to
        # keep diagnostics readable.
        empty: list[str] = []
        for cap_name, ids in self.capability_index.items():
            with suppress(ValueError):
                ids.remove(node_id)
            if not ids:
                empty.append(cap_name)
        for cap_name in empty:
            self.capability_index.pop(cap_name, None)


# ---------------------------------------------------------------------------
# Public server.
# ---------------------------------------------------------------------------


class NodeBridgeServer:
    """Public server handle.

    Use :meth:`bind` to start serving; :meth:`shutdown` to tear down.
    Holding an instance keeps the server task alive; dropping all
    references after :meth:`shutdown` lets the asyncio runtime reclaim
    it.
    """

    def __init__(self, cfg: NodeBridgeServerConfig, hook_bus: HookBus) -> None:
        self._state = _ServerState(cfg, hook_bus)
        self._server: Any = None  # websockets.asyncio.server.Server
        self._bound_host: str | None = None
        self._bound_port: int | None = None

    # ----- lifecycle -----

    async def bind(self) -> tuple[str, int]:
        """Bind the TCP listener and start accepting connections.

        Returns the resolved local ``(host, port)`` (useful when
        ``cfg.port = 0``).
        """
        try:
            self._server = await serve(
                self._handle_connection,
                self._state.cfg.host,
                self._state.cfg.port,
                # Path filtering is enforced inside the handler so we
                # can speak ``RegisterRejected`` semantics rather than
                # only HTTP-level 404. The websockets library serves
                # every path by default.
            )
        except OSError as exc:
            raise NodeBridgeBindError(str(exc)) from exc
        # `Server.sockets` exposes the bound asyncio servers; first
        # socket's getsockname gives us the resolved address.
        sock = next(iter(self._server.sockets))
        host, port = sock.getsockname()[:2]
        self._bound_host = host
        self._bound_port = port
        _log.info("nodebridge server bound at %s:%s", host, port)
        return host, port

    def local_addr(self) -> tuple[str, int] | None:
        """Resolved ``(host, port)`` after :meth:`bind`."""
        if self._bound_host is None or self._bound_port is None:
            return None
        return self._bound_host, self._bound_port

    async def shutdown(self) -> None:
        """Stop accepting new connections and close existing ones."""
        if self._server is None:
            return
        self._server.close()
        with suppress(Exception):
            await self._server.wait_closed()
        self._server = None

    async def __aenter__(self) -> NodeBridgeServer:
        await self.bind()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.shutdown()

    # ----- introspection -----

    def connected_nodes(self) -> dict[str, str]:
        """Snapshot of currently-connected node ids mapped to node_type."""
        return {sid: s.node_type for sid, s in self._state.sessions.items()}

    def connected_count(self) -> int:
        return len(self._state.sessions)

    # ----- dispatch -----

    async def dispatch_job(self, kind: str, params: Any, timeout_ms: int) -> NodeBridgeMessage:
        """Dispatch a job to whichever node first advertised ``kind``.

        Returns the :class:`JobResult` received from that node. Raises
        :class:`NodeBridgeTimeout` after ``timeout_ms`` if no result
        arrives; :class:`NodeBridgeNoCapableNode` if no node advertises
        ``kind`` (no wire traffic in that case);
        :class:`NodeBridgeProtocolError` if the session's outbox is
        gone (e.g. test fixture, or the session disconnected between
        lookup and send).
        """
        session = self._state.find_capable_node(kind)
        if session is None:
            raise NodeBridgeNoCapableNode(kind)

        job_id = self._state.next_job_id()
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[NodeBridgeMessage] = loop.create_future()
        self._state.pending_jobs[job_id] = _PendingJob(future=fut)

        msg = DispatchJob(job_id=job_id, job_kind=kind, params=params, timeout_ms=timeout_ms)
        if session.outbox is None:
            self._state.pending_jobs.pop(job_id, None)
            raise NodeBridgeProtocolError("session has no outbox (test fixture?)")
        try:
            await session.outbox.put(msg)
        except Exception as exc:  # pragma: no cover — Queue.put rarely raises
            self._state.pending_jobs.pop(job_id, None)
            raise NodeBridgeProtocolError(f"client outbox send failed: {exc}") from exc

        try:
            return await asyncio.wait_for(fut, timeout=timeout_ms / 1000.0)
        except TimeoutError as exc:
            self._state.pending_jobs.pop(job_id, None)
            raise NodeBridgeTimeout(timeout_ms) from exc
        except asyncio.CancelledError:
            self._state.pending_jobs.pop(job_id, None)
            raise

    # ----- WebSocket connection handling -----

    async def _handle_connection(self, ws: ServerConnection) -> None:
        # Enforce the single supported path. The websockets library
        # exposes the request line on `ws.request.path`.
        path = getattr(ws.request, "path", None) if ws.request is not None else None
        if path != NODEBRIDGE_PATH:
            _log.warning("nodebridge: rejecting connection on path %r", path)
            await ws.close(code=4004, reason="unknown path")
            return
        await _connection_loop(ws, self._state)


# ---------------------------------------------------------------------------
# Connection-loop primitives. Kept as free functions so the test suite
# can drive ``_handle_client_frame`` independently of the socket.
# ---------------------------------------------------------------------------


async def _reject(ws: ServerConnection, code: str, message: str) -> None:
    """Send a ``RegisterRejected`` frame and close.

    Mirrors the Rust ``reject`` helper.
    """
    frame = RegisterRejected(code=code, message=message)
    with suppress(Exception):
        await ws.send(encode_message(frame))
    with suppress(Exception):
        await ws.close()


async def _read_first_frame(ws: ServerConnection) -> str | None:
    try:
        msg = await ws.recv()
    except ConnectionClosed:
        return None
    if isinstance(msg, bytes):
        # The wire contract is JSON text; if a client sent us bytes
        # we treat it the same as Rust's ``other => warn + close``.
        return None
    return msg


async def _connection_loop(ws: ServerConnection, state: _ServerState) -> None:
    # Step 1: first frame must be Register.
    first_text = await _read_first_frame(ws)
    if first_text is None:
        _log.warning("nodebridge: missing Register frame")
        with suppress(Exception):
            await ws.close()
        return
    try:
        first = decode_message(first_text)
    except ValidationError as err:
        _log.warning("nodebridge: first frame parse failed: %s", err)
        await _reject(ws, "bad_frame", str(err))
        return
    if not isinstance(first, Register):
        _log.warning("nodebridge: first frame was not Register")
        await _reject(ws, "protocol_violation", "first frame must be Register")
        return

    register: Register = first

    # Step 2: signing policy.
    if register.signature is None and not state.cfg.accept_unsigned:
        _log.warning("nodebridge: unsigned registration refused (node_id=%s)", register.node_id)
        await _reject(
            ws,
            "unsigned_registration",
            "signature required; accept_unsigned is false",
        )
        return

    # Step 3: build session + outbox.
    outbox: asyncio.Queue[NodeBridgeMessage] = asyncio.Queue(maxsize=64)
    session = NodeSession(
        id=register.node_id,
        node_type=register.node_type,
        capabilities=list(register.capabilities),
        version=register.version,
        last_heartbeat_ms=_now_ms(),
        outbox=outbox,
    )
    try:
        state.register_session(session)
    except NodeBridgeRegisterRejected as err:
        _log.warning("nodebridge: register refused (%s): %s", err.code, err.message)
        await _reject(ws, err.code, str(err))
        return

    # Step 4: send Registered ack.
    ack = Registered(
        node_id=register.node_id,
        server_version=SPEC_VERSION,
        heartbeat_secs=state.cfg.heartbeat_secs,
    )
    try:
        await ws.send(encode_message(ack))
    except ConnectionClosed:
        state.remove_session(register.node_id)
        return
    _log.info("nodebridge: node registered (node_id=%s)", register.node_id)

    # Step 5/6/7: spawn writer + heartbeat + reader, then await them.
    missed = _AtomicCounter()
    writer_task = asyncio.create_task(_writer_loop(ws, outbox))
    heartbeat_task = asyncio.create_task(_heartbeat_loop(state, register.node_id, outbox, missed))
    try:
        await _reader_loop(ws, state, session, missed)
    finally:
        # Step 8: cleanup.
        state.remove_session(register.node_id)
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task
        # Sentinel ``None`` tells the writer to exit; it then closes
        # the socket. Use ``put_nowait`` because the queue may already
        # be full and we don't want to block teardown.
        with suppress(asyncio.QueueFull):
            outbox.put_nowait(_WRITER_SENTINEL)  # type: ignore[arg-type]
        with suppress(asyncio.CancelledError):
            await writer_task
        _log.info("nodebridge: node disconnected (node_id=%s)", register.node_id)


class _AtomicCounter:
    """Single-task counter — asyncio is single-threaded per loop so a
    plain ``int`` suffices. Wrapped for parity with the Rust
    ``AtomicU32`` (signals intent to readers).
    """

    __slots__ = ("value",)

    def __init__(self) -> None:
        self.value = 0

    def reset(self) -> None:
        self.value = 0

    def fetch_add(self, delta: int) -> int:
        prior = self.value
        self.value = prior + delta
        return prior


# Sentinel used to wake the writer task at teardown. Anything that isn't
# a ``NodeBridgeMessage`` works; we pick ``None`` and isinstance-check.
_WRITER_SENTINEL: Any = None


async def _writer_loop(ws: ServerConnection, outbox: asyncio.Queue[NodeBridgeMessage]) -> None:
    """Drain the outbox into the socket. Exits on sentinel or send failure."""
    while True:
        msg = await outbox.get()
        if msg is _WRITER_SENTINEL:
            break
        try:
            text = encode_message(msg)
        except Exception as err:
            _log.warning("nodebridge: serialize failed, dropping frame: %s", err)
            continue
        try:
            await ws.send(text)
        except ConnectionClosed:
            break
    with suppress(Exception):
        await ws.close()


async def _heartbeat_loop(
    state: _ServerState,
    node_id: str,
    outbox: asyncio.Queue[NodeBridgeMessage],
    missed: _AtomicCounter,
) -> None:
    """Probe with Ping every ``heartbeat_secs``.

    Each tick increments the miss counter; the reader loop resets it on
    every inbound frame. After :data:`MAX_MISSED_HEARTBEATS` ticks
    without an inbound frame, the session is dropped.
    """
    interval = max(1, state.cfg.heartbeat_secs)
    while True:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        try:
            outbox.put_nowait(Ping())
        except asyncio.QueueFull:
            # Outbox is jammed — treat as a disconnection signal.
            break
        prior = missed.fetch_add(1)
        if prior + 1 >= MAX_MISSED_HEARTBEATS:
            _log.warning(
                "nodebridge: heartbeat miss threshold hit, dropping (node_id=%s, misses=%d)",
                node_id,
                prior + 1,
            )
            state.remove_session(node_id)
            break


async def _reader_loop(
    ws: ServerConnection,
    state: _ServerState,
    session: NodeSession,
    missed: _AtomicCounter,
) -> None:
    """Dispatch inbound frames until the socket closes."""
    while True:
        try:
            raw = await ws.recv()
        except ConnectionClosed:
            return
        if isinstance(raw, (bytes, bytearray)):
            # Binary frames aren't part of the wire contract.
            continue
        try:
            msg = decode_message(raw)
        except ValidationError as err:
            _log.warning("nodebridge: bad frame, ignoring (node_id=%s): %s", session.id, err)
            continue
        # Every inbound frame is a liveness signal.
        missed.reset()
        session.touch(_now_ms())
        await _handle_client_frame(state, session.id, msg)


async def _handle_client_frame(state: _ServerState, node_id: str, msg: NodeBridgeMessage) -> None:
    """Per-frame dispatch table.

    Mirrors ``handle_client_frame`` in Rust 1:1, including the policy
    that ``Register*`` / ``DispatchJob`` / ``Shutdown`` arriving from the
    client are silently dropped as "wrong direction".
    """
    if isinstance(msg, (Heartbeat, Pong)):
        # Liveness already stamped by the reader loop.
        return
    if isinstance(msg, JobResult):
        pending = state.pending_jobs.pop(msg.job_id, None)
        if pending is None:
            _log.debug(
                "nodebridge: JobResult for unknown job (node_id=%s, job_id=%s)",
                node_id,
                msg.job_id,
            )
            return
        if not pending.future.done():
            pending.future.set_result(msg)
        return
    if isinstance(msg, Telemetry):
        event = HookEvent.Telemetry(
            node_id=msg.node_id,
            metric=msg.metric,
            value=msg.value,
            tags=dict(msg.tags),
        )
        with suppress(Exception):
            await state.hook_bus.emit(event)
        return
    if isinstance(msg, Ping):
        # Reply with Pong if we can get at the session's outbox.
        sess = state.sessions.get(node_id)
        if sess is not None and sess.outbox is not None:
            with suppress(asyncio.QueueFull):
                sess.outbox.put_nowait(Pong())
        return
    # Server-bound-only frames: client violated direction. Ignore.
    if isinstance(msg, (Register, Registered, RegisterRejected, DispatchJob, Shutdown)):
        _log.debug(
            "nodebridge: unexpected direction (node_id=%s, kind=%s)",
            node_id,
            msg.kind,
        )
        return
    # Defensive: any new variant lands here until the dispatcher learns
    # it. Logged at debug to keep CI noise low.
    _log.debug("nodebridge: unhandled frame kind: %s", msg.kind)


# Re-export so callers can ``from corlinman_nodebridge.server import
# Capability`` (mirrors the Rust ``pub use`` in ``lib.rs``).
__all__ += ["Capability", "NodeBridgeError"]
