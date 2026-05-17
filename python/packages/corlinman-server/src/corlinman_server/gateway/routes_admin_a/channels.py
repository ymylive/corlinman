"""``/admin/channels/qq*`` — QQ/OneBot channel management.

Python port of ``rust/crates/corlinman-gateway/src/routes/admin/channels.rs``.

Three routes (all behind :func:`require_admin_dependency`):

* ``GET  /admin/channels/qq/status``     — configuration snapshot. Reads
  ``state.channels_config`` which the bootstrapper hands in as a dict
  with the keys ``enabled``, ``ws_url``, ``self_ids``, ``group_keywords``.
* ``POST /admin/channels/qq/reconnect``  — placeholder; returns 501
  ``reconnect_unsupported`` matching the Rust contract.
* ``POST /admin/channels/qq/keywords``   — updates the
  ``group_keywords`` map and persists via ``state.channels_writer``.

NapCat-flavoured sub-routes (``/admin/channels/qq/{qrcode,accounts,
quick-login,qrcode/status}``) are part of the ``napcat`` Rust module
in the Rust tree — assigned to the parallel ``routes_admin_b`` agent.
This module deliberately does **not** mount them.
"""

from __future__ import annotations

import inspect
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

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


class StatusOut(BaseModel):
    configured: bool
    enabled: bool
    ws_url: str | None
    self_ids: list[int] = Field(default_factory=list)
    group_keywords: dict[str, list[str]] = Field(default_factory=dict)
    runtime: str = "unknown"
    recent_messages: list[Any] = Field(default_factory=list)


class KeywordsBody(BaseModel):
    """Full replacement map: ``group_id → [keyword, …]``."""

    group_keywords: dict[str, list[str]] = Field(default_factory=dict)


class KeywordsOut(BaseModel):
    status: str
    group_keywords: dict[str, list[str]]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _qq_config(state: AdminState) -> dict[str, Any] | None:
    """Borrow the QQ subsection of the channels config dict. Returns
    ``None`` when the bootstrapper didn't pre-populate it (the Rust
    ``cfg.channels.qq.is_none()`` path)."""
    if state.channels_config is None:
        return None
    qq = state.channels_config.get("qq")
    if not isinstance(qq, dict):
        return None
    return qq


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    """Sub-router for ``/admin/channels/qq*``."""
    r = APIRouter(dependencies=[Depends(require_admin_dependency)])

    @r.get(
        "/admin/channels/qq/status",
        response_model=StatusOut,
        summary="Snapshot of the QQ channel configuration",
    )
    async def status_handler(
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> StatusOut:
        qq = _qq_config(state)
        if qq is None:
            return StatusOut(
                configured=False,
                enabled=False,
                ws_url=None,
            )
        return StatusOut(
            configured=True,
            enabled=bool(qq.get("enabled", False)),
            ws_url=qq.get("ws_url"),
            self_ids=list(qq.get("self_ids", [])),
            group_keywords=dict(qq.get("group_keywords", {})),
        )

    @r.post(
        "/admin/channels/qq/reconnect",
        summary="Placeholder — force a QQ ws reconnect (not implemented)",
    )
    async def reconnect(
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> None:
        qq = _qq_config(state)
        if qq is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": "channel_not_configured",
                    "message": "no [channels.qq] section in config",
                },
            )
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail={
                "error": "reconnect_unsupported",
                "message": (
                    "force-reconnect control is not yet implemented; "
                    "the OneBot client handles reconnect internally"
                ),
            },
        )

    @r.post(
        "/admin/channels/qq/keywords",
        response_model=KeywordsOut,
        summary="Replace the QQ per-group keyword overrides",
    )
    async def update_keywords(
        body: KeywordsBody,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> KeywordsOut:
        # Validate up front so an empty group / keyword is rejected
        # before we touch the writer.
        for group, kws in body.group_keywords.items():
            if not group:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "error": "invalid_group",
                        "message": "group id must be non-empty",
                    },
                )
            if any(not k for k in kws):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "error": "invalid_keyword",
                        "message": "keyword must be non-empty",
                    },
                )

        qq = _qq_config(state)
        if qq is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": "channel_not_configured",
                    "message": (
                        "[channels.qq] missing; add a stub in config.toml "
                        "before editing keywords"
                    ),
                },
            )

        qq["group_keywords"] = dict(body.group_keywords)

        writer = state.channels_writer
        if writer is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": "config_path_unset",
                    "message": "gateway booted without a config writer",
                },
            )
        try:
            ret = writer(state.channels_config)
            if inspect.isawaitable(ret):
                await ret
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error": "write_failed", "message": str(exc)},
            ) from exc

        return KeywordsOut(
            status="ok",
            group_keywords=dict(qq.get("group_keywords", {})),
        )

    return r


__all__ = [
    "KeywordsBody",
    "KeywordsOut",
    "StatusOut",
    "router",
]
