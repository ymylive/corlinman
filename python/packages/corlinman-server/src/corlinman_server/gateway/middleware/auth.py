"""API-key (Bearer) auth middleware for ``/v1/*``.

Python port of ``rust/crates/corlinman-gateway/src/middleware/auth.rs``.
The Rust file is a TODO stub; this implementation tracks the
ultimately-intended contract documented across the gateway:

* Read ``Authorization: Bearer <token>`` (or, as a curl-friendly fallback,
  ``X-API-Key: <token>`` — same precedence as the rest of the codebase).
* Verify the cleartext against
  :meth:`corlinman_server.tenancy.AdminDb.verify_api_key` (sha256 lookup
  against ``tenant_api_keys`` with ``revoked_at_ms IS NULL``).
* On a hit, stash the matching :class:`~corlinman_server.tenancy.ApiKeyRow`
  on ``request.state.api_key`` and the resolved :class:`TenantId` on
  ``request.state.tenant`` so downstream handlers (and the
  ``tenant_scope`` middleware) can read it without a second DB hit.
* On a miss / missing header, short-circuit with HTTP 401 in the same
  envelope shape the Rust admin_auth path uses (``{"error":
  "unauthorized", "reason": "..."}``).

The middleware is **path-scoped**: only requests whose path starts with
one of the configured prefixes (default ``["/v1/"]``) are gated. Public
routes (``/healthz``, ``/metrics``, ``/admin/*`` — admin_auth gates that
prefix) pass through untouched. The path filter avoids accidentally
breaking the unauthenticated bootstrap surface while still failing
closed on the protected one.

Also exposes :func:`require_api_key` — a FastAPI ``Depends`` factory
sibling for handlers that prefer per-route gating over the
middleware-wide path filter. The two paths share
:func:`_verify_token_against_admin_db` so behaviour stays consistent
whichever entry point a route picks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog
from fastapi import Depends, HTTPException, Request, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from corlinman_server.tenancy import AdminDb, ApiKeyRow, TenantId

logger = structlog.get_logger(__name__)


#: Path prefixes the middleware gates by default. Mirrors the Rust
#: gateway's ``/v1/*`` mount point. Public-by-design endpoints (metrics,
#: health, the admin surface, the OpenAPI docs) are deliberately absent.
DEFAULT_PROTECTED_PREFIXES: tuple[str, ...] = ("/v1/",)


@dataclass
class ApiKeyAuthState:
    """Cloneable bundle of state the API-key middleware reads on every
    request. ``admin_db`` is the single source of truth for active keys;
    ``protected_prefixes`` controls which paths require a token.

    Both fields are mutable so an operator can rotate the AdminDb
    handle (rare) or extend the protected prefix list (more common, e.g.
    adding ``/mcp/`` once the MCP surface goes private) without
    re-installing the middleware.
    """

    admin_db: AdminDb | None = None
    protected_prefixes: tuple[str, ...] = DEFAULT_PROTECTED_PREFIXES


# ---------------------------------------------------------------------------
# Helpers — shared by the middleware and the Depends factory.
# ---------------------------------------------------------------------------


def extract_bearer_token(request: Request) -> str | None:
    """Pull the bearer token out of ``Authorization`` / ``X-API-Key``.

    Order: ``Authorization: Bearer <token>`` first (mirrors the Rust
    precedence + the rest of the Python codebase), then ``X-API-Key``
    as a curl / SDK fallback. Returns ``None`` if neither header carries
    a usable token.
    """

    auth = request.headers.get("authorization")
    if auth is not None:
        # Case-insensitive prefix match — RFC 7235 says the scheme is
        # case-insensitive, and clients (curl, fetch) routinely send
        # ``bearer ...`` lowercased.
        if auth[:7].lower() == "bearer ":
            token = auth[7:].strip()
            if token:
                return token

    api_key = request.headers.get("x-api-key")
    if api_key is not None:
        token = api_key.strip()
        if token:
            return token

    return None


def _unauthorized(reason: str) -> JSONResponse:
    """401 response in the shape the rest of the gateway uses."""

    return JSONResponse(
        {"error": "unauthorized", "reason": reason},
        status_code=status.HTTP_401_UNAUTHORIZED,
        headers={"WWW-Authenticate": 'Bearer realm="corlinman"'},
    )


async def _verify_token_against_admin_db(
    admin_db: AdminDb, token: str
) -> ApiKeyRow | None:
    """Thin wrapper so the middleware + Depends path share one code path.

    Returns the matched :class:`ApiKeyRow` on success or ``None`` on miss
    / revoked / unknown. DB errors propagate — boot wiring should catch
    them at install time, not on every request.
    """

    return await admin_db.verify_api_key(token)


def _resolve_state(request: Request) -> ApiKeyAuthState | None:
    """Pull the auth state off ``app.state``. Returns ``None`` if the
    middleware was installed without an explicit state and boot never
    populated ``app.state.api_key_auth`` either — in that case the
    middleware fails closed (401)."""

    state = getattr(request.app.state, "api_key_auth", None)
    if isinstance(state, ApiKeyAuthState):
        return state
    return None


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class ApiKeyAuthMiddleware(BaseHTTPMiddleware):
    """Gate ``/v1/*`` (configurable) behind a tenant API key.

    Construction takes an explicit :class:`ApiKeyAuthState`. The state
    is also published on ``app.state.api_key_auth`` so the
    :func:`require_api_key` :class:`Depends` factory (and routes that
    want to peek without re-validating) can pick it up.
    """

    def __init__(
        self,
        app: ASGIApp,
        state: ApiKeyAuthState | None = None,
    ) -> None:
        super().__init__(app)
        self._state = state or ApiKeyAuthState()

    async def dispatch(
        self,
        request: Request,
        call_next: Any,
    ) -> Response:
        # Re-resolve so boot can rebind the state after install.
        state = _resolve_state(request) or self._state

        if not _path_is_protected(request.url.path, state.protected_prefixes):
            return await call_next(request)

        if state.admin_db is None:
            # Fail closed: protected route but nothing to verify against.
            logger.warning(
                "api_key_auth.no_admin_db",
                path=request.url.path,
            )
            return _unauthorized("admin_db_not_configured")

        token = extract_bearer_token(request)
        if token is None:
            return _unauthorized("missing_authorization")

        try:
            row = await _verify_token_against_admin_db(state.admin_db, token)
        except Exception as exc:  # noqa: BLE001 — surface as 401, log details
            logger.warning(
                "api_key_auth.verify_failed",
                path=request.url.path,
                error=str(exc),
            )
            return _unauthorized("verify_failed")

        if row is None:
            return _unauthorized("invalid_token")

        # Stash on request.state so handlers + tenant_scope can read.
        request.state.api_key = row
        request.state.tenant = row.tenant_id

        return await call_next(request)


def _path_is_protected(path: str, prefixes: tuple[str, ...]) -> bool:
    """Whether ``path`` falls under one of the gated prefixes."""

    return any(path.startswith(p) for p in prefixes)


def install_api_key_middleware(
    app: Any,
    *,
    admin_db: AdminDb | None = None,
    protected_prefixes: tuple[str, ...] = DEFAULT_PROTECTED_PREFIXES,
) -> ApiKeyAuthState:
    """Attach :class:`ApiKeyAuthMiddleware` to ``app``.

    Returns the :class:`ApiKeyAuthState` instance so the caller can
    rebind ``admin_db`` later (e.g. after lazy tenancy init in boot).
    The same instance is also published on ``app.state.api_key_auth``.
    """

    state = ApiKeyAuthState(admin_db=admin_db, protected_prefixes=protected_prefixes)
    app.state.api_key_auth = state
    app.add_middleware(ApiKeyAuthMiddleware, state=state)
    return state


# ---------------------------------------------------------------------------
# FastAPI ``Depends`` factory
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthenticatedApiKey:
    """The successful result of :func:`require_api_key`.

    Carries the matched :class:`ApiKeyRow` plus its resolved
    :class:`TenantId` so handlers don't have to import the tenancy
    module to reach into the row.
    """

    api_key: ApiKeyRow
    tenant: TenantId = field(init=False)

    def __post_init__(self) -> None:
        # ApiKeyRow already holds the TenantId; expose it on the wrapper
        # so handlers can write ``auth.tenant`` rather than
        # ``auth.api_key.tenant_id``.
        object.__setattr__(self, "tenant", self.api_key.tenant_id)


def require_api_key() -> Any:
    """Return a FastAPI dependency that validates the request's bearer
    token and resolves it to an :class:`AuthenticatedApiKey`.

    Usage::

        @router.get("/v1/something")
        async def handler(auth: AuthenticatedApiKey = Depends(require_api_key())):
            ...

    Raises :class:`HTTPException` 401 with the same envelope shape the
    middleware uses. Handlers that already sit behind
    :class:`ApiKeyAuthMiddleware` can skip this — ``request.state.api_key``
    is already populated.
    """

    async def dependency(request: Request) -> AuthenticatedApiKey:
        # Reuse a row stashed by the middleware if present — avoids a
        # second DB round-trip for routes that have both gates wired.
        existing = getattr(request.state, "api_key", None)
        if isinstance(existing, ApiKeyRow):
            return AuthenticatedApiKey(api_key=existing)

        state = _resolve_state(request)
        if state is None or state.admin_db is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "error": "unauthorized",
                    "reason": "admin_db_not_configured",
                },
                headers={"WWW-Authenticate": 'Bearer realm="corlinman"'},
            )

        token = extract_bearer_token(request)
        if token is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "unauthorized", "reason": "missing_authorization"},
                headers={"WWW-Authenticate": 'Bearer realm="corlinman"'},
            )

        try:
            row = await _verify_token_against_admin_db(state.admin_db, token)
        except Exception as exc:  # noqa: BLE001
            logger.warning("api_key_auth.depends.verify_failed", error=str(exc))
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "unauthorized", "reason": "verify_failed"},
                headers={"WWW-Authenticate": 'Bearer realm="corlinman"'},
            ) from exc

        if row is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "unauthorized", "reason": "invalid_token"},
                headers={"WWW-Authenticate": 'Bearer realm="corlinman"'},
            )

        request.state.api_key = row
        request.state.tenant = row.tenant_id
        return AuthenticatedApiKey(api_key=row)

    return Depends(dependency)


__all__ = [
    "DEFAULT_PROTECTED_PREFIXES",
    "ApiKeyAuthMiddleware",
    "ApiKeyAuthState",
    "AuthenticatedApiKey",
    "extract_bearer_token",
    "install_api_key_middleware",
    "require_api_key",
]
