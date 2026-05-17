"""Smoke tests for :class:`NodeBridgeClient`.

The client helper has no Rust analog (the Rust crate ships server +
contract only); these tests cover the handshake state machine.
"""

from __future__ import annotations

import pytest
from corlinman_nodebridge import (
    SPEC_VERSION,
    Capability,
    NodeBridgeClient,
    NodeBridgeRegisterRejected,
)

from tests.conftest import Harness


async def test_client_connect_succeeds_when_unsigned_allowed(harness) -> None:
    h: Harness = await harness(True, 15)
    async with await NodeBridgeClient.connect(
        h.ws_url,
        node_id="ios-client-1",
        node_type="ios",
        capabilities=[Capability.new("camera", "1.0", {"type": "object"})],
        auth_token="tok",
        version="0.1.0",
    ) as client:
        assert client.node_id == "ios-client-1"
        assert client.server_version == SPEC_VERSION
        assert client.heartbeat_secs == 15


async def test_client_connect_raises_on_unsigned_rejection(harness) -> None:
    h: Harness = await harness(False, 15)
    with pytest.raises(NodeBridgeRegisterRejected) as info:
        await NodeBridgeClient.connect(
            h.ws_url,
            node_id="ios-client-2",
            node_type="ios",
            capabilities=[],
            auth_token="tok",
            version="0.1.0",
            signature=None,
        )
    assert info.value.code == "unsigned_registration"
