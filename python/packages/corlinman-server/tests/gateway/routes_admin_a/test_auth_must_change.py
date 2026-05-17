"""Tests for the W1.1 ``must_change_password`` propagation.

Covers the wire path:

* ``/admin/login`` → ``/admin/me`` surfaces ``must_change_password=True`` while the in-memory
  ``AdminState`` is still the first-boot default.
* ``/admin/password`` (successful rotation) flips the flag both in memory *and* on disk so the
  next ``/admin/me`` (and the next gateway reboot) returns ``must_change_password=False``.
* A "reboot" simulated by re-running ``ensure_admin_credentials`` against the same config file
  reads back the persisted ``must_change_password=false`` value.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from corlinman_server.gateway.lifecycle.admin_seed import ensure_admin_credentials
from corlinman_server.gateway.routes_admin_a.auth import router as auth_router
from corlinman_server.gateway.routes_admin_a.state import (
    AdminState,
    set_admin_state,
)
from corlinman_server.gateway.routes_admin_a._session_store import (
    SESSION_COOKIE_NAME,
    AdminSessionStore,
)


def _build_client(state: AdminState) -> TestClient:
    """Spin up a minimal app around the auth router only.

    We deliberately avoid ``build_router()`` so this test stays orthogonal
    to sibling W3 submodules (``profiles.py`` etc.) that may not be wired
    up cleanly yet — the only handlers we exercise here live in
    ``auth.py``.
    """
    app = FastAPI()
    set_admin_state(state)
    app.include_router(auth_router())
    return TestClient(app)


@pytest.fixture
def seeded_state(tmp_path: Path) -> AdminState:
    """An ``AdminState`` populated by the same code path the lifespan uses."""
    cfg = tmp_path / "config.toml"
    seeded = asyncio.run(ensure_admin_credentials(config_path=cfg))
    return AdminState(
        data_dir=tmp_path,
        admin_username=seeded.username,
        admin_password_hash=seeded.password_hash,
        config_path=seeded.config_path,
        must_change_password=seeded.must_change_password,
        session_store=AdminSessionStore(ttl_seconds=3600),
        admin_write_lock=asyncio.Lock(),
    )


def _login(client: TestClient, *, username: str, password: str) -> str:
    """Drive the login route and return the freshly-issued cookie value."""
    resp = client.post(
        "/admin/login", json={"username": username, "password": password}
    )
    assert resp.status_code == 200, resp.text
    cookie = resp.cookies.get(SESSION_COOKIE_NAME)
    assert cookie, "login did not set the session cookie"
    return cookie


def test_me_reports_must_change_password_after_first_boot(
    seeded_state: AdminState,
) -> None:
    client = _build_client(seeded_state)
    cookie = _login(client, username="admin", password="root")

    me = client.get("/admin/me", cookies={SESSION_COOKIE_NAME: cookie})
    assert me.status_code == 200, me.text
    body = me.json()
    assert body["user"] == "admin"
    assert body["must_change_password"] is True


def test_change_password_flips_must_change_password(
    seeded_state: AdminState,
) -> None:
    client = _build_client(seeded_state)
    cookie = _login(client, username="admin", password="root")

    rotated = client.post(
        "/admin/password",
        json={"old_password": "root", "new_password": "fresh-pass-1"},
        cookies={SESSION_COOKIE_NAME: cookie},
    )
    assert rotated.status_code == 200, rotated.text

    me = client.get("/admin/me", cookies={SESSION_COOKIE_NAME: cookie})
    assert me.status_code == 200, me.text
    assert me.json()["must_change_password"] is False


def test_password_rotation_persists_flag_for_next_boot(
    seeded_state: AdminState, tmp_path: Path
) -> None:
    """Reboot equivalent: re-load the AdminState via ``ensure_admin_credentials``
    after a rotation and confirm ``must_change_password`` stayed ``False``."""
    client = _build_client(seeded_state)
    cookie = _login(client, username="admin", password="root")
    rotated = client.post(
        "/admin/password",
        json={"old_password": "root", "new_password": "fresh-pass-1"},
        cookies={SESSION_COOKIE_NAME: cookie},
    )
    assert rotated.status_code == 200

    # Simulate a fresh boot — same config file, fresh state.
    re_seeded = asyncio.run(
        ensure_admin_credentials(config_path=seeded_state.config_path)
    )
    assert re_seeded.must_change_password is False
    assert re_seeded.seeded_now is False
    # The username + hash also persist verbatim.
    assert re_seeded.username == "admin"
    assert re_seeded.password_hash == seeded_state.admin_password_hash
