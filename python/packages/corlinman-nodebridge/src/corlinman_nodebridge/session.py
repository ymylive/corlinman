"""Per-connection state held by the server once a client finishes
registration.

Mirrors ``rust/crates/corlinman-nodebridge/src/session.rs``.
:class:`NodeSession` is lightweight: the long-lived state lives in the
server's session map, and a session mostly exists so tests and
diagnostics can ask "which nodes are connected, advertising what, and
when did we last hear from them?".
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from corlinman_nodebridge.protocol import Capability, NodeBridgeMessage

__all__ = ["NodeSession"]


@dataclass
class NodeSession:
    """A single connected client.

    Cheap to share by reference (the outbox is an :class:`asyncio.Queue`
    that the writer task drains). The Rust version is ``Clone`` because
    its internal handles are themselves cloneable; in Python sharing the
    same instance plays the same role.

    ``outbox`` is ``None`` in test fixtures that want to build a session
    without a real socket (see :meth:`for_tests`).
    """

    id: str
    node_type: str
    capabilities: list[Capability]
    version: str
    # Wall-clock millis since the Unix epoch at last heartbeat/frame.
    last_heartbeat_ms: int = 0
    # Write half. ``None`` in test fixtures.
    outbox: asyncio.Queue[NodeBridgeMessage] | None = field(default=None, repr=False)

    def touch(self, at_ms: int) -> None:
        """Update :attr:`last_heartbeat_ms` to ``at_ms``.

        Called from the reader loop on every inbound frame, not just
        :class:`Heartbeat` — any client liveness (even a
        :class:`JobResult`) proves the socket is alive.
        """
        self.last_heartbeat_ms = at_ms

    def advertises(self, kind: str) -> bool:
        """Whether this session advertises ``kind``.

        Used by ``ServerState.find_capable_node``.
        """
        return any(c.name == kind for c in self.capabilities)

    @classmethod
    def for_tests(cls, node_id: str, caps: list[str]) -> NodeSession:
        """Test-only builder: skip the socket plumbing and still
        exercise capability lookup.

        Mirrors the Rust ``NodeSession::for_tests(id, caps)`` helper —
        the Python parameter is named ``node_id`` to avoid shadowing
        the builtin ``id``.
        """
        return cls(
            id=node_id,
            node_type="other",
            capabilities=[
                Capability(name=name, version="1.0", params_schema={"type": "object"})
                for name in caps
            ],
            version="test",
            last_heartbeat_ms=0,
            outbox=None,
        )
