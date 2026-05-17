"""Public type surface for the subagent runtime.

Mirrors ``rust/crates/corlinman-subagent/src/types.rs``. These types
form the envelope that crosses the boundary between the parent's
``subagent.spawn`` tool call and the child's reasoning loop.

The Rust crate marked these ``#[derive(Serialize, Deserialize)]`` so
the PyO3 bridge could ferry JSON across the FFI seam. On the Python
plane both sides run in the same process so the in-memory dataclass is
the wire format; :meth:`TaskSpec.to_dict` / :meth:`from_dict` (and the
matching :class:`TaskResult` / :class:`ParentContext` helpers) exist
solely for hook-bus / forensic-trace payloads that still get
JSON-encoded for cross-process transport.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = [
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_TOOL_CALLS",
    "DEFAULT_MAX_WALL_SECONDS",
    "FinishReason",
    "ParentContext",
    "TaskResult",
    "TaskSpec",
    "ToolCallSummary",
]


# ---------------------------------------------------------------------------
# Defaults — mirror ``types::defaults`` in the Rust crate verbatim.
# ---------------------------------------------------------------------------

#: Hard ceiling on a child's wall-clock budget. ``task.max_wall_seconds``
#: may *lower* this but never raise it; the supervisor enforces the upper
#: bound.
DEFAULT_MAX_WALL_SECONDS: int = 60

#: Cap on the child's ``_MAX_ROUNDS`` (parent loop's own ceiling is 8;
#: children get a slightly higher allowance because they often chain
#: search → fetch → summarise).
DEFAULT_MAX_TOOL_CALLS: int = 12

#: Maximum nesting depth (parent → child → grandchild). ``>=`` this
#: triggers the supervisor's ``depth_capped`` short-circuit.
DEFAULT_MAX_DEPTH: int = 2


# ---------------------------------------------------------------------------
# FinishReason — discriminator the parent's LLM branches on.
# ---------------------------------------------------------------------------


class FinishReason(str, Enum):
    """Why the child stopped.

    Lowercase snake_case string values mirror the Rust serde
    ``#[serde(rename_all = "snake_case")]`` wire shape so the on-disk
    / hook-bus JSON keeps the same discriminant the Rust crate's
    consumers already parse.
    """

    #: Normal termination — provider returned a final response.
    STOP = "stop"
    #: Hit ``max_tool_calls`` without producing a final.
    LENGTH = "length"
    #: Wall-clock budget exhausted; partial output preserved.
    TIMEOUT = "timeout"
    #: Runner raised; see :attr:`TaskResult.error`.
    ERROR = "error"
    #: Parent depth >= ``max_depth``; child loop never invoked.
    DEPTH_CAPPED = "depth_capped"
    #: Concurrency / tenant quota / allowlist escalation rejected the
    #: spawn before any work happened.
    REJECTED = "rejected"

    def is_pre_spawn_rejection(self) -> bool:
        """Mirror of the Rust ``is_pre_spawn_rejection`` method.

        The supervisor uses this when short-circuiting a spawn: no
        child loop was driven, so no output / tool calls / session.
        """
        return self in (FinishReason.DEPTH_CAPPED, FinishReason.REJECTED)

    def as_str(self) -> str:
        """Lowercase snake_case string representation.

        Matches the Rust ``as_str()`` helper that returns ``&'static
        str`` for hook-event payloads without a JSON round-trip.
        """
        return self.value


# ---------------------------------------------------------------------------
# TaskSpec — parent-loop request to spawn one child.
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class TaskSpec:
    """Mirror of the Rust ``TaskSpec`` struct.

    ``tool_allowlist`` semantics:

    - ``None`` → inherit parent's tool set.
    - ``[]`` (empty list) → pure LLM call, no tools.
    - non-empty list → must be a subset of parent's tools or the
      escalation check rejects it.

    ``extra_context`` is ordered (Python 3.7+ ``dict`` is
    insertion-ordered) which substitutes for the Rust ``BTreeMap``'s
    lexicographic guarantee. Callers that need byte-stable JSON should
    insert keys sorted; see :meth:`to_dict` for the canonical
    serialization.
    """

    goal: str
    tool_allowlist: list[str] | None = None
    max_wall_seconds: int = DEFAULT_MAX_WALL_SECONDS
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS
    extra_context: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize matching the Rust crate's ``serde_json`` wire shape.

        ``tool_allowlist`` is elided when ``None`` and ``extra_context``
        is elided when empty (mirrors
        ``#[serde(skip_serializing_if = ...)]``). The ``max_*`` defaults
        always serialize so the consumer sees explicit values.
        """
        out: dict[str, Any] = {"goal": self.goal}
        if self.tool_allowlist is not None:
            out["tool_allowlist"] = list(self.tool_allowlist)
        out["max_wall_seconds"] = self.max_wall_seconds
        out["max_tool_calls"] = self.max_tool_calls
        if self.extra_context:
            # Sort keys to match Rust's BTreeMap iteration order so the
            # JSON envelope is byte-stable for replay fingerprints.
            out["extra_context"] = dict(sorted(self.extra_context.items()))
        return out

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> TaskSpec:
        """Inverse of :meth:`to_dict`. Defaults fill missing fields."""
        return cls(
            goal=payload["goal"],
            tool_allowlist=(
                list(payload["tool_allowlist"]) if "tool_allowlist" in payload else None
            ),
            max_wall_seconds=payload.get("max_wall_seconds", DEFAULT_MAX_WALL_SECONDS),
            max_tool_calls=payload.get("max_tool_calls", DEFAULT_MAX_TOOL_CALLS),
            extra_context=dict(payload.get("extra_context", {})),
        )


