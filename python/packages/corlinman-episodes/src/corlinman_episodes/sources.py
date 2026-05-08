"""Multi-stream source-event gathering for the distillation pass.

A pass over ``[window_start, window_end)`` joins five streams per
tenant (per design §"What gets distilled"):

1. **Session messages** in ``sessions.sqlite``.
2. **Evolution signals** in ``evolution.sqlite``.
3. **Evolution history** apply / revert / auto-rollback rows; joins
   to signals via the proposal's ``signal_ids`` JSON.
4. **Hook events** with kind ∈ ``{evolution_applied, tool_approved,
   tool_denied, error, auto_rollback_fired, identity_unified}``
   (Phase 4 W1.5 wired ``HookEvent.tenant_id``).
5. **Identity merges** — ``verification_phrases`` rows consumed
   in-window (Phase 4 W2 B2 design).

This module owns iter 2: the dataclass shapes plus
:func:`collect_bundles`, which returns one bundle per touched
``session_key`` (tenant-scoped, empty-bundle-dropped). Streams 4 and
5 are wired with the column names the design references; if a
deployment hasn't applied those Rust-side migrations yet, the
sources gracefully degrade to "no rows" rather than raising — keeps
the runner usable on a clean dev DB seeded from the synthetic
fixture.

All times are unix milliseconds; window is half-open
``[window_start, window_end)`` so a row at ``ts == window_end`` rolls
to the next window (consistent with the doc's part-2 episode story).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from corlinman_episodes.config import DEFAULT_TENANT_ID

# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------

#: Set of hook-event ``kind`` values that count as narratively
#: load-bearing for episode distillation. Mirrors the list in
#: §"What gets distilled" item 4.
HOOK_KINDS_OF_INTEREST: frozenset[str] = frozenset(
    {
        "evolution_applied",
        "tool_approved",
        "tool_denied",
        "error",
        "auto_rollback_fired",
        "identity_unified",
    }
)


@dataclass(frozen=True)
class SessionMessage:
    """One row from ``sessions.sqlite`` lifted to a typed dataclass.

    The ``ts`` column is stored as TEXT in SQLite (RFC3339); the
    extractor parses it to milliseconds for the window comparison so
    the runner can do numeric arithmetic without redoing the parse.
    """

    session_key: str
    seq: int
    role: str
    content: str
    ts_ms: int


@dataclass(frozen=True)
class SignalRow:
    id: int
    event_kind: str
    target: str | None
    severity: str
    payload_json: str
    session_id: str | None
    observed_at_ms: int


@dataclass(frozen=True)
class HistoryRow:
    id: int
    proposal_id: str
    kind: str
    target: str
    applied_at_ms: int
    rolled_back_at_ms: int | None
    rollback_reason: str | None
    signal_ids: tuple[int, ...]


@dataclass(frozen=True)
class HookEventRow:
    id: int
    kind: str
    payload_json: str
    session_key: str | None
    occurred_at_ms: int


@dataclass(frozen=True)
class IdentityMergeRow:
    id: int
    user_a: str
    user_b: str
    channel: str | None
    occurred_at_ms: int


@dataclass
class SourceBundle:
    """Per-``session_key`` bundle of in-window source rows.

    Empty bundles are dropped by the collector. The runner consumes
    one bundle at a time, classifies it, distills it to a summary, and
    writes one ``episodes`` row. If a single session straddles the
    window boundary, only the in-window rows show up here — the next
    pass picks up the rest as a part-2 episode (per Phase 4 OQ 2).
    """

    tenant_id: str
    session_key: str | None  # None = "no session" (e.g. signals from a cron handler)
    messages: list[SessionMessage] = field(default_factory=list)
    signals: list[SignalRow] = field(default_factory=list)
    history: list[HistoryRow] = field(default_factory=list)
    hooks: list[HookEventRow] = field(default_factory=list)
    identity_merges: list[IdentityMergeRow] = field(default_factory=list)

    @property
    def started_at(self) -> int:
        """Earliest ``ts_ms`` across all streams.

        Falls back to ``ended_at`` for bundles with no session
        messages — defensive default keeps the sql ``started_at <=
        ended_at`` invariant when only signals fired in-window.
        """
        candidates: list[int] = []
        candidates.extend(m.ts_ms for m in self.messages)
        candidates.extend(s.observed_at_ms for s in self.signals)
        candidates.extend(h.applied_at_ms for h in self.history)
        candidates.extend(e.occurred_at_ms for e in self.hooks)
        candidates.extend(m.occurred_at_ms for m in self.identity_merges)
        if not candidates:
            return 0
        return min(candidates)

    @property
    def ended_at(self) -> int:
        candidates: list[int] = []
        candidates.extend(m.ts_ms for m in self.messages)
        candidates.extend(s.observed_at_ms for s in self.signals)
        candidates.extend(h.applied_at_ms for h in self.history)
        candidates.extend(e.occurred_at_ms for e in self.hooks)
        candidates.extend(m.occurred_at_ms for m in self.identity_merges)
        if not candidates:
            return 0
        return max(candidates)

    def is_empty(self) -> bool:
        """True iff every stream is empty — collector drops these."""
        return not (
            self.messages
            or self.signals
            or self.history
            or self.hooks
            or self.identity_merges
        )


# ---------------------------------------------------------------------------
# Window selection
# ---------------------------------------------------------------------------


def select_window(
    *,
    now_ms: int,
    distillation_window_hours: float,
    last_ok_run_window_end_ms: int | None,
) -> tuple[int, int]:
    """Compute ``(window_start, window_end)`` for a fresh run.

    Spec from §Distillation job: ``window_end = now``,
    ``window_start = max(now - distillation_window_hours,
    last_ok_run.window_end)``. The latter clamp prevents successive
    runs from re-distilling rows already covered by a prior pass.

    Returns ``(0, 0)`` is forbidden — the caller is expected to feed
    a positive ``now_ms``. We don't enforce ``window_start <
    window_end`` here so the runner can decide whether to short-
    circuit (``min_window_secs`` from config) or open a near-empty
    window for tracing.
    """
    end = int(now_ms)
    rolling_start = end - int(distillation_window_hours * 3_600_000)
    if last_ok_run_window_end_ms is None:
        return (rolling_start, end)
    return (max(rolling_start, int(last_ok_run_window_end_ms)), end)


def window_too_small(
    *,
    window_start_ms: int,
    window_end_ms: int,
    min_window_secs: int,
) -> bool:
    """Pure helper for the runner's short-circuit decision."""
    return (window_end_ms - window_start_ms) < (min_window_secs * 1000)


