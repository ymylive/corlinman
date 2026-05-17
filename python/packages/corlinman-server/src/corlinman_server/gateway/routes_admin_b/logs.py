"""``/admin/logs/stream`` — Server-Sent Events feed.

Port of ``rust/crates/corlinman-gateway/src/routes/admin/logs.rs``.

Each connection subscribes afresh to the gateway's log broadcaster (lazy
import of ``corlinman_server.gateway.core.log_broadcast``) and yields
each ``LogRecord`` as an ``event: log`` SSE frame. Lagging subscribers
get an ``event: lag`` heartbeat rather than a torn-down stream.

Filters: ``level=debug|info|warn|error`` (>= threshold),
``subsystem=<str>`` substring, ``trace_id=<str>`` exact.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    get_admin_state,
    require_admin,
)

# Keep-alive interval — match Rust default of 15s.
KEEPALIVE_INTERVAL = 15.0


class LogStreamQuery(BaseModel):
    level: str | None = None
    subsystem: str | None = None
    trace_id: str | None = None


_LEVEL_RANK = {
    "trace": 0,
    "debug": 1,
    "info": 2,
    "warn": 3,
    "warning": 3,
    "error": 4,
    "err": 4,
}


def _level_rank(level: str | None) -> int | None:
    if level is None:
        return None
    return _LEVEL_RANK.get(level.lower())


def matches(query: LogStreamQuery, record: dict[str, Any]) -> bool:
    """Apply the filter to ``record``. Public so tests can exercise it."""
    if query.level:
        min_rank = _level_rank(query.level)
        if min_rank is None:
            return True
        actual = _level_rank(record.get("level", ""))
        if actual is None or actual < min_rank:
            return False
    if query.subsystem:
        sub = record.get("subsystem")
        if not isinstance(sub, str) or query.subsystem not in sub:
            return False
    if query.trace_id:
        tid = record.get("trace_id")
        if tid != query.trace_id:
            return False
    return True


def _logs_disabled() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "error": "logs_disabled",
            "message": "log broadcast layer not installed on this gateway",
        },
    )


def _resolve_broadcaster(state: AdminState) -> Any | None:
    if state.log_broadcast is not None:
        return state.log_broadcast
    # Lazy import — sibling parallel agent provides this module.
    try:
        from corlinman_server.gateway.core import log_broadcast as _lb  # type: ignore  # noqa: PLC0415
    except ImportError:
        return None
    return getattr(_lb, "DEFAULT_BROADCASTER", None)


async def _sse_stream(broadcaster: Any, query: LogStreamQuery):
    """Async generator yielding SSE bytes from ``broadcaster.subscribe()``.

    The contract for ``broadcaster`` is intentionally loose so the
    parallel ``log_broadcast`` agent can ship whichever async-iterator
    shape lands first. We require:

    * ``broadcaster.subscribe() -> AsyncIterator[dict | tuple[str, Any]]``
      The iterator yields either a record dict or a sentinel
      ``("lag", count)`` tuple. The latter is reflected on the wire as
      an ``event: lag`` frame.
    """
    sub = broadcaster.subscribe()
    last_emit = asyncio.get_running_loop().time()
    try:
        while True:
            try:
                next_item_co = sub.__anext__() if hasattr(sub, "__anext__") else sub.recv()
                item = await asyncio.wait_for(next_item_co, timeout=KEEPALIVE_INTERVAL)
            except asyncio.TimeoutError:
                # Keep-alive heartbeat — SSE comment line.
                yield b": keep-alive\n\n"
                last_emit = asyncio.get_running_loop().time()
                continue
            except (StopAsyncIteration, StopIteration):
                break
            if isinstance(item, tuple) and len(item) == 2 and item[0] == "lag":
                payload = json.dumps({"skipped": item[1]})
                yield f"event: lag\ndata: {payload}\n\n".encode()
                last_emit = asyncio.get_running_loop().time()
                continue
            if not isinstance(item, dict):
                continue
            if not matches(query, item):
                continue
            payload = json.dumps(item, default=str)
            yield f"event: log\ndata: {payload}\n\n".encode()
            last_emit = asyncio.get_running_loop().time()
            _ = last_emit  # silence "assigned but unused"
    finally:
        close = getattr(sub, "aclose", None) or getattr(sub, "close", None)
        if close is not None:
            try:
                res = close()
                if asyncio.iscoroutine(res):
                    await res
            except Exception:  # noqa: BLE001
                pass


def router() -> APIRouter:
    r = APIRouter(dependencies=[Depends(require_admin)], tags=["admin", "logs"])

    @r.get("/admin/logs/stream")
    async def stream_logs(  # noqa: D401 — handler
        level: str | None = Query(None),
        subsystem: str | None = Query(None),
        trace_id: str | None = Query(None),
    ):
        state = get_admin_state()
        broadcaster = _resolve_broadcaster(state)
        if broadcaster is None:
            return _logs_disabled()
        q = LogStreamQuery(level=level, subsystem=subsystem, trace_id=trace_id)
        return StreamingResponse(
            _sse_stream(broadcaster, q),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return r
