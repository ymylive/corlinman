"""Tests for the ``/admin/profiles*`` FastAPI surface.

Covers:
* POST /admin/profiles 201 → GET /admin/profiles lists ["default", "research"]
* POST duplicate slug → 409
* POST {slug: "BAD"} → 422 with ``error="invalid_slug"``
* DELETE /admin/profiles/default → 409 (ProfileProtected)
* DELETE /admin/profiles/research → 204; subsequent GET → 404
* PATCH display_name → 200 + reflected on subsequent GET

Each test builds a fresh app + ProfileStore via the ``client`` fixture so
parallel test execution doesn't cross-pollinate state.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from corlinman_server.gateway.routes_admin_a import (
    AdminState,
    build_router,
    set_admin_state,
)
from corlinman_server.profiles import ProfileStore


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    """Build a fresh app + ProfileStore + AdminState per test.

    The store is pre-seeded with a "default" profile to mirror the
    production boot path (entrypoint.py creates "default" on first
    run). Tests that need a clean slate can call
    ``store.delete(slug)`` for non-default rows.
    """
    profiles_dir = tmp_path / "profiles"
    store = ProfileStore(profiles_dir)
    store.create(slug="default", display_name="Default")

    state = AdminState(
        data_dir=tmp_path,
        profile_store=store,
    )
    set_admin_state(state)

    app = FastAPI()
    app.include_router(build_router())

    with TestClient(app) as c:
        yield c

    set_admin_state(None)
    store.close()


# ---------------------------------------------------------------------------
# GET /admin/profiles
# ---------------------------------------------------------------------------


def test_list_starts_with_default(client: TestClient) -> None:
    resp = client.get("/admin/profiles")
    assert resp.status_code == 200
    rows = resp.json()
    assert isinstance(rows, list)
    assert [r["slug"] for r in rows] == ["default"]
    assert rows[0]["display_name"] == "Default"


# ---------------------------------------------------------------------------
# POST /admin/profiles
# ---------------------------------------------------------------------------


def test_create_profile_201_and_list_reflects(client: TestClient) -> None:
    resp = client.post("/admin/profiles", json={"slug": "research"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["slug"] == "research"
    # Default display_name == slug.
    assert body["display_name"] == "research"
    assert body["parent_slug"] is None

    listed = client.get("/admin/profiles").json()
    assert [r["slug"] for r in listed] == ["default", "research"]


def test_create_with_display_name_and_description(client: TestClient) -> None:
    resp = client.post(
        "/admin/profiles",
        json={
            "slug": "research",
            "display_name": "Research Bot",
            "description": "Reads papers",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["display_name"] == "Research Bot"
    assert body["description"] == "Reads papers"


def test_create_duplicate_slug_409(client: TestClient) -> None:
    client.post("/admin/profiles", json={"slug": "research"})
    resp = client.post("/admin/profiles", json={"slug": "research"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "profile_exists"


def test_create_invalid_slug_422(client: TestClient) -> None:
    resp = client.post("/admin/profiles", json={"slug": "BAD"})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "invalid_slug"


def test_create_clone_from_missing_404(client: TestClient) -> None:
    resp = client.post(
        "/admin/profiles",
        json={"slug": "child", "clone_from": "ghost"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "parent_not_found"


def test_create_clone_from_default_succeeds(client: TestClient) -> None:
    resp = client.post(
        "/admin/profiles",
        json={"slug": "child", "clone_from": "default"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["parent_slug"] == "default"


# ---------------------------------------------------------------------------
# GET /admin/profiles/{slug}
# ---------------------------------------------------------------------------


def test_get_profile_200(client: TestClient) -> None:
    client.post("/admin/profiles", json={"slug": "research"})
    resp = client.get("/admin/profiles/research")
    assert resp.status_code == 200
    assert resp.json()["slug"] == "research"


def test_get_profile_404(client: TestClient) -> None:
    resp = client.get("/admin/profiles/ghost")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "profile_not_found"


# ---------------------------------------------------------------------------
# PATCH /admin/profiles/{slug}
# ---------------------------------------------------------------------------


def test_patch_display_name_updates_and_persists(client: TestClient) -> None:
    client.post("/admin/profiles", json={"slug": "research"})

    resp = client.patch(
        "/admin/profiles/research",
        json={"display_name": "Research Prime"},
    )
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Research Prime"

    # GET reflects the update.
    got = client.get("/admin/profiles/research").json()
    assert got["display_name"] == "Research Prime"


def test_patch_description_only(client: TestClient) -> None:
    client.post(
        "/admin/profiles",
        json={"slug": "research", "display_name": "Research"},
    )
    resp = client.patch(
        "/admin/profiles/research",
        json={"description": "blurb"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["display_name"] == "Research"
    assert body["description"] == "blurb"


def test_patch_missing_404(client: TestClient) -> None:
    resp = client.patch("/admin/profiles/ghost", json={"display_name": "X"})
    assert resp.status_code == 404


def test_patch_empty_display_name_422(client: TestClient) -> None:
    client.post("/admin/profiles", json={"slug": "research"})
    resp = client.patch(
        "/admin/profiles/research",
        json={"display_name": "   "},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# DELETE /admin/profiles/{slug}
# ---------------------------------------------------------------------------


def test_delete_default_409(client: TestClient) -> None:
    resp = client.delete("/admin/profiles/default")
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "profile_protected"


def test_delete_research_204(client: TestClient) -> None:
    client.post("/admin/profiles", json={"slug": "research"})
    resp = client.delete("/admin/profiles/research")
    assert resp.status_code == 204
    # Subsequent GET → 404.
    resp = client.get("/admin/profiles/research")
    assert resp.status_code == 404


def test_delete_missing_404(client: TestClient) -> None:
    resp = client.delete("/admin/profiles/ghost")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 503 path — store not wired
# ---------------------------------------------------------------------------


def test_profile_store_missing_returns_503(tmp_path: Path) -> None:
    """If the bootstrapper didn't wire ``profile_store`` the routes 503."""
    state = AdminState(data_dir=tmp_path)
    set_admin_state(state)
    app = FastAPI()
    app.include_router(build_router())
    try:
        with TestClient(app) as c:
            resp = c.get("/admin/profiles")
            assert resp.status_code == 503
            assert resp.json()["detail"]["error"] == "profile_store_missing"
    finally:
        set_admin_state(None)


