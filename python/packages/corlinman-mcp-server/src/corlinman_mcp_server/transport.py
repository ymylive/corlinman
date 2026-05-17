"""WebSocket transport for the ``/mcp`` endpoint.

Mirrors the Rust ``server::transport`` module. The auth gate lives
**pre-upgrade** so a wrong / missing token surfaces as HTTP 401 (no WS
upgrade). Successful upgrades enter :func:`connection_loop`, which
reads JSON-RPC frames off the socket and dispatches them through a
:class:`FrameHandler`.

Per-connection state owned by the loop:

* :class:`SessionState` — handshake gate
* :class:`SessionContext` — derived from the resolved :class:`TokenAcl`
* ``max_frame_bytes`` — frames larger than this trigger a WS close
  with code 1009 (Message Too Big) per RFC 6455 §7.4.1
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import structlog
import websockets
from websockets.asyncio.server import ServerConnection
from websockets.asyncio.server import serve as ws_serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from .adapters import SessionContext
from .auth import TokenAcl, resolve_token
from .dispatch import (
    AdapterDispatcher,
    FrameHandler,
    ServerInfo,
    StubMethodNotFoundHandler,
)
from .errors import (
    McpError,
    McpParseError,
    McpTransportError,
)
from .session import SessionState
from .types import (
    JsonRpcRequest,
    JsonRpcResponse,
)

log = structlog.get_logger(__name__)

CLOSE_CODE_MESSAGE_TOO_BIG: int = 1009
"""WebSocket close code for "Message Too Big" (RFC 6455 §7.4.1)."""

DEFAULT_MAX_FRAME_BYTES: int = 1_048_576
"""Default cap on a single inbound frame (1 MiB)."""


@dataclass
class McpServerConfig:
    """Configuration for :class:`McpServer`."""

    tokens: list[TokenAcl] = field(default_factory=list)
    """Accepted bearer-token ACLs. **Empty list = reject everything**
    (fail-closed). Each ACL pins per-capability allowlists + tenant
    scope."""

    max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES
    """Per-frame size limit. Inbound frames over this trigger a 1009
    close."""

    @classmethod
    def with_token(cls, token: str) -> McpServerConfig:
        """Convenience: a single permissive token, default frame cap."""
        return cls(
            tokens=[TokenAcl.permissive(token)],
            max_frame_bytes=DEFAULT_MAX_FRAME_BYTES,
        )

    @classmethod
    def with_acl(cls, acl: TokenAcl) -> McpServerConfig:
        """Convenience: a single fully-pinned ACL."""
        return cls(tokens=[acl], max_frame_bytes=DEFAULT_MAX_FRAME_BYTES)


class McpServer:
    """WebSocket server hosting the ``/mcp`` endpoint.

    Construct with :meth:`__init__`, then call :meth:`bind` to start
    listening on a given host/port. The returned async context manager
    yields a :class:`websockets.asyncio.server.Server` you can stop via
    ``await server.close(); await server.wait_closed()``.
    """

    def __init__(
        self,
        cfg: McpServerConfig,
        handler: FrameHandler | AdapterDispatcher,
    ) -> None:
        self._cfg = cfg
        self._handler = handler

    @classmethod
    def with_stub(cls, cfg: McpServerConfig) -> McpServer:
        """Convenience: construct with the stub
        :class:`StubMethodNotFoundHandler`."""
        return cls(cfg, StubMethodNotFoundHandler())

    @property
    def config(self) -> McpServerConfig:
        return self._cfg

    @property
    def handler(self) -> FrameHandler | AdapterDispatcher:
        return self._handler

    async def bind(self, host: str = "127.0.0.1", port: int = 0):
        """Spawn the WebSocket server. Returns the
        :class:`websockets.asyncio.server.Server` ready to ``await
        server.wait_closed()``; the caller is responsible for shutdown.
        """
        cfg = self._cfg
        handler = self._handler

        async def _connection(ws: ServerConnection) -> None:
            # Token is pre-extracted in process_request; carry it
            # through via the connection.
            acl: TokenAcl | None = getattr(ws, "_corlinman_acl", None)
            if acl is None:
                # Defensive: process_request rejected the upgrade, but
                # in case of any race close cleanly.
                await ws.close(code=1011, reason="missing acl")
                return
            await connection_loop(ws, cfg, handler, acl)

        def _process_request(
            connection: ServerConnection, request: Request
        ) -> Response | None:
            """Pre-upgrade gate. Returning an :class:`Response` aborts
            the upgrade with that HTTP response; returning ``None`` lets
            the upgrade proceed."""
            # Path + query parse.
            target = request.path
            parsed = urllib.parse.urlparse(target)
            if parsed.path != "/mcp":
                return _reject(404, "not found")
            query = urllib.parse.parse_qs(parsed.query)
            token_values = query.get("token", [])
            token = token_values[0] if token_values else ""
            if not token:
                log.warning("mcp: missing/empty token; rejecting pre-upgrade")
                return _reject(401, "missing token")
            acl = resolve_token(cfg.tokens, token)
            if acl is None:
                log.warning("mcp: invalid token; rejecting pre-upgrade")
                return _reject(401, "invalid token")
            log.info(
                "mcp: token resolved; upgrading",
                label=acl.label,
                tenant=acl.effective_tenant(),
            )
            # Stash the ACL on the connection so the handler can pick it up.
            connection._corlinman_acl = acl  # type: ignore[attr-defined]
            return None

        server = await ws_serve(
            _connection,
            host=host,
            port=port,
            # We do our own size enforcement so we can send a 1009 close
            # frame ourselves; tell websockets not to also enforce.
            max_size=None,
            process_request=_process_request,
        )
        return server


def _reject(status: int, message: str) -> Response:
    """Build a JSON-shaped HTTP rejection response (mirrors the Rust
    ``reject_unauthorized`` helper)."""
    body = json.dumps(
        {
            "code": "auth_rejected" if status == 401 else "rejected",
            "message": message,
        }
    ).encode("utf-8")
    headers = Headers(
        [
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(body))),
        ]
    )
    return Response(status, "Unauthorized" if status == 401 else "Not Found", headers, body)


async def connection_loop(
    ws: ServerConnection,
    cfg: McpServerConfig,
    handler: FrameHandler | AdapterDispatcher,
    acl: TokenAcl,
) -> None:
    """Per-connection reader/writer loop. One WebSocket = one
    :class:`SessionState` + one :class:`SessionContext`."""
    session = SessionState()
    session_lock = asyncio.Lock()
    ctx = acl.to_session_context()
    log.info("mcp: connection opened", label=acl.label)

    try:
        async for message in ws:
            # Binary frames are rejected; MCP is text-only.
            if isinstance(message, bytes):
                log.warning("mcp: binary frame rejected; MCP is text-only")
                continue
            assert isinstance(message, str)
            text = message

            # Frame-size gate.
            if len(text) > cfg.max_frame_bytes:
                log.warning(
                    "mcp: oversize frame; closing with 1009",
                    size=len(text),
                    cap=cfg.max_frame_bytes,
                )
                await ws.close(code=CLOSE_CODE_MESSAGE_TOO_BIG, reason="frame too large")
                return

            # Parse the envelope. Bad JSON → JSON-RPC parse error with
            # `id: null` (per spec the id is unknown when parsing
            # fails).
            try:
                raw = json.loads(text)
                req = JsonRpcRequest.model_validate(raw)
            except json.JSONDecodeError as err:
                resp = JsonRpcResponse.err(
                    None,
                    McpParseError(str(err)).to_jsonrpc_error(),
                )
                try:
                    await _send_json(ws, resp)
                except McpTransportError:
                    return
                continue
            except Exception as err:
                # Pydantic / validation error.
                resp = JsonRpcResponse.err(
                    None,
                    McpParseError(str(err)).to_jsonrpc_error(),
                )
                try:
                    await _send_json(ws, resp)
                except McpTransportError:
                    return
                continue

            request_id = req.id
            is_notif = req.is_notification()
            try:
                reply = await handler.handle(req, session, session_lock, ctx)
            except McpError as err:
                if is_notif:
                    log.debug(
                        "mcp: notification handler errored; suppressed per spec",
                        err=str(err),
                    )
                    continue
                resp = JsonRpcResponse.err(request_id, err.to_jsonrpc_error())
                try:
                    await _send_json(ws, resp)
                except McpTransportError as werr:
                    log.warning("mcp: write failed; tearing down", err=str(werr))
                    return
                continue
            except Exception as err:
                if is_notif:
                    log.debug(
                        "mcp: notification handler errored; suppressed per spec",
                        err=str(err),
                    )
                    continue
                # Wrap unexpected exceptions as internal error.
                from .errors import McpInternalError

                resp = JsonRpcResponse.err(
                    request_id,
                    McpInternalError(str(err)).to_jsonrpc_error(),
                )
                try:
                    await _send_json(ws, resp)
                except McpTransportError as werr:
                    log.warning("mcp: write failed; tearing down", err=str(werr))
                    return
                continue

            if reply is None:
                # Handler chose not to reply (notifications).
                continue
            try:
                await _send_json(ws, reply)
            except McpTransportError as err:
                log.warning("mcp: write failed; tearing down", err=str(err))
                return
    except ConnectionClosed:
        pass
    finally:
        log.info("mcp: connection closed")


async def _send_json(ws: ServerConnection, resp) -> None:
    try:
        if hasattr(resp, "model_dump"):
            payload = resp.model_dump()
        else:
            payload = resp
        text = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        raise McpTransportError(f"response serialize: {e}") from e
    try:
        await ws.send(text)
    except Exception as e:
        raise McpTransportError(str(e)) from e


__all__ = [
    "CLOSE_CODE_MESSAGE_TOO_BIG",
    "DEFAULT_MAX_FRAME_BYTES",
    "McpServer",
    "McpServerConfig",
    "connection_loop",
]
