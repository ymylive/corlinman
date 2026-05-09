"""Async SQLite store for ``episodes.sqlite``.

The schema is documented in ``docs/design/phase4-w4-d1-design.md``
§"Episode shape — schema". Two tables:

- ``episodes`` — one row per distilled narrative slice, carrying
  source-key joins, a frozen importance score, optional embedding,
  and a ``last_referenced_at`` marker that drives the cold-archive
  pruner.
- ``episode_distillation_runs`` — idempotency log; one row per pass.
  ``UNIQUE(tenant_id, window_start, window_end)`` guards re-runs.

Iter 1 shipped the bootstrap; iter 2 layers ``insert_episode`` plus
the run-log CRUD (``open_run`` / ``finish_run`` / ``find_run`` /
``sweep_stale_runs``) and a tiny ULID-ish id generator. Later iters
wire the join, classifier, distiller, and runner.

All times are unix milliseconds.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import aiosqlite

from corlinman_episodes.config import DEFAULT_TENANT_ID

# ---------------------------------------------------------------------------
# Episode kind enum
# ---------------------------------------------------------------------------


class EpisodeKind(StrEnum):
    """Narrative shape of an episode.

    The kind biases the LLM prompt segment selected at distillation
    time and is also the read-side filter for ``{{episodes.kind(<k>)}}``.
    Adding a new kind is purely additive (register a prompt segment +
    a classifier rule) so this enum is the canonical list — the Rust
    gateway resolver mirrors it.

    ``StrEnum`` (Python 3.11+) means raw column writes round-trip
    without an explicit converter and string comparisons just work.
    """

    CONVERSATION = "conversation"
    EVOLUTION = "evolution"
    INCIDENT = "incident"
    ONBOARDING = "onboarding"
    OPERATOR = "operator"

    @classmethod
    def values(cls) -> tuple[str, ...]:
        """Return the canonical ordered tuple of valid kind strings.

        Convenience for argparse ``choices=`` and the gateway resolver
        whitelist.
        """
        return tuple(k.value for k in cls)


# ---------------------------------------------------------------------------
# Run status sentinels
# ---------------------------------------------------------------------------

#: ``episode_distillation_runs.status`` enum-by-convention. Strings live
#: here so the runner sweeper and the test asserts agree on spelling
#: without a Python ``Enum`` subclass — the column is just a TEXT.
RUN_STATUS_RUNNING = "running"
RUN_STATUS_OK = "ok"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_SKIPPED_EMPTY = "skipped_empty"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

#: Idempotent ``CREATE … IF NOT EXISTS`` DDL applied on every open.
#: Matches §"Episode shape — schema" verbatim. Indexes are spelled out
#: rather than synthesised from a config so a SQLite ``EXPLAIN QUERY
#: PLAN`` against a checked-in DB matches the design doc grep output.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS episodes (
    id                  TEXT PRIMARY KEY,
    tenant_id           TEXT NOT NULL DEFAULT 'default',
    started_at          INTEGER NOT NULL,
    ended_at            INTEGER NOT NULL,
    kind                TEXT NOT NULL,
    summary_text        TEXT NOT NULL,
    source_session_keys TEXT NOT NULL DEFAULT '[]',
    source_signal_ids   TEXT NOT NULL DEFAULT '[]',
    source_history_ids  TEXT NOT NULL DEFAULT '[]',
    embedding           BLOB,
    embedding_dim       INTEGER,
    importance_score    REAL NOT NULL DEFAULT 0.5,
    last_referenced_at  INTEGER,
    distilled_by        TEXT NOT NULL,
    distilled_at        INTEGER NOT NULL,
    schema_version      INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_episodes_tenant_ended
    ON episodes(tenant_id, ended_at DESC);
CREATE INDEX IF NOT EXISTS idx_episodes_tenant_importance
    ON episodes(tenant_id, importance_score DESC, ended_at DESC);
CREATE INDEX IF NOT EXISTS idx_episodes_kind
    ON episodes(tenant_id, kind, ended_at DESC);

CREATE TABLE IF NOT EXISTS episode_distillation_runs (
    run_id           TEXT PRIMARY KEY,
    tenant_id        TEXT NOT NULL,
    window_start     INTEGER NOT NULL,
    window_end       INTEGER NOT NULL,
    started_at       INTEGER NOT NULL,
    finished_at      INTEGER,
    episodes_written INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL,
    error_message    TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_distillation_window
    ON episode_distillation_runs(tenant_id, window_start, window_end);
"""


# ---------------------------------------------------------------------------
# ULID-ish id generator
# ---------------------------------------------------------------------------