# ---------------------------------------------------------------------------
# Source collectors
# ---------------------------------------------------------------------------


@contextmanager
def _ro_connect(path: Path) -> Iterator[sqlite3.Connection]:
    """Open ``path`` read-only via SQLite's ``mode=ro`` URI param.

    Read-only connections protect adjacent writers (the gateway, the
    evolution applier) from any accidental contention from this
    distillation pass. We use the synchronous ``sqlite3`` module here
    instead of ``aiosqlite`` since collection is a one-shot
    bounded-rowcount join and the savings of true async are dwarfed
    by the LLM call that follows. ``aiosqlite`` shows up in
    ``store.py`` for the writer side because that path stays open
    across the run.
    """
    if not path.exists():
        # Treat a missing peer DB as "no rows" — keeps a clean dev
        # checkout from blowing up before the Rust side has bootstrapped
        # ``evolution.sqlite`` / ``hook_events.sqlite``.
        conn = sqlite3.connect(":memory:")
        try:
            yield conn
        finally:
            conn.close()
        return
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        yield conn
    finally:
        conn.close()


def _ts_to_ms(value: object) -> int:
    """Best-effort ``ts`` → unix-ms parser.

    ``sessions.sqlite`` stores ``ts`` as TEXT (RFC3339 with millisecond
    precision); ``evolution.sqlite`` stores it as INTEGER ms; hook
    events vary by deployment. Accept either shape so the runner
    works against both real fixtures and the test conftest seeds.
    """
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return 0
    # Plain integer string (e.g. "1715156400000").
    try:
        return int(text)
    except ValueError:
        pass
    # RFC3339 — accept "2026-05-08T06:00:00.000Z" and the common
    # naive-utc variant "2026-05-08T06:00:00.000".
    from datetime import datetime

    cleaned = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(cleaned)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return 0


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    found = cur.fetchone() is not None
    cur.close()
    return found


