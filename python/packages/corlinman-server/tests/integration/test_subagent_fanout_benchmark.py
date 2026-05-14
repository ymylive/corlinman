"""Phase 4 W4 D3 iter 10 — research-fan-out E2E benchmark.

This is the **D3 acceptance benchmark** for Wave 4 (per
``docs/design/phase4-w4-d3-design.md`` § "Implementation order" iter 10
and ``phase4-roadmap.md:309,429``):

    Research-fan-out scenario ("research X, summarize, draft 3 angles")
    with 3 children each on a sub-topic. Measure wall-clock vs serial
    baseline. Acceptance: ``< 0.7 *`` serial.

The shape locked in by the design (§ "Why this exists"):

* "Research-and-summarize fan-out" is 3 independent sub-tasks the
  parent fans out, then synthesises. The W4 win is that wall-clock
  collapses from ``Σ child_durations`` (serial inside one loop) to
  ``max(child_durations) + overhead`` (parallel via subagents).

* The benchmark proves the contract end-to-end: an LLM-shaped tool
  call (``ToolCallEvent("subagent.spawn", args_json)``) goes through
  :func:`dispatch_subagent_spawn`, the dispatcher acquires a
  supervisor slot, the runner drives a fresh
  :class:`ReasoningLoop` for each child, and every child returns
  its :class:`TaskResult` envelope. Three of these run concurrently
  via ``asyncio.gather``; the parent receives three independent
  ``ToolResult.content`` strings and would synthesise them in a
  follow-up provider round.

**No real LLM**: per the iter-10 hard constraint, the children use a
``_SleepingMockProvider`` that yields token chunks **after a fixed
``asyncio.sleep`` window**. The sleep is the deterministic stand-in
for "child does some non-trivial provider work". Wall-clock
measurement compares:

* **Serial baseline** — drive the three children one after another via
  ``await`` chain → total ≈ ``Σ sleep_seconds`` plus per-call overhead.
* **Parallel** — drive them via ``asyncio.gather`` → total ≈ ``max
  sleep_seconds`` plus dispatch overhead.

The acceptance check is ``parallel_ms < 0.7 * serial_ms``. With three
children at ~150ms each the math is forgiving: serial ≈ 450ms,
parallel ≈ 150ms → ratio ≈ 0.33, well below the 0.7 threshold.

The supervisor slot accountant (the iter 5+ Rust crate) is not in
this test's path: ``dispatch_subagent_spawn`` is called with
``supervisor_acquire=None``, the documented test-mode that runs
without slot enforcement. The benchmark is about wall-clock parity
of the **runner concurrency**; cap accounting is exercised in the
unit tests under ``corlinman-subagent`` (iter 3) and
``test_subagent_tool_wrapper`` (iter 8).

Lives in ``python/packages/corlinman-server/tests/integration/`` per
the design doc's iter-10 destination directive — keeping it next to
the gateway/servicer integration suite signals "this is the wave
acceptance benchmark", not a unit test.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from corlinman_agent.agents.card import AgentCard
from corlinman_agent.agents.registry import AgentCardRegistry
from corlinman_agent.subagent import (
    FinishReason,
    ParentContext,
    dispatch_subagent_spawn,
)
from corlinman_providers.base import ProviderChunk

# ---------------------------------------------------------------------------
# Benchmark knobs
# ---------------------------------------------------------------------------

#: Per-child sleep window in seconds. Chosen to be (a) large enough that
#: dispatch overhead is a small fraction of the measurement (so the
#: serial-vs-parallel ratio is dominated by the sleep) and (b) small
#: enough that the test still completes in well under one second per
#: leg. ~150ms per child x 3 = ~450ms serial; concurrent ≈ 150ms.
_CHILD_SLEEP_SECONDS: float = 0.15

#: Number of children spawned in parallel. Three matches the canonical
#: research-fan-out scenario in the design doc and the per-parent
#: concurrency cap default (``[subagent].max_concurrent_per_parent =
#: 3``). The supervisor would reject a fourth concurrent spawn; we test
#: at the cap, not above it.
_FANOUT_DEGREE: int = 3

#: Acceptance ratio from ``phase4-roadmap.md:309,429``. Parallel
#: wall-clock must be strictly less than 0.7 x the serial wall-clock.
_ACCEPTANCE_RATIO: float = 0.7

#: Per-child sub-topics. Match the design's "research X, summarize,
#: draft 3 angles" — three distinct child goals so each is plausibly an
#: independent sub-task.
_CHILD_GOALS: list[tuple[str, str]] = [
    ("researcher_a", "research transformer architectures"),
    ("researcher_b", "research diffusion models"),
    ("researcher_c", "research mixture-of-experts"),
]


# ---------------------------------------------------------------------------
# Mock provider
# ---------------------------------------------------------------------------


class _SleepingMockProvider:
    """Deterministic stand-in for an LLM provider that takes time.

    On ``chat_stream`` the provider does exactly three things:

    1. ``await asyncio.sleep(sleep_seconds)`` — the deterministic delay
       that lets the wall-clock comparison have signal.
    2. ``yield ProviderChunk(kind="token", text=output)`` — so the
       runner has a ``output_text`` to surface in :class:`TaskResult`.
    3. ``yield ProviderChunk(kind="done", finish_reason="stop")`` — so
       the runner exits with :attr:`FinishReason.STOP`.

    The sleep is *not* sliced into multiple awaits because the goal is
    to make each child take a known amount of wall-clock time, not to
    exercise streaming-token cancellation (that's iter 6's
    ``test_child_timeout_returns_partial_output``).
    """

    def __init__(self, *, sleep_seconds: float, output: str) -> None:
        self._sleep_seconds = sleep_seconds
        self._output = output
        # Counter so the test can assert each provider was actually
        # invoked (vs the dispatcher short-circuiting somewhere).
        self.call_count = 0

    async def chat_stream(
        self, **_: Any
    ) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
        self.call_count += 1
        await asyncio.sleep(self._sleep_seconds)
        yield ProviderChunk(kind="token", text=self._output)
        yield ProviderChunk(kind="done", finish_reason="stop")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_registry() -> AgentCardRegistry:
    """Construct an in-memory :class:`AgentCardRegistry` carrying one
    card per sub-topic. Bypasses the YAML loader because the test
    benchmarks the runtime, not the registry's parsing path."""
    cards = {
        name: AgentCard(
            name=name,
            description="benchmark child",
            system_prompt=f"You are {name}; answer concisely.",
        )
        for name, _goal in _CHILD_GOALS
    }
    return AgentCardRegistry(cards)


