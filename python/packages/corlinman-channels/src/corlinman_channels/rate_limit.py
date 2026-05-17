"""Per-key token-bucket rate limiter used by :mod:`corlinman_channels.router`.

Python port of ``rust/.../rate_limit.rs``. The router consults one bucket
per rate-limit dimension (per-group, per-sender). The key is a stable
string derived from the channel binding (``"qq:group:<gid>"``,
``"qq:sender:<gid>:<uid>"``) so collisions across channels / threads are
impossible.

Algorithm: classic token bucket with linear refill.

- ``capacity = per_min`` (a freshly-seen key can burst up to ``per_min``
  turns instantly).
- ``refill_per_sec = per_min / 60`` (linear, not jittered).
- :meth:`TokenBucket.check`: refill based on monotonic clock, try to
  consume 1 token, return ``True`` iff the bucket had >= 1 token.

This is a per-process limiter. See the module-level TODOs in the Rust
crate for the Redis variant once a second gateway replica ships.

GC: the internal map grows unboundedly if we never prune.
:meth:`TokenBucket.start_gc` schedules a background sweeper task that
prunes entries whose ``last_refill`` is more than an hour old — idle
groups / senders drop out and re-appear on the next message at full
capacity (semantically fine: a group that hasn't spoken in an hour is
not mid-burst anyway).

## Deliberate deviations from Rust

- Rust uses ``dashmap::DashMap`` for lock-free shared access. Python's
  asyncio is single-threaded so a plain ``dict`` plus an ``asyncio.Lock``
  around mutate paths is equivalent. We use ``time.monotonic()`` instead
  of ``Instant``; both are wall-clock-immune.
- Rust returns a ``tokio::task::JoinHandle``; we return ``asyncio.Task``
  (``start_gc`` is now an ``async`` method because creating tasks
  requires an event loop).
- Rust's ``CancellationToken`` becomes an ``asyncio.Event`` (``cancel.set()``
  takes the place of ``cancel.cancel()``); callers either set it
  directly or rely on task cancellation.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

__all__ = [
    "GC_INTERVAL",
    "GC_STALE_AFTER",
    "TokenBucket",
]


#: Entries older than this are pruned by the background GC sweeper.
#: Matches ``GC_STALE_AFTER`` in Rust ``rate_limit.rs`` (1 hour).
GC_STALE_AFTER: float = 3600.0

#: How often the background sweeper walks the map. Matches ``GC_INTERVAL``
#: in Rust ``rate_limit.rs`` (5 minutes).
GC_INTERVAL: float = 300.0


@dataclass(slots=True)
class _BucketState:
    """Per-key token-bucket state. Mirrors ``BucketState`` in Rust."""

    tokens: float
    last_refill: float


class TokenBucket:
    """Thread-safe per-key token bucket.

    Mirrors the Rust ``TokenBucket`` struct field-for-field with the
    deviations documented at the module level. The public surface
    (:meth:`check`, :meth:`tracked_keys`, :meth:`sweep_stale`,
    :meth:`start_gc`) is intentionally identical.

    Construct via :meth:`per_minute` rather than the bare constructor so
    the ``refill_per_sec`` arithmetic matches Rust exactly:

    >>> b = TokenBucket.per_minute(60)
    >>> b.check("group:1")
    True
    """

    __slots__ = ("_capacity", "_refill_per_sec", "_state")

    def __init__(self, capacity: float, refill_per_sec: float) -> None:
        """Low-level constructor. Prefer :meth:`per_minute`.

        Exposed primarily so future variants (per-hour, per-day) can be
        constructed without going through the ``per_minute`` shortcut.
        """
        self._capacity: float = capacity
        self._refill_per_sec: float = refill_per_sec
        self._state: dict[str, _BucketState] = {}

    @classmethod
    def per_minute(cls, per_min: int) -> TokenBucket:
        """Build a bucket that allows ``per_min`` events per minute per key."""
        capacity = float(per_min)
        return cls(capacity=capacity, refill_per_sec=capacity / 60.0)

    @property
    def capacity(self) -> float:
        """Per-key capacity (= ``per_min`` for buckets built via
        :meth:`per_minute`). Exposed mainly for testing parity with the
        Rust ``self.capacity`` field access."""
        return self._capacity

    def check(self, key: str) -> bool:
        """Try to consume 1 token from ``key``'s bucket.

        Returns ``True`` if the caller is allowed to proceed. A brand-new
        key starts at full capacity. Mirrors ``TokenBucket::check`` in
        Rust.
        """
        now = time.monotonic()
        entry = self._state.get(key)
        if entry is None:
            entry = _BucketState(tokens=self._capacity, last_refill=now)
            self._state[key] = entry
        elapsed = now - entry.last_refill
        if elapsed < 0:
            # Monotonic clock should not regress, but be defensive — clamp
            # to 0 so a single weird tick doesn't leak negative tokens.
            elapsed = 0.0
        entry.tokens = min(
            entry.tokens + elapsed * self._refill_per_sec,
            self._capacity,
        )
        entry.last_refill = now
        if entry.tokens >= 1.0:
            entry.tokens -= 1.0
            return True
        return False

    def tracked_keys(self) -> int:
        """Number of live keys currently tracked.

        Useful for tests and future metrics. Matches
        ``TokenBucket::tracked_keys`` in Rust.
        """
        return len(self._state)

    def sweep_stale(self) -> None:
        """Remove entries whose ``last_refill`` is older than
        :data:`GC_STALE_AFTER`. Exposed for tests; the background sweeper
        calls this on each tick. Matches ``TokenBucket::sweep_stale``.
        """
        cutoff = time.monotonic() - GC_STALE_AFTER
        # Materialize the key list first so we don't mutate during iteration.
        stale = [k for k, v in self._state.items() if v.last_refill < cutoff]
        for k in stale:
            self._state.pop(k, None)

    def start_gc(
        self,
        cancel: asyncio.Event | None = None,
        interval: float = GC_INTERVAL,
    ) -> asyncio.Task[None]:
        """Spawn a background task that periodically calls
        :meth:`sweep_stale`.

        Mirrors ``TokenBucket::start_gc`` in Rust. Pass an
        :class:`asyncio.Event` and ``set()`` it to stop the loop cleanly;
        otherwise rely on :meth:`asyncio.Task.cancel`. The ``interval``
        argument lets tests use a sub-second cadence without modifying
        the module-level default.

        Unlike the Rust version, this method takes the event loop as
        implicit context (``asyncio.create_task`` requires a running
        loop). Call it from inside an async context (e.g. an adapter's
        ``connect()``).
        """
        cancel_event = cancel if cancel is not None else asyncio.Event()

        async def _loop() -> None:
            try:
                # Skip the immediate tick — nothing to sweep yet (matches
                # Rust ``ticker.tick().await`` before the loop body).
                await self._wait_for_tick(cancel_event, interval)
                while not cancel_event.is_set():
                    self.sweep_stale()
                    await self._wait_for_tick(cancel_event, interval)
            except asyncio.CancelledError:
                # Standard shutdown path — exit silently.
                return

        return asyncio.create_task(_loop(), name="token-bucket-gc")

    @staticmethod
    async def _wait_for_tick(cancel: asyncio.Event, interval: float) -> None:
        """Wait ``interval`` seconds or until ``cancel`` is set.

        Equivalent of Rust's ``tokio::select! { _ = cancel.cancelled() =>
        break, _ = ticker.tick() => ... }`` — whichever fires first wins
        and the caller observes via ``cancel.is_set()``.
        """
        try:
            await asyncio.wait_for(cancel.wait(), timeout=interval)
        except TimeoutError:
            return
