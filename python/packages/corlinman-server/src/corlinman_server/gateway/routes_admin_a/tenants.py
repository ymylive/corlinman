"""``/admin/tenants*`` — operator-only multi-tenant registry routes.

Python port of ``rust/crates/corlinman-gateway/src/routes/admin/tenants.rs``.

Routes (all behind :func:`require_admin_dependency`):

* ``GET  /admin/tenants``   — list active rows from
  :class:`AdminDb.list_active` plus the operator-allowed set.
* ``POST /admin/tenants``   — create a new tenant + its first
  argon2id-hashed admin row. Mirrors the ``corlinman tenant create``
  CLI flow.
* ``GET  /admin/tenants/{tenant}/prompt_segments/{name}``
* ``GET  /admin/tenants/{tenant}/agent_cards/{name}``
  — read per-tenant content files for the diff view. ``exists = False``
  is a legitimate response (not a 404) so the UI's diff view can
  render an empty "before" pane.

Disabled / unconfigured envelopes:

* **403 ``tenants_disabled``** when ``state.tenants_enabled = False``.
* **503 ``tenants_disabled`` + ``reason=admin_db_missing``** when the
  gateway booted with ``tenants_enabled = True`` but
  :class:`AdminDb` failed to open.
"""

from __future__ import annotations

import os
import time as _time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from argon2 import PasswordHasher
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from corlinman_server.gateway.routes_admin_a._auth_shim import (
    require_admin_dependency,
)
from corlinman_server.gateway.routes_admin_a.state import (
    AdminState,
    get_admin_state,
)
from corlinman_server.tenancy import (
    AdminDb,
    TenantExistsError,
    TenantId,
    TenantIdError,
    tenant_root_dir,
)


# ---------------------------------------------------------------------------
# argon2 helper — mirror of the CLI's :func:`hash_password`.
# ---------------------------------------------------------------------------
#
# The Rust comment on ``tenants.rs::hash_password`` explicitly chooses to
# inline the helper rather than widen the ``corlinman-tenant`` API
# surface for a single caller. The Python port keeps the same
# discipline: a single :class:`PasswordHasher` reused across calls so
# the parameter setup cost happens once at import.
_HASHER = PasswordHasher()


def _hash_password(password: str) -> str:
    """Argon2id hash. Matches the Rust ``hash_password`` byte-for-byte
    in algorithm choice (the same ``argon2::Argon2::default()`` params
    the ``argon2-cffi`` ``PasswordHasher()`` no-arg constructor uses)."""
    return _HASHER.hash(password)


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class TenantOut(BaseModel):
    """One row in ``GET /admin/tenants``. ``created_at`` is RFC-3339 /
    ISO-8601 string (the SQLite column is unix-millis; we convert at
    the wire boundary)."""

    tenant_id: str
    display_name: str
    created_at: str


class TenantsListOut(BaseModel):
    """``GET /admin/tenants`` response."""

    tenants: list[TenantOut] = Field(default_factory=list)
    allowed: list[str] = Field(default_factory=list)


class CreateBody(BaseModel):
    """``POST /admin/tenants`` body. ``display_name`` is optional; when
    omitted the slug doubles as the display name."""

    slug: str
    display_name: str | None = None
    admin_username: str
    admin_password: str


class CreateOut(BaseModel):
    """``POST /admin/tenants`` 201 response."""

    tenant_id: str


class TenantContentOut(BaseModel):
    """``GET /admin/tenants/{tenant}/{kind}/{name}`` response. ``exists
    = False`` is a legitimate response shape, not a 404, so the UI can
    render an empty "before" pane in the diff view."""

    tenant_id: str
    kind: str
    name: str
    exists: bool
    content: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_created_at_ms(ms: int) -> str:
    """Unix-millis → RFC-3339 / ISO-8601 with ``Z`` suffix. Falls back
    to ``str(ms)`` for timestamps outside any sane range (matches the
    Rust ``format_created_at_ms`` fallback)."""
    try:
        dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
        iso = dt.isoformat()
        if iso.endswith("+00:00"):
            return iso[:-6] + "Z"
        return iso
    except (OverflowError, OSError, ValueError):
        return str(ms)


