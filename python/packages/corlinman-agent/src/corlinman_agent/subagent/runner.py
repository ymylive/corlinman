"""Happy-path child driver — :func:`run_child`.

Iter 4 (this commit) lands the **happy path only**: the supervisor (Rust,
``corlinman-subagent`` crate) calls in here once it has decided the spawn
is allowed and a slot is reserved; the runner builds a fresh
:class:`ChatStart`, optionally seeds a fresh persona row under a mangled
``agent_id``, drives a fresh :class:`ReasoningLoop` to exhaustion,
collects the streamed events, and returns a :class:`TaskResult`.

What is *deliberately* NOT here yet:

* timeout enforcement → wraps in ``tokio::time::timeout`` from the Rust
  side in iter 6;
* tool-allowlist filtering / escalation reject → iter 7;
* PyO3 entry point → the supervisor calls this function over GIL in
  iter 5; for now it's pure Python and unit-tested in isolation;
* hook-bus observability → iter 9.

The split between this happy-path runner and the supervisor is the same
split documented in the design § "Implementation surface — Rust supervisor
+ Python runner": the **isolation contract** lives where the LLM cannot
reach it (Rust); the **loop driver** has to call into Python because that's
where :class:`ReasoningLoop` and the providers live.
"""

from __future__ import annotations

import asyncio
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
    FinishReason,
    ParentContext,
    TaskResult,
    TaskSpec,
    ToolCallSummary,
)

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
    persona_store: "PersonaStore | None" = None,
    tool_result_timeout: float = 0.05,
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
        # Tool list is empty in iter 4 (filtering arrives in iter 7).
        # The reasoning loop treats empty tools as "no tool calls
        # allowed" which is exactly what we want for the iter-4 happy
        # path: the mock provider streams text and emits a single
        # ``done(stop)`` chunk.
        tools=[],
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
    except asyncio.TimeoutError:
        # Cooperative cancel first: the loop's own cancel handler emits
        # an ErrorEvent and drains, which lets the drain coroutine exit
        # cleanly with the partial output already accumulated.
        loop.cancel("subagent_timeout")
        try:
            await asyncio.wait_for(
                asyncio.shield(drain_task),
                timeout=_TIMEOUT_GRACE_SECONDS,
            )
        except asyncio.TimeoutError:
            # Cooperative grace exhausted — force-drop. ``cancel()`` on
            # the asyncio.Task throws CancelledError into the coroutine;
            # we suppress it because we've already captured whatever
            # the drain produced before the freeze.
            drain_task.cancel()
            try:
                await drain_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
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
    store: "PersonaStore",
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
__all__ = ["run_child", "replace"]
