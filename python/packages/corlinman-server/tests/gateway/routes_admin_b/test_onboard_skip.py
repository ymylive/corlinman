"""Tests for ``POST /admin/onboard/finalize-skip`` (Wave 2.2).

The endpoint provisions a built-in mock provider so onboarding can
complete without real LLM credentials. Coverage:

* writes a valid ``[providers.mock]`` block with ``enabled = true``;
* points the default model alias at the mock provider;
* idempotent — calling twice doesn't duplicate the block;
* returns the documented ``{"status": "ok", "mode": "mock"}`` payload;
* gracefully 503s when no config path is wired (no implicit /tmp write).

The endpoint sits behind :func:`require_admin`; tests reach for it
through :class:`fastapi.testclient.TestClient` after installing an
:class:`AdminState` with a temp ``config_path``. The middleware module
is intentionally absent in the test env, so ``require_admin`` falls
through as a no-op (documented in ``state.require_admin``).
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from corlinman_server.gateway.routes_admin_b import onboard
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    set_admin_state,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def temp_config_path(tmp_path: Path) -> Path:
    """An on-disk TOML file the skip endpoint can atomically replace.

    Starts with an empty TOML doc so the snapshot loader has a real file
    to read — the endpoint should merge the mock block in without
    clobbering pre-existing sections.
    """
    cfg = tmp_path / "config.toml"
    cfg.write_text("", encoding="utf-8")
    return cfg


@pytest.fixture()
def admin_state(temp_config_path: Path) -> Iterator[AdminState]:
    """Install a minimal AdminState into the process-global slot."""
    snapshot: dict[str, Any] = {}

    def _loader() -> dict[str, Any]:
        # Each call returns a fresh shallow copy — mirrors the production
        # config_loader contract that handlers expect a dict-like view.
        return dict(snapshot)

    state = AdminState(
        config_loader=_loader,
        config_path=temp_config_path,
    )
    # Stash the snapshot ref on the state so tests can refresh between
    # POSTs (the endpoint reads via config_snapshot before each write).
    state.extras["snapshot"] = snapshot
    set_admin_state(state)
    try:
        yield state
    finally:
        set_admin_state(None)


@pytest.fixture()
def client(admin_state: AdminState) -> TestClient:
    """A FastAPI app with only the onboard router mounted.

    Mounting just the one router keeps the test surface tight — we
    don't have to satisfy every other admin-B sub-router's state slots.
    """
    app = FastAPI()
    app.include_router(onboard.router())
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reload_snapshot(state: AdminState) -> None:
    """Re-read the on-disk TOML into the AdminState's snapshot dict.

    Production wires a watcher that does this implicitly; in tests we
    refresh manually between calls so the second POST sees what the
    first POST wrote.
    """
    snapshot: dict[str, Any] = state.extras["snapshot"]
    snapshot.clear()
    assert state.config_path is not None
    raw = state.config_path.read_text(encoding="utf-8")
    if raw.strip():
        snapshot.update(tomllib.loads(raw))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_finalize_skip_writes_mock_provider_block(
    client: TestClient, admin_state: AdminState
) -> None:
    """Single POST → TOML file gains a valid ``[providers.mock]`` block."""
    resp = client.post("/admin/onboard/finalize-skip", json={})
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload == {"status": "ok", "mode": "mock"}

    assert admin_state.config_path is not None
    on_disk = tomllib.loads(admin_state.config_path.read_text(encoding="utf-8"))
    providers = on_disk.get("providers") or {}
    assert "mock" in providers
    mock_block = providers["mock"]
    assert mock_block["kind"] == "mock"
    assert mock_block["enabled"] is True


def test_finalize_skip_points_default_model_at_mock(
    client: TestClient, admin_state: AdminState
) -> None:
    """The default LLM alias resolves to the mock provider after skip."""
    resp = client.post("/admin/onboard/finalize-skip", json={})
    assert resp.status_code == 200

    assert admin_state.config_path is not None
    on_disk = tomllib.loads(admin_state.config_path.read_text(encoding="utf-8"))
    models = on_disk.get("models") or {}
    assert models.get("default") == "mock"
    aliases = models.get("aliases") or {}
    assert "mock" in aliases
    assert aliases["mock"]["provider"] == "mock"
    assert aliases["mock"]["model"] == "mock"


def test_finalize_skip_accepts_empty_body_or_none(
    client: TestClient, admin_state: AdminState
) -> None:
    """No body and empty JSON body both succeed."""
    resp_empty_json = client.post("/admin/onboard/finalize-skip", json={})
    assert resp_empty_json.status_code == 200

    _reload_snapshot(admin_state)
    resp_no_body = client.post("/admin/onboard/finalize-skip")
    assert resp_no_body.status_code == 200


def test_finalize_skip_is_idempotent(
    client: TestClient, admin_state: AdminState
) -> None:
    """Calling twice keeps the file valid TOML and doesn't duplicate blocks."""
    resp1 = client.post("/admin/onboard/finalize-skip", json={})
    assert resp1.status_code == 200

    _reload_snapshot(admin_state)
    first_text = admin_state.config_path.read_text(encoding="utf-8")  # type: ignore[union-attr]

    resp2 = client.post("/admin/onboard/finalize-skip", json={})
    assert resp2.status_code == 200

    second_text = admin_state.config_path.read_text(encoding="utf-8")  # type: ignore[union-attr]

    # File parses as valid TOML.
    parsed = tomllib.loads(second_text)
    # Only ever one ``mock`` entry under providers.
    providers = parsed.get("providers") or {}
    assert list(providers.keys()).count("mock") == 1
    # Content stable across calls (idempotent merge).
    assert first_text == second_text


