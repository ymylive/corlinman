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

import asyncio
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
    SUBAGENT_SPAWN_MANY_TOOL,
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


#: Hard cap on the number of siblings one ``subagent.spawn_many`` call
#: can dispatch. Matches the supervisor's per-parent concurrency ceiling
#: so the cap surfaces as an args-invalid rejection (a clear, actionable
#: signal to the LLM) instead of N-3 silent ``parent_concurrency_exceeded``
#: rejections inside the gather. Raise this only if the supervisor's
#: ``SupervisorPolicy::max_concurrent_per_parent`` is raised in lock-step.
SUBAGENT_SPAWN_MANY_MAX_TASKS: int = 3


def subagent_spawn_many_tool_schema(
    *,
    default_max_wall_seconds: int = DEFAULT_MAX_WALL_SECONDS,
    default_max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
    max_tasks: int = SUBAGENT_SPAWN_MANY_MAX_TASKS,
) -> dict[str, Any]:
    """Return the OpenAI-shaped tool descriptor for ``subagent.spawn_many``.

    The orchestrator persona is the primary consumer. The descriptor
    accepts a list of per-child specs (each shaped like a
    :func:`subagent_spawn_tool_schema` body) and the dispatcher fans
    them out concurrently under one parent context. The siblings run in
    parallel, bounded by the supervisor's per-parent concurrency cap
    (which is why ``max_tasks`` defaults to that cap).

    The schema deliberately does NOT carry a ``blackboard_key`` field —
    coordination between siblings is a *content* concern handled by
    putting the same key into each child's ``extra_context``. Keeping
    the fan-out tool ignorant of the blackboard means the same fan-out
    primitive serves shared-state and no-shared-state patterns.
    """
    per_task = {
        "type": "object",
        "properties": {
            "agent": {
                "type": "string",
                "description": (
                    "Name of the registered agent card to spawn for "
                    "this sibling."
                ),
            },
            "goal": {
                "type": "string",
                "description": (
                    "User-turn prompt this sibling will receive as its "
                    "only message."
                ),
            },
            "tool_allowlist": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional subset of the parent's tool set this "
                    "sibling is allowed to call. Inherit if omitted."
                ),
            },
            "max_wall_seconds": {
                "type": "integer",
                "description": (
                    f"Hard wall-clock budget for this sibling. Default "
                    f"{default_max_wall_seconds}s."
                ),
                "minimum": 1,
            },
            "max_tool_calls": {
                "type": "integer",
                "description": (
                    f"Cap on this sibling's reasoning rounds. Default "
                    f"{default_max_tool_calls}."
                ),
                "minimum": 1,
            },
            "extra_context": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": (
                    "Optional {ctx.<key>: <text>} blobs spliced into "
                    "this sibling's system prompt. Use this to pass a "
                    "shared 'blackboard_key' to coordinating siblings."
                ),
            },
        },
        "required": ["agent", "goal"],
        "additionalProperties": False,
    }
    return {
        "type": "function",
        "function": {
            "name": SUBAGENT_SPAWN_MANY_TOOL,
            "description": (
                "Dispatch up to "
                f"{max_tasks} sibling child agents concurrently and "
                "block until all return. Use for true fan-out: "
                "research + edit, query multiple sources, compare "
                "approaches. Each sibling runs in its own fresh "
                "context; pass a shared key via extra_context if they "
                "need to coordinate through the blackboard tools. "
                "Returns {\"tasks\": [TaskResult, ...]} in input order."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "items": per_task,
                        "minItems": 1,
                        "maxItems": max_tasks,
                        "description": (
                            f"1..{max_tasks} per-sibling task specs. "
                            "Each sibling is dispatched concurrently."
                        ),
                    },
                },
                "required": ["tasks"],
                "additionalProperties": False,
            },
        },
    }


