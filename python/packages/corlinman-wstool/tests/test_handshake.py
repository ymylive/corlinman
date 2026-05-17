"""Handshake tests. Exercise the auth layer directly with raw websockets
client calls — no runner client library — so an auth bug can't be
masked by the client happening to also reject.

Mirrors ``rust/crates/corlinman-wstool/tests/handshake.rs``.
"""

from __future__ import annotations

import asyncio

import pytest
import websockets
from websockets.exceptions import InvalidStatus

from corlinman_wstool.protocol import WsToolMessage

from .conftest import Harness, simple_advert


@pytest.mark.asyncio
async def test_handshake_accepts_valid_token(harness: Harness) -> None:
    url = (
        f"{harness.ws_url}/wstool/connect"
        f"?auth_token={harness.token}&runner_id=rx-1&version=0.1.0"
    )
    async with websockets.connect(url, ping_interval=None) as ws:
        msg = WsToolMessage.Accept(
            server_version="0.1.0",
            heartbeat_secs=15,
            supported_tools=[simple_advert("handshake.echo")],
        )
        await ws.send(msg.to_json())

        # Wait until the server registers the tool -> proof of acceptance.
        deadline = asyncio.get_running_loop().time() + 2.0
        while True:
            if "handshake.echo" in harness.server.advertised_tools():
                break
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("tool never registered")
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_handshake_rejects_invalid_token(harness: Harness) -> None:
    url = (
        f"{harness.ws_url}/wstool/connect"
        "?auth_token=WRONG&runner_id=rx-2&version=0.1.0"
    )
    with pytest.raises(InvalidStatus) as excinfo:
        async with websockets.connect(url, ping_interval=None):
            pass
    # The HTTP status is 401.
    assert excinfo.value.response.status_code == 401
    # Server's runner count is still zero.
    await asyncio.sleep(0)
    assert harness.server.runner_count() == 0
