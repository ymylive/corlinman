"""Port of the unit tests embedded in ``server.rs`` (``find_capable_node`` +
``remove_session``) translated against the Python ``_ServerState`` helper.
"""

from __future__ import annotations

from corlinman_hooks import HookBus
from corlinman_nodebridge import NodeBridgeServerConfig, NodeSession
from corlinman_nodebridge.server import _ServerState


def _state() -> _ServerState:
    return _ServerState(NodeBridgeServerConfig.loopback(True), HookBus(8))


def test_find_capable_node_returns_first_match() -> None:
    state = _state()
    s1 = NodeSession.for_tests("n1", ["camera"])
    s2 = NodeSession.for_tests("n2", ["camera", "system.notify"])
    state.register_session(s1)
    state.register_session(s2)

    got = state.find_capable_node("camera")
    assert got is not None
    assert got.id == "n1"  # first inserted wins

    got = state.find_capable_node("system.notify")
    assert got is not None
    assert got.id == "n2"

    assert state.find_capable_node("missing") is None


def test_remove_session_prunes_capability_index() -> None:
    state = _state()
    s1 = NodeSession.for_tests("n1", ["camera"])
    state.register_session(s1)
    assert state.find_capable_node("camera") is not None
    state.remove_session("n1")
    assert state.find_capable_node("camera") is None
    assert "camera" not in state.capability_index