# Crockford base32 alphabet (no I, L, O, U) — matches the canonical
# ULID alphabet so ids sort lexicographically by timestamp prefix.
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _now_ms() -> int:
    """Unix milliseconds; pulled out so tests can monkeypatch."""
    return int(time.time() * 1000)


def new_episode_id(*, ts_ms: int | None = None) -> str:
    """ULID-ish identifier — 10 char ms-timestamp prefix + 16 char
    random suffix, total 26 chars.

    Sortable by ``ts_ms`` so ``ORDER BY id`` mirrors creation order
    cheaply when ``ended_at`` ties; meaningful when crashed-mid-run
    rows get retried with a near-identical timestamp. We import-time
    skip the canonical ``ulid`` dep — the encoding is short and the
    crate has no further ULID-specific needs.
    """
    ts = ts_ms if ts_ms is not None else _now_ms()
    # 10 chars * 5 bits = 50 bits, plenty for ms timestamps until 5138.
    out = []
    for _ in range(10):
        out.append(_CROCKFORD[ts & 0x1F])
        ts >>= 5
    out.reverse()
    rand_bytes = secrets.token_bytes(10)
    rand_int = int.from_bytes(rand_bytes, "big")
    rand_chars = []
    for _ in range(16):
        rand_chars.append(_CROCKFORD[rand_int & 0x1F])
        rand_int >>= 5
    rand_chars.reverse()
    return "".join(out) + "".join(rand_chars)


def new_run_id() -> str:
    """Run id is the same shape as an episode id; alias kept so call
    sites read clearly.
    """
    return new_episode_id()


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass
class Episode:
    """Plain dataclass mirror of an ``episodes`` row.

    The runner builds one of these in-memory before calling
    ``insert_episode``. Source-id lists are stored as JSON-encoded
    strings on disk; we lift them to ``list[T]`` on the dataclass for
    ergonomics. Embedding is bytes-or-None (NULL until the second
    embed pass populates it).
    """

    id: str
    tenant_id: str
    started_at: int
    ended_at: int
    kind: EpisodeKind
    summary_text: str
    source_session_keys: list[str] = field(default_factory=list)
    source_signal_ids: list[int] = field(default_factory=list)
    source_history_ids: list[int] = field(default_factory=list)
    embedding: bytes | None = None
    embedding_dim: int | None = None
    importance_score: float = 0.5
    distilled_by: str = ""
    distilled_at: int = 0
    last_referenced_at: int | None = None
    schema_version: int = 1


@dataclass(frozen=True)
class PendingEmbeddingRow:
    """Lightweight projection of an episode awaiting an embedding pass.

    Returned by :meth:`EpisodesStore.fetch_pending_embeddings` so the
    embedder doesn't have to materialise full :class:`Episode` rows
    just to call the provider with ``summary_text``. Frozen so the
    embedder can stash the list and trust ids stay stable through the
    sweep.
    """

    episode_id: str
    summary_text: str