# ---------------------------------------------------------------------------
# GET / PUT /admin/profiles/{slug}/soul  (W3.2)
# ---------------------------------------------------------------------------


def test_get_soul_empty_when_file_missing(
    client: TestClient, tmp_path: Path
) -> None:
    """Fresh profile with no SOUL content → 200 + ``{content: ""}``.

    The ``client`` fixture preseeds ``default`` via ``store.create`` which
    materialises the placeholder file empty — so the GET returns an empty
    string rather than 404.
    """
    resp = client.get("/admin/profiles/default/soul")
    assert resp.status_code == 200
    assert resp.json() == {"content": ""}


def test_put_then_get_soul_roundtrip(client: TestClient, tmp_path: Path) -> None:
    client.post("/admin/profiles", json={"slug": "research"})
    body = "# Research persona\n\nLikes papers."
    put = client.put(
        "/admin/profiles/research/soul",
        json={"content": body},
    )
    assert put.status_code == 200
    assert put.json() == {"content": body}

    got = client.get("/admin/profiles/research/soul")
    assert got.status_code == 200
    assert got.json() == {"content": body}

    # Disk side-effect — confirm we hit the right path.
    soul_file = tmp_path / "profiles" / "research" / "SOUL.md"
    assert soul_file.read_text(encoding="utf-8") == body


def test_get_soul_404_for_missing_profile(client: TestClient) -> None:
    resp = client.get("/admin/profiles/ghost/soul")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "profile_not_found"


def test_put_soul_404_for_missing_profile(client: TestClient) -> None:
    resp = client.put(
        "/admin/profiles/ghost/soul", json={"content": "anything"}
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "profile_not_found"


def test_put_soul_atomic_replaces_previous_content(
    client: TestClient, tmp_path: Path
) -> None:
    """Repeated PUTs overwrite cleanly with no stale tempfiles left over."""
    client.post("/admin/profiles", json={"slug": "research"})
    client.put("/admin/profiles/research/soul", json={"content": "v1"})
    client.put("/admin/profiles/research/soul", json={"content": "v2"})

    got = client.get("/admin/profiles/research/soul").json()
    assert got["content"] == "v2"

    # No stale ``.soul-*.tmp`` files left behind.
    profile_dir = tmp_path / "profiles" / "research"
    stragglers = list(profile_dir.glob(".soul-*.tmp"))
    assert stragglers == []


def test_get_soul_404_when_store_missing(tmp_path: Path) -> None:
    state = AdminState(data_dir=tmp_path)
    set_admin_state(state)
    app = FastAPI()
    app.include_router(build_router())
    try:
        with TestClient(app) as c:
            resp = c.get("/admin/profiles/default/soul")
            assert resp.status_code == 503
            assert resp.json()["detail"]["error"] == "profile_store_missing"
    finally:
        set_admin_state(None)
