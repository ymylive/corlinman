"""End-to-end tests for the API-key + admin auth middleware.

Exercises the path-filter, the 401 envelope, and the
``request.state.api_key`` / ``request.state.admin_user`` side effects.
A fake :class:`AdminDb` keeps the test surface tiny — the real DB is
covered by the tenancy tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from corlinman_server.gateway.middleware import (
    AdminAuthState,
    AdminPrincipal,
    ApiKeyAuthState,
    AuthenticatedApiKey,
    install_admin_auth_middleware,
    install_api_key_middleware,
    require_admin,
    require_api_key,
)
from corlinman_server.tenancy import (
    ApiKeyRow,
    TenantId,
    default_tenant,
)


# ---------------------------------------------------------------------------
# Stubs.
# ---------------------------------------------------------------------------


@dataclass
class _FakeAdminDb:
    """Implements only the methods the middleware calls."""

    valid_token: str = "secret-token"
    admin_username: str = "admin"
    admin_password_hash: str = ""

    async def verify_api_key(self, token: str) -> ApiKeyRow | None:
        if token != self.valid_token:
            return None
        return ApiKeyRow(
            key_id="key_test",
            tenant_id=default_tenant(),
            username="alice",
            scope="full",
            label=None,
            token_hash="dummy",
            created_at_ms=0,
            last_used_at_ms=None,
            revoked_at_ms=None,
        )

    async def get_admin(self, tenant: TenantId, username: str) -> Any:
        if username != self.admin_username or not self.admin_password_hash:
            return None

        class _Row:
            password_hash = self.admin_password_hash

        return _Row()


# ---------------------------------------------------------------------------
# api-key middleware
# ---------------------------------------------------------------------------


def _api_app(admin_db: _FakeAdminDb) -> FastAPI:
    app = FastAPI()
    install_api_key_middleware(app, admin_db=admin_db)  # type: ignore[arg-type]

    @app.get("/v1/ping")
    def ping(auth: AuthenticatedApiKey = require_api_key()) -> dict[str, str]:
        return {"user": auth.api_key.username, "tenant": auth.tenant.as_str()}

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"ok": "true"}

    return app


def test_api_key_missing_header_returns_401() -> None:
    client = TestClient(_api_app(_FakeAdminDb()))
    resp = client.get("/v1/ping")
    assert resp.status_code == 401
    assert resp.json()["reason"] == "missing_authorization"
    assert resp.headers.get("www-authenticate", "").lower().startswith("bearer")


def test_api_key_invalid_token_returns_401() -> None:
    client = TestClient(_api_app(_FakeAdminDb()))
    resp = client.get("/v1/ping", headers={"Authorization": "Bearer WRONG"})
    assert resp.status_code == 401
    assert resp.json()["reason"] == "invalid_token"


def test_api_key_valid_bearer_passes_through() -> None:
    client = TestClient(_api_app(_FakeAdminDb()))
    resp = client.get(
        "/v1/ping", headers={"Authorization": "Bearer secret-token"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"user": "alice", "tenant": "default"}


def test_api_key_x_api_key_header_also_accepted() -> None:
    client = TestClient(_api_app(_FakeAdminDb()))
    resp = client.get("/v1/ping", headers={"X-API-Key": "secret-token"})
    assert resp.status_code == 200


def test_api_key_public_path_passes_through() -> None:
    client = TestClient(_api_app(_FakeAdminDb()))
    resp = client.get("/healthz")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# admin auth middleware
# ---------------------------------------------------------------------------


def _admin_app(state_kwargs: dict[str, Any] | None = None) -> FastAPI:
    app = FastAPI()
    install_admin_auth_middleware(app, **(state_kwargs or {}))

    @app.get("/admin/ping")
    def ping(admin: AdminPrincipal = require_admin()) -> dict[str, str]:
        return {"user": admin.user, "tenant": admin.tenant.as_str()}

    return app


def test_admin_missing_credentials_returns_401() -> None:
    db = _FakeAdminDb()
    client = TestClient(_admin_app({"admin_db": db}))
    resp = client.get("/admin/ping")
    assert resp.status_code == 401
    assert resp.json()["reason"] == "missing_authorization"
    assert resp.headers.get("www-authenticate", "").lower().startswith("basic")


def test_admin_malformed_authorization_returns_401() -> None:
    db = _FakeAdminDb()
    client = TestClient(_admin_app({"admin_db": db}))
    resp = client.get("/admin/ping", headers={"Authorization": "Bearer xyz"})
    assert resp.status_code == 401
    assert resp.json()["reason"] == "malformed_authorization"


def test_admin_no_admin_db_returns_401() -> None:
    client = TestClient(_admin_app({}))
    resp = client.get("/admin/ping", headers={"Authorization": "Basic Zm9vOmJhcg=="})
    assert resp.status_code == 401
    assert resp.json()["reason"] == "admin_not_configured"


@pytest.mark.parametrize(
    "path,expected_status",
    [
        ("/admin/ping", 401),  # gated
        ("/something_else", 404),  # unprotected → routing handles it (no route)
    ],
)
def test_admin_path_filter_only_gates_admin_prefix(path: str, expected_status: int) -> None:
    db = _FakeAdminDb()
    client = TestClient(_admin_app({"admin_db": db}))
    resp = client.get(path)
    assert resp.status_code == expected_status
