"""``/admin/evolution*`` — EvolutionLoop proposal queue admin endpoints.

Python port of ``rust/crates/corlinman-gateway/src/routes/admin/evolution.rs``.

Five routes (the Rust source carries seven — the two ``EvolutionApplier``
forward paths, ``/apply`` + ``/rollback``, surface a 501 ``applier_not_wired``
on the Python side until the typed applier lands as part of Wave 2-A):

* ``GET  /admin/evolution``                — list proposals filtered by
  ``?status=pending&limit=50`` (defaults: ``pending``, 50, max 200).
* ``GET  /admin/evolution/budget``         — per-kind weekly quota snapshot
  (the engine + UI both consume the same wire shape).
* ``GET  /admin/evolution/history``        — terminal-state (applied /
  rolled_back) audit rows joined against the proposals table so the
  History tab can render baseline metrics + shadow metrics in one round
  trip.
* ``GET  /admin/evolution/{id}``           — single proposal detail.
* ``POST /admin/evolution/{id}/approve``   — body ``{"decided_by": "..."}``.
  Transitions ``pending|shadow_done → approved``.
* ``POST /admin/evolution/{id}/deny``      — body ``{"decided_by", "reason"}``.
  Transitions ``pending|shadow_done → denied``; deny reason is appended
  to ``reasoning`` with a ``[DENIED: ...]`` prefix.
* ``POST /admin/evolution/{id}/apply``     — 501 stub until the
  Python-side ``EvolutionApplier`` lands. Same response envelope shape
  the Rust route emits when the applier handle is missing
  (``evolution_disabled`` 503 vs. ``applier_not_wired`` 501 mirrors the
  two error funnels the UI keys off).
* ``POST /admin/evolution/{id}/rollback``  — 501 stub for the same
  reason as ``/apply``.

### State machine

Illegal transitions return **409 Conflict** with
``{"error": "invalid_state_transition", "from": "...", "to": "..."}``.

```text
pending ─┐
         ├─► approved ──► applied
shadow_done ─┘   │
                 └─► denied
```

### Disabled mode

When ``AdminState.evolution_store`` is ``None`` every route 503s with
``{"error": "evolution_disabled", ...}`` — same UX as the Rust gate so
the admin UI can render a single subsystem-off banner.

### Meta-approver gate

Phase 4 W2 B1 iter 5: meta kinds (``engine_config`` / ``engine_prompt``
/ ``observer_filter`` / ``cluster_threshold``) require the ``decided_by``
identifier to appear in ``[admin].meta_approver_users``. Non-meta kinds
short-circuit. Empty allow-list (the config default) means **no one**
can approve meta — operators MUST opt in by listing the user explicitly.
Returns 403 ``meta_approver_required`` with ``{user, kind}`` otherwise.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    config_snapshot,
    get_admin_state,
    require_admin,
)

# ---------------------------------------------------------------------------
# Constants (mirror the Rust DEFAULT_LIMIT / MAX_LIMIT)
# ---------------------------------------------------------------------------

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

# Phase 4 W2 B1: meta kinds that need a vetted operator. Mirrors
# ``EvolutionKind::is_meta`` on the typed enum — duplicated here as a
# plain set so the route can stay importable even when
# ``corlinman_evolution_store`` is not installed (defensive: lazy
# imports below preserve the same "503 disabled" UX as the Rust gate).
META_KINDS = frozenset(
    {
        "engine_config",
        "engine_prompt",
        "observer_filter",
        "cluster_threshold",
    }
)

# Statuses from which approve/deny are allowed.
_DECIDABLE_STATUSES = frozenset({"pending", "shadow_done"})


# ---------------------------------------------------------------------------
# Wire shapes (pydantic v2)
# ---------------------------------------------------------------------------


class ProposalOut(BaseModel):
    """Wire-projection of one proposal row. Mirrors the Rust
    ``ProposalOut`` struct field-for-field so existing UI clients
    don't notice the language switch."""

    id: str
    kind: str
    target: str
    diff: str
    reasoning: str
    risk: str
    budget_cost: int
    status: str
    shadow_metrics: Any | None = None
    signal_ids: list[int] = Field(default_factory=list)
    trace_ids: list[str] = Field(default_factory=list)
    created_at: int
    decided_at: int | None = None
    decided_by: str | None = None
    applied_at: int | None = None
    rollback_of: str | None = None
    eval_run_id: str | None = None
    baseline_metrics_json: Any | None = None
    auto_rollback_at: int | None = None
    auto_rollback_reason: str | None = None