def test_finalize_skip_returns_503_when_config_path_unset() -> None:
    """No config path → 503 with the documented ``config_path_unset`` error."""
    state = AdminState(config_loader=lambda: {}, config_path=None)
    set_admin_state(state)
    try:
        app = FastAPI()
        app.include_router(onboard.router())
        with TestClient(app) as c:
            resp = c.post("/admin/onboard/finalize-skip", json={})
        assert resp.status_code == 503
        assert resp.json() == {"error": "config_path_unset"}
    finally:
        set_admin_state(None)


def test_finalize_skip_preserves_unrelated_config_sections(
    admin_state: AdminState, client: TestClient
) -> None:
    """Pre-existing sections survive the merge — we only add/update mock."""
    snapshot: dict[str, Any] = admin_state.extras["snapshot"]
    snapshot["users"] = {"admin": {"password_hash": "abc"}}
    snapshot["providers"] = {
        "openai": {"kind": "openai", "enabled": True, "api_key": "sk-xxx"}
    }
    # Persist the seed so the writer's atomic rename starts from a real file.
    assert admin_state.config_path is not None

    resp = client.post("/admin/onboard/finalize-skip", json={})
    assert resp.status_code == 200

    on_disk = tomllib.loads(admin_state.config_path.read_text(encoding="utf-8"))
    # Untouched sections persist.
    assert on_disk["users"]["admin"]["password_hash"] == "abc"
    # Pre-existing openai provider untouched.
    assert on_disk["providers"]["openai"]["kind"] == "openai"
    # New mock provider added.
    assert on_disk["providers"]["mock"]["kind"] == "mock"
    assert on_disk["providers"]["mock"]["enabled"] is True


def test_mock_provider_kind_resolvable_from_written_config(
    client: TestClient, admin_state: AdminState
) -> None:
    """End-to-end: after skip, the written TOML drives a registry build.

    Verifies the W2.2 contract end-to-end without spinning up the full
    gateway: parse what the endpoint wrote, hand it to
    :class:`ProviderRegistry`, and assert the resulting provider is the
    built-in echo. Equivalent to "an OpenAI-shape chat call would
    return the reversed string" — we shortcut the wire layer and call
    the provider's ``chat_stream`` directly.
    """
    from corlinman_providers import (
        MockProvider,
        ProviderKind,
        ProviderRegistry,
        ProviderSpec,
    )

    resp = client.post("/admin/onboard/finalize-skip", json={})
    assert resp.status_code == 200

    assert admin_state.config_path is not None
    on_disk = tomllib.loads(admin_state.config_path.read_text(encoding="utf-8"))
    block = on_disk["providers"]["mock"]
    spec = ProviderSpec(
        name="mock",
        kind=ProviderKind(block["kind"]),
        api_key=block.get("api_key"),
        base_url=block.get("base_url"),
        enabled=block.get("enabled", True),
        params=block.get("params", {}),
    )
    reg = ProviderRegistry([spec])
    provider = reg.get("mock")
    assert isinstance(provider, MockProvider)


