"""Tests for ``/admin/curator/*`` (Wave 4.6 — curator UI backend).

Covers the seven endpoints:

* ``GET /admin/curator/profiles`` — listing + skill / origin histogram
* ``POST /admin/curator/{slug}/preview`` — dry-run transitions
* ``POST /admin/curator/{slug}/run`` — real run; SKILL.md state persists
* ``POST /admin/curator/{slug}/pause`` — pause toggle short-circuits
* ``PATCH /admin/curator/{slug}/thresholds`` — validation + persistence
* ``GET /admin/curator/{slug}/skills`` — state + origin filters + search
* ``POST /admin/curator/{slug}/skills/{name}/pin`` — pin writeback

Plus the two error envelopes the UI keys off:

* unknown slug → 404 ``profile_not_found``
* missing curator_state_repo → 503 ``curator_state_repo_missing``

Each test builds:
* a temp ``profiles/<slug>/skills/*`` tree with SKILL.md files
* a real :class:`ProfileStore` so the slug-exists check is live
* a real :class:`EvolutionStore` (async sqlite) for the curator repo
* a tiny skill_registry_factory pointing at the temp skills dir

This mirrors ``tests/gateway/routes_admin_a/test_profiles.py`` plus the
async-fixture pattern from ``tests/gateway/evolution/test_curator.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from corlinman_evolution_store import (
    CuratorStateRepo,
    EvolutionStore,
    SignalsRepo,
)
from corlinman_server.gateway.routes_admin_b import curator as curator_routes
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    set_admin_state,
)
from corlinman_server.profiles import ProfileStore
from corlinman_skills_registry import SkillRegistry
from corlinman_skills_registry.parse import parse_skill
from corlinman_skills_registry.usage import SkillUsage, write_usage


UTC = timezone.utc
FIXED_NOW = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# On-disk helpers — mirror the curator-engine tests so the fixtures look
# familiar across the two suites.
# ---------------------------------------------------------------------------


def _write_skill_md(
    skills_dir: Path,
    *,
    name: str,
    state: str = "active",
    origin: str = "agent-created",
    pinned: bool = False,
    description: str | None = None,
    version: str = "1.0.0",
    created_at: datetime | None = None,
    last_used_at: datetime | None = None,
) -> Path:
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path = skill_dir / "SKILL.md"
    created = created_at or (FIXED_NOW - timedelta(days=365))
    desc = description or f"{name} test skill"
    text = (
        "---\n"
        f"name: {name}\n"
        f"description: {desc}\n"
        f"version: {version}\n"
        f"origin: {origin}\n"
        f"state: {state}\n"
        f"pinned: {'true' if pinned else 'false'}\n"
        f"created_at: {created.isoformat()}\n"
        "---\n"
        f"# {name}\n\nbody for {name}\n"
    )
    skill_path.write_text(text, encoding="utf-8")
    if last_used_at is not None:
        write_usage(
            skill_dir,
            SkillUsage(
                use_count=1,
                last_used_at=last_used_at,
                created_at=created,
            ),
        )
    return skill_path


def _reload_state(skill_path: Path) -> str:
    return parse_skill(skill_path, skill_path.read_text(encoding="utf-8")).state


def _reload_pinned(skill_path: Path) -> bool:
    return parse_skill(
        skill_path, skill_path.read_text(encoding="utf-8")
    ).pinned


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> AsyncIterator[EvolutionStore]:
    """A per-test :class:`EvolutionStore` so each test gets a clean
    ``curator_state`` + ``evolution_signals`` table."""
    db_path = tmp_path / "evolution-curator-routes-tests.sqlite"
    s = await EvolutionStore.open(db_path)
    try:
        yield s
    finally:
        await s.close()


@pytest_asyncio.fixture
async def profile_store(tmp_path: Path) -> AsyncIterator[ProfileStore]:
    """A profile store rooted at ``<tmp>/profiles`` with one ``default``
    profile preseeded — mirrors the production boot path. The profile's
    skills dir is created empty so individual tests can drop SKILL.md
    files into it without needing the store to provision them."""
    profiles_root = tmp_path / "profiles"
    s = ProfileStore(profiles_root)
    s.create(slug="default", display_name="Default")
    (profiles_root / "default" / "skills").mkdir(parents=True, exist_ok=True)
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def skill_registry_factory(tmp_path: Path):
    """Per-test factory: load the SKILL.md tree under
    ``<tmp>/profiles/<slug>/skills`` on every call. Mirrors what the
    entrypoint wires up at boot."""

    def _factory(slug: str) -> SkillRegistry:
        skills_dir = tmp_path / "profiles" / slug / "skills"
        return SkillRegistry.load_from_dir(skills_dir)

    return _factory


@pytest_asyncio.fixture
async def client(
    tmp_path: Path,
    store: EvolutionStore,
    profile_store: ProfileStore,
    skill_registry_factory,
) -> AsyncIterator[TestClient]:
    """Mount just the curator router with a fully-wired admin state.

    The test owns every handle so the route-level 503 paths are exercised
    by the dedicated *missing*-fixture tests below, not by this one.
    """
    state = AdminState(
        data_dir=tmp_path,
        profile_store=profile_store,
        curator_state_repo=CuratorStateRepo(store.conn),
        signals_repo=SignalsRepo(store.conn),
        skill_registry_factory=skill_registry_factory,
    )
    set_admin_state(state)
    try:
        app = FastAPI()
        app.include_router(curator_routes.router())
        yield TestClient(app)
    finally:
        set_admin_state(None)


# ---------------------------------------------------------------------------
# GET /admin/curator/profiles
# ---------------------------------------------------------------------------


def test_profiles_returns_default_row_with_zero_run_count(
    client: TestClient,
) -> None:
    """Fresh DB → one row (``default``) with run_count=0 and
    DDL-default thresholds. Skill counts are 0 because the skills dir is
    empty in this fixture."""
    resp = client.get("/admin/curator/profiles")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "profiles" in body
    rows = body["profiles"]
    assert len(rows) == 1

    row = rows[0]
    assert row["slug"] == "default"
    assert row["paused"] is False
    assert row["run_count"] == 0
    assert row["last_review_at"] is None
    assert row["interval_hours"] == 168
    assert row["stale_after_days"] == 30
    assert row["archive_after_days"] == 90
    # Counts are zero with an empty skills dir.
    assert row["skill_counts"]["total"] == 0


def test_profiles_includes_skill_and_origin_histograms(
    client: TestClient, tmp_path: Path
) -> None:
    """A skills dir with 3 active + 1 stale (mixed origins) reflects in
    both histograms."""
    skills_dir = tmp_path / "profiles" / "default" / "skills"
    _write_skill_md(skills_dir, name="a1", state="active", origin="agent-created")
    _write_skill_md(skills_dir, name="a2", state="active", origin="bundled")
    _write_skill_md(skills_dir, name="a3", state="active", origin="user-requested")
    _write_skill_md(skills_dir, name="b1", state="stale", origin="agent-created")

    resp = client.get("/admin/curator/profiles")
    assert resp.status_code == 200
    row = resp.json()["profiles"][0]
    assert row["skill_counts"] == {
        "active": 3,
        "stale": 1,
        "archived": 0,
        "total": 4,
    }
    # ``origin_counts`` uses the wire-name with hyphen.
    origins = row["origin_counts"]
    assert origins["bundled"] == 1
    assert origins["user-requested"] == 1
    assert origins["agent-created"] == 2


# ---------------------------------------------------------------------------
# POST /admin/curator/{slug}/preview
# ---------------------------------------------------------------------------


def test_preview_returns_transitions_without_disk_mutation(
    client: TestClient, tmp_path: Path
) -> None:
    """Dry-run returns the would-be ``active → stale`` transition; the
    on-disk SKILL.md ``state`` field stays ``active``."""
    skills_dir = tmp_path / "profiles" / "default" / "skills"
    skill_path = _write_skill_md(
        skills_dir,
        name="research_agent",
        state="active",
        last_used_at=FIXED_NOW - timedelta(days=40),
    )

    resp = client.post("/admin/curator/default/preview")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["profile_slug"] == "default"
    assert body["marked_stale"] == 1
    assert len(body["transitions"]) == 1
    t = body["transitions"][0]
    assert t["skill_name"] == "research_agent"
    assert t["from_state"] == "active"
    assert t["to_state"] == "stale"
    assert t["reason"] == "stale_threshold"
    # Disk untouched.
    assert _reload_state(skill_path) == "active"


# ---------------------------------------------------------------------------
# POST /admin/curator/{slug}/run
# ---------------------------------------------------------------------------


def test_run_persists_state_transition_to_disk(
    client: TestClient, tmp_path: Path
) -> None:
    """Real run mutates the SKILL.md ``state`` field on disk."""
    skills_dir = tmp_path / "profiles" / "default" / "skills"
    skill_path = _write_skill_md(
        skills_dir,
        name="research_agent",
        state="active",
        last_used_at=FIXED_NOW - timedelta(days=40),
    )

    resp = client.post("/admin/curator/default/run")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["marked_stale"] == 1
    # Disk reflects the transition.
    assert _reload_state(skill_path) == "stale"


# ---------------------------------------------------------------------------
# POST /admin/curator/{slug}/pause
# ---------------------------------------------------------------------------


def test_pause_flag_short_circuits_subsequent_run(
    client: TestClient, tmp_path: Path
) -> None:
    """After ``/pause {paused: true}`` a follow-up ``/run`` is rejected
    with 409 ``curator_paused`` and the on-disk state stays untouched."""
    skills_dir = tmp_path / "profiles" / "default" / "skills"
    skill_path = _write_skill_md(
        skills_dir,
        name="research_agent",
        state="active",
        last_used_at=FIXED_NOW - timedelta(days=40),
    )

    pause = client.post("/admin/curator/default/pause", json={"paused": True})
    assert pause.status_code == 200
    assert pause.json()["paused"] is True

    run = client.post("/admin/curator/default/run")
    assert run.status_code == 409
    assert run.json()["detail"]["error"] == "curator_paused"
    # Disk untouched — paused short-circuit happens before the pass.
    assert _reload_state(skill_path) == "active"


def test_pause_then_resume_round_trips(client: TestClient) -> None:
    """The flag toggles in both directions and persists on the row."""
    client.post("/admin/curator/default/pause", json={"paused": True})
    resp = client.post("/admin/curator/default/pause", json={"paused": False})
    assert resp.status_code == 200
    assert resp.json()["paused"] is False

    # Listing reflects the most recent value.
    rows = client.get("/admin/curator/profiles").json()["profiles"]
    assert rows[0]["paused"] is False


# ---------------------------------------------------------------------------
# PATCH /admin/curator/{slug}/thresholds
# ---------------------------------------------------------------------------


def test_thresholds_patch_persists_values(client: TestClient) -> None:
    """Partial PATCH leaves the un-named fields alone."""
    resp = client.patch(
        "/admin/curator/default/thresholds",
        json={"interval_hours": 24},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["interval_hours"] == 24
    # Other thresholds untouched.
    assert body["stale_after_days"] == 30
    assert body["archive_after_days"] == 90


def test_thresholds_archive_must_exceed_stale(client: TestClient) -> None:
    """``archive_after_days <= stale_after_days`` → 422 invalid_thresholds."""
    resp = client.patch(
        "/admin/curator/default/thresholds",
        json={"stale_after_days": 60, "archive_after_days": 60},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "invalid_thresholds"
    assert detail["stale_after_days"] == 60
    assert detail["archive_after_days"] == 60


def test_thresholds_interval_must_be_positive(client: TestClient) -> None:
    """``interval_hours = 0`` is rejected by the pydantic ``Field(ge=1)``."""
    resp = client.patch(
        "/admin/curator/default/thresholds",
        json={"interval_hours": 0},
    )
    assert resp.status_code == 422


def test_thresholds_patch_is_idempotent(client: TestClient) -> None:
    """Same body twice → identical response both times."""
    body = {"interval_hours": 72, "stale_after_days": 14}
    r1 = client.patch("/admin/curator/default/thresholds", json=body).json()
    r2 = client.patch("/admin/curator/default/thresholds", json=body).json()
    assert r1["interval_hours"] == r2["interval_hours"] == 72
    assert r1["stale_after_days"] == r2["stale_after_days"] == 14


# ---------------------------------------------------------------------------
# GET /admin/curator/{slug}/skills (filters)
# ---------------------------------------------------------------------------


def test_skills_list_filters_by_state(
    client: TestClient, tmp_path: Path
) -> None:
    """``?state=stale`` returns only stale skills."""
    skills_dir = tmp_path / "profiles" / "default" / "skills"
    _write_skill_md(skills_dir, name="alpha", state="active")
    _write_skill_md(skills_dir, name="beta", state="stale")
    _write_skill_md(skills_dir, name="gamma", state="archived")

    resp = client.get("/admin/curator/default/skills", params={"state": "stale"})
    assert resp.status_code == 200, resp.text
    names = [s["name"] for s in resp.json()["skills"]]
    assert names == ["beta"]


def test_skills_list_filters_by_origin(
    client: TestClient, tmp_path: Path
) -> None:
    """``?origin=agent-created`` returns only agent-created skills."""
    skills_dir = tmp_path / "profiles" / "default" / "skills"
    _write_skill_md(skills_dir, name="alpha", origin="bundled")
    _write_skill_md(skills_dir, name="beta", origin="agent-created")
    _write_skill_md(skills_dir, name="gamma", origin="user-requested")

    resp = client.get(
        "/admin/curator/default/skills", params={"origin": "agent-created"}
    )
    assert resp.status_code == 200
    rows = resp.json()["skills"]
    assert len(rows) == 1
    assert rows[0]["name"] == "beta"
    assert rows[0]["origin"] == "agent-created"


def test_skills_list_search_substring(
    client: TestClient, tmp_path: Path
) -> None:
    """``?search=`` matches case-insensitive substring on name OR
    description."""
    skills_dir = tmp_path / "profiles" / "default" / "skills"
    _write_skill_md(skills_dir, name="code-review", description="reviews code")
    _write_skill_md(skills_dir, name="weather", description="forecast helper")
    _write_skill_md(
        skills_dir,
        name="formatter",
        description="Formats CODE blocks",
    )

    resp = client.get(
        "/admin/curator/default/skills", params={"search": "code"}
    )
    assert resp.status_code == 200
    names = sorted(s["name"] for s in resp.json()["skills"])
    # ``code-review`` matches on name; ``formatter`` matches on description.
    assert names == ["code-review", "formatter"]


def test_skills_list_returns_full_metadata(
    client: TestClient, tmp_path: Path
) -> None:
    """Each row carries name / description / version / state / origin /
    pinned / created_at / use_count / last_used_at."""
    skills_dir = tmp_path / "profiles" / "default" / "skills"
    last_used = FIXED_NOW - timedelta(days=2)
    _write_skill_md(
        skills_dir,
        name="research_agent",
        state="active",
        origin="agent-created",
        version="1.2.0",
        last_used_at=last_used,
    )

    resp = client.get("/admin/curator/default/skills")
    assert resp.status_code == 200
    rows = resp.json()["skills"]
    assert len(rows) == 1
    r = rows[0]
    assert r["name"] == "research_agent"
    assert r["version"] == "1.2.0"
    assert r["state"] == "active"
    assert r["origin"] == "agent-created"
    assert r["pinned"] is False
    assert r["use_count"] == 1
    assert r["last_used_at"] is not None


# ---------------------------------------------------------------------------
# POST /admin/curator/{slug}/skills/{name}/pin
# ---------------------------------------------------------------------------


def test_pin_writes_back_to_skill_md(
    client: TestClient, tmp_path: Path
) -> None:
    """A POST ``{pinned: true}`` flips the on-disk SKILL.md ``pinned``
    field so a subsequent registry load picks it up."""
    skills_dir = tmp_path / "profiles" / "default" / "skills"
    skill_path = _write_skill_md(
        skills_dir, name="research_agent", pinned=False
    )

    resp = client.post(
        "/admin/curator/default/skills/research_agent/pin",
        json={"pinned": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["pinned"] is True
    # File on disk reflects the new pin.
    assert _reload_pinned(skill_path) is True


def test_pin_unknown_skill_404(client: TestClient, tmp_path: Path) -> None:
    """Skill name not in the registry → 404 skill_not_found."""
    (tmp_path / "profiles" / "default" / "skills").mkdir(
        parents=True, exist_ok=True
    )
    resp = client.post(
        "/admin/curator/default/skills/ghost/pin",
        json={"pinned": True},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "skill_not_found"


# ---------------------------------------------------------------------------
# 404 paths
# ---------------------------------------------------------------------------


def test_unknown_profile_404_on_preview(client: TestClient) -> None:
    resp = client.post("/admin/curator/ghost/preview")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "profile_not_found"


def test_unknown_profile_404_on_skills(client: TestClient) -> None:
    resp = client.get("/admin/curator/ghost/skills")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "profile_not_found"


def test_unknown_profile_404_on_pause(client: TestClient) -> None:
    resp = client.post(
        "/admin/curator/ghost/pause", json={"paused": True}
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 503 paths — missing handles
# ---------------------------------------------------------------------------


def test_curator_state_repo_missing_returns_503(tmp_path: Path) -> None:
    """When ``curator_state_repo`` is None every curator route 503s with
    ``curator_state_repo_missing``."""
    profiles_root = tmp_path / "profiles"
    s = ProfileStore(profiles_root)
    s.create(slug="default", display_name="Default")
    (profiles_root / "default" / "skills").mkdir(parents=True, exist_ok=True)

    def _factory(_slug: str) -> SkillRegistry:
        return SkillRegistry.load_from_dir(profiles_root / "default" / "skills")

    state = AdminState(
        data_dir=tmp_path,
        profile_store=s,
        skill_registry_factory=_factory,
        # curator_state_repo intentionally absent.
    )
    set_admin_state(state)
    try:
        app = FastAPI()
        app.include_router(curator_routes.router())
        with TestClient(app) as c:
            resp = c.get("/admin/curator/profiles")
            assert resp.status_code == 503
            assert (
                resp.json()["detail"]["error"]
                == "curator_state_repo_missing"
            )
    finally:
        set_admin_state(None)
        s.close()


def test_profile_store_missing_returns_503(tmp_path: Path) -> None:
    """Without ``profile_store`` we 503 before even checking the slug —
    same envelope shape the W3.1 profiles route uses."""
    state = AdminState(data_dir=tmp_path)
    set_admin_state(state)
    try:
        app = FastAPI()
        app.include_router(curator_routes.router())
        with TestClient(app) as c:
            resp = c.get("/admin/curator/profiles")
            assert resp.status_code == 503
            assert resp.json()["detail"]["error"] == "profile_store_missing"
    finally:
        set_admin_state(None)
