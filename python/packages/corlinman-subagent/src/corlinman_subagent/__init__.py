"""`corlinman-subagent` — Python supervisor for the parent reasoning loop's
``subagent.spawn`` tool.

Python port of ``rust/crates/corlinman-subagent``. The Rust crate split the
supervisor (depth/concurrency/timeout caps + lifecycle) from the Python
``run_child`` runner and bridged the two via PyO3. On the Python plane both
halves live in-process so this package is a pure-asyncio supervisor — no
PyO3, no JSON marshalling, just a single :class:`Supervisor` class that
holds the caps and drives the agent callable under a wall-clock budget.

Public API mirrors the Rust crate's prelude:

- :class:`Supervisor` — cap accountant + spawn entry point.
- :class:`SupervisorPolicy` — knobs for the three concurrency caps plus
  the wall-clock ceiling.
- :class:`Slot` — drop-guard for an acquired concurrency reservation.
- :class:`AgentCallable` — protocol any async agent runner must satisfy.
- :class:`TaskSpec` / :class:`TaskResult` / :class:`ParentContext` /
  :class:`ToolCallSummary` / :class:`FinishReason` — wire envelope.
- :class:`AcquireReject` / :class:`AcquireRejectError` /
  :class:`SubagentError` / :class:`SubagentTimeoutError` /
  :class:`BridgeError` — error surface.
- :data:`DEFAULT_MAX_DEPTH` / :data:`DEFAULT_MAX_TOOL_CALLS` /
  :data:`DEFAULT_MAX_WALL_SECONDS` — module-level defaults that match
  the Rust ``types::defaults`` constants.
"""

from __future__ import annotations

from corlinman_subagent.errors import (
    AcquireReject,
    AcquireRejectError,
    BridgeError,
    SubagentError,
    SubagentTimeoutError,
)
from corlinman_subagent.supervisor import (
    AgentCallable,
    Slot,
    Supervisor,
    SupervisorPolicy,
)
from corlinman_subagent.types import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_TOOL_CALLS,
    DEFAULT_MAX_WALL_SECONDS,
    FinishReason,
    ParentContext,
    TaskResult,
    TaskSpec,
    ToolCallSummary,
)

__all__ = [
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_TOOL_CALLS",
    "DEFAULT_MAX_WALL_SECONDS",
    "AcquireReject",
    "AcquireRejectError",
    "AgentCallable",
    "BridgeError",
    "FinishReason",
    "ParentContext",
    "Slot",
    "SubagentError",
    "SubagentTimeoutError",
    "Supervisor",
    "SupervisorPolicy",
    "TaskResult",
    "TaskSpec",
    "ToolCallSummary",
]
