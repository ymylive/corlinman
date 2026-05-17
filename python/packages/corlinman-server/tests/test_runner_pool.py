"""Tests for the v0.7.1 warm pool.

Six contracts to lock:

1. **Hit vs miss accounting.** First acquire under a fresh key is a
   miss; releasing then re-acquiring is a hit.
2. **Per-key warm cap.** Releasing beyond the per-key cap drops the
   surplus (the caller's reference becomes the last one).
3. **Active-total cap with oldest-idle eviction.** Pressing the
   pool past ``max_active_total`` evicts the oldest-idle entry,
   any key.
4. **Pre-warm honours both caps.** A factory call landing into a
   full pool no-ops; one landing into a non-full pool warms.
5. **Handle is a drop-guard.** Context-manager protocol releases on
   exit; double-release is a no-op.
6. **Cold-spawn happens outside the lock.** A slow factory under one
   key does not serialise acquires under another key.
"""

from __future__ import annotations

import threading
import time

import pytest
from corlinman_server.runner_pool import RunnerPool


def _factory(label: str):
    def make() -> str:
        return label
    return make


# ---------------------------------------------------------------------------
# Hit / miss accounting
# ---------------------------------------------------------------------------


def test_first_acquire_is_miss_release_then_acquire_is_hit() -> None:
    pool: RunnerPool[str] = RunnerPool(max_warm_per_key=2, max_active_total=4)
    h = pool.acquire(("p", "m"), _factory("v1"))
    assert h.value == "v1"
    h.release()
    s = pool.stats()
    assert s.misses == 1 and s.hits == 0

    h2 = pool.acquire(("p", "m"), _factory("WRONG"))
    # Factory must not have been called — we got the warm entry.
    assert h2.value == "v1"
    h2.release()
    s = pool.stats()
    assert s.hits == 1 and s.misses == 1


def test_stats_snapshot_is_a_copy() -> None:
    """Reading stats then mutating the pool should not retroactively
    change the snapshot the caller already holds."""
    pool: RunnerPool[str] = RunnerPool()
    s1 = pool.stats()
    pool.acquire(("p", "m"), _factory("v")).release()
    s2 = pool.stats()
    assert s1.misses == 0
    assert s2.misses == 1


# ---------------------------------------------------------------------------
# Per-key cap
# ---------------------------------------------------------------------------


def test_release_beyond_per_key_cap_is_dropped() -> None:
    """Two warm slots; releasing a third sends the surplus to /dev/null.
    Pool stays at the cap; warm_count reflects the limit."""
    pool: RunnerPool[str] = RunnerPool(max_warm_per_key=2, max_active_total=10)
    handles = [pool.acquire(("p", "m"), _factory(f"v{i}")) for i in range(3)]
    for h in handles:
        h.release()
    s = pool.stats()
    assert s.warm_count == 2, "per-key cap binds before total cap"


# ---------------------------------------------------------------------------
# Active total cap + oldest-idle eviction
# ---------------------------------------------------------------------------


def test_active_total_cap_evicts_oldest_idle_across_keys() -> None:
    """max_active_total=2 across two distinct keys. Release two; warm
    a third under a third key. The oldest-idle release goes away."""
    pool: RunnerPool[str] = RunnerPool(max_warm_per_key=2, max_active_total=2)

    h1 = pool.acquire(("p", "a"), _factory("a"))
    h1.release()
    time.sleep(0.005)  # ensure monotonic clock moves
    h2 = pool.acquire(("p", "b"), _factory("b"))
    h2.release()
    assert pool.stats().warm_count == 2

    # Pre-warm under a third key — total would be 3, cap is 2, so the
    # oldest-idle (a) is evicted.
    pool.prewarm(("p", "c"), _factory("c"))
    s = pool.stats()
    assert s.warm_count == 2
    assert s.evictions == 1

    # Acquire under 'a' should miss (it got evicted); 'b' should hit.
    # We deliberately DO NOT release the cold-spawn handle so it
    # doesn't trigger a follow-on eviction that would displace 'b'.
    base = pool.stats()
    miss_handle = pool.acquire(("p", "a"), _factory("a-new"))  # miss
    pool.acquire(("p", "b"), _factory("WRONG")).release()      # hit
    after = pool.stats()
    assert after.misses - base.misses == 1
    assert after.hits - base.hits == 1
    miss_handle.release()