@dataclass
class DistillationRun:
    """Plain dataclass mirror of an ``episode_distillation_runs`` row."""

    run_id: str
    tenant_id: str
    window_start: int
    window_end: int
    started_at: int
    finished_at: int | None = None
    episodes_written: int = 0
    status: str = RUN_STATUS_RUNNING
    error_message: str | None = None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RunWindowConflictError(Exception):
    """Raised when ``open_run`` collides on the unique window guard.

    Carries the conflicting window so callers can pivot to ``find_run``
    and decide whether to short-circuit or retry. Named with the
    ``Error`` suffix per ``ruff N818``; aliased as ``RunWindowConflict``
    for callers that prefer the shorter name.
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        window_start: int,
        window_end: int,
    ) -> None:
        super().__init__(
            f"distillation-run window conflict: tenant={tenant_id!r} "
            f"window=[{window_start}, {window_end})"
        )
        self.tenant_id = tenant_id
        self.window_start = window_start
        self.window_end = window_end


# Backwards-compatible alias — keep both names callable so future
# rename rounds don't churn import sites.
RunWindowConflict = RunWindowConflictError


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class EpisodesStore:
    """Async wrapper around ``episodes.sqlite``.

    Iter 1 shipped open/close + schema bootstrap. Iter 2 layers on
    ``insert_episode`` plus the run-log CRUD and a stale-run sweeper.
    Use as an async context manager so the connection closes cleanly
    even if the caller aborts mid-task.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    @classmethod
    async def open_or_create(cls, path: Path) -> EpisodesStore:
        """Open the DB (creating the file + schema if absent) and
        return an *entered* store.

        Convenience for callers that don't need ``async with`` framing
        (CLI subcommands, single-shot tests). The caller is responsible
        for ``await store.close()``.
        """
        store = cls(path)
        await store._open()
        return store

    async def __aenter__(self) -> EpisodesStore:
        await self._open()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def _open(self) -> None:
        # ``aiosqlite.connect`` will create the file on demand; the
        # schema script's ``IF NOT EXISTS`` guards make second opens
        # cheap. Foreign keys aren't used yet, but we toggle them on so
        # a future iter that adds an FK doesn't silently no-op.
        # Make sure the parent directory exists for the per-tenant
        # ``<data_dir>/tenants/<slug>/episodes.sqlite`` layout — the
        # gateway will be the caller in prod and may not have created
        # it yet for a freshly-onboarded tenant.
        os.makedirs(self._path.parent, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.executescript(SCHEMA_SQL)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("EpisodesStore used outside async context")
        return self._conn

    @property
    def path(self) -> Path:
        return self._path

    # ---- Episodes CRUD ---------------------------------------------------

    async def insert_episode(self, episode: Episode) -> None:
        """Insert one ``episodes`` row.

        ``embedding`` is allowed to be ``NULL`` so the runner can split
        the summary-write from the embedding-write (a remote-embedding
        outage shouldn't block episode persistence). Source-id lists
        are JSON-encoded inline — the design doc accepts the storage
        cost since rowcount stays low (a few thousand per tenant per
        year at most).
        """
        await self.conn.execute(
            """INSERT INTO episodes
                 (id, tenant_id, started_at, ended_at, kind, summary_text,
                  source_session_keys, source_signal_ids, source_history_ids,
                  embedding, embedding_dim, importance_score,
                  last_referenced_at, distilled_by, distilled_at,
                  schema_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                episode.id,
                episode.tenant_id,
                int(episode.started_at),
                int(episode.ended_at),
                str(episode.kind),
                episode.summary_text,
                json.dumps(list(episode.source_session_keys)),
                json.dumps([int(x) for x in episode.source_signal_ids]),
                json.dumps([int(x) for x in episode.source_history_ids]),
                episode.embedding,
                episode.embedding_dim,
                float(episode.importance_score),
                episode.last_referenced_at,
                episode.distilled_by,
                int(episode.distilled_at),
                int(episode.schema_version),
            ),
        )
        await self.conn.commit()

    async def update_episode_embedding(
        self,
        *,
        episode_id: str,
        embedding: bytes,
        embedding_dim: int,
    ) -> None:
        """Backfill the ``embedding`` BLOB + ``embedding_dim`` for one row.

        Used by :func:`corlinman_episodes.embed.populate_pending_embeddings`
        — the runner writes episodes with ``embedding=NULL`` so a
        remote-embedding outage doesn't gate summary persistence; this
        method completes the second pass.

        Both columns are stamped in the same UPDATE so a partial write
        can never leave a row with a dim but no vector. ``embedding_dim``
        is committed alongside the BLOB rather than inferred from the
        BLOB length on read so a future schema migration that changes
        the on-disk encoding (e.g. f16) doesn't have to re-derive it.
        """
        if embedding_dim <= 0:
            raise ValueError(
                f"embedding_dim must be positive, got {embedding_dim}"
            )
        # Sanity-check the BLOB size matches the declared dim (f32 = 4
        # bytes per value). The check is cheap and catches a wrong-dim
        # callable upstream before the row is committed.
        if len(embedding) != embedding_dim * 4:
            raise ValueError(
                f"embedding bytes {len(embedding)} does not match "
                f"embedding_dim*4 = {embedding_dim * 4}"
            )
        await self.conn.execute(
            """UPDATE episodes
               SET embedding = ?, embedding_dim = ?
               WHERE id = ?""",
            (embedding, int(embedding_dim), episode_id),
        )
        await self.conn.commit()

    async def fetch_pending_embeddings(
        self,
        *,
        tenant_id: str,
        limit: int | None = None,
    ) -> list[PendingEmbeddingRow]:
        """Return rows that need an embedding pass, newest first.

        The second-pass embedder selects rows where ``embedding IS NULL``
        (rather than re-checking ``embedding_dim`` or some sentinel) —
        the column is the source of truth, and the ``UPDATE`` writes
        both atomically so a half-finished row can't show up here as
        "needs work".

        ``limit=None`` means no cap; the caller can pass a small
        ``max_episodes`` to bound a single sweep when an embedding
        provider is rate-limited.
        """
        sql = (
            "SELECT id, summary_text FROM episodes "
            "WHERE tenant_id = ? AND embedding IS NULL "
            "ORDER BY ended_at DESC, id DESC"
        )
        params: tuple[Any, ...] = (tenant_id,)
        if limit is not None:
            sql += " LIMIT ?"
            params = (tenant_id, int(limit))
        cursor = await self.conn.execute(sql, params)
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            PendingEmbeddingRow(
                episode_id=str(r[0]),
                summary_text=str(r[1]),
            )
            for r in rows
        ]

    async def find_episode_by_natural_key(
        self,
        *,
        tenant_id: str,
        started_at: int,
        ended_at: int,
        kind: EpisodeKind | str,
    ) -> Episode | None:
        """Look up an episode by the ``(tenant, started, ended, kind)``
        natural key.

        Used by the runner as a guard against double-minting when a
        crashed half-flushed pass is retried — defence in depth on top
        of the run-log unique window.
        """
        cursor = await self.conn.execute(
            """SELECT id, tenant_id, started_at, ended_at, kind, summary_text,
                      source_session_keys, source_signal_ids, source_history_ids,
                      embedding, embedding_dim, importance_score,
                      last_referenced_at, distilled_by, distilled_at,
                      schema_version
               FROM episodes
               WHERE tenant_id = ?
                 AND started_at = ? AND ended_at = ?
                 AND kind = ?
               LIMIT 1""",
            (tenant_id, int(started_at), int(ended_at), str(kind)),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return _row_to_episode(row)

    # ---- Distillation-run CRUD ------------------------------------------

    async def open_run(
        self,
        *,
        tenant_id: str,
        window_start: int,
        window_end: int,
        started_at: int | None = None,
    ) -> DistillationRun:
        """Insert a ``running`` row, returning the freshly-minted run.

        Raises ``RunWindowConflictError`` if another row already exists
        for the same ``(tenant_id, window_start, window_end)`` — the
        caller handles the collision via :func:`find_run`.
        """
        run = DistillationRun(
            run_id=new_run_id(),
            tenant_id=tenant_id,
            window_start=int(window_start),
            window_end=int(window_end),
            started_at=int(started_at) if started_at is not None else _now_ms(),
            status=RUN_STATUS_RUNNING,
        )
        try:
            await self.conn.execute(
                """INSERT INTO episode_distillation_runs
                     (run_id, tenant_id, window_start, window_end,
                      started_at, status)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    run.run_id,
                    run.tenant_id,
                    run.window_start,
                    run.window_end,
                    run.started_at,
                    run.status,
                ),
            )
            await self.conn.commit()
        except aiosqlite.IntegrityError as exc:
            raise RunWindowConflictError(
                tenant_id=tenant_id,
                window_start=window_start,
                window_end=window_end,
            ) from exc
        return run

    async def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        episodes_written: int = 0,
        error_message: str | None = None,
        finished_at: int | None = None,
    ) -> None:
        """Mark a previously-opened run as terminal.

        ``status`` must be one of the non-``running`` sentinels — we
        don't enforce it in SQL (just a TEXT column) but the runner
        uses the constants and tests grep them.
        """
        if status == RUN_STATUS_RUNNING:
            raise ValueError("finish_run with status='running' is a no-op")
        await self.conn.execute(
            """UPDATE episode_distillation_runs
               SET status = ?,
                   episodes_written = ?,
                   error_message = ?,
                   finished_at = ?
               WHERE run_id = ?""",
            (
                status,
                int(episodes_written),
                error_message,
                int(finished_at) if finished_at is not None else _now_ms(),
                run_id,
            ),
        )
        await self.conn.commit()

    async def find_run(
        self,
        *,
        tenant_id: str,
        window_start: int,
        window_end: int,
    ) -> DistillationRun | None:
        """Return the run row for an exact window, if any."""
        cursor = await self.conn.execute(
            """SELECT run_id, tenant_id, window_start, window_end,
                      started_at, finished_at, episodes_written,
                      status, error_message
               FROM episode_distillation_runs
               WHERE tenant_id = ?
                 AND window_start = ? AND window_end = ?
               LIMIT 1""",
            (tenant_id, int(window_start), int(window_end)),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return _row_to_run(row)

    async def latest_ok_run(
        self,
        *,
        tenant_id: str,
    ) -> DistillationRun | None:
        """Last successful run for a tenant.

        The runner uses ``window_start = max(now - window_hours,
        latest_ok_run.window_end)`` so successive runs don't reprocess
        already-distilled material. Skipped-empty runs count as "ok"
        for window-advancement purposes — they prove the window was
        examined and yielded nothing.
        """
        cursor = await self.conn.execute(
            """SELECT run_id, tenant_id, window_start, window_end,
                      started_at, finished_at, episodes_written,
                      status, error_message
               FROM episode_distillation_runs
               WHERE tenant_id = ?
                 AND status IN (?, ?)
               ORDER BY window_end DESC, started_at DESC
               LIMIT 1""",
            (tenant_id, RUN_STATUS_OK, RUN_STATUS_SKIPPED_EMPTY),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return _row_to_run(row)

    async def sweep_stale_runs(
        self,
        *,
        now_ms: int,
        stale_after_secs: int,
    ) -> list[str]:
        """Mark ``running`` rows older than ``stale_after_secs`` as
        ``failed``.

        Returns the swept ``run_id`` list so the caller can log.
        Mirrors the ``apply_intent_log`` crash-resume contract called
        out in the design doc — a previous run that crashed mid-flight
        leaves a half-row behind; a new run sweeps it before opening
        its own row, so the unique-window guard doesn't fire on the
        ghost.
        """
        threshold = now_ms - (stale_after_secs * 1000)
        cursor = await self.conn.execute(
            """SELECT run_id FROM episode_distillation_runs
               WHERE status = ? AND started_at < ?""",
            (RUN_STATUS_RUNNING, threshold),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        swept = [str(r[0]) for r in rows]
        if not swept:
            return []
        await self.conn.execute(
            """UPDATE episode_distillation_runs
               SET status = ?,
                   finished_at = ?,
                   error_message = COALESCE(error_message, 'sweeper: stale running row')
               WHERE status = ? AND started_at < ?""",
            (RUN_STATUS_FAILED, now_ms, RUN_STATUS_RUNNING, threshold),
        )
        await self.conn.commit()
        return swept


# ---------------------------------------------------------------------------
# Row decoders (private)
# ---------------------------------------------------------------------------


def _decode_int_list(raw: str) -> list[int]:
    """Decode the JSON-encoded ``source_*_ids`` columns; tolerate junk."""
    try:
        parsed = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    out: list[int] = []
    for v in parsed:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return out


def _decode_str_list(raw: str) -> list[str]:
    """Decode the JSON-encoded ``source_session_keys`` column."""
    try:
        parsed = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(v) for v in parsed]


def _coerce_kind(raw: str) -> EpisodeKind:
    """Parse a kind string from the DB, defaulting to ``CONVERSATION``
    on unknown values rather than raising — the resolver shouldn't
    crash on a stray row left over from a future EpisodeKind.
    """
    try:
        return EpisodeKind(raw)
    except ValueError:
        return EpisodeKind.CONVERSATION


def _row_to_episode(row: aiosqlite.Row | tuple[Any, ...]) -> Episode:
    return Episode(
        id=str(row[0]),
        tenant_id=str(row[1]),
        started_at=int(row[2]),
        ended_at=int(row[3]),
        kind=_coerce_kind(str(row[4])),
        summary_text=str(row[5]),
        source_session_keys=_decode_str_list(str(row[6])),
        source_signal_ids=_decode_int_list(str(row[7])),
        source_history_ids=_decode_int_list(str(row[8])),
        embedding=bytes(row[9]) if row[9] is not None else None,
        embedding_dim=int(row[10]) if row[10] is not None else None,
        importance_score=float(row[11]),
        last_referenced_at=int(row[12]) if row[12] is not None else None,
        distilled_by=str(row[13]),
        distilled_at=int(row[14]),
        schema_version=int(row[15]),
    )


def _row_to_run(row: aiosqlite.Row | tuple[Any, ...]) -> DistillationRun:
    return DistillationRun(
        run_id=str(row[0]),
        tenant_id=str(row[1]),
        window_start=int(row[2]),
        window_end=int(row[3]),
        started_at=int(row[4]),
        finished_at=int(row[5]) if row[5] is not None else None,
        episodes_written=int(row[6]),
        status=str(row[7]),
        error_message=str(row[8]) if row[8] is not None else None,
    )


__all__ = [
    "DEFAULT_TENANT_ID",
    "RUN_STATUS_FAILED",
    "RUN_STATUS_OK",
    "RUN_STATUS_RUNNING",
    "RUN_STATUS_SKIPPED_EMPTY",
    "SCHEMA_SQL",
    "DistillationRun",
    "Episode",
    "EpisodeKind",
    "EpisodesStore",
    "PendingEmbeddingRow",
    "RunWindowConflict",
    "RunWindowConflictError",
    "new_episode_id",
    "new_run_id",
]
