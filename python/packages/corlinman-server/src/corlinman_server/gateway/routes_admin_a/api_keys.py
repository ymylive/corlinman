"""``/admin/api_keys*`` — operator-facing API-key mint surface.

Python port of ``rust/crates/corlinman-gateway/src/routes/admin/api_keys.rs``.

Three routes (all behind :func:`require_admin_dependency`):

* ``POST   /admin/api_keys`` — mint a fresh bearer token. Body
  ``{ scope, username?, label? }``. 201 returns the cleartext
  ``token`` **once** alongside ``key_id``, ``tenant_id``, etc.
* ``GET    /admin/api_keys`` — list active keys for the resolved
  tenant. Excludes both the cleartext and the hash.
* ``DELETE /admin/api_keys/{key_id}`` — flip a key to revoked.
  ``false`` (already revoked / unknown id) is a 404.

Tenant resolution: the Rust side uses the ``Tenant`` extractor that
the tenant-scope middleware populates. Here we read the resolved
tenant from the ``?tenant=`` query param, falling back to the state's
``default_tenant`` (which itself defaults to ``corlinman_server.tenancy
.default_tenant()``). The bootstrapper that mounts the real
tenant-scope middleware can override this via a tenant-resolver
dependency.

When ``state.admin_db`` is ``None``, every route returns
**503 ``tenants_disabled``**.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from corlinman_server.gateway.routes_admin_a._auth_shim import (
    require_admin_dependency,
)
from corlinman_server.gateway.routes_admin_a.state import (
    AdminState,
    get_admin_state,
)
from corlinman_server.tenancy import (
    AdminDb,
    MintedApiKey,
    TenantId,
    TenantIdError,
    default_tenant,
)


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class MintBody(BaseModel):
    scope: str
    username: str | None = None
    label: str | None = None


class MintResponse(BaseModel):
    key_id: str
    tenant_id: str
    username: str
    scope: str
    label: str | None
    token: str
    created_at_ms: int

    @classmethod
    def from_minted(cls, m: MintedApiKey) -> MintResponse:
        return cls(
            key_id=m.row.key_id,
            tenant_id=m.row.tenant_id.as_str(),
            username=m.row.username,
            scope=m.row.scope,
            label=m.row.label,
            token=m.token,
            created_at_ms=m.row.created_at_ms,
        )


class ApiKeyOut(BaseModel):
    key_id: str
    tenant_id: str
    username: str
    scope: str
    label: str | None
    created_at_ms: int
    last_used_at_ms: int | None


class ApiKeyListOut(BaseModel):
    keys: list[ApiKeyOut]


class RevokeOut(BaseModel):
    revoked: bool
    key_id: str


# ---------------------------------------------------------------------------
# Tenant resolution
# ---------------------------------------------------------------------------


def _tenants_disabled() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "error": "tenants_disabled",
            "message": "tenant admin DB is not configured on this gateway",
        },
    )


def _require_admin_db(state: AdminState) -> AdminDb:
    if state.admin_db is None:
        raise _tenants_disabled()
    return state.admin_db


def _resolve_tenant(state: AdminState, tenant_q: str | None) -> TenantId:
    """Resolve the request's tenant.

    Order of precedence:

    1. ``?tenant=...`` query string (the Rust ``tenant_scope`` middleware
       reads this from request extensions; the Python port surfaces it
       via the query param until the real middleware lands).
    2. ``state.default_tenant`` configured on the bootstrapped
       :class:`AdminState`.
    3. :func:`corlinman_server.tenancy.default_tenant` — the legacy
       single-tenant ``"default"`` slug.

    A malformed slug yields **400 ``invalid_tenant_slug``**.
    """
    if tenant_q:
        try:
            return TenantId.new(tenant_q)
        except TenantIdError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "invalid_tenant_slug",
                    "reason": str(exc),
                    "slug": tenant_q,
                },
            ) from exc
    if state.default_tenant is not None:
        return state.default_tenant
    return default_tenant()


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    """Sub-router for ``/admin/api_keys*``."""
    r = APIRouter(dependencies=[Depends(require_admin_dependency)])

    @r.post(
        "/admin/api_keys",
        response_model=MintResponse,
        status_code=status.HTTP_201_CREATED,
        summary="Mint a fresh bearer token",
    )
    async def mint_key(
        body: MintBody,
        state: Annotated[AdminState, Depends(get_admin_state)],
        tenant: Annotated[str | None, Query()] = None,
    ) -> MintResponse:
        db = _require_admin_db(state)
        tenant_id = _resolve_tenant(state, tenant)

        scope = body.scope.strip()
        if not scope:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "invalid_request",
                    "message": "`scope` is required and must be non-empty",
                },
            )

        username = (body.username or "").strip() or "admin"
        label = (body.label or "").strip() or None

        try:
            minted = await db.mint_api_key(tenant_id, username, scope, label)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error": "mint_failed", "message": str(exc)},
            ) from exc

        return MintResponse.from_minted(minted)

    @r.get(
        "/admin/api_keys",
        response_model=ApiKeyListOut,
        summary="List active API keys for the resolved tenant",
    )
    async def list_keys(
        state: Annotated[AdminState, Depends(get_admin_state)],
        tenant: Annotated[str | None, Query()] = None,
    ) -> ApiKeyListOut:
        db = _require_admin_db(state)
        tenant_id = _resolve_tenant(state, tenant)
        try:
            rows = await db.list_api_keys(tenant_id)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error": "list_failed", "message": str(exc)},
            ) from exc

        return ApiKeyListOut(
            keys=[
                ApiKeyOut(
                    key_id=row.key_id,
                    tenant_id=row.tenant_id.as_str(),
                    username=row.username,
                    scope=row.scope,
                    label=row.label,
                    created_at_ms=row.created_at_ms,
                    last_used_at_ms=row.last_used_at_ms,
                )
                for row in rows
            ]
        )

    @r.delete(
        "/admin/api_keys/{key_id}",
        response_model=RevokeOut,
        summary="Revoke an API key",
    )
    async def revoke_key(
        key_id: str,
        state: Annotated[AdminState, Depends(get_admin_state)],
        tenant: Annotated[str | None, Query()] = None,
    ) -> RevokeOut:
        db = _require_admin_db(state)
        # Resolve the tenant for parity even though revoke is keyed on
        # ``key_id`` alone — the middleware would have rejected an
        # invalid slug before reaching us.
        _ = _resolve_tenant(state, tenant)
        try:
            revoked = await db.revoke_api_key(key_id)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error": "revoke_failed", "message": str(exc)},
            ) from exc
        if not revoked:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "not_found",
                    "resource": "api_key",
                    "key_id": key_id,
                },
            )
        return RevokeOut(revoked=True, key_id=key_id)

    return r


__all__ = [
    "ApiKeyListOut",
    "ApiKeyOut",
    "MintBody",
    "MintResponse",
    "RevokeOut",
    "router",
]