# ---------------------------------------------------------------------------
# Prewarm
# ---------------------------------------------------------------------------


def test_prewarm_under_full_pool_noops() -> None:
    pool: RunnerPool[str] = RunnerPool(max_warm_per_key=1, max_active_total=10)
    pool.prewarm(("p", "m"), _factory("v1"))
    # Second prewarm under same key should noop because per-key cap is 1.
    pool.prewarm(("p", "m"), _factory("v2"))
    s = pool.stats()
    assert s.warm_count == 1
    # Acquire returns the first value, proving the second prewarm
    # didn't displace it.
    h = pool.acquire(("p", "m"), _factory("WRONG"))
    assert h.value == "v1"


def test_prewarm_respects_active_cap() -> None:
    """Pre-warming across more keys than active_cap triggers eviction
    on each spillover."""
    pool: RunnerPool[str] = RunnerPool(max_warm_per_key=1, max_active_total=2)
    pool.prewarm(("p", "a"), _factory("a"))
    time.sleep(0.005)
    pool.prewarm(("p", "b"), _factory("b"))
    time.sleep(0.005)
    pool.prewarm(("p", "c"), _factory("c"))
    s = pool.stats()
    assert s.warm_count == 2
    assert s.evictions == 1


# ---------------------------------------------------------------------------
# Handle drop-guard
# ---------------------------------------------------------------------------


def test_handle_context_manager_releases_on_exit() -> None:
    pool: RunnerPool[str] = RunnerPool()
    handle = pool.acquire(("p", "m"), _factory("v"))
    with handle as value:
        assert value == "v"
    # After context-exit, the next acquire is a hit.
    pool.acquire(("p", "m"), _factory("WRONG")).release()
    assert pool.stats().hits == 1


def test_double_release_is_idempotent() -> None:
    pool: RunnerPool[str] = RunnerPool(max_warm_per_key=2)
    h = pool.acquire(("p", "m"), _factory("v"))
    h.release()
    h.release()
    s = pool.stats()
    # The pool sees one warm entry, not two — the second release
    # short-circuited.
    assert s.warm_count == 1


# ---------------------------------------------------------------------------
# Concurrent acquires under different keys
# ---------------------------------------------------------------------------


def test_factory_runs_outside_the_lock() -> None:
    """A slow factory under key A must not block an acquire under key B.

    We don't pin a wall-clock budget (CI flake risk), only assert
    ordering: thread B's acquire completes while thread A's factory
    is still running."""
    pool: RunnerPool[str] = RunnerPool()
    a_started = threading.Event()
    a_can_finish = threading.Event()
    b_completed = threading.Event()

    def slow_factory() -> str:
        a_started.set()
        a_can_finish.wait(timeout=2.0)
        return "a"

    def fast_factory() -> str:
        return "b"

    def thread_a() -> None:
        pool.acquire(("p", "a"), slow_factory).release()

    def thread_b() -> None:
        a_started.wait(timeout=1.0)
        pool.acquire(("p", "b"), fast_factory).release()
        b_completed.set()

    ta = threading.Thread(target=thread_a)
    tb = threading.Thread(target=thread_b)
    ta.start()
    tb.start()
    # Thread B finishes while A is still inside slow_factory.
    assert b_completed.wait(timeout=1.5)
    a_can_finish.set()
    ta.join(timeout=2.0)
    tb.join(timeout=2.0)
    assert not ta.is_alive()
    assert not tb.is_alive()


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("warm", "active"),
    [
        (0, 1),
        (1, 0),
        (3, 2),  # warm > active is nonsensical
    ],
)
def test_pool_rejects_invalid_caps(warm: int, active: int) -> None:
    with pytest.raises(ValueError):
        RunnerPool(max_warm_per_key=warm, max_active_total=active)