def _parent_ctx() -> ParentContext:
    """Top-level parent context — depth=0 so every child runs at depth
    1, well below ``DEFAULT_MAX_DEPTH=2``. The supervisor's depth cap
    therefore never fires for this benchmark; the iter-10 contract is
    about wall-clock parity, not cap accounting."""
    return ParentContext(
        tenant_id="bench-tenant",
        parent_agent_id="bench-parent",
        parent_session_key="bench-root",
        depth=0,
        trace_id="bench-trace",
    )


def _spawn_args(agent_name: str, goal: str) -> bytes:
    """Build the JSON args payload the LLM would emit for one
    ``subagent.spawn`` tool call. Matches the design's wire envelope:
    ``{"agent": "<card name>", "goal": "<sub-task prompt>"}``."""
    return json.dumps({"agent": agent_name, "goal": goal}).encode("utf-8")


async def _dispatch_one(
    *,
    parent_ctx: ParentContext,
    registry: AgentCardRegistry,
    provider: _SleepingMockProvider,
    agent_name: str,
    goal: str,
    child_seq: int,
) -> dict[str, Any]:
    """Drive one ``subagent.spawn`` round and decode its result envelope.

    Returns the parsed :class:`TaskResult`-shaped dict so callers can
    assert on the wire shape. The dispatcher returns a JSON string
    that the parent's loop would feed into ``ToolResult.content`` — we
    decode here so the test reads as "did this child produce the
    expected envelope".
    """
    content = await dispatch_subagent_spawn(
        args_json=_spawn_args(agent_name, goal),
        parent_ctx=parent_ctx,
        agent_registry=registry,
        provider=provider,
        # supervisor_acquire=None: documented test-mode (no slot
        # enforcement). The dispatcher's iter-8 contract carries the
        # cap behaviour through the wire envelope, exercised in
        # test_subagent_tool_wrapper.py — not the focus here.
        supervisor_acquire=None,
        child_seq=child_seq,
    )
    return json.loads(content)


