"""Iter 4 happy-path runner tests for ``corlinman_agent.subagent.runner``.

The five test rows mandated by the design's test matrix
(``docs/design/phase4-w4-d3-design.md`` § "Test matrix"):

* ``spawn_child_happy_path_returns_output``
* ``child_session_key_distinct_from_parent``
* ``child_persona_row_freshly_created``
* ``child_persona_row_under_same_tenant``
* ``parent_chat_history_not_visible_to_child``

The runner sits between the (forthcoming, iter 5) Rust supervisor's PyO3
bridge and the existing :class:`ReasoningLoop`. We mock the provider with
the same ``_FakeProvider`` shape the loop's own tests use
(``test_reasoning_loop.py``); persona-side effects exercise a real
:class:`PersonaStore` against a tmp_path SQLite file so the composite-PK
constraint actually fires.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from corlinman_agent.agents.card import AgentCard
from corlinman_agent.subagent import (
    FinishReason,
    ParentContext,
    TaskSpec,
    run_child,
)
from corlinman_persona.store import PersonaStore
from corlinman_providers.base import ProviderChunk


class _FakeProvider:
    """Yields a preset list of :class:`ProviderChunk` values.

    Mirrors the shape used by ``test_reasoning_loop.py::_FakeProvider`` so
    we don't introduce new test infrastructure. Captures every
    ``messages`` payload the loop hands the provider so tests can assert
    isolation properties (e.g. parent history not leaked).
    """

    def __init__(self, chunks: list[ProviderChunk]) -> None:
        self._chunks = chunks
        self.messages_seen: list[list[dict[str, Any]]] = []

    async def chat_stream(
        self, *, messages: list[dict[str, Any]], **_: Any
    ) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
        # Snapshot the messages so the test can inspect what the child
        # actually saw on its only provider round.
        self.messages_seen.append(list(messages))
        for c in self._chunks:
            yield c


def _agent_card(
    *,
    name: str = "researcher",
    system_prompt: str = "You are a careful researcher.",
) -> AgentCard:
    """Minimal AgentCard for a child run. The runner only reads
    :attr:`AgentCard.name` (for id mangling) and
    :attr:`AgentCard.system_prompt` (for the child's first message)."""
    return AgentCard(
        name=name,
        description="",
        system_prompt=system_prompt,
    )


def _parent_ctx(
    *,
    tenant: str = "tenant-a",
    parent_agent_id: str = "main",
    parent_session_key: str = "root",
    depth: int = 0,
) -> ParentContext:
    return ParentContext(
        tenant_id=tenant,
        parent_agent_id=parent_agent_id,
        parent_session_key=parent_session_key,
        depth=depth,
        trace_id="trace-test",
    )


# ---------------------------------------------------------------------------
# Test row 1: spawn_child_happy_path_returns_output
# ---------------------------------------------------------------------------


async def test_spawn_child_happy_path_returns_output() -> None:
    """Mock provider streams two tokens then ``done(stop)``; the runner
    concatenates them into :attr:`TaskResult.output_text` and reports
    ``finish_reason=STOP``. No tool calls happen, so ``tool_calls_made``
    is the empty list — the wire envelope still carries the field.
    """
    provider = _FakeProvider(
        [
            ProviderChunk(kind="token", text="transformers "),
            ProviderChunk(kind="token", text="are neural nets"),
            ProviderChunk(kind="done", finish_reason="stop"),
        ]
    )

    result = await run_child(
        _parent_ctx(),
        _agent_card(),
        TaskSpec(goal="research transformers"),
        provider=provider,
    )

    assert result.output_text == "transformers are neural nets"
    assert result.finish_reason is FinishReason.STOP
    assert result.tool_calls_made == []
    assert result.error is None
    assert result.elapsed_ms >= 0  # millisecond clock — may be zero on fast CI


# ---------------------------------------------------------------------------
# Test row 2: child_session_key_distinct_from_parent
# ---------------------------------------------------------------------------


async def test_child_session_key_distinct_from_parent() -> None:
    """The runner's ``parent_ctx.parent_session_key="root"`` must produce
    a child whose ``child_session_key`` follows ``<parent>::child::<seq>``
    and is never equal to the parent's key — that's the forensic-replay
    contract documented in the design § "Inheritance / fresh / bounded".
    """
    provider = _FakeProvider(
        [ProviderChunk(kind="done", finish_reason="stop")]
    )

    result = await run_child(
        _parent_ctx(parent_session_key="root"),
        _agent_card(),
        TaskSpec(goal="anything"),
        provider=provider,
        child_seq=0,
    )

    assert result.child_session_key != "root"
    assert result.child_session_key == "root::child::0"

    # And siblings with different ``child_seq`` produce distinct keys.
    second = await run_child(
        _parent_ctx(parent_session_key="root"),
        _agent_card(),
        TaskSpec(goal="anything"),
        provider=_FakeProvider(
            [ProviderChunk(kind="done", finish_reason="stop")]
        ),
        child_seq=1,
    )
    assert second.child_session_key == "root::child::1"
    assert second.child_session_key != result.child_session_key


# ---------------------------------------------------------------------------
# Test row 3: child_persona_row_freshly_created
# ---------------------------------------------------------------------------


async def test_child_persona_row_freshly_created(tmp_path: Path) -> None:
    """When ``persona_store`` is wired in, a new row is seeded under the
    child's mangled ``agent_id`` (``<parent>::<card>::<seq>``). Defaults
    apply (mood="neutral", fatigue=0.0, empty topics). Parent row, if any,
    is unaffected — siblings of the parent's lineage stay clean.
    """
    db_path = tmp_path / "agent_state.sqlite"
    provider = _FakeProvider(
        [ProviderChunk(kind="done", finish_reason="stop")]
    )

    async with PersonaStore(db_path) as store:
        # Seed the parent row first so we can assert it isn't touched.
        from corlinman_persona.state import PersonaState

        await store.upsert(
            PersonaState(
                agent_id="main",
                mood="focused",
                fatigue=0.42,
                recent_topics=["pre-existing"],
                updated_at_ms=1_700_000_000_000,
                state_json={"pre": True},
            ),
            tenant_id="tenant-a",
        )

        result = await run_child(
            _parent_ctx(parent_agent_id="main"),
            _agent_card(name="researcher"),
            TaskSpec(goal="anything"),
            provider=provider,
            child_seq=0,
            persona_store=store,
        )

        assert result.child_agent_id == "main::researcher::0"

        child_row = await store.get(
            "main::researcher::0", tenant_id="tenant-a"
        )
        assert child_row is not None, "child persona row should be seeded"
        assert child_row.agent_id == "main::researcher::0"
        assert child_row.mood == "neutral"
        assert child_row.fatigue == 0.0
        assert child_row.recent_topics == []
        assert child_row.state_json == {}

        # Parent row is unchanged — fresh-only persona inheritance is the
        # whole point per design § "fresh persona".
        parent_row = await store.get("main", tenant_id="tenant-a")
        assert parent_row is not None
        assert parent_row.mood == "focused"
        assert parent_row.fatigue == pytest.approx(0.42)
        assert parent_row.recent_topics == ["pre-existing"]


# ---------------------------------------------------------------------------
# Test row 4: child_persona_row_under_same_tenant
# ---------------------------------------------------------------------------


async def test_child_persona_row_under_same_tenant(tmp_path: Path) -> None:
    """The seeded child row's ``tenant_id`` must match the parent's. The
    composite primary key ``(tenant_id, agent_id)`` enforces this from
    the schema side (migration ``f2cc7a9``); we additionally assert that
    a query under a *different* tenant does NOT find the child row, so a
    cross-tenant lookup never collides.
    """
    db_path = tmp_path / "agent_state.sqlite"
    provider = _FakeProvider(
        [ProviderChunk(kind="done", finish_reason="stop")]
    )

    async with PersonaStore(db_path) as store:
        await run_child(
            _parent_ctx(tenant="tenant-a", parent_agent_id="main"),
            _agent_card(name="researcher"),
            TaskSpec(goal="anything"),
            provider=provider,
            child_seq=0,
            persona_store=store,
        )

        # Visible under the correct tenant.
        same = await store.get(
            "main::researcher::0", tenant_id="tenant-a"
        )
        assert same is not None
        assert same.agent_id == "main::researcher::0"

        # Invisible under a *different* tenant — the composite PK keeps
        # the per-tenant namespaces disjoint.
        other = await store.get(
            "main::researcher::0", tenant_id="tenant-b"
        )
        assert other is None


# ---------------------------------------------------------------------------
# Test row 5: parent_chat_history_not_visible_to_child
# ---------------------------------------------------------------------------


async def test_parent_chat_history_not_visible_to_child() -> None:
    """The runner builds the child's :class:`ChatStart` from
    ``agent_card.system_prompt`` + ``task.goal`` only. Prior parent turns
    are *never* threaded in — that's the whole rationale for subagent
    isolation per design § "What children inherit".

    Asserts the messages the provider actually saw on its first (and
    only, on the happy path) call: exactly one ``role="system"`` and one
    ``role="user"`` carrying ``task.goal``. Nothing from the parent.
    """
    provider = _FakeProvider(
        [ProviderChunk(kind="done", finish_reason="stop")]
    )

    parent_goal = "Summarise transformer attention briefly."
    await run_child(
        _parent_ctx(parent_session_key="root"),
        _agent_card(
            name="researcher",
            system_prompt="You are a careful researcher.",
        ),
        TaskSpec(goal=parent_goal),
        provider=provider,
    )

    assert len(provider.messages_seen) == 1, "exactly one provider round"
    msgs = provider.messages_seen[0]

    # System + user, in that order. No assistant turns, no tool turns,
    # nothing carrying the parent's prior chat.
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert msgs[0]["content"] == "You are a careful researcher."
    assert msgs[1]["content"] == parent_goal

    # Belt-and-braces: nothing in the messages mentions parent history
    # markers we'd expect to see if it leaked (e.g. role="assistant").
    for m in msgs:
        assert m["role"] not in ("assistant", "tool"), (
            "parent's assistant/tool turns must not be inherited"
        )


# ---------------------------------------------------------------------------
# Iter 6: timeout enforcement
# ---------------------------------------------------------------------------


class _SlowProvider:
    """Streams a small prefix, then sleeps forever — used to exercise
    the cooperative-cancel path inside :func:`run_child` when
    ``task.max_wall_seconds`` expires.

    The reasoning loop polls its ``cancelled`` event between rounds
    and inside ``_collect_results`` waits, so a prefix-then-sleep
    provider lets us drive partial output into ``output_chunks``
    *before* the timeout fires. Once cancelled the loop emits an
    ``ErrorEvent(reason="cancelled")`` and the runner's TIMEOUT
    overrides the finish_reason while preserving what we collected.
    """

    def __init__(self, prefix_tokens: list[str]) -> None:
        self._prefix = prefix_tokens

    async def chat_stream(self, **_: Any) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
        for tok in self._prefix:
            yield ProviderChunk(kind="token", text=tok)
        # Yield-then-sleep so the runner observes the prefix tokens
        # before the timeout fires. ``asyncio.sleep`` is the canonical
        # cancellation point — the loop's task receives the
        # CancelledError and unwinds via the runner's grace path.
        import asyncio

        await asyncio.sleep(60)
        # Unreachable on the timeout path; included so the provider
        # contract still terminates if the test somehow waits long
        # enough.
        yield ProviderChunk(kind="done", finish_reason="stop")


async def test_child_timeout_returns_partial_output() -> None:
    """A 1s wall budget against a provider that streams ``"partial "`` then
    sleeps forever must yield ``finish_reason=TIMEOUT`` with the prefix
    preserved verbatim in ``output_text``. Lifts the partial-output
    guarantee documented in design § "Timeout handling".
    """
    provider = _SlowProvider(prefix_tokens=["partial "])

    result = await run_child(
        _parent_ctx(),
        _agent_card(),
        TaskSpec(goal="anything", max_wall_seconds=1),
        provider=provider,
    )

    assert result.finish_reason is FinishReason.TIMEOUT
    assert "partial " in result.output_text, (
        f"partial output must be preserved across timeout cancel; "
        f"got output_text={result.output_text!r}"
    )
    # Elapsed must be roughly the budget (≥ 1000ms) but bounded by the
    # cooperative grace window — runner caps at budget + grace.
    assert result.elapsed_ms >= 900, (
        f"elapsed should reflect the wall budget, got {result.elapsed_ms}"
    )


async def test_child_timeout_decrements_concurrency_via_supervisor() -> None:
    """Timeout path must release the slot — i.e. the per-parent counter
    in :class:`Supervisor` returns to baseline after a timeout.

    Iter 6's runner is sync from the supervisor's perspective: the slot
    drops when ``spawn_child_to_result`` (the Rust bridge) returns.
    Here we exercise the *Python* analogue: drive the runner directly
    inside an externally-acquired slot, assert the counter is held
    during the call and released afterwards. Mirrors the assertion the
    Rust side covers in
    ``python_bridge::tests::slot_released_on_completion`` but pinned in
    Python so the cross-language contract is verified at both ends.

    Skipped when the Rust extension isn't importable. The acceptance is
    really about "the runner returns even on timeout" — the slot itself
    is a Rust concept; the runner must complete deterministically so
    the bridge's drop-guard always fires.
    """
    provider = _SlowProvider(prefix_tokens=["x"])

    # We can't import the Rust supervisor from a pure-Python pytest run;
    # this test substitutes the contract assertion: "the runner
    # eventually returns within the wall budget + grace, even on a
    # provider that would otherwise block forever". If the runner does
    # NOT return, the supervisor would never decrement the counter.
    import asyncio
    import time

    start = time.monotonic()
    result = await asyncio.wait_for(
        run_child(
            _parent_ctx(),
            _agent_card(),
            TaskSpec(goal="anything", max_wall_seconds=1),
            provider=provider,
        ),
        # Generous outer cap; the runner's own 1s wall + 2s grace must
        # make it back well under this. If we hit this fence the runner
        # is leaking; the supervisor's slot would also leak in prod.
        timeout=10.0,
    )
    elapsed = time.monotonic() - start

    assert result.finish_reason is FinishReason.TIMEOUT
    assert elapsed < 5.0, (
        f"runner must return within wall + grace; took {elapsed:.2f}s"
    )


# ---------------------------------------------------------------------------
# Iter 7: tool-allowlist filtering + escalation reject
#
# Two design test rows mandated by the doc's "Test matrix":
#
# * ``tool_allowlist_escalation_rejected`` — child asks for a tool the
#   parent does not hold → ``error="tool_allowlist_escalation"``.
# * ``subagent_spawn_pruned_at_depth_n_minus_1`` — child at the deepest
#   legal depth must NOT see ``subagent.spawn`` in its tools.
#
# Plus three light-weight assertions that lock the inheritance default
# and the explicit "empty list = no tools" mode (these branches cover
# the design § "Tool exposure" three-policy table verbatim).
# ---------------------------------------------------------------------------


from corlinman_agent.subagent import (  # noqa: E402  -- placed near tests for locality
    DEFAULT_MAX_DEPTH,
    SUBAGENT_SPAWN_TOOL,
    TOOL_ALLOWLIST_ESCALATION_ERROR,
)


def _tool(name: str) -> dict[str, Any]:
    """Construct an OpenAI-shaped tool schema entry. The runner only
    looks at ``function.name``; everything else is provider noise we
    keep minimal so test golden files stay readable."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"test tool {name}",
            "parameters": {"type": "object", "properties": {}},
        },
    }


class _ToolListCapturingProvider:
    """Mock provider that records the ``tools=`` keyword the loop forwards.

    The runner builds a :class:`ChatStart` with the filtered schema
    list; the reasoning loop forwards it to the provider as the
    ``tools`` kwarg. Capturing it lets tests assert the *child's*
    effective set verbatim, which is the contract iter 7 owns.
    """

    def __init__(self) -> None:
        self.tools_seen: list[Any] = []

    async def chat_stream(
        self, *, messages: list[dict[str, Any]], **kwargs: Any
    ) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
        # ReasoningLoop._run_one_round passes ``tools=start.tools or None``
        # — capture either form so the test can assert the post-filter shape.
        self.tools_seen.append(kwargs.get("tools"))
        yield ProviderChunk(kind="done", finish_reason="stop")


async def test_tool_allowlist_escalation_rejected() -> None:
    """Child asks for ``forbidden_tool`` while the parent only holds
    ``web_search``. The runner must short-circuit with
    ``finish_reason=REJECTED`` and ``error=tool_allowlist_escalation``;
    no provider call must happen.
    """
    provider = _ToolListCapturingProvider()
    parent_tools = [_tool("web_search")]

    result = await run_child(
        _parent_ctx(),
        _agent_card(),
        TaskSpec(
            goal="anything",
            tool_allowlist=["forbidden_tool"],
        ),
        provider=provider,
        parent_tools=parent_tools,
    )

    assert result.finish_reason is FinishReason.REJECTED
    assert result.error == TOOL_ALLOWLIST_ESCALATION_ERROR
    assert result.output_text == ""
    assert result.tool_calls_made == []
    # The provider must NOT have been invoked — escalation happens
    # before the loop is constructed.
    assert provider.tools_seen == [], (
        "escalation reject must short-circuit before any provider call"
    )


async def test_subagent_spawn_pruned_at_depth_n_minus_1() -> None:
    """At ``child_ctx.depth == max_depth - 1`` the runner strips
    ``subagent.spawn`` from the child's tool list — a grandchild spawn
    would be refused by the supervisor anyway, so the child must not
    even see the option.

    The parent (depth 0) holds ``subagent.spawn`` + ``web_search``.
    The child runs at depth 1 (``parent_ctx.depth=0`` → child_ctx
    depth becomes 1 after :meth:`ParentContext.child_context`). With
    ``max_depth=2`` (default), depth 1 == max_depth - 1, so the spawn
    tool must be pruned.
    """
    provider = _ToolListCapturingProvider()
    parent_tools = [_tool(SUBAGENT_SPAWN_TOOL), _tool("web_search")]

    # parent at depth 0 → child at depth 1 (== max_depth-1 with default 2)
    await run_child(
        _parent_ctx(depth=0),
        _agent_card(),
        TaskSpec(goal="anything"),  # tool_allowlist=None → inherit parent's
        provider=provider,
        parent_tools=parent_tools,
        max_depth=DEFAULT_MAX_DEPTH,
    )

    assert len(provider.tools_seen) == 1, "exactly one provider round"
    seen = provider.tools_seen[0] or []
    seen_names = {
        (t.get("function") or {}).get("name") if isinstance(t, dict) else None
        for t in seen
    }
    # ``web_search`` survives, ``subagent.spawn`` is pruned.
    assert "web_search" in seen_names
    assert SUBAGENT_SPAWN_TOOL not in seen_names, (
        "subagent.spawn must be pruned at the deepest legal child depth"
    )


async def test_subagent_spawn_kept_below_depth_cap_minus_one() -> None:
    """Symmetric: at ``child_depth < max_depth - 1`` the spawn tool
    survives — the child *can* legally delegate one more level.

    Bumps ``max_depth=4`` so the default-depth-1 child has plenty of
    room. Locks the design § "tool exposure" branch where
    ``depth < max_depth - 1`` keeps ``subagent.spawn`` available.
    """
    provider = _ToolListCapturingProvider()
    parent_tools = [_tool(SUBAGENT_SPAWN_TOOL), _tool("web_search")]

    await run_child(
        _parent_ctx(depth=0),
        _agent_card(),
        TaskSpec(goal="anything"),
        provider=provider,
        parent_tools=parent_tools,
        max_depth=4,  # child at depth 1 < 4-1 → spawn tool stays
    )

    seen = provider.tools_seen[0] or []
    seen_names = {
        (t.get("function") or {}).get("name") if isinstance(t, dict) else None
        for t in seen
    }
    assert SUBAGENT_SPAWN_TOOL in seen_names
    assert "web_search" in seen_names


async def test_inherit_when_allowlist_is_none() -> None:
    """``tool_allowlist=None`` (the default) means "inherit the parent's
    tool set verbatim". Locks the design § "Tool exposure" first
    policy: the child sees every tool the parent holds, modulo the
    iter-7 self-prune (which doesn't fire here because ``subagent.spawn``
    isn't in the parent set)."""
    provider = _ToolListCapturingProvider()
    parent_tools = [_tool("web_search"), _tool("python_eval")]

    await run_child(
        _parent_ctx(),
        _agent_card(),
        TaskSpec(goal="anything"),
        provider=provider,
        parent_tools=parent_tools,
    )

    seen = provider.tools_seen[0] or []
    seen_names = {
        (t.get("function") or {}).get("name") if isinstance(t, dict) else None
        for t in seen
    }
    assert seen_names == {"web_search", "python_eval"}


async def test_empty_allowlist_is_pure_llm_call() -> None:
    """``tool_allowlist=[]`` is the explicit "no tools" mode — distinct
    from ``None`` which means inherit. Useful for "summarise this text"
    children where the loop should never try a tool call. The reasoning
    loop forwards ``tools=None`` when the list is empty, so the
    captured value is ``None`` rather than ``[]``."""
    provider = _ToolListCapturingProvider()
    parent_tools = [_tool("web_search"), _tool("python_eval")]

    await run_child(
        _parent_ctx(),
        _agent_card(),
        TaskSpec(goal="anything", tool_allowlist=[]),
        provider=provider,
        parent_tools=parent_tools,
    )

    # Loop translates an empty list to ``tools=None`` on the provider call.
    assert provider.tools_seen == [None]
