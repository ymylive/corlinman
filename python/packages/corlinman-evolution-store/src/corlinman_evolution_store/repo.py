"""Async repos for the four evolution tables.

Ported 1:1 from ``rust/crates/corlinman-evolution/src/repo.rs``. The four
public types — :class:`SignalsRepo`, :class:`ProposalsRepo`,
:class:`HistoryRepo`, :class:`IntentLogRepo` — wrap one shared
:class:`aiosqlite.Connection` (handed in via the
:class:`~corlinman_evolution_store.store.EvolutionStore`).

Time convention: callers pass unix-millisecond timestamps explicitly.
There is no implicit clock here so replay / test paths stay
deterministic.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import aiosqlite

from corlinman_evolution_store.types import (
    DEFAULT_TENANT_ID,
    EvolutionHistory,
    EvolutionKind,
    EvolutionProposal,
    EvolutionRisk,
    EvolutionSignal,
    EvolutionStatus,
    Json,
    ParseError,
    ProposalId,
    ShadowMetrics,
    SignalSeverity,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RepoError(RuntimeError):
    """Base class for every error raised by a repo. Mirrors the Rust
    ``RepoError`` enum — subclasses below cover the typed variants."""


class SqliteRepoError(RepoError):
    """Wraps an underlying ``sqlite3.Error`` / ``aiosqlite`` exception."""


class MalformedJsonError(RepoError):
    """Raised when a JSON-typed column fails to serialise / deserialise."""

    def __init__(self, column: str, source: Exception) -> None:
        super().__init__(f"malformed json column '{column}': {source}")
        self.column = column
        self.source = source


class MalformedEnumError(RepoError):
    """Raised when a TEXT enum column holds a value outside the known set."""

    def __init__(self, column: str, value: str) -> None:
        super().__init__(f"malformed enum '{column}': {value}")
        self.column = column
        self.value = value


class NotFoundError(RepoError):
    """Raised when a row keyed on an ``id`` / ``proposal_id`` is absent —
    or when an UPDATE matched zero rows because the WHERE clause's
    status guard refused the transition."""

    def __init__(self, id_: str) -> None:
        super().__init__(f"not found: {id_}")
        self.id = id_


class RecursionGuardViolationError(RepoError):
    """Clause A of the meta recursion guard — refuse a meta proposal
    whose ``metadata.parent_meta_proposal_id`` resolves to another meta
    row."""

    def __init__(self, parent_id: str, parent_kind: EvolutionKind) -> None:
        super().__init__(
            f"recursion guard: meta proposal descends from another meta proposal "
            f"(parent_id={parent_id}, parent_kind={parent_kind!r})"
        )
        self.parent_id = parent_id
        self.parent_kind = parent_kind


class RecursionGuardCooldownError(RepoError):
    """Clause B of the meta recursion guard — refuse a meta proposal
    when the same ``(tenant_id, kind)`` saw an applied / rolled-back
    meta row inside the configured cooldown window."""

    def __init__(
        self,
        last_applied_at_ms: int,
        window_secs: int,
        remaining_secs: int,
    ) -> None:
        super().__init__(
            f"recursion guard cooldown: last meta apply at {last_applied_at_ms}ms "
            f"within {window_secs}s window ({remaining_secs}s remaining)"
        )
        self.last_applied_at_ms = last_applied_at_ms
        self.window_secs = window_secs
        self.remaining_secs = remaining_secs


# ---------------------------------------------------------------------------
# Guard config & helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvolutionGuardConfig:
    """Phase 4 W2 B1 iter 3 configuration for the dual-clause meta
    recursion guard. Default value of ``meta_kind_cooldown_secs`` is
    one hour, matching the Rust default."""

    meta_kind_cooldown_secs: int = 3_600


def iso_week_window(now_ms: int) -> tuple[int, int]:
    """``(start_ms, end_ms)`` for the ISO week containing ``now_ms``.

    Start is Monday 00:00:00 UTC inclusive; end is the following Monday
    00:00:00 UTC exclusive. Pure helper so the admin API stamps the
    same window it queries against without re-deriving from a fresh
    ``now``.
    """
    now = datetime.fromtimestamp(now_ms / 1000.0, tz=UTC)
    days_since_monday = now.weekday()  # Monday = 0
    monday_date = now.date() - timedelta(days=days_since_monday)
    start = datetime(
        monday_date.year, monday_date.month, monday_date.day, 0, 0, 0, tzinfo=UTC
    )
    end = start + timedelta(days=7)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    return start_ms, end_ms


def _parent_meta_proposal_id_from_metadata(metadata: Json | None) -> str | None:
    """Pull ``metadata.parent_meta_proposal_id`` out of the free-form
    metadata blob. Returns ``None`` when the metadata is absent, not an
    object, missing the key, JSON-null, or not a string — defensive
    against operator hand-edits and out-of-band keys (B3's
    ``federated_from`` etc.) that coexist in the same blob."""
    if not isinstance(metadata, dict):
        return None
    raw = metadata.get("parent_meta_proposal_id")
    if raw is None:
        return None
    if not isinstance(raw, str):
        return None
    return raw


def _json_dumps(value: Any, column: str) -> str:
    try:
        return json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise MalformedJsonError(column, exc) from exc


def _json_loads(raw: str, column: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MalformedJsonError(column, exc) from exc


def _enum_or_raise(parse_fn, column: str, value: str):
    try:
        return parse_fn(value)
    except ParseError as exc:
        raise MalformedEnumError(column, value) from exc


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------


class SignalsRepo:
    """Async repo for the ``evolution_signals`` table."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def insert(self, signal: EvolutionSignal) -> int:
        """Insert one signal. Returns the autoincrement id."""
        payload = _json_dumps(signal.payload_json, "payload_json")
        cursor = await self._conn.execute(
            """INSERT INTO evolution_signals
                 (event_kind, target, severity, payload_json, trace_id,
                  session_id, observed_at, tenant_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               RETURNING id""",
            (
                signal.event_kind,
                signal.target,
                signal.severity.as_str(),
                payload,
                signal.trace_id,
                signal.session_id,
                signal.observed_at,
                signal.tenant_id,
            ),
        )
        row = await cursor.fetchone()
        await cursor.close()
        await self._conn.commit()
        assert row is not None
        return int(row[0])

    async def list_since(
        self,
        since_ms: int,
        event_kind: str | None,
        limit: int,
    ) -> list[EvolutionSignal]:
        """Read signals observed in ``[since_ms, ∞)``, optionally
        filtered by ``event_kind``. Used by the Python engine when
        clustering."""
        if event_kind is not None:
            cursor = await self._conn.execute(
                """SELECT id, event_kind, target, severity, payload_json,
                          trace_id, session_id, observed_at, tenant_id
                   FROM evolution_signals
                   WHERE observed_at >= ? AND event_kind = ?
                   ORDER BY observed_at ASC
                   LIMIT ?""",
                (since_ms, event_kind, limit),
            )
        else:
            cursor = await self._conn.execute(
                """SELECT id, event_kind, target, severity, payload_json,
                          trace_id, session_id, observed_at, tenant_id
                   FROM evolution_signals
                   WHERE observed_at >= ?
                   ORDER BY observed_at ASC
                   LIMIT ?""",
                (since_ms, limit),
            )
        rows = await cursor.fetchall()
        await cursor.close()
        return [self._decode_signal(r) for r in rows]

    async def prune_before(self, before_ms: int) -> int:
        """Delete signals older than ``before_ms``. Returns rows
        affected."""
        cursor = await self._conn.execute(
            "DELETE FROM evolution_signals WHERE observed_at < ?",
            (before_ms,),
        )
        affected = cursor.rowcount
        await cursor.close()
        await self._conn.commit()
        return int(affected)

    @staticmethod
    def _decode_signal(row: aiosqlite.Row | tuple[Any, ...]) -> EvolutionSignal:
        severity_raw = str(row[3])
        severity = _enum_or_raise(SignalSeverity.from_str, "severity", severity_raw)
        payload_str = str(row[4])
        payload_json = _json_loads(payload_str, "payload_json")
        return EvolutionSignal(
            id=int(row[0]),
            event_kind=str(row[1]),
            target=None if row[2] is None else str(row[2]),
            severity=severity,
            payload_json=payload_json,
            trace_id=None if row[5] is None else str(row[5]),
            session_id=None if row[6] is None else str(row[6]),
            observed_at=int(row[7]),
            tenant_id=str(row[8]),
        )


