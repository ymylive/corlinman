"""Tests for ``POST /admin/username`` (W1.2).

Mirrors the matrix in the plan:

* happy path: ``admin`` → ``newuser`` updates ``/admin/me``, lets the same session continue to
  rotate the password, and the on-disk ``[admin]`` block reflects the new name while reusing the
  same hash + flag.
* unauthenticated requests 401.
* wrong ``old_password`` 401 ``invalid_old_password``.
* empty / illegal-character / too-long new usernames 422 ``invalid_username``.
* idempotent: same username → 200 with ``status="unchanged"``.
"""

from __future__ import annotations

import asyncio
import tomllib
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
    """Spin up a minimal app around the auth router only — keeps the
    test independent of sibling W3 submodules whose routers may not be
    fully wired yet."""
    app = FastAPI()
    set_admin_state(state)
    app.include_router(auth_router())
    return TestClient(app)


@pytest.fixture
def seeded_state(tmp_path: Path) -> AdminState:
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
    resp = client.post(
        "/admin/login", json={"username": username, "password": password}
    )
    assert resp.status_code == 200, resp.text
    cookie = resp.cookies.get(SESSION_COOKIE_NAME)
    assert cookie
    return cookie


def test_happy_path_renames_and_persists(seeded_state: AdminState) -> None:
    client = _build_client(seeded_state)
    cookie = _login(client, username="admin", password="root")
    original_hash = seeded_state.admin_password_hash
    assert original_hash is not None

    resp = client.post(
        "/admin/username",
        json={"old_password": "root", "new_username": "newuser"},
        cookies={SESSION_COOKIE_NAME: cookie},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "ok", "username": "newuser"}

    # /admin/me reflects the new username; the cookie is still valid
    # because the in-place session rename keeps it bound to the new
    # operator identity.
    me = client.get("/admin/me", cookies={SESSION_COOKIE_NAME: cookie})
    assert me.status_code == 200, me.text
    assert me.json()["user"] == "newuser"

    # And subsequent operations (password rotation) work with the new name.
    rotate = client.post(
        "/admin/password",
        json={"old_password": "root", "new_password": "fresh-pass-1"},
        cookies={SESSION_COOKIE_NAME: cookie},
    )
    assert rotate.status_code == 200, rotate.text

    # Disk: same hash (until rotate ran above), same must_change_password.
    cfg_text = seeded_state.config_path.read_text(encoding="utf-8")
    parsed = tomllib.loads(cfg_text)
    assert parsed["admin"]["username"] == "newuser"
    # After the rotate step the on-disk hash changes; checking the flag
    # is still serialised verbatim is the contract we care about.
    assert parsed["admin"]["must_change_password"] is False


def test_username_only_persists_keep_existing_hash(
    seeded_state: AdminState,
) -> None:
    """A pure rename (no password rotation) leaves the hash + flag untouched on disk."""
    client = _build_client(seeded_state)
    cookie = _login(client, username="admin", password="root")
    original_hash = seeded_state.admin_password_hash

    resp = client.post(
        "/admin/username",
        json={"old_password": "root", "new_username": "ops"},
        cookies={SESSION_COOKIE_NAME: cookie},
    )
    assert resp.status_code == 200, resp.text

    parsed = tomllib.loads(
        seeded_state.config_path.read_text(encoding="utf-8")
    )
    assert parsed["admin"]["username"] == "ops"
    assert parsed["admin"]["password_hash"] == original_hash
    assert parsed["admin"]["must_change_password"] is True  # unchanged


def test_missing_session_returns_401(seeded_state: AdminState) -> None:
    client = _build_client(seeded_state)
    resp = client.post(
        "/admin/username",
        json={"old_password": "root", "new_username": "newuser"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "unauthenticated"


def test_wrong_old_password_returns_401(seeded_state: AdminState) -> None:
    client = _build_client(seeded_state)
    cookie = _login(client, username="admin", password="root")
    resp = client.post(
        "/admin/username",
        json={"old_password": "wrong-pass", "new_username": "newuser"},
        cookies={SESSION_COOKIE_NAME: cookie},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "invalid_old_password"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "user@bad",
        "with space",
        "tab\there",
        "x" * 65,  # too long
    ],
)
def test_invalid_usernames_return_422(
    seeded_state: AdminState, bad: str
) -> None:
    client = _build_client(seeded_state)
    cookie = _login(client, username="admin", password="root")
    resp = client.post(
        "/admin/username",
        json={"old_password": "root", "new_username": bad},
        cookies={SESSION_COOKIE_NAME: cookie},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error"] == "invalid_username"


def test_idempotent_same_username(seeded_state: AdminState) -> None:
    client = _build_client(seeded_state)
    cookie = _login(client, username="admin", password="root")
    resp = client.post(
        "/admin/username",
        json={"old_password": "root", "new_username": "admin"},
        cookies={SESSION_COOKIE_NAME: cookie},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "unchanged", "username": "admin"}
