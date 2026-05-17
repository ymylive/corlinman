"""``/admin/approvals*`` — tool-approval queue admin endpoints.

Python port of ``rust/crates/corlinman-gateway/src/routes/admin/approvals.rs``.

Three routes (all behind :func:`require_admin_dependency`):

* ``GET  /admin/approvals?include_decided=false`` — JSON list backed by
  :class:`corlinman_providers.plugins.ApprovalStore`.
* ``POST /admin/approvals/{call_id}/decide`` — record an approve / deny
  decision and wake any in-process waiter via
  :class:`~corlinman_providers.plugins.ApprovalQueue`.
* ``GET  /admin/approvals/stream`` — Server-Sent Events feed of fresh
  ``pending`` / ``decided`` rows. Uses Starlette's
  :class:`fastapi.responses.StreamingResponse` because the Python
  ``ApprovalQueue`` doesn't ship a broadcast bus — we poll the store
  every ``poll_interval`` seconds and emit deltas.

When ``state.approval_store`` is ``None`` every route returns
**503 ``approvals_disabled``**, mirroring the Rust gate.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from corlinman_server.gateway.routes_admin_a._auth_shim import (
    require_admin_dependency,
)
from corlinman_server.gateway.routes_admin_a.state import (
    AdminState,
    get_admin_state,
)


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class ApprovalOut(BaseModel):
    """Flat JSON shape returned to the UI. Mirrors the Rust
    ``ApprovalOut`` envelope projected onto the Python
    :class:`~corlinman_providers.plugins.ApprovalRecord` shape.

    Field naming follows the Python side's ``ApprovalRecord``
    (``call_id``, ``args_preview``, ``created_at``) — the Rust side's
    flat ``SqliteStore::PendingApproval`` shape carried different
    column names (``id``, ``args_json``, ``requested_at``). UI clients
    of the Python plane should consume this Python-native shape.
    """

    call_id: str
    plugin: str
    tool: str
    session_key: str
    args_preview: str
    reason: str
    created_at: float
    decision: str | None = None
    decided_at: float | None = None


class DecideBody(BaseModel):
    """``POST /admin/approvals/{call_id}/decide`` body.

    ``approve = True`` maps to :class:`ApprovalDecision.ALLOW`;
    ``approve = False`` to :class:`ApprovalDecision.DENY`. The
    optional ``reason`` is reserved for the audit log (the Python
    ``ApprovalStore`` doesn't persist it today; we accept it on the
    wire for parity with the Rust contract)."""

    approve: bool
    reason: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _approvals_disabled() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "error": "approvals_disabled",
            "message": "approval gate is not configured on this gateway",
        },
    )


def _require_store(state: AdminState) -> Any:
    """Return ``state.approval_store`` or raise the 503 envelope."""
    store = state.approval_store
    if store is None:
        raise _approvals_disabled()
    return store


def _record_to_out(record: Any) -> ApprovalOut:
    """Convert a :class:`ApprovalRecord` to the wire envelope."""
    decision = getattr(record, "decision", None)
    decided_at = getattr(record, "decided_at", None)
    return ApprovalOut(
        call_id=record.call_id,
        plugin=record.plugin,
        tool=record.tool,
        session_key=record.session_key,
        args_preview=record.args_preview,
        reason=record.reason,
        created_at=float(record.created_at),
        decision=(decision.value if decision is not None else None),
        decided_at=(float(decided_at) if decided_at is not None else None),
    )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    """Sub-router for ``/admin/approvals*``."""
    r = APIRouter(dependencies=[Depends(require_admin_dependency)])

    @r.get(
        "/admin/approvals",
        response_model=list[ApprovalOut],
        summary="List pending (and optionally decided) approvals",
    )
    async def list_approvals(
        state: Annotated[AdminState, Depends(get_admin_state)],
        include_decided: Annotated[bool, Query()] = False,
    ) -> list[ApprovalOut]:
        store = _require_store(state)
        try:
            if include_decided:
                # The Python store doesn't ship a single "list everything"
                # method — fall back to two queries. The pending list is
                # the operator's primary view; the decided trickle is
                # informational so we tolerate the second round-trip.
                pending = await store.pending()
                rows = list(pending)
                # ``ApprovalStore`` doesn't expose a list-all helper
                # publicly; opportunistically use the underlying
                # connection when present so the operator sees both.
                rows.extend(await _list_decided(store))
            else:
                rows = await store.pending()
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error": "storage_error", "message": str(exc)},
            ) from exc
        return [_record_to_out(rec) for rec in rows]

    @r.post(
        "/admin/approvals/{call_id}/decide",
        summary="Approve or deny a pending tool call",
    )
    async def decide_approval(
        call_id: str,
        body: DecideBody,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> dict[str, str]:
        store = _require_store(state)
        # Resolve the ApprovalDecision enum lazily so a missing
        # ``corlinman_providers`` install doesn't break imports.
        try:
            from corlinman_providers.plugins import ApprovalDecision
        except ImportError as exc:  # pragma: no cover — providers always installed
            raise _approvals_disabled() from exc

        decision = (
            ApprovalDecision.ALLOW if body.approve else ApprovalDecision.DENY
        )

        # Prefer the queue (wakes in-process waiters) when wired; fall
        # back to the store directly otherwise.
        target = state.approval_queue or store
        try:
            updated = await target.decide(call_id, decision)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error": "decide_failed", "message": str(exc)},
            ) from exc
        if not updated:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "not_found",
                    "resource": "approval",
                    "id": call_id,
                },
            )
        return {"id": call_id, "decision": decision.value}

    @r.get(
        "/admin/approvals/stream",
        summary="SSE stream of pending / decided approval events",
    )
    async def stream_approvals(
        request: Request,
        state: Annotated[AdminState, Depends(get_admin_state)],
        poll_interval: Annotated[float, Query(ge=0.05, le=10.0)] = 0.5,
    ) -> StreamingResponse:
        store = _require_store(state)
        return StreamingResponse(
            _sse_iter(store, request, poll_interval=poll_interval),
            media_type="text/event-stream",
        )

    return r


async def _list_decided(store: Any) -> list[Any]:
    """Best-effort list of decided rows. Returns an empty list when the
    store doesn't expose a compatible query path (so the
    ``include_decided=true`` query degrades gracefully rather than
    raising)."""
    # The Python ApprovalStore only exposes ``pending()`` publicly;
    # reach into its connection helper to fetch decided rows. We use
    # the documented private ``_conn`` async context manager rather
    # than re-implement the SQL.
    if not hasattr(store, "_conn"):
        return []
    rows: list[Any] = []
    try:
        async with store._conn() as conn:  # type: ignore[attr-defined]
            cur = await conn.execute(
                "SELECT * FROM pending_approvals "
                "WHERE decision IS NOT NULL "
                "ORDER BY decided_at DESC"
            )
            sql_rows = await cur.fetchall()
        for r in sql_rows:
            rows.append(_row_to_record(r))
    except Exception:  # pragma: no cover — informational only
        return []
    return rows


def _row_to_record(row: Any) -> Any:
    """Mirror the private ``_row_to_record`` helper inside the
    Python ApprovalStore so the include-decided query renders the
    same ``ApprovalRecord`` shape the pending() call returns."""
    from corlinman_providers.plugins import ApprovalDecision, ApprovalRecord

    decision_raw = row["decision"]
    return ApprovalRecord(
        call_id=row["call_id"],
        plugin=row["plugin"],
        tool=row["tool"],
        args_preview=row["args_preview"],
        session_key=row["session_key"],
        reason=row["reason"],
        created_at=float(row["created_at"]),
        decision=ApprovalDecision(decision_raw) if decision_raw else None,
        decided_at=float(row["decided_at"]) if row["decided_at"] is not None else None,
    )


async def _sse_iter(
    store: Any, request: Request, *, poll_interval: float
) -> AsyncIterator[bytes]:
    """Poll-based SSE feed.

    The Rust side uses a broadcast bus on the ``ApprovalGate``; the
    Python ApprovalQueue doesn't expose one yet. We poll the store
    every ``poll_interval`` seconds and emit two event kinds matching
    the Rust ``ApprovalEvent::{Pending,Decided}`` enum:

    * ``data: {"kind": "pending", "approval": {...}}\\n\\n`` — when a
      row appears in ``pending()``.
    * ``data: {"kind": "decided", "id": ..., "decision": ...}\\n\\n``
      — when a previously pending row gains a decision.

    Drops out cleanly when the client disconnects.
    """
    seen_pending: set[str] = set()
    seen_decided: set[str] = set()

    # Seed: emit the current pending backlog so a fresh subscriber
    # doesn't miss the queue. ``await store.pending()`` is cheap.
    backlog = await store.pending()
    for rec in backlog:
        seen_pending.add(rec.call_id)
        payload = {"kind": "pending", "approval": _record_to_out(rec).model_dump()}
        yield _sse_frame(payload)

    while True:
        if await request.is_disconnected():
            return
        try:
            await asyncio.sleep(poll_interval)
        except asyncio.CancelledError:
            return
        try:
            pending = await store.pending()
        except Exception as exc:  # surface as an ``lag`` frame and bail
            yield _sse_frame({"kind": "lag", "error": str(exc)}, event="lag")
            return

        current_ids = {rec.call_id for rec in pending}
        # New pending rows.
        for rec in pending:
            if rec.call_id not in seen_pending:
                seen_pending.add(rec.call_id)
                payload = {
                    "kind": "pending",
                    "approval": _record_to_out(rec).model_dump(),
                }
                yield _sse_frame(payload)

        # Rows that *were* pending and no longer are = newly decided.
        newly_decided = seen_pending - current_ids - seen_decided
        for call_id in newly_decided:
            seen_decided.add(call_id)
            try:
                record = await store.get(call_id)
            except Exception:
                record = None
            decision = (
                getattr(getattr(record, "decision", None), "value", None)
                if record is not None
                else None
            )
            payload = {
                "kind": "decided",
                "id": call_id,
                "decision": decision,
            }
            yield _sse_frame(payload)


def _sse_frame(payload: dict[str, Any], *, event: str | None = None) -> bytes:
    """Encode a single ``data:`` line (optionally with an ``event:``
    label). Mirrors the Rust ``SseEvent::default().data(...)`` shape."""
    lines: list[str] = []
    if event is not None:
        lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(payload, separators=(',', ':'))}")
    lines.append("")  # terminating blank line — required by the SSE spec
    return ("\n".join(lines) + "\n").encode("utf-8")


__all__ = [
    "ApprovalOut",
    "DecideBody",
    "router",
]
