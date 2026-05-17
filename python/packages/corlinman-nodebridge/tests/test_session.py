"""Port of ``rust/crates/corlinman-nodebridge/src/session.rs`` tests."""

from __future__ import annotations

from corlinman_nodebridge import NodeSession


def test_advertises_returns_true_only_for_known_capability() -> None:
    s = NodeSession.for_tests("n1", ["system.notify", "camera"])
    assert s.advertises("system.notify")
    assert s.advertises("camera")
    assert not s.advertises("missing")


def test_touch_updates_last_heartbeat() -> None:
    s = NodeSession.for_tests("n1", [])
    assert s.last_heartbeat_ms == 0
    s.touch(1_700_000_000_000)
    assert s.last_heartbeat_ms == 1_700_000_000_000
