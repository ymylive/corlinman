"""``POST /v1/plugins/callback/{task_id}`` — async plugin completion webhook.

Python port of
``rust/crates/corlinman-gateway/src/routes/plugin_callback.rs``.

Long-running ("async") plugins respond to ``tools/call`` with a
synthetic ``{"task_id": "tsk_..."}`` result, which the gateway's
tool executor parks on a process-wide
:class:`corlinman_providers.plugins.AsyncTaskRegistry`. The plugin
later POSTs the real result here; that call wakes the parked tool
call so the chat reasoning loop resumes.

The Rust route is mounted at ``/plugin-callback/:task_id``; we mount
at the more conventional ``/v1/plugins/callback/{task_id}`` to align
with the rest of the ``/v1/*`` surface — but we ALSO keep the
legacy ``/plugin-callback/{task_id}`` route as an alias so an
in-flight plugin built against the Rust gateway still works.

Auth model: the ``task_id`` itself is a one-shot, unguessable
credential. The registry only accepts the first callback for a
given id and drops the entry. No other authentication sits in front
of this route — same model as the Rust route.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from corlinman_providers.plugins.async_task import (
    AsyncTaskCompletionError,
    AsyncTaskRegistry,
    CompleteError,
)

__all__ = ["router"]


def router(registry: AsyncTaskRegistry | None = None) -> APIRouter:
    """Build the plugin-callback sub-router.

    :param registry: :class:`AsyncTaskRegistry` holding the parked
        tool calls. When ``None`` every callback returns 501 with the
        Rust-compatible ``not_implemented`` envelope.
    """
    api = APIRouter()

    async def _handle(task_id: str, request: Request) -> JSONResponse:
        if registry is None:
            return JSONResponse(
                {
                    "error": "not_implemented",
                    "route": "/v1/plugins/callback/{task_id}",
                    "message": (
                        "no AsyncTaskRegistry wired; build router(registry=...)"
                    ),
                },
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
            )

        # The plugin can post any JSON; we treat it opaquely and hand
        # it back to the waiter verbatim. An empty body is fine — some
        # plugins signal completion with no payload.
        try:
            payload: Any = await request.json()
        except Exception:  # noqa: BLE001 — invalid JSON → 400
            return JSONResponse(
                {
                    "error": "invalid_json",
                    "task_id": task_id,
                    "message": "request body must be valid JSON (use {} for none)",
                },
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        try:
            registry.complete(task_id, payload)
        except AsyncTaskCompletionError as err:
            if err.reason == CompleteError.NOT_FOUND:
                return JSONResponse(
                    {
                        "error": "task_not_found",
                        "task_id": task_id,
                    },
                    status_code=status.HTTP_404_NOT_FOUND,
                )
            # CompleteError.WAITER_DROPPED → 410 Gone, mirrors the Rust
            # CompleteError::WaiterDropped arm.
            return JSONResponse(
                {
                    "error": "waiter_dropped",
                    "task_id": task_id,
                    "message": (
                        "callback arrived after chat client disconnected "
                        "or timed out"
                    ),
                },
                status_code=status.HTTP_410_GONE,
            )

        return JSONResponse({"status": "ok"})

    @api.post("/v1/plugins/callback/{task_id}")
    async def plugin_callback_v1(task_id: str, request: Request) -> JSONResponse:  # noqa: D401
        """Canonical ``/v1/*`` route."""
        return await _handle(task_id, request)

    @api.post("/plugin-callback/{task_id}")
    async def plugin_callback_legacy(task_id: str, request: Request) -> JSONResponse:  # noqa: D401
        """Legacy Rust-compatible route alias."""
        return await _handle(task_id, request)

    return api
