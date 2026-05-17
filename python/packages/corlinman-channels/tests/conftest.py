"""Pytest fixtures shared across channel adapter tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
import websockets
from websockets.asyncio.server import ServerConnection


@pytest.fixture
def anyio_backend() -> str:
    """Force the asyncio backend; pytest-asyncio is the harness."""
    return "asyncio"


# ---------------------------------------------------------------------------
# WebSocket fixtures (used by OneBot + LogStream tests).
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _ws_server(
    handler: Callable[[ServerConnection], Awaitable[None]],
) -> AsyncIterator[str]:
    """Start a one-off WebSocket server bound to an ephemeral localhost port.

    Yields the ``ws://127.0.0.1:<port>`` URL. The server is torn down on
    exit so each test gets a fresh port (avoids cross-test bleed).
    """
    server = await websockets.serve(handler, "127.0.0.1", 0)
    try:
        socks = server.sockets or []
        if not socks:
            raise RuntimeError("ws server has no socket")
        port = socks[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}"
    finally:
        server.close()
        await server.wait_closed()


@pytest.fixture
def ws_server() -> Callable[[Callable[[ServerConnection], Awaitable[None]]], Any]:
    """Public-test fixture exposing the WS-server context manager."""
    return _ws_server


# ---------------------------------------------------------------------------
# Telegram fixture — httpx.MockTransport that scripts canned responses.
# ---------------------------------------------------------------------------


class TelegramScript:
    """Helper test double that scripts ``getMe`` / ``getUpdates`` responses.

    Each ``add_*`` call appends one response to the queue; the request
    handler pops in arrival order. Designed to be tiny + obvious for
    pytest output — production-grade mocking would use ``respx`` but
    we want to keep the dev dep surface to the basics.
    """

    def __init__(
        self,
        bot_id: int = 999,
        bot_username: str = "corlinman_bot",
    ) -> None:
        self.bot_id = bot_id
        self.bot_username = bot_username
        self.update_batches: list[list[dict[str, Any]]] = []
        self.calls: list[httpx.Request] = []

    def add_updates(self, updates: list[dict[str, Any]]) -> None:
        """Queue one batch of ``getUpdates`` responses."""
        self.update_batches.append(updates)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        if path.endswith("/getMe"):
            body = {
                "ok": True,
                "result": {
                    "id": self.bot_id,
                    "is_bot": True,
                    "username": self.bot_username,
                },
            }
            return httpx.Response(200, json=body)
        if path.endswith("/getUpdates"):
            batch = self.update_batches.pop(0) if self.update_batches else []
            return httpx.Response(200, json={"ok": True, "result": batch})
        return httpx.Response(404, json={"ok": False, "description": "not mocked"})

    def transport(self) -> httpx.MockTransport:
        """Build the ``httpx.MockTransport`` that routes to :meth:`_handle`."""
        return httpx.MockTransport(self._handle)

    def client(self) -> httpx.AsyncClient:
        """Build a ``httpx.AsyncClient`` using the mock transport."""
        return httpx.AsyncClient(transport=self.transport())


@pytest.fixture
def tg_script() -> TelegramScript:
    """Fresh :class:`TelegramScript` per test."""
    return TelegramScript()


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------


def send_json(ws_send: Callable[[str], Awaitable[None]], obj: dict[str, Any]) -> Awaitable[None]:
    """Tiny convenience used by inline WS handlers in tests."""
    return ws_send(json.dumps(obj))


@pytest.fixture
def event_loop_timeout() -> float:
    """Cap individual ``await`` waits so a hung adapter fails fast."""
    return 5.0


async def wait_for(coro: Awaitable[Any], timeout: float = 5.0) -> Any:
    """``asyncio.wait_for`` with a default timeout for ad-hoc test waits."""
    return await asyncio.wait_for(coro, timeout=timeout)
