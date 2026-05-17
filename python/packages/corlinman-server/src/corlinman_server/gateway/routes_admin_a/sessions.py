"""``/admin/sessions*`` — operator-facing replay surface.

Python port of ``rust/crates/corlinman-gateway/src/routes/admin/sessions.rs``.

Two routes (both behind :func:`require_admin_dependency`):

* ``GET  /admin/sessions``                  — list of sessions for the
  resolved tenant. Reads
  ``<data_dir>/tenants/<tenant>/sessions.sqlite`` via the
  :mod:`corlinman_replay` primitive (and falls back to the flat
  ``<data_dir>/sessions.sqlite`` when ``tenants_enabled = False`` and
  the tenant is the legacy default).
* ``POST /admin/sessions/{session_key}/replay`` — deterministic
  transcript dump. Body ``{ "mode": "transcript" | "rerun" }``;
  defaults to ``"transcript"`` when omitted. ``"rerun"`` ships in
  v1 with **503 ``rerun_disabled``** because the chat-service wiring
  needed to regenerate the assistant turn lives in the parallel
  ``routes_admin_b`` scope.

Disabled gate: when ``state.sessions_disabled = True`` every route
returns **503 ``sessions_disabled``**.

Tenant resolution mirrors :mod:`api_keys`:

1. ``?tenant=`` query string,
2. ``state.default_tenant``,
3. :func:`corlinman_server.tenancy.default_tenant`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from corlinman_replay import (
    ReplayError,
    ReplayMode,
    SessionListRow,
    SessionNotFoundError,
    SqliteSessionStore,
    StoreLoadError,
    StoreOpenError,
    list_sessions as replay_list_sessions,
    replay as replay_fn,
    replay_from_messages,
    sessions_db_path,
)
from corlinman_replay import TenantId as ReplayTenantId

from corlinman_server.gateway.routes_admin_a._auth_shim import (
    require_admin_dependency,
)
from corlinman_server.gateway.routes_admin_a.state import (
    AdminState,
    get_admin_state,
)
from corlinman_server.tenancy import (
    TenantId,
    TenantIdError,
    default_tenant,
)


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class SessionSummaryOut(BaseModel):
    """One row in ``GET /admin/sessions``."""

    session_key: str
    last_message_at: int  # unix milliseconds
    message_count: int


class SessionsListOut(BaseModel):
    """``GET /admin/sessions`` response."""

    sessions: list[SessionSummaryOut] = Field(default_factory=list)


class ReplayBody(BaseModel):
    """``POST /admin/sessions/{session_key}/replay`` body."""

    mode: str | None = None  # "transcript" | "rerun" | None → "transcript"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sessions_disabled() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"error": "sessions_disabled"},
    )


def _session_not_found(session_key: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "not_found", "session_key": session_key},
    )


def _storage_error(exc: BaseException) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"error": "storage_error", "message": str(exc)},
    )


def _rerun_disabled() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"error": "rerun_disabled"},
    )


def _invalid_tenant_slug(slug: str, reason: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={
            "error": "invalid_tenant_slug",
            "reason": reason,
            "slug": slug,
        },
    )


def _resolve_tenant(state: AdminState, tenant_q: str | None) -> TenantId:
    """Same precedence chain as :mod:`api_keys._resolve_tenant`."""
    if tenant_q:
        try:
            return TenantId.new(tenant_q)
        except TenantIdError as exc:
            raise _invalid_tenant_slug(tenant_q, str(exc)) from exc
    if state.default_tenant is not None:
        return state.default_tenant
    return default_tenant()


def _resolve_data_dir(state: AdminState) -> Path:
    """Mirror the Rust ``resolve_data_dir``: prefer the state override
    (used by tests pinning a tempdir), fall back to ``CORLINMAN_DATA_DIR``,
    finally ``~/.corlinman``."""
    if state.data_dir is not None:
        return Path(state.data_dir)
    env = os.environ.get("CORLINMAN_DATA_DIR")
    if env:
        return Path(env)
    return Path.home() / ".corlinman"


def _should_use_flat_legacy_sessions(
    state: AdminState, tenant: TenantId
) -> bool:
    """Mirror the Rust ``should_use_flat_legacy_sessions``: when the
    operator hasn't opted into multi-tenant AND the resolved tenant is
    the legacy default, read from the flat ``<data_dir>/sessions.sqlite``
    instead of the per-tenant path."""
    return (not state.tenants_enabled) and tenant.is_legacy_default()


def _to_replay_tenant(tenant: TenantId) -> ReplayTenantId:
    """Convert a server :class:`TenantId` into a replay-package
    :class:`ReplayTenantId`. Both use the same slug regex so the cast is
    safe; we re-validate to keep type checkers happy."""
    return ReplayTenantId.new(tenant.as_str())


# --- flat-legacy fallback ---------------------------------------------------


async def _list_flat_legacy_sessions(data_dir: Path) -> list[SessionListRow]:
    """List sessions out of the legacy single-file
    ``<data_dir>/sessions.sqlite``."""
    path = data_dir / "sessions.sqlite"
    store = await SqliteSessionStore.open(path)
    try:
        rows = await store.list_sessions()
    finally:
        await store.close()
    return [SessionListRow.from_summary(s) for s in rows]


async def _replay_flat_legacy_session(
    data_dir: Path, tenant: ReplayTenantId, session_key: str, mode: ReplayMode
) -> Any:
    """Replay a session out of the legacy single-file
    ``<data_dir>/sessions.sqlite``."""
    path = data_dir / "sessions.sqlite"
    store = await SqliteSessionStore.open(path)
    try:
        messages = await store.load(session_key)
    finally:
        await store.close()
    return replay_from_messages(tenant, session_key, mode, messages)


# --- dispatch helpers -------------------------------------------------------


async def _list_sessions_for_request(
    state: AdminState, data_dir: Path, tenant: TenantId
) -> list[SessionListRow]:
    if _should_use_flat_legacy_sessions(state, tenant):
        return await _list_flat_legacy_sessions(data_dir)
    return await replay_list_sessions(data_dir, _to_replay_tenant(tenant))


async def _replay_for_request(
    state: AdminState,
    data_dir: Path,
    tenant: TenantId,
    session_key: str,
    mode: ReplayMode,
) -> Any:
    rep_tenant = _to_replay_tenant(tenant)
    if _should_use_flat_legacy_sessions(state, tenant):
        return await _replay_flat_legacy_session(
            data_dir, rep_tenant, session_key, mode
        )
    return await replay_fn(data_dir, rep_tenant, session_key, mode)


def _parse_mode(raw: str | None) -> ReplayMode:
    """Map the wire ``mode`` field to a :class:`ReplayMode`. ``None`` /
    empty defaults to ``TRANSCRIPT`` (matches the CLI default)."""
    if raw is None:
        return ReplayMode.TRANSCRIPT
    lowered = raw.lower()
    if lowered == "rerun":
        return ReplayMode.RERUN
    return ReplayMode.TRANSCRIPT


def _replay_to_dict(out: Any) -> dict[str, Any]:
    """Serialise a :class:`ReplayOutput` to the same JSON shape the Rust
    side emits."""
    summary = {
        "message_count": out.summary.message_count,
        "tenant_id": out.summary.tenant_id,
    }
    if out.summary.rerun_diff is not None:
        summary["rerun_diff"] = out.summary.rerun_diff
    return {
        "session_key": out.session_key,
        "mode": out.mode,
        "transcript": [
            {"role": m.role, "content": m.content, "ts": m.ts}
            for m in out.transcript
        ],
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    """Sub-router for ``/admin/sessions*``."""
    r = APIRouter(dependencies=[Depends(require_admin_dependency)])

    @r.get(
        "/admin/sessions",
        response_model=SessionsListOut,
        summary="List sessions for the resolved tenant",
    )
    async def list_handler(
        state: Annotated[AdminState, Depends(get_admin_state)],
        tenant: Annotated[str | None, Query()] = None,
    ) -> SessionsListOut:
        if state.sessions_disabled:
            raise _sessions_disabled()
        tenant_id = _resolve_tenant(state, tenant)
        data_dir = _resolve_data_dir(state)
        try:
            rows = await _list_sessions_for_request(state, data_dir, tenant_id)
        except StoreOpenError:
            # No sessions.sqlite for this tenant yet — return an empty
            # list (matches the Rust handler's StoreOpen path).
            rows = []
        except ReplayError as exc:
            raise _storage_error(exc) from exc
        return SessionsListOut(
            sessions=[
                SessionSummaryOut(
                    session_key=r.session_key,
                    last_message_at=r.last_message_at,
                    message_count=r.message_count,
                )
                for r in rows
            ]
        )

    @r.post(
        "/admin/sessions/{session_key}/replay",
        summary="Deterministic replay of a session",
    )
    async def replay_handler(
        session_key: str,
        state: Annotated[AdminState, Depends(get_admin_state)],
        tenant: Annotated[str | None, Query()] = None,
        body: ReplayBody | None = None,
    ) -> dict[str, Any]:
        if state.sessions_disabled:
            raise _sessions_disabled()

        mode = _parse_mode(body.mode if body is not None else None)
        tenant_id = _resolve_tenant(state, tenant)
        data_dir = _resolve_data_dir(state)

        # Always run the underlying replay in TRANSCRIPT mode — rerun
        # mode is wholly served by the chat-service plumbing in
        # ``routes_admin_b`` which the admin-A slice doesn't own.
        try:
            out = await _replay_for_request(
                state, data_dir, tenant_id, session_key, ReplayMode.TRANSCRIPT
            )
        except (SessionNotFoundError, StoreOpenError) as exc:
            raise _session_not_found(session_key) from exc
        except (StoreLoadError, ReplayError) as exc:
            raise _storage_error(exc) from exc

        if mode == ReplayMode.TRANSCRIPT:
            return _replay_to_dict(out)

        # mode == RERUN — the chat-service handle (Rust: ``replay_chat_service``)
        # is owned by ``routes_admin_b``. Until it's wired we return the
        # same 503 envelope the Rust side emits when the service is
        # missing.
        raise _rerun_disabled()

    return r


__all__ = [
    "ReplayBody",
    "SessionSummaryOut",
    "SessionsListOut",
    "router",
]
