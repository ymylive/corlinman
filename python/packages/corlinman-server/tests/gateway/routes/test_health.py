"""Tests for ``corlinman_server.gateway.routes.health``."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from corlinman_server.gateway.routes import health


def _client(state: health.HealthState | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(health.router(state))
    return TestClient(app)


def test_stub_router_returns_ok_with_empty_checks() -> None:
    """No state wired → mirrors the Rust ``health_stub`` behaviour."""
    client = _client(None)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["checks"] == []
    assert "version" in body


def test_readyz_also_responds() -> None:
    """Both endpoints share the same body in this milestone."""
    client = _client(None)
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_all_probes_ok_returns_ok() -> None:
    async def ping_chat() -> bool:
        return True

    async def ping_db() -> bool:
        return True

    async def ping_providers() -> int:
        return 3

    state = health.HealthState(
        ping_chat_service=ping_chat,
        ping_db=ping_db,
        ping_providers=ping_providers,
        plugin_registry_diagnostics=lambda: (2, 0),
    )
    client = _client(state)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    names = {c["name"] for c in body["checks"]}
    assert names == {"chat_service", "db", "providers", "plugin_registry"}
    for c in body["checks"]:
        assert c["status"] == "ok"


def test_failed_chat_probe_flips_unhealthy() -> None:
    async def ping_chat() -> bool:
        raise RuntimeError("agent dead")

    state = health.HealthState(ping_chat_service=ping_chat)
    client = _client(state)
    resp = client.get("/healthz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unhealthy"
    chat = next(c for c in body["checks"] if c["name"] == "chat_service")
    assert chat["status"] == "fail"


def test_diagnostics_warn_returns_degraded() -> None:
    state = health.HealthState(plugin_registry_diagnostics=lambda: (5, 2))
    client = _client(state)
    resp = client.get("/healthz")
    assert resp.status_code == 200  # warn = 200 with degraded
    body = resp.json()
    assert body["status"] == "degraded"


def test_overall_status_aggregation() -> None:
    entries = [
        health.CheckEntry.ok("a"),
        health.CheckEntry.warn("b", "x"),
        health.CheckEntry.fail("c", "y"),
    ]
    assert health.overall_status(entries) == "unhealthy"
    assert (
        health.overall_status([health.CheckEntry.ok("a"), health.CheckEntry.warn("b", "x")])
        == "degraded"
    )
    assert health.overall_status([]) == "ok"
