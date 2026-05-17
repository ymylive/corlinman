"""Port of ``corlinman-scheduler::runtime``'s dispatch + cancel unit tests.

Mirrors the Rust ``mod tests`` in ``src/runtime.rs``:

* ``dispatch_subprocess_success_emits_completed``
* ``dispatch_subprocess_failure_emits_failed_with_exit_code``
* ``dispatch_subprocess_timeout_emits_failed_timeout``
* ``dispatch_subprocess_missing_binary_emits_spawn_failed``
* ``unsupported_action_emits_failed``
* ``cancel_stops_job_loop_promptly``

Hook events flow through :mod:`corlinman_hooks` (the workspace's
Python port of ``corlinman-hooks``). Each test subscribes at
``HookPriority.NORMAL`` and asserts on the first event off the
subscription — the bus delivers in tier order so a single Normal
subscriber sees everything for these tests.
"""

from __future__ import annotations

import asyncio

import pytest
from corlinman_hooks import HookBus, HookEvent, HookPriority, Lagged

from corlinman_server.scheduler import (
    JobAction,
    JobSpec,
    SchedulerConfig,
    SchedulerJob,
    dispatch,
    spawn,
)


def _spec_for(action: JobAction) -> JobSpec:
    """Build a :class:`JobSpec` directly — tests call :func:`dispatch`
    rather than going through the tick loop, so the cron expression is
    a placeholder. Mirrors the Rust ``spec_for`` helper."""
    cfg = SchedulerJob(name="unit", cron="0 0 0 * * * *", action=action)
    spec = JobSpec.from_config(cfg)
    assert spec is not None, "test cron should parse"
    return spec


async def _next_event(sub) -> object:
    """Receive the next event, with a small timeout so a hung dispatch
    doesn't deadlock the test binary. Mirrors the Rust helper of the
    same name."""

    async def _loop():
        while True:
            try:
                return await sub.recv()
            except Lagged:
                # Slow subscriber surfaced a Lagged exception — skip
                # forward, the bus has already advanced.
                continue

    return await asyncio.wait_for(_loop(), timeout=2.0)


async def test_dispatch_subprocess_success_emits_completed() -> None:
    """A successful firing emits :class:`HookEvent.EngineRunCompleted`."""
    bus = HookBus(16)
    sub = bus.subscribe(HookPriority.NORMAL)
    spec = _spec_for(JobAction.subprocess(command="true", timeout_secs=5))
    await dispatch(spec, bus)
    evt = await _next_event(sub)
    assert isinstance(evt, HookEvent.EngineRunCompleted), f"got {evt!r}"


async def test_dispatch_subprocess_failure_emits_failed_with_exit_code() -> None:
    """``false`` → :class:`HookEvent.EngineRunFailed` with
    ``error_kind = "exit_code"`` and ``exit_code = 1``."""
    bus = HookBus(16)
    sub = bus.subscribe(HookPriority.NORMAL)
    spec = _spec_for(JobAction.subprocess(command="false", timeout_secs=5))
    await dispatch(spec, bus)
    evt = await _next_event(sub)
    assert isinstance(evt, HookEvent.EngineRunFailed), f"got {evt!r}"
    assert evt.error_kind == "exit_code"
    assert evt.exit_code == 1


async def test_dispatch_subprocess_timeout_emits_failed_timeout() -> None:
    """``sleep 30`` with a 1s timeout → ``error_kind = "timeout"`` and
    ``exit_code = None``."""
    bus = HookBus(16)
    sub = bus.subscribe(HookPriority.NORMAL)
    spec = _spec_for(JobAction.subprocess(command="sleep", args=("30",), timeout_secs=1))
    await dispatch(spec, bus)
    evt = await _next_event(sub)
    assert isinstance(evt, HookEvent.EngineRunFailed), f"got {evt!r}"
    assert evt.error_kind == "timeout"
    assert evt.exit_code is None


async def test_dispatch_subprocess_missing_binary_emits_spawn_failed() -> None:
    """A missing binary surfaces as ``error_kind = "spawn_failed"`` —
    the spawn fails before we ever have a child to inspect."""
    bus = HookBus(16)
    sub = bus.subscribe(HookPriority.NORMAL)
    spec = _spec_for(
        JobAction.subprocess(command="/nonexistent/__corlinman_test_bin__", timeout_secs=5)
    )
    await dispatch(spec, bus)
    evt = await _next_event(sub)
    assert isinstance(evt, HookEvent.EngineRunFailed), f"got {evt!r}"
    assert evt.error_kind == "spawn_failed"


async def test_unsupported_action_emits_failed() -> None:
    """``RunAgent`` (and ``RunTool``) are not wired end-to-end yet, so
    a dispatch must surface ``error_kind = "unsupported_action"`` on
    the bus — operators see the missing wiring instead of a silent drop."""
    bus = HookBus(16)
    sub = bus.subscribe(HookPriority.NORMAL)
    spec = _spec_for(JobAction.run_agent(prompt="x"))
    await dispatch(spec, bus)
    evt = await _next_event(sub)
    assert isinstance(evt, HookEvent.EngineRunFailed), f"got {evt!r}"
    assert evt.error_kind == "unsupported_action"


async def test_cancel_stops_job_loop_promptly() -> None:
    """A cron that fires only once a year would block forever without
    a working cancel path. Flipping the cancel event must let
    ``join_all`` return inside the 2-second timeout."""
    bus = HookBus(16)
    cancel = asyncio.Event()
    cfg = SchedulerConfig(
        jobs=(
            SchedulerJob(
                name="yearly",
                cron="0 0 0 1 1 * *",  # 00:00:00 on Jan 1, any year
                action=JobAction.subprocess(command="true", timeout_secs=5),
            ),
        )
    )
    handle = spawn(cfg, bus, cancel)
    # Let the loop park on `sleep_until` before flipping cancel — a
    # 50ms yield is enough on every CI host we've observed.
    await asyncio.sleep(0.05)
    cancel.set()
    await asyncio.wait_for(handle.join_all(), timeout=2.0)


@pytest.mark.parametrize(
    "bad_action",
    [
        JobAction.run_agent(prompt="x"),
        JobAction.run_tool(plugin="p", tool="t", args=None),
    ],
)
async def test_both_unsupported_actions_emit_failed(bad_action: JobAction) -> None:
    """Both ``RunAgent`` and ``RunTool`` go down the unsupported branch.
    Parametrising the case keeps the two paths covered without a second
    near-identical test function."""
    bus = HookBus(16)
    sub = bus.subscribe(HookPriority.NORMAL)
    spec = _spec_for(bad_action)
    await dispatch(spec, bus)
    evt = await _next_event(sub)
    assert isinstance(evt, HookEvent.EngineRunFailed)
    assert evt.error_kind == "unsupported_action"
