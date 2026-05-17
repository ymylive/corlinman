"""SQLite-backed persistence for the scheduler.

Python-side addition that wasn't present in the Rust crate (which is
purely in-memory and emits to the hook bus). The brief asks for an
:mod:`aiosqlite`-backed persistence layer so the scheduler's
run-history survives a process restart; gateway shutdown writes the
last outcome, gateway startup can replay / inspect for the admin UI.

One table:

* ``scheduler_runs`` — append-only per-firing record. Captures the
  job name, generated ``run_id``, action kind, outcome kind,
  ``error_kind`` (for failed runs, mirroring the
  ``EngineRunFailed::error_kind`` discriminant), ``exit_code``,
  ``duration_ms``, and a wall-clock ``fired_at_ms`` stamp.

The store is intentionally thin: it doesn't auto-attach to the
runtime tick loop. The gateway integration code (out of scope for
this submodule) wires a hook-subscription that calls
:meth:`SchedulerStore.record_outcome` on every emitted
``EngineRunCompleted`` / ``EngineRunFailed``. Keeping that wiring
external means tests can exercise the runtime without an SQLite
file and the store without a tick loop.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from corlinman_server.scheduler.runner import SubprocessOutcome, SubprocessOutcomeKind

__all__ = [
    "SCHEDULER_SCHEMA_SQL",
    "RunRecord",
    "SchedulerStore",
    "SchedulerStoreConnectError",
    "SchedulerStoreError",
]


# Idempotent CREATE TABLE. Re-applying is safe; column-stable v1. New
# columns must land via an idempotent ALTER (mirror the convention
# the tenancy / evolution stores use elsewhere in the codebase).
SCHEDULER_SCHEMA_SQL: str = r"""
CREATE TABLE IF NOT EXISTS scheduler_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    job_name       TEXT NOT NULL,
    run_id         TEXT NOT NULL,
    action_kind    TEXT NOT NULL,
    outcome_kind   TEXT NOT NULL,
    error_kind     TEXT,
    exit_code      INTEGER,
    duration_ms    INTEGER NOT NULL,
    fired_at_ms    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scheduler_runs_job
    ON scheduler_runs(job_name, fired_at_ms DESC);
CREATE INDEX IF NOT EXISTS idx_scheduler_runs_run_id
    ON scheduler_runs(run_id);
"""


@dataclass(frozen=True)
class RunRecord:
    """One row from ``scheduler_runs``.

    * ``outcome_kind`` is one of ``"success"``, ``"non_zero_exit"``,
      ``"timeout"``, ``"spawn_failed"``, ``"unsupported_action"``
      (matches the :class:`SubprocessOutcomeKind` discriminant plus
      the unsupported-action branch from ``dispatch``).
    * ``error_kind`` is ``None`` on successful runs and one of
      ``"exit_code" | "timeout" | "spawn_failed" | "unsupported_action"``
      otherwise — same vocabulary as ``HookEvent::EngineRunFailed.error_kind``.
    """

    id: int
    job_name: str
    run_id: str
    action_kind: str
    outcome_kind: str
    error_kind: str | None
    exit_code: int | None
    duration_ms: int
    fired_at_ms: int


class SchedulerStoreError(RuntimeError):
    """Base class for scheduler-store failures."""


class SchedulerStoreConnectError(SchedulerStoreError):
    """Connection open or schema-apply failed. Wraps the underlying
    :class:`aiosqlite.Error` (or whatever surfaced from
    :mod:`sqlite3` underneath) so callers don't need to import the
    :mod:`aiosqlite` tree."""

    def __init__(self, db_path: Path, source: BaseException) -> None:
        self.db_path = db_path
        self.source = source
        super().__init__(f"connect / apply schema {db_path}: {source}")


def _unix_now_ms() -> int:
    """Wall-clock unix-millis. Local helper so the store doesn't pull
    in a date library just to stamp rows. Mirrors the
    :func:`_unix_now_ms` helper in :mod:`corlinman_server.tenancy.admin_schema`
    for consistency across the codebase's SQLite stores."""
    ts = time.time()
    if ts <= 0:
        return 0
    millis = int(ts * 1000)
    # Clamp to INT64 max so the SQLite INTEGER column never overflows.
    return min(millis, 9223372036854775807)


