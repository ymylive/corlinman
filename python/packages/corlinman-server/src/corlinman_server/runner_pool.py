"""v0.7.1 warm pool for the agent runtime.

OpenClaw inspired the surface: ``acquire(key)`` returns a warm handle
when one is available; otherwise it cold-spawns via a per-key factory
and counts the miss. ``release(handle)`` returns the entity to the
pool, subject to per-key warm cap. Idle eviction fires when the active
total presses on ``max_active``.

What this pool is for, in v0.7.1:

corlinman's Rust gateway talks gRPC to a long-running Python servicer;
chat sessions don't spawn fresh OS processes per request. The cold-
start cost lives instead in **provider SDK first-call setup** (httpx
client, auth, model schema validation) and in the **agent-card +
context assembler** initialisation that lazy-runs on first chat.

The pool is the abstraction that lets us amortise those costs:

- **Pre-warm** at servicer boot: the operator's most-used
  ``(provider_alias, model)`` keys get one warm provider instance ready
  before the first user request.
- **Acquire / release** per chat: existing warm provider is reused;
  the SDK's HTTP/2 connection pool stays warm across requests.
- **Eviction**: idle entries past ``max_active`` are evicted oldest-
  first so memory doesn't grow unbounded under churn.

The pool is provider-agnostic: it stores ``Any`` and the factory
decides what gets cached. Initial caller in v0.7.1 is the provider
resolver in :mod:`corlinman_server.agent_servicer`; later releases
may pool reasoning loops or context assemblers.

Structured-log observability uses ``structlog`` (matches the rest of
the Python side); Prometheus counters are deliberately not added here
because the Rust gateway already exports per-chat latency that
operators monitor. Reach for prometheus_client only when an explicit
operator request lands.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Lock

import structlog

logger = structlog.get_logger(__name__)


PoolKey = tuple[str, str]
"""``(group, sub_key)`` — for the v0.7.1 provider case, that's
``(provider_alias, model)``. Generic enough to repurpose for later
pooled resources without redesigning the key shape."""


@dataclass
class PoolStats:
    """Per-pool counters + warm gauge. Read via :meth:`RunnerPool.stats`;
    mutate via the pool's internal hooks only. ``warm_age_seconds``
    surfaces the oldest warm entry's age so an operator can spot
    pool-stagnation regressions."""

    hits: int = 0
    misses: int = 0
    evictions: int = 0
    warm_count: int = 0
    warm_age_seconds: float = 0.0


@dataclass
class _Entry[T]:
    """One pool entry. ``last_idle_at`` is reset on each release so
    eviction can pick the oldest-idle. ``created_at`` is fixed at the
    cold-spawn moment for warm-age accounting."""

    value: T
    key: PoolKey
    created_at: float = field(default_factory=time.monotonic)
    last_idle_at: float = field(default_factory=time.monotonic)


@dataclass
class RunnerHandle[T]:
    """Drop-guard returned by :meth:`RunnerPool.acquire`. Hold while
    the caller is using the resource; call :meth:`release` (or use
    via the context-manager protocol) to return it to the pool.

    Re-using a released handle is a programmer error; the pool's
    internal accounting would double-release. ``_released`` short-
    circuits the path so explicit ``release()`` + scope-exit don't
    decrement twice.
    """

    key: PoolKey
    value: T
    _pool: RunnerPool[T]
    _released: bool = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._pool._return(self.key, self.value)

    def __enter__(self) -> T:
        return self.value

    def __exit__(self, *_exc: object) -> None:
        self.release()


class RunnerPool[T]:
    """Bounded warm pool. Thread-safe under the internal lock; safe to
    share across asyncio coroutines because Python sqlite3-style
    coarse locking is enough for the rate the pool operates at (one
    acquire per chat request).

    Parameters
    ----------
    max_warm_per_key
        How many entries to keep warm under a single key. The pool
        never warms more than this; release-after-full evicts.
    max_active_total
        Cap on warm entries across *all* keys. When pressed, the
        oldest-idle entry (any key) is evicted.
    """

    def __init__(
        self,
        *,
        max_warm_per_key: int = 2,
        max_active_total: int = 8,
    ) -> None:
        if max_warm_per_key < 1 or max_active_total < 1:
            raise ValueError("pool caps must be positive")
        if max_warm_per_key > max_active_total:
            raise ValueError(
                "max_warm_per_key cannot exceed max_active_total"
            )
        self._max_warm_per_key = max_warm_per_key
        self._max_active_total = max_active_total
        # Per-key LRU of warm entries. The outer dict is keyed by the
        # PoolKey; the inner OrderedDict carries one entry per warm
        # slot, ordered oldest-first so eviction is a popitem(last=False)
        # away. (We use the entry's id() as the inner key so the same
        # value never collides with itself.)
        self._warm: dict[PoolKey, OrderedDict[int, _Entry[T]]] = {}
        self._lock = Lock()
        self._stats = PoolStats()

    # ─── Public surface ────────────────────────────────────────────

    def acquire(self, key: PoolKey, factory: Callable[[], T]) -> RunnerHandle[T]:
        """Return a warm entry under ``key``, cold-spawning via
        ``factory`` if none is available. ``factory`` runs *outside*
        the lock so a slow-to-construct provider doesn't stall other
        coroutines acquiring different keys.
        """
        with self._lock:
            entries = self._warm.get(key)
            if entries:
                # LIFO pick — the most-recently released is also the
                # most-likely-to-have-warm-connections. Pop from end.
                _, entry = entries.popitem(last=True)
                if not entries:
                    del self._warm[key]
                self._stats.hits += 1
                logger.debug(
                    "runner_pool.hit",
                    key=key,
                    age_seconds=time.monotonic() - entry.created_at,
                )
                self._refresh_warm_count_unlocked()
                return RunnerHandle(key=key, value=entry.value, _pool=self)
            # Miss — cold-spawn outside the lock.
            self._stats.misses += 1
        value = factory()
        logger.info("runner_pool.miss_cold_spawn", key=key)
        return RunnerHandle(key=key, value=value, _pool=self)

    def prewarm(self, key: PoolKey, factory: Callable[[], T]) -> None:
        """Cold-spawn one entry under ``key`` and park it warm. Used
        at servicer boot for the operator's most-used aliases.

        If the per-key warm cap is already at the limit, the call is a
        no-op (idempotent). Honours ``max_active_total`` by evicting
        oldest-idle if necessary; the freshly-warmed entry always
        wins the slot.
        """
        value = factory()
        entry = _Entry(value=value, key=key)
        with self._lock:
            entries = self._warm.setdefault(key, OrderedDict())
            if len(entries) >= self._max_warm_per_key:
                return
            self._enforce_active_cap_unlocked()
            entries[id(entry)] = entry
            logger.info("runner_pool.prewarmed", key=key)
            self._refresh_warm_count_unlocked()

    def stats(self) -> PoolStats:
        """Snapshot of current pool counters. The values are copied so
        callers don't observe mid-mutation state."""
        with self._lock:
            self._refresh_warm_count_unlocked()
            return PoolStats(
                hits=self._stats.hits,
                misses=self._stats.misses,
                evictions=self._stats.evictions,
                warm_count=self._stats.warm_count,
                warm_age_seconds=self._stats.warm_age_seconds,
            )

    # ─── Internal: handle re-entry on release ──────────────────────

    def _return(self, key: PoolKey, value: T) -> None:
        """Called by :meth:`RunnerHandle.release`. If pool is full,
        the entry is dropped (caller's reference is the last one)."""
        entry = _Entry(value=value, key=key)
        with self._lock:
            entries = self._warm.setdefault(key, OrderedDict())
            if len(entries) >= self._max_warm_per_key:
                logger.debug("runner_pool.dropped_full_per_key", key=key)
                if not entries:
                    del self._warm[key]
                return
            self._enforce_active_cap_unlocked()
            entry.last_idle_at = time.monotonic()
            entries[id(entry)] = entry
            self._refresh_warm_count_unlocked()

    def _enforce_active_cap_unlocked(self) -> None:
        """If adding one more warm entry would breach ``max_active_total``,
        evict the oldest-idle entry (across all keys) to make room.
        Caller must hold ``self._lock``.
        """
        current = sum(len(v) for v in self._warm.values())
        if current < self._max_active_total:
            return
        oldest_key: PoolKey | None = None
        oldest_entry_id: int | None = None
        oldest_idle = float("inf")
        for k, entries in self._warm.items():
            for eid, entry in entries.items():
                if entry.last_idle_at < oldest_idle:
                    oldest_idle = entry.last_idle_at
                    oldest_key = k
                    oldest_entry_id = eid
        if oldest_key is not None and oldest_entry_id is not None:
            entries = self._warm[oldest_key]
            entries.pop(oldest_entry_id, None)
            if not entries:
                del self._warm[oldest_key]
            self._stats.evictions += 1
            logger.info("runner_pool.evicted_oldest_idle", key=oldest_key)

    def _refresh_warm_count_unlocked(self) -> None:
        total = 0
        oldest_age = 0.0
        now = time.monotonic()
        for entries in self._warm.values():
            for entry in entries.values():
                total += 1
                age = now - entry.created_at
                if age > oldest_age:
                    oldest_age = age
        self._stats.warm_count = total
        self._stats.warm_age_seconds = oldest_age


__all__ = [
    "PoolKey",
    "PoolStats",
    "RunnerHandle",
    "RunnerPool",
]
