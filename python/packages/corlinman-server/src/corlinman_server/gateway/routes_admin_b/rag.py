"""``/admin/rag*`` — RAG corpus stats + debug query + FTS rebuild.

Port of ``rust/crates/corlinman-gateway/src/routes/admin/rag.rs``. Three
routes:

* ``GET  /admin/rag/stats``       — file / chunk / tag counts.
* ``GET  /admin/rag/query?q=&k=`` — BM25 debug search.
* ``POST /admin/rag/rebuild``     — rebuild the ``chunks_fts`` FTS5
  virtual table.

Backed by ``corlinman_embedding.vector.SqliteStore`` (a.k.a. the local
RAG corpus). 503 ``rag_disabled`` when no store is attached.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from corlinman_server.gateway.routes_admin_b.state import (
    get_admin_state,
    require_admin,
)

PREVIEW_LEN = 240


class StatsOut(BaseModel):
    ready: bool
    files: int
    chunks: int
    tags: int


class QueryHit(BaseModel):
    chunk_id: int
    score: float
    content_preview: str


class QueryResponse(BaseModel):
    backend: str
    q: str
    k: int
    hits: list[QueryHit]


def _disabled() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "error": "rag_disabled",
            "message": "RAG store is not attached to this gateway",
        },
    )


def _truncate(text: str) -> str:
    if len(text) <= PREVIEW_LEN:
        return text
    return text[:PREVIEW_LEN] + "…"


async def _maybe_await(value: Any) -> Any:
    """Accept either sync or async returns from the SqliteStore."""
    import inspect  # noqa: PLC0415

    if inspect.isawaitable(value):
        return await value
    return value


def router() -> APIRouter:
    r = APIRouter(dependencies=[Depends(require_admin)], tags=["admin", "rag"])

    @r.get("/admin/rag/stats", response_model=StatsOut)
    async def stats():
        state = get_admin_state()
        store = state.rag_store
        if store is None:
            return _disabled()
        try:
            files = await _maybe_await(store.count_files())
            chunks = await _maybe_await(store.count_chunks())
            tags = await _maybe_await(store.count_tags())
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                status_code=500,
                content={"error": "storage_error", "message": str(exc)},
            )
        return StatsOut(ready=True, files=int(files), chunks=int(chunks), tags=int(tags))

    @r.get("/admin/rag/query", response_model=QueryResponse)
    async def query(q: str = Query(""), k: int = Query(10)):
        state = get_admin_state()
        store = state.rag_store
        if store is None:
            return _disabled()
        if not q.strip():
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_query", "message": "q must be non-empty"},
            )
        k_clamped = max(1, min(k, 100))
        try:
            raw = await _maybe_await(store.search_bm25(q, k_clamped))
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                status_code=500,
                content={"error": "storage_error", "message": str(exc)},
            )
        if not raw:
            return QueryResponse(backend="bm25", q=q, k=k_clamped, hits=[])
        ids = [int(item[0]) for item in raw]
        try:
            chunks = await _maybe_await(store.query_chunks_by_ids(ids))
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                status_code=500,
                content={"error": "storage_error", "message": str(exc)},
            )
        by_id = {}
        for c in chunks:
            cid = getattr(c, "id", None) or (c.get("id") if isinstance(c, dict) else None)
            content = (
                getattr(c, "content", None)
                or (c.get("content") if isinstance(c, dict) else None)
                or ""
            )
            if cid is not None:
                by_id[int(cid)] = content
        hits = [
            QueryHit(
                chunk_id=int(cid),
                score=float(score),
                content_preview=_truncate(by_id.get(int(cid), "")),
            )
            for cid, score in raw
        ]
        return QueryResponse(backend="bm25", q=q, k=k_clamped, hits=hits)

    @r.post("/admin/rag/rebuild")
    async def rebuild():
        state = get_admin_state()
        store = state.rag_store
        if store is None:
            return _disabled()
        try:
            await _maybe_await(store.rebuild_fts())
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                status_code=500,
                content={"error": "rebuild_failed", "message": str(exc)},
            )
        return {"status": "ok", "target": "chunks_fts"}

    return r
