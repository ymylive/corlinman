"""Tests for ``subagent.spawn_many`` — the v0.7 parallel sibling fan-out.

The fan-out wrapper is a thin orchestration layer over the existing
``dispatch_subagent_spawn``: same per-child validation, same supervisor
acquire callable, but N coros running under ``asyncio.gather``. The
tests below pin three contracts:

1. **Wire shape.** The tool descriptor matches OpenAI's
   function-calling schema and the envelope is
   ``{"tasks": [TaskResult, ...]}`` in input order.
2. **Concurrency.** N siblings really run in parallel — total wall
   clock is ``max(child_time)``, not ``sum(child_time)``.
3. **Cap enforcement.** Asking for more siblings than the per-fanout
   cap returns a top-level args-invalid envelope before any child
   spawns.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from corlinman_agent.agents.card import AgentCard
from corlinman_agent.agents.registry import AgentCardRegistry
from corlinman_agent.subagent import (
    ARGS_INVALID_ERROR,
    SUBAGENT_SPAWN_MANY_MAX_TASKS,
    SUBAGENT_SPAWN_MANY_TOOL,
    ParentContext,
    dispatch_subagent_spawn_many,
    subagent_spawn_many_tool_schema,
)
from corlinman_providers.base import ProviderChunk


def _registry(*cards: AgentCard) -> AgentCardRegistry:
    return AgentCardRegistry({c.name: c for c in cards})


def _card(name: str) -> AgentCard:
    return AgentCard(name=name, description="", system_prompt="You are " + name)


def _parent_ctx() -> ParentContext:
    return ParentContext(
        tenant_id="tenant-a",
        parent_agent_id="main",
        parent_session_key="root",
        depth=0,
        trace_id="trace-test",
    )


class _SleepyProvider:
    """Sleeps for ``delay_s`` then emits one token + done. Lets the
    parallelism test measure wall-clock vs. summed delays."""

    def __init__(self, *, text: str, delay_s: float) -> None:
        self._text = text
        self._delay_s = delay_s
        self.calls = 0

    async def chat_stream(self, **_: Any) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
        self.calls += 1
        await asyncio.sleep(self._delay_s)
        yield ProviderChunk(kind="token", text=self._text)
        yield ProviderChunk(kind="done", finish_reason="stop")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_spawn_many_schema_shape_is_openai_compatible() -> None:
    """Tool name is the wire-stable identifier; ``tasks`` is required;
    ``minItems`` / ``maxItems`` lock the per-fanout cap."""
    schema = subagent_spawn_many_tool_schema()
    assert schema["type"] == "function"
    fn = schema["function"]
    assert fn["name"] == SUBAGENT_SPAWN_MANY_TOOL
    assert fn["name"] == "subagent.spawn_many"
    params = fn["parameters"]
    assert params["required"] == ["tasks"]
    tasks = params["properties"]["tasks"]
    assert tasks["type"] == "array"
    assert tasks["minItems"] == 1
    assert tasks["maxItems"] == SUBAGENT_SPAWN_MANY_MAX_TASKS
    # The per-task object shape must accept the same fields as
    # subagent.spawn so the LLM doesn't have to learn two schemas.
    per_task = tasks["items"]
    assert set(per_task["required"]) == {"agent", "goal"}
    for key in ("agent", "goal", "tool_allowlist", "max_wall_seconds",
                "max_tool_calls", "extra_context"):
        assert key in per_task["properties"], f"missing {key}"


# ---------------------------------------------------------------------------
# Happy path — fan-out wire shape
# ---------------------------------------------------------------------------


async def test_dispatch_many_returns_tasks_envelope_in_input_order() -> None:
    """Two siblings dispatched, envelope shape is
    ``{"tasks": [TaskResult, TaskResult]}`` in the order the LLM
    specified — *not* completion order. Order stability is what lets
    the orchestrator's reduce step reference siblings by index."""
    provider_a = _SleepyProvider(text="from-a", delay_s=0.0)
    # One shared provider: the dispatcher calls ``provider.chat_stream``
    # once per sibling, and we assert on order + finish_reason rather
    # than on which provider answered which sibling.
    args = json.dumps(
        {
            "tasks": [
                {"agent": "researcher", "goal": "A"},
                {"agent": "editor", "goal": "B"},
            ]
        }
    )
    # Both siblings see the same provider (test artifact); the
    # ``output_text`` differs because we stick the agent name in.
    content = await dispatch_subagent_spawn_many(
        args_json=args,
        parent_ctx=_parent_ctx(),
        agent_registry=_registry(_card("researcher"), _card("editor")),
        provider=provider_a,
    )
    payload = json.loads(content)
    assert "error" not in payload, "happy path must elide the outer error"
    assert "tasks" in payload
    assert len(payload["tasks"]) == 2
    # Order matches the input list — the dispatcher uses asyncio.gather
    # which preserves input order regardless of completion order.
    assert payload["tasks"][0]["finish_reason"] == "stop"
    assert payload["tasks"][1]["finish_reason"] == "stop"
    # child_seq disambiguates siblings: 0 and 1 respectively.
    assert payload["tasks"][0]["child_session_key"] == "root::child::0"
    assert payload["tasks"][1]["child_session_key"] == "root::child::1"


# ---------------------------------------------------------------------------
# Concurrency — the whole point of spawn_many
# ---------------------------------------------------------------------------


