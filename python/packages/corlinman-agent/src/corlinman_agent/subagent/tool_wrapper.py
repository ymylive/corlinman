"""Parent-loop integration for the ``subagent.spawn`` tool.

Iter 8 of the D3 plan in ``docs/design/phase4-w4-d3-design.md``. Iter 7
landed the runner-side filtering; this module provides the bits needed
to actually expose ``subagent.spawn`` to the parent's LLM:

1. :func:`subagent_spawn_tool_schema` — OpenAI-shaped tool descriptor.
   Drop this into the parent's ``ChatStart.tools`` list and the model
   will emit ``ToolCallEvent("subagent.spawn", {"agent": "...",
   "goal": "..."})`` calls.
2. :func:`dispatch_subagent_spawn` — async helper that consumes a tool
   call's ``args_json``, resolves the requested agent card, drives
   :func:`corlinman_agent.subagent.run_child`, and returns the JSON
   string the gateway dispatcher feeds back as
   :attr:`corlinman_agent.reasoning_loop.ToolResult.content`. The
   parent's loop then appends a ``role="tool"`` message and continues —
   the tool-call envelope is the result-merge format the design fixes
   in § "Result merging — tool-call envelope wins".

The Rust supervisor (``corlinman-subagent`` crate) is the canonical
owner of the depth / concurrency / tenant caps. This module's
:func:`dispatch_subagent_spawn` therefore takes a *callable* —
``supervisor_acquire`` — that the production caller binds to either the
real Rust ``Supervisor::try_acquire`` (via the iter-5 PyO3 bridge) or
to an in-process Python stub for tests. Keeping the supervisor
abstract here means we can unit-test the dispatch contract without
spinning a Rust interpreter.

Failure mapping (the parent's LLM must observe every kind of failure
deterministically so the evolution loop can learn from it):

* unknown agent name → :attr:`FinishReason.REJECTED`,
  ``error="agent_not_found"``;
* malformed ``args_json`` (not JSON, missing ``agent`` / ``goal``,
  wrong types) → :attr:`FinishReason.REJECTED`, ``error`` carries the
  parse / validation message;
* supervisor refused the spawn (cap / depth) →
  :attr:`FinishReason.REJECTED` or :attr:`FinishReason.DEPTH_CAPPED`
  via :meth:`TaskResult.rejected`;
* uncaught exception in :func:`run_child` →
  :attr:`FinishReason.ERROR`. The runner already catches its own
  exceptions, so this branch is the belt-and-braces case for
  programmer error in the dispatch layer.

The whole module is pure Python — no PyO3, no gateway — so the unit
tests in ``test_subagent_tool_wrapper.py`` exercise the full
LLM↔runner round-trip without needing the Rust crate built.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from contextlib import nullcontext
from typing import TYPE_CHECKING, Any

import structlog

from corlinman_agent.subagent.api import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_TOOL_CALLS,
    DEFAULT_MAX_WALL_SECONDS,
    FinishReason,
    ParentContext,
    TaskResult,
    TaskSpec,
)
from corlinman_agent.subagent.runner import (
    SUBAGENT_SPAWN_TOOL,
    run_child,
)

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    from corlinman_persona.store import PersonaStore

    from corlinman_agent.agents.registry import AgentCardRegistry

logger = structlog.get_logger(__name__)


#: Sentinel error returned when ``args.agent`` doesn't resolve through
#: the registry. Pinned as a constant so the parent's prompt branches
#: on a stable string and the iter-9 hook event payload can carry it
#: verbatim.
AGENT_NOT_FOUND_ERROR: str = "agent_not_found"

#: Sentinel error returned when the JSON args fail validation. The
#: details (which field, what type) ride in the error message verbatim.
ARGS_INVALID_ERROR: str = "args_invalid"


# ---------------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------------


def subagent_spawn_tool_schema(
    *,
    default_max_wall_seconds: int = DEFAULT_MAX_WALL_SECONDS,
    default_max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
) -> dict[str, Any]:
    """Return the OpenAI-shaped tool descriptor for ``subagent.spawn``.

    The descriptor is what the parent's reasoning loop hands the
    provider so the LLM can emit a ``ToolCallEvent`` for
    ``subagent.spawn``. Field naming matches the design's
    :class:`TaskSpec` exactly so a one-to-one
    ``json.loads(args_json) → TaskSpec(**...)`` works in
    :func:`dispatch_subagent_spawn`.

    The ``default_*`` parameters are surfaced in the schema's
    ``description`` strings (not as JSON-Schema defaults — providers'
    treatment of those varies) so the LLM has a reasonable expectation
    of what'll happen when it omits the budget knobs. The runner /
    supervisor enforce the actual numbers; this is documentation for
    the model.
    """
    return {
        "type": "function",
        "function": {
            "name": SUBAGENT_SPAWN_TOOL,
            "description": (
                "Delegate a self-contained subtask to a child agent and "
                "block until it returns. The child runs in a fresh "
                "context (fresh persona, fresh session) with read-only "
                "access to the parent's memory federation. Use for "
                "research-and-summarise fan-out, multi-source queries, "
                "or fan-out evaluation where context isolation matters."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent": {
                        "type": "string",
                        "description": (
                            "Name of the registered agent card to "
                            "spawn (filename stem under the agents/ "
                            "directory)."
                        ),
                    },
                    "goal": {
                        "type": "string",
                        "description": (
                            "User-turn prompt the child will receive "
                            "as its only message. Should be self-"
                            "contained — the child cannot see the "
                            "parent's chat history."
                        ),
                    },
                    "tool_allowlist": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional subset of the parent's tool set "
                            "the child is allowed to call. Omit to "
                            "inherit; pass [] to forbid all tools "
                            "(pure LLM call). Asking for a tool the "
                            "parent doesn't hold rejects the spawn."
                        ),
                    },
                    "max_wall_seconds": {
                        "type": "integer",
                        "description": (
                            f"Hard wall-clock budget for the child. "
                            f"Default {default_max_wall_seconds}s; "
                            f"capped from above by the supervisor's "
                            f"max_wall_seconds_ceiling policy."
                        ),
                        "minimum": 1,
                    },
                    "max_tool_calls": {
                        "type": "integer",
                        "description": (
                            f"Cap on the child's reasoning rounds. "
                            f"Default {default_max_tool_calls}."
                        ),
                        "minimum": 1,
                    },
                    "extra_context": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                        "description": (
                            "Optional {ctx.<key>: <text>} blobs "
                            "spliced into the child's system prompt."
                        ),
                    },
                },
                "required": ["agent", "goal"],
                "additionalProperties": False,
            },
        },
    }


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

#: Type of the supervisor-acquire callable. Returns either a context
#: manager (the slot drop-guard) on success, or a string describing
#: the rejection reason. Strings ``"depth_capped"`` / anything else are
#: mapped to :attr:`FinishReason.DEPTH_CAPPED` / :attr:`FinishReason.REJECTED`
#: in :func:`dispatch_subagent_spawn`.
#:
#: We use a callable + sentinel rather than raising because the
#: production binding (PyO3 → ``Supervisor::try_acquire``) returns a
#: ``Result``, not a Python exception, and we want the dispatch layer
#: to be agnostic to which side of the FFI it sits on.
SupervisorAcquire = Callable[[ParentContext], Any]


async def dispatch_subagent_spawn(
    *,
    args_json: bytes | str,
    parent_ctx: ParentContext,
    agent_registry: AgentCardRegistry,
    provider: Any,
    parent_tools: Sequence[dict[str, Any]] | None = None,
    persona_store: PersonaStore | None = None,
    supervisor_acquire: SupervisorAcquire | None = None,
    child_seq: int = 0,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_wall_seconds_ceiling: int | None = None,
) -> str:
    """Translate one ``subagent.spawn`` tool call into a JSON
    :class:`TaskResult` envelope.

    Parameters
    ----------
    args_json
        Raw ``ToolCallEvent.args_json`` bytes (or already-decoded
        string). Parsed as JSON; failure → :attr:`FinishReason.REJECTED`
        with ``error=args_invalid``.
    parent_ctx
        Parent's :class:`ParentContext`. The runner derives the
        child's own context from this; the supervisor uses the depth
        to gate recursion.
    agent_registry
        Source of :class:`AgentCard` lookups. The dispatcher resolves
        ``args.agent`` here; an unknown name short-circuits with
        :attr:`FinishReason.REJECTED` and ``error=agent_not_found``.
    provider
        Provider the *child* will use. The production caller (gateway
        / agent_servicer, iter 8 wiring) typically passes the same
        provider the parent is using; tests pass a fake.
    parent_tools
        OpenAI-shaped tool list the parent is configured with. Forwarded
        to :func:`run_child` as the allowlist source-of-truth (iter 7).
        ``None`` is treated as "parent has no tools", which means the
        child is restricted to a pure LLM call regardless of the
        request's ``tool_allowlist``.
    persona_store
        Forwarded to :func:`run_child` for fresh-row seeding under the
        child's mangled ``agent_id``. ``None`` skips seeding.
    supervisor_acquire
        Callable that reserves a slot in the Rust supervisor. ``None``
        runs without slot enforcement (test mode). On rejection the
        callable returns either ``"depth_capped"`` or any other string
        identifying the cap that fired; the dispatcher maps these to
        the appropriate :class:`FinishReason`.
    child_seq
        Sibling-disambiguation sequence number. The production caller
        keeps a per-parent counter (``parent_session_key`` →
        :class:`AtomicUsize` on the Rust side); tests pass 0.
    max_depth
        Threaded into :func:`run_child` so its self-prune at
        ``child_depth >= max_depth - 1`` matches the live policy.
    max_wall_seconds_ceiling
        Optional ceiling on the request's ``max_wall_seconds``. The
        design's ``[subagent].max_wall_seconds_ceiling`` (default 300)
        is enforced from above — if the LLM asks for more, we clamp.

    Returns
    -------
    str
        JSON-serialised :class:`TaskResult`. The caller feeds this
        verbatim into :class:`ToolResult.content`. Always returns;
        never raises (the parent's loop must keep going).
    """
    # ── 1. Parse + validate the LLM's args. ──────────────────────────
    try:
        spec, agent_name = _parse_args(args_json)
    except _ArgsInvalidError as exc:
        logger.warning(
            "subagent.dispatch.args_invalid",
            session=parent_ctx.parent_session_key,
            error=exc.message,
        )
        return _result_json(
            _rejected_result(
                parent_ctx=parent_ctx,
                reason=FinishReason.REJECTED,
                error=f"{ARGS_INVALID_ERROR}: {exc.message}",
            )
        )

    # ── 2. Resolve the agent card. ───────────────────────────────────
    card = agent_registry.get(agent_name)
    if card is None:
        logger.info(
            "subagent.dispatch.agent_not_found",
            session=parent_ctx.parent_session_key,
            requested=agent_name,
        )
        return _result_json(
            _rejected_result(
                parent_ctx=parent_ctx,
                reason=FinishReason.REJECTED,
                error=f"{AGENT_NOT_FOUND_ERROR}: {agent_name!r}",
            )
        )

    # ── 3. Clamp request-side budgets to policy ceiling. ─────────────
    if (
        max_wall_seconds_ceiling is not None
        and spec.max_wall_seconds > max_wall_seconds_ceiling
    ):
        # Frozen dataclass — rebuild rather than mutate.
        from dataclasses import replace as _dc_replace

        spec = _dc_replace(spec, max_wall_seconds=max_wall_seconds_ceiling)

    # ── 4. Acquire a supervisor slot (real or stubbed). ──────────────
    slot_cm: Any
    if supervisor_acquire is None:
        slot_cm = nullcontext()
    else:
        outcome = supervisor_acquire(parent_ctx)
        if isinstance(outcome, str):
            # Rejection — map the string to a finish reason. The Rust
            # bridge serialises ``AcquireReject::DepthCapped`` as
            # ``"depth_capped"``; anything else is a per-parent /
            # tenant cap rejection.
            reason = (
                FinishReason.DEPTH_CAPPED
                if outcome == "depth_capped"
                else FinishReason.REJECTED
            )
            return _result_json(
                _rejected_result(
                    parent_ctx=parent_ctx,
                    reason=reason,
                    error=f"supervisor: {outcome}",
                )
            )
        slot_cm = outcome  # context-manager-shaped slot drop-guard

    # ── 5. Drive the child runner under the slot. ────────────────────
    try:
        with slot_cm:
            result = await run_child(
                parent_ctx,
                card,
                spec,
                provider=provider,
                child_seq=child_seq,
                persona_store=persona_store,
                parent_tools=parent_tools,
                max_depth=max_depth,
            )
    except Exception as exc:
        logger.exception(
            "subagent.dispatch.runner_uncaught",
            session=parent_ctx.parent_session_key,
        )
        result = TaskResult(
            output_text="",
            tool_calls_made=[],
            child_session_key=f"{parent_ctx.parent_session_key}::child::{child_seq}",
            child_agent_id=f"{parent_ctx.parent_agent_id}::{agent_name}::{child_seq}",
            elapsed_ms=0,
            finish_reason=FinishReason.ERROR,
            error=str(exc),
        )

    return _result_json(result)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


class _ArgsInvalidError(Exception):
    """Raised by :func:`_parse_args` when the LLM's arguments are
    unparseable or fail shape validation. Caught in
    :func:`dispatch_subagent_spawn` and folded into a rejected result.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _parse_args(args_json: bytes | str) -> tuple[TaskSpec, str]:
    """Parse + validate the raw ``args_json`` from the tool call.

    Returns a ``(spec, agent_name)`` pair. The agent name lives outside
    :class:`TaskSpec` because :class:`TaskSpec` is the *child's* request
    contract (no awareness of agent-card identity); the *spawn-tool's*
    contract is the union of "what to spawn" + "how to spawn it".
    """
    if isinstance(args_json, (bytes, bytearray)):
        try:
            decoded = args_json.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _ArgsInvalidError(f"args_json not utf-8: {exc}") from exc
    else:
        decoded = args_json

    try:
        raw = json.loads(decoded) if decoded else {}
    except json.JSONDecodeError as exc:
        raise _ArgsInvalidError(f"args_json not JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise _ArgsInvalidError(
            f"args_json must be a JSON object, got {type(raw).__name__}"
        )

    agent = raw.get("agent")
    if not isinstance(agent, str) or not agent:
        raise _ArgsInvalidError("missing or empty 'agent' field")
    goal = raw.get("goal")
    if not isinstance(goal, str) or not goal:
        raise _ArgsInvalidError("missing or empty 'goal' field")

    # Optional fields — validate type, fall through to defaults.
    tool_allowlist = raw.get("tool_allowlist")
    if tool_allowlist is not None and (
        not isinstance(tool_allowlist, list)
        or not all(isinstance(t, str) for t in tool_allowlist)
    ):
        raise _ArgsInvalidError("'tool_allowlist' must be a list of strings")

    max_wall_seconds = raw.get("max_wall_seconds", DEFAULT_MAX_WALL_SECONDS)
    if not isinstance(max_wall_seconds, int) or max_wall_seconds <= 0:
        raise _ArgsInvalidError("'max_wall_seconds' must be a positive integer")

    max_tool_calls = raw.get("max_tool_calls", DEFAULT_MAX_TOOL_CALLS)
    if not isinstance(max_tool_calls, int) or max_tool_calls <= 0:
        raise _ArgsInvalidError("'max_tool_calls' must be a positive integer")

    extra_context = raw.get("extra_context", {})
    if not isinstance(extra_context, dict) or not all(
        isinstance(k, str) and isinstance(v, str)
        for k, v in extra_context.items()
    ):
        raise _ArgsInvalidError("'extra_context' must be a dict[str, str]")

    spec = TaskSpec(
        goal=goal,
        tool_allowlist=list(tool_allowlist) if tool_allowlist is not None else None,
        max_wall_seconds=max_wall_seconds,
        max_tool_calls=max_tool_calls,
        extra_context=dict(extra_context),
    )
    return spec, agent


def _rejected_result(
    *,
    parent_ctx: ParentContext,
    reason: FinishReason,
    error: str,
) -> TaskResult:
    """Construct the synthetic envelope for a pre-spawn rejection.

    Mirrors :meth:`TaskResult.rejected` for the supervisor's own
    rejection path but accepts a free-form error string so the
    args-invalid / agent-not-found cases can carry their specific
    messages. The ``::child::-`` session-key convention marks the
    refused slot for operator UIs.
    """
    return TaskResult(
        output_text="",
        tool_calls_made=[],
        child_session_key=f"{parent_ctx.parent_session_key}::child::-",
        child_agent_id="",
        elapsed_ms=0,
        finish_reason=reason,
        error=error,
    )


def _result_json(result: TaskResult) -> str:
    """JSON-serialise a :class:`TaskResult` for the wire envelope.

    :class:`FinishReason` inherits from ``str`` so its ``.value`` lands
    naturally; :class:`ToolCallSummary` is a dataclass we hand-flatten
    to keep the JSON shape Rust-compatible (Rust expects an object,
    not a tuple). ``error`` is included only when populated to keep
    the parent's prompt token-spend low on the happy path — matches
    the Rust ``#[serde(skip_serializing_if = "Option::is_none")]``
    behaviour byte-for-byte.
    """
    payload: dict[str, Any] = {
        "output_text": result.output_text,
        "tool_calls_made": [
            {
                "name": call.name,
                "args_summary": call.args_summary,
                "duration_ms": call.duration_ms,
            }
            for call in result.tool_calls_made
        ],
        "child_session_key": result.child_session_key,
        "child_agent_id": result.child_agent_id,
        "elapsed_ms": result.elapsed_ms,
        "finish_reason": result.finish_reason.value,
    }
    if result.error is not None:
        payload["error"] = result.error
    return json.dumps(payload)


__all__ = [
    "AGENT_NOT_FOUND_ERROR",
    "ARGS_INVALID_ERROR",
    "SupervisorAcquire",
    "dispatch_subagent_spawn",
    "subagent_spawn_tool_schema",
]
