"""`corlinman-nodebridge` — v1 NodeBridge protocol + stub WebSocket server.

Python port of ``rust/crates/corlinman-nodebridge``.

Scope (deliberate): this package ships the **wire contract** for device
clients (iOS / Android / macOS / Linux / future Electron) to target. It
does *not* ship a real client; :class:`NodeBridgeClient` exists only so
the test suite and integration scripts have a typed asyncio handle.

Public surface:

- :data:`NodeBridgeMessage` — tagged union covering every frame the v1
  protocol defines, JSON-serialised over WebSocket text frames. Use
  :func:`encode_message` / :func:`decode_message` to round-trip.
- :class:`Capability` — what a node advertises at registration time.
- :class:`NodeBridgeServer` + :class:`NodeBridgeServerConfig` — the
  asyncio reference server. Accepts registrations, monitors heartbeats,
  routes ``DispatchJob`` to the first capable session, and forwards
  ``Telemetry`` to ``corlinman_hooks.HookEvent.Telemetry`` on the bus.
- :class:`NodeSession` — per-connection state.
- :class:`NodeBridgeClient` — asyncio client helper.
- :class:`NodeBridgeError` plus the concrete subclasses
  (:class:`NodeBridgeRegisterRejected`, :class:`NodeBridgeTimeout`,
  :class:`NodeBridgeNoCapableNode`, :class:`NodeBridgeProtocolError`,
  :class:`NodeBridgeBindError`, :class:`NodeBridgeInvalidListenAddr`).

The spec version advertised in the ``Registered`` frame is
:data:`SPEC_VERSION` (``"1.0.0-alpha"``); bump it on any breaking
change to the :data:`NodeBridgeMessage` union.
"""

from __future__ import annotations

from corlinman_nodebridge.client import NodeBridgeClient
from corlinman_nodebridge.protocol import (
    Capability,
    DispatchJob,
    Heartbeat,
    JobResult,
    NodeBridgeMessage,
    NodeBridgeMessageAdapter,
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
from corlinman_nodebridge.server import (
    DEFAULT_HEARTBEAT_SECS,
    MAX_MISSED_HEARTBEATS,
    NODEBRIDGE_PATH,
    SPEC_VERSION,
    NodeBridgeServer,
    NodeBridgeServerConfig,
)
from corlinman_nodebridge.session import NodeSession
from corlinman_nodebridge.types import (
    NodeBridgeBindError,
    NodeBridgeError,
    NodeBridgeInvalidListenAddr,
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
    "Capability",
    "DispatchJob",
    "Heartbeat",
    "JobResult",
    "NodeBridgeBindError",
    "NodeBridgeClient",
    "NodeBridgeError",
    "NodeBridgeInvalidListenAddr",
    "NodeBridgeMessage",
    "NodeBridgeMessageAdapter",
    "NodeBridgeNoCapableNode",
    "NodeBridgeProtocolError",
    "NodeBridgeRegisterRejected",
    "NodeBridgeServer",
    "NodeBridgeServerConfig",
    "NodeBridgeTimeout",
    "NodeSession",
    "Ping",
    "Pong",
    "Register",
    "RegisterRejected",
    "Registered",
    "Shutdown",
    "Telemetry",
    "decode_message",
    "encode_message",
]