def collect_session_messages(
    *,
    sessions_db: Path,
    tenant_id: str,
    window_start_ms: int,
    window_end_ms: int,
) -> list[SessionMessage]:
    """Return all in-window rows from ``sessions.sqlite``.

    Half-open window: ``window_start_ms <= ts < window_end_ms``. Rows
    without a parseable ``ts`` are skipped silently — defensive
    against the legacy column shape.
    """
    out: list[SessionMessage] = []
    with _ro_connect(sessions_db) as conn:
        if not _table_exists(conn, "sessions"):
            return out
        # ``tenant_id`` lives on rows since Phase 4 W1 4-1A; older DBs
        # lack the column. Probe and pick the right query.
        has_tenant = bool(
            conn.execute(
                "SELECT 1 FROM pragma_table_info('sessions') WHERE name='tenant_id'"
            ).fetchone()
        )
        if has_tenant:
            cur = conn.execute(
                """SELECT session_key, seq, role, content, ts
                   FROM sessions
                   WHERE tenant_id = ?
                   ORDER BY session_key ASC, seq ASC""",
                (tenant_id,),
            )
        else:
            cur = conn.execute(
                """SELECT session_key, seq, role, content, ts
                   FROM sessions
                   ORDER BY session_key ASC, seq ASC"""
            )
        for row in cur.fetchall():
            ts_ms = _ts_to_ms(row[4])
            if ts_ms < window_start_ms or ts_ms >= window_end_ms:
                continue
            out.append(
                SessionMessage(
                    session_key=str(row[0]),
                    seq=int(row[1]),
                    role=str(row[2]),
                    content=str(row[3]),
                    ts_ms=ts_ms,
                )
            )
        cur.close()
    return out


def collect_signals(
    *,
    evolution_db: Path,
    tenant_id: str,
    window_start_ms: int,
    window_end_ms: int,
) -> list[SignalRow]:
    """Return all in-window ``evolution_signals`` rows for a tenant."""
    out: list[SignalRow] = []
    with _ro_connect(evolution_db) as conn:
        if not _table_exists(conn, "evolution_signals"):
            return out
        has_tenant = bool(
            conn.execute(
                "SELECT 1 FROM pragma_table_info('evolution_signals') "
                "WHERE name='tenant_id'"
            ).fetchone()
        )
        if has_tenant:
            cur = conn.execute(
                """SELECT id, event_kind, target, severity, payload_json,
                          session_id, observed_at
                   FROM evolution_signals
                   WHERE tenant_id = ?
                     AND observed_at >= ? AND observed_at < ?
                   ORDER BY observed_at ASC""",
                (tenant_id, int(window_start_ms), int(window_end_ms)),
            )
        else:
            cur = conn.execute(
                """SELECT id, event_kind, target, severity, payload_json,
                          session_id, observed_at
                   FROM evolution_signals
                   WHERE observed_at >= ? AND observed_at < ?
                   ORDER BY observed_at ASC""",
                (int(window_start_ms), int(window_end_ms)),
            )
        for row in cur.fetchall():
            out.append(
                SignalRow(
                    id=int(row[0]),
                    event_kind=str(row[1]),
                    target=(str(row[2]) if row[2] is not None else None),
                    severity=str(row[3]),
                    payload_json=str(row[4]) if row[4] is not None else "{}",
                    session_id=(str(row[5]) if row[5] is not None else None),
                    observed_at_ms=int(row[6]),
                )
            )
        cur.close()
    return out