class ApproveBody(BaseModel):
    decided_by: str


class DenyBody(BaseModel):
    decided_by: str
    reason: str | None = None


class RollbackBody(BaseModel):
    reason: str | None = None


class DecisionResponse(BaseModel):
    id: str
    status: str


class BudgetKindRow(BaseModel):
    kind: str
    limit: int
    used: int
    remaining: int


class BudgetTotal(BaseModel):
    limit: int
    used: int
    remaining: int


class BudgetSnapshot(BaseModel):
    enabled: bool
    window_start_ms: int
    window_end_ms: int
    weekly_total: BudgetTotal
    per_kind: list[BudgetKindRow]


class HistoryEntryOut(BaseModel):
    proposal_id: str
    kind: str
    target: str
    risk: str
    status: str
    applied_at: int
    rolled_back_at: int | None = None
    rollback_reason: str | None = None
    auto_rollback_reason: str | None = None
    metrics_baseline: Any
    shadow_metrics: Any | None = None
    baseline_metrics_json: Any | None = None
    before_sha: str
    after_sha: str
    eval_run_id: str | None = None
    reasoning: str


# ---------------------------------------------------------------------------
# Error envelopes (mirror the Rust JSON shapes byte-for-byte)
# ---------------------------------------------------------------------------


def _evolution_disabled() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "error": "evolution_disabled",
            "message": "evolution proposal queue is not configured on this gateway",
        },
    )


def _applier_not_wired() -> JSONResponse:
    """The Python-side EvolutionApplier (forward + revert path) has not
    landed yet. Distinguished from ``evolution_disabled`` so the UI can
    tell "store + applier both off" from "store on, applier mid-port"."""
    return JSONResponse(
        status_code=501,
        content={
            "error": "applier_not_wired",
            "message": (
                "evolution applier is not wired on this Python gateway yet; "
                "apply/rollback are read-only stubs until Wave 2-A lands"
            ),
        },
    )


def _invalid_state_transition(from_status: str, to_status: str) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "error": "invalid_state_transition",
            "from": from_status,
            "to": to_status,
        },
    )


def _not_found(id_: str) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={
            "error": "not_found",
            "resource": "evolution_proposal",
            "id": id_,
        },
    )


def _invalid_status(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"error": "invalid_status", "message": message},
    )


def _storage_error(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={"error": "storage_error", "message": message},
    )


def _meta_approver_required(user: str, kind: str) -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content={
            "error": "meta_approver_required",
            "user": user,
            "kind": kind,
        },
    )


# ---------------------------------------------------------------------------
# Helpers — lazy import + adapter
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    """Unix milliseconds. Matches the Rust ``now_ms`` helper."""
    return int(time.time() * 1000)


def _clamp_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    return max(1, min(int(limit), MAX_LIMIT))


def _resolve_connection(store: Any) -> Any:
    """The Python ``EvolutionStore`` exposes its underlying
    ``aiosqlite.Connection`` via the ``conn`` property; older / mock
    stores may use ``connection`` or be a raw connection themselves.
    Accept all three so the routes don't depend on the exact handle
    shape — mirrors the same try-ladder in :mod:`.memory`.
    """
    return getattr(store, "conn", None) or getattr(store, "connection", None) or store


