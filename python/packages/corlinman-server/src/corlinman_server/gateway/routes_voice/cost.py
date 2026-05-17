"""Cost-gating primitives for the ``/voice`` route.

Direct Python port of
``rust/crates/corlinman-gateway/src/routes/voice/cost.rs``. Three
layers, in priority order:

1. **Feature flag** — enforced upstream by the route handler in
   :mod:`.mod`.
2. **Per-tenant daily minutes budget** — a session-start check refuses
   ``budget_minutes_per_tenant_per_day`` overage with HTTP 429 /
   ``budget_exhausted``. Mid-session, a 1-Hz ticker drives
   :meth:`SessionMeter.poll` which transitions through ``Ok`` →
   ``BudgetWarn`` (60 s before cap) → ``Terminate`` (kill).
3. **Hard kill at session length cap** — a per-session timer
   independent of the daily budget. Defends against a stuck session no
   client has the courtesy to end.

The Rust code reads ``VoiceConfig`` from ``corlinman-core::config``;
the Python side has no equivalent shared config crate, so this module
defines a minimal :class:`VoiceConfig` dataclass with the same fields
the cost gate reads. The route-handler / lifecycle integration in
``corlinman_server`` already owns the live config; the voice handler
will build a :class:`VoiceConfig` from whatever shape it lands as.
"""

from __future__ import annotations

import math
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from typing import Final, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Minimal VoiceConfig — fields the gate reads
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VoiceConfig:
    """Subset of the Rust ``corlinman_core::config::VoiceConfig`` the
    voice cost gate consumes.

    The Rust struct has more fields (``provider_alias``,
    ``sample_rate_hz_in``, ``retain_audio`` …) the WebSocket session
    driver in :mod:`.mod` reaches for; we keep them all on the same
    dataclass for parity, but the cost-gate logic only ever touches
    ``enabled``, ``budget_minutes_per_tenant_per_day`` and
    ``max_session_seconds``.
    """

    enabled: bool = False
    budget_minutes_per_tenant_per_day: int = 0
    max_session_seconds: int = 0
    provider_alias: str = ""
    sample_rate_hz_in: int = 16_000
    sample_rate_hz_out: int = 24_000
    retain_audio: bool = False


# ---------------------------------------------------------------------------
# DaySpend — single-day per-tenant bucket
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DaySpend:
    """A single day's spend bucket per tenant. We track seconds (not
    minutes) so partial-minute drift doesn't accumulate; the budget is
    expressed in minutes and converted at check time.

    ``day_epoch`` — days since UNIX epoch (UTC). Used as the bucket key
    so the counter rolls over at midnight UTC without an explicit reset.
    """

    day_epoch: int
    seconds_used: int = 0
    sessions_count: int = 0

    @classmethod
    def fresh(cls, day_epoch: int) -> DaySpend:
        return cls(day_epoch=day_epoch, seconds_used=0, sessions_count=0)


# ---------------------------------------------------------------------------
# VoiceSpend trait surface
# ---------------------------------------------------------------------------


@runtime_checkable
class VoiceSpend(Protocol):
    """Trait surface for spend accounting. The default in-process impl
    is :class:`InMemoryVoiceSpend`; a SQLite-backed swap can land
    behind the same Protocol without touching the route handler.

    All methods are synchronous: the in-memory map is contended for at
    most a few times per session start / per second per session, so an
    async surface would be pure overhead.
    """

    def snapshot(self, tenant: str, day_epoch: int) -> DaySpend: ...

    def record_session_start(self, tenant: str, day_epoch: int) -> DaySpend: ...

    def add_seconds(self, tenant: str, day_epoch: int, seconds: int) -> DaySpend: ...


