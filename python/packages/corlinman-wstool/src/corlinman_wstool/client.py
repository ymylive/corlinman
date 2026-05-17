"""``WsToolRunner`` — client side of the WS tool-bus protocol.

Python port of ``rust/crates/corlinman-wstool/src/runner.rs``.

A runner is usually a separate process (or even machine) that owns one
or more tool handlers, dials the gateway's WS endpoint, and serves
invocations for as long as the connection is alive.

Reconnect: :meth:`WsToolRunner.run_forever` rebuilds the socket and
re-sends the ``accept`` advertisement on any unexpected disconnect.
In-flight requests on the old socket are abandoned — the server
surfaces them as ``error{code: "disconnected"}`` to its caller, so the
runner does not need to ship replay state.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Awaitable, Callable, Protocol

import websockets
from websockets.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from corlinman_wstool.protocol import ToolAdvert, WsToolMessage
from corlinman_wstool.types import AcceptInfo, ToolError

__all__ = [
    "ProgressSink",
    "ToolHandler",
    "WsToolRunner",
    "build_connect_url",
    "url_encode",
]

_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Handler interface + progress sink.
# ---------------------------------------------------------------------------


class ProgressSink:
    """Write-end of the per-invocation progress channel.

    Each call to :meth:`emit` produces one ``progress`` frame on the
    wire. Implementations may safely ignore the sink.
    """

    __slots__ = ("_request_id", "_outbox", "_closed")

    def __init__(self, request_id: str, outbox: asyncio.Queue[object]) -> None:
        self._request_id = request_id
        self._outbox = outbox
        self._closed = False

    @property
    def request_id(self) -> str:
        return self._request_id

    async def emit(self, data: object) -> None:
        """Best-effort emit; silently drops if the writer is gone."""
        if self._closed:
            return
        try:
            await self._outbox.put(
                WsToolMessage.Progress(request_id=self._request_id, data=data)
            )
        except Exception:  # pragma: no cover — closed outbox
            self._closed = True

    @classmethod
    def discarding(cls) -> ProgressSink:
        """Build a sink whose :meth:`emit` calls go nowhere.

        Useful for unit tests that call :meth:`ToolHandler.invoke`
        directly without an attached runner.
        """
        sink = cls.__new__(cls)
        sink._request_id = "discarding"
        sink._outbox = asyncio.Queue(maxsize=1)
        sink._closed = True
        return sink


class ToolHandler(Protocol):
    """What a runner can serve.

    Implementors do the actual work of executing a tool once we've
    framed arguments for them. ``progress`` is a cheap sink for
    mid-flight updates; ``cancel`` is an :class:`asyncio.Event` set
    when the gateway sent a ``cancel`` frame for this request — long
    running handlers should ``await`` on it (or race it with their
    own work via :func:`asyncio.wait`).

    Successful returns are serialized as the ``payload`` of the
    terminal ``result`` frame. Raising :class:`ToolError` produces an
    ``error`` frame with the carried ``code`` + ``message``.
    """

    async def invoke(
        self,
        tool: str,
        args: object,
        progress: ProgressSink,
        cancel: asyncio.Event,
    ) -> object:  # pragma: no cover — protocol stub
        ...


# ---------------------------------------------------------------------------
# URL helpers.
# ---------------------------------------------------------------------------


def url_encode(s: str) -> str:
    """Minimal percent-encoder.

    Mirrors the Rust crate's ``urlenc`` so the wire format matches byte
    for byte: only the unreserved characters are passed through; the
    rest become ``%HH`` (uppercase hex).
    """
    unreserved = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~")
    out: list[str] = []
    for ch in s:
        if ch in unreserved:
            out.append(ch)
        else:
            for b in ch.encode("utf-8"):
                out.append(f"%{b:02X}")
    return "".join(out)


def build_connect_url(
    gateway_url: str, auth_token: str, runner_id: str, version: str = "0.1.0"
) -> str:
    """Map ``ws://host:port`` -> ``ws://host:port/wstool/connect?...``.

    Mirrors ``rust/crates/corlinman-wstool/src/runner.rs::build_connect_url``.
    """
    base = gateway_url.rstrip("/")
    path = "" if base.endswith("/wstool/connect") else "/wstool/connect"
    return (
        f"{base}{path}?auth_token={url_encode(auth_token)}"
        f"&runner_id={url_encode(runner_id)}"
        f"&version={url_encode(version)}"
    )


# ---------------------------------------------------------------------------
# Runner.
# ---------------------------------------------------------------------------


class WsToolRunner:
    """Connected runner session.

    Call :meth:`connect` to dial + handshake; then :meth:`serve_with`
    to process invocations until the gateway hangs up.
    """

    def __init__(
        self,
        gateway_url: str,
        auth_token: str,
        runner_id: str,
        advert: list[ToolAdvert],
        ws: ClientConnection,
        accept_info: AcceptInfo,
    ) -> None:
        self.gateway_url = gateway_url
        self.auth_token = auth_token
        self._runner_id = runner_id
        self._advert = advert
        self._ws: ClientConnection | None = ws
        self._accept_info = accept_info
        self._cancel_all = asyncio.Event()
        self._per_req: dict[str, asyncio.Event] = {}
        self._handler_tasks: set[asyncio.Task[None]] = set()

    @property
    def runner_id(self) -> str:
        return self._runner_id

    def server_info(self) -> AcceptInfo:
        return self._accept_info

    @classmethod
    async def connect(
        cls,
        gateway_url: str,
        auth_token: str,
        runner_id: str,
        tools: list[ToolAdvert],
    ) -> WsToolRunner:
        """Dial the gateway, exchange handshake, and return a ready runner.

        The handshake is: connect (HTTP 101 upgrade) -> send ``accept``
        frame with the advertised tools. The Rust server doesn't echo an
        Accept of its own — silence after auth means "accepted".
        """
        url = build_connect_url(gateway_url, auth_token, runner_id)
        # Disable websockets' built-in keepalive ping; the protocol
        # carries its own ``ping``/``pong`` frames.
        ws = await websockets.connect(url, ping_interval=None)
        accept = WsToolMessage.Accept(
            server_version="0.1.0",
            heartbeat_secs=15,
            supported_tools=list(tools),
        )
        await ws.send(accept.to_json())
        info = AcceptInfo(server_version="unknown", heartbeat_secs=15)
        _LOG.info("wstool: runner connected runner_id=%s", runner_id)
        return cls(
            gateway_url=gateway_url,
            auth_token=auth_token,
            runner_id=runner_id,
            advert=list(tools),
            ws=ws,
            accept_info=info,
        )

    # ----- serve loop ---------------------------------------------------
    async def serve_with(self, handler: ToolHandler) -> None:
        """Serve invocations with ``handler`` until the socket closes.

        Returns normally when the connection ends. If the surrounding
        task is cancelled (``serve.cancel()``), the runner-wide cancel
        event fires, every in-flight handler observes it via its own
        per-request event, and the writer task drains and closes.
        """
        ws = self._ws
        if ws is None:
            raise RuntimeError("runner already consumed")
        self._ws = None

        outbox: asyncio.Queue[object] = asyncio.Queue(maxsize=64)
        # Writer task: drain outbox -> socket.
        writer_task = asyncio.create_task(self._writer_loop(ws, outbox))

        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    continue
                try:
                    msg = WsToolMessage.from_json(raw)
                except (ValueError, json.JSONDecodeError) as err:
                    _LOG.warning("wstool runner: bad frame: %s", err)
                    continue
                if isinstance(msg, WsToolMessage.Ping):
                    await outbox.put(WsToolMessage.Pong())
                    continue
                if isinstance(msg, WsToolMessage.Invoke):
                    self._spawn_handler_task(handler, msg, outbox)
                    continue
                if isinstance(msg, WsToolMessage.Cancel):
                    ev = self._per_req.pop(msg.request_id, None)
                    if ev is not None:
                        ev.set()
                    continue
                # accept/reject/progress/result/error from server side:
                # ignore. (Server shouldn't be sending them.)
        except ConnectionClosed:
            pass
        finally:
            # Cancel all handler tasks; they observe via per-req events
            # (set below) and exit promptly.
            self._cancel_all.set()
            for ev in list(self._per_req.values()):
                ev.set()
            for task in list(self._handler_tasks):
                task.cancel()
            for task in list(self._handler_tasks):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            self._handler_tasks.clear()
            # Stop the writer.
            writer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await writer_task
            with contextlib.suppress(Exception):
                await ws.close()

    async def _writer_loop(
        self, ws: ClientConnection, outbox: asyncio.Queue[object]
    ) -> None:
        try:
            while True:
                msg = await outbox.get()
                assert hasattr(msg, "to_json")
                text = msg.to_json()  # type: ignore[union-attr]
                try:
                    await ws.send(text)
                except ConnectionClosed:
                    break
        except asyncio.CancelledError:
            pass

    def _spawn_handler_task(
        self,
        handler: ToolHandler,
        msg: object,
        outbox: asyncio.Queue[object],
    ) -> None:
        invoke = msg  # WsToolMessage.Invoke
        request_id = invoke.request_id  # type: ignore[attr-defined]
        tool = invoke.tool  # type: ignore[attr-defined]
        args = invoke.args  # type: ignore[attr-defined]

        cancel_evt = asyncio.Event()
        self._per_req[request_id] = cancel_evt
        sink = ProgressSink(request_id=request_id, outbox=outbox)

        async def _runner() -> None:
            try:
                result = await handler.invoke(tool, args, sink, cancel_evt)
                frame = WsToolMessage.Result(
                    request_id=request_id, ok=True, payload=result
                )
            except ToolError as err:
                frame = WsToolMessage.Error(
                    request_id=request_id, code=err.code, message=err.message
                )
            except asyncio.CancelledError:
                # Handler was aborted (runner shutting down). No frame
                # — the writer is shutting down anyway.
                self._per_req.pop(request_id, None)
                raise
            except Exception as err:  # noqa: BLE001 — surface everything
                frame = WsToolMessage.Error(
                    request_id=request_id,
                    code="handler_exception",
                    message=f"{type(err).__name__}: {err}",
                )
            self._per_req.pop(request_id, None)
            with contextlib.suppress(Exception):
                await outbox.put(frame)

        task = asyncio.create_task(_runner())
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_tasks.discard)

    # ----- shutdown ------------------------------------------------------
    async def close(self) -> None:
        self._cancel_all.set()
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None

    # ----- reconnect loop -----------------------------------------------
    @classmethod
    async def run_forever(
        cls,
        gateway_url: str,
        auth_token: str,
        runner_id: str,
        tools: list[ToolAdvert],
        make_handler: Callable[[], ToolHandler] | Callable[[], Awaitable[ToolHandler]],
        shutdown: asyncio.Event,
    ) -> None:
        """Reconnect-forever loop with exponential backoff.

        Each new connection gets a fresh handler from ``make_handler``.
        Stops when ``shutdown`` is set.
        """
        delay = 1.0
        while not shutdown.is_set():
            try:
                runner = await cls.connect(gateway_url, auth_token, runner_id, tools)
                handler_or_coro = make_handler()
                if asyncio.iscoroutine(handler_or_coro):
                    handler = await handler_or_coro
                else:
                    handler = handler_or_coro  # type: ignore[assignment]
                serve_task = asyncio.create_task(runner.serve_with(handler))
                shutdown_task = asyncio.create_task(shutdown.wait())
                done, pending = await asyncio.wait(
                    {serve_task, shutdown_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if shutdown_task in done:
                    serve_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await serve_task
                    return
                for t in pending:
                    t.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await t
                delay = 1.0
            except Exception as err:  # noqa: BLE001
                _LOG.warning("wstool runner: connect failed: %s", err)
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=delay)
                return
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2.0, 30.0)