def _decode_signal_ids(raw: str) -> tuple[int, ...]:
    """Pull ``signal_ids`` out of a proposal's JSON payload."""
    try:
        parsed = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        return ()
    if not isinstance(parsed, list):
        return ()
    out: list[int] = []
    for v in parsed:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return tuple(out)


def collect_history(
    *,
    evolution_db: Path,
    tenant_id: str,
    window_start_ms: int,
    window_end_ms: int,
) -> list[HistoryRow]:
    """Return ``evolution_history`` rows whose apply OR rollback fell
    in the window.

    Joins to ``evolution_proposals`` to pull ``signal_ids`` (carried
    on the proposal, not on history per the schema) so the importance
    scorer can credit "this apply was driven by N signals".
    """
    out: list[HistoryRow] = []
    with _ro_connect(evolution_db) as conn:
        if not _table_exists(conn, "evolution_history"):
            return out
        has_tenant = bool(
            conn.execute(
                "SELECT 1 FROM pragma_table_info('evolution_proposals') "
                "WHERE name='tenant_id'"
            ).fetchone()
        )
        # An apply OR rollback in the window matters; either the
        # ``applied_at`` or ``rolled_back_at`` lands inside.
        if has_tenant:
            cur = conn.execute(
                """SELECT h.id, h.proposal_id, h.kind, h.target,
                          h.applied_at, h.rolled_back_at, h.rollback_reason,
                          p.signal_ids
                   FROM evolution_history AS h
                   JOIN evolution_proposals AS p ON p.id = h.proposal_id
                   WHERE p.tenant_id = ?
                     AND ((h.applied_at >= ? AND h.applied_at < ?)
                          OR (h.rolled_back_at >= ? AND h.rolled_back_at < ?))
                   ORDER BY h.applied_at ASC""",
                (
                    tenant_id,
                    int(window_start_ms),
                    int(window_end_ms),
                    int(window_start_ms),
                    int(window_end_ms),
                ),
            )
        else:
            cur = conn.execute(
                """SELECT h.id, h.proposal_id, h.kind, h.target,
                          h.applied_at, h.rolled_back_at, h.rollback_reason,
                          p.signal_ids
                   FROM evolution_history AS h
                   JOIN evolution_proposals AS p ON p.id = h.proposal_id
                   WHERE (h.applied_at >= ? AND h.applied_at < ?)
                      OR (h.rolled_back_at >= ? AND h.rolled_back_at < ?)
                   ORDER BY h.applied_at ASC""",
                (
                    int(window_start_ms),
                    int(window_end_ms),
                    int(window_start_ms),
                    int(window_end_ms),
                ),
            )
        for row in cur.fetchall():
            out.append(
                HistoryRow(
                    id=int(row[0]),
                    proposal_id=str(row[1]),
                    kind=str(row[2]),
                    target=str(row[3]),
                    applied_at_ms=int(row[4]),
                    rolled_back_at_ms=int(row[5]) if row[5] is not None else None,
                    rollback_reason=(str(row[6]) if row[6] is not None else None),
                    signal_ids=_decode_signal_ids(str(row[7]) if row[7] else "[]"),
                )
            )
        cur.close()
    return out


