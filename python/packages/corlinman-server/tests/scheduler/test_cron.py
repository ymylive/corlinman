"""Port of ``corlinman-scheduler::cron`` unit tests to pytest.

Mirrors the Rust ``mod tests`` in ``src/cron.rs``:

* ``parses_seven_field_cron``
* ``rejects_garbage``

Plus Python-flavour coverage for the 5- and 6-field branches because
:mod:`croniter` accepts those natively and the port treats them as
first-class inputs (the Rust crate only ever sees 7-field expressions
because the gateway's config schema mandates it).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from corlinman_server.scheduler.cron import (
    CronParseError,
    Schedule,
    next_after,
    parse,
)


def test_parses_seven_field_cron() -> None:
    """The Rust ``cron`` crate's 7-field format must round-trip through
    the Python parser. We pin the schedule to "0 0 3 * * * *" (daily 3am)
    so the next firing is always within the next 24h regardless of when
    the test runs."""
    s = parse("0 0 3 * * * *")
    now = datetime.now(tz=timezone.utc)
    nxt = next_after(s, now)
    assert nxt is not None, "daily cron should have a next firing"
    assert nxt > now


def test_rejects_garbage() -> None:
    """Malformed expressions surface as :class:`CronParseError`. The
    wrapper exception carries the original expression so logs at the
    call site can include it without re-threading the string."""
    with pytest.raises(CronParseError) as ei:
        parse("not a cron")
    assert ei.value.expr == "not a cron"


def test_parses_five_field_posix_cron() -> None:
    """The Python-flavour 5-field POSIX form (``min hour dom mon dow``)
    must also parse — the gateway's TOML configs are 7-field today, but
    tests / admin tooling sometimes pass 5-field strings."""
    s = parse("0 3 * * *")  # daily 3am, 5-field
    now = datetime.now(tz=timezone.utc)
    assert next_after(s, now) is not None


def test_parses_six_field_with_seconds() -> None:
    """6-field (``sec min hour dom mon dow``) form is what croniter
    natively calls ``second_at_beginning=True``. Used in tests that want
    a per-second cron without the 7-field year column."""
    s = parse("* * * * * *")  # every second
    now = datetime.now(tz=timezone.utc)
    nxt = next_after(s, now)
    assert nxt is not None
    # The next firing is within 2s of "now" — generous bound for CI
    # scheduling jitter; the cron itself fires every second.
    assert (nxt - now).total_seconds() <= 2.0


def test_next_after_is_strictly_after_now() -> None:
    """``next_after`` matches the Rust ``Schedule::after(&now).next()``
    contract: strictly-after, not at-or-after. If "now" lands exactly
    on a firing, the returned datetime is the *next* one."""
    s = parse("0 0 3 * * * *")
    # Pick a deterministic "now" exactly on a firing (today, 03:00:00).
    today = datetime.now(tz=timezone.utc).replace(
        hour=3, minute=0, second=0, microsecond=0
    )
    nxt = next_after(s, today)
    assert nxt is not None
    assert nxt > today


def test_next_after_accepts_naive_datetime() -> None:
    """A naive ``now`` is coerced to UTC rather than rejected. The
    rest of the gateway uses tz-aware datetimes; this helper covers
    test fixtures that pass ``datetime.utcnow()`` (which is naive)."""
    s = parse("0 0 3 * * * *")
    naive = datetime.now().replace(tzinfo=None)
    nxt = next_after(s, naive)
    assert nxt is not None
    assert nxt.tzinfo is not None


def test_schedule_expr_returns_normalised_string() -> None:
    """:meth:`Schedule.expr` returns the normalised cron — the 7-field
    input has had its year column dropped, leaving the 6-field form
    that croniter understands. Useful for log lines + parity assertions."""
    s = parse("0 0 3 * * * *")
    assert isinstance(s, Schedule)
    # Normalisation drops the trailing year column ("*" in real configs).
    assert s.expr() == "0 0 3 * * *"