class InMemoryVoiceSpend:
    """Process-local spend store. Single-tenant deployments and tests
    use this; multi-tenant production swaps to a SQLite-backed impl
    behind the :class:`VoiceSpend` Protocol.
    """

    def __init__(self) -> None:
        self._inner: dict[tuple[str, int], DaySpend] = {}
        # Mirrors the Rust ``Mutex`` so concurrent gateway tasks don't
        # double-bill on a tick race.
        self._lock = threading.Lock()

    def snapshot(self, tenant: str, day_epoch: int) -> DaySpend:
        with self._lock:
            return self._inner.get((tenant, day_epoch), DaySpend.fresh(day_epoch))

    def record_session_start(self, tenant: str, day_epoch: int) -> DaySpend:
        with self._lock:
            key = (tenant, day_epoch)
            entry = self._inner.get(key, DaySpend.fresh(day_epoch))
            entry = replace(entry, sessions_count=entry.sessions_count + 1)
            self._inner[key] = entry
            return entry

    def add_seconds(self, tenant: str, day_epoch: int, seconds: int) -> DaySpend:
        # `saturating_add` in Rust; Python ints are unbounded but we
        # still clamp negatives for safety (a buggy caller mustn't
        # decrement the meter).
        if seconds < 0:
            seconds = 0
        with self._lock:
            key = (tenant, day_epoch)
            entry = self._inner.get(key, DaySpend.fresh(day_epoch))
            entry = replace(entry, seconds_used=entry.seconds_used + seconds)
            self._inner[key] = entry
            return entry


# ---------------------------------------------------------------------------
# Budget check (session-start)
# ---------------------------------------------------------------------------


class BudgetDenyReason:
    """Reasons :func:`evaluate_budget` may refuse a session at start.

    Implemented as a plain class with two construction helpers (mirror
    of the Rust ``enum BudgetDenyReason`` with payloads). The
    discriminator is :attr:`kind`; payload fields are populated per
    variant.
    """

    DAY_BUDGET_EXHAUSTED: Final[str] = "day_budget_exhausted"
    BUDGET_IS_ZERO: Final[str] = "budget_is_zero"

    __slots__ = ("kind", "used_seconds", "cap_seconds")

    def __init__(
        self,
        kind: str,
        *,
        used_seconds: int = 0,
        cap_seconds: int = 0,
    ) -> None:
        self.kind = kind
        self.used_seconds = used_seconds
        self.cap_seconds = cap_seconds

    @classmethod
    def day_budget_exhausted(cls, used_seconds: int, cap_seconds: int) -> BudgetDenyReason:
        return cls(
            cls.DAY_BUDGET_EXHAUSTED,
            used_seconds=used_seconds,
            cap_seconds=cap_seconds,
        )

    @classmethod
    def budget_is_zero(cls) -> BudgetDenyReason:
        return cls(cls.BUDGET_IS_ZERO)

    def __repr__(self) -> str:  # pragma: no cover — debug aid
        if self.kind == self.DAY_BUDGET_EXHAUSTED:
            return (
                f"BudgetDenyReason.day_budget_exhausted("
                f"used_seconds={self.used_seconds}, "
                f"cap_seconds={self.cap_seconds})"
            )
        return "BudgetDenyReason.budget_is_zero()"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, BudgetDenyReason):
            return NotImplemented
        if self.kind != other.kind:
            return False
        if self.kind == self.DAY_BUDGET_EXHAUSTED:
            return (
                self.used_seconds == other.used_seconds
                and self.cap_seconds == other.cap_seconds
            )
        return True

    def __hash__(self) -> int:
        return hash((self.kind, self.used_seconds, self.cap_seconds))


@dataclass(frozen=True)
class BudgetDecision:
    """Outcome of a session-start budget check.

    * ``allowed=True`` → session may proceed; ``seconds_remaining``
      tells the caller whether to also schedule a ``budget_warning``
      near the cap.
    * ``allowed=False`` → session refused at start; ``reason``
      explains why and ``reset_at`` is the UNIX seconds at which the
      day-budget resets (next UTC midnight).
    """

    allowed: bool
    seconds_remaining: int = 0
    reason: BudgetDenyReason | None = None
    reset_at: int = 0

    @classmethod
    def allow(cls, seconds_remaining: int) -> BudgetDecision:
        return cls(allowed=True, seconds_remaining=seconds_remaining)

    @classmethod
    def deny(cls, reason: BudgetDenyReason, reset_at: int) -> BudgetDecision:
        return cls(allowed=False, reason=reason, reset_at=reset_at)


