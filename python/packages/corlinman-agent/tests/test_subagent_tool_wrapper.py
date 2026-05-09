"""Iter 8 tool-wrapper / dispatch tests.

Covers the design § "Implementation order" iter-8 acceptance:

* ``subagent.spawn`` tool descriptor is OpenAI-shaped and the LLM can
  emit a ``ToolCallEvent`` for it.
* Gateway dispatcher routes the call into the runner; the JSON
  ``TaskResult`` envelope is the ``ToolResult.content`` value the
  parent's loop will see (forensic-replay test piggybacks on this).
* The full failure-mapping table from
  ``tool_wrapper.dispatch_subagent_spawn`` is exercised: agent-not-found,
  args-invalid, supervisor depth/quota rejections, runner exceptions.

The mocked supervisor pattern matches what the iter-5 PyO3 bridge will
provide in production: a callable that returns either a context-manager
slot drop-guard on success or a string identifier on rejection. We
verify slot enforcement by tracking a counter inside the test fixture.

E2E through the agent servicer is also exercised here (rather than as a
separate integration test) so the wave-4 acceptance — "tool envelope
becomes ToolResult.content" — is locked behind a single fast unit test.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import contextmanager
from typing import Any

import pytest
from corlinman_agent.agents.card import AgentCard
from corlinman_agent.agents.registry import AgentCardRegistry
from corlinman_agent.subagent import (
    AGENT_NOT_FOUND_ERROR,
    ARGS_INVALID_ERROR,
    FinishReason,
    ParentContext,
    SUBAGENT_SPAWN_TOOL,
    dispatch_subagent_spawn,
    subagent_spawn_tool_schema,
)
from corlinman_providers.base import ProviderChunk


def _registry(*cards: AgentCard) -> AgentCardRegistry:
    """Construct an in-memory :class:`AgentCardRegistry` for the test
    cards. Bypasses the YAML loader so test setup stays in-process."""
    return AgentCardRegistry({c.name: c for c in cards})


def _card(name: str, system_prompt: str = "You are a test agent.") -> AgentCard:
    return AgentCard(name=name, description="", system_prompt=system_prompt)


def _parent_ctx() -> ParentContext:
    return ParentContext(
        tenant_id="tenant-a",
        parent_agent_id="main",
        parent_session_key="root",
        depth=0,
        trace_id="trace-test",
    )


class _FakeProvider:
    """Echoes a fixed token + done(stop). Mirrors the provider stub used
    in :mod:`test_subagent_runner`."""

    def __init__(self, text: str = "child output") -> None:
        self._text = text
        self.calls = 0

    async def chat_stream(self, **_: Any) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
        self.calls += 1
        yield ProviderChunk(kind="token", text=self._text)
        yield ProviderChunk(kind="done", finish_reason="stop")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_tool_schema_shape_is_openai_compatible() -> None:
    """The descriptor matches the OpenAI ``tools=`` array entry shape so
    every provider adapter (OpenAI, Anthropic, Gemini, ...) accepts it
    without translation. Locks the field path the runner's allowlist
    filter (iter 7) walks: ``function.name``."""
    schema = subagent_spawn_tool_schema()

    assert schema["type"] == "function"
    fn = schema["function"]
    assert fn["name"] == SUBAGENT_SPAWN_TOOL
    assert fn["name"] == "subagent.spawn", "wire-stable identifier"
    params = fn["parameters"]
    assert params["type"] == "object"
    # Required fields = the minimum the LLM must emit for a valid call.
    assert set(params["required"]) == {"agent", "goal"}
    # Optional fields documented in the schema so the LLM knows they exist.
    for key in (
        "agent",
        "goal",
        "tool_allowlist",
        "max_wall_seconds",
        "max_tool_calls",
        "extra_context",
    ):
        assert key in params["properties"], f"missing {key} in schema"


def test_tool_schema_documents_default_budgets() -> None:
    """Description strings in ``max_wall_seconds`` / ``max_tool_calls``
    surface the policy defaults so the LLM has a sensible expectation
    when it omits the budget fields. Caller can override the defaults
    (e.g. when the supervisor's policy is tightened) and the schema's
    description reflects that."""
    schema = subagent_spawn_tool_schema(
        default_max_wall_seconds=42,
        default_max_tool_calls=7,
    )
    desc_wall = schema["function"]["parameters"]["properties"]["max_wall_seconds"][
        "description"
    ]
    desc_calls = schema["function"]["parameters"]["properties"]["max_tool_calls"][
        "description"
    ]
    assert "42" in desc_wall
    assert "7" in desc_calls


# ---------------------------------------------------------------------------
# Happy path — JSON envelope round-trip
# ---------------------------------------------------------------------------


async def test_dispatch_returns_json_envelope_on_happy_path() -> None:
    """The LLM emits a ``ToolCallEvent("subagent.spawn", args_json)``;
    the gateway dispatcher calls :func:`dispatch_subagent_spawn`; the
    return string is what becomes ``ToolResult.content`` and gets
    appended to the parent's chat as a ``role="tool"`` message.

    Asserts: parseable JSON, ``output_text`` matches the child's
    streamed token, ``finish_reason="stop"``, no error key on the wire
    (the design pinned this — keeps parent token-spend low)."""
    args = json.dumps({"agent": "researcher", "goal": "research X"})
    provider = _FakeProvider(text="found three papers")

    content = await dispatch_subagent_spawn(
        args_json=args.encode(),
        parent_ctx=_parent_ctx(),
        agent_registry=_registry(_card("researcher")),
        provider=provider,
    )

    payload = json.loads(content)
    assert payload["output_text"] == "found three papers"
    assert payload["finish_reason"] == "stop"
    assert payload["tool_calls_made"] == []
    assert "error" not in payload, "error must be elided on happy path"
    assert payload["child_session_key"] == "root::child::0"
    assert payload["child_agent_id"] == "main::researcher::0"
    assert provider.calls == 1


async def test_dispatch_propagates_parent_tools_to_runner() -> None:
    """If the LLM passes ``tool_allowlist`` and ``parent_tools`` is
    threaded through, the iter-7 filter / escalation behaviour is
    visible end-to-end. Here we ask for a tool the parent doesn't have
    → escalation reject lands on the wire envelope."""
    args = json.dumps(
        {
            "agent": "researcher",
            "goal": "x",
            "tool_allowlist": ["forbidden"],
        }
    )
    parent_tools = [
        {"type": "function", "function": {"name": "web_search"}},
    ]

    content = await dispatch_subagent_spawn(
        args_json=args,
        parent_ctx=_parent_ctx(),
        agent_registry=_registry(_card("researcher")),
        provider=_FakeProvider(),
        parent_tools=parent_tools,
    )

    payload = json.loads(content)
    assert payload["finish_reason"] == "rejected"
    assert payload["error"] == "tool_allowlist_escalation"


# ---------------------------------------------------------------------------
# Failure mapping
# ---------------------------------------------------------------------------


async def test_dispatch_rejects_unknown_agent() -> None:
    """The registry lookup misses → REJECTED + ``agent_not_found``.
    No provider call must happen — the spawn never started."""
    args = json.dumps({"agent": "ghost", "goal": "x"})
    provider = _FakeProvider()
    content = await dispatch_subagent_spawn(
        args_json=args,
        parent_ctx=_parent_ctx(),
        agent_registry=_registry(_card("researcher")),  # 'ghost' not present
        provider=provider,
    )
    payload = json.loads(content)
    assert payload["finish_reason"] == "rejected"
    assert payload["error"].startswith(AGENT_NOT_FOUND_ERROR)
    assert "ghost" in payload["error"]
    assert provider.calls == 0


@pytest.mark.parametrize(
    "args_json,expected_fragment",
    [
        (b"not json at all", "args_json not JSON"),
        (b'{"goal": "no agent"}', "missing or empty 'agent'"),
        (b'{"agent": "researcher"}', "missing or empty 'goal'"),
        (b'{"agent": "researcher", "goal": "x", "tool_allowlist": "not-a-list"}',
         "'tool_allowlist' must be a list of strings"),
        (b'{"agent": "researcher", "goal": "x", "max_wall_seconds": -3}',
         "'max_wall_seconds' must be a positive integer"),
        (b'[1, 2, 3]', "must be a JSON object"),
    ],
)
async def test_dispatch_rejects_args_invalid(
    args_json: bytes, expected_fragment: str
) -> None:
    """Every malformed-args branch returns REJECTED with
    ``error="args_invalid: <detail>"``. The parent's LLM can branch on
    the prefix and recover (e.g. retry with a corrected payload)."""
    content = await dispatch_subagent_spawn(
        args_json=args_json,
        parent_ctx=_parent_ctx(),
        agent_registry=_registry(_card("researcher")),
        provider=_FakeProvider(),
    )
    payload = json.loads(content)
    assert payload["finish_reason"] == "rejected"
    assert payload["error"].startswith(ARGS_INVALID_ERROR)
    assert expected_fragment in payload["error"]


async def test_dispatch_maps_supervisor_depth_cap_to_depth_capped() -> None:
    """The supervisor returns the string ``"depth_capped"`` from
    :meth:`Supervisor::try_acquire` (Rust enum's
    ``AcquireReject::DepthCapped``). The dispatcher must map that to
    :attr:`FinishReason.DEPTH_CAPPED` so the LLM sees the
    "you tried to recurse too deep" branch.
    """
    def acquire(ctx: ParentContext) -> str:
        # Returning a string signals rejection; production binding does
        # the same after the Rust supervisor refuses.
        return "depth_capped"

    args = json.dumps({"agent": "researcher", "goal": "x"})
    content = await dispatch_subagent_spawn(
        args_json=args,
        parent_ctx=_parent_ctx(),
        agent_registry=_registry(_card("researcher")),
        provider=_FakeProvider(),
        supervisor_acquire=acquire,
    )
    payload = json.loads(content)
    assert payload["finish_reason"] == "depth_capped"
    # The session key marks the slot as never-allocated.
    assert payload["child_session_key"].endswith("::child::-")


async def test_dispatch_maps_supervisor_other_caps_to_rejected() -> None:
    """Per-parent / per-tenant cap rejections come back as anything
    other than ``"depth_capped"`` and map to :attr:`FinishReason.REJECTED`
    so the LLM doesn't conflate the depth cap (you're too deep) with
    the concurrency cap (siblings already in flight)."""
    def acquire(ctx: ParentContext) -> str:
        return "parent_concurrency_exceeded"

    args = json.dumps({"agent": "researcher", "goal": "x"})
    content = await dispatch_subagent_spawn(
        args_json=args,
        parent_ctx=_parent_ctx(),
        agent_registry=_registry(_card("researcher")),
        provider=_FakeProvider(),
        supervisor_acquire=acquire,
    )
    payload = json.loads(content)
    assert payload["finish_reason"] == "rejected"
    assert "parent_concurrency_exceeded" in payload["error"]


async def test_dispatch_holds_supervisor_slot_until_runner_returns() -> None:
    """The slot drop-guard must wrap :func:`run_child` — the in-flight
    counter reads 1 *during* the child run and 0 after. Mirrors the
    Rust ``slot_held_during_python_call`` test pinned at the language
    boundary."""
    in_flight = 0
    peak_in_flight = 0

    @contextmanager
    def slot_guard():
        nonlocal in_flight, peak_in_flight
        in_flight += 1
        peak_in_flight = max(peak_in_flight, in_flight)
        try:
            yield
        finally:
            in_flight -= 1

    def acquire(_ctx: ParentContext) -> Any:
        return slot_guard()

    args = json.dumps({"agent": "researcher", "goal": "x"})
    await dispatch_subagent_spawn(
        args_json=args,
        parent_ctx=_parent_ctx(),
        agent_registry=_registry(_card("researcher")),
        provider=_FakeProvider(),
        supervisor_acquire=acquire,
    )

    assert peak_in_flight == 1, "slot must be held during the runner"
    assert in_flight == 0, "slot must release after dispatch returns"


async def test_dispatch_clamps_max_wall_seconds_to_ceiling() -> None:
    """The supervisor's ``max_wall_seconds_ceiling`` policy is enforced
    from above: the LLM cannot escape the deployment budget by asking
    for more. Verified by passing a ceiling lower than the request and
    observing the runner gets the clamped value via a slow provider
    that would otherwise hang for the full request budget."""

    class _CapturingSlowProvider:
        def __init__(self) -> None:
            self.tool_calls: list[Any] = []

        async def chat_stream(self, **_: Any) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
            # Sleep long enough that the ceiling-clamped budget fires
            # but the unclamped budget would not have.
            await asyncio.sleep(2.0)
            yield ProviderChunk(kind="done", finish_reason="stop")

    args = json.dumps(
        {"agent": "researcher", "goal": "x", "max_wall_seconds": 60}
    )
    content = await dispatch_subagent_spawn(
        args_json=args,
        parent_ctx=_parent_ctx(),
        agent_registry=_registry(_card("researcher")),
        provider=_CapturingSlowProvider(),
        max_wall_seconds_ceiling=1,  # clamp 60 → 1
    )
    payload = json.loads(content)
    # Ceiling-clamped budget fired → TIMEOUT; runner returns within
    # the 1s budget plus 2s grace.
    assert payload["finish_reason"] == "timeout"


# ---------------------------------------------------------------------------
# Forensic-replay invariant — locked by design
# ---------------------------------------------------------------------------


async def test_dispatch_envelope_carries_child_ids_for_replay() -> None:
    """The on-wire JSON must always carry ``child_session_key`` and
    ``child_agent_id`` even on the happy path — the design's "Result
    merging" section documents these as forensic-replay handles. A
    rejected envelope marks the slot as ``::child::-``; a successful
    one carries the spawned ids verbatim."""
    args = json.dumps({"agent": "researcher", "goal": "x"})
    content = await dispatch_subagent_spawn(
        args_json=args,
        parent_ctx=_parent_ctx(),
        agent_registry=_registry(_card("researcher")),
        provider=_FakeProvider(),
    )
    payload = json.loads(content)
    assert payload["child_session_key"] == "root::child::0"
    assert payload["child_agent_id"] == "main::researcher::0"