def _project_proposal(p: Any) -> ProposalOut:
    """Map a typed :class:`EvolutionProposal` (from
    :mod:`corlinman_evolution_store`) onto the wire envelope. Defensive
    against missing attributes so the projection survives schema drift
    (extra columns on the source struct are dropped silently)."""
    shadow_metrics = getattr(p, "shadow_metrics", None)
    if shadow_metrics is not None:
        # ShadowMetrics is a dataclass with a single ``data`` dict
        # attribute; emit just the dict on the wire to match the Rust
        # ``serde_json::to_value(MetricsSnapshot)`` projection.
        data = getattr(shadow_metrics, "data", None)
        shadow_metrics = data if data is not None else shadow_metrics

    kind = getattr(p, "kind", "")
    risk = getattr(p, "risk", "")
    status = getattr(p, "status", "")
    rollback_of = getattr(p, "rollback_of", None)

    return ProposalOut(
        id=str(getattr(p, "id", "")),
        kind=kind.as_str() if hasattr(kind, "as_str") else str(kind),
        target=str(getattr(p, "target", "")),
        diff=str(getattr(p, "diff", "")),
        reasoning=str(getattr(p, "reasoning", "")),
        risk=risk.as_str() if hasattr(risk, "as_str") else str(risk),
        budget_cost=int(getattr(p, "budget_cost", 0)),
        status=status.as_str() if hasattr(status, "as_str") else str(status),
        shadow_metrics=shadow_metrics,
        signal_ids=list(getattr(p, "signal_ids", []) or []),
        trace_ids=list(getattr(p, "trace_ids", []) or []),
        created_at=int(getattr(p, "created_at", 0)),
        decided_at=getattr(p, "decided_at", None),
        decided_by=getattr(p, "decided_by", None),
        applied_at=getattr(p, "applied_at", None),
        rollback_of=str(rollback_of) if rollback_of else None,
        eval_run_id=getattr(p, "eval_run_id", None),
        baseline_metrics_json=getattr(p, "baseline_metrics_json", None),
        auto_rollback_at=getattr(p, "auto_rollback_at", None),
        auto_rollback_reason=getattr(p, "auto_rollback_reason", None),
    )


def _assert_meta_approver(
    state: AdminState, kind_str: str, decided_by: str
) -> JSONResponse | None:
    """Phase 4 W2 B1 iter 5 gate. Returns ``None`` when the call is
    allowed, otherwise the 403 envelope to short-circuit with."""
    if kind_str not in META_KINDS:
        return None
    cfg = config_snapshot(state)
    admin_cfg = cfg.get("admin") if isinstance(cfg, dict) else None
    allow_list: list[str] = []
    if isinstance(admin_cfg, dict):
        raw = admin_cfg.get("meta_approver_users") or []
        if isinstance(raw, list):
            allow_list = [str(u) for u in raw]
    if decided_by in allow_list:
        return None
    return _meta_approver_required(decided_by, kind_str)


