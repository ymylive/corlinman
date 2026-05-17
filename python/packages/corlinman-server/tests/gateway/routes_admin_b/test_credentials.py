"""Tests for ``/admin/credentials/*`` (Wave 2.3).

The endpoint surface manages ``[providers.<name>]`` blocks in the config
TOML — masked-by-default, paste-only writes, whitelist-gated keys. Each
test below pins one contract decision so a regression turns into a
single failing assertion with a precise diagnostic.

Coverage:

* GET on empty config returns every well-known provider with
  ``set=false`` and the conventional ``env_ref`` hints.
* PUT openai.api_key → GET reports ``set=true`` + ``preview="…last4"`` +
  the block is now ``enabled``.
* DELETE drops the field and flips ``enabled`` back to false.
* PUT to an unknown whitelisted-field is rejected with 400.
* POST enable=false leaves field data alone but turns the block off.
* Multiple writes never duplicate the block; unrelated TOML sections
  (``[admin]``, ``[models]``) survive untouched.
* PUT twice with the same value is idempotent — file contents stay
  byte-identical after the second call.

Same fixture pattern as ``test_onboard_skip.py`` — mount just the
credentials router, install an ``AdminState`` with a temp config path,
refresh the snapshot between writes the same way the production watcher
would.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from corlinman_server.gateway.routes_admin_b import credentials
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    set_admin_state,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def temp_config_path(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text("", encoding="utf-8")
    return cfg


@pytest.fixture()
def admin_state(temp_config_path: Path) -> Iterator[AdminState]:
    """A minimal AdminState with a backing TOML file the routes can write."""
    snapshot: dict[str, Any] = {}

    def _loader() -> dict[str, Any]:
        return dict(snapshot)

    state = AdminState(
        config_loader=_loader,
        config_path=temp_config_path,
    )
    state.extras["snapshot"] = snapshot
    set_admin_state(state)
    try:
        yield state
    finally:
        set_admin_state(None)


@pytest.fixture()
def client(admin_state: AdminState) -> TestClient:
    app = FastAPI()
    app.include_router(credentials.router())
    return TestClient(app)


def _reload(state: AdminState) -> None:
    """Re-read the TOML file into the snapshot — mimics the prod watcher."""
    snapshot: dict[str, Any] = state.extras["snapshot"]
    snapshot.clear()
    assert state.config_path is not None
    raw = state.config_path.read_text(encoding="utf-8")
    if raw.strip():
        snapshot.update(tomllib.loads(raw))


def _on_disk(state: AdminState) -> dict[str, Any]:
    assert state.config_path is not None
    raw = state.config_path.read_text(encoding="utf-8")
    if not raw.strip():
        return {}
    return tomllib.loads(raw)


# ---------------------------------------------------------------------------
# GET — well-known list shape
# ---------------------------------------------------------------------------


def test_get_on_empty_config_lists_well_known_providers(
    client: TestClient,
) -> None:
    """No TOML written yet → every well-known provider shows up as a stub."""
    resp = client.get("/admin/credentials")
    assert resp.status_code == 200
    payload = resp.json()

    names = [p["name"] for p in payload["providers"]]
    # Must include the documented well-known set.
    for required in ("openai", "anthropic", "openrouter", "ollama", "mock", "custom"):
        assert required in names

    openai = next(p for p in payload["providers"] if p["name"] == "openai")
    assert openai["enabled"] is False
    # All openai fields are unset and carry the conventional env-var hint.
    field_names = [f["key"] for f in openai["fields"]]
    assert field_names == ["api_key", "base_url", "org_id"]
    api_key = next(f for f in openai["fields"] if f["key"] == "api_key")
    assert api_key["set"] is False
    assert api_key["preview"] is None
    assert api_key["env_ref"] == "OPENAI_API_KEY"


def test_get_lists_mock_without_fields(client: TestClient) -> None:
    """Mock provider has no editable fields but is still listed as a stub."""
    payload = client.get("/admin/credentials").json()
    mock = next(p for p in payload["providers"] if p["name"] == "mock")
    assert mock["fields"] == []
    assert mock["enabled"] is False


# ---------------------------------------------------------------------------
# PUT — happy path
# ---------------------------------------------------------------------------


def test_put_api_key_marks_field_set_and_enables_provider(
    client: TestClient, admin_state: AdminState
) -> None:
    """First write of api_key flips set=true, primes preview, enables block."""
    resp = client.put(
        "/admin/credentials/openai/api_key",
        json={"value": "sk-test123secret"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "ok"}

    _reload(admin_state)
    payload = client.get("/admin/credentials").json()
    openai = next(p for p in payload["providers"] if p["name"] == "openai")
    assert openai["enabled"] is True
    api_key = next(f for f in openai["fields"] if f["key"] == "api_key")
    assert api_key["set"] is True
    # Preview shows the trailing 4 chars only.
    assert api_key["preview"] == "…cret"

    # The on-disk file stores the literal string, not a {value=...} dict.
    on_disk = _on_disk(admin_state)
    assert on_disk["providers"]["openai"]["api_key"] == "sk-test123secret"
    assert on_disk["providers"]["openai"]["enabled"] is True


def test_put_short_value_renders_triple_asterisk_preview(
    client: TestClient, admin_state: AdminState
) -> None:
    """Sub-5-char literals should never echo any characters back."""
    resp = client.put(
        "/admin/credentials/openai/api_key",
        json={"value": "abc"},
    )
    assert resp.status_code == 200

    _reload(admin_state)
    payload = client.get("/admin/credentials").json()
    openai = next(p for p in payload["providers"] if p["name"] == "openai")
    api_key = next(f for f in openai["fields"] if f["key"] == "api_key")
    assert api_key["set"] is True
    assert api_key["preview"] == "***"


def test_put_unknown_field_returns_400(client: TestClient) -> None:
    """Anything outside the per-provider whitelist is rejected cleanly."""
    resp = client.put(
        "/admin/credentials/openai/secret_token",
        json={"value": "nope"},
    )
    assert resp.status_code == 400
    assert resp.json() == {"error": "unknown_field"}


def test_put_idempotent_keeps_file_byte_identical(
    client: TestClient, admin_state: AdminState
) -> None:
    """Two identical PUTs must produce identical on-disk TOML."""
    body = {"value": "sk-stable-secret-value"}
    resp1 = client.put("/admin/credentials/openai/api_key", json=body)
    assert resp1.status_code == 200
    first_text = admin_state.config_path.read_text(encoding="utf-8")  # type: ignore[union-attr]

    _reload(admin_state)
    resp2 = client.put("/admin/credentials/openai/api_key", json=body)
    assert resp2.status_code == 200
    second_text = admin_state.config_path.read_text(encoding="utf-8")  # type: ignore[union-attr]

    assert first_text == second_text


# ---------------------------------------------------------------------------
# DELETE — field removal + enabled fallthrough
# ---------------------------------------------------------------------------


def test_delete_removes_field_and_disables_when_primary_gone(
    client: TestClient, admin_state: AdminState
) -> None:
    """Deleting the primary field flips enabled back to false."""
    client.put(
        "/admin/credentials/openai/api_key",
        json={"value": "sk-deletable-secret"},
    )
    _reload(admin_state)

    resp = client.delete("/admin/credentials/openai/api_key")
    assert resp.status_code == 204

    _reload(admin_state)
    payload = client.get("/admin/credentials").json()
    openai = next(p for p in payload["providers"] if p["name"] == "openai")
    api_key = next(f for f in openai["fields"] if f["key"] == "api_key")
    assert api_key["set"] is False
    assert openai["enabled"] is False

    on_disk = _on_disk(admin_state)
    # The block stub stays (so the UI keeps showing the placeholder row)
    # but the field itself is gone from the TOML.
    assert "api_key" not in on_disk["providers"]["openai"]


def test_delete_unknown_field_returns_400(client: TestClient) -> None:
    """Unknown keys are rejected on DELETE too — not a silent no-op."""
    resp = client.delete("/admin/credentials/openai/totally_made_up")
    assert resp.status_code == 400
    assert resp.json() == {"error": "unknown_field"}


def test_delete_field_when_block_absent_is_silent_204(
    client: TestClient, admin_state: AdminState
) -> None:
    """Deleting a field on a never-written block is a clean 204."""
    resp = client.delete("/admin/credentials/openai/api_key")
    assert resp.status_code == 204


# ---------------------------------------------------------------------------
# POST enable — provider-wide toggle
# ---------------------------------------------------------------------------


def test_enable_false_disables_but_leaves_field_intact(
    client: TestClient, admin_state: AdminState
) -> None:
    """Toggling enabled off must not touch the stored credential."""
    client.put(
        "/admin/credentials/openai/api_key",
        json={"value": "sk-keep-this-value"},
    )
    _reload(admin_state)

    resp = client.post(
        "/admin/credentials/openai/enable", json={"enabled": False}
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}

    _reload(admin_state)
    on_disk = _on_disk(admin_state)
    assert on_disk["providers"]["openai"]["enabled"] is False
    assert on_disk["providers"]["openai"]["api_key"] == "sk-keep-this-value"

    # And the GET surface reflects it as well.
    payload = client.get("/admin/credentials").json()
    openai = next(p for p in payload["providers"] if p["name"] == "openai")
    assert openai["enabled"] is False
    api_key = next(f for f in openai["fields"] if f["key"] == "api_key")
    assert api_key["set"] is True


def test_enable_true_on_empty_block_creates_kind_stub(
    client: TestClient, admin_state: AdminState
) -> None:
    """Enabling without any field yet leaves a valid block with a kind."""
    resp = client.post(
        "/admin/credentials/anthropic/enable", json={"enabled": True}
    )
    assert resp.status_code == 200

    on_disk = _on_disk(admin_state)
    block = on_disk["providers"]["anthropic"]
    assert block["enabled"] is True
    # Default kind for anthropic should have been seeded.
    assert block["kind"] == "anthropic"


# ---------------------------------------------------------------------------
# Cross-section preservation
# ---------------------------------------------------------------------------


def test_writes_preserve_unrelated_sections(
    client: TestClient, admin_state: AdminState
) -> None:
    """PUT/DELETE must NOT clobber [admin] / [models] / sibling providers."""
    snapshot: dict[str, Any] = admin_state.extras["snapshot"]
    snapshot["admin"] = {"username": "ops", "password_hash": "argon2id$..."}
    snapshot["models"] = {
        "default": "newapi",
        "aliases": {"newapi": {"model": "gpt-4", "provider": "newapi"}},
    }
    snapshot["providers"] = {
        "newapi": {"kind": "newapi", "enabled": True, "api_key": "existing"},
    }

    # First write: openai api_key should land alongside newapi.
    resp = client.put(
        "/admin/credentials/openai/api_key",
        json={"value": "sk-coexist-with-newapi"},
    )
    assert resp.status_code == 200

    on_disk = _on_disk(admin_state)
    assert on_disk["admin"]["username"] == "ops"
    assert on_disk["models"]["default"] == "newapi"
    assert on_disk["providers"]["newapi"]["api_key"] == "existing"
    assert on_disk["providers"]["openai"]["api_key"] == "sk-coexist-with-newapi"

    # Delete shouldn't touch siblings either.
    _reload(admin_state)
    resp = client.delete("/admin/credentials/openai/api_key")
    assert resp.status_code == 204

    on_disk = _on_disk(admin_state)
    assert on_disk["admin"]["username"] == "ops"
    assert on_disk["models"]["default"] == "newapi"
    assert on_disk["providers"]["newapi"]["api_key"] == "existing"


def test_env_ref_passthrough_from_existing_block(
    client: TestClient, admin_state: AdminState
) -> None:
    """A pre-existing ``api_key = { env = "FOO" }`` reports env_ref="FOO"."""
    snapshot: dict[str, Any] = admin_state.extras["snapshot"]
    snapshot["providers"] = {
        "openai": {
            "kind": "openai",
            "enabled": True,
            "api_key": {"env": "MY_CUSTOM_OPENAI_KEY"},
        }
    }

    payload = client.get("/admin/credentials").json()
    openai = next(p for p in payload["providers"] if p["name"] == "openai")
    api_key = next(f for f in openai["fields"] if f["key"] == "api_key")
    assert api_key["set"] is True
    # No preview leaks — env-shaped credentials are opaque to the surface.
    assert api_key["preview"] is None
    assert api_key["env_ref"] == "MY_CUSTOM_OPENAI_KEY"


def test_multiple_provider_writes_share_one_providers_table(
    client: TestClient, admin_state: AdminState
) -> None:
    """Sequential writes to different providers don't fragment the TOML."""
    client.put(
        "/admin/credentials/openai/api_key", json={"value": "sk-openai-secret"}
    )
    _reload(admin_state)
    client.put(
        "/admin/credentials/anthropic/api_key",
        json={"value": "sk-ant-secret-value"},
    )
    _reload(admin_state)
    client.put(
        "/admin/credentials/ollama/base_url",
        json={"value": "http://localhost:11434"},
    )
    _reload(admin_state)

    on_disk = _on_disk(admin_state)
    assert set(on_disk["providers"].keys()) >= {"openai", "anthropic", "ollama"}
    assert on_disk["providers"]["openai"]["api_key"] == "sk-openai-secret"
    assert on_disk["providers"]["anthropic"]["api_key"] == "sk-ant-secret-value"
    assert on_disk["providers"]["ollama"]["base_url"] == "http://localhost:11434"
    # Ollama uses base_url as primary → enabled flips on.
    assert on_disk["providers"]["ollama"]["enabled"] is True


# ---------------------------------------------------------------------------
# 503 — no config path
# ---------------------------------------------------------------------------


def test_put_503_when_config_path_unset() -> None:
    state = AdminState(config_loader=lambda: {}, config_path=None)
    set_admin_state(state)
    try:
        app = FastAPI()
        app.include_router(credentials.router())
        with TestClient(app) as c:
            resp = c.put(
                "/admin/credentials/openai/api_key", json={"value": "x"}
            )
        assert resp.status_code == 503
        assert resp.json() == {"error": "config_path_unset"}
    finally:
        set_admin_state(None)
