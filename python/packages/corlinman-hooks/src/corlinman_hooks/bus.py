"""`HookBus` + `HookSubscription`.

Mirrors ``rust/crates/corlinman-hooks/src/bus.rs``.

Internally, the bus holds a list of per-subscriber :class:`asyncio.Queue`
instances, one set per priority tier. :meth:`HookBus.emit` publishes in
strict tier order (Critical -> Normal -> Low) so Critical subscribers
always observe an event before lower-priority ones. Each tier is
awaited via :func:`asyncio.sleep(0)` between tiers to give subscribers
a scheduling opportunity, matching the Rust ``tokio::task::yield_now``
semantics.

The Rust crate is built on ``tokio::sync::broadcast``: every active
subscriber receives every published event, and slow subscribers see a
``Lagged(n)`` error when they fall behind. We replicate that by giving
each subscriber its own bounded queue and dropping the oldest item +
incrementing a per-subscriber "missed" counter when the queue is full.
The next :meth:`HookSubscription.recv` call surfaces the counter as a
:class:`Lagged` exception, then resumes normal delivery.
"""

from __future__ import annotations

import asyncio
import weakref
from collections import deque
from typing import TYPE_CHECKING

from corlinman_hooks.error import Closed, HookCancelledError, Lagged
from corlinman_hooks.priority import CancelToken, HookPriority

if TYPE_CHECKING:
    from corlinman_hooks.event import _HookEventBase

__all__ = ["HookBus", "HookSubscription"]


class HookSubscription:
    """A handle to one priority tier of the bus.

    Dropping it removes the slot from the bus's per-tier subscriber
    list (via a finaliser); other subscribers and the emitter are
    unaffected.

    The internal buffer is a bounded :class:`collections.deque` paired
    with an :class:`asyncio.Event` to signal availability. The deque is
    capped at the bus's ``capacity``; when the emitter publishes into
    a full buffer it discards the oldest event and increments
    :attr:`_lag`. The next :meth:`recv` call observes the non-zero lag
    and returns it as :class:`Lagged` before resuming normal delivery.
    """

    __slots__ = (
        "__weakref__",
        "_buffer",
        "_capacity",
        "_closed",
        "_event",
        "_lag",
        "_priority",
    )

    def __init__(
        self,
        priority: HookPriority,
        capacity: int,
        bus: HookBus,
    ) -> None:
        # The ``bus`` arg is accepted for symmetry with the Rust
        # constructor signature (subscription needs to know which bus
        # it belongs to in the Rust version because the broadcast
        # receiver is bound to a specific sender). The Python side does
        # the bookkeeping through the bus's own weak-ref list; we
        # don't need to retain a back-reference here.
        del bus
        self._priority = priority
        self._capacity = capacity
        self._buffer: deque[_HookEventBase] = deque()
        self._event = asyncio.Event()
        self._lag = 0
        self._closed = False

    @property
    def priority(self) -> HookPriority:
        return self._priority

    def _push(self, event: _HookEventBase) -> None:
        """Bus-internal: push an event into this subscriber's buffer.

        On overflow we drop the oldest item to match the
        ``broadcast::channel`` policy (slow subscribers see ``Lagged``
        rather than back-pressuring the emitter).
        """
        if self._closed:
            return
        if len(self._buffer) >= self._capacity:
            self._buffer.popleft()
            self._lag += 1
        self._buffer.append(event)
        self._event.set()

    def _close(self) -> None:
        """Bus-internal: mark this subscription closed (the bus is gone)."""
        self._closed = True
        self._event.set()

    async def recv(self) -> _HookEventBase:
        """Await the next event on this tier.

        Raises :class:`Lagged` if the subscriber fell behind since the
        last successful ``recv``. Raises :class:`Closed` if the bus
        has been garbage-collected and no events remain buffered.
        """
        if self._lag > 0:
            lag = self._lag
            self._lag = 0
            raise Lagged(lag)
        while not self._buffer:
            if self._closed:
                raise Closed()
            self._event.clear()
            # Re-check after clearing to avoid lost wakeups.
            if self._buffer or self._closed:
                continue
            await self._event.wait()
        return self._buffer.popleft()