async def test_dispatch_many_runs_siblings_concurrently() -> None:
    """Three siblings sleeping 0.25s each must complete in well under
    the summed 0.75s — proves asyncio.gather actually runs them
    concurrently rather than awaiting them serially. We use a generous
    upper bound to keep the test stable on slow CI hardware."""
    delay = 0.25
    n = 3
    provider = _SleepyProvider(text="slow", delay_s=delay)
    args = json.dumps(
        {
            "tasks": [
                {"agent": "researcher", "goal": f"task-{i}"}
                for i in range(n)
            ]
        }
    )
    start = time.perf_counter()
    content = await dispatch_subagent_spawn_many(
        args_json=args,
        parent_ctx=_parent_ctx(),
        agent_registry=_registry(_card("researcher")),
        provider=provider,
    )
    elapsed = time.perf_counter() - start

    payload = json.loads(content)
    assert len(payload["tasks"]) == n
    # Strict: must be closer to a single delay than to N*delay. We pick
    # 2x single-delay as the ceiling so a slow CI machine doesn't flake.
    assert elapsed < 2 * delay, (
        f"siblings ran serially: {elapsed:.3f}s for {n} * {delay}s siblings"
    )
    # And > 0 so the test fails loudly if the fixture stops sleeping.
    assert elapsed > delay * 0.5


# ---------------------------------------------------------------------------
# Cap & validation
# ---------------------------------------------------------------------------


async def test_dispatch_many_rejects_over_cap_before_dispatch() -> None:
    """Four siblings asked, cap is 3 → top-level args-invalid envelope.
    No child runs (provider.calls == 0). This shape is critical: the
    LLM sees one clear error, not three successes plus one cap-rejected
    sibling."""
    provider = _SleepyProvider(text="x", delay_s=0.0)
    args = json.dumps(
        {
            "tasks": [
                {"agent": "researcher", "goal": f"t{i}"}
                for i in range(SUBAGENT_SPAWN_MANY_MAX_TASKS + 1)
            ]
        }
    )
    content = await dispatch_subagent_spawn_many(
        args_json=args,
        parent_ctx=_parent_ctx(),
        agent_registry=_registry(_card("researcher")),
        provider=provider,
    )
    payload = json.loads(content)
    assert payload["tasks"] == []
    assert payload["error"].startswith(ARGS_INVALID_ERROR)
    assert "exceeds the per-fanout cap" in payload["error"]
    assert provider.calls == 0, "no child should run when args-invalid"


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ("not-json", "args_json not JSON"),
        ('{"tasks": "not-a-list"}', "non-empty list"),
        ('{"tasks": []}', "non-empty list"),
        ('{"tasks": [123]}', "must be a JSON object"),
    ],
)
async def test_dispatch_many_args_invalid_envelope(body: str, fragment: str) -> None:
    """Malformed envelopes return ``{"tasks": [], "error": "args_invalid: ..."}``
    so the LLM can route on a single error string."""
    content = await dispatch_subagent_spawn_many(
        args_json=body,
        parent_ctx=_parent_ctx(),
        agent_registry=_registry(_card("researcher")),
        provider=_SleepyProvider(text="", delay_s=0.0),
    )
    payload = json.loads(content)
    assert payload["tasks"] == []
    assert payload["error"].startswith(ARGS_INVALID_ERROR)
    assert fragment in payload["error"]


# ---------------------------------------------------------------------------
# Supervisor integration — per-sibling slot acquire
# ---------------------------------------------------------------------------


async def test_dispatch_many_acquires_one_slot_per_sibling() -> None:
    """The supervisor sees N acquire calls — one per sibling. Verified
    by counting calls to the mock acquire callable. Critical for the
    per-parent concurrency cap to do its job: each sibling enters the
    cap budget independently."""
    acquired = 0

    from contextlib import contextmanager as _ctx

    @_ctx
    def slot_guard() -> Any:
        nonlocal acquired
        acquired += 1
        yield

    def acquire(_ctx_arg: ParentContext) -> Any:
        return slot_guard()

    provider = _SleepyProvider(text="x", delay_s=0.0)
    args = json.dumps(
        {
            "tasks": [
                {"agent": "researcher", "goal": "a"},
                {"agent": "researcher", "goal": "b"},
            ]
        }
    )
    await dispatch_subagent_spawn_many(
        args_json=args,
        parent_ctx=_parent_ctx(),
        agent_registry=_registry(_card("researcher")),
        provider=provider,
        supervisor_acquire=acquire,
    )
    assert acquired == 2, "one supervisor acquire per sibling"


async def test_dispatch_many_one_sibling_rejected_others_succeed() -> None:
    """When the supervisor refuses one sibling (e.g. tenant quota hit
    on the third call), the envelope still returns three siblings —
    the rejected one carries ``finish_reason="rejected"``, the others
    carry their normal results. The wire shape stays uniform so the
    orchestrator's reduce step never sees a hole in the list."""
    call_count = 0

    def acquire(_ctx_arg: ParentContext) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            # Reject the second sibling specifically (call_count is
            # incremented before the return so call_count==2 means the
            # second acquire fired).
            return "tenant_quota_exceeded"
        # Other siblings get a do-nothing slot.
        from contextlib import nullcontext
        return nullcontext()

    args = json.dumps(
        {
            "tasks": [
                {"agent": "researcher", "goal": "a"},
                {"agent": "researcher", "goal": "b"},
                {"agent": "researcher", "goal": "c"},
            ]
        }
    )
    content = await dispatch_subagent_spawn_many(
        args_json=args,
        parent_ctx=_parent_ctx(),
        agent_registry=_registry(_card("researcher")),
        provider=_SleepyProvider(text="ok", delay_s=0.0),
        supervisor_acquire=acquire,
    )
    payload = json.loads(content)
    assert len(payload["tasks"]) == 3
    reasons = [t["finish_reason"] for t in payload["tasks"]]
    # Exactly one rejection somewhere — order depends on which sibling
    # the supervisor refused. Two stops + one rejected is the contract.
    assert reasons.count("rejected") == 1
    assert reasons.count("stop") == 2
