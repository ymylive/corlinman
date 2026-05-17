# corlinman-wstool

Python port of the Rust `corlinman-wstool` crate.

Distributed tool-execution protocol over WebSocket: a runner dials a
gateway, advertises a set of tools, and serves invocations over a single
multiplexed WebSocket connection. The wire protocol is JSON over text
frames and is compatible with the Rust implementation — a Python runner
can talk to a Rust gateway and vice versa.

## Modules

- `protocol` — wire-level frame definitions (`WsToolMessage`, `ToolAdvert`).
- `types` — value types (`WsToolConfig`, `AcceptInfo`, `FetchedBlob`, ...).
- `registry` — tool registry helpers (server-side handles, in-flight bookkeeping).
- `server` — `WsToolServer`: the in-gateway half of the protocol.
- `client` — `WsToolRunner` + `ToolHandler`: the runner-side client.

See module docstrings for design notes.
