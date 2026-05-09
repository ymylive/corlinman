"""Subagent delegation runtime — Python half.

The parent reasoning loop's ``subagent.spawn`` tool is dispatched to the
Rust supervisor (``corlinman-subagent`` crate, owns the depth /
concurrency / time-budget caps) which then re-enters this module via
PyO3 (lands in iter 5) to actually drive a child :class:`ReasoningLoop`.

This iteration (iter 4 of the D3 plan in
``docs/design/phase4-w4-d3-design.md``) ships the **Python-side** bits:

* :mod:`.api` — :class:`TaskSpec`, :class:`TaskResult`,
  :class:`ParentContext`, :class:`FinishReason` dataclasses that mirror
  the Rust types in ``rust/crates/corlinman-subagent/src/types.rs``
  one-to-one. They form the JSON envelope on the wire so PyO3 can
  marshal in either direction without bespoke conversion logic.
* :mod:`.runner` — :func:`run_child`, the happy-path child driver.
  Builds a fresh :class:`ChatStart`, optionally seeds a fresh persona
  row (when a :class:`PersonaStore` is wired in), drives a fresh
  :class:`ReasoningLoop`, drains the event stream into a
  :class:`TaskResult`. No timeout / cap / tool-allowlist filtering yet
  — those live behind the Rust supervisor (iter 5+) and downstream
  iters in this module.

What's intentionally NOT here in iter 4: the ``subagent.spawn`` tool
registration in the agent registry (iter 8), the PyO3 bridge wiring
(iter 5), and tool-allowlist filtering (iter 7).
"""

from __future__ import annotations

from corlinman_agent.subagent.api import (
    DEFAULT_MAX_TOOL_CALLS,
    DEFAULT_MAX_WALL_SECONDS,
    FinishReason,
    ParentContext,
    TaskResult,
    TaskSpec,
    ToolCallSummary,
)
from corlinman_agent.subagent.runner import run_child

__all__ = [
    "DEFAULT_MAX_TOOL_CALLS",
    "DEFAULT_MAX_WALL_SECONDS",
    "FinishReason",
    "ParentContext",
    "TaskResult",
    "TaskSpec",
    "ToolCallSummary",
    "run_child",
]