class HookBus:
    """Cross-cutting event bus.

    Cheap to share: every subscriber holds its own bounded buffer, and
    the bus only retains weak references back to each subscription so
    dropped subscribers are GC'd automatically.

    Mirrors the Rust ``HookBus``. The Rust crate is ``Clone`` because
    the per-tier broadcast senders are themselves cloneable; in Python
    sharing the same instance by reference plays the same role.
    """

    __slots__ = (
        "_cancel",
        "_capacity",
        "_subscribers",
    )

    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        self._capacity = capacity
        # Per-tier list of weak refs to live subscriptions. Using weak
        # refs lets a subscriber's `__del__` implicitly remove it
        # without needing an explicit unsubscribe call.
        self._subscribers: dict[HookPriority, list[weakref.ReferenceType[HookSubscription]]] = {
            HookPriority.CRITICAL: [],
            HookPriority.NORMAL: [],
            HookPriority.LOW: [],
        }
        self._cancel = CancelToken()

    @property
    def capacity(self) -> int:
        return self._capacity

    def cancel_token(self) -> CancelToken:
        """Reference to the bus-wide cancel token.

        Flipping it stops future :meth:`emit` calls from publishing.
        Returned by reference (not a copy) so callers can either flip
        it themselves or hand the same instance to a shutdown signal.
        """
        return self._cancel

    def receiver_count(self, priority: HookPriority) -> int:
        """Number of live subscribers on ``priority``."""
        self._compact(priority)
        return len(self._subscribers[priority])

    def _compact(self, priority: HookPriority) -> None:
        """Drop dead weak refs from ``priority``'s subscriber list."""
        live = [ref for ref in self._subscribers[priority] if ref() is not None]
        self._subscribers[priority] = live

    def subscribe(self, priority: HookPriority) -> HookSubscription:
        """Subscribe to a priority tier.

        The subscription only sees events published to its tier, but
        tiers are fed in strict order by :meth:`emit`, so a Critical
        subscriber is guaranteed to observe the event before any
        Normal/Low subscriber on a single-threaded asyncio runtime.
        """
        sub = HookSubscription(priority, self._capacity, self)
        self._subscribers[priority].append(weakref.ref(sub))
        return sub

    def _fanout_tier(self, priority: HookPriority, event: _HookEventBase) -> None:
        """Push ``event`` into every live subscriber on ``priority``.

        Dead weak refs are collected lazily — a single pass either
        resolves the ref to push or appends nothing, then we compact at
        the end. Matching the Rust ``broadcast::send`` semantics, a
        tier with no subscribers is a no-op.
        """
        survivors: list[weakref.ReferenceType[HookSubscription]] = []
        for ref in self._subscribers[priority]:
            sub = ref()
            if sub is None:
                continue
            sub._push(event)
            survivors.append(ref)
        self._subscribers[priority] = survivors

    async def emit(self, event: _HookEventBase) -> None:
        """Emit in strict priority order.

        Raises :class:`HookCancelledError` if the cancel token has been
        flipped by the time we start (or between any two tiers). Having
        no subscribers on a tier is not an error; the send is a no-op.

        Between tiers we ``await asyncio.sleep(0)`` so subscribers on
        the just-published tier can drain before we publish to the next
        tier. This is what enforces the ordering guarantee on a
        single-threaded asyncio runtime.
        """
        if self._cancel.is_cancelled():
            raise HookCancelledError()
        for tier in HookPriority.ordered():
            if self._cancel.is_cancelled():
                raise HookCancelledError()
            self._fanout_tier(tier, event)
            # Yield so subscribers on this tier can drain before we
            # publish to the next tier.
            await asyncio.sleep(0)

    def emit_nonblocking(self, event: _HookEventBase) -> None:
        """Fire-and-forget variant. Never awaits.

        Useful from sync contexts (e.g. atexit, config-reload
        callbacks) where blocking on scheduler yields isn't possible.
        Skips the per-tier yield: all three tiers are fanned out
        immediately, so the strict per-tier observation ordering is
        not guaranteed from this entry point. Mirrors the Rust
        ``emit_nonblocking`` semantics.
        """
        if self._cancel.is_cancelled():
            return
        for tier in HookPriority.ordered():
            self._fanout_tier(tier, event)

    def __repr__(self) -> str:  # pragma: no cover — debug aid
        counts = {p.value: self.receiver_count(p) for p in HookPriority.ordered()}
        return f"HookBus(capacity={self._capacity}, subscribers={counts})"


# Backwards-friendly alias for the registration vocabulary used in the
# port spec ("register_hook"). The bus's subscription model is the
# Python-native form of "register a hook listener"; this helper makes
# the call site read more like the Rust crate's documentation prose.
def register_hook(bus: HookBus, priority: HookPriority = HookPriority.NORMAL) -> HookSubscription:
    """Register a hook listener at ``priority`` and return its subscription.

    Equivalent to ``bus.subscribe(priority)``. Provided to satisfy the
    "register / unregister" vocabulary in the port spec; the unregister
    side is implicit — drop the returned :class:`HookSubscription` and
    the bus's weak-ref-based bookkeeping cleans up on the next
    :meth:`HookBus.emit` or :meth:`HookBus.receiver_count` call.
    """
    return bus.subscribe(priority)


__all__ = ["HookBus", "HookSubscription", "register_hook"]
