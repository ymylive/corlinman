"""``/admin/memory/*`` — operator escape hatches for the memory pipeline.

Port of ``rust/crates/corlinman-gateway/src/routes/admin/memory.rs``.

Today this is exactly one route: ``POST /admin/memory/decay/reset`` —
force a chunk's ``decay_score`` back to 1.0 + write a synthetic
``evolution_history`` audit pair (proposal + history rows) tagged
``decided_by = "admin:manual"``.

Backed by:

* ``corlinman_embedding.vector.SqliteStore`` (a.k.a. the RAG store) on
  :attr:`AdminState.rag_store` — exposes ``reset_chunk_decay`` /
  ``get_chunk_decay_state``.
* ``corlinman_evolution_store`` repos on :attr:`AdminState.evolution_store`
  for ``ProposalsRepo`` + ``HistoryRepo``.

503 ``memory_admin_disabled`` when either handle is missing.
"""

from __future__ import annotations

import time
import uuid

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from corlinman_server.gateway.routes_admin_b.state import (
    get_admin_state,
    require_admin,
)


class ResetRequest(BaseModel):
    chunk_id: int = Field(..., description="kb chunk id whose decay_score to reset")
    reason: str = Field(default="", description="operator-supplied rationale")


class ResetResponse(BaseModel):
    chunk_id: int
    history_id: int
    proposal_id: str
    applied_at: int


def _disabled(detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"error": "memory_admin_disabled", "detail": detail},
    )


def router() -> APIRouter:
    r = APIRouter(dependencies=[Depends(require_admin)], tags=["admin", "memory"])

    @r.post("/admin/memory/decay/reset", response_model=ResetResponse)
    async def reset_decay(req: ResetRequest):
        state = get_admin_state()
        kb = state.rag_store
        evo = state.evolution_store
        if kb is None:
            return _disabled("kb store not configured")
        if evo is None:
            return _disabled("evolution store not configured")

        # Step 1 — forward correction on the kb. 0 affected rows means
        # the chunk doesn't exist.
        try:
            affected = await kb.reset_chunk_decay(req.chunk_id)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                status_code=500,
                content={"error": "kb_update_failed", "detail": str(exc)},
            )
        if affected == 0:
            return JSONResponse(
                status_code=404,
                content={"error": "chunk_not_found", "chunk_id": req.chunk_id},
            )

        # Step 2 — synthetic proposal row (FK target for history).
        now_ms = int(time.time() * 1000)
        proposal_id = f"manual-{uuid.uuid4()}"
        target = f"decay_reset:{req.chunk_id}"
        reason = req.reason.strip()
        reasoning = f"manual decay reset: {reason}" if reason else "manual decay reset"

        # Resolve repos lazily via the evolution store contract.
        try:
            from corlinman_evolution_store import (  # noqa: PLC0415
                EvolutionHistory,
                EvolutionKind,
                EvolutionProposal,
                EvolutionRisk,
                EvolutionStatus,
                HistoryRepo,
                ProposalId,
                ProposalsRepo,
            )
        except ImportError as exc:  # pragma: no cover — defensive
            return JSONResponse(
                status_code=503,
                content={"error": "memory_admin_disabled", "detail": str(exc)},
            )

        # Some EvolutionStore implementations expose ``pool``/``connection``
        # accessors; we accept either.
        pool = getattr(evo, "pool", None) or getattr(evo, "connection", None) or evo
        proposals_repo = ProposalsRepo(pool)
        history_repo = HistoryRepo(pool)

        proposal = EvolutionProposal(
            id=ProposalId(proposal_id),
            kind=EvolutionKind.MEMORY_OP,
            target=target,
            diff="",
            reasoning=reasoning,
            risk=EvolutionRisk.LOW,
            budget_cost=0,
            status=EvolutionStatus.APPLIED,
            shadow_metrics=None,
            signal_ids=[],
            trace_ids=[],
            created_at=now_ms,
            decided_at=now_ms,
            decided_by="admin:manual",
            applied_at=now_ms,
            rollback_of=None,
            eval_run_id=None,
            baseline_metrics_json=None,
            auto_rollback_at=None,
            auto_rollback_reason=None,
            metadata=None,
        )

        try:
            await proposals_repo.insert(proposal)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                status_code=500,
                content={
                    "error": "audit_write_failed",
                    "detail": str(exc),
                    "warning": "kb decay reset succeeded but audit row was not written",
                },
            )

        history = EvolutionHistory(
            id=None,
            proposal_id=ProposalId(proposal_id),
            kind=EvolutionKind.MEMORY_OP,
            target=target,
            before_sha="",
            after_sha="",
            inverse_diff="",
            metrics_baseline=None,
            applied_at=now_ms,
            rolled_back_at=None,
            rollback_reason=None,
            share_with=None,
        )

        try:
            history_id = await history_repo.insert(history)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                status_code=500,
                content={
                    "error": "audit_write_failed",
                    "detail": str(exc),
                    "warning": "kb decay reset succeeded but history row was not written",
                },
            )

        return ResetResponse(
            chunk_id=req.chunk_id,
            history_id=int(history_id),
            proposal_id=proposal_id,
            applied_at=now_ms,
        )

    return r