class SchedulerStore:
    """Thin CRUD wrapper over the ``scheduler_runs`` table.

    Holds a single :class:`aiosqlite.Connection` opened at
    :meth:`open`. Cheap to share across coroutines (single connection,
    aiosqlite serialises writes internally); the gateway opens one
    instance at boot and hands it to the scheduler-hook subscriber.

    Construct via :meth:`SchedulerStore.open` — the ``__init__`` is
    internal so callers don't accidentally hand it a pre-opened handle
    that hasn't had the schema applied.
    """

    def __init__(self, conn: aiosqlite.Connection, db_path: Path) -> None:
        # Internal — call :meth:`open` instead.
        self._conn = conn
        self._db_path = db_path

    # ---- lifecycle ---------------------------------------------------------

    @classmethod
    async def open(cls, path: Path | str) -> SchedulerStore:
        """Open (or create) the scheduler DB at ``path``. Applies
        :data:`SCHEDULER_SCHEMA_SQL` idempotently. WAL +
        ``synchronous=NORMAL`` + ``foreign_keys=ON`` matches the rest
        of the corlinman SQLite stores.

        Raises:
            SchedulerStoreConnectError: connect or schema apply failed.
        """
        db_path = Path(path)
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = await aiosqlite.connect(str(db_path))
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
            await conn.execute("PRAGMA foreign_keys=ON")
            await conn.executescript(SCHEDULER_SCHEMA_SQL)
            await conn.commit()
        except BaseException as exc:
            raise SchedulerStoreConnectError(db_path, exc) from exc
        return cls(conn, db_path)

    async def close(self) -> None:
        """Close the underlying connection. Idempotent — a second
        ``close()`` after the connection is already gone is silently
        swallowed (best-effort drain on shutdown)."""
        with contextlib.suppress(Exception):
            await self._conn.close()

    def db_path(self) -> Path:
        """The path the wrapper was opened against."""
        return self._db_path

    def connection(self) -> aiosqlite.Connection:
        """Borrow the underlying connection. Useful for tests; production
        code should prefer the typed methods below."""
        return self._conn

    # ---- writers -----------------------------------------------------------

    async def record_outcome(
        self,
        *,
        job_name: str,
        run_id: str,
        action_kind: str,
        outcome: SubprocessOutcome,
        fired_at_ms: int | None = None,
    ) -> int:
        """Persist one run. Returns the inserted row's ``id``.

        Derives ``outcome_kind`` / ``error_kind`` from
        :class:`SubprocessOutcome` exactly the way the dispatcher does
        when building the hook event — so a sqlite query and a hook
        subscription see the same vocabulary for the same firing.
        """
        error_kind: str | None
        if outcome.kind is SubprocessOutcomeKind.SUCCESS:
            error_kind = None
        elif outcome.kind is SubprocessOutcomeKind.NON_ZERO_EXIT:
            error_kind = "exit_code"
        elif outcome.kind is SubprocessOutcomeKind.TIMEOUT:
            error_kind = "timeout"
        elif outcome.kind is SubprocessOutcomeKind.SPAWN_FAILED:
            error_kind = "spawn_failed"
        else:  # pragma: no cover - exhaustive over the enum
            raise AssertionError(f"unknown SubprocessOutcomeKind: {outcome.kind}")

        return await self.record_raw(
            job_name=job_name,
            run_id=run_id,
            action_kind=action_kind,
            outcome_kind=outcome.kind.value,
            error_kind=error_kind,
            exit_code=outcome.exit_code,
            duration_ms=int(outcome.duration_secs * 1000),
            fired_at_ms=fired_at_ms,
        )

    async def record_raw(
        self,
        *,
        job_name: str,
        run_id: str,
        action_kind: str,
        outcome_kind: str,
        error_kind: str | None,
        exit_code: int | None,
        duration_ms: int,
        fired_at_ms: int | None = None,
    ) -> int:
        """Lower-level insert used by the "unsupported_action" branch
        of the dispatcher (no :class:`SubprocessOutcome` to wrap).
        Public so callers can shape arbitrary outcomes."""
        if fired_at_ms is None:
            fired_at_ms = _unix_now_ms()
        cursor = await self._conn.execute(
            "INSERT INTO scheduler_runs "
            "(job_name, run_id, action_kind, outcome_kind, error_kind, "
            " exit_code, duration_ms, fired_at_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job_name,
                run_id,
                action_kind,
                outcome_kind,
                error_kind,
                exit_code,
                duration_ms,
                fired_at_ms,
            ),
        )
        row_id = cursor.lastrowid or 0
        await cursor.close()
        await self._conn.commit()
        return int(row_id)

    # ---- readers -----------------------------------------------------------

    async def list_recent(self, limit: int = 100) -> list[RunRecord]:
        """Most-recent ``limit`` rows across all jobs, newest first.

        Ordered by ``fired_at_ms DESC, id DESC`` so rows with the
        same millisecond stamp (possible under fast firings) still
        tie-break deterministically by insertion order.
        """
        rows: list[RunRecord] = []
        async with self._conn.execute(
            "SELECT id, job_name, run_id, action_kind, outcome_kind, "
            "       error_kind, exit_code, duration_ms, fired_at_ms "
            "FROM scheduler_runs "
            "ORDER BY fired_at_ms DESC, id DESC "
            "LIMIT ?",
            (limit,),
        ) as cursor:
            async for r in cursor:
                rows.append(_row_to_record(r))
        return rows

    async def list_for_job(self, job_name: str, limit: int = 100) -> list[RunRecord]:
        """Most-recent ``limit`` rows for one job, newest first.

        Uses the ``idx_scheduler_runs_job`` index so the query is
        O(log n) on the (job_name, fired_at_ms DESC) tuple even on
        long histories.
        """
        rows: list[RunRecord] = []
        async with self._conn.execute(
            "SELECT id, job_name, run_id, action_kind, outcome_kind, "
            "       error_kind, exit_code, duration_ms, fired_at_ms "
            "FROM scheduler_runs "
            "WHERE job_name = ? "
            "ORDER BY fired_at_ms DESC, id DESC "
            "LIMIT ?",
            (job_name, limit),
        ) as cursor:
            async for r in cursor:
                rows.append(_row_to_record(r))
        return rows

    async def get_by_run_id(self, run_id: str) -> RunRecord | None:
        """Fetch one row by its ``run_id`` (the uuid the dispatcher
        generates per firing). Returns ``None`` when no such row exists —
        callers that need a strict lookup can branch on the ``None``
        return without parsing exception strings."""
        async with self._conn.execute(
            "SELECT id, job_name, run_id, action_kind, outcome_kind, "
            "       error_kind, exit_code, duration_ms, fired_at_ms "
            "FROM scheduler_runs WHERE run_id = ?",
            (run_id,),
        ) as cursor:
            r = await cursor.fetchone()
        return _row_to_record(r) if r is not None else None

    async def count(self) -> int:
        """Total number of rows. Cheap (uses SQLite's COUNT(*) which
        is O(table-scan) but the table is small in practice — minutes
        of firings, not millions of rows)."""
        async with self._conn.execute(
            "SELECT COUNT(*) FROM scheduler_runs"
        ) as cursor:
            r = await cursor.fetchone()
        return int(r[0]) if r is not None else 0


def _row_to_record(r: object) -> RunRecord:
    """Translate a raw aiosqlite row tuple into a typed :class:`RunRecord`.

    Free helper rather than a method so the readers don't have to keep
    re-typing the column unpacking dance."""
    # aiosqlite returns sqlite3.Row-like sequences indexable by int.
    return RunRecord(
        id=int(r[0]),  # type: ignore[index]
        job_name=str(r[1]),  # type: ignore[index]
        run_id=str(r[2]),  # type: ignore[index]
        action_kind=str(r[3]),  # type: ignore[index]
        outcome_kind=str(r[4]),  # type: ignore[index]
        error_kind=(str(r[5]) if r[5] is not None else None),  # type: ignore[index]
        exit_code=(int(r[6]) if r[6] is not None else None),  # type: ignore[index]
        duration_ms=int(r[7]),  # type: ignore[index]
        fired_at_ms=int(r[8]),  # type: ignore[index]
    )