# ---------------------------------------------------------------------------
# Proposals
# ---------------------------------------------------------------------------


_PROPOSAL_COLUMNS = (
    "id, kind, target, diff, reasoning, risk, budget_cost, status, "
    "shadow_metrics, signal_ids, trace_ids, "
    "created_at, decided_at, decided_by, applied_at, rollback_of, "
    "eval_run_id, baseline_metrics_json, "
    "auto_rollback_at, auto_rollback_reason, "
    "metadata"
)


class ProposalsRepo:
    """Async repo for ``evolution_proposals``.

    Opt into the Phase 4 W2 B1 iter 3 meta recursion guard via
    :meth:`with_guard`. Unguarded repos take the legacy fast path (a
    single INSERT, no SELECTs) on every insert.
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        self._guard: EvolutionGuardConfig | None = None

    def with_guard(self, cfg: EvolutionGuardConfig) -> ProposalsRepo:
        """Builder — enable the dual-clause recursion guard. Returns
        ``self`` for chaining."""
        self._guard = cfg
        return self

    async def insert(self, proposal: EvolutionProposal) -> None:
        if self._guard is not None and proposal.kind.is_meta():
            await self._check_meta_recursion_guard(proposal, self._guard)

        signal_ids = _json_dumps(list(proposal.signal_ids), "signal_ids")
        trace_ids = _json_dumps(list(proposal.trace_ids), "trace_ids")
        shadow_metrics = None
        if proposal.shadow_metrics is not None:
            shadow_metrics = _json_dumps(proposal.shadow_metrics.data, "shadow_metrics")
        metadata = None
        if proposal.metadata is not None:
            metadata = _json_dumps(proposal.metadata, "metadata")

        await self._conn.execute(
            """INSERT INTO evolution_proposals
                 (id, kind, target, diff, reasoning, risk, budget_cost, status,
                  shadow_metrics, signal_ids, trace_ids,
                  created_at, decided_at, decided_by, applied_at, rollback_of,
                  metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                proposal.id,
                proposal.kind.as_str(),
                proposal.target,
                proposal.diff,
                proposal.reasoning,
                proposal.risk.as_str(),
                int(proposal.budget_cost),
                proposal.status.as_str(),
                shadow_metrics,
                signal_ids,
                trace_ids,
                proposal.created_at,
                proposal.decided_at,
                proposal.decided_by,
                proposal.applied_at,
                proposal.rollback_of,
                metadata,
            ),
        )
        await self._conn.commit()

    async def _check_meta_recursion_guard(
        self,
        proposal: EvolutionProposal,
        cfg: EvolutionGuardConfig,
    ) -> None:
        # Clause A — semantic descent via metadata.parent_meta_proposal_id.
        parent_id = _parent_meta_proposal_id_from_metadata(proposal.metadata)
        if parent_id is not None:
            cursor = await self._conn.execute(
                "SELECT kind FROM evolution_proposals WHERE id = ?",
                (parent_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is not None:
                kind_str = str(row[0])
                parent_kind = _enum_or_raise(EvolutionKind.from_str, "kind", kind_str)
                if parent_kind.is_meta():
                    raise RecursionGuardViolationError(parent_id, parent_kind)

        # Clause B — temporal cooldown per (tenant_id, kind).
        cursor = await self._conn.execute(
            "SELECT MAX(applied_at) FROM evolution_proposals "
            " WHERE tenant_id = ? AND kind = ? "
            "   AND status IN ('applied', 'rolled_back') "
            "   AND applied_at IS NOT NULL",
            (DEFAULT_TENANT_ID, proposal.kind.as_str()),
        )
        row = await cursor.fetchone()
        await cursor.close()
        last_applied_at_ms = None if row is None or row[0] is None else int(row[0])
        if last_applied_at_ms is not None:
            window_ms = cfg.meta_kind_cooldown_secs * 1_000
            elapsed_ms = proposal.created_at - last_applied_at_ms
            if elapsed_ms < window_ms:
                remaining_ms = window_ms - max(elapsed_ms, 0)
                # Round up so the operator-facing message never claims
                # "0s remaining" while the gate is still shut.
                remaining_secs = max(0, (remaining_ms + 999) // 1_000)
                raise RecursionGuardCooldownError(
                    last_applied_at_ms=last_applied_at_ms,
                    window_secs=cfg.meta_kind_cooldown_secs,
                    remaining_secs=int(remaining_secs),
                )

    async def get(self, id_: ProposalId) -> EvolutionProposal:
        cursor = await self._conn.execute(
            f"SELECT {_PROPOSAL_COLUMNS} FROM evolution_proposals WHERE id = ?",
            (id_,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            raise NotFoundError(str(id_))
        return _decode_proposal(row)

    async def list_by_status(
        self,
        status: EvolutionStatus,
        limit: int,
    ) -> list[EvolutionProposal]:
        cursor = await self._conn.execute(
            f"""SELECT {_PROPOSAL_COLUMNS}
                FROM evolution_proposals
                WHERE status = ?
                ORDER BY created_at DESC
                LIMIT ?""",
            (status.as_str(), limit),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [_decode_proposal(r) for r in rows]

    async def set_decision(
        self,
        id_: ProposalId,
        new_status: EvolutionStatus,
        decided_at_ms: int,
        decided_by: str,
    ) -> None:
        """Patch ``status`` + ``decided_at`` + ``decided_by`` atomically.
        Used by the admin API on approve / deny."""
        cursor = await self._conn.execute(
            "UPDATE evolution_proposals "
            "  SET status = ?, decided_at = ?, decided_by = ? "
            "WHERE id = ?",
            (new_status.as_str(), decided_at_ms, decided_by, id_),
        )
        affected = cursor.rowcount
        await cursor.close()
        await self._conn.commit()
        if affected == 0:
            raise NotFoundError(str(id_))

    async def mark_applied(self, id_: ProposalId, applied_at_ms: int) -> None:
        """Patch ``status`` + ``applied_at`` when the EvolutionApplier
        finishes."""
        cursor = await self._conn.execute(
            "UPDATE evolution_proposals "
            "  SET status = 'applied', applied_at = ? "
            "WHERE id = ?",
            (applied_at_ms, id_),
        )
        affected = cursor.rowcount
        await cursor.close()
        await self._conn.commit()
        if affected == 0:
            raise NotFoundError(str(id_))

    async def mark_auto_rolled_back(
        self,
        id_: ProposalId,
        rolled_back_at_ms: int,
        reason: str,
    ) -> None:
        """Phase 3 W1-B AutoRollback transition ``applied → rolled_back``
        plus audit fields. The ``WHERE status = 'applied'`` clause makes
        a double-revert race surface as :class:`NotFoundError` instead
        of a silent second rollback."""
        cursor = await self._conn.execute(
            "UPDATE evolution_proposals "
            "  SET status = 'rolled_back', "
            "      auto_rollback_at = ?, "
            "      auto_rollback_reason = ? "
            "WHERE id = ? AND status = 'applied'",
            (rolled_back_at_ms, reason, id_),
        )
        affected = cursor.rowcount
        await cursor.close()
        await self._conn.commit()
        if affected == 0:
            raise NotFoundError(str(id_))

    async def list_applied_in_grace_window(
        self,
        now_ms: int,
        grace_window_hours: int,
        limit: int,
    ) -> list[EvolutionProposal]:
        """List proposals applied within ``[now_ms - grace_hours,
        now_ms]`` that are still in :attr:`EvolutionStatus.APPLIED`."""
        since_ms = now_ms - grace_window_hours * 3_600 * 1_000
        cursor = await self._conn.execute(
            f"""SELECT {_PROPOSAL_COLUMNS}
                FROM evolution_proposals
                WHERE status = 'applied'
                  AND applied_at IS NOT NULL
                  AND applied_at >= ?
                  AND applied_at <= ?
                ORDER BY applied_at DESC
                LIMIT ?""",
            (since_ms, now_ms, limit),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [_decode_proposal(r) for r in rows]

    async def list_pending_for_shadow(
        self,
        kind: EvolutionKind,
        risks: list[EvolutionRisk],
        limit: int,
    ) -> list[EvolutionProposal]:
        """List ``pending`` proposals for ``kind`` whose risk is in
        ``risks``, newest first. Used by the ShadowRunner."""
        if not risks:
            return []
        placeholders = ",".join("?" for _ in risks)
        sql = (
            f"SELECT {_PROPOSAL_COLUMNS} "
            "FROM evolution_proposals "
            f"WHERE status = 'pending' AND kind = ? AND risk IN ({placeholders}) "
            "ORDER BY created_at DESC "
            "LIMIT ?"
        )
        params: list[Any] = [kind.as_str()]
        params.extend(r.as_str() for r in risks)
        params.append(limit)
        cursor = await self._conn.execute(sql, params)
        rows = await cursor.fetchall()
        await cursor.close()
        return [_decode_proposal(r) for r in rows]

    async def claim_for_shadow(self, id_: ProposalId) -> None:
        """Atomically transition a proposal from ``pending`` to
        ``shadow_running``. Raises :class:`NotFoundError` if the row is
        not in ``pending`` (avoids racing two runners)."""
        cursor = await self._conn.execute(
            "UPDATE evolution_proposals "
            "  SET status = 'shadow_running' "
            "WHERE id = ? AND status = 'pending'",
            (id_,),
        )
        affected = cursor.rowcount
        await cursor.close()
        await self._conn.commit()
        if affected == 0:
            raise NotFoundError(str(id_))

    async def count_proposals_in_iso_week(
        self,
        now_ms: int,
        kind: EvolutionKind | None,
    ) -> int:
        """Count proposals whose ``created_at`` falls within the ISO
        week containing ``now_ms``, optionally filtered to one kind.

        Used by the W1-C budget gate; every status counts because the
        budget caps the *file rate*, not the net effect.
        """
        start_ms, end_ms = iso_week_window(now_ms)
        if kind is not None:
            cursor = await self._conn.execute(
                "SELECT COUNT(*) FROM evolution_proposals "
                " WHERE created_at >= ? AND created_at < ? AND kind = ?",
                (start_ms, end_ms, kind.as_str()),
            )
        else:
            cursor = await self._conn.execute(
                "SELECT COUNT(*) FROM evolution_proposals "
                " WHERE created_at >= ? AND created_at < ?",
                (start_ms, end_ms),
            )
        row = await cursor.fetchone()
        await cursor.close()
        count = 0 if row is None or row[0] is None else int(row[0])
        # Clamp to u32-equivalent range to match Rust semantics.
        return max(0, min(count, 0xFFFF_FFFF))

    async def mark_shadow_done(
        self,
        id_: ProposalId,
        eval_run_id: str,
        baseline_metrics_json: Json,
        shadow_metrics: Json,
    ) -> None:
        """Persist shadow run output: ``eval_run_id``,
        ``baseline_metrics_json``, ``shadow_metrics``, and transition
        ``shadow_running → shadow_done`` in one UPDATE."""
        baseline = _json_dumps(baseline_metrics_json, "baseline_metrics_json")
        shadow = _json_dumps(shadow_metrics, "shadow_metrics")
        cursor = await self._conn.execute(
            "UPDATE evolution_proposals "
            "  SET status = 'shadow_done', "
            "      eval_run_id = ?, "
            "      baseline_metrics_json = ?, "
            "      shadow_metrics = ? "
            "WHERE id = ?",
            (eval_run_id, baseline, shadow, id_),
        )
        affected = cursor.rowcount
        await cursor.close()
        await self._conn.commit()
        if affected == 0:
            raise NotFoundError(str(id_))


def _decode_proposal(row: aiosqlite.Row | tuple[Any, ...]) -> EvolutionProposal:
    """Decode one SELECT row into :class:`EvolutionProposal`. Column
    order must match :data:`_PROPOSAL_COLUMNS`."""
    proposal_id_str = str(row[0])
    kind = _enum_or_raise(EvolutionKind.from_str, "kind", str(row[1]))
    risk = _enum_or_raise(EvolutionRisk.from_str, "risk", str(row[5]))
    status = _enum_or_raise(EvolutionStatus.from_str, "status", str(row[7]))

    signal_ids_raw = str(row[9])
    parsed_signal_ids = _json_loads(signal_ids_raw, "signal_ids")
    if not isinstance(parsed_signal_ids, list):
        raise MalformedJsonError(
            "signal_ids", TypeError("expected list of int")
        )
    signal_ids: list[int] = [int(x) for x in parsed_signal_ids]

    trace_ids_raw = str(row[10])
    parsed_trace_ids = _json_loads(trace_ids_raw, "trace_ids")
    if not isinstance(parsed_trace_ids, list):
        raise MalformedJsonError(
            "trace_ids", TypeError("expected list of str")
        )
    trace_ids: list[str] = [str(x) for x in parsed_trace_ids]

    shadow_metrics: ShadowMetrics | None = None
    if row[8] is not None:
        parsed = _json_loads(str(row[8]), "shadow_metrics")
        if not isinstance(parsed, dict):
            raise MalformedJsonError(
                "shadow_metrics", TypeError("expected JSON object")
            )
        shadow_metrics = ShadowMetrics(data=dict(parsed))

    baseline_metrics_json: Json | None = None
    if row[17] is not None:
        baseline_metrics_json = _json_loads(str(row[17]), "baseline_metrics_json")

    # Tolerant decode of metadata: corrupt TEXT downgrades to None with
    # a logging.warning, mirroring the Rust ``tracing::warn!`` path.
    metadata: Json | None = None
    if row[20] is not None:
        try:
            metadata = json.loads(str(row[20]))
        except json.JSONDecodeError as exc:
            logger.warning(
                "evolution_proposals.metadata held non-JSON TEXT; decoding as None"
                " (proposal_id=%s, error=%s)",
                proposal_id_str,
                exc,
            )
            metadata = None

    return EvolutionProposal(
        id=ProposalId(proposal_id_str),
        kind=kind,
        target=str(row[2]),
        diff=str(row[3]),
        reasoning=str(row[4]),
        risk=risk,
        budget_cost=int(row[6]),
        status=status,
        shadow_metrics=shadow_metrics,
        signal_ids=signal_ids,
        trace_ids=trace_ids,
        created_at=int(row[11]),
        decided_at=None if row[12] is None else int(row[12]),
        decided_by=None if row[13] is None else str(row[13]),
        applied_at=None if row[14] is None else int(row[14]),
        rollback_of=None if row[15] is None else ProposalId(str(row[15])),
        eval_run_id=None if row[16] is None else str(row[16]),
        baseline_metrics_json=baseline_metrics_json,
        auto_rollback_at=None if row[18] is None else int(row[18]),
        auto_rollback_reason=None if row[19] is None else str(row[19]),
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


class HistoryRepo:
    """Async repo for ``evolution_history``."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def insert(self, h: EvolutionHistory) -> int:
        metrics = _json_dumps(h.metrics_baseline, "metrics_baseline")
        share_with: str | None = None
        if h.share_with is not None:
            share_with = _json_dumps(list(h.share_with), "share_with")
        cursor = await self._conn.execute(
            """INSERT INTO evolution_history
                 (proposal_id, kind, target, before_sha, after_sha,
                  inverse_diff, metrics_baseline, applied_at,
                  rolled_back_at, rollback_reason, share_with)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               RETURNING id""",
            (
                h.proposal_id,
                h.kind.as_str(),
                h.target,
                h.before_sha,
                h.after_sha,
                h.inverse_diff,
                metrics,
                h.applied_at,
                h.rolled_back_at,
                h.rollback_reason,
                share_with,
            ),
        )
        row = await cursor.fetchone()
        await cursor.close()
        await self._conn.commit()
        assert row is not None
        return int(row[0])

    async def latest_for_proposal(
        self,
        proposal_id: ProposalId,
    ) -> EvolutionHistory:
        """Most recent history row for a given proposal. Used by the
        AutoRollback revert path to fetch ``inverse_diff``."""
        cursor = await self._conn.execute(
            """SELECT id, proposal_id, kind, target, before_sha, after_sha,
                      inverse_diff, metrics_baseline, applied_at,
                      rolled_back_at, rollback_reason, share_with
               FROM evolution_history
               WHERE proposal_id = ?
               ORDER BY applied_at DESC, id DESC
               LIMIT 1""",
            (proposal_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            raise NotFoundError(str(proposal_id))

        kind = _enum_or_raise(EvolutionKind.from_str, "kind", str(row[2]))
        metrics_baseline = _json_loads(str(row[7]), "metrics_baseline")

        # Tolerant decode mirroring evolution_proposals.metadata.
        share_with: list[str] | None = None
        if row[11] is not None:
            try:
                parsed = json.loads(str(row[11]))
                if isinstance(parsed, list):
                    share_with = [str(x) for x in parsed]
                else:
                    logger.warning(
                        "evolution_history.share_with was JSON but not a list;"
                        " decoding as None (proposal_id=%s)",
                        proposal_id,
                    )
                    share_with = None
            except json.JSONDecodeError as exc:
                logger.warning(
                    "evolution_history.share_with held non-JSON TEXT;"
                    " decoding as None (proposal_id=%s, error=%s)",
                    proposal_id,
                    exc,
                )
                share_with = None

        return EvolutionHistory(
            id=int(row[0]),
            proposal_id=ProposalId(str(row[1])),
            kind=kind,
            target=str(row[3]),
            before_sha=str(row[4]),
            after_sha=str(row[5]),
            inverse_diff=str(row[6]),
            metrics_baseline=metrics_baseline,
            applied_at=int(row[8]),
            rolled_back_at=None if row[9] is None else int(row[9]),
            rollback_reason=None if row[10] is None else str(row[10]),
            share_with=share_with,
        )

    async def mark_rolled_back(
        self,
        proposal_id: ProposalId,
        rolled_back_at_ms: int,
        reason: str,
    ) -> None:
        cursor = await self._conn.execute(
            "UPDATE evolution_history "
            "  SET rolled_back_at = ?, rollback_reason = ? "
            "WHERE proposal_id = ?",
            (rolled_back_at_ms, reason, proposal_id),
        )
        affected = cursor.rowcount
        await cursor.close()
        await self._conn.commit()
        if affected == 0:
            raise NotFoundError(str(proposal_id))


# ---------------------------------------------------------------------------
# Intent log
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApplyIntent:
    """One row from ``apply_intent_log`` — the subset read by the
    half-committed scan at gateway startup."""

    id: int
    proposal_id: str
    kind: str
    target: str
    intent_at: int


class IntentLogRepo:
    """Async repo for ``apply_intent_log`` — Phase 3.1 forward-apply
    intent log. Write a row before the kb mutation, stamp
    ``committed_at`` / ``failed_at`` after."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def record_intent(
        self,
        proposal_id: str,
        kind: str,
        target: str,
        intent_at_ms: int,
    ) -> int:
        """Open a new intent. Returns the autoincrement id — caller
        passes it back to :meth:`mark_committed` / :meth:`mark_failed`
        so the stamp updates exactly the row we opened."""
        cursor = await self._conn.execute(
            """INSERT INTO apply_intent_log
                 (proposal_id, kind, target, intent_at,
                  committed_at, failed_at, failure_reason)
               VALUES (?, ?, ?, ?, NULL, NULL, NULL)
               RETURNING id""",
            (proposal_id, kind, target, intent_at_ms),
        )
        row = await cursor.fetchone()
        await cursor.close()
        await self._conn.commit()
        assert row is not None
        return int(row[0])

    async def mark_committed(
        self,
        intent_id: int,
        committed_at_ms: int,
    ) -> None:
        """Stamp ``committed_at``. Idempotent: a second call no-ops on
        the partial-index hot path because the row no longer matches
        the ``committed_at IS NULL`` predicate."""
        await self._conn.execute(
            "UPDATE apply_intent_log SET committed_at = ? "
            "WHERE id = ? AND committed_at IS NULL AND failed_at IS NULL",
            (committed_at_ms, intent_id),
        )
        await self._conn.commit()

    async def mark_failed(
        self,
        intent_id: int,
        failed_at_ms: int,
        reason: str,
    ) -> None:
        """Stamp ``failed_at`` + ``failure_reason``."""
        await self._conn.execute(
            "UPDATE apply_intent_log SET failed_at = ?, failure_reason = ? "
            "WHERE id = ? AND committed_at IS NULL AND failed_at IS NULL",
            (failed_at_ms, reason, intent_id),
        )
        await self._conn.commit()

    async def list_uncommitted(self) -> list[ApplyIntent]:
        """Every row that opened an intent and never reached a terminal
        stamp. Sorted oldest-first so the operator sees the longest-
        outstanding tickets at the top."""
        cursor = await self._conn.execute(
            """SELECT id, proposal_id, kind, target, intent_at
               FROM apply_intent_log
               WHERE committed_at IS NULL AND failed_at IS NULL
               ORDER BY intent_at ASC"""
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            ApplyIntent(
                id=int(r[0]),
                proposal_id=str(r[1]),
                kind=str(r[2]),
                target=str(r[3]),
                intent_at=int(r[4]),
            )
            for r in rows
        ]


# ---------------------------------------------------------------------------
# Curator state (Phase 4 W4.2)
# ---------------------------------------------------------------------------


# DDL defaults — kept in sync with ``curator_state`` in
# ``schema.SCHEMA_SQL``. Synthesised onto :class:`CuratorState` rows
# when no row exists yet, so callers always get a usable struct.
_CURATOR_DEFAULT_INTERVAL_HOURS: int = 168
_CURATOR_DEFAULT_STALE_AFTER_DAYS: int = 30
_CURATOR_DEFAULT_ARCHIVE_AFTER_DAYS: int = 90


@dataclass(frozen=True)
class CuratorState:
    """One row of ``curator_state`` — per-profile curator-loop bookkeeping.

    Ported from hermes ``agent/curator.py`` (where the same shape lived
    in a JSON ``.curator_state`` file). Times stored as unix-millisecond
    ``int`` at the SQL boundary; surfaced as ``datetime`` here to match
    Python ergonomics. ``paused`` is the SQL 0/1 surface as ``bool``.
    """

    profile_slug: str
    last_review_at: datetime | None
    last_review_duration_ms: int | None
    last_review_summary: str | None
    run_count: int
    paused: bool
    interval_hours: int
    stale_after_days: int
    archive_after_days: int
    tenant_id: str = DEFAULT_TENANT_ID


def _dt_to_ms(value: datetime | None) -> int | None:
    """Convert a timezone-aware ``datetime`` to unix milliseconds. Naive
    inputs are assumed UTC — matches the rest of the codebase's
    convention (e.g. :func:`iso_week_window`)."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def _ms_to_dt(value: int | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(int(value) / 1000.0, tz=UTC)


def _default_curator_state(profile_slug: str, tenant_id: str) -> CuratorState:
    """Synthetic default row used when no DB row exists yet. Values
    mirror the column DEFAULTs in :data:`schema.SCHEMA_SQL`."""
    return CuratorState(
        profile_slug=profile_slug,
        last_review_at=None,
        last_review_duration_ms=None,
        last_review_summary=None,
        run_count=0,
        paused=False,
        interval_hours=_CURATOR_DEFAULT_INTERVAL_HOURS,
        stale_after_days=_CURATOR_DEFAULT_STALE_AFTER_DAYS,
        archive_after_days=_CURATOR_DEFAULT_ARCHIVE_AFTER_DAYS,
        tenant_id=tenant_id,
    )


_CURATOR_COLUMNS = (
    "profile_slug, last_review_at, last_review_duration_ms, "
    "last_review_summary, run_count, paused, interval_hours, "
    "stale_after_days, archive_after_days, tenant_id"
)


def _decode_curator_state(row: aiosqlite.Row | tuple[Any, ...]) -> CuratorState:
    """Decode one SELECT row into :class:`CuratorState`. Column order
    must match :data:`_CURATOR_COLUMNS`."""
    return CuratorState(
        profile_slug=str(row[0]),
        last_review_at=_ms_to_dt(None if row[1] is None else int(row[1])),
        last_review_duration_ms=None if row[2] is None else int(row[2]),
        last_review_summary=None if row[3] is None else str(row[3]),
        run_count=int(row[4]),
        paused=bool(int(row[5])),
        interval_hours=int(row[6]),
        stale_after_days=int(row[7]),
        archive_after_days=int(row[8]),
        tenant_id=str(row[9]),
    )


class CuratorStateRepo:
    """Async repo for the ``curator_state`` table.

    Used by the curator loop (W4.3) to remember when it last ran for
    a profile, and by ``/admin/evolution`` (W4.6) to render an overview
    of every profile's curator status.
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def get(
        self,
        profile_slug: str,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> CuratorState:
        """Fetch one profile's curator state. **Never raises**
        :class:`NotFoundError` — when no row exists yet we synthesise
        one with the DDL-default values (``paused=False``,
        ``run_count=0``, ``last_review_at=None``, …) so callers can treat
        the result as a struct rather than ``Optional[CuratorState]``."""
        cursor = await self._conn.execute(
            f"SELECT {_CURATOR_COLUMNS} FROM curator_state "
            " WHERE profile_slug = ? AND tenant_id = ?",
            (profile_slug, tenant_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return _default_curator_state(profile_slug, tenant_id)
        return _decode_curator_state(row)

    async def upsert(self, state: CuratorState) -> None:
        """``INSERT OR REPLACE`` the full row. Only the curator itself
        should call this — every column is overwritten."""
        last_review_ms = _dt_to_ms(state.last_review_at)
        await self._conn.execute(
            """INSERT OR REPLACE INTO curator_state
                 (profile_slug, last_review_at, last_review_duration_ms,
                  last_review_summary, run_count, paused, interval_hours,
                  stale_after_days, archive_after_days, tenant_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                state.profile_slug,
                last_review_ms,
                state.last_review_duration_ms,
                state.last_review_summary,
                int(state.run_count),
                1 if state.paused else 0,
                int(state.interval_hours),
                int(state.stale_after_days),
                int(state.archive_after_days),
                state.tenant_id,
            ),
        )
        await self._conn.commit()

    async def mark_run(
        self,
        profile_slug: str,
        *,
        duration_ms: int,
        summary: str,
        now: datetime | None = None,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> CuratorState:
        """Bump ``run_count`` by one, stamp ``last_review_at`` /
        ``last_review_duration_ms`` / ``last_review_summary``. Preserves
        the operator-tunable thresholds (``interval_hours``,
        ``stale_after_days``, ``archive_after_days``, ``paused``).

        Returns the post-update :class:`CuratorState`."""
        when = now if now is not None else datetime.now(tz=UTC)
        existing = await self.get(profile_slug, tenant_id=tenant_id)
        updated = CuratorState(
            profile_slug=profile_slug,
            last_review_at=when,
            last_review_duration_ms=int(duration_ms),
            last_review_summary=summary,
            run_count=existing.run_count + 1,
            paused=existing.paused,
            interval_hours=existing.interval_hours,
            stale_after_days=existing.stale_after_days,
            archive_after_days=existing.archive_after_days,
            tenant_id=tenant_id,
        )
        await self.upsert(updated)
        return updated

    async def list_all(
        self,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> list[CuratorState]:
        """Every profile's curator state for a tenant. Sorted by
        ``profile_slug`` so the admin UI gets a stable order without
        sorting again client-side."""
        cursor = await self._conn.execute(
            f"SELECT {_CURATOR_COLUMNS} FROM curator_state "
            " WHERE tenant_id = ? "
            " ORDER BY profile_slug ASC",
            (tenant_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [_decode_curator_state(r) for r in rows]


__all__ = [
    "ApplyIntent",
    "CuratorState",
    "CuratorStateRepo",
    "EvolutionGuardConfig",
    "HistoryRepo",
    "IntentLogRepo",
    "MalformedEnumError",
    "MalformedJsonError",
    "NotFoundError",
    "ProposalsRepo",
    "RecursionGuardCooldownError",
    "RecursionGuardViolationError",
    "RepoError",
    "SignalsRepo",
    "SqliteRepoError",
    "iso_week_window",
]
