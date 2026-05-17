"""Tests for ``corlinman_channels.rate_limit``.

Mirrors the unit tests in ``rust/.../rate_limit.rs`` test module.
The ``refills_over_time`` test uses a tiny custom bucket so we don't
have to wait the full second the Rust test sleeps.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from corlinman_channels.rate_limit import (
    GC_STALE_AFTER,
    TokenBucket,
)


class TestPerKeyBudget:
    """Mirrors ``rate_limit::tests`` (the synchronous unit tests)."""

    def test_bucket_allows_within_capacity(self) -> None:
        b = TokenBucket.per_minute(20)
        for _ in range(20):
            assert b.check("g:1")

    def test_bucket_denies_when_empty(self) -> None:
        b = TokenBucket.per_minute(20)
        for _ in range(20):
            assert b.check("g:1")
        # 21st immediately after exhausting → refill << 1 token → deny.
        assert b.check("g:1") is False

    def test_bucket_isolates_keys(self) -> None:
        b = TokenBucket.per_minute(3)
        for _ in range(3):
            assert b.check("a")
        assert b.check("a") is False
        # Key "b" has its own bucket, unaffected.
        for _ in range(3):
            assert b.check("b")
        assert b.check("b") is False

    def test_bucket_capacity_caps_refill(self) -> None:
        """A bucket idle for a long time should not accumulate beyond
        capacity. Mirrors ``bucket_capacity_caps_refill`` in Rust."""
        b = TokenBucket.per_minute(5)
        # Seed an idle key by checking once, then rewind ``last_refill``
        # far enough to accumulate >> capacity worth.
        assert b.check("k")
        state = b._state["k"]
        state.last_refill = time.monotonic() - 3600.0
        state.tokens = 0.0
        # One refill computation on next check → should cap at capacity,
        # not at refill_per_sec * elapsed = 300.
        assert b.check("k")
        # After consuming 1, bucket should have exactly capacity-1.
        remaining = b._state["k"].tokens
        assert abs(remaining - (b.capacity - 1.0)) < 1e-6, (
            f"expected refill capped at capacity, got {remaining}"
        )


class TestSweep:
    def test_sweep_drops_stale_keys(self) -> None:
        b = TokenBucket.per_minute(5)
        assert b.check("idle")
        assert b.tracked_keys() == 1
        # Rewind last_refill past the stale cutoff.
        state = b._state["idle"]
        state.last_refill = time.monotonic() - GC_STALE_AFTER - 1.0
        b.sweep_stale()
        assert b.tracked_keys() == 0

    def test_sweep_keeps_fresh_keys(self) -> None:
        """Sister of ``sweep_drops_stale_keys`` — ensures we don't bin
        active senders by mistake. Not in the Rust suite but cheap
        defensive coverage."""
        b = TokenBucket.per_minute(5)
        assert b.check("fresh")
        b.sweep_stale()
        assert b.tracked_keys() == 1


class TestRefillOverTime:
    """Refill-after-time test. We rewind ``last_refill`` rather than
    actually sleeping so the test stays sub-millisecond regardless of
    machine speed.

    The Rust counterpart sleeps 1.1s for a 60/min bucket (1 token/sec).
    We achieve identical *coverage* — "exhaust, wait, retry → success"
    — by manipulating the internal state directly, which the dataclass
    cleanly allows. Hot CI runners would otherwise risk false negatives
    if a wall-clock sleep races with the refill arithmetic."""

    def test_bucket_refills_over_time(self) -> None:
        b = TokenBucket.per_minute(60)
        for _ in range(60):
            assert b.check("g:1")
        assert b.check("g:1") is False
        # Rewind last_refill by 1.1s — equivalent to having slept that
        # long. Refill at 1 tok/sec yields ~1.1 tokens.
        state = b._state["g:1"]
        state.last_refill = time.monotonic() - 1.1
        assert b.check("g:1"), "expected refill after 1.1s at 1 tok/sec"


class TestGc:
    """Async-only path: the background sweeper task."""

    @pytest.mark.asyncio
    async def test_start_gc_sweeps_periodically(self) -> None:
        b = TokenBucket.per_minute(5)
        assert b.check("idle")
        # Pre-age the entry so the first sweep prunes it.
        state = b._state["idle"]
        state.last_refill = time.monotonic() - GC_STALE_AFTER - 1.0

        cancel = asyncio.Event()
        task = b.start_gc(cancel=cancel, interval=0.02)

        try:
            # First tick is skipped (matches Rust ``ticker.tick().await``
            # before the loop), so we need >= 2 * interval to see a sweep.
            for _ in range(50):
                await asyncio.sleep(0.02)
                if b.tracked_keys() == 0:
                    break
            assert b.tracked_keys() == 0, "expected sweep to prune the stale key"
        finally:
            cancel.set()
            await asyncio.wait_for(task, timeout=1.0)

    @pytest.mark.asyncio
    async def test_start_gc_cancel_event_stops_loop_cleanly(self) -> None:
        b = TokenBucket.per_minute(5)
        cancel = asyncio.Event()
        task = b.start_gc(cancel=cancel, interval=0.05)
        await asyncio.sleep(0.01)
        cancel.set()
        await asyncio.wait_for(task, timeout=1.0)
        assert task.done()
        assert task.exception() is None

    @pytest.mark.asyncio
    async def test_start_gc_task_cancel_also_stops_loop(self) -> None:
        import contextlib

        b = TokenBucket.per_minute(5)
        task = b.start_gc(interval=0.05)
        await asyncio.sleep(0.01)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1.0)
        assert task.done()