def collect_hook_events(
    *,
    hook_events_db: Path,
    tenant_id: str,
    window_start_ms: int,
    window_end_ms: int,
) -> list[HookEventRow]:
    """Return in-window rows from ``hook_events`` whose ``kind``
    is in :data:`HOOK_KINDS_OF_INTEREST`.

    The hook-events DB schema is owned Rust-side; a missing table or
    column is treated as "no rows" so the test fixtures don't have
    to over-mirror the gateway. The exact column-name probe lives on
    the open connection so this stays robust to migration drift.
    """
    out: list[HookEventRow] = []
    with _ro_connect(hook_events_db) as conn:
        if not _table_exists(conn, "hook_events"):
            return out
        cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(hook_events)").fetchall()
        }
        if not {"id", "kind", "occurred_at"} <= cols:
            return out
        ts_col = "occurred_at"
        kind_col = "kind"
        payload_col = "payload_json" if "payload_json" in cols else None
        session_col = "session_key" if "session_key" in cols else None
        tenant_col = "tenant_id" if "tenant_id" in cols else None
        select = "id, " + kind_col + ", "
        select += payload_col if payload_col else "''"
        select += ", "
        select += session_col if session_col else "NULL"
        select += ", " + ts_col
        where = f"{ts_col} >= ? AND {ts_col} < ?"
        params: list[object] = [int(window_start_ms), int(window_end_ms)]
        if tenant_col:
            where += f" AND {tenant_col} = ?"
            params.append(tenant_id)
        cur = conn.execute(
            f"SELECT {select} FROM hook_events WHERE {where} "
            f"ORDER BY {ts_col} ASC",
            tuple(params),
        )
        for row in cur.fetchall():
            kind = str(row[1])
            if kind not in HOOK_KINDS_OF_INTEREST:
                continue
            out.append(
                HookEventRow(
                    id=int(row[0]),
                    kind=kind,
                    payload_json=str(row[2]) if row[2] is not None else "{}",
                    session_key=(str(row[3]) if row[3] is not None else None),
                    occurred_at_ms=int(row[4]),
                )
            )
        cur.close()
    return out


def collect_identity_merges(
    *,
    identity_db: Path,
    tenant_id: str,
    window_start_ms: int,
    window_end_ms: int,
) -> list[IdentityMergeRow]:
    """Pull in-window ``verification_phrases`` consumption rows.

    Phase 4 W2 B2 wired the table; B1 deployments may not have it.
    Probes for the ``user_a`` + ``user_b`` columns the design lists
    and degrades to "no rows" otherwise. The exact column names match
    the B2 design doc; if Rust drifts post-this iter we revisit.
    """
    out: list[IdentityMergeRow] = []
    with _ro_connect(identity_db) as conn:
        if not _table_exists(conn, "verification_phrases"):
            return out
        cols = {
            r[1]
            for r in conn.execute(
                "PRAGMA table_info(verification_phrases)"
            ).fetchall()
        }
        if not {"id", "consumed_at"} <= cols:
            return out
        # Be lenient on the merge-pair column names — different
        # iterations of B2 used ``user_a/user_b`` vs.
        # ``primary_user/secondary_user``. Pick whichever pair is
        # present; ``channel`` is optional.
        if {"user_a", "user_b"} <= cols:
            a_col, b_col = "user_a", "user_b"
        elif {"primary_user", "secondary_user"} <= cols:
            a_col, b_col = "primary_user", "secondary_user"
        else:
            return out
        channel_col = "channel" if "channel" in cols else "NULL"
        tenant_col = "tenant_id" if "tenant_id" in cols else None
        where = "consumed_at IS NOT NULL AND consumed_at >= ? AND consumed_at < ?"
        params: list[object] = [int(window_start_ms), int(window_end_ms)]
        if tenant_col:
            where += f" AND {tenant_col} = ?"
            params.append(tenant_id)
        cur = conn.execute(
            f"SELECT id, {a_col}, {b_col}, {channel_col}, consumed_at "
            f"FROM verification_phrases WHERE {where} ORDER BY consumed_at ASC",
            tuple(params),
        )
        for row in cur.fetchall():
            out.append(
                IdentityMergeRow(
                    id=int(row[0]),
                    user_a=str(row[1]),
                    user_b=str(row[2]),
                    channel=(str(row[3]) if row[3] is not None else None),
                    occurred_at_ms=int(row[4]),
                )
            )
        cur.close()
    return out


