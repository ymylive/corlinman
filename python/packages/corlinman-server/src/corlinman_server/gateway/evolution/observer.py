"""``EvolutionObserver`` — passive watcher over the hook bus.

Port of :rust:`corlinman_gateway::evolution_observer`.

Subscribes to the shared :class:`corlinman_hooks.HookBus`, adapts a
curated subset of :class:`corlinman_hooks.HookEvent` variants into
:class:`corlinman_evolution_store.EvolutionSignal` rows, and persists
them via :class:`corlinman_evolution_store.SignalsRepo`. The Rust
design lives in ``docs/design/auto-evolution.md`` §4.1; the adapter
mapping below mirrors that doc 1:1.

Adapter mapping
---------------

================================ =========================================
Design name                      Hook variant
================================ =========================================
``tool.call.failed``             ``ToolCalled`` with ``ok = False`` and
                                 ``error_code != "timeout"``
``tool.call.timeout``            ``ToolCalled`` with ``ok = False`` and
                                 ``error_code == "timeout"``
``approval.rejected``            ``ApprovalDecided`` with
                                 ``decision != "allow"``
``engine.run.completed``         ``EngineRunCompleted``
``engine.run.failed``            ``EngineRunFailed``
``subagent.spawned``             ``SubagentSpawned``
``subagent.completed``           ``SubagentCompleted``
``subagent.timed_out``           ``SubagentTimedOut``
``subagent.depth_capped``        ``SubagentDepthCapped``
``session.ended``                *no equivalent on the bus today — skipped*
================================ =========================================

Anything not listed above falls through :func:`adapt` as ``None`` so
adding new mappings later is purely additive.

Backpressure
------------

The hook subscription drains into a bounded ``asyncio.Queue`` sized by
:attr:`EvolutionObserverConfig.queue_capacity`. When the queue is full
the *oldest* queued row is evicted (so the freshest context is what
gets persisted on a sustained burst) and a debug log is emitted (the
Rust crate also bumps a Prometheus counter; the Python port leaves
that hook open as a TODO since the gateway hasn't ported its metric
registry yet).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from corlinman_evolution_store import (
    EvolutionSignal,
    SignalSeverity,
    SignalsRepo,
)
from corlinman_hooks import HookBus, HookEvent, HookPriority
from corlinman_hooks.error import Closed, Lagged

__all__ = [
    "EvolutionObserver",
    "EvolutionObserverConfig",
    "adapt",
    "now_ms",
]


log = logging.getLogger(__name__)


# ─── Config ───────────────────────────────────────────────────────────


@dataclass
class EvolutionObserverConfig:
    """Knobs the observer accepts. Mirrors the slice of
    :rust:`corlinman_core::config::EvolutionObserverConfig` the
    observer actually consumes."""

    queue_capacity: int = 256
    """Max in-flight signal queue depth. Mirrors the Rust default."""


# ─── Observer ─────────────────────────────────────────────────────────


class EvolutionObserver:
    """Subscriber + writer pair as a single async task pair.

    Construct + call :meth:`start` to spawn the two background tasks
    (subscriber loop reading from the bus, writer loop draining into
    the SignalsRepo). :meth:`stop` cancels both cooperatively and
    awaits drain.

    The Rust crate exposes a single ``spawn`` free function returning a
    join handle for the writer task; the Python port wraps that pattern
    in a class so callers can hold a stop handle and the test surface
    can ``await observer.stop()`` deterministically.
    """

    def __init__(
        self,
        bus: HookBus,
        repo: SignalsRepo,
        cfg: EvolutionObserverConfig | None = None,
    ) -> None:
        self._bus = bus
        self._repo = repo
        self._cfg = cfg or EvolutionObserverConfig()
        capacity = max(1, self._cfg.queue_capacity)
        self._queue: asyncio.Queue[EvolutionSignal] = asyncio.Queue(maxsize=capacity)
        self._subscriber_task: asyncio.Task[None] | None = None
        self._writer_task: asyncio.Task[None] | None = None
        self._closing = asyncio.Event()

    def start(self) -> None:
        """Spawn subscriber + writer tasks. Idempotent — re-calling
        after :meth:`stop` is a no-op (the existing tasks are already
        cancelled and won't be revived)."""
        if self._subscriber_task is not None:
            return
        self._subscriber_task = asyncio.create_task(
            self._run_subscriber(),
            name="evolution-observer-subscriber",
        )
        self._writer_task = asyncio.create_task(
            self._run_writer(),
            name="evolution-observer-writer",
        )

    async def stop(self) -> None:
        """Cooperative shutdown: close the subscriber, drain the queue,
        await the writer. Idempotent."""
        self._closing.set()
        # Subscriber: cancel directly — the bus's ``Closed`` arm in
        # the Rust crate handles the equivalent termination.
        if self._subscriber_task is not None:
            self._subscriber_task.cancel()
            try:
                await self._subscriber_task
            except (asyncio.CancelledError, BaseException):  # noqa: BLE001
                pass
        # Writer: feed it a sentinel-free shutdown via the closing
        # event + the queue empties naturally because no producer is
        # left. We wait for it to drain.
        if self._writer_task is not None:
            try:
                await self._writer_task
            except (asyncio.CancelledError, BaseException):  # noqa: BLE001
                pass

    # ─── Internals ────────────────────────────────────────────────

    async def _run_subscriber(self) -> None:
        """Read from the bus on the LOW tier (same as Rust), adapt
        events, push into the bounded queue with oldest-eviction
        backpressure."""
        sub = self._bus.subscribe(HookPriority.LOW)
        try:
            while not self._closing.is_set():
                try:
                    event = await sub.recv()
                except Lagged as err:
                    # The LOW tier broadcast channel ran ahead of us;
                    # we missed ``err.count`` events. Mirror the Rust
                    # ``Lagged(n)`` branch: count them as drops and
                    # log + continue.
                    log.warning(
                        "evolution_observer.lagged dropped=%s",
                        getattr(err, "count", 1),
                    )
                    continue
                except Closed:
                    log.debug("evolution_observer.subscriber_closed")
                    return
                except asyncio.CancelledError:
                    return
                signal = adapt(event)
                if signal is None:
                    continue
                await self._enqueue_with_eviction(signal)
        finally:
            # Mirror the Rust ``drop(tx)`` — closing the queue lets
            # the writer drain and exit. We use the closing event +
            # an explicit sentinel via empty-queue + closing check.
            self._closing.set()

    async def _run_writer(self) -> None:
        """Drain the queue into :class:`SignalsRepo.insert`. Lives as
        long as the subscriber is running; exits cleanly once the
        subscriber sets ``closing`` and the queue is empty.
        """
        while True:
            if self._closing.is_set() and self._queue.empty():
                log.debug("evolution_observer.writer_drained")
                return
            try:
                # ``wait_for`` so we poll the closing flag without
                # busy-looping. The 0.1s tick is fine — the observer
                # is not a hot path.
                signal = await asyncio.wait_for(self._queue.get(), timeout=0.1)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                return
            try:
                await self._repo.insert(signal)
            except Exception as err:  # noqa: BLE001 — log + drop
                log.warning(
                    "evolution_observer.write_failed event_kind=%s err=%s",
                    signal.event_kind,
                    err,
                )

    async def _enqueue_with_eviction(self, signal: EvolutionSignal) -> None:
        """Push a signal into the queue, evicting the *oldest* entry
        on overflow. Mirrors :rust:`enqueue_with_eviction` —
        ``try_send`` + on ``Full`` pop one and retry until the put
        succeeds. The eviction path doesn't block because both the
        ``get_nowait`` and ``put_nowait`` paths are non-awaiting.
        """
        while True:
            try:
                self._queue.put_nowait(signal)
                return
            except asyncio.QueueFull:
                log.warning("evolution_observer.queue_full dropping_oldest")
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    # Writer raced us. Loop and try the put again.
                    pass
                # Loop and retry.


# ─── Adapter (pure function — easy to test) ──────────────────────────


def adapt(event: Any) -> EvolutionSignal | None:
    """Adapt one :class:`HookEvent` variant into an
    :class:`EvolutionSignal`. Returns ``None`` for events we don't
    track. Mirrors :rust:`adapt` line for line.

    Accepts ``Any`` so callers don't need to pre-check the variant
    type — ``isinstance`` dispatch below picks the matching arm.
    """
    if isinstance(event, HookEvent.ToolCalled):
        if event.ok:
            return None
        is_timeout = event.error_code == "timeout"
        event_kind = "tool.call.timeout" if is_timeout else "tool.call.failed"
        severity = SignalSeverity.WARN if is_timeout else SignalSeverity.ERROR
        payload: dict[str, Any] = {
            "tool": event.tool,
            "runner_id": event.runner_id,
            "duration_ms": event.duration_ms,
            "ok": event.ok,
            "error_code": event.error_code,
        }
        return EvolutionSignal(
            event_kind=event_kind,
            severity=severity,
            payload_json=payload,
            target=event.tool,
            observed_at=now_ms(),
            tenant_id=event.tenant_id or "default",
        )

    if isinstance(event, HookEvent.ApprovalDecided):
        if event.decision == "allow":
            return None
        payload = {
            "id": event.id,
            "decision": event.decision,
            "decider": event.decider,
            "decided_at_ms": event.decided_at_ms,
        }
        return EvolutionSignal(
            event_kind="approval.rejected",
            severity=SignalSeverity.WARN,
            payload_json=payload,
            target=event.id,
            observed_at=now_ms(),
            tenant_id=event.tenant_id or "default",
        )

    if isinstance(event, HookEvent.EngineRunCompleted):
        payload = {
            "run_id": event.run_id,
            "proposals_generated": event.proposals_generated,
            "duration_ms": event.duration_ms,
        }
        return EvolutionSignal(
            event_kind="engine.run.completed",
            severity=SignalSeverity.INFO,
            payload_json=payload,
            target=event.run_id,
            observed_at=now_ms(),
            tenant_id="default",
        )

    if isinstance(event, HookEvent.EngineRunFailed):
        payload = {
            "run_id": event.run_id,
            "error_kind": event.error_kind,
            "exit_code": event.exit_code,
        }
        return EvolutionSignal(
            event_kind="engine.run.failed",
            severity=SignalSeverity.ERROR,
            payload_json=payload,
            target=event.run_id,
            observed_at=now_ms(),
            tenant_id="default",
        )

    if isinstance(event, HookEvent.SubagentSpawned):
        payload = {
            "parent_session_key": event.parent_session_key,
            "child_session_key": event.child_session_key,
            "child_agent_id": event.child_agent_id,
            "agent_card": event.agent_card,
            "depth": event.depth,
            "parent_trace_id": event.parent_trace_id,
        }
        return EvolutionSignal(
            event_kind="subagent.spawned",
            severity=SignalSeverity.INFO,
            payload_json=payload,
            target=event.child_agent_id,
            trace_id=event.parent_trace_id,
            session_id=event.child_session_key,
            observed_at=now_ms(),
            tenant_id=event.tenant_id,
        )

    if isinstance(event, HookEvent.SubagentCompleted):
        # Mirror Rust severity-by-finish_reason split.
        if event.finish_reason == "error":
            severity = SignalSeverity.ERROR
        elif event.finish_reason == "length":
            severity = SignalSeverity.WARN
        else:
            severity = SignalSeverity.INFO
        payload = {
            "parent_session_key": event.parent_session_key,
            "child_session_key": event.child_session_key,
            "child_agent_id": event.child_agent_id,
            "finish_reason": event.finish_reason,
            "elapsed_ms": event.elapsed_ms,
            "tool_calls_made": event.tool_calls_made,
            "parent_trace_id": event.parent_trace_id,
        }
        return EvolutionSignal(
            event_kind="subagent.completed",
            severity=severity,
            payload_json=payload,
            target=event.child_agent_id,
            trace_id=event.parent_trace_id,
            session_id=event.child_session_key,
            observed_at=now_ms(),
            tenant_id=event.tenant_id,
        )

    if isinstance(event, HookEvent.SubagentTimedOut):
        payload = {
            "parent_session_key": event.parent_session_key,
            "child_session_key": event.child_session_key,
            "child_agent_id": event.child_agent_id,
            "elapsed_ms": event.elapsed_ms,
            "parent_trace_id": event.parent_trace_id,
        }
        return EvolutionSignal(
            event_kind="subagent.timed_out",
            severity=SignalSeverity.WARN,
            payload_json=payload,
            target=event.child_agent_id,
            trace_id=event.parent_trace_id,
            session_id=event.child_session_key,
            observed_at=now_ms(),
            tenant_id=event.tenant_id,
        )

    if isinstance(event, HookEvent.SubagentDepthCapped):
        payload = {
            "parent_session_key": event.parent_session_key,
            "attempted_depth": event.attempted_depth,
            "reason": event.reason,
            "parent_trace_id": event.parent_trace_id,
        }
        return EvolutionSignal(
            event_kind="subagent.depth_capped",
            severity=SignalSeverity.WARN,
            payload_json=payload,
            target=event.parent_session_key,
            trace_id=event.parent_trace_id,
            # No child session was allocated — ``session_id`` falls
            # back to the parent's session_key so the engine's session-
            # clustering still finds the row. Mirrors the Rust comment.
            session_id=event.parent_session_key,
            observed_at=now_ms(),
            tenant_id=event.tenant_id,
        )

    return None


def now_ms() -> int:
    """Unix milliseconds — pulled out for tests (matches Rust ``now_ms``)."""
    return int(time.time() * 1000)