def _now_unix_ms() -> int:
    """Current wall-clock unix milliseconds."""
    return int(_time.time() * 1000)


def _resolve_data_dir(state: AdminState) -> Path:
    """Same precedence chain as :mod:`sessions._resolve_data_dir`:
    state override → ``$CORLINMAN_DATA_DIR`` → ``~/.corlinman``."""
    if state.data_dir is not None:
        return Path(state.data_dir)
    env = os.environ.get("CORLINMAN_DATA_DIR")
    if env:
        return Path(env)
    return Path.home() / ".corlinman"


def _is_valid_segment_name(s: str) -> bool:
    """Mirror Rust ``is_valid_segment_name`` — defensive validation for
    prompt_segment ids read off the URL path. Drift between this and
    the applier-side validator is OK; the applier is stricter and
    rejects anything this passes that fails the canonical spec."""
    if not s or len(s) > 128:
        return False
    if s.startswith(".") or s.endswith(".") or ".." in s:
        return False
    return all(c.islower() and c.isascii() or c.isdigit() or c in "_." for c in s)


def _is_valid_agent_name(s: str) -> bool:
    """Mirror Rust ``is_valid_agent_name`` — same shape as the segment
    validator but for the ``agent_card`` whitelist
    (``[a-z][a-z0-9_-]{0,63}``)."""
    if not s or len(s) > 64:
        return False
    first = s[0]
    if not (first.isascii() and first.islower()):
        return False
    return all(
        (c.isascii() and c.islower()) or c.isdigit() or c in "_-" for c in s
    )


# --- 4xx / 5xx envelopes ----------------------------------------------------


def _tenants_disabled_403() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"error": "tenants_disabled"},
    )


def _admin_db_missing_503() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"error": "tenants_disabled", "reason": "admin_db_missing"},
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


def _missing_admin_username() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"error": "missing_admin_username"},
    )


def _missing_admin_password() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"error": "missing_admin_password"},
    )


def _tenant_exists() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"error": "tenant_exists"},
    )


def _storage_error(exc: BaseException) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"error": "storage_error", "message": str(exc)},
    )


def _invalid_name(kind: str, name: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"error": "invalid_name", "kind": kind, "name": name},
    )


def _require_active(state: AdminState) -> AdminDb:
    """Decide which "disabled / unconfigured" envelope applies and raise
    it; otherwise return the live :class:`AdminDb`."""
    if not state.tenants_enabled:
        raise _tenants_disabled_403()
    if state.admin_db is None:
        raise _admin_db_missing_503()
    return state.admin_db