def evaluate_budget(
    cfg: VoiceConfig, today: DaySpend, next_midnight_unix_secs: int
) -> BudgetDecision:
    """Pure budget check. Decoupled from the spend store so a
    SQLite-backed impl can reuse the same logic against a snapshot."""
    cap_minutes = max(int(cfg.budget_minutes_per_tenant_per_day), 0)
    if cap_minutes == 0:
        return BudgetDecision.deny(
            BudgetDenyReason.budget_is_zero(), next_midnight_unix_secs
        )
    cap_seconds = cap_minutes * 60
    if today.seconds_used >= cap_seconds:
        return BudgetDecision.deny(
            BudgetDenyReason.day_budget_exhausted(
                used_seconds=today.seconds_used, cap_seconds=cap_seconds
            ),
            next_midnight_unix_secs,
        )
    return BudgetDecision.allow(cap_seconds - today.seconds_used)


# ---------------------------------------------------------------------------
# Mid-session ticker — per-session second counter
# ---------------------------------------------------------------------------


class TerminateReason:
    """Why :class:`SessionMeter` says "kill this session"."""

    DAY_BUDGET_EXHAUSTED: Final[str] = "day_budget_exhausted"
    MAX_SESSION_SECONDS: Final[str] = "max_session_seconds"


class MeterTick:
    """Outcome of a single :meth:`SessionMeter.poll`. Mirrors the Rust
    ``MeterTick`` enum (``Ok`` / ``BudgetWarn`` / ``Terminate``).

    Implemented as a plain class with construction helpers and a
    discriminating :attr:`kind`. Use the class constants for cheap
    pattern-match equivalents.
    """

    OK: Final[str] = "ok"
    BUDGET_WARN: Final[str] = "budget_warn"
    TERMINATE: Final[str] = "terminate"

    __slots__ = ("kind", "minutes_remaining", "reason", "close_code")

    def __init__(
        self,
        kind: str,
        *,
        minutes_remaining: int | None = None,
        reason: str | None = None,
        close_code: int | None = None,
    ) -> None:
        self.kind = kind
        self.minutes_remaining = minutes_remaining
        self.reason = reason
        self.close_code = close_code

    @classmethod
    def ok(cls) -> MeterTick:
        return cls(cls.OK)

    @classmethod
    def budget_warn(cls, minutes_remaining: int) -> MeterTick:
        return cls(cls.BUDGET_WARN, minutes_remaining=minutes_remaining)

    @classmethod
    def terminate(cls, reason: str, close_code: int) -> MeterTick:
        return cls(cls.TERMINATE, reason=reason, close_code=close_code)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, MeterTick):
            return NotImplemented
        return (
            self.kind == other.kind
            and self.minutes_remaining == other.minutes_remaining
            and self.reason == other.reason
            and self.close_code == other.close_code
        )

    def __hash__(self) -> int:
        return hash((self.kind, self.minutes_remaining, self.reason, self.close_code))

    def __repr__(self) -> str:  # pragma: no cover — debug aid
        if self.kind == self.OK:
            return "MeterTick.ok()"
        if self.kind == self.BUDGET_WARN:
            return f"MeterTick.budget_warn(minutes_remaining={self.minutes_remaining})"
        return (
            f"MeterTick.terminate(reason={self.reason!r}, close_code={self.close_code})"
        )


CLOSE_CODE_BUDGET: Final[int] = 4002
"""WebSocket close code for "day budget exhausted mid-session"."""

CLOSE_CODE_MAX_SESSION: Final[int] = 4001
"""WebSocket close code for "session length cap reached"."""


