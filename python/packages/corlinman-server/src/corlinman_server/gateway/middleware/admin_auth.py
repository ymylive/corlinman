"""Auth guard for ``/admin/*``.

Python port of ``rust/crates/corlinman-gateway/src/middleware/admin_auth.rs``.

Two credentials get a request past the guard, checked in order:

1. ``Cookie: corlinman_session=<token>`` validated against
   :class:`~corlinman_server.gateway.middleware.admin_session.AdminSessionStore`
   — the normal UI path after ``/admin/login``.
2. ``Authorization: Basic base64(user:pass)`` verified via argon2id
   against an :class:`~corlinman_server.tenancy.AdminRow` looked up
   by ``(tenant_id, username)`` in :class:`~corlinman_server.tenancy.AdminDb`.
   This is the fallback that keeps curl / CI / the initial login request
   working when no cookie is yet established.

Both paths short-circuit the other: a cookie hit skips Basic entirely,
and a missing / expired cookie falls through to Basic instead of 401.
This mirrors the Rust impl byte-for-byte so the existing UI contract
holds.

On success the middleware stashes:

* ``request.state.admin_user`` — the username (str).
* ``request.state.admin_tenant`` — the :class:`TenantId` the credential
  is scoped to (defaults to ``TenantId.legacy_default()`` for Basic auth
  when no tenant is encoded in the request).
* ``request.state.admin_session`` — the resolved
  :class:`~corlinman_server.gateway.middleware.admin_session.AdminSession`
  when authentication came via cookie (``None`` on Basic-auth hits).

These keys are also what :func:`require_admin` (the ``Depends`` factory
sibling) returns, packaged into an :class:`AdminPrincipal`.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

import structlog
from fastapi import Depends, HTTPException, Request, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from corlinman_server.gateway.middleware.admin_session import (
    AdminSession,
    AdminSessionStore,
)
from corlinman_server.tenancy import AdminDb, TenantId, default_tenant

logger = structlog.get_logger(__name__)


#: Cookie name carrying the opaque session token issued by ``/admin/login``.
#: Exported so the login/logout handlers can write exactly the same name.
SESSION_COOKIE_NAME: str = "corlinman_session"


#: Path prefixes the middleware gates. Mirrors the Rust gateway's
#: ``/admin/*`` mount point. ``/admin/login`` is intentionally included
#: in the gated set so the login handler can also be reached by a
#: pre-existing cookie — the Rust router mounts ``/admin/login``
#: *outside* this layer; the Python port lets a route-level
#: ``Depends`` opt out per-endpoint instead (e.g. login itself).
DEFAULT_ADMIN_PREFIXES: tuple[str, ...] = ("/admin/",)


# ---------------------------------------------------------------------------
# Cloneable state
# ---------------------------------------------------------------------------


@dataclass
class AdminAuthState:
    """Bundle of handles the admin auth middleware reads on every request.

    ``admin_db`` looks up the :class:`AdminRow` and its argon2id hash;
    ``session_store`` is the in-memory cookie validator. Either can be
    ``None`` for tests / partial wiring — a missing ``admin_db`` fails
    closed (401 ``admin_not_configured``); a missing ``session_store``
    means cookies are never consulted and every request must present
    Basic auth.

    ``default_tenant_id`` is the tenant the middleware scopes Basic-auth
    lookups to when the request doesn't otherwise carry tenant context.
    Mirrors the Rust crate's reliance on ``config.admin.username`` /
    ``password_hash`` which are implicitly scoped to the legacy default.
    """

    admin_db: AdminDb | None = None
    session_store: AdminSessionStore | None = None
    default_tenant_id: TenantId = default_tenant()
    protected_prefixes: tuple[str, ...] = DEFAULT_ADMIN_PREFIXES


# ---------------------------------------------------------------------------
# Header / cookie parsing — pure helpers, exported for tests.
# ---------------------------------------------------------------------------


def parse_basic(header_value: str) -> tuple[str, str] | None:
    """Parse ``Authorization: Basic <base64>`` → ``(user, pass)``.

    Returns ``None`` for any malformed or non-Basic header. Byte-for-byte
    match with the Rust ``parse_basic`` helper.
    """

    if not header_value.lower().startswith("basic "):
        return None
    rest = header_value[6:].strip()
    try:
        decoded = base64.b64decode(rest, validate=True)
    except (ValueError, base64.binascii.Error):
        return None
    try:
        s = decoded.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if ":" not in s:
        return None
    user, _, password = s.partition(":")
    return user, password


def extract_cookie(header_value: str, name: str) -> str | None:
    """Pull a named cookie value out of a ``Cookie:`` header.

    Hand-rolled scan instead of pulling a cookie parser dep — matches
    the Rust ``extract_cookie`` helper. Returns ``None`` if the header
    is absent or the cookie isn't present.
    """

    for part in header_value.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        k, _, v = part.partition("=")
        if k.strip() == name:
            return v.strip()
    return None


def argon2_verify(password: str, stored_hash: str) -> bool:
    """Verify ``password`` against an argon2id PHC-encoded hash.

    Any parse / verify failure yields ``False`` — we never distinguish
    "wrong password" from "malformed stored hash" so the response
    doesn't leak hash shape. Mirrors the Rust ``argon2_verify`` helper.
    """

    try:
        from argon2 import PasswordHasher
        from argon2.exceptions import (
            InvalidHashError,
            VerificationError,
            VerifyMismatchError,
        )
    except ImportError:
        logger.error("admin_auth.argon2_missing")
        return False

    hasher = PasswordHasher()
    try:
        return hasher.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    except Exception:  # noqa: BLE001 — defensive: never leak hash shape
        logger.warning("admin_auth.argon2_verify_unexpected")
        return False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _unauthorized(reason: str) -> JSONResponse:
    """401 response with the canonical envelope shape."""

    return JSONResponse(
        {"error": "unauthorized", "reason": reason},
        status_code=status.HTTP_401_UNAUTHORIZED,
        headers={"WWW-Authenticate": 'Basic realm="corlinman-admin"'},
    )


def _resolve_state(request: Request) -> AdminAuthState | None:
    state = getattr(request.app.state, "admin_auth", None)
    if isinstance(state, AdminAuthState):
        return state
    return None


def _path_is_protected(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path.startswith(p) for p in prefixes)


@dataclass(frozen=True)
class _AuthResult:
    """Outcome of running the auth chain. Either ``ok`` is populated
    with the :class:`AdminPrincipal`, or ``reason`` carries the 401
    short-circuit code."""

    ok: "AdminPrincipal | None" = None
    reason: str | None = None


async def _run_auth_chain(
    request: Request, state: AdminAuthState
) -> _AuthResult:
    """Cookie-then-Basic auth chain shared by the middleware and the
    ``Depends`` sibling. Returns either a populated principal or the
    canonical 401 reason."""

    if state.admin_db is None:
        return _AuthResult(reason="admin_not_configured")

    # 1) Cookie path — only consulted when a session store is wired.
    if state.session_store is not None:
        cookie_header = request.headers.get("cookie")
        if cookie_header is not None:
            token = extract_cookie(cookie_header, SESSION_COOKIE_NAME)
            if token is not None:
                session = state.session_store.validate(token)
                if session is not None:
                    principal = AdminPrincipal(
                        user=session.user,
                        tenant=state.default_tenant_id,
                        session=session,
                    )
                    return _AuthResult(ok=principal)
                # Cookie present but invalid/expired: fall through to
                # Basic rather than 401 so curl / CI still works.

    # 2) Basic auth fallback.
    auth_header = request.headers.get("authorization")
    if auth_header is None:
        return _AuthResult(reason="missing_authorization")

    parsed = parse_basic(auth_header)
    if parsed is None:
        return _AuthResult(reason="malformed_authorization")

    user, password = parsed

    # Look up the admin row. The Rust port reads
    # ``config.admin.username`` + ``password_hash`` directly; the Python
    # port has moved that data into ``tenant_admins`` on the AdminDb so
    # we route through it. The lookup is scoped to the default tenant
    # unless something further up has annotated otherwise.
    admin_row = None
    try:
        admin_row = await _lookup_admin(state.admin_db, state.default_tenant_id, user)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "admin_auth.lookup_failed",
            tenant=state.default_tenant_id.as_str(),
            user=user,
            error=str(exc),
        )
        return _AuthResult(reason="invalid_credentials")

    if admin_row is None:
        return _AuthResult(reason="invalid_credentials")

    if not argon2_verify(password, admin_row.password_hash):
        return _AuthResult(reason="invalid_credentials")

    return _AuthResult(
        ok=AdminPrincipal(
            user=user,
            tenant=state.default_tenant_id,
            session=None,
        )
    )


async def _lookup_admin(
    admin_db: AdminDb, tenant: TenantId, username: str
) -> Any | None:
    """Look up ``(tenant, username)`` in ``tenant_admins``. Tries
    :meth:`AdminDb.get_admin` first (the preferred CRUD path); falls
    back to scanning :meth:`AdminDb.list_admins` if the former is
    missing on older revisions.
    """

    getter = getattr(admin_db, "get_admin", None)
    if callable(getter):
        return await getter(tenant, username)
    lister = getattr(admin_db, "list_admins", None)
    if callable(lister):
        for row in await lister(tenant):
            if getattr(row, "username", None) == username:
                return row
    return None


# ---------------------------------------------------------------------------
# Principal — the thing handlers see.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdminPrincipal:
    """Resolved admin identity. Stashed on ``request.state.admin_*`` and
    returned by :func:`require_admin`."""

    user: str
    tenant: TenantId
    session: AdminSession | None


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class AdminAuthMiddleware(BaseHTTPMiddleware):
    """Cookie + HTTP Basic guard for ``/admin/*``.

    Construction takes an explicit :class:`AdminAuthState`; the same
    state is published on ``app.state.admin_auth`` so :func:`require_admin`
    and admin handlers can read it.
    """

    def __init__(
        self,
        app: ASGIApp,
        state: AdminAuthState | None = None,
    ) -> None:
        super().__init__(app)
        self._state = state or AdminAuthState()

    async def dispatch(
        self,
        request: Request,
        call_next: Any,
    ) -> Response:
        state = _resolve_state(request) or self._state

        if not _path_is_protected(request.url.path, state.protected_prefixes):
            return await call_next(request)

        result = await _run_auth_chain(request, state)
        if result.ok is None:
            assert result.reason is not None
            return _unauthorized(result.reason)

        principal = result.ok
        request.state.admin_user = principal.user
        request.state.admin_tenant = principal.tenant
        request.state.admin_session = principal.session

        return await call_next(request)


def install_admin_auth_middleware(
    app: Any,
    *,
    admin_db: AdminDb | None = None,
    session_store: AdminSessionStore | None = None,
    default_tenant_id: TenantId | None = None,
    protected_prefixes: tuple[str, ...] = DEFAULT_ADMIN_PREFIXES,
) -> AdminAuthState:
    """Attach :class:`AdminAuthMiddleware` to ``app``.

    Returns the :class:`AdminAuthState` instance so the caller can rebind
    handles after install (e.g. when ``admin_db`` is opened lazily). The
    same instance is published on ``app.state.admin_auth``.
    """

    state = AdminAuthState(
        admin_db=admin_db,
        session_store=session_store,
        default_tenant_id=default_tenant_id or default_tenant(),
        protected_prefixes=protected_prefixes,
    )
    app.state.admin_auth = state
    app.add_middleware(AdminAuthMiddleware, state=state)
    return state


# ---------------------------------------------------------------------------
# FastAPI ``Depends`` factory
# ---------------------------------------------------------------------------


def require_admin() -> Any:
    """Per-route guard returning the authenticated :class:`AdminPrincipal`.

    Usage::

        @router.get("/admin/something")
        async def handler(admin: AdminPrincipal = Depends(require_admin())):
            ...

    Reuses any principal the middleware already validated on this
    request (avoids a second DB round-trip / argon2 call). Raises
    :class:`HTTPException` 401 in the canonical envelope on failure.
    """

    async def dependency(request: Request) -> AdminPrincipal:
        # Reuse middleware-populated state when available.
        cached_user = getattr(request.state, "admin_user", None)
        if isinstance(cached_user, str):
            return AdminPrincipal(
                user=cached_user,
                tenant=getattr(request.state, "admin_tenant", default_tenant()),
                session=getattr(request.state, "admin_session", None),
            )

        state = _resolve_state(request)
        if state is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "unauthorized", "reason": "admin_not_configured"},
                headers={"WWW-Authenticate": 'Basic realm="corlinman-admin"'},
            )

        result = await _run_auth_chain(request, state)
        if result.ok is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "unauthorized", "reason": result.reason or "unknown"},
                headers={"WWW-Authenticate": 'Basic realm="corlinman-admin"'},
            )

        principal = result.ok
        request.state.admin_user = principal.user
        request.state.admin_tenant = principal.tenant
        request.state.admin_session = principal.session
        return principal

    return Depends(dependency)


__all__ = [
    "DEFAULT_ADMIN_PREFIXES",
    "SESSION_COOKIE_NAME",
    "AdminAuthMiddleware",
    "AdminAuthState",
    "AdminPrincipal",
    "argon2_verify",
    "extract_cookie",
    "install_admin_auth_middleware",
    "parse_basic",
    "require_admin",
]
