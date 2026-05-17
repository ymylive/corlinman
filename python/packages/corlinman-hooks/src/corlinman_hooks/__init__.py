"""`corlinman-hooks` — cross-cutting event bus for the corlinman platform.

Python port of ``rust/crates/corlinman-hooks``. The public API mirrors
the Rust crate's prelude:

- :class:`HookBus` + :class:`HookSubscription` for fan-out delivery.
- :class:`HookEvent` (tagged union) for the wire-stable payload.
- :class:`HookPriority` for the three priority tiers.
- :class:`CancelToken` for cooperative cancellation.
- :class:`HookError` (with :class:`HookCancelledError`) for emit
  failures, plus :class:`RecvError` (:class:`Closed`, :class:`Lagged`)
  for subscriber-side failures.

Design highlights (matching the Rust crate):

- Three priority tiers (``CRITICAL`` < ``NORMAL`` < ``LOW``). ``emit``
  fans out in that order and yields between tiers so Critical
  subscribers always observe an event before Normal/Low do on a
  single-threaded asyncio runtime.
- Each subscriber gets its own bounded buffer (asyncio-flavoured analog
  of the Rust ``tokio::sync::broadcast`` channel). Dropped subscribers
  are transparent; slow subscribers see :class:`Lagged` and skip
  forward.
- :class:`CancelToken` is a cooperative flag: emitters check it and
  bail without publishing, so downstream listeners stop seeing new
  events once a shutdown/abort is signalled upstream.
"""

from __future__ import annotations

from corlinman_hooks.bus import HookBus, HookSubscription, register_hook
from corlinman_hooks.error import (
    Closed,
    HookCancelledError,
    HookError,
    Lagged,
    RecvError,
)
from corlinman_hooks.event import HookEvent
from corlinman_hooks.priority import CancelToken, HookPriority

__all__ = [
    "CancelToken",
    "Closed",
    "HookBus",
    "HookCancelledError",
    "HookError",
    "HookEvent",
    "HookPriority",
    "HookSubscription",
    "Lagged",
    "RecvError",
    "register_hook",
]
