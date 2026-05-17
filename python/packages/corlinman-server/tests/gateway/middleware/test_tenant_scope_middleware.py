"""End-to-end tests for :mod:`corlinman_server.gateway.middleware.tenant_scope`.

Mirrors the Rust ``tenant_scope.rs`` ``#[tokio::test]`` suite: disabled
mode resolves to default, enabled mode honours ``?tenant=`` and falls
back to the configured default, invalid slugs surface 400, and
disallowed slugs surface 403.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from corlinman_server.gateway.middleware import (
    TenantScopeState,
    install_tenant_scope_middleware,
    require_tenant,
)
from corlinman_server.tenancy import TenantId, default_tenant


def _build_app(state: TenantScopeState | None = None) -> FastAPI:
    app = FastAPI()
    if state is None:
        install_tenant_scope_middleware(app)
    else:
        app.state.tenant_scope = state
        from corlinman_server.gateway.middleware.tenant_scope import (
            TenantScopeMiddleware,
        )

        app.add_middleware(TenantScopeMiddleware, state=state)

    @app.get("/probe")
    def probe(tenant: TenantId = require_tenant()) -> dict[str, str]:
        return {"tenant": tenant.as_str()}

    return app


def test_disabled_resolves_every_request_to_default() -> None:
    app = _build_app(TenantScopeState.disabled())
    client = TestClient(app)
    resp = client.get("/probe?tenant=acme")
    assert resp.status_code == 200
    assert resp.json() == {"tenant": "default"}


def test_enabled_falls_back_to_configured_default_when_query_absent() -> None:
    state = TenantScopeState(
        enabled=True,
        allowed=frozenset({default_tenant(), TenantId.new("acme")}),
        fallback=default_tenant(),
    )
    client = TestClient(_build_app(state))
    resp = client.get("/probe")
    assert resp.status_code == 200
    assert resp.json() == {"tenant": "default"}


def test_enabled_resolves_allowed_tenant_query() -> None:
    state = TenantScopeState(
        enabled=True,
        allowed=frozenset({default_tenant(), TenantId.new("acme"), TenantId.new("bravo")}),
        fallback=default_tenant(),
    )
    client = TestClient(_build_app(state))
    resp = client.get("/probe?tenant=bravo")
    assert resp.status_code == 200
    assert resp.json() == {"tenant": "bravo"}


def test_enabled_rejects_invalid_slug_with_400() -> None:
    state = TenantScopeState(
        enabled=True,
        allowed=frozenset({default_tenant(), TenantId.new("acme")}),
        fallback=default_tenant(),
    )
    client = TestClient(_build_app(state))
    resp = client.get("/probe?tenant=BAD!!")
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "invalid_tenant_slug"
    assert body["slug"] == "BAD!!"


def test_enabled_rejects_disallowed_tenant_with_403() -> None:
    state = TenantScopeState(
        enabled=True,
        allowed=frozenset({default_tenant(), TenantId.new("acme")}),
        fallback=default_tenant(),
    )
    client = TestClient(_build_app(state))
    resp = client.get("/probe?tenant=bravo")
    assert resp.status_code == 403
    body = resp.json()
    assert body["error"] == "tenant_not_allowed"
    assert body["slug"] == "bravo"


def test_require_tenant_500s_when_middleware_missing() -> None:
    """Wiring bug: ``require_tenant`` without the middleware mounted
    returns the canonical 500 envelope so a refactor that drops the
    layer fails loudly (mirrors Rust's
    ``missing_extension_returns_500_explicit_wiring_bug``)."""

    app = FastAPI()

    @app.get("/probe")
    def probe(tenant: TenantId = require_tenant()) -> dict[str, str]:
        return {"tenant": tenant.as_str()}

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/probe")
    assert resp.status_code == 500
    body = resp.json()
    assert body["detail"]["error"] == "tenant_extension_missing"