# ---------------------------------------------------------------------------
# Benchmark — the wave-4 acceptance test
# ---------------------------------------------------------------------------


async def test_e2e_research_fanout_beats_serial_walltime() -> None:
    """Wave-4 acceptance: 3-way fan-out via ``subagent.spawn`` must
    beat the serial baseline by at least ``1 - 0.7 = 30%``.

    Design row: ``e2e_research_fanout_beats_serial_walltime`` in the
    design doc's test matrix (line 316). Roadmap pin:
    ``phase4-roadmap.md:309,429``.

    Method:
      1. Build three sleep-based mock providers, one per child.
      2. Drive them serially via ``await`` chain → ``serial_ms``.
      3. Build a fresh trio (so per-child counters reset) and drive
         them concurrently via ``asyncio.gather`` → ``parallel_ms``.
      4. Assert ``parallel_ms < 0.7 * serial_ms``.

    The two passes hit the *same* ``dispatch_subagent_spawn`` code
    path; the only difference is whether each call awaits the previous
    one (serial) or runs alongside the others (parallel). That's
    exactly the contract the design asserts under "Why this exists" —
    the wall-clock cut comes from the runner's concurrency, not from
    any per-child speedup.
    """
    registry = _build_registry()
    parent_ctx = _parent_ctx()

    # ── 1. Serial baseline. ─────────────────────────────────────────
    # Fresh providers per pass: the call_count assertion below would
    # otherwise muddy the diagnostic ("which pass invoked it?").
    serial_providers = [
        _SleepingMockProvider(
            sleep_seconds=_CHILD_SLEEP_SECONDS,
            output=f"summary-{name}",
        )
        for name, _goal in _CHILD_GOALS
    ]

    serial_started = time.perf_counter()
    serial_results: list[dict[str, Any]] = []
    for idx, ((agent_name, goal), provider) in enumerate(
        zip(_CHILD_GOALS, serial_providers, strict=True)
    ):
        # Sequential: each await blocks the next dispatch. Wall-clock
        # ≈ Σ child_sleep + dispatch overhead.
        envelope = await _dispatch_one(
            parent_ctx=parent_ctx,
            registry=registry,
            provider=provider,
            agent_name=agent_name,
            goal=goal,
            child_seq=idx,
        )
        serial_results.append(envelope)
    serial_ms = int((time.perf_counter() - serial_started) * 1000)

    # ── 2. Parallel pass. ───────────────────────────────────────────
    parallel_providers = [
        _SleepingMockProvider(
            sleep_seconds=_CHILD_SLEEP_SECONDS,
            output=f"summary-{name}",
        )
        for name, _goal in _CHILD_GOALS
    ]

    parallel_started = time.perf_counter()
    parallel_results = await asyncio.gather(
        *[
            _dispatch_one(
                parent_ctx=parent_ctx,
                registry=registry,
                provider=provider,
                agent_name=agent_name,
                goal=goal,
                # child_seq=idx ensures the child_session_key /
                # child_agent_id formatting matches the dispatcher's
                # ``::child::<seq>`` convention without sibling
                # collisions.
                child_seq=idx,
            )
            for idx, ((agent_name, goal), provider) in enumerate(
                zip(_CHILD_GOALS, parallel_providers, strict=True)
            )
        ]
    )
    parallel_ms = int((time.perf_counter() - parallel_started) * 1000)

    # ── 3. Sanity-check the wire envelopes. ─────────────────────────
    # Both passes must have produced ``finish_reason=stop`` results
    # with the per-child output the mock provider streamed. If the
    # dispatcher silently failed (e.g. agent_not_found) we'd see
    # ``rejected`` here and the wall-clock numbers would be
    # meaningless.
    for envelope in (*serial_results, *parallel_results):
        assert envelope["finish_reason"] == FinishReason.STOP.value, (
            f"non-stop result in benchmark — wall-clock numbers are "
            f"meaningless if any child rejected. envelope={envelope!r}"
        )
        assert envelope["output_text"].startswith("summary-"), (
            f"missing/garbled output_text — provider didn't stream "
            f"its token? envelope={envelope!r}"
        )

    # Each provider was invoked exactly once across its pass — proves
    # the dispatcher hit the runner once per child rather than
    # short-circuiting somewhere.
    for prov in serial_providers + parallel_providers:
        assert prov.call_count == 1, (
            f"each provider must be invoked exactly once "
            f"(got {prov.call_count})"
        )

    # ── 4. The acceptance check. ────────────────────────────────────
    threshold_ms = _ACCEPTANCE_RATIO * serial_ms
    assert parallel_ms < threshold_ms, (
        f"D3 acceptance failed — parallel fan-out did not beat the "
        f"<{_ACCEPTANCE_RATIO:.2f}*serial threshold. "
        f"serial_ms={serial_ms}, parallel_ms={parallel_ms}, "
        f"threshold_ms={threshold_ms:.0f}, "
        f"observed_ratio={parallel_ms / serial_ms:.3f}"
    )

    # Also assert the parallel wall-clock is in the right order of
    # magnitude — within 3x the per-child sleep. Catches a future
    # regression where dispatch overhead balloons (e.g. an accidental
    # synchronous file IO on every spawn) without it actually
    # serialising the children.
    expected_parallel_ceiling_ms = int(_CHILD_SLEEP_SECONDS * 1000 * 3)
    assert parallel_ms <= expected_parallel_ceiling_ms, (
        f"parallel pass took {parallel_ms}ms — far above the per-child "
        f"sleep ceiling {expected_parallel_ceiling_ms}ms; dispatch "
        f"overhead may have regressed."
    )


