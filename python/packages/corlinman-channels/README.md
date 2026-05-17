# corlinman-channels (Python)

Python port of the Rust `corlinman-channels` crate.

Inbound channel adapters that bridge external transports into a uniform,
normalized `InboundEvent` stream:

- **OneBot v11 WebSocket** (QQ via gocq / Lagrange / NapCatQQ) — forward-WS
  client with reconnect + heartbeat.
- **LogStream WebSocket** — generic log-line subscriber that decodes
  newline-delimited JSON frames.
- **Telegram** — HTTPS `getUpdates` long-poll (25s timeout, offset
  bookkeeping, backoff on transient errors).

## Shape

All three adapters expose the same shape:

```python
async with adapter:                    # connect / authenticate
    async for event in adapter.inbound():
        # event is corlinman_channels.InboundEvent (a normalized envelope:
        # channel slug + opaque payload + optional UserId resolution hook)
        ...
```

Internally each module also exports the wire-level types so callers can opt
into transport-specific details (OneBot CQ segments, Telegram MessageEntity,
etc.) without re-implementing the parse layer.

## Cross-references

- `corlinman_identity.UserId` — when an adapter has access to an
  `IdentityStore`, it can resolve the per-channel id (`qq:1234`,
  `telegram:9876`) to a canonical opaque user id.
- Mirrors `rust/crates/corlinman-channels/src/` 1:1.
