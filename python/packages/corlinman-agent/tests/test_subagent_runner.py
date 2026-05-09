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