async def test_fanout_envelopes_carry_distinct_child_ids() -> None:
    """Companion structural test to the wall-clock benchmark: even
    under ``asyncio.gather`` concurrency, every child's
    :attr:`TaskResult.child_session_key` and ``child_agent_id`` are
    distinct — the ``::child::<seq>`` convention from
    :meth:`ParentContext.child_context` does not collide across
    siblings dispatched at the same instant.

    This is the structural twin of
    ``parallel_siblings_complete_independently`` from the design's
    test matrix: the runner crate's iter-3 supervisor unit tests
    exercise the cap counter side; this test verifies the wire-level
    identity story end-to-end through the dispatcher.
    """
    registry = _build_registry()
    parent_ctx = _parent_ctx()
    providers = [
        _SleepingMockProvider(
            sleep_seconds=_CHILD_SLEEP_SECONDS,
            output=f"summary-{name}",
        )
        for name, _goal in _CHILD_GOALS
    ]

    envelopes = await asyncio.gather(
        *[
            _dispatch_one(
                parent_ctx=parent_ctx,
                registry=registry,
                provider=provider,
                agent_name=agent_name,
                goal=goal,
                child_seq=idx,
            )
            for idx, ((agent_name, goal), provider) in enumerate(
                zip(_CHILD_GOALS, providers, strict=True)
            )
        ]
    )

    session_keys = {e["child_session_key"] for e in envelopes}
    agent_ids = {e["child_agent_id"] for e in envelopes}

    assert len(session_keys) == _FANOUT_DEGREE, (
        f"siblings collided on child_session_key under concurrent "
        f"dispatch: {sorted(session_keys)!r}"
    )
    assert len(agent_ids) == _FANOUT_DEGREE, (
        f"siblings collided on child_agent_id under concurrent "
        f"dispatch: {sorted(agent_ids)!r}"
    )

    # Each session key follows the ``<parent>::child::<seq>`` shape
    # documented in :meth:`ParentContext.child_context`. Locking the
    # format here means the operator UI's tree-collapse logic (Open
    # Question 3 in the design) has a stable string to split on.
    for envelope in envelopes:
        assert envelope["child_session_key"].startswith(
            f"{parent_ctx.parent_session_key}::child::"
        ), (
            f"child_session_key does not follow ``<parent>::child::<seq>``: "
            f"{envelope['child_session_key']!r}"
        )
