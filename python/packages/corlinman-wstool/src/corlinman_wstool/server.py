"""``WsToolServer`` — the in-gateway half of the distributed tool bus.

Python port of ``rust/crates/corlinman-wstool/src/server.rs``.

A runner dials ``ws://host/wstool/connect?auth_token=...&runner_id=...&version=...``;
we validate the token (returning HTTP 401 pre-upgrade on mismatch), then
enter a long-lived per-connection task that multiplexes many concurrent
``invoke`` request/reply pairs over one socket.

Heartbeats: the connection task spawns a heartbeat task that wakes every
``heartbeat_secs`` seconds, pushes ``ping`` onto the outbox, and bumps
a missed-ping counter. ``pong`` clears the counter. After
``max_missed_pings`` consecutive missed pings the connection is forcibly
closed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

import websockets
from websockets.asyncio.server import ServerConnection
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Response

from corlinman_wstool.protocol import WsToolMessage
from corlinman_wstool.registry import OUTBOX_CLOSE, ConnHandle, ServerState
from corlinman_wstool.types import (
    Disconnected,
    InvokeOutcome,
    TimeoutError_,
    ToolFailed,
    Unsupported,
    WsToolConfig,
    WsToolError,
)

if TYPE_CHECKING:
    from corlinman_hooks.bus import HookBus

__all__ = ["WsToolServer", "invoke_once"]

_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hook-event emission (best-effort; missing/incompatible hooks are skipped).
# ---------------------------------------------------------------------------


async def _emit_tool_called(
    state: ServerState,
    tool: str,
    runner_id: str,
    duration_ms: int,
    ok: bool,
    error_code: str | None,
) -> None:
    bus = state.hook_bus
    if bus is None:
        return
    try:
        # Lazy import so the wstool package can be used without hooks if
        # the caller passed ``hook_bus=None``.
        from corlinman_hooks.event import HookEvent

        event = HookEvent.ToolCalled(
            tool=tool,
            runner_id=runner_id,
            duration_ms=duration_ms,
            ok=ok,
            error_code=error_code,
            tenant_id=None,
            user_id=None,
        )
        await bus.emit(event)
    except Exception as err:  # pragma: no cover — best-effort emission
        _LOG.debug("wstool: hook emit failed: %s", err)


# ---------------------------------------------------------------------------
# Public server.
# ---------------------------------------------------------------------------


class WsToolServer:
    """Public server handle.

    Construct with :class:`WsToolServer(cfg, hook_bus)`, then await
    :meth:`bind` to start the listener. The server runs until
    :meth:`shutdown` is called.
    """

    def __init__(self, cfg: WsToolConfig, hook_bus: HookBus | None = None) -> None:
        self.state = ServerState(cfg, hook_bus)
        self._server: websockets.server.Server | None = None
        self._bound_host: str | None = None
        self._bound_port: int | None = None

    # ----- lifecycle ----------------------------------------------------
    async def bind(self) -> tuple[str, int]:
        """Bind on ``cfg.bind_host:cfg.bind_port`` and start serving.

        Returns the bound (host, port). The port is non-zero even when
        the caller asked for port 0 — the OS-allocated port is read back
        from the socket.
        """
        cfg = self.state.cfg
        server = await websockets.serve(
            self._handler,
            cfg.bind_host,
            cfg.bind_port,
            process_request=self._process_request,
            # Disable websockets' built-in keepalive ping — we send our
            # own ``WsToolMessage.Ping`` frames per the protocol.
            ping_interval=None,
        )
        self._server = server
        # `server.sockets` is a list of underlying sockets. Take the
        # first; loopback bind only opens one.
        sock = next(iter(server.sockets))  # type: ignore[arg-type]
        host, port = sock.getsockname()[:2]
        self._bound_host = host
        self._bound_port = port
        _LOG.info("wstool server bound on %s:%s", host, port)
        return host, port

    async def shutdown(self) -> None:
        """Close the listener + every live connection. Idempotent."""
        if self._server is None:
            return
        self._server.close()
        with contextlib.suppress(Exception):
            await self._server.wait_closed()
        self._server = None

    # ----- introspection ------------------------------------------------
    def local_addr(self) -> tuple[str, int] | None:
        if self._bound_host is None or self._bound_port is None:
            return None
        return self._bound_host, self._bound_port

    def advertised_tools(self) -> dict[str, str]:
        return self.state.advertised_tools()

    def runner_count(self) -> int:
        return self.state.runner_count()

    async def invoke(
        self,
        tool: str,
        args: object,
        *,
        timeout_ms: int = 30_000,
        cancel_event: asyncio.Event | None = None,
    ) -> object:
        """Dispatch one tool invocation through the connected runners.

        Convenience wrapper around :func:`invoke_once` so callers don't
        have to thread ``state`` around manually.
        """
        return await invoke_once(
            self.state,
            tool=tool,
            args=args,
            timeout_ms=timeout_ms,
            cancel_event=cancel_event,
        )

    # ----- websockets glue ---------------------------------------------
    async def _process_request(
        self, connection: ServerConnection, request: object
    ) -> Response | None:
        """Pre-upgrade auth check.

        Returns a 401 response if the auth_token is missing or wrong,
        which prevents the upgrade entirely so the runner sees a plain
        HTTP error (matching the Rust implementation's behaviour).
        """
        # The ``request.path`` lives on the request object. websockets
        # exposes the parsed URL via ``connection.request.path`` once the
        # upgrade starts, but at pre-upgrade time we read from the raw
        # request.
        path = getattr(request, "path", "")
        parsed = urlparse(path)
        params = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
        token = params.get("auth_token", "")
        runner_id = params.get("runner_id", "")
        cfg = self.state.cfg
        if token != cfg.auth_token:
            body = json.dumps(
                {"code": "auth_rejected", "message": "invalid auth_token"}
            ).encode()
            _LOG.warning("wstool auth rejected for runner_id=%s", runner_id)
            headers = Headers()
            headers["content-type"] = "application/json"
            headers["content-length"] = str(len(body))
            return Response(
                status_code=401,
                reason_phrase="Unauthorized",
                headers=headers,
                body=body,
            )
        if not parsed.path.endswith("/wstool/connect"):
            body = b"not found"
            headers = Headers()
            headers["content-length"] = str(len(body))
            return Response(
                status_code=404,
                reason_phrase="Not Found",
                headers=headers,
                body=body,
            )
        # Stash the runner_id on the connection so the handler can read
        # it back without re-parsing the URL.
        connection.runner_id = runner_id  # type: ignore[attr-defined]
        return None

    async def _handler(self, ws: ServerConnection) -> None:
        runner_id: str = getattr(ws, "runner_id", "") or "unknown"
        await _connection_loop(ws, self.state, runner_id)


# ---------------------------------------------------------------------------
# Per-connection loop.
# ---------------------------------------------------------------------------


async def _connection_loop(
    ws: ServerConnection, state: ServerState, runner_id: str
) -> None:
    """Validate handshake, then multiplex frames until the socket closes
    or the heartbeat check fires the disconnect.
    """
    # Step 1: wait for the runner's first frame. Must be ``accept``.
    try:
        first = await ws.recv()
    except ConnectionClosed:
        _LOG.warning("wstool: socket closed before Accept (runner_id=%s)", runner_id)
        return
    if not isinstance(first, str):
        _LOG.warning("wstool: first frame was binary (runner_id=%s)", runner_id)
        return
    try:
        msg = WsToolMessage.from_json(first)
    except (ValueError, json.JSONDecodeError) as err:
        _LOG.warning("wstool: first frame parse failed: %s", err)
        return
    if not isinstance(msg, WsToolMessage.Accept):
        _LOG.warning(
            "wstool: first frame was not Accept (runner_id=%s, kind=%s)",
            runner_id,
            msg.kind(),
        )
        return

    # Step 2: register the runner + its tools.
    outbox: asyncio.Queue[object] = asyncio.Queue(maxsize=64)
    conn = ConnHandle(
        runner_id=runner_id,
        tools=list(msg.supported_tools),
        outbox=outbox,
    )
    state.register_runner(conn)
    _LOG.info("wstool: runner connected runner_id=%s tools=%d", runner_id, len(conn.tools))

    # Step 3 & 4: spawn writer + heartbeat tasks.
    missed = {"count": 0}
    writer_task = asyncio.create_task(_writer_loop(ws, outbox))
    heartbeat_task = asyncio.create_task(
        _heartbeat_loop(state, conn, missed, state.cfg.heartbeat_secs, state.cfg.max_missed_pings)
    )

    # Step 5: reader loop — dispatch incoming frames.
    try:
        async for raw in ws:
            if isinstance(raw, bytes):
                # We don't speak binary on this protocol; ignore.
                continue
            try:
                frame = WsToolMessage.from_json(raw)
            except (ValueError, json.JSONDecodeError) as err:
                _LOG.warning(
                    "wstool: bad frame from runner_id=%s: %s", runner_id, err
                )
                continue
            _handle_runner_frame(state, conn, missed, frame)
    except ConnectionClosed:
        pass

    # Step 6: cleanup. Fail in-flight requests, purge registrations,
    # stop writer + heartbeat tasks.
    conn.fail_pending()
    state.deregister_runner(runner_id)
    # Ask writer to drain + exit.
    with contextlib.suppress(asyncio.QueueFull):
        outbox.put_nowait(OUTBOX_CLOSE)
    for task in (heartbeat_task, writer_task):
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    _LOG.info("wstool: runner disconnected runner_id=%s", runner_id)


async def _writer_loop(ws: ServerConnection, outbox: asyncio.Queue[object]) -> None:
    """Drain ``outbox`` and forward serialized frames to the socket."""
    try:
        while True:
            msg = await outbox.get()
            if msg is OUTBOX_CLOSE:
                break
            assert hasattr(msg, "to_json")
            text = msg.to_json()  # type: ignore[union-attr]
            try:
                await ws.send(text)
            except ConnectionClosed:
                break
    except asyncio.CancelledError:
        pass
    finally:
        with contextlib.suppress(Exception):
            await ws.close()


async def _heartbeat_loop(
    state: ServerState,
    conn: ConnHandle,
    missed: dict[str, int],
    heartbeat_secs: int,
    max_missed: int,
) -> None:
    """Send ``ping`` frames; disconnect the runner after ``max_missed`` misses."""
    interval = max(1, heartbeat_secs)
    try:
        while True:
            await asyncio.sleep(interval)
            if not await conn.send(WsToolMessage.Ping()):
                break
            missed["count"] += 1
            if missed["count"] >= max_missed:
                _LOG.warning(
                    "wstool: heartbeat miss threshold hit, disconnecting runner_id=%s",
                    conn.runner_id,
                )
                state.deregister_runner(conn.runner_id)
                # Close the outbox so the writer cascades the socket close.
                with contextlib.suppress(asyncio.QueueFull):
                    conn.outbox.put_nowait(OUTBOX_CLOSE)
                break
    except asyncio.CancelledError:
        pass


def _handle_runner_frame(
    state: ServerState,
    conn: ConnHandle,
    missed: dict[str, int],
    msg: object,
) -> None:
    """Server-side dispatch for one incoming runner frame.

    The ``state`` argument is currently unused beyond future-proofing
    for hook emission on runner-originating frames.
    """
    del state  # not yet needed beyond hook plumbing
    if isinstance(msg, WsToolMessage.Pong):
        missed["count"] = 0
        return
    if isinstance(msg, WsToolMessage.Result):
        fut = conn.pending.pop(msg.request_id, None)
        if fut is not None and not fut.done():
            outcome = (
                InvokeOutcome(kind="ok", payload=msg.payload)
                if msg.ok
                else InvokeOutcome(kind="result_error", payload=msg.payload)
            )
            fut.set_result(outcome)
        return
    if isinstance(msg, WsToolMessage.Error):
        fut = conn.pending.pop(msg.request_id, None)
        if fut is not None and not fut.done():
            fut.set_result(
                InvokeOutcome(kind="tool_failed", code=msg.code, message=msg.message)
            )
        return
    if isinstance(msg, WsToolMessage.Progress):
        # Progress is dropped for now; a future change can plumb it
        # through a per-request asyncio.Queue.
        return
    if isinstance(msg, (WsToolMessage.Accept, WsToolMessage.Reject)):
        # Duplicate handshake; ignore.
        return
    if isinstance(
        msg, (WsToolMessage.Invoke, WsToolMessage.Cancel, WsToolMessage.Ping)
    ):
        # Server-bound frames — runner violated direction. Ignore.
        return


# ---------------------------------------------------------------------------
# Public invoke entry point.
# ---------------------------------------------------------------------------


async def invoke_once(
    state: ServerState,
    *,
    tool: str,
    args: object,
    timeout_ms: int,
    cancel_event: asyncio.Event | None = None,
) -> object:
    """Dispatch one ``invoke`` through the connected runners.

    Raises one of the :class:`WsToolError` subclasses on failure;
    returns the JSON-decodable payload on success.

    ``cancel_event`` is checked concurrently with the deadline; firing
    it causes the in-flight request to be marked cancelled (a ``cancel``
    frame is sent to the runner, but we do not wait for ack).
    """
    conn = state.resolve_tool(tool)
    if conn is None:
        raise Unsupported(tool=tool)

    request_id = state.next_request_id()
    loop = asyncio.get_running_loop()
    waiter: asyncio.Future[InvokeOutcome] = loop.create_future()
    conn.pending[request_id] = waiter

    invoke_frame = WsToolMessage.Invoke(
        request_id=request_id,
        tool=tool,
        args=args,
        timeout_ms=timeout_ms,
    )
    if not await conn.send(invoke_frame):
        conn.pending.pop(request_id, None)
        raise Disconnected()

    started = loop.time()
    runner_id = conn.runner_id

    async def _wait_cancel() -> None:
        if cancel_event is None:
            await asyncio.Event().wait()  # never
        else:
            await cancel_event.wait()

    timeout_s = timeout_ms / 1000.0
    cancel_task = asyncio.create_task(_wait_cancel())
    try:
        try:
            done, _pending = await asyncio.wait(
                {waiter, cancel_task},
                timeout=timeout_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            if not cancel_task.done():
                cancel_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cancel_task

        duration_ms = int((loop.time() - started) * 1000)

        # Cancel won the race.
        if cancel_task in done and waiter not in done:
            conn.pending.pop(request_id, None)
            await conn.send(WsToolMessage.Cancel(request_id=request_id))
            await _emit_tool_called(state, tool, runner_id, duration_ms, False, "cancelled")
            raise ToolFailed(code="cancelled", message="caller cancelled")

        # Timeout (neither future resolved).
        if waiter not in done:
            conn.pending.pop(request_id, None)
            await conn.send(WsToolMessage.Cancel(request_id=request_id))
            await _emit_tool_called(state, tool, runner_id, timeout_ms, False, "timeout")
            raise TimeoutError_(millis=timeout_ms)

        outcome = waiter.result()
        if outcome.kind == "ok":
            await _emit_tool_called(state, tool, runner_id, duration_ms, True, None)
            return outcome.payload
        if outcome.kind == "tool_failed":
            await _emit_tool_called(
                state, tool, runner_id, duration_ms, False, outcome.code
            )
            raise ToolFailed(code=outcome.code, message=outcome.message)
        if outcome.kind == "result_error":
            await _emit_tool_called(
                state, tool, runner_id, duration_ms, False, "result_error"
            )
            raise ToolFailed(code="result_error", message=json.dumps(outcome.payload))
        if outcome.kind == "disconnected":
            await _emit_tool_called(
                state, tool, runner_id, duration_ms, False, "disconnected"
            )
            raise Disconnected()
        # Shouldn't happen.
        raise WsToolError(f"unexpected invoke outcome kind: {outcome.kind}")
    finally:
        conn.pending.pop(request_id, None)
