"""Cron expression parsing helpers.

Python port of ``rust/crates/corlinman-scheduler/src/cron.rs``.

The Rust crate uses the ``cron`` crate with its 7-field grammar
(``sec min hour day month weekday year``); the Python ecosystem's
de-facto cron parser is :mod:`croniter`, which natively accepts the
5-field POSIX form (``min hour dom mon dow``) plus the 6-field
``sec min hour dom mon dow`` extension. We bridge the two so existing
TOML configs written for the Rust gateway continue to work after the
port:

* 5-field expressions go straight into :mod:`croniter`.
* 6-field expressions go straight into :mod:`croniter` (it has a
  ``second_at_beginning=True`` mode for that exact shape).
* 7-field expressions (Rust's native format) are normalised by
  dropping the trailing ``year`` field — :mod:`croniter` doesn't
  parse the year column. Every conceivable production cron is a "*"
  in the year slot (yearly schedules use the ``mon dom`` slots), so
  this is lossless for the corlinman config corpus. We *log nothing*
  when normalising; the call site already validated the expression
  shape if it cares.

The two public entry points mirror the Rust API 1:1:

* :func:`parse` returns a :class:`Schedule` object (a thin wrapper
  around the parsed :mod:`croniter` form so callers don't have to
  re-thread the cron string on every ``next_after`` call).
* :func:`next_after` returns the next firing strictly after a
  reference :class:`datetime.datetime` (timezone-aware, UTC).

:class:`CronParseError` is the typed parse failure. Mirrors the Rust
``cron::error::Error`` surface in the sense that the Rust function
returns a ``Result``; the Python equivalent raises (per the project's
"use exceptions, not error-tuples" style).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from croniter import CroniterBadCronError, croniter

__all__ = [
    "CronParseError",
    "Schedule",
    "next_after",
    "parse",
]


class CronParseError(ValueError):
    """Raised by :func:`parse` when the expression doesn't compile.

    Wraps the underlying :class:`croniter.CroniterBadCronError` (when
    that's the cause) but is its own class so callers can ``except``
    on a stable name without depending on the :mod:`croniter` import.
    """

    def __init__(self, expr: str, source: BaseException) -> None:
        self.expr = expr
        self.source = source
        super().__init__(f"invalid cron expression {expr!r}: {source}")


@dataclass(frozen=True)
class Schedule:
    """Parsed cron expression. Cheap to clone; cheap to query.

    Mirrors the Rust ``cron::Schedule`` enum. The actual parsing state
    lives in ``_expr`` (the *normalised* expression — 7-field configs
    have already had the year column dropped); each :func:`next_after`
    call builds a fresh :class:`croniter.croniter` iterator anchored
    at the supplied "now" so the schedule struct itself stays
    stateless and shareable across coroutines.
    """

    _expr: str
    """Normalised cron expression (5 or 6 fields; never 7)."""
    _has_seconds: bool
    """True iff the expression is 6-field (sec min hour dom mon dow)."""

    def expr(self) -> str:
        """The normalised cron string this schedule was built from.

        Useful for diagnostics / log lines; matches the Rust
        :meth:`Schedule::source` accessor in intent (we don't preserve
        the original 7-field form because the runtime never needs it).
        """
        return self._expr


def _normalise_seven_field(expr: str) -> tuple[str, bool]:
    """Turn the input cron string into the form :mod:`croniter` accepts.

    Returns ``(normalised_expr, has_seconds)``:

    * 5 fields: passed through untouched. ``has_seconds=False``.
    * 6 fields: passed through untouched. ``has_seconds=True``.
    * 7 fields: the trailing year column is dropped (the corlinman
      config corpus has only ever used ``*`` for the year slot — see
      module docstring for the rationale). ``has_seconds=True``.
    * Anything else: returned as-is so :mod:`croniter` surfaces the
      parse failure with its native error message (we don't want to
      hide a typo behind a generic "wrong field count").
    """
    parts = expr.split()
    if len(parts) == 7:
        # `sec min hour dom mon dow year` → drop year, keep the rest.
        # croniter parses the remaining 6-field form with
        # second_at_beginning=True.
        return " ".join(parts[:6]), True
    if len(parts) == 6:
        return expr, True
    # 5-field, or whatever shape — let croniter validate.
    return expr, False


def parse(expr: str) -> Schedule:
    """Parse ``expr`` into a :class:`Schedule`.

    Accepts the Rust crate's 7-field grammar (``sec min hour dom mon
    dow year``) for back-compat with the existing TOML corpus, plus
    the 5- and 6-field :mod:`croniter`-native forms.

    Raises:
        CronParseError: if the expression doesn't compile.
    """
    normalised, has_seconds = _normalise_seven_field(expr)
    try:
        # Validate by constructing one iterator at parse time. We don't
        # keep it — `next_after` makes its own with the caller's "now".
        # ``croniter`` raises CroniterBadCronError on bad input; we
        # also defensively catch ValueError because some malformed
        # inputs surface as that on older croniter versions.
        croniter(normalised, datetime.now(tz=timezone.utc), second_at_beginning=has_seconds)
    except (CroniterBadCronError, ValueError) as exc:
        raise CronParseError(expr, exc) from exc
    return Schedule(_expr=normalised, _has_seconds=has_seconds)


def next_after(schedule: Schedule, now: datetime) -> datetime | None:
    """Compute the next firing strictly after ``now``.

    Returns ``None`` for schedules that have no upcoming firing (e.g.
    a ``Feb 30`` combo). Mirrors the Rust :func:`next_after` contract:
    callers should treat ``None`` as "this job will never fire" and
    bail out of the per-job tick loop rather than busy-looping.

    ``now`` must be timezone-aware. If it's naive, we coerce to UTC
    (the rest of the gateway timestamps are UTC); the Rust side takes
    ``DateTime<Utc>`` so this matches its assumed timezone.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    try:
        it = croniter(schedule._expr, now, second_at_beginning=schedule._has_seconds)
        # ``get_next(datetime)`` returns the next firing strictly after
        # the base time — matches the Rust ``Schedule::after(&now).next()``
        # contract (strictly-after, not at-or-after).
        nxt = it.get_next(datetime)
    except (CroniterBadCronError, ValueError, StopIteration):
        # croniter raises on impossible schedules (e.g. day-of-month
        # 31 in a 30-day month combined with a month-only filter).
        return None
    # croniter sometimes returns a naive datetime when the base is
    # naive; we coerced above so this branch is defensive.
    if nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=timezone.utc)
    return nxt
