# corlinman-nodebridge

NodeBridge v1 protocol + stub WebSocket server. Python port of
`rust/crates/corlinman-nodebridge` — same wire contract, same lifecycle,
re-implemented on top of `asyncio` and the `websockets` library.

This package ships **no** real device client. Per the project philosophy
it ships the wire contract; a future iOS/Android/macOS/Electron client
can read `protocol.py` and implement the Register/Heartbeat/JobResult/
Telemetry side against this server with no shared code.

## Public surface

```python
from corlinman_nodebridge import (
    Capability,
    NodeBridgeMessage,         # tagged union (pydantic v2 discriminated union)
    NodeBridgeError,
    NodeBridgeServer,
    NodeBridgeServerConfig,
    NodeBridgeClient,          # asyncio client helper (for tests / scripts)
    NodeSession,
    SPEC_VERSION,
)
```

## Connection lifecycle

1. Client dials `ws://host:port/nodebridge/connect`.
2. First frame **must** be `register`. Anything else, or a `register` with
   `signature = None` when `accept_unsigned = False`, produces a
   `register_rejected` frame followed by a close.
3. Server replies with `registered { server_version, heartbeat_secs }`
   and stores the session.
4. Reader loop dispatches inbound frames (`heartbeat`, `job_result`,
   `telemetry`, `pong`). Heartbeat misses are counted; after three the
   session is removed and the socket closed.
5. `NodeBridgeServer.dispatch_job` fans out to the first registered
   session whose capabilities contain `kind`. The returned coroutine
   resolves when the session posts a matching `job_result`, or raises
   `NodeBridgeError` (`NoCapableNode`, `Timeout`, `Protocol`) on
   failure.

## Spec version

`SPEC_VERSION = "1.0.0-alpha"` — bumped on any breaking change to
`NodeBridgeMessage`.
