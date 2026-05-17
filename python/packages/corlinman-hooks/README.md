# corlinman-hooks

Cross-cutting event bus for the corlinman Python plane. Mirrors the Rust
crate `corlinman-hooks`:

- Three priority tiers (`Critical` < `Normal` < `Low`). `emit` fans out in
  that order and yields between tiers so Critical subscribers always
  observe an event before Normal/Low do.
- Each tier is fan-out via `asyncio.Queue` per subscriber. Dropped
  subscribers are transparent; slow subscribers see `RecvError.Lagged`
  and skip forward when their queue overflows the configured capacity.
- `CancelToken` is a cooperative flag: emitters check it and bail
  without publishing.

Public surface:

```python
from corlinman_hooks import (
    HookBus,
    HookSubscription,
    HookEvent,
    HookPriority,
    HookError,
    RecvError,
    CancelToken,
)

bus = HookBus(capacity=64)
sub = bus.subscribe(HookPriority.NORMAL)
await bus.emit(HookEvent.GatewayStartup(version="0.1.0"))
event = await sub.recv()
```
