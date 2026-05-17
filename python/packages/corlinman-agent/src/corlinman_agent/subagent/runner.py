"""Happy-path child driver — :func:`run_child`.

Iter 4 landed the **happy path only**; iter 6 layered the cooperative
``max_wall_seconds`` enforcement on top; iter 7 (this revision) adds the
**tool-allowlist filter** and **privilege-escalation reject** documented
in design § "Tool exposure".

What :func:`run_child` now does end-to-end:

1. Resolve the child's effective tool list via :func:`_filter_tools_for_child`
   — intersection of ``task.tool_allowlist`` with the parent's
   ``tools_allowed``; ``None`` allowlist means "inherit parent's set".
2. Reject escalation outright: a request for any tool the parent doesn't
   already hold returns a synthetic
   :class:`TaskResult` with ``finish_reason=REJECTED`` and
   ``error="tool_allowlist_escalation"`` *before* the loop is driven.
3. Prune ``subagent.spawn`` from the child's allowlist when the *child's*
   depth would equal ``max_depth - 1`` — at that depth a grandchild
   spawn is the next thing the supervisor would refuse with
   ``DepthCapped`` anyway, so we save the LLM the round-trip.
4. Project the resulting *names* back onto the parent's *tool schemas*
   (the OpenAI `tools=` array) so the child's :class:`ChatStart`
   carries usable tool definitions, not just names.

What is *still* deliberately NOT here:

* PyO3 entry point — the supervisor calls this function over the GIL
  via the iter-5 bridge; iter 8 wires the production caller.
* Hook-bus observability — `SubagentSpawned/Completed/...` lands in
  iter 9.

The split between this runner and the supervisor remains the same
split documented in the design § "Implementation surface — Rust supervisor
+ Python runner": the **isolation contract** lives where the LLM cannot
reach it (Rust); the **loop driver** has to call into Python because that's
where :class:`ReasoningLoop` and the providers live.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import structlog

from corlinman_agent.agents.card import AgentCard
from corlinman_agent.reasoning_loop import (
    ChatStart,
    DoneEvent,
    ErrorEvent,
    ReasoningLoop,
    TokenEvent,
    ToolCallEvent,
)
from corlinman_agent.subagent.api import (
    DEFAULT_MAX_DEPTH,
    FinishReason,
    ParentContext,
    TaskResult,
    TaskSpec,
    ToolCallSummary,
)

#: Reserved tool name the parent's reasoning loop emits when it wants
#: to delegate. Pruned from the child's allowlist at the deepest legal
#: depth (``child_ctx.depth >= max_depth - 1``) so a grandchild can't
#: spawn a great-grandchild that the supervisor would reject with
#: :attr:`FinishReason.DEPTH_CAPPED` anyway. Lifting the literal into a
#: module constant keeps the iter-8 tool-wrapper registration in one
#: place — registry code imports the same name.
SUBAGENT_SPAWN_TOOL: str = "subagent.spawn"

#: Fan-out sibling of ``subagent.spawn`` — the orchestrator agent (v0.7)
#: emits this to dispatch N children concurrently under one parent. The
#: supervisor's per-parent concurrency cap (default 3) still bounds the
#: live siblings; the dispatcher splits the task list and awaits all via
#: ``asyncio.gather``. Pruned from the child's allowlist by the same
#: depth-1 rule that prunes ``subagent.spawn``.
SUBAGENT_SPAWN_MANY_TOOL: str = "subagent.spawn_many"

#: Sentinel error string surfaced on a privilege-escalation rejection.
#: Pinned in :attr:`TaskResult.error` so the LLM (and forensic queries)
#: can branch on the exact reason the child was refused.
TOOL_ALLOWLIST_ESCALATION_ERROR: str = "tool_allowlist_escalation"

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    # Avoids forcing a runtime import of corlinman-persona for callers
    # who pass `persona_store=None`. The pyproject lists corlinman-persona
    # as a dep so it's *available* at runtime — but lazy import keeps
    # the cost out of the import-time path of corlinman_agent.subagent.
    from corlinman_persona.store import PersonaStore

logger = structlog.get_logger(__name__)


async def run_child(
    parent_ctx: ParentContext,
    agent_card: AgentCard,
    task: TaskSpec,
    *,
    provider: Any,
    child_seq: int = 0,
    persona_store: PersonaStore | None = None,
    tool_result_timeout: float = 0.05,
    parent_tools: Sequence[dict[str, Any]] | None = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> TaskResult:
    """Drive one child reasoning loop and return its :class:`TaskResult`.

    Parameters
    ----------
    parent_ctx
        Snapshot of the parent's identity. The runner *derives* the
        child context internally via :meth:`ParentContext.child_context`
        — the supervisor passes the **parent's** context, not the
        child's, so the depth-/agent-id-mangling logic stays in one
        place. The supervisor (iter 5+) is responsible for the
        depth-cap check before calling this.
    agent_card
        The child's agent card. ``agent_card.system_prompt`` becomes
        the child's system message and ``agent_card.name`` is mangled
        into the child's :attr:`ParentContext.parent_agent_id` (the
        spawned child's own ``agent_id`` from a persona-row perspective).
    task
        Wire-format request: ``goal`` is the child's only user-turn
        message, ``tool_allowlist`` is recorded but not yet filtered
        (iter 7), ``max_wall_seconds`` / ``max_tool_calls`` are
        recorded but not yet enforced (iter 6+).
    provider
        Anything matching the :class:`CorlinmanProvider` Protocol — same
        contract :class:`ReasoningLoop` itself takes. Using duck-typing
        rather than the imported Protocol means tests can pass the same
        ``_FakeProvider`` they use for the loop's own tests without
        importing the heavyweight provider module.
    child_seq
        Sequence number disambiguating siblings under the same parent.
        Default 0 is fine for a single child; concurrent fan-out
        callers (iter 8+) pass increasing values.
    persona_store
        If given, a fresh persona row is seeded for the child's mangled
        ``agent_id`` under the parent's ``tenant_id``. ``None`` skips
        seeding entirely — useful for unit tests that don't care about
        persona side effects. The seed is best-effort: a write failure
        logs a warning and the child still runs (it doesn't read
        persona state directly; the resolver does, on the next prompt
        render).
    tool_result_timeout
        Forwarded to :class:`ReasoningLoop`. Default 0.05s is the same
        as the loop's own default — for iter 4 (no tools wired) the
        loop short-circuits on the first round, so the value doesn't
        actually gate happy-path tests.
    parent_tools
        OpenAI-shaped tool schemas the *parent's* reasoning loop is
        configured with (each entry has at least ``{"function":
        {"name": "..."}}`` or a top-level ``"name"``). Iter 7 uses this
        list as both the *allowlist source-of-truth* (its names form the
        parent's ``tools_allowed``) and the *schema source* projected
        onto the child's :class:`ChatStart`. ``None`` is treated as the
        parent having no tools at all — child gets the empty list
        regardless of ``task.tool_allowlist`` (calling for tools the
        parent never had is itself escalation).
    max_depth
        The supervisor's ``[subagent].max_depth`` policy value. The
        runner reads it only for the ``subagent.spawn`` self-prune at
        ``child_ctx.depth == max_depth - 1`` — *not* for the depth-cap
        check itself, which still belongs to the supervisor (the runner
        is called by the supervisor *after* the cap admits the spawn).
        Defaults to :data:`api.DEFAULT_MAX_DEPTH` so unit tests don't
        need to thread the policy through; production callers (iter 8)
        pass the live policy value.

    Returns
    -------
    TaskResult
        Always populated; on errors the runner catches the exception,
        logs, and returns a ``finish_reason=ERROR`` result with the
        exception's message in :attr:`TaskResult.error` rather than
        propagating. The Rust supervisor's ``finally`` releases the slot
        regardless, so a structured return path keeps the cap accounting
        deterministic.

    Notes on isolation guarantees verified by iter 4 tests:

    * The child's :class:`ChatStart.messages` contains only the
      ``role="system"`` prompt + the ``role="user"`` goal. Parent's
      chat history is **not** visible — iter 4 covers the
      ``include_parent_history=False`` default; the optional opt-in
      lands later (Open Question 1 in the design doc).
    * The child's session_key follows ``<parent_session>::child::<seq>``.
    * The child's ``agent_id`` follows ``<parent_agent>::<card>::<seq>``.
    * The persona row, when seeded, is keyed by the child's mangled
      ``agent_id`` under the parent's ``tenant_id`` — iter 5+ memory-host
      lookups will see it without colliding with the parent's row.
    """
    started_ms = _now_ms()
    child_ctx = parent_ctx.child_context(agent_card.name, child_seq)

    # Iter 7: tool-allowlist filter + escalation gate. Run *before*
    # persona seeding / loop construction so a rejected spawn produces
    # zero side effects (no orphaned persona row, no provider call).
    # ``parent_tool_names`` is the canonical source-of-truth for what
    # the parent is allowed to invoke; the child can never see anything
    # outside this set.
    parent_tool_names = _tool_names(parent_tools)
    try:
        child_tool_names = _filter_tools_for_child(
            parent_tool_names=parent_tool_names,
            requested_allowlist=task.tool_allowlist,
            child_depth=child_ctx.depth,
            max_depth=max_depth,
        )
    except _ToolAllowlistEscalationError as exc:
        logger.info(
            "subagent.runner.tool_allowlist_escalation",
            child_session_key=child_ctx.parent_session_key,
            child_agent_id=child_ctx.parent_agent_id,
            offending_tools=sorted(exc.offending),
        )
        return TaskResult(
            output_text="",
            tool_calls_made=[],
            child_session_key=child_ctx.parent_session_key,
            child_agent_id=child_ctx.parent_agent_id,
            elapsed_ms=max(0, _now_ms() - started_ms),
            finish_reason=FinishReason.REJECTED,
            error=TOOL_ALLOWLIST_ESCALATION_ERROR,
        )

    # Project filtered names back onto schemas (the OpenAI-shaped dicts
    # the reasoning loop forwards to the provider). Empty allowlist →
    # empty schema list → loop runs as a pure LLM call.
    child_tools = _project_tool_schemas(parent_tools, child_tool_names)

    # Seed persona row before driving the loop. Best-effort: a failure
    # here would prevent the child from running which is heavier than
    # we want for an observability-only side effect.
    if persona_store is not None:
        try:
            await _seed_child_persona(persona_store, agent_card, child_ctx)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "subagent.runner.persona_seed_failed",
                child_agent_id=child_ctx.parent_agent_id,
                tenant_id=child_ctx.tenant_id,
                error=str(exc),
            )

    messages = _build_child_messages(agent_card, task)
    chat_start = ChatStart(
        # ``model=""`` is a placeholder — real provider routing wires
        # this from the parent's resolved model alias in iter 8. Tests
        # supply a fake provider that ignores the model field.
        model="",
        messages=messages,
        # Iter 7: filtered+pruned schema list. Empty when the parent
        # had no tools, when ``task.tool_allowlist == []`` (the explicit
        # "pure LLM" mode), or when every parent tool was excluded by
        # the depth-prune (only ``subagent.spawn`` at the deepest legal
        # depth, in practice).
        tools=child_tools,
        session_key=child_ctx.parent_session_key,
    )

    loop = ReasoningLoop(provider, tool_result_timeout=tool_result_timeout)
    return await _drive_and_collect(
        loop, chat_start, child_ctx, started_ms, task
    )


#: Cooperative-shutdown grace period after a hard timeout fires. After
#: ``ReasoningLoop.cancel`` is signalled the runner waits up to this many
#: seconds for the loop's own cancel-aware paths to drain (yielding the
#: terminal :class:`ErrorEvent`) before force-dropping the loop task.
#: Matches design § "Timeout handling" — "wait 2s, drops the future".
_TIMEOUT_GRACE_SECONDS: float = 2.0


async def _drive_and_collect(
    loop: ReasoningLoop,
    chat_start: ChatStart,
    child_ctx: ParentContext,
    started_ms: int,
    task: TaskSpec,
) -> TaskResult:
    """Drain :meth:`ReasoningLoop.run` into a :class:`TaskResult`,
    enforcing :attr:`TaskSpec.max_wall_seconds` cooperatively.

    Iter 6 (this revision): the drain is wrapped in
    ``asyncio.wait_for(..., max_wall_seconds)``. On expiry the runner
    cooperates with the loop's existing cancel path
    (``ReasoningLoop.cancel("subagent_timeout")`` →
    ``ErrorEvent(reason="cancelled")``) for up to
    :data:`_TIMEOUT_GRACE_SECONDS`, then force-drops the task. Either
    way: the partial ``output_text`` collected so far is preserved
    verbatim and :attr:`FinishReason.TIMEOUT` lands on the result so
    the parent's LLM observes the wall-clock failure mode.

    The timeout is enforced from Python rather than from the Rust
    supervisor's ``tokio::time::timeout`` because the PyO3 bridge
    (iter 5) hands control to Python under a sync GIL acquisition;
    a parallel ``tokio::time::timeout`` cannot interrupt that. Putting
    the budget here keeps the contract self-consistent and lets unit
    tests exercise it without spinning Rust.
    """
    output_chunks: list[str] = []
    tool_calls: list[ToolCallSummary] = []
    state: _DrainState = {
        "finish_reason": FinishReason.STOP,
        "error_msg": None,
    }

    drain_task: asyncio.Task[None] = asyncio.ensure_future(
        _drain_events(loop, chat_start, output_chunks, tool_calls, state)
    )
    try:
        # ``asyncio.wait_for`` is the cooperative analogue the design
        # called for. ``task.max_wall_seconds`` is the hard ceiling; the
        # supervisor (iter 5) caps this from above via the policy
        # ``max_wall_seconds_ceiling`` (default 300 — see config block).
        await asyncio.wait_for(
            asyncio.shield(drain_task),
            timeout=float(task.max_wall_seconds),
        )
    except TimeoutError:
        # Cooperative cancel first: the loop's own cancel handler emits
        # an ErrorEvent and drains, which lets the drain coroutine exit
        # cleanly with the partial output already accumulated.
        loop.cancel("subagent_timeout")
        try:
            await asyncio.wait_for(
                asyncio.shield(drain_task),
                timeout=_TIMEOUT_GRACE_SECONDS,
            )
        except TimeoutError:
            # Cooperative grace exhausted — force-drop. ``cancel()`` on
            # the asyncio.Task throws CancelledError into the coroutine;
            # we suppress it because we've already captured whatever
            # the drain produced before the freeze.
            drain_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await drain_task
        state["finish_reason"] = FinishReason.TIMEOUT
        # Preserve any partial error_msg the loop set (e.g. cancelled
        # ErrorEvent). If none, leave error blank — TIMEOUT is itself
        # the failure indicator the parent's LLM branches on.
    except Exception as exc:  # pragma: no cover - belt and braces
        logger.warning(
            "subagent.runner.loop_uncaught",
            child_session_key=child_ctx.parent_session_key,
            error=str(exc),
        )
        state["error_msg"] = str(exc)
        state["finish_reason"] = FinishReason.ERROR

    elapsed_ms = max(0, _now_ms() - started_ms)
    return TaskResult(
        output_text="".join(output_chunks),
        tool_calls_made=tool_calls,
        child_session_key=child_ctx.parent_session_key,
        child_agent_id=child_ctx.parent_agent_id,
        elapsed_ms=elapsed_ms,
        finish_reason=state["finish_reason"],
        error=state["error_msg"],
    )


# Drain-state contract: a tiny TypedDict-shaped dict the drain coroutine
# mutates so the cooperative-cancel path can observe partial output
# without racing the drain task's own return value. Plain `dict` keeps
# pyright happy without forcing a TypedDict import for two keys.
_DrainState = dict


async def _drain_events(
    loop: ReasoningLoop,
    chat_start: ChatStart,
    output_chunks: list[str],
    tool_calls: list[ToolCallSummary],
    state: _DrainState,
) -> None:
    """Pump the reasoning loop's event stream into shared collectors.

    Mutating shared lists (rather than returning a tuple) lets the
    timeout layer in :func:`_drive_and_collect` recover whatever was
    collected up to the moment the cancel fired. Without this contract
    the partial-output guarantee documented in design § "Timeout
    handling" wouldn't hold — a TaskCancelled would erase the
    intermediate state along with the task's local frame.
    """
    try:
        async for event in loop.run(chat_start):
            if isinstance(event, TokenEvent):
                # ``is_reasoning`` tokens are the model's thinking trace —
                # we deliberately fold them into output_text so the
                # parent can still observe them; iter 8+ may decide to
                # split reasoning out into its own field.
                output_chunks.append(event.text)
            elif isinstance(event, ToolCallEvent):
                tool_calls.append(_summarise_tool_call(event))
            elif isinstance(event, DoneEvent):
                state["finish_reason"] = _map_finish_reason(event.finish_reason)
            elif isinstance(event, ErrorEvent):
                state["error_msg"] = event.message
                state["finish_reason"] = FinishReason.ERROR
    except asyncio.CancelledError:
        # Re-raise so the wait_for sees cancellation. The shared lists
        # already carry whatever was drained before the cancel fired.
        raise


class _ToolAllowlistEscalationError(Exception):
    """Internal signal raised by :func:`_filter_tools_for_child` when a
    request asks for tools the parent doesn't already hold.

    Caught in :func:`run_child` and translated into a rejected
    :class:`TaskResult` with ``error=tool_allowlist_escalation``. Not
    a public exception — callers see the rejection envelope, never
    this. Carries the offending tool names so the log line is
    actionable for operators.
    """

    def __init__(self, offending: set[str]) -> None:
        super().__init__(
            f"requested tools not in parent allowlist: {sorted(offending)!r}"
        )
        self.offending: frozenset[str] = frozenset(offending)


def _filter_tools_for_child(
    *,
    parent_tool_names: frozenset[str],
    requested_allowlist: list[str] | None,
    child_depth: int,
    max_depth: int,
) -> frozenset[str]:
    """Compute the child's effective tool-name set.

    Implements the design § "Tool exposure" rules verbatim:

    * ``requested_allowlist is None`` (the default) → child inherits
      ``parent_tool_names`` verbatim.
    * ``requested_allowlist == []`` → empty set; pure LLM call. Distinct
      from ``None`` so the parent can opt the child out of all tools
      without the runner inferring "they meant inherit".
    * non-empty list → must be a subset of ``parent_tool_names``;
      anything outside raises :class:`_ToolAllowlistEscalationError`.

    After resolution, prune ``subagent.spawn`` when the child is at the
    deepest depth that could still spawn a grandchild
    (``child_depth >= max_depth - 1``). The supervisor would refuse the
    grandchild's spawn anyway with :attr:`FinishReason.DEPTH_CAPPED`;
    we strip the tool entry so the LLM doesn't waste a round trying.

    Returns a :class:`frozenset` to make the result hash-eq comparable
    in tests and to telegraph immutability — the iter 9 hook event
    payload may capture this set verbatim, and we don't want callers
    accidentally mutating that record.
    """
    if requested_allowlist is None:
        # Inherit: copy the parent's set so the prune below doesn't
        # mutate the parent's view (frozenset is immutable but the call
        # site might not realise we already returned that exact object).
        effective = set(parent_tool_names)
    else:
        requested = set(requested_allowlist)
        # Escalation check first — empty list is a legal subset of every
        # set so it falls straight through to the prune step.
        offending = requested - parent_tool_names
        if offending:
            raise _ToolAllowlistEscalationError(offending)
        effective = requested

    # Self-prune at the deepest legal depth. ``max_depth - 1`` is the
    # last depth at which a child *could* spawn a grandchild; pruning
    # the spawn tool here means the LLM isn't tempted to call it. Below
    # that depth the spawn tool is left in place so the child can
    # delegate one more level.
    if child_depth >= max_depth - 1:
        effective.discard(SUBAGENT_SPAWN_TOOL)
        effective.discard(SUBAGENT_SPAWN_MANY_TOOL)

    return frozenset(effective)


def _tool_names(tools: Sequence[dict[str, Any]] | None) -> frozenset[str]:
    """Extract the OpenAI-shaped tool name set from a schema list.

    Recognises both the wrapped form (``{"type": "function", "function":
    {"name": "..."}}``) and the flat form (``{"name": "..."}``). The
    wrapped form is what the gateway forwards to providers; the flat
    form is what older tests and some adapters use. ``None`` / missing
    entries are skipped silently — a malformed entry isn't worth
    crashing the child over; it's just not visible to it.
    """
    if not tools:
        return frozenset()
    names: set[str] = set()
    for entry in tools:
        if not isinstance(entry, dict):
            continue
        # Wrapped form (the canonical OpenAI shape).
        function = entry.get("function")
        if isinstance(function, dict):
            name = function.get("name")
            if isinstance(name, str) and name:
                names.add(name)
                continue
        # Flat form fallback.
        flat_name = entry.get("name")
        if isinstance(flat_name, str) and flat_name:
            names.add(flat_name)
    return frozenset(names)


def _project_tool_schemas(
    tools: Sequence[dict[str, Any]] | None,
    keep_names: frozenset[str],
) -> list[dict[str, Any]]:
    """Filter the parent's tool-schema list down to the names in
    ``keep_names``, preserving order.

    Order preservation matters for two reasons: (a) some providers
    deterministically prefer earlier-listed tools when the model is
    ambiguous; (b) golden-file tests on iter-8 wire payloads compare
    the JSON shape verbatim. Falls back to skipping malformed entries
    (same rationale as :func:`_tool_names`).
    """
    if not tools:
        return []
    out: list[dict[str, Any]] = []
    for entry in tools:
        if not isinstance(entry, dict):
            continue
        function = entry.get("function")
        name = function.get("name") if isinstance(function, dict) else entry.get("name")
        if isinstance(name, str) and name in keep_names:
            out.append(entry)
    return out


def _build_child_messages(
    agent_card: AgentCard, task: TaskSpec
) -> list[dict[str, Any]]:
    """Assemble the child's chat messages.

    Two-message minimum: ``system`` from the agent card + ``user``
    carrying the task goal. Parent history is **not** inherited —
    that's the whole point of subagent isolation. ``task.extra_context``
    is folded into the system prompt as ``[ctx.<key>]`` blocks; the
    keys are ``BTreeMap``-ordered on the Rust side so the rendered
    prompt is deterministic across processes.
    """
    system_parts: list[str] = []
    if agent_card.system_prompt:
        system_parts.append(agent_card.system_prompt)
    if task.extra_context:
        # Sort for determinism (matches Rust ``BTreeMap`` iteration).
        for key in sorted(task.extra_context.keys()):
            value = task.extra_context[key]
            system_parts.append(f"[ctx.{key}]\n{value}")

    messages: list[dict[str, Any]] = []
    if system_parts:
        messages.append({"role": "system", "content": "\n\n".join(system_parts)})
    messages.append({"role": "user", "content": task.goal})
    return messages


def _summarise_tool_call(event: ToolCallEvent) -> ToolCallSummary:
    """Compress a :class:`ToolCallEvent` into a :class:`ToolCallSummary`.

    The summary shape is fixed by the JSON wire envelope (see
    ``rust/crates/corlinman-subagent/src/types.rs::ToolCallSummary``).
    ``args_summary`` is a one-line synopsis — for iter 4 we just truncate
    the raw arguments JSON to 200 chars; iter 7 will let the tool plugin
    supply a custom summariser.
    """
    raw = event.args_json.decode("utf-8", errors="replace") if event.args_json else ""
    args_summary = raw[:200] + ("…" if len(raw) > 200 else "")
    return ToolCallSummary(
        name=event.tool or event.plugin or "unknown",
        args_summary=args_summary,
        # iter-4 has no per-call timing yet — iter 7 wires from
        # plugin executor latency. Zero is fine because the parent's
        # prompt is allowed to display it as "n/a".
        duration_ms=0,
    )


def _map_finish_reason(provider_reason: str) -> FinishReason:
    """Map :class:`DoneEvent.finish_reason` strings to :class:`FinishReason`.

    The reasoning loop emits the OpenAI-standard vocabulary
    (``"stop"`` / ``"length"`` / ``"tool_calls"`` / ``"content_filter"``).
    We promote ``"stop"`` and ``"length"`` to their direct counterparts;
    everything else maps to ``STOP`` for iter 4 (the parent's prompt
    only branches on ``stop`` vs the rejection reasons for now).
    """
    match provider_reason:
        case "stop":
            return FinishReason.STOP
        case "length":
            return FinishReason.LENGTH
        case _:
            # ``tool_calls`` is the second-most common finish reason
            # but at iter 4 we have no tools wired so the loop won't
            # actually emit it on the happy path. iter 7 may need to
            # re-classify for the parent's prompt.
            return FinishReason.STOP


async def _seed_child_persona(
    store: PersonaStore,
    agent_card: AgentCard,
    child_ctx: ParentContext,
) -> None:
    """Insert a default-shaped persona row for the child's mangled id.

    Mirrors :func:`corlinman_persona.seeder.seed_from_card` but bypasses
    the YAML round-trip: we already have the in-memory :class:`AgentCard`
    and the child's mangled ``agent_id`` is what we need to persist
    under the parent's ``tenant_id``. Skips the write if a row already
    exists (idempotent — re-runs of the same child during a test
    fixture replay don't double-seed).

    Lazy-imports :mod:`corlinman_persona.state` so callers that pass
    ``persona_store=None`` to :func:`run_child` don't pay the import
    cost — the dependency is declared in pyproject so this never fails
    in production but keeps the runtime graph minimal in tests that
    stub everything.
    """
    from corlinman_persona.state import PersonaState  # local import: see docstring

    existing = await store.get(
        child_ctx.parent_agent_id, tenant_id=child_ctx.tenant_id
    )
    if existing is not None:
        # Sibling re-runs / forensic replays: do NOT mutate. Matches
        # the seeder's "leave existing rows alone" stance.
        return

    state = PersonaState(
        agent_id=child_ctx.parent_agent_id,
        mood="neutral",
        fatigue=0.0,
        recent_topics=[],
        # ``upsert`` fills updated_at with "now" when we pass 0, which
        # is what the YAML seeder also relies on.
        updated_at_ms=0,
        state_json={},
    )
    await store.upsert(state, tenant_id=child_ctx.tenant_id)


def _now_ms() -> int:
    """Wall-clock milliseconds. Test fixtures monkey-patch this — keep
    the signature trivial."""
    return int(time.time() * 1000)


# Pylint quietener: ``replace`` re-export keeps the public surface tidy
# even though the runner doesn't itself dataclass-replace anything in
# iter 4. iter 6 will reach for it when overlaying timeout outcomes
# onto a partial TaskResult.
__all__ = [
    "SUBAGENT_SPAWN_MANY_TOOL",
    "SUBAGENT_SPAWN_TOOL",
    "TOOL_ALLOWLIST_ESCALATION_ERROR",
    "replace",
    "run_child",
]
