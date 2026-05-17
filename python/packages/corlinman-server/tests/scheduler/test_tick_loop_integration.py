"""Port of ``rust/crates/corlinman-scheduler/tests/tick_loop_integration.rs``.

Validates the end-to-end tick path (cron parse → sleep until next →
dispatch → bus emit) without involving the gateway.

A per-second cron job that successfully exits 0 must emit
:class:`HookEvent.EngineRunCompleted` at least 3 times in 5 seconds.

We use the 6-field "every second" expression ``* * * * * *`` (no year
column). The Python port also accepts the Rust-native 7-field
``* * * * * * *`` form but the 6-field flavour reads more naturally
in a Python test file.
"""

from __future__ import annotations

import asyncio

import pytest
from corlinman_hooks import Closed, HookBus, HookEvent, HookPriority, Lagged

from corlinman_server.scheduler import (
    JobAction,
    SchedulerConfig,
    SchedulerJob,
    spawn,
)


@pytest.mark.slow
async def test_per_second_cron_fires_multiple_times_in_five_seconds() -> None:
    """End-to-end tick test. Marked ``slow`` so a developer can
    ``pytest -m "not slow"`` to skip the 5-second wait when iterating."""
    bus = HookBus(64)
    sub = bus.subscribe(HookPriority.NORMAL)
    cancel = asyncio.Event()

    cfg = SchedulerConfig(
        jobs=(
            SchedulerJob(
                name="tick",
                cron="* * * * * *",  # every second (6-field)
                action=JobAction.subprocess(command="true", timeout_secs=5),
            ),
        )
    )

    handle = spawn(cfg, bus, cancel)

    async def _drain_for(deadline_secs: float) -> int:
        """Drain ``EngineRunCompleted`` events until ``deadline_secs``
        wall-clock elapses; return the count."""
        count = 0
        loop = asyncio.get_running_loop()
        end_at = loop.time() + deadline_secs
        while True:
            remaining = end_at - loop.time()
            if remaining <= 0:
                return count
            try:
                evt = await asyncio.wait_for(sub.recv(), timeout=min(0.5, remaining))
            except asyncio.TimeoutError:
                continue
            except Lagged:
                # Slow subscriber — bus advanced, keep counting on
                # subsequent events.
                continue
            except Closed:
                return count
            if isinstance(evt, HookEvent.EngineRunCompleted):
                count += 1

    try:
        collected = await asyncio.wait_for(_drain_for(5.0), timeout=6.0)
    finally:
        cancel.set()
        with (
            __import__("contextlib").suppress(BaseException),
        ):
            await asyncio.wait_for(handle.join_all(), timeout=3.0)

    # 2-fire allowance for boot scheduling jitter (the loop may first
    # sleep up to ~1s before the very first firing) — match the Rust
    # `>= 3` bound exactly.
    assert collected >= 3, f"expected >= 3 firings in 5s, got {collected}"
