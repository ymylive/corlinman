# corlinman-subagent

Subagent supervisor for the parent reasoning loop's `subagent.spawn`
tool. Python port of `rust/crates/corlinman-subagent`.

The Rust crate split the supervisor (depth/concurrency/timeout caps +
lifecycle) from the Python `run_child` runner and bridged the two via
PyO3. On the Python plane both halves live in-process, so this package
is a pure-asyncio supervisor:

- `try_acquire` runs the cap accountant (depth / per-parent /
  per-tenant) and returns a `Slot` whose `release()` decrements both
  counters atomically.
- `Supervisor.spawn_child(...)` (async) wraps the user-supplied agent
  callable, applies the wall-clock timeout via `asyncio.wait_for`, and
  emits `Subagent{Spawned,Completed,TimedOut,DepthCapped}` lifecycle
  events on the optional `HookBus`.
- The "bridge to Python" surface from the Rust crate becomes a
  `typing.Protocol` (`AgentCallable`) that any async callable matching
  `(TaskSpec, ParentContext) -> Awaitable[TaskResult]` can satisfy.

## Public API

```python
from corlinman_subagent import (
    AcquireReject,
    AgentCallable,
    BridgeError,
    FinishReason,
    ParentContext,
    Slot,
    Supervisor,
    SupervisorPolicy,
    TaskResult,
    TaskSpec,
    ToolCallSummary,
)
```

See `tests/` for usage examples covering depth caps, per-parent /
per-tenant concurrency, timeout-as-`TaskResult.finish_reason=Timeout`,
and hook-bus emit shape.