def _decidable(status_str: str) -> bool:
    return status_str in _DECIDABLE_STATUSES


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:  # noqa: C901 — single APIRouter factory, mirrors Rust pattern
    r = APIRouter(dependencies=[Depends(require_admin)], tags=["admin", "evolution"])

    # `/admin/evolution/budget` and `/admin/evolution/history` are
    # registered before `/admin/evolution/{id}` so the literal paths
    # win the FastAPI router match (otherwise the path-param would
    # capture "budget" / "history" and try to look up a proposal of
    # that id). FastAPI uses first-registration-wins on overlapping
    # path templates, same convention as Rust's axum router.

    @r.get("/admin/evolution", response_model=list[ProposalOut])
    async def list_proposals(
        status: str = Query("pending"),
        limit: int | None = Query(None),
    ):
        state = get_admin_state()
        store = state.evolution_store
        if store is None:
            return _evolution_disabled()

        try:
            from corlinman_evolution_store import (  # noqa: PLC0415
                EvolutionStatus,
                ProposalsRepo,
            )
        except ImportError:
            return _evolution_disabled()

        try:
            status_enum = EvolutionStatus.from_str(status)
        except Exception as exc:  # noqa: BLE001 — typed ParseError mapped to 400
            return _invalid_status(str(exc))

        n = _clamp_limit(limit)
        repo = ProposalsRepo(_resolve_connection(store))
        try:
            rows = await repo.list_by_status(status_enum, n)
        except Exception as exc:  # noqa: BLE001
            return _storage_error(str(exc))
        return [_project_proposal(p).model_dump() for p in rows]

    @r.get("/admin/evolution/budget", response_model=BudgetSnapshot)
    async def budget():
        state = get_admin_state()
        store = state.evolution_store
        if store is None:
            return _evolution_disabled()

        try:
            from corlinman_evolution_store import (  # noqa: PLC0415
                EvolutionKind,
                ProposalsRepo,
                iso_week_window,
            )
        except ImportError:
            return _evolution_disabled()

        cfg = config_snapshot(state)
        evo_cfg = cfg.get("evolution") if isinstance(cfg, dict) else None
        budget_cfg = (evo_cfg or {}).get("budget") or {}
        enabled = bool(budget_cfg.get("enabled", False))
        weekly_total_limit = int(budget_cfg.get("weekly_total", 0))
        per_kind_cfg: dict[str, int] = {}
        raw_per_kind = budget_cfg.get("per_kind")
        if isinstance(raw_per_kind, dict):
            for k, v in raw_per_kind.items():
                try:
                    per_kind_cfg[str(k)] = int(v)
                except (TypeError, ValueError):
                    continue

        now = _now_ms()
        window_start_ms, window_end_ms = iso_week_window(now)

        repo = ProposalsRepo(_resolve_connection(store))
        try:
            weekly_used = await repo.count_proposals_in_iso_week(now, None)
        except Exception as exc:  # noqa: BLE001
            return _storage_error(str(exc))

        rows: list[BudgetKindRow] = []
        for kind_str, limit in per_kind_cfg.items():
            if limit == 0:
                # Explicit zero cap means "block this kind entirely" —
                # the engine handles that without surfacing a row in the
                # snapshot. Mirrors the Rust filter.
                continue
            try:
                kind_enum = EvolutionKind.from_str(kind_str)
            except Exception:  # noqa: BLE001 — unknown kind in config: skip + carry on
                continue
            try:
                used = await repo.count_proposals_in_iso_week(now, kind_enum)
            except Exception as exc:  # noqa: BLE001
                return _storage_error(str(exc))
            rows.append(
                BudgetKindRow(
                    kind=kind_str,
                    limit=limit,
                    used=int(used),
                    remaining=max(limit - int(used), 0),
                )
            )
        rows.sort(key=lambda row: row.kind)

        snap = BudgetSnapshot(
            enabled=enabled,
            window_start_ms=window_start_ms,
            window_end_ms=window_end_ms,
            weekly_total=BudgetTotal(
                limit=weekly_total_limit,
                used=int(weekly_used),
                remaining=max(weekly_total_limit - int(weekly_used), 0),
            ),
            per_kind=rows,
        )
        return snap

    @r.get("/admin/evolution/history", response_model=list[HistoryEntryOut])
    async def history(limit: int | None = Query(None)):
        state = get_admin_state()
        store = state.evolution_store
        if store is None:
            return _evolution_disabled()

        n = _clamp_limit(limit)
        conn = _resolve_connection(store)

        sql = (
            "SELECT h.proposal_id, p.kind, p.target, p.risk, p.status, "
            "       h.applied_at, h.rolled_back_at, h.rollback_reason, "
            "       p.auto_rollback_reason, h.metrics_baseline, "
            "       p.shadow_metrics, p.baseline_metrics_json, "
            "       h.before_sha, h.after_sha, p.eval_run_id, p.reasoning "
            "  FROM evolution_history h "
            "  JOIN evolution_proposals p ON p.id = h.proposal_id "
            " ORDER BY h.applied_at DESC "
            " LIMIT ?"
        )
        try:
            cursor = await conn.execute(sql, (n,))
            try:
                rows = await cursor.fetchall()
            finally:
                await cursor.close()
        except Exception as exc:  # noqa: BLE001
            return _storage_error(str(exc))

        import json as _json  # noqa: PLC0415 — local import keeps top-level lean

        out: list[HistoryEntryOut] = []
        for row in rows:
            # Row order matches the SELECT column order above.
            try:
                metrics_baseline_str = row[9]
                metrics_baseline = (
                    _json.loads(metrics_baseline_str)
                    if isinstance(metrics_baseline_str, str)
                    else metrics_baseline_str
                )
            except Exception as exc:  # noqa: BLE001 — malformed JSON is a 500
                return _storage_error(f"metrics_baseline: {exc}")

            def _opt_json(val: Any) -> Any | None:
                if val is None:
                    return None
                if not isinstance(val, str):
                    return val
                try:
                    return _json.loads(val)
                except Exception:  # noqa: BLE001 — best-effort, return None on bad JSON
                    return None

            out.append(
                HistoryEntryOut(
                    proposal_id=str(row[0]),
                    kind=str(row[1]),
                    target=str(row[2]),
                    risk=str(row[3]),
                    status=str(row[4]),
                    applied_at=int(row[5]),
                    rolled_back_at=row[6],
                    rollback_reason=row[7],
                    auto_rollback_reason=row[8],
                    metrics_baseline=metrics_baseline,
                    shadow_metrics=_opt_json(row[10]),
                    baseline_metrics_json=_opt_json(row[11]),
                    before_sha=str(row[12]),
                    after_sha=str(row[13]),
                    eval_run_id=row[14],
                    reasoning=str(row[15] or ""),
                )
            )
        return [e.model_dump() for e in out]

    @r.get("/admin/evolution/{id}", response_model=ProposalOut)
    async def get_proposal(id: str):
        state = get_admin_state()
        store = state.evolution_store
        if store is None:
            return _evolution_disabled()

        try:
            from corlinman_evolution_store import (  # noqa: PLC0415
                NotFoundError,
                ProposalId,
                ProposalsRepo,
            )
        except ImportError:
            return _evolution_disabled()

        repo = ProposalsRepo(_resolve_connection(store))
        try:
            proposal = await repo.get(ProposalId(id))
        except NotFoundError:
            return _not_found(id)
        except Exception as exc:  # noqa: BLE001
            return _storage_error(str(exc))
        return _project_proposal(proposal).model_dump()

    @r.post("/admin/evolution/{id}/approve", response_model=DecisionResponse)
    async def approve_proposal(id: str, body: ApproveBody):
        state = get_admin_state()
        store = state.evolution_store
        if store is None:
            return _evolution_disabled()

        try:
            from corlinman_evolution_store import (  # noqa: PLC0415
                EvolutionStatus,
                NotFoundError,
                ProposalId,
                ProposalsRepo,
            )
        except ImportError:
            return _evolution_disabled()

        repo = ProposalsRepo(_resolve_connection(store))
        try:
            current = await repo.get(ProposalId(id))
        except NotFoundError:
            return _not_found(id)
        except Exception as exc:  # noqa: BLE001
            return _storage_error(str(exc))

        current_status = current.status
        current_status_str = (
            current_status.as_str()
            if hasattr(current_status, "as_str")
            else str(current_status)
        )
        if not _decidable(current_status_str):
            return _invalid_state_transition(current_status_str, "approved")

        kind = current.kind
        kind_str = kind.as_str() if hasattr(kind, "as_str") else str(kind)
        meta_resp = _assert_meta_approver(state, kind_str, body.decided_by)
        if meta_resp is not None:
            return meta_resp

        try:
            await repo.set_decision(
                ProposalId(id),
                EvolutionStatus.APPROVED,
                _now_ms(),
                body.decided_by,
            )
        except NotFoundError:
            return _not_found(id)
        except Exception as exc:  # noqa: BLE001
            return _storage_error(str(exc))
        return DecisionResponse(id=id, status="approved")

    @r.post("/admin/evolution/{id}/deny", response_model=DecisionResponse)
    async def deny_proposal(id: str, body: DenyBody):
        state = get_admin_state()
        store = state.evolution_store
        if store is None:
            return _evolution_disabled()

        try:
            from corlinman_evolution_store import (  # noqa: PLC0415
                EvolutionStatus,
                NotFoundError,
                ProposalId,
                ProposalsRepo,
            )
        except ImportError:
            return _evolution_disabled()

        conn = _resolve_connection(store)
        repo = ProposalsRepo(conn)
        try:
            current = await repo.get(ProposalId(id))
        except NotFoundError:
            return _not_found(id)
        except Exception as exc:  # noqa: BLE001
            return _storage_error(str(exc))

        current_status = current.status
        current_status_str = (
            current_status.as_str()
            if hasattr(current_status, "as_str")
            else str(current_status)
        )
        if not _decidable(current_status_str):
            return _invalid_state_transition(current_status_str, "denied")

        # Mirror the Rust deny path: preserve the operator-supplied
        # reason inside ``reasoning`` with a fixed ``[DENIED: ...]``
        # prefix so the History tab surfaces it without a new column.
        reason = (body.reason or "").strip()
        if reason:
            current_reasoning = getattr(current, "reasoning", "") or ""
            updated = (
                f"[DENIED: {reason}]"
                if not current_reasoning
                else f"{current_reasoning}\n[DENIED: {reason}]"
            )
            try:
                cursor = await conn.execute(
                    "UPDATE evolution_proposals SET reasoning = ? WHERE id = ?",
                    (updated, id),
                )
                affected = cursor.rowcount
                await cursor.close()
                await conn.commit()
            except Exception as exc:  # noqa: BLE001
                return _storage_error(str(exc))
            if affected == 0:
                return _not_found(id)

        try:
            await repo.set_decision(
                ProposalId(id),
                EvolutionStatus.DENIED,
                _now_ms(),
                body.decided_by,
            )
        except NotFoundError:
            return _not_found(id)
        except Exception as exc:  # noqa: BLE001
            return _storage_error(str(exc))
        return DecisionResponse(id=id, status="denied")

    @r.post("/admin/evolution/{id}/apply")
    async def apply_proposal(id: str):  # noqa: ARG001 — id mirrors Rust route signature
        state = get_admin_state()
        if state.evolution_store is None:
            return _evolution_disabled()
        # TODO(wave-2a-python): wire `corlinman_evolution_engine.EvolutionApplier`
        # once it lands on the Python side. The Rust path issues
        # `applier.apply(&pid)` and maps `ApplyError` variants onto
        # 4xx/5xx envelopes (unsupported_kind / invalid_target / drift
        # mismatch / meta_approver_required / etc). Until then this is
        # a 501 stub so the UI's banner ("apply not wired") differs
        # from the global subsystem-off case.
        return _applier_not_wired()

    @r.post("/admin/evolution/{id}/rollback")
    async def rollback_proposal(
        id: str,  # noqa: ARG001 — id mirrors Rust route signature
        body: RollbackBody | None = None,  # noqa: ARG001 — kept for wire parity
    ):
        state = get_admin_state()
        if state.evolution_store is None:
            return _evolution_disabled()
        # TODO(wave-2a-python): wire `EvolutionApplier.revert` once the
        # Python applier lands. Same envelope plan as `/apply` above.
        return _applier_not_wired()

    return r