async def dispatch_subagent_spawn_many(
    *,
    args_json: bytes | str,
    parent_ctx: ParentContext,
    agent_registry: AgentCardRegistry,
    provider: Any,
    parent_tools: Sequence[dict[str, Any]] | None = None,
    persona_store: PersonaStore | None = None,
    supervisor_acquire: SupervisorAcquire | None = None,
    base_child_seq: int = 0,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_wall_seconds_ceiling: int | None = None,
    max_tasks: int = SUBAGENT_SPAWN_MANY_MAX_TASKS,
) -> str:
    """Translate one ``subagent.spawn_many`` tool call into a JSON
    envelope of :class:`TaskResult` siblings, run concurrently.

    The dispatcher splits the LLM's ``tasks`` list, builds an isolated
    ``args_json`` for each, and awaits :func:`dispatch_subagent_spawn`
    on all of them in parallel via ``asyncio.gather``. The supervisor's
    per-parent concurrency cap (default 3) is the hard limit on live
    siblings; this dispatcher also rejects ``len(tasks) > max_tasks``
    up-front so the LLM sees a clean args-invalid envelope instead of
    N-3 silent slot rejections.

    Children are disambiguated by ``child_seq = base_child_seq + i`` so
    their ``ParentContext.child_context`` derivations don't collide.
    Failures in one sibling are isolated: ``asyncio.gather`` is called
    with ``return_exceptions=True`` and any exception is folded into a
    synthetic ERROR envelope for that index, keeping the wire shape
    ``{"tasks": [TaskResult, ...]}`` intact.

    Returns
    -------
    str
        JSON object ``{"tasks": [TaskResult, ...]}`` in input order.
        ``error`` lives on individual siblings; the outer envelope is
        always shaped the same.
    """
    # ── 1. Parse + validate the LLM's args. ──────────────────────────
    try:
        task_specs = _parse_spawn_many_args(args_json, max_tasks=max_tasks)
    except _ArgsInvalidError as exc:
        logger.warning(
            "subagent.dispatch_many.args_invalid",
            session=parent_ctx.parent_session_key,
            error=exc.message,
        )
        # Fan-out's args-invalid surfaces as a top-level error
        # envelope (the LLM sees no per-sibling results) so it can't
        # confuse a parse failure with a sibling's runtime failure.
        return json.dumps(
            {
                "tasks": [],
                "error": f"{ARGS_INVALID_ERROR}: {exc.message}",
            }
        )

    # ── 2. Fan out. asyncio.gather over per-sibling dispatch_spawn. ──
    coros = [
        dispatch_subagent_spawn(
            args_json=task_args,
            parent_ctx=parent_ctx,
            agent_registry=agent_registry,
            provider=provider,
            parent_tools=parent_tools,
            persona_store=persona_store,
            supervisor_acquire=supervisor_acquire,
            child_seq=base_child_seq + i,
            max_depth=max_depth,
            max_wall_seconds_ceiling=max_wall_seconds_ceiling,
        )
        for i, task_args in enumerate(task_specs)
    ]
    raw = await asyncio.gather(*coros, return_exceptions=True)

    # ── 3. Normalise each result. dispatch_subagent_spawn already
    #      returns a JSON string; if a coro raised (programmer error,
    #      not a sibling-level failure), synthesise the ERROR envelope
    #      so the wire shape is always ``{"tasks": [TaskResult, ...]}``.
    siblings: list[dict[str, Any]] = []
    for i, item in enumerate(raw):
        if isinstance(item, BaseException):
            logger.exception(
                "subagent.dispatch_many.gather_uncaught",
                session=parent_ctx.parent_session_key,
                child_index=i,
                exc_info=item,
            )
            siblings.append(
                {
                    "output_text": "",
                    "tool_calls_made": [],
                    "child_session_key": (
                        f"{parent_ctx.parent_session_key}"
                        f"::child::{base_child_seq + i}"
                    ),
                    "child_agent_id": "",
                    "elapsed_ms": 0,
                    "finish_reason": FinishReason.ERROR.value,
                    "error": str(item),
                }
            )
        else:
            siblings.append(json.loads(item))

    return json.dumps({"tasks": siblings})


def _parse_spawn_many_args(
    args_json: bytes | str,
    *,
    max_tasks: int,
) -> list[str]:
    """Validate ``{"tasks": [...]}`` and return per-sibling args_json.

    Each returned string is a ready-to-feed argument to
    :func:`dispatch_subagent_spawn` — pre-shaped so the per-sibling
    dispatch reuses the same validation in
    :func:`_parse_args` rather than duplicating the field-by-field
    type checks here. The fan-out wrapper does only the *envelope*
    shape (list size, list-of-objects).
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
    tasks = raw.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise _ArgsInvalidError("'tasks' must be a non-empty list of objects")
    if len(tasks) > max_tasks:
        raise _ArgsInvalidError(
            f"'tasks' length {len(tasks)} exceeds the per-fanout cap of {max_tasks}"
        )
    out: list[str] = []
    for i, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise _ArgsInvalidError(
                f"tasks[{i}] must be a JSON object, got {type(task).__name__}"
            )
        # Re-serialise each per-sibling spec so the per-sibling
        # dispatcher's own field validation runs identically to the
        # single-spawn path. Cheap and keeps validation in one place.
        out.append(json.dumps(task))
    return out


__all__ = [
    "AGENT_NOT_FOUND_ERROR",
    "ARGS_INVALID_ERROR",
    "SUBAGENT_SPAWN_MANY_MAX_TASKS",
    "SupervisorAcquire",
    "dispatch_subagent_spawn",
    "dispatch_subagent_spawn_many",
    "subagent_spawn_many_tool_schema",
    "subagent_spawn_tool_schema",
]