@dataclass
class SessionMeter:
    """Per-session meter. Holds the wall-clock start, the latest budget
    snapshot at session-start, and the configured caps. The ticker
    calls :meth:`poll` every ~1 s; each call returns a
    :class:`MeterTick`.

    Rust uses ``std::time::Instant`` (monotonic). Python's
    :func:`time.monotonic` is the equivalent; we keep all timestamps as
    float seconds rather than ``timedelta`` so the arithmetic stays a
    direct mirror of the Rust ``u64`` seconds path.
    """

    started_at: float  # monotonic seconds, from time.monotonic()
    cap_seconds: int
    max_session_seconds: int
    start_seconds_used: int
    warn_fired: bool = False
    warn_at_elapsed: int | None = None

    @classmethod
    def start(
        cls,
        cfg: VoiceConfig,
        start_seconds_used: int,
        started_at: float,
    ) -> SessionMeter:
        cap_seconds = max(int(cfg.budget_minutes_per_tenant_per_day), 0) * 60
        max_session_seconds = max(int(cfg.max_session_seconds), 0)
        # Per design: warn 60 s before the day-budget cap.
        warn_at_elapsed: int | None = None
        day_remaining = cap_seconds - start_seconds_used
        if day_remaining > 0:
            candidate = day_remaining - 60
            if candidate > 0:
                warn_at_elapsed = candidate
        return cls(
            started_at=started_at,
            cap_seconds=cap_seconds,
            max_session_seconds=max_session_seconds,
            start_seconds_used=start_seconds_used,
            warn_fired=False,
            warn_at_elapsed=warn_at_elapsed,
        )

    def elapsed_secs(self, now: float) -> int:
        """Elapsed wall-clock seconds since ``started_at`` (monotonic).
        Rust's ``saturating_duration_since`` clamps to zero; Python
        :func:`max` does the same.
        """
        return max(int(now - self.started_at), 0)

    def poll(self, now: float) -> MeterTick:
        """Drive the meter at ``now``. Must be called by the per-session
        ~1 Hz ticker."""
        elapsed = self.elapsed_secs(now)

        # 1. Hard kill — independent of day budget.
        if self.max_session_seconds > 0 and elapsed >= self.max_session_seconds:
            return MeterTick.terminate(
                reason=TerminateReason.MAX_SESSION_SECONDS,
                close_code=CLOSE_CODE_MAX_SESSION,
            )

        # 2. Day budget cap.
        day_used = self.start_seconds_used + elapsed
        if self.cap_seconds > 0 and day_used >= self.cap_seconds:
            return MeterTick.terminate(
                reason=TerminateReason.DAY_BUDGET_EXHAUSTED,
                close_code=CLOSE_CODE_BUDGET,
            )

        # 3. One-shot warn.
        if (
            not self.warn_fired
            and self.warn_at_elapsed is not None
            and elapsed >= self.warn_at_elapsed
        ):
            self.warn_fired = True
            remaining_seconds = max(self.cap_seconds - day_used, 0)
            # Rust uses `div_ceil(60)`; Python equivalent below.
            minutes_remaining = math.ceil(remaining_seconds / 60) if remaining_seconds > 0 else 0
            return MeterTick.budget_warn(minutes_remaining=minutes_remaining)

        return MeterTick.ok()


# ---------------------------------------------------------------------------
# UTC day epoch helper
# ---------------------------------------------------------------------------


_SECONDS_PER_DAY: Final[int] = 86_400


def utc_day_epoch(unix_secs: int) -> int:
    """Days since UNIX epoch in UTC. The voice spend bucket key."""
    return int(unix_secs) // _SECONDS_PER_DAY


def next_utc_midnight(unix_secs: int) -> int:
    """Next UTC midnight as a UNIX timestamp; emitted as ``reset_at``
    in the 429 budget-exhausted body."""
    return (utc_day_epoch(unix_secs) + 1) * _SECONDS_PER_DAY


def now_unix_secs() -> int:
    """Wall-clock UNIX seconds. Pulled out so tests can monkey-patch."""
    return int(time.time())


__all__ = [
    "VoiceConfig",
    "DaySpend",
    "VoiceSpend",
    "InMemoryVoiceSpend",
    "BudgetDecision",
    "BudgetDenyReason",
    "evaluate_budget",
    "MeterTick",
    "TerminateReason",
    "SessionMeter",
    "CLOSE_CODE_BUDGET",
    "CLOSE_CODE_MAX_SESSION",
    "utc_day_epoch",
    "next_utc_midnight",
    "now_unix_secs",
]