# ---------------------------------------------------------------------------
# Bundle assembly
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourcePaths:
    """Path bundle the runner passes through.

    Three are required (sessions, evolution, hook events). Identity
    is optional — Phase 4 W2 B2 introduced it; older deployments hit
    the degrades-to-no-rows path in :func:`collect_identity_merges`.
    """

    sessions_db: Path
    evolution_db: Path
    hook_events_db: Path
    identity_db: Path | None = None


def collect_bundles(
    *,
    paths: SourcePaths,
    tenant_id: str = DEFAULT_TENANT_ID,
    window_start_ms: int,
    window_end_ms: int,
) -> list[SourceBundle]:
    """Join all five streams over ``[window_start_ms, window_end_ms)``
    and return one :class:`SourceBundle` per touched ``session_key``.

    Per design doc (§"Distillation job" item 1): empty bundles
    dropped. Signals + history rows that don't carry a ``session_id``
    fall into a single "orphan" bundle keyed by ``session_key=None``
    so a cron-handler-only window still produces one episode (kind
    typically ``EVOLUTION`` or ``INCIDENT``).
    """
    messages = collect_session_messages(
        sessions_db=paths.sessions_db,
        tenant_id=tenant_id,
        window_start_ms=window_start_ms,
        window_end_ms=window_end_ms,
    )
    signals = collect_signals(
        evolution_db=paths.evolution_db,
        tenant_id=tenant_id,
        window_start_ms=window_start_ms,
        window_end_ms=window_end_ms,
    )
    history = collect_history(
        evolution_db=paths.evolution_db,
        tenant_id=tenant_id,
        window_start_ms=window_start_ms,
        window_end_ms=window_end_ms,
    )
    hooks = collect_hook_events(
        hook_events_db=paths.hook_events_db,
        tenant_id=tenant_id,
        window_start_ms=window_start_ms,
        window_end_ms=window_end_ms,
    )
    if paths.identity_db is not None:
        identity = collect_identity_merges(
            identity_db=paths.identity_db,
            tenant_id=tenant_id,
            window_start_ms=window_start_ms,
            window_end_ms=window_end_ms,
        )
    else:
        identity = []

    bundles: dict[str | None, SourceBundle] = {}

    def _bucket(key: str | None) -> SourceBundle:
        if key not in bundles:
            bundles[key] = SourceBundle(tenant_id=tenant_id, session_key=key)
        return bundles[key]

    for m in messages:
        _bucket(m.session_key).messages.append(m)
    for s in signals:
        _bucket(s.session_id).signals.append(s)
    # History rows don't carry a session id directly — bucket them
    # under the orphan key. The classifier surfaces "this episode
    # contains an apply" via the bundle's `history` field rather than
    # its session linkage, so this is fine.
    for h in history:
        _bucket(None).history.append(h)
    for e in hooks:
        _bucket(e.session_key).hooks.append(e)
    # Identity merges are global, no session linkage; orphan bucket.
    for im in identity:
        _bucket(None).identity_merges.append(im)

    # Empty-bundle filter; sort deterministically so the runner
    # writes episodes in a stable order across re-runs.
    out = [b for b in bundles.values() if not b.is_empty()]
    out.sort(key=lambda b: (b.session_key or "", b.started_at))
    return out


__all__ = [
    "HOOK_KINDS_OF_INTEREST",
    "HistoryRow",
    "HookEventRow",
    "IdentityMergeRow",
    "SessionMessage",
    "SignalRow",
    "SourceBundle",
    "SourcePaths",
    "collect_bundles",
    "collect_history",
    "collect_hook_events",
    "collect_identity_merges",
    "collect_session_messages",
    "collect_signals",
    "select_window",
    "window_too_small",
]