# ---------------------------------------------------------------------------
# ToolCallSummary + TaskResult — child run output envelope.
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class ToolCallSummary:
    """One entry of :attr:`TaskResult.tool_calls_made`.

    Mirrors the Rust ``ToolCallSummary``. Carries enough for the parent
    to attribute behaviour without re-pulling the raw arguments.
    """

    name: str
    args_summary: str
    duration_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "args_summary": self.args_summary,
            "duration_ms": self.duration_ms,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ToolCallSummary:
        return cls(
            name=payload["name"],
            args_summary=payload["args_summary"],
            duration_ms=payload["duration_ms"],
        )


@dataclass(slots=True)
class TaskResult:
    """Mirror of the Rust ``TaskResult``.

    One child run = one of these. The Rust crate marked the struct
    ``Clone + PartialEq``; we keep it mutable (no ``frozen=True``) so
    the supervisor's timeout branch can stamp ``elapsed_ms`` /
    ``finish_reason=Timeout`` onto an existing envelope returned by a
    partially-completed child without copying.
    """

    output_text: str
    tool_calls_made: list[ToolCallSummary]
    child_session_key: str
    child_agent_id: str
    elapsed_ms: int
    finish_reason: FinishReason
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "output_text": self.output_text,
            "tool_calls_made": [c.to_dict() for c in self.tool_calls_made],
            "child_session_key": self.child_session_key,
            "child_agent_id": self.child_agent_id,
            "elapsed_ms": self.elapsed_ms,
            "finish_reason": self.finish_reason.value,
        }
        if self.error is not None:
            out["error"] = self.error
        return out

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> TaskResult:
        return cls(
            output_text=payload["output_text"],
            tool_calls_made=[
                ToolCallSummary.from_dict(c) for c in payload.get("tool_calls_made", [])
            ],
            child_session_key=payload["child_session_key"],
            child_agent_id=payload["child_agent_id"],
            elapsed_ms=payload["elapsed_ms"],
            finish_reason=FinishReason(payload["finish_reason"]),
            error=payload.get("error"),
        )

    @classmethod
    def rejected(
        cls,
        reason: FinishReason,
        parent_session_key: str,
        error: str,
    ) -> TaskResult:
        """Constructor for the supervisor's pre-spawn rejection path.

        Depth cap or concurrency / allowlist refusal: the child loop
        never ran, so output / tool calls are empty and the session /
        agent ids are placeholders the parent can ignore.

        Raises:
            ValueError: if ``reason`` is not a pre-spawn rejection (the
                Rust crate uses ``debug_assert!``; we use a hard raise
                because Python has no debug/release split).
        """
        if not reason.is_pre_spawn_rejection():
            raise ValueError(
                "TaskResult.rejected() is for DEPTH_CAPPED/REJECTED only; "
                f"got {reason!r}"
            )
        return cls(
            output_text="",
            tool_calls_made=[],
            # Convention: `::child::-` marks a slot that was refused
            # rather than allocated. The supervisor uses this when
            # emitting the Rejected hook event so operators can tell a
            # never-spawned child from a spawned-then-failed one.
            child_session_key=f"{parent_session_key}::child::-",
            child_agent_id="",
            elapsed_ms=0,
            finish_reason=reason,
            error=error,
        )


# ---------------------------------------------------------------------------
# ParentContext — per-spawn snapshot of the parent's identity.
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class ParentContext:
    """Mirror of the Rust ``ParentContext`` struct.

    The supervisor reads :attr:`depth` for the recursion cap and
    :attr:`tenant_id` for the per-tenant quota; observability reads
    :attr:`trace_id` for evolution-signal linking.
    """

    tenant_id: str
    parent_agent_id: str
    parent_session_key: str
    #: 0 for top-level user-driven turns; ``+1`` per spawn frame.
    #: ``>= max_depth`` triggers the supervisor short-circuit.
    depth: int = 0
    trace_id: str = ""

    def child_context(self, child_card: str, child_seq: int) -> ParentContext:
        """Derive the child's :class:`ParentContext` for one nested spawn.

        Used by the supervisor *after* the depth check passes.
        ``child_seq`` increments per child within one parent frame so
        agent_id / session_key collisions cannot happen.

        Depth saturates at the Rust ``u8::MAX`` (255) cap so even a
        pathological caller passing a huge depth can't wrap.
        """
        return ParentContext(
            tenant_id=self.tenant_id,
            parent_agent_id=f"{self.parent_agent_id}::{child_card}::{child_seq}",
            parent_session_key=f"{self.parent_session_key}::child::{child_seq}",
            depth=min(self.depth + 1, 255),
            # Children inherit the parent's trace_id verbatim so the
            # evolution observer's join query finds them by
            # `parent_trace_id == self.trace_id`.
            trace_id=self.trace_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "parent_agent_id": self.parent_agent_id,
            "parent_session_key": self.parent_session_key,
            "depth": self.depth,
            "trace_id": self.trace_id,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ParentContext:
        return cls(
            tenant_id=payload["tenant_id"],
            parent_agent_id=payload["parent_agent_id"],
            parent_session_key=payload["parent_session_key"],
            depth=payload.get("depth", 0),
            trace_id=payload.get("trace_id", ""),
        )
