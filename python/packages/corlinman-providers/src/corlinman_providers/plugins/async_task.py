"""Async task registry: pair ``task_id`` → :class:`asyncio.Future` for
``/plugin-callback/:task_id``.

Python port of ``rust/crates/corlinman-plugins/src/async_task.rs``. Async
plugins may return ``{"result": {"task_id": "tsk_..."}}``, which the stdio
runtime surfaces as :class:`~corlinman_providers.plugins.sandbox.PluginOutput`
with ``kind == "accepted_for_later"``. The gateway parks the tool_call until a
matching HTTP callback arrives (or a deadline elapses); this module owns the
park/complete map.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class CompleteError(StrEnum):
    """Error outcomes for :meth:`AsyncTaskRegistry.complete`."""

    NOT_FOUND = "task_not_found"
    WAITER_DROPPED = "waiter_dropped"


class AsyncTaskCompletionError(Exception):
    """Raised by :meth:`AsyncTaskRegistry.complete` on a non-success."""

    def __init__(self, reason: CompleteError) -> None:
        self.reason = reason
        super().__init__(reason.value)


@dataclass
class _Entry:
    fut: asyncio.Future[Any]
    registered_at: float = field(default_factory=time.monotonic)


class AsyncTaskRegistry:
    """Registry for async plugin tasks awaiting an HTTP callback.

    Cheap to share; every method is safe to call from any task.
    """

    def __init__(self) -> None:
        self._pending: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()
        self._sweep_task: asyncio.Task[None] | None = None

    def register(self, task_id: str) -> asyncio.Future[Any]:
        """Register a pending async task and obtain a future that resolves
        when :meth:`complete` is called for the same ``task_id``.

        If ``task_id`` is already registered, the previous future is
        cancelled (waking the old waiter with a :class:`asyncio.CancelledError`)
        and replaced. This is a defensive path for plugins that reuse ids.
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        old = self._pending.pop(task_id, None)
        if old is not None and not old.fut.done():
            old.fut.cancel()
        self._pending[task_id] = _Entry(fut=fut)
        return fut

    def complete(self, task_id: str, payload: Any) -> None:
        """Complete a pending task.

        :raises AsyncTaskCompletionError: with ``NOT_FOUND`` when no entry
            exists, or ``WAITER_DROPPED`` when the entry was removed but the
            future had already been cancelled / had its result consumed.
        """
        entry = self._pending.pop(task_id, None)
        if entry is None:
            raise AsyncTaskCompletionError(CompleteError.NOT_FOUND)
        if entry.fut.done() or entry.fut.cancelled():
            raise AsyncTaskCompletionError(CompleteError.WAITER_DROPPED)
        entry.fut.set_result(payload)

    def cancel(self, task_id: str) -> bool:
        """Remove a pending task without delivering a payload."""
        entry = self._pending.pop(task_id, None)
        if entry is None:
            return False
        if not entry.fut.done():
            entry.fut.cancel()
        return True

    def is_pending(self, task_id: str) -> bool:
        return task_id in self._pending

    def __len__(self) -> int:
        return len(self._pending)

    def is_empty(self) -> bool:
        return not self._pending

    def sweep_expired(self, ttl_seconds: float) -> int:
        """Drop pending entries older than ``ttl_seconds``. Returns the
        number evicted. Evicted futures are cancelled.
        """
        now = time.monotonic()
        expired = [
            k
            for k, entry in self._pending.items()
            if (now - entry.registered_at) > ttl_seconds
        ]
        count = 0
        for k in expired:
            entry = self._pending.pop(k, None)
            if entry is None:
                continue
            if not entry.fut.done():
                entry.fut.cancel()
            count += 1
        return count

    def start_sweep(
        self,
        *,
        interval_seconds: float,
        ttl_seconds: float,
    ) -> asyncio.Task[None]:
        """Spawn a background task that periodically calls
        :meth:`sweep_expired`. Returns the task handle so callers can abort
        it at shutdown.
        """

        async def _loop() -> None:
            # Skip the first immediate tick to mirror the Rust ticker
            # `tick().await` behaviour.
            try:
                while True:
                    await asyncio.sleep(interval_seconds)
                    self.sweep_expired(ttl_seconds)
            except asyncio.CancelledError:  # graceful shutdown
                return

        self._sweep_task = asyncio.create_task(_loop())
        return self._sweep_task


__all__ = [
    "AsyncTaskCompletionError",
    "AsyncTaskRegistry",
    "CompleteError",
]
