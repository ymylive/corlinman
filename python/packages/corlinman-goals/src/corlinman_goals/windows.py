"""Tier window math + tier-derived ``target_date`` defaults.

Pure date arithmetic (no I/O) so the store, CLI, and reflection job can
all share one implementation. Two contracts the design pins:

- Windows are wall-clock UTC, not "rolling N days from now". Two operators
  asking "what's this week's score?" must mean the same range.
- ``target_date`` defaults are derived from tier: short → run-day midnight
  UTC; mid → following Monday midnight UTC; long → ``created_at + 90d``.
  The CLI calls into here so the operator never calculates an ISO week
  by hand.

All times are unix milliseconds. The ``now`` inputs are explicit so tests
can pin a deterministic clock instead of monkey-patching ``time.time``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from corlinman_goals.state import TIER_VALUES

# Window sizes in calendar units. Lifted from the design's "Tier windows"
# table; surfaced as constants so iter 8 (cascade) and iter 5 (reflection)
# can reuse the same numbers without re-deriving them.
SHORT_WINDOW_HOURS: Final[int] = 24
MID_WINDOW_DAYS: Final[int] = 7
LONG_WINDOW_DAYS: Final[int] = 90

_MS_PER_SECOND: Final[int] = 1000
_SECONDS_PER_DAY: Final[int] = 86_400
_SECONDS_PER_HOUR: Final[int] = 3600


@dataclass(frozen=True)
class Window:
    """One reflection window; both bounds in unix milliseconds.

    ``start_ms`` is inclusive, ``end_ms`` is exclusive — the standard
    half-open interval the SQLite `WHERE created_at >= ? AND created_at <
    ?` query plan expects.
    """

    start_ms: int
    end_ms: int


def _to_ms(dt: datetime) -> int:
    """Aware datetime → unix ms. Bare-naive datetimes raise; ambiguity
    is exactly the bug we're trying to avoid."""
    if dt.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return int(dt.timestamp() * _MS_PER_SECOND)


def _from_ms(ms: int) -> datetime:
    """Unix ms → UTC-aware datetime."""
    return datetime.fromtimestamp(ms / _MS_PER_SECOND, tz=UTC)


def _midnight_utc(dt: datetime) -> datetime:
    """Truncate to UTC midnight on the same calendar day."""
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _next_monday_midnight_utc(dt: datetime) -> datetime:
    """Midnight at the start of the next ISO Monday (strictly after ``dt``).

    ``dt.weekday()`` returns 0 for Monday. We add ``(7 - weekday) % 7``
    days, then add 7 if the result equals ``dt`` (i.e. ``dt`` is already
    Monday midnight) — the design's "following Monday" semantics never
    coincide with ``now``.
    """
    base = _midnight_utc(dt)
    days_ahead = (7 - base.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return base + timedelta(days=days_ahead)


def short_window(now_ms: int) -> Window:
    """Rolling 24h ending at ``now``. Used by ``{{episodes.last_24h}}`` /
    the short-tier reflection cron."""
    return Window(
        start_ms=now_ms - SHORT_WINDOW_HOURS * _SECONDS_PER_HOUR * _MS_PER_SECOND,
        end_ms=now_ms,
    )


def mid_window(now_ms: int) -> Window:
    """Current ISO week (Mon 00:00 UTC inclusive → Mon 00:00 UTC exclusive).

    ``now_ms`` falling inside Monday produces ``(this Monday, next
    Monday)`` — the operator's intuition for "this week" includes the
    day we're standing on.
    """
    now = _from_ms(now_ms)
    midnight_today = _midnight_utc(now)
    days_since_monday = midnight_today.weekday()  # Mon=0 .. Sun=6
    monday = midnight_today - timedelta(days=days_since_monday)
    next_monday = monday + timedelta(days=7)
    return Window(start_ms=_to_ms(monday), end_ms=_to_ms(next_monday))


def long_window(now_ms: int, created_at_ms: int) -> Window:
    """``[created_at, created_at + 90d)``.

    Long is per-goal (calendar quarters mislead — operators set
    quarterlies on arbitrary days). Reflection uses this for the
    `{{episodes.last_quarter}}`-shaped slice, plus the upper bound as
    the goal's ``target_date``.
    """
    end = created_at_ms + LONG_WINDOW_DAYS * _SECONDS_PER_DAY * _MS_PER_SECOND
    return Window(start_ms=created_at_ms, end_ms=end)


def default_target_date_ms(tier: str, *, now_ms: int) -> int:
    """Default ``target_date`` for a goal authored at ``now_ms``.

    | tier  | returns                                |
    |-------|----------------------------------------|
    | short | next UTC midnight strictly after ``now`` |
    | mid   | following Monday's midnight UTC        |
    | long  | ``now + 90 days``                      |

    The placeholder filter ``target_date >= now()`` (iter 3) uses
    "now-at-render-time", so a today's-already-passed midnight would
    drop a freshly-authored short goal off ``{{goals.today}}``
    immediately. Returning the **next** midnight gives the goal a full
    day's surface area before the 00:05 UTC cron grades and rolls it.

    Mid uses ``following Monday`` so a goal set on a Monday morning is
    still scored across a full ISO week, not zero days. Long is goal-
    relative (90d from creation) — see :func:`long_window` for the
    rationale against calendar quarters.
    """
    if tier not in TIER_VALUES:
        raise ValueError(f"tier={tier!r} not in {sorted(TIER_VALUES)}")
    now = _from_ms(now_ms)
    if tier == "short":
        # Next UTC midnight strictly after ``now`` — the goal "matures" at
        # day-end, when the short-tier cron runs at 00:05 UTC.
        next_midnight = _midnight_utc(now) + timedelta(days=1)
        return _to_ms(next_midnight)
    if tier == "mid":
        return _to_ms(_next_monday_midnight_utc(now))
    # long
    return now_ms + LONG_WINDOW_DAYS * _SECONDS_PER_DAY * _MS_PER_SECOND


def tier_rank(tier: str) -> int:
    """Ordering for the cross-tier-parent guard.

    A goal can only have a parent of **strictly higher** tier:
    short (0) < mid (1) < long (2). The CLI's
    ``cli_set_rejects_cross_tier_parent`` test exercises the boundary;
    iter 4's CLI imports this helper.
    """
    order = {"short": 0, "mid": 1, "long": 2}
    if tier not in order:
        raise ValueError(f"tier={tier!r} not in {sorted(order)}")
    return order[tier]


__all__ = [
    "LONG_WINDOW_DAYS",
    "MID_WINDOW_DAYS",
    "SHORT_WINDOW_HOURS",
    "Window",
    "default_target_date_ms",
    "long_window",
    "mid_window",
    "short_window",
    "tier_rank",
]
