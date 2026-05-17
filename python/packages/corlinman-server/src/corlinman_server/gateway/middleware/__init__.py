"""``corlinman_server.gateway.middleware`` — FastAPI middleware ported from
``rust/crates/corlinman-gateway/src/middleware``.

Submodules:
    * :mod:`auth` — Bearer / API-key check for ``/v1/*`` (against
      :meth:`corlinman_server.tenancy.AdminDb.verify_api_key`).
    * :mod:`admin_auth` — cookie + HTTP Basic guard for ``/admin/*``.
    * :mod:`admin_session` — in-memory session store (uuid → AdminSession).
    * :mod:`approval` — facade over
      :class:`corlinman_providers.plugins.ApprovalStore` with the
      rule-matching layer.
    * :mod:`tenant_scope` — resolves ``?tenant=<slug>`` (or header / path
      param) to a :class:`TenantId` and stashes it on
      ``request.state.tenant``.
    * :mod:`trace` — HTTP request metrics + traceparent propagation.

Each module exposes both an ``install_*_middleware(app)`` helper *and* a
:func:`require_*` :class:`Depends` factory so the gateway boot can
compose them in the order the Rust crate uses (outermost first:
``trace → tenant_scope → admin_auth/auth → approval``) while
individual routes can also opt-in via dependency injection.
"""

from __future__ import annotations

from corlinman_server.gateway.middleware.admin_auth import (
    DEFAULT_ADMIN_PREFIXES,
    SESSION_COOKIE_NAME,
    AdminAuthMiddleware,
    AdminAuthState,
    AdminPrincipal,
    argon2_verify,
    extract_cookie,
    install_admin_auth_middleware,
    parse_basic,
    require_admin,
)
from corlinman_server.gateway.middleware.admin_session import (
    AdminSession,
    AdminSessionStore,
)
from corlinman_server.gateway.middleware.approval import (
    DEFAULT_PROMPT_TIMEOUT_SECONDS,
    ApprovalDecision,
    ApprovalGate,
    ApprovalMiddleware,
    ApprovalMiddlewareState,
    ApprovalMode,
    ApprovalRule,
    RuleMatch,
    RuleMatchKind,
    install_approval_middleware,
    match_rule,
    require_approval,
)
from corlinman_server.gateway.middleware.auth import (
    DEFAULT_PROTECTED_PREFIXES,
    ApiKeyAuthMiddleware,
    ApiKeyAuthState,
    AuthenticatedApiKey,
    extract_bearer_token,
    install_api_key_middleware,
    require_api_key,
)
from corlinman_server.gateway.middleware.tenant_scope import (
    TENANT_HEADER_NAME,
    TENANT_PATH_PARAM,
    TenantScopeMiddleware,
    TenantScopeState,
    extract_tenant_query,
    install_tenant_scope_middleware,
    require_tenant,
)
from corlinman_server.gateway.middleware.trace import (
    TraceMiddleware,
    install_trace_middleware,
)

__all__ = [
    # admin_auth
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
    # admin_session
    "AdminSession",
    "AdminSessionStore",
    # approval
    "DEFAULT_PROMPT_TIMEOUT_SECONDS",
    "ApprovalDecision",
    "ApprovalGate",
    "ApprovalMiddleware",
    "ApprovalMiddlewareState",
    "ApprovalMode",
    "ApprovalRule",
    "RuleMatch",
    "RuleMatchKind",
    "install_approval_middleware",
    "match_rule",
    "require_approval",
    # auth
    "DEFAULT_PROTECTED_PREFIXES",
    "ApiKeyAuthMiddleware",
    "ApiKeyAuthState",
    "AuthenticatedApiKey",
    "extract_bearer_token",
    "install_api_key_middleware",
    "require_api_key",
    # tenant_scope
    "TENANT_HEADER_NAME",
    "TENANT_PATH_PARAM",
    "TenantScopeMiddleware",
    "TenantScopeState",
    "extract_tenant_query",
    "install_tenant_scope_middleware",
    "require_tenant",
    # trace
    "TraceMiddleware",
    "install_trace_middleware",
]
