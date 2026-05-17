"""Tenant-scoping middleware for ``/admin/*`` (and any other gated surface).

Python port of ``rust/crates/corlinman-gateway/src/middleware/tenant_scope.rs``.

Two policy modes, decided once at boot from the ``[tenants].enabled``
config switch:

* **Disabled (legacy single-tenant)** — every request gets
  :func:`~corlinman_server.tenancy.default_tenant` without consulting
  the request. The middleware is effectively transparent. This is the
  byte-for-byte pre-Phase-4 behaviour.
* **Enabled (multi-tenant)** — the middleware extracts a candidate
  slug from ``?tenant=<slug>`` (or the ``X-Corlinman-Tenant`` header
  fallback for clients that can't put query params in the URL). The
  slug is parsed through :meth:`TenantId.new` and validated against
  the operator-allowed set in :attr:`TenantScopeState.allowed`. Empty
  / missing falls back to :attr:`TenantScopeState.fallback`.

Two short-circuit error paths mirror the Rust impl exactly:

* HTTP **400 ``invalid_tenant_slug``** when the query carries a slug
  that fails the ``^[a-z][a-z0-9-]{0,62}$`` shape.
* HTTP **403 ``tenant_not_allowed``** when the slug parses but is not
  in the allowed set.

Mount order: this layer sits *inside* :class:`AdminAuthMiddleware` (so
anonymous callers never see ``tenant_not_allowed`` — they get 401 first)
and *outside* per-route handlers (so handlers always observe a resolved
:class:`TenantId` on ``request.state.tenant``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, unquote

import structlog
from fastapi import Depends, HTTPException, Request, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from corlinman_server.tenancy import (
    TenantId,
    TenantIdError,
    default_tenant,
)

logger = structlog.get_logger(__name__)


#: Header clients may use as an alternative to ``?tenant=<slug>`` when a
#: query parameter is awkward (e.g. websocket upgrade requests). Read
#: only when the query is absent so query-based scoping stays
#: authoritative.
TENANT_HEADER_NAME: str = "X-Corlinman-Tenant"

#: Path key middleware can also pull a tenant slug from, e.g. routes
#: mounted under ``/v1/tenants/{tenant}/...``. Activates when the
#: matched route exposes the parameter in ``request.path_params``.
TENANT_PATH_PARAM: str = "tenant"


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass
class TenantScopeState:
    """Boot-time state for the tenant-scope middleware.

    ``enabled`` mirrors ``Config::tenants::enabled`` at gateway start;
    ``allowed`` is the union of ``[tenants].allowed`` slugs plus the
    legacy default; ``fallback`` is the tenant returned when the request
    omits a tenant indicator (matches ``[tenants].default``).
    """

    enabled: bool = False
    allowed: frozenset[TenantId] = field(default_factory=frozenset)
    fallback: TenantId = default_tenant()

    @classmethod
    def disabled(cls) -> "TenantScopeState":
        """Build a disabled state where every request resolves to
        :func:`~corlinman_server.tenancy.default_tenant`. Used by tests
        that want to assert handler behaviour without exercising tenant
        scoping."""

        return cls(enabled=False, allowed=frozenset(), fallback=default_tenant())


# ---------------------------------------------------------------------------
# Helpers — pure, exported for tests.
# ---------------------------------------------------------------------------


def extract_tenant_query(query: str) -> str | None:
    """Pull the first ``tenant=`` value out of a percent-encoded query
    string.

    Mirrors the Rust ``extract_tenant_query`` helper: the tenant query
    is single-valued, slugs are ``[a-z0-9-]`` (none of which need
    percent-encoding), so we just defensively unescape ``%2D`` /
    ``%2d``. Anything more exotic falls through and
    :meth:`TenantId.new` rejects it.
    """

    if not query:
        return None
    # ``parse_qsl`` keeps order and respects ``&`` separators.
    for key, value in parse_qsl(query, keep_blank_values=True):
        if key == "tenant":
            return unquote(value).replace("%2D", "-").replace("%2d", "-")
    return None


def _candidate_slug(request: Request) -> str | None:
    """Find a tenant slug on the request, in priority order:

    1. ``?tenant=<slug>`` query string.
    2. ``X-Corlinman-Tenant`` header.
    3. Matched ``{tenant}`` path parameter.

    Returns ``None`` when none of the three sources carries a value.
    """

    raw_query = request.url.query
    q = extract_tenant_query(raw_query) if raw_query else None
    if q:
        return q

    hdr = request.headers.get(TENANT_HEADER_NAME)
    if hdr:
        return hdr.strip()

    path_value = request.path_params.get(TENANT_PATH_PARAM)
    if isinstance(path_value, str) and path_value:
        return path_value

    return None


def _resolve_state(request: Request) -> TenantScopeState | None:
    state = getattr(request.app.state, "tenant_scope", None)
    if isinstance(state, TenantScopeState):
        return state
    return None


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class TenantScopeMiddleware(BaseHTTPMiddleware):
    """Resolve the inbound request's tenant and stash it on
    ``request.state.tenant``.

    See module docs for the policy. On success the inner handler sees
    ``request.state.tenant`` populated; on policy failure the
    middleware short-circuits with 400 / 403 and the handler never runs.
    """

    def __init__(
        self,
        app: ASGIApp,
        state: TenantScopeState | None = None,
    ) -> None:
        super().__init__(app)
        self._state = state or TenantScopeState.disabled()

    async def dispatch(
        self,
        request: Request,
        call_next: Any,
    ) -> Response:
        state = _resolve_state(request) or self._state

        # Skip resolution when something upstream (e.g. ApiKeyAuthMiddleware)
        # already pinned a tenant; that upstream value is the source of
        # truth — re-resolving here could surface a 403 for a path the
        # caller never opted into.
        existing = getattr(request.state, "tenant", None)
        if isinstance(existing, TenantId):
            return await call_next(request)

        if not state.enabled:
            request.state.tenant = state.fallback
            return await call_next(request)

        raw = _candidate_slug(request)
        if raw is None or raw == "":
            request.state.tenant = state.fallback
            return await call_next(request)

        try:
            tenant = TenantId.new(raw)
        except TenantIdError as exc:
            return JSONResponse(
                {
                    "error": "invalid_tenant_slug",
                    "slug": raw,
                    "reason": str(exc),
                },
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        if tenant not in state.allowed:
            return JSONResponse(
                {
                    "error": "tenant_not_allowed",
                    "slug": tenant.as_str(),
                },
                status_code=status.HTTP_403_FORBIDDEN,
            )

        request.state.tenant = tenant
        return await call_next(request)


def install_tenant_scope_middleware(
    app: Any,
    *,
    enabled: bool = False,
    allowed: frozenset[TenantId] | set[TenantId] | None = None,
    fallback: TenantId | None = None,
) -> TenantScopeState:
    """Attach :class:`TenantScopeMiddleware` to ``app``.

    Returns the :class:`TenantScopeState` so the caller can swap
    ``allowed`` / ``fallback`` later (config reload). The same instance
    is published on ``app.state.tenant_scope``.
    """

    state = TenantScopeState(
        enabled=enabled,
        allowed=frozenset(allowed or ()),
        fallback=fallback or default_tenant(),
    )
    app.state.tenant_scope = state
    app.add_middleware(TenantScopeMiddleware, state=state)
    return state


# ---------------------------------------------------------------------------
# FastAPI ``Depends`` factory
# ---------------------------------------------------------------------------


def require_tenant() -> Any:
    """Per-route extractor returning the resolved :class:`TenantId`.

    Usage::

        @router.get("/admin/things")
        async def handler(tenant: TenantId = Depends(require_tenant())):
            ...

    Returns whatever the middleware stashed on
    ``request.state.tenant``. Raises HTTP 500 with a clear wiring-bug
    hint when the middleware wasn't mounted (matches the Rust
    extractor's ``tenant_extension_missing`` envelope).
    """

    def dependency(request: Request) -> TenantId:
        tenant = getattr(request.state, "tenant", None)
        if isinstance(tenant, TenantId):
            return tenant
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "tenant_extension_missing",
                "hint": "tenant_scope middleware was not mounted before this handler",
            },
        )

    return Depends(dependency)


__all__ = [
    "TENANT_HEADER_NAME",
    "TENANT_PATH_PARAM",
    "TenantScopeMiddleware",
    "TenantScopeState",
    "extract_tenant_query",
    "install_tenant_scope_middleware",
    "require_tenant",
]