async def _read_tenant_content(
    state: AdminState,
    tenant_raw: str,
    name: str,
    *,
    kind: str,
    name_validator: Any,
    subdir: str,
) -> TenantContentOut:
    """Read a per-tenant content file and surface its current bytes for
    the operator UI's diff view. ``kind`` parameterises both the
    directory layout and the wire-shape ``kind`` field."""
    _require_active(state)

    # Reject malformed slugs before touching the filesystem.
    try:
        tenant = TenantId.new(tenant_raw)
    except TenantIdError as exc:
        raise _invalid_tenant_slug(tenant_raw, str(exc)) from exc

    if not name_validator(name):
        raise _invalid_name(kind, name)

    data_dir = _resolve_data_dir(state)
    path = tenant_root_dir(data_dir, tenant) / subdir / f"{name}.md"

    try:
        content = path.read_text(encoding="utf-8")
        exists = True
    except FileNotFoundError:
        content = ""
        exists = False
    except OSError as exc:
        raise _storage_error(exc) from exc

    return TenantContentOut(
        tenant_id=tenant.as_str(),
        kind=kind,
        name=name,
        exists=exists,
        content=content,
    )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    """Sub-router for ``/admin/tenants*``."""
    r = APIRouter(dependencies=[Depends(require_admin_dependency)])

    @r.get(
        "/admin/tenants",
        response_model=TenantsListOut,
        summary="List active tenants + the operator-allowed set",
    )
    async def list_tenants(
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> TenantsListOut:
        db = _require_active(state)
        try:
            rows = await db.list_active()
        except Exception as exc:
            raise _storage_error(exc) from exc
        return TenantsListOut(
            tenants=[
                TenantOut(
                    tenant_id=row.tenant_id.as_str(),
                    display_name=row.display_name,
                    created_at=_format_created_at_ms(row.created_at),
                )
                for row in rows
            ],
            allowed=sorted(t.as_str() for t in state.allowed_tenants),
        )

    @r.post(
        "/admin/tenants",
        response_model=CreateOut,
        status_code=status.HTTP_201_CREATED,
        summary="Create a tenant + first admin row",
    )
    async def create_tenant(
        body: CreateBody,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> CreateOut:
        db = _require_active(state)

        try:
            tenant_id = TenantId.new(body.slug)
        except TenantIdError as exc:
            raise _invalid_tenant_slug(body.slug, str(exc)) from exc

        # The pydantic model would already reject missing *keys*; we
        # only need to guard empty strings here. Matches the Rust contract.
        if not body.admin_username:
            raise _missing_admin_username()
        if not body.admin_password:
            raise _missing_admin_password()

        # Pre-create the per-tenant directory tree — downstream stores
        # call ``tenant_db_path(...)`` which assumes the parent exists.
        data_dir = _resolve_data_dir(state)
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            tenant_root_dir(data_dir, tenant_id).mkdir(
                parents=True, exist_ok=True
            )
        except OSError as exc:
            raise _storage_error(exc) from exc

        display_name = body.display_name or body.slug
        if not display_name:
            display_name = body.slug

        now_ms = _now_unix_ms()

        # Insert the tenant row first so a duplicate slug fails fast and
        # we don't waste an argon2 hash cycle on a request that's
        # already rejected.
        try:
            await db.create_tenant(tenant_id, display_name, now_ms)
        except TenantExistsError as exc:
            raise _tenant_exists() from exc
        except Exception as exc:
            raise _storage_error(exc) from exc

        try:
            password_hash = _hash_password(body.admin_password)
        except Exception as exc:
            raise _storage_error(exc) from exc

        try:
            await db.add_admin(
                tenant_id, body.admin_username, password_hash, now_ms
            )
        except Exception as exc:
            # The tenant row landed but the admin row didn't. Match the
            # Rust contract: surface the storage error rather than try
            # to roll back. Soft-delete / cleanup is out of scope.
            raise _storage_error(exc) from exc

        return CreateOut(tenant_id=tenant_id.as_str())

    @r.get(
        "/admin/tenants/{tenant}/prompt_segments/{name}",
        response_model=TenantContentOut,
        summary="Read a per-tenant prompt segment for the diff view",
    )
    async def read_prompt_segment(
        tenant: str,
        name: str,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> TenantContentOut:
        return await _read_tenant_content(
            state,
            tenant,
            name,
            kind="prompt_template",
            name_validator=_is_valid_segment_name,
            subdir="prompt_segments",
        )

    @r.get(
        "/admin/tenants/{tenant}/agent_cards/{name}",
        response_model=TenantContentOut,
        summary="Read a per-tenant agent card for the diff view",
    )
    async def read_agent_card(
        tenant: str,
        name: str,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> TenantContentOut:
        return await _read_tenant_content(
            state,
            tenant,
            name,
            kind="agent_card",
            name_validator=_is_valid_agent_name,
            subdir="agent_cards",
        )

    return r


__all__ = [
    "CreateBody",
    "CreateOut",
    "TenantContentOut",
    "TenantOut",
    "TenantsListOut",
    "router",
]
