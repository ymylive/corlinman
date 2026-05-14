"""Wire-format dataclasses for the subagent delegation runtime.

These types mirror the Rust definitions in
``rust/crates/corlinman-subagent/src/types.rs`` field-for-field. The PyO3
bridge (iter 5) marshals between the two sides via JSON serialisation —
keeping field names and serde rename rules identical means we never
need bespoke ``#[pyo3]`` extract/IntoPyObject impls.

The frozen dataclasses also let us freely share :class:`TaskSpec` /
:class:`ParentContext` instances across event loops; the Rust supervisor
can hand a context object to N concurrent siblings without copy.

Open question: when do we promote :class:`FinishReason` from a string-
valued ``Enum`` to a proper Literal? The current ``Enum`` lets the LLM's
JSON tool result deserialise straight in (Pydantic / json.loads handle
the value matching), and the cost is one indirection at access time. If
M-series performance wants the literal-typed branch we'll convert when
the parent-loop integration (iter 8) starts measuring tool-result parse
overhead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

# Defaults pulled from the design doc's `[subagent]` config block. Mirror
# the `defaults::*` consts in Rust `types.rs` so a Python-side override
# can never silently drift from the Rust supervisor's accounting.

#: Hard ceiling on a child's wall-clock budget. ``task.max_wall_seconds``
#: may *lower* this but never raise it; the Rust supervisor enforces the
#: upper bound at acquire time (iter 5+).
DEFAULT_MAX_WALL_SECONDS: int = 60

#: Cap on the child's ``_MAX_ROUNDS`` (parent loop's own ceiling is 8 —
#: ``reasoning_loop.py:143``). Children get a slightly higher allowance
#: because they often need to chain ``search → fetch → summarise``.
DEFAULT_MAX_TOOL_CALLS: int = 12

#: Maximum nesting depth (parent → child → grandchild). Used by the
#: supervisor's ``depth_capped`` short-circuit; runner only reads it for
#: the ``subagent.spawn`` self-prune at ``depth == max_depth - 1`` (iter 7).
DEFAULT_MAX_DEPTH: int = 2


class FinishReason(StrEnum):
    """Why the child stopped. String-valued so JSON serialisation drops
    straight onto the wire under the same lowercase snake_case names the
    Rust ``FinishReason`` enum uses (`#[serde(rename_all = "snake_case")]`).

    Inheriting from ``StrEnum`` means ``json.dumps`` emits the value verbatim
    and ``FinishReason("stop")`` parses back without a custom decoder —
    important because the parent's LLM produces and consumes these strings.
    """

    #: Provider returned a final response cleanly.
    STOP = "stop"
    #: Hit ``max_tool_calls`` without producing a final.
    LENGTH = "length"
    #: Wall-clock budget exhausted; partial output may be present.
    TIMEOUT = "timeout"
    #: Runner raised an exception; see :attr:`TaskResult.error`.
    ERROR = "error"
    #: Parent depth >= ``max_depth``; child loop was never invoked.
    DEPTH_CAPPED = "depth_capped"
    #: Concurrency / tenant quota / allowlist escalation rejected the
    #: spawn before any work happened.
    REJECTED = "rejected"

    def is_pre_spawn_rejection(self) -> bool:
        """``True`` for variants where the child loop never ran."""
        return self in (FinishReason.DEPTH_CAPPED, FinishReason.REJECTED)


@dataclass(slots=True, frozen=True)
class TaskSpec:
    """Parent-loop request to spawn one child.

    Mirrors ``rust/crates/corlinman-subagent/src/types.rs::TaskSpec``;
    field names match exactly so a JSON round-trip is lossless.
    """

    #: User-turn prompt the child sees as its only message.
    goal: str
    #: ``None`` → inherit parent's tool set.
    #: ``[]`` → pure LLM call, no tools.
    #: non-empty list → must be ⊆ parent's tools (iter 7 enforces).
    tool_allowlist: list[str] | None = None
    #: Hard timeout. Capped from above by
    #: ``[subagent].max_wall_seconds_ceiling`` in the supervisor.
    max_wall_seconds: int = DEFAULT_MAX_WALL_SECONDS
    #: Per-child round cap.
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS
    #: ``{ctx.<key>}`` blobs spliced into the child's system prompt.
    #: ``dict`` rather than ``frozenset`` so we keep insertion order for
    #: deterministic prompt rendering / fingerprint reproducibility.
    extra_context: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class ToolCallSummary:
    """One entry of :attr:`TaskResult.tool_calls_made`.

    Carries enough for the parent to attribute behaviour without
    re-pulling the (often huge) raw arguments. Mirrors
    ``rust/crates/corlinman-subagent/src/types.rs::ToolCallSummary``.
    """

    name: str
    #: Short freeform synopsis of args (e.g. ``"query=transformers"``).
    #: Not the raw JSON — that lives in the child session for replay.
    args_summary: str
    duration_ms: int


@dataclass(slots=True, frozen=True)
class TaskResult:
    """Result envelope the parent loop receives as ``ToolResult.content``.

    JSON-serialised per design § "Result merging — tool-call envelope
    wins". The parent's LLM is trained to consume tool results, so this
    becomes one ``role="tool"`` message in the parent's chat history.
    Mirrors ``rust/crates/corlinman-subagent/src/types.rs::TaskResult``.
    """

    #: Concatenated assistant token stream — *always* a string. Parent's
    #: prompt does any schema-validation ("ask child for JSON; you parse").
    output_text: str
    #: Attribution trail. ``list`` (not ``Optional``) so an empty result
    #: still serialises as ``[]`` — parent's prompt can rely on the field.
    tool_calls_made: list[ToolCallSummary]
    #: Forensic replay handle. Format: ``<parent_session>::child::<seq>``.
    child_session_key: str
    #: Persona row identity. Format: ``<parent_agent>::<card>::<seq>``.
    child_agent_id: str
    elapsed_ms: int
    finish_reason: FinishReason
    #: Populated iff ``finish_reason == ERROR``. ``None`` is the
    #: happy-path default — keeps the parent's prompt token-spend low.
    error: str | None = None

    @classmethod
    def rejected(
        cls,
        reason: FinishReason,
        parent_session_key: str,
        error: str,
    ) -> TaskResult:
        """Helper for the supervisor's pre-spawn rejection path.

        The child loop never ran, so output / tool calls are empty and
        the session / agent ids are placeholders the parent can ignore.
        Mirrors ``Rust types.rs::TaskResult::rejected``. The
        ``::child::-`` convention marks a slot that was refused (rather
        than allocated) so operator UIs can tell never-spawned from
        spawned-then-failed.
        """
        if not reason.is_pre_spawn_rejection():
            raise ValueError(
                f"TaskResult.rejected() is for DEPTH_CAPPED/REJECTED only; "
                f"got {reason!r}"
            )
        return cls(
            output_text="",
            tool_calls_made=[],
            child_session_key=f"{parent_session_key}::child::-",
            child_agent_id="",
            elapsed_ms=0,
            finish_reason=reason,
            error=error,
        )


@dataclass(slots=True, frozen=True)
class ParentContext:
    """Per-spawn snapshot of the parent's identity.

    The Rust supervisor reads :attr:`depth` for the recursion cap and
    :attr:`tenant_id` for the per-tenant quota; iter-9 observability
    reads :attr:`trace_id` for evolution-signal linking. Mirrors
    ``rust/crates/corlinman-subagent/src/types.rs::ParentContext``.

    Memory-host handles are deliberately *not* in this struct yet — the
    read-only host wrapper lives in ``corlinman-memory-host`` (iter 2)
    and only enters the Python side once the full tool-allowlist /
    memory-host runtime wiring lands in iter 7+.
    """

    tenant_id: str
    parent_agent_id: str
    parent_session_key: str
    #: 0 for top-level user-driven turns. ``+1`` per spawn frame.
    depth: int = 0
    #: Stable id used to fold child evolution signals into the same
    #: trace tree as the parent (iter 9). ``str`` rather than ``UUID``
    #: because gateway-side trace ids are already string-typed.
    trace_id: str = ""

    def child_context(self, child_card: str, child_seq: int) -> ParentContext:
        """Derive the child's :class:`ParentContext` for one nested spawn.

        ``child_seq`` increments per child within one parent frame so
        agent_id / session_key collisions cannot happen — siblings
        share the parent but disambiguate by sequence number. Saturates
        depth at 255 (Rust mirrors with ``u8::saturating_add``).

        The child's ``parent_agent_id`` becomes the *spawned child's*
        agent_id from the persona-row perspective: it's the row written
        under :attr:`tenant_id` and read back when siblings query
        memory.
        """
        return ParentContext(
            tenant_id=self.tenant_id,
            parent_agent_id=f"{self.parent_agent_id}::{child_card}::{child_seq}",
            parent_session_key=f"{self.parent_session_key}::child::{child_seq}",
            depth=min(self.depth + 1, 255),
            # Children inherit the parent's trace_id verbatim so the
            # evolution observer's join query (iter 9) finds them by
            # ``parent_trace_id == self.trace_id``.
            trace_id=self.trace_id,
        )


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
