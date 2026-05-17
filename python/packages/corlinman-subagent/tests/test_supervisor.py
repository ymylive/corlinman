"""Port of the Rust ``supervisor::tests`` module — cap accountant
behaviour, slot drop-guard, and hook-bus emit shape.

Where the Rust crate uses ``DashMap`` per-key entries we hold a single
:class:`asyncio.Lock`-guarded dict, but the public contract is
identical: depth → per-parent → per-tenant, counter rollback on
later-stage rejection, idempotent release, and best-effort hook emits.

Async tests live alongside the sync ones because :meth:`Supervisor.
spawn_child` is async (it wraps the agent callable under
:func:`asyncio.wait_for`); the underlying :meth:`try_acquire` is sync
and the hook emits are non-blocking.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from corlinman_hooks import HookBus, HookEvent, HookPriority
from corlinman_subagent import (
    AcquireReject,
    AcquireRejectError,
    AgentCallable,
    FinishReason,
    ParentContext,
    Slot,
    SubagentError,
    Supervisor,
    SupervisorPolicy,
    TaskResult,
    TaskSpec,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_parent(tenant: str, session: str, depth: int = 0) -> ParentContext:
    """Mirror of the Rust ``parent_ctx`` test helper."""
    return ParentContext(
        tenant_id=tenant,
        parent_agent_id=f"agent-of-{session}",
        parent_session_key=session,
        depth=depth,
        trace_id=f"trace-of-{session}",
    )


async def drain_events(sub: Any, *, deadline_s: float = 0.1) -> list[Any]:
    """Drain everything currently buffered on a :class:`HookSubscription`.

    Bounded loop with a tiny deadline so a test that never enqueues an
    event doesn't hang. Mirrors the Rust ``drain_events`` helper.
    """
    out: list[Any] = []
    loop = asyncio.get_event_loop()
    deadline = loop.time() + deadline_s
    while True:
        timeout = deadline - loop.time()
        if timeout <= 0:
            return out
        try:
            event = await asyncio.wait_for(sub.recv(), timeout=timeout)
        except (asyncio.TimeoutError, Exception):
            return out
        out.append(event)


# ---------------------------------------------------------------------------
# Cap accountant — direct Rust ports.
# ---------------------------------------------------------------------------


def test_policy_defaults_match_design() -> None:
    """Maps to ``policy_defaults_match_design`` in Rust."""
    p = SupervisorPolicy()
    assert p.max_concurrent_per_parent == 3
    assert p.max_concurrent_per_tenant == 15
    assert p.max_depth == 2
    # Python-side addition: the wall-clock ceiling lives on the policy
    # struct (Rust enforced this in a separate config layer).
    assert p.max_wall_seconds_ceiling == 60


def test_concurrency_cap_rejects_fourth_when_three_in_flight() -> None:
    """Maps to ``concurrency_cap_rejects_fourth_when_three_in_flight``."""
    sup = Supervisor(SupervisorPolicy())  # 3 per parent
    ctx = make_parent("t1", "session-A")

    s1 = sup.try_acquire(ctx)
    s2 = sup.try_acquire(ctx)
    s3 = sup.try_acquire(ctx)
    assert sup.parent_count("session-A") == 3

    # Fourth refused with the per-parent reason.
    with pytest.raises(AcquireRejectError) as ei:
        sup.try_acquire(ctx)
    assert ei.value.reason is AcquireReject.PARENT_CONCURRENCY_EXCEEDED
    assert sup.parent_count("session-A") == 3, "rejected acquire must not increment"

    # Releasing one frees the slot for another.
    s1.release()
    assert sup.parent_count("session-A") == 2
    s4 = sup.try_acquire(ctx)
    assert sup.parent_count("session-A") == 3

    s2.release()
    s3.release()
    s4.release()
    assert sup.parent_count("session-A") == 0
    assert sup.tenant_count("t1") == 0, "tenant counter must follow"


def test_tenant_quota_caps_across_parents() -> None:
    """Maps to ``tenant_quota_caps_across_parents`` in Rust."""
    policy = SupervisorPolicy(
        max_concurrent_per_parent=100,  # disable per-parent for this test
        max_concurrent_per_tenant=4,
        max_depth=2,
    )
    sup = Supervisor(policy)

    # Spread across 4 parent sessions, all under tenant `shared`.
    held: list[Slot] = []
    for i in range(4):
        ctx = make_parent("shared", f"sess-{i}")
        held.append(sup.try_acquire(ctx))
    assert sup.tenant_count("shared") == 4

    # 5th refused with TenantQuotaExceeded — even though it's a
    # brand-new parent session.
    ctx_new = make_parent("shared", "sess-new")
    with pytest.raises(AcquireRejectError) as ei:
        sup.try_acquire(ctx_new)
    assert ei.value.reason is AcquireReject.TENANT_QUOTA_EXCEEDED
    assert sup.tenant_count("shared") == 4, "tenant counter unchanged on rejection"
    assert sup.parent_count("sess-new") == 0, "per-parent must roll back when tenant rejects"

    # A different tenant is unaffected. Bind the slot so the GC
    # doesn't release it before we assert on the counter (Python's
    # ``Slot.__del__`` finaliser would otherwise drop the count back
    # to zero immediately after the expression).
    ctx_other = make_parent("isolated", "sess-x")
    other_slot = sup.try_acquire(ctx_other)
    assert sup.tenant_count("isolated") == 1
    # Pin held slots so the GC doesn't release them mid-test.
    assert len(held) == 4
    assert other_slot.released is False


def test_depth_cap_blocks_grandchild_at_depth_2() -> None:
    """Maps to ``depth_cap_blocks_grandchild_at_depth_2`` in Rust."""
    sup = Supervisor(SupervisorPolicy())  # max_depth=2

    # depth 0 (top-level user turn): allowed.
    ctx0 = make_parent("t", "s", 0)
    _s0 = sup.try_acquire(ctx0)

    # depth 1 (child wants to spawn grandchild): allowed.
    ctx1 = make_parent("t", "s::child::0", 1)
    _s1 = sup.try_acquire(ctx1)

    # depth 2 (grandchild wants to spawn): refused — that would be a
    # great-grandchild beyond the cap.
    ctx2 = make_parent("t", "s::child::0::child::0", 2)
    with pytest.raises(AcquireRejectError) as ei:
        sup.try_acquire(ctx2)
    assert ei.value.reason is AcquireReject.DEPTH_CAPPED

    # No counters should have been incremented for the rejected spawn.
    assert sup.parent_count("s::child::0::child::0") == 0
    # Pin held slots so they're not gc'd before the assertion.
    assert _s0.released is False
    assert _s1.released is False


def test_explicit_release_is_idempotent_and_decrements_once() -> None:
    """Maps to ``explicit_release_is_idempotent_and_decrements_once``."""
    sup = Supervisor(SupervisorPolicy())
    ctx = make_parent("t", "s")

    slot = sup.try_acquire(ctx)
    assert sup.parent_count("s") == 1
    slot.release()
    assert sup.parent_count("s") == 0
    # Double-release must be a no-op.
    slot.release()
    assert sup.parent_count("s") == 0
    # Second slot in the same key works (no double-decrement leaked).
    _slot2 = sup.try_acquire(ctx)
    assert sup.parent_count("s") == 1


def test_slot_context_manager_releases_on_exit() -> None:
    """Python-flavoured addition: the ``with`` form is the idiomatic way
    to scope a slot, and exiting the block must release the slot the
    same way Rust's ``Drop`` does on scope-end.
    """
    sup = Supervisor(SupervisorPolicy())
    ctx = make_parent("t", "s")
    with sup.try_acquire(ctx) as slot:
        assert sup.parent_count("s") == 1
        assert slot.released is False
    assert sup.parent_count("s") == 0


def test_slot_releases_on_exception_inside_with() -> None:
    """Belt-and-braces: an exception raised inside the ``with`` body
    must still release the slot. Mirrors the Rust ``Drop``-on-unwind
    guarantee.
    """
    sup = Supervisor(SupervisorPolicy())
    ctx = make_parent("t", "s")
    with pytest.raises(RuntimeError):
        with sup.try_acquire(ctx):
            assert sup.parent_count("s") == 1
            raise RuntimeError("boom")
    assert sup.parent_count("s") == 0


# ---------------------------------------------------------------------------
# Hook-bus emit shape — direct Rust ports + Python-flavoured async glue.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_spawned_carries_parent_trace_id() -> None:
    """Maps to ``emit_spawned_carries_parent_trace_id`` in Rust."""
    bus = HookBus(64)
    sub = bus.subscribe(HookPriority.NORMAL)

    sup = Supervisor(SupervisorPolicy(), hook_bus=bus)
    parent = make_parent("tenant-a", "sess-root", 0)
    child = parent.child_context("researcher", 0)
    sup.emit_spawned(parent, child, "researcher")

    events = await drain_events(sub)
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, HookEvent.SubagentSpawned)
    assert event.parent_session_key == "sess-root"
    assert event.child_session_key == "sess-root::child::0"
    assert event.child_agent_id == "agent-of-sess-root::researcher::0"
    assert event.agent_card == "researcher"
    assert event.depth == 1
    assert event.parent_trace_id == "trace-of-sess-root"
    assert event.tenant_id == "tenant-a"


@pytest.mark.asyncio
async def test_emit_finished_completed_on_stop() -> None:
    """Maps to ``emit_finished_completed_on_stop`` in Rust."""
    bus = HookBus(64)
    sub = bus.subscribe(HookPriority.NORMAL)
    sup = Supervisor(SupervisorPolicy(), hook_bus=bus)
    parent = make_parent("t", "s")
    result = TaskResult(
        output_text="ok",
        tool_calls_made=[],
        child_session_key="s::child::0",
        child_agent_id="agent::card::0",
        elapsed_ms=42,
        finish_reason=FinishReason.STOP,
    )
    sup.emit_finished(parent, result)
    events = await drain_events(sub)
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, HookEvent.SubagentCompleted)
    assert event.finish_reason == "stop"
    assert event.elapsed_ms == 42
    assert event.tool_calls_made == 0
    assert event.parent_trace_id == "trace-of-s"


@pytest.mark.asyncio
async def test_emit_finished_timed_out_on_timeout() -> None:
    """Maps to ``emit_finished_timed_out_on_timeout`` in Rust."""
    bus = HookBus(64)
    sub = bus.subscribe(HookPriority.NORMAL)
    sup = Supervisor(SupervisorPolicy(), hook_bus=bus)
    parent = make_parent("t", "s")
    result = TaskResult(
        output_text="",
        tool_calls_made=[],
        child_session_key="s::child::0",
        child_agent_id="a::c::0",
        elapsed_ms=1234,
        finish_reason=FinishReason.TIMEOUT,
    )
    sup.emit_finished(parent, result)
    events = await drain_events(sub)
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, HookEvent.SubagentTimedOut)
    assert event.child_session_key == "s::child::0"
    assert event.elapsed_ms == 1234
    assert event.parent_trace_id == "trace-of-s"


@pytest.mark.asyncio
async def test_emit_finished_skips_pre_spawn_reasons() -> None:
    """Maps to ``emit_finished_skips_pre_spawn_reasons`` in Rust."""
    bus = HookBus(64)
    sub = bus.subscribe(HookPriority.NORMAL)
    sup = Supervisor(SupervisorPolicy(), hook_bus=bus)
    parent = make_parent("t", "s")
    for reason in (FinishReason.DEPTH_CAPPED, FinishReason.REJECTED):
        result = TaskResult(
            output_text="",
            tool_calls_made=[],
            child_session_key="s::child::-",
            child_agent_id="",
            elapsed_ms=0,
            finish_reason=reason,
            error="noop",
        )
        sup.emit_finished(parent, result)
    events = await drain_events(sub)
    assert events == [], f"pre-spawn reasons must not double-emit, got {events!r}"


@pytest.mark.asyncio
async def test_try_acquire_emits_depth_capped_on_cap_hit() -> None:
    """Maps to ``try_acquire_emits_depth_capped_on_cap_hit`` in Rust."""
    bus = HookBus(64)
    sub = bus.subscribe(HookPriority.NORMAL)
    sup = Supervisor(SupervisorPolicy(), hook_bus=bus)

    # depth >= max_depth (2) refused immediately.
    ctx = make_parent("t", "s", 2)
    with pytest.raises(AcquireRejectError):
        sup.try_acquire(ctx)

    events = await drain_events(sub)
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, HookEvent.SubagentDepthCapped)
    assert event.parent_session_key == "s"
    assert event.attempted_depth == 2
    assert event.reason == "depth_capped"
    assert event.parent_trace_id == "trace-of-s"
    assert event.tenant_id == "t"


@pytest.mark.asyncio
async def test_try_acquire_emits_depth_capped_on_concurrency_cap() -> None:
    """Maps to ``try_acquire_emits_depth_capped_on_concurrency_cap``."""
    bus = HookBus(64)
    sub = bus.subscribe(HookPriority.NORMAL)
    sup = Supervisor(SupervisorPolicy(), hook_bus=bus)

    ctx = make_parent("t", "s")
    _s1 = sup.try_acquire(ctx)
    _s2 = sup.try_acquire(ctx)
    _s3 = sup.try_acquire(ctx)
    # Fourth refused — concurrency cap.
    with pytest.raises(AcquireRejectError):
        sup.try_acquire(ctx)

    events = await drain_events(sub)
    assert events, "expected at least one event"
    last = events[-1]
    assert isinstance(last, HookEvent.SubagentDepthCapped)
    assert last.reason == "parent_concurrency_exceeded"


def test_no_hook_bus_emits_are_silent() -> None:
    """Maps to ``no_hook_bus_emits_are_silent`` in Rust.

    Supervisor without a hook bus must be a no-op on every emit helper
    — the ``hook_bus`` field is ``None`` by default and every emit
    method returns early.
    """
    sup = Supervisor(SupervisorPolicy())
    ctx = make_parent("t", "s")
    # None of these must raise; the absence of a bus is the contract.
    sup.emit_spawned(ctx, ctx.child_context("c", 0), "c")
    sup.emit_finished(
        ctx,
        TaskResult(
            output_text="",
            tool_calls_made=[],
            child_session_key="s::child::0",
            child_agent_id="a",
            elapsed_ms=0,
            finish_reason=FinishReason.STOP,
        ),
    )


# ---------------------------------------------------------------------------
# spawn_child — Python-flavoured tests of the timeout / agent-callable
# layer (the Rust crate covered this via the PyO3 ``python_bridge``
# tests; we exercise the equivalent Python contract here).
# ---------------------------------------------------------------------------


def _make_runner(result: TaskResult) -> AgentCallable:
    """Test helper: build an async callable that returns ``result``."""

    async def _runner(spec: TaskSpec, child_ctx: ParentContext) -> TaskResult:
        # Verify the runner sees the child_ctx (one frame deeper than
        # the caller's parent context).
        assert child_ctx.depth >= 1
        # Echo the goal into the result so tests can verify the spec
        # round-trip.
        return TaskResult(
            output_text=f"goal={spec.goal}",
            tool_calls_made=list(result.tool_calls_made),
            child_session_key=child_ctx.parent_session_key,
            child_agent_id=child_ctx.parent_agent_id,
            elapsed_ms=result.elapsed_ms,
            finish_reason=result.finish_reason,
            error=result.error,
        )

    return _runner


@pytest.mark.asyncio
async def test_spawn_child_happy_path() -> None:
    """End-to-end: try_acquire → emit_spawned → run agent → emit_finished
    and release the slot. Mirrors the Rust ``handshake_roundtrip_returns_task_result``
    test from the PyO3 bridge.
    """
    sup = Supervisor(SupervisorPolicy())
    parent = make_parent("tenant-a", "sess_root")
    task = TaskSpec(goal="research transformers")

    runner = _make_runner(
        TaskResult(
            output_text="ignored — runner overrides",
            tool_calls_made=[],
            child_session_key="will-be-overridden",
            child_agent_id="will-be-overridden",
            elapsed_ms=7,
            finish_reason=FinishReason.STOP,
        )
    )
    result = await sup.spawn_child(runner, parent, task, agent_card="researcher")

    assert result.output_text == "goal=research transformers"
    assert result.finish_reason is FinishReason.STOP
    assert result.child_session_key == "sess_root::child::0"
    assert result.child_agent_id == "agent-of-sess_root::researcher::0"

    # Slot released — counters back to zero. Mirrors the Rust bridge's
    # ``slot_released_on_completion`` assertion.
    assert sup.parent_count("sess_root") == 0
    assert sup.tenant_count("tenant-a") == 0


@pytest.mark.asyncio
async def test_spawn_child_timeout_folds_into_task_result() -> None:
    """Timeout layer: a slow agent should be cancelled at the
    wall-clock budget and the supervisor should return a TaskResult
    with ``finish_reason=TIMEOUT`` instead of raising.
    """
    sup = Supervisor(SupervisorPolicy(max_wall_seconds_ceiling=60))
    parent = make_parent("t", "s")
    # Task asks for 1 second — well below the ceiling — but the agent
    # sleeps for 60s, far longer.
    task = TaskSpec(goal="slow", max_wall_seconds=1)

    async def slow_runner(spec: TaskSpec, child_ctx: ParentContext) -> TaskResult:
        await asyncio.sleep(60)
        raise AssertionError("should be cancelled before this fires")

    result = await sup.spawn_child(slow_runner, parent, task)
    assert result.finish_reason is FinishReason.TIMEOUT
    assert result.child_session_key == "s::child::0"
    # Slot released even after a timeout — drop-guard fires through the
    # context-manager exit.
    assert sup.parent_count("s") == 0


@pytest.mark.asyncio
async def test_spawn_child_releases_slot_on_agent_exception() -> None:
    """Mirrors ``slot_released_on_python_exception`` in the Rust bridge:
    the supervisor's slot must release even when the agent callable
    raises, so a buggy child can't permanently consume a slot.
    """
    sup = Supervisor(SupervisorPolicy())
    parent = make_parent("tenant-a", "sess_root")
    task = TaskSpec(goal="explodes")

    async def angry_runner(spec: TaskSpec, child_ctx: ParentContext) -> TaskResult:
        raise RuntimeError("child blew up mid-flight")

    with pytest.raises(SubagentError) as ei:
        await sup.spawn_child(angry_runner, parent, task)
    assert "child blew up mid-flight" in str(ei.value)
    assert sup.parent_count("sess_root") == 0
    assert sup.tenant_count("tenant-a") == 0


@pytest.mark.asyncio
async def test_spawn_child_raises_on_acquire_reject() -> None:
    """Depth-cap reject must surface as :class:`AcquireRejectError`
    *before* the agent callable is invoked. Mirrors
    ``depth_cap_short_circuits_before_python`` in the Rust bridge.
    """
    sup = Supervisor(SupervisorPolicy())
    parent = make_parent("t", "s", depth=5)  # way over the cap
    task = TaskSpec(goal="never runs")

    invoked = False

    async def bomb_runner(spec: TaskSpec, child_ctx: ParentContext) -> TaskResult:
        nonlocal invoked
        invoked = True
        raise AssertionError("agent must not run when cap rejects")

    with pytest.raises(AcquireRejectError) as ei:
        await sup.spawn_child(bomb_runner, parent, task)
    assert ei.value.reason is AcquireReject.DEPTH_CAPPED
    assert invoked is False
    # No counter changes — depth check happens before any increment.
    assert sup.parent_count("s") == 0


@pytest.mark.asyncio
async def test_spawn_child_to_result_folds_exception_into_error_result() -> None:
    """Mirrors ``convenience_wrapper_folds_exception_into_error_result``
    in the Rust bridge. The wrapper hides the failure mode from the
    caller; the result envelope always lands in the parent's loop.
    """
    sup = Supervisor(SupervisorPolicy())
    parent = make_parent("tenant-a", "sess_root")
    task = TaskSpec(goal="explodes")

    async def angry_runner(spec: TaskSpec, child_ctx: ParentContext) -> TaskResult:
        raise ValueError("bad json")

    result = await sup.spawn_child_to_result(angry_runner, parent, task)
    assert result.finish_reason is FinishReason.ERROR
    assert result.error is not None
    assert "bad json" in result.error
    assert result.child_session_key == "sess_root::child::-"
    assert sup.parent_count("sess_root") == 0


@pytest.mark.asyncio
async def test_spawn_child_to_result_maps_depth_cap_to_rejected_envelope() -> None:
    """Mirrors ``convenience_wrapper_maps_caps_to_rejected_result``."""
    sup = Supervisor(SupervisorPolicy())
    parent = make_parent("t", "s", depth=2)
    task = TaskSpec(goal="over depth")

    async def runner(spec: TaskSpec, child_ctx: ParentContext) -> TaskResult:
        raise AssertionError("agent must not run")

    result = await sup.spawn_child_to_result(runner, parent, task)
    assert result.finish_reason is FinishReason.DEPTH_CAPPED
    assert result.child_session_key.endswith("::child::-")


@pytest.mark.asyncio
async def test_spawn_child_to_result_maps_concurrency_cap_to_rejected() -> None:
    """Per-parent concurrency cap should fold into a ``REJECTED``
    envelope (mirrors Rust's mapping of non-depth caps to
    ``FinishReason::Rejected``).
    """
    sup = Supervisor(SupervisorPolicy())
    parent = make_parent("t", "sess")

    # Saturate the per-parent counter.
    held = [sup.try_acquire(parent) for _ in range(3)]
    assert sup.parent_count("sess") == 3

    async def runner(spec: TaskSpec, child_ctx: ParentContext) -> TaskResult:
        raise AssertionError("agent must not run")

    result = await sup.spawn_child_to_result(runner, parent, TaskSpec(goal="x"))
    assert result.finish_reason is FinishReason.REJECTED
    assert result.child_session_key.endswith("::child::-")

    for s in held:
        s.release()


@pytest.mark.asyncio
async def test_parallel_spawn_respects_per_parent_cap() -> None:
    """Concurrency stress: gathering more parallel spawns than the
    per-parent cap allows must reject the excess (folded into REJECTED
    via ``spawn_child_to_result``) without leaking slots.
    """
    sup = Supervisor(SupervisorPolicy(max_concurrent_per_parent=2))
    parent = make_parent("t", "sess")

    # Slow agent so all 5 spawns are in flight simultaneously.
    async def slow_runner(spec: TaskSpec, child_ctx: ParentContext) -> TaskResult:
        await asyncio.sleep(0.05)
        return TaskResult(
            output_text="ok",
            tool_calls_made=[],
            child_session_key=child_ctx.parent_session_key,
            child_agent_id=child_ctx.parent_agent_id,
            elapsed_ms=50,
            finish_reason=FinishReason.STOP,
        )

    results = await asyncio.gather(
        *(
            sup.spawn_child_to_result(
                slow_runner,
                parent,
                TaskSpec(goal=f"task-{i}"),
                child_seq=i,
            )
            for i in range(5)
        )
    )
    # 2 should have succeeded, 3 rejected with REJECTED.
    successes = [r for r in results if r.finish_reason is FinishReason.STOP]
    rejects = [r for r in results if r.finish_reason is FinishReason.REJECTED]
    assert len(successes) == 2
    assert len(rejects) == 3
    # All slots released.
    assert sup.parent_count("sess") == 0


@pytest.mark.asyncio
async def test_spawn_child_emits_full_lifecycle_on_hook_bus() -> None:
    """End-to-end emit verification: a successful spawn should emit
    SubagentSpawned + SubagentCompleted in that order on a
    bus-equipped supervisor.
    """
    bus = HookBus(64)
    sub = bus.subscribe(HookPriority.NORMAL)
    sup = Supervisor(SupervisorPolicy(), hook_bus=bus)
    parent = make_parent("tenant-a", "sess_root")
    task = TaskSpec(goal="research")

    async def runner(spec: TaskSpec, child_ctx: ParentContext) -> TaskResult:
        return TaskResult(
            output_text="ok",
            tool_calls_made=[],
            child_session_key=child_ctx.parent_session_key,
            child_agent_id=child_ctx.parent_agent_id,
            elapsed_ms=1,
            finish_reason=FinishReason.STOP,
        )

    await sup.spawn_child(runner, parent, task, agent_card="researcher")
    events = await drain_events(sub)
    assert len(events) == 2
    assert isinstance(events[0], HookEvent.SubagentSpawned)
    assert isinstance(events[1], HookEvent.SubagentCompleted)
