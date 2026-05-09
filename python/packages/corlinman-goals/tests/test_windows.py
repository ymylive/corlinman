"""Tests for :mod:`corlinman_goals.windows` (iter 2).

Pin the tier-derived ``target_date`` math, the half-open window bounds,
and the cross-tier-parent ordering used by the CLI guard. Pure date
arithmetic — no fixtures, no I/O, just deterministic UTC inputs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from corlinman_goals.windows import (
    LONG_WINDOW_DAYS,
    MID_WINDOW_DAYS,
    SHORT_WINDOW_HOURS,
    default_target_date_ms,
    long_window,
    mid_window,
    short_window,
    tier_rank,
)


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> int:
    """Build a deterministic unix-ms timestamp for a UTC wall-clock."""
    return int(
        datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp()
        * 1000
    )


# ---------------------------------------------------------------------------
# short_window — rolling 24h ending at run time
# ---------------------------------------------------------------------------


def test_short_window_is_24h_ending_at_now() -> None:
    now = _utc(2026, 5, 9, 14, 0)
    w = short_window(now)
    assert w.end_ms == now
    assert w.end_ms - w.start_ms == SHORT_WINDOW_HOURS * 3600 * 1000


# ---------------------------------------------------------------------------
# mid_window — current ISO week (Mon 00:00 UTC inclusive → next Mon excl)
# ---------------------------------------------------------------------------


def test_mid_window_is_current_iso_week() -> None:
    """2026-05-09 is a Saturday; the containing ISO week is
    Mon 2026-05-04 → Mon 2026-05-11."""
    now = _utc(2026, 5, 9, 14, 30)
    w = mid_window(now)
    assert w.start_ms == _utc(2026, 5, 4, 0, 0)  # Monday 00:00 UTC
    assert w.end_ms == _utc(2026, 5, 11, 0, 0)
    # Half-open exactly 7 calendar days wide.
    assert w.end_ms - w.start_ms == MID_WINDOW_DAYS * 86_400 * 1000


def test_mid_window_when_now_is_monday_includes_today() -> None:
    """Monday 00:30 UTC: the window starts at the same Monday 00:00, not
    the previous Monday — operator intuition for "this week" includes
    the day they're standing on."""
    now = _utc(2026, 5, 4, 0, 30)
    w = mid_window(now)
    assert w.start_ms == _utc(2026, 5, 4, 0, 0)
    assert w.end_ms == _utc(2026, 5, 11, 0, 0)


def test_mid_window_when_now_is_sunday_evening_includes_today() -> None:
    """Sunday at 23:30 UTC is the last full hour of the week. The window
    must still span the **current** week, not the next one."""
    now = _utc(2026, 5, 10, 23, 30)
    w = mid_window(now)
    assert w.start_ms == _utc(2026, 5, 4, 0, 0)
    assert w.end_ms == _utc(2026, 5, 11, 0, 0)


# ---------------------------------------------------------------------------
# long_window — 90d from creation
# ---------------------------------------------------------------------------


def test_long_window_is_90d_from_created_at() -> None:
    created = _utc(2026, 5, 9)
    w = long_window(now_ms=_utc(2026, 5, 30), created_at_ms=created)
    assert w.start_ms == created
    assert w.end_ms - w.start_ms == LONG_WINDOW_DAYS * 86_400 * 1000


# ---------------------------------------------------------------------------
# default_target_date_ms — what the CLI uses when --target-date omitted
# ---------------------------------------------------------------------------


def test_default_target_date_short_is_next_utc_midnight() -> None:
    """Goal authored Tuesday 14:30 UTC → matures at Wed 00:00 UTC.

    Short-tier cron runs at 00:05 UTC; the placeholder filter
    ``target_date >= now()`` uses now-at-render-time. Returning
    "today's already-passed midnight" would drop the goal off
    ``{{goals.today}}`` immediately on creation, which is wrong."""
    now = _utc(2026, 5, 5, 14, 30)  # Tuesday afternoon
    expected = _utc(2026, 5, 6, 0, 0)  # Wednesday 00:00 UTC
    assert default_target_date_ms("short", now_ms=now) == expected


def test_default_target_date_short_at_midnight_yields_following_midnight() -> None:
    """Boundary: now is exactly midnight UTC. The semantic is "next
    midnight strictly after now", so a goal authored at 00:00 still has
    a full day's window."""
    now = _utc(2026, 5, 5, 0, 0)
    expected = _utc(2026, 5, 6, 0, 0)
    assert default_target_date_ms("short", now_ms=now) == expected


def test_default_target_date_mid_is_next_monday_midnight_when_midweek() -> None:
    """Mid goal authored Saturday 2026-05-09 → matures Mon 2026-05-11."""
    now = _utc(2026, 5, 9, 14, 30)
    expected = _utc(2026, 5, 11, 0, 0)
    assert default_target_date_ms("mid", now_ms=now) == expected


def test_default_target_date_mid_when_now_is_monday_returns_following_monday() -> None:
    """Mid goal authored Monday 09:00 UTC: ``following Monday midnight``,
    i.e. seven days out — we never collapse the window to zero."""
    now = _utc(2026, 5, 4, 9, 0)  # Monday
    expected = _utc(2026, 5, 11, 0, 0)
    assert default_target_date_ms("mid", now_ms=now) == expected


def test_default_target_date_mid_when_now_is_monday_midnight_returns_next_monday() -> (
    None
):
    """Boundary: Mon 00:00 UTC. ``following`` Monday is one week out, not
    zero — design pins the strict-after semantics."""
    now = _utc(2026, 5, 4, 0, 0)
    expected = _utc(2026, 5, 11, 0, 0)
    assert default_target_date_ms("mid", now_ms=now) == expected


def test_default_target_date_long_is_90d_from_now() -> None:
    now = _utc(2026, 5, 9)
    expected = now + LONG_WINDOW_DAYS * 86_400 * 1000
    assert default_target_date_ms("long", now_ms=now) == expected
    # Sanity check: 2026-05-09 + 90d ⇒ 2026-08-07. (Calendar arithmetic
    # crosses a month boundary, useful for catching off-by-one drift if
    # someone later swaps 90d for "3 calendar months".)
    expected_dt = datetime(2026, 5, 9, tzinfo=UTC) + timedelta(days=90)
    assert expected_dt == datetime(2026, 8, 7, tzinfo=UTC)


def test_default_target_date_rejects_unknown_tier() -> None:
    with pytest.raises(ValueError, match=r"tier="):
        default_target_date_ms("bogus", now_ms=_utc(2026, 5, 9))


# ---------------------------------------------------------------------------
# tier_rank — cross-tier-parent guard for iter 4's CLI
# ---------------------------------------------------------------------------


def test_tier_rank_is_strictly_increasing() -> None:
    """short < mid < long. The CLI's parent guard rejects equal-or-lower
    parents, so the strict ordering is the contract."""
    assert tier_rank("short") < tier_rank("mid") < tier_rank("long")


def test_tier_rank_rejects_unknown_tier() -> None:
    with pytest.raises(ValueError, match=r"tier="):
        tier_rank("bogus")
