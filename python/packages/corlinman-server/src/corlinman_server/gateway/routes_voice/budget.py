"""Mid-session budget enforcement + checkpoint ticker.

Direct Python port of
``rust/crates/corlinman-gateway/src/routes/voice/budget.rs``. Wraps the
pure-logic primitives in :mod:`.cost` into a single per-session
enforcer that:

1. Drives a 1-Hz tick loop the route handler uses as its budget polling
   cadence.
2. **Checkpoints in-flight seconds to the spend store on every tick**
   so a gateway crash mid-session can't leak unbilled minutes.
3. Returns a :class:`BudgetTickAction` enum the handler maps to
   server-side side-effects (continue / warn / terminate).
4. On graceful close, :meth:`BudgetEnforcer.finalize` flushes the last
   delta to the spend store so the day-budget total includes every
   second the session was alive.

Why a separate type? The :class:`SessionMeter` in :mod:`.cost` is pure
(no I/O). The enforcer adds the single-source-of-truth checkpoint
write so the spend store is always within ~1 s of the live session's
accumulated usage.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from corlinman_server.gateway.routes_voice.cost import (
    MeterTick,
    SessionMeter,
    TerminateReason,
    VoiceConfig,
    VoiceSpend,
)


@dataclass(frozen=True)
class BudgetTickAction:
    """Result of one :meth:`BudgetEnforcer.tick` call. Maps 1:1 onto
    the side effects the route handler must perform.

    Discriminator is :attr:`kind`; payload fields are populated per
    variant. Use :meth:`continue_`, :meth:`emit_warning`,
    :meth:`terminate` to construct.
    """

    kind: str
    minutes_remaining: int | None = None
    reason: str | None = None
    close_code: int | None = None

    CONTINUE: Final[str] = "continue"
    EMIT_WARNING: Final[str] = "emit_warning"
    TERMINATE: Final[str] = "terminate"

    @classmethod
    def continue_(cls) -> BudgetTickAction:
        return cls(kind=cls.CONTINUE)

    @classmethod
    def emit_warning(cls, minutes_remaining: int) -> BudgetTickAction:
        return cls(kind=cls.EMIT_WARNING, minutes_remaining=minutes_remaining)

    @classmethod
    def terminate(cls, reason: str, close_code: int) -> BudgetTickAction:
        return cls(kind=cls.TERMINATE, reason=reason, close_code=close_code)


class BudgetEnforcer:
    """Per-session budget enforcer. Owns the tenant slug + day_epoch
    (immutable for the session — a session that crosses midnight UTC
    keeps the same day_epoch so the day's row stays consistent), the
    :class:`SessionMeter` (pure timing logic), an :class:`VoiceSpend`
    handle for checkpoint writes, and the cumulative seconds
    already-billed-back-to-the-store so each tick only writes the delta
    from the previous tick.
    """

    def __init__(
        self,
        cfg: VoiceConfig,
        spend: VoiceSpend,
        tenant: str,
        day_epoch: int,
        started_at: float,
    ) -> None:
        snap = spend.snapshot(tenant, day_epoch)
        self._spend = spend
        self._tenant = tenant
        self._day_epoch = day_epoch
        self._meter = SessionMeter.start(cfg, snap.seconds_used, started_at)
        self._started_at = started_at
        self._last_checkpointed = 0

    # The Rust API uses an associated `start(cfg, spend, …)`
    # constructor; Python's __init__ does the same work. Keep an alias
    # to keep call-sites visually parallel to the Rust port.
    @classmethod
    def start(
        cls,
        cfg: VoiceConfig,
        spend: VoiceSpend,
        tenant: str,
        day_epoch: int,
        started_at: float,
    ) -> BudgetEnforcer:
        return cls(cfg, spend, tenant, day_epoch, started_at)

    def tick(self, now: float) -> BudgetTickAction:
        """Drive the enforcer at ``now``. Each call:

        1. Checkpoints any new elapsed seconds into the spend store
           (delta-only; never re-writes already-billed seconds).
        2. Polls the :class:`SessionMeter` to decide whether to emit a
           warning or terminate.
        """
        elapsed = self._meter.elapsed_secs(now)
        self._checkpoint_delta(elapsed)

        tick = self._meter.poll(now)
        if tick.kind == MeterTick.OK:
            return BudgetTickAction.continue_()
        if tick.kind == MeterTick.BUDGET_WARN:
            assert tick.minutes_remaining is not None
            return BudgetTickAction.emit_warning(tick.minutes_remaining)
        # MeterTick.TERMINATE
        assert tick.reason is not None
        assert tick.close_code is not None
        return BudgetTickAction.terminate(reason=tick.reason, close_code=tick.close_code)

    def finalize(self, now: float) -> int:
        """Force a final checkpoint flush at session close. Returns the
        total seconds attributed to this session — the caller writes
        this into ``voice_sessions.duration_secs``."""
        elapsed = self._meter.elapsed_secs(now)
        self._checkpoint_delta(elapsed)
        return elapsed

    def _checkpoint_delta(self, elapsed: int) -> None:
        delta = elapsed - self._last_checkpointed
        if delta <= 0:
            return
        self._spend.add_seconds(self._tenant, self._day_epoch, delta)
        self._last_checkpointed = elapsed

    def elapsed_secs(self, now: float) -> int:
        """Elapsed seconds since session start, rounded down. Exposed
        so the handler can stamp ``voice_sessions.duration_secs``
        without re-deriving it."""
        return self._meter.elapsed_secs(now)

    def started_at(self) -> float:
        """The wall-clock anchor — handed back to the handler for
        callers that need to compute their own elapsed math."""
        return self._started_at

    # Test seam — peek at the cumulative seconds already written to the
    # store. Mirrors the Rust `#[cfg(test)] pub(crate) last_checkpointed`.
    def last_checkpointed(self) -> int:
        return self._last_checkpointed


def terminate_reason_to_end_reason(reason: str) -> str:
    """Map a :class:`TerminateReason` value to its persisted
    ``voice_sessions.end_reason`` string."""
    if reason == TerminateReason.DAY_BUDGET_EXHAUSTED:
        return "budget"
    if reason == TerminateReason.MAX_SESSION_SECONDS:
        return "max_session"
    raise ValueError(f"unknown terminate reason '{reason}'")


def terminate_reason_to_message(reason: str) -> str:
    """Map a :class:`TerminateReason` value to a human-readable error
    message — the handler emits this verbatim in the final ``error``
    server frame."""
    if reason == TerminateReason.DAY_BUDGET_EXHAUSTED:
        return "daily voice budget exhausted; session terminated"
    if reason == TerminateReason.MAX_SESSION_SECONDS:
        return "session length cap reached; session terminated"
    raise ValueError(f"unknown terminate reason '{reason}'")


def terminate_reason_to_code(reason: str) -> str:
    """Map a :class:`TerminateReason` value to a stable error-code
    string. Clients pattern-match on the ``error.code`` JSON field."""
    if reason == TerminateReason.DAY_BUDGET_EXHAUSTED:
        return "budget_exhausted"
    if reason == TerminateReason.MAX_SESSION_SECONDS:
        return "max_session_reached"
    raise ValueError(f"unknown terminate reason '{reason}'")


__all__ = [
    "BudgetTickAction",
    "BudgetEnforcer",
    "terminate_reason_to_end_reason",
    "terminate_reason_to_message",
    "terminate_reason_to_code",
]
