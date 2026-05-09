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

Iter 7 added :mod:`.runner` tool-allowlist filtering and the
escalation-reject envelope. Iter 8 (this revision) ships
:mod:`.tool_wrapper`:

* :func:`subagent_spawn_tool_schema` — the OpenAI descriptor parents
  drop into ``ChatStart.tools`` so the LLM can emit
  ``ToolCallEvent("subagent.spawn", {...})``;
* :func:`dispatch_subagent_spawn` — async shim that takes one tool
  call's ``args_json``, drives :func:`run_child` (subject to the Rust
  supervisor's slot acquire), and returns the JSON-encoded
  :class:`TaskResult` the gateway feeds back as
  :class:`ToolResult.content`.
"""

from __future__ import annotations

from corlinman_agent.subagent.api import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_TOOL_CALLS,
    DEFAULT_MAX_WALL_SECONDS,
    FinishReason,
    ParentContext,
    TaskResult,
    TaskSpec,
    ToolCallSummary,
)
from corlinman_agent.subagent.runner import (
    SUBAGENT_SPAWN_TOOL,
    TOOL_ALLOWLIST_ESCALATION_ERROR,
    run_child,
)
from corlinman_agent.subagent.tool_wrapper import (
    AGENT_NOT_FOUND_ERROR,
    ARGS_INVALID_ERROR,
    dispatch_subagent_spawn,
    subagent_spawn_tool_schema,
)

__all__ = [
    "AGENT_NOT_FOUND_ERROR",
    "ARGS_INVALID_ERROR",
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_TOOL_CALLS",
    "DEFAULT_MAX_WALL_SECONDS",
    "FinishReason",
    "ParentContext",
    "SUBAGENT_SPAWN_TOOL",
    "TOOL_ALLOWLIST_ESCALATION_ERROR",
    "TaskResult",
    "TaskSpec",
    "ToolCallSummary",
    "dispatch_subagent_spawn",
    "run_child",
    "subagent_spawn_tool_schema",
]
