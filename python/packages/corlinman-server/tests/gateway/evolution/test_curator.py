"""Tests for the W4.3 lifecycle curator.

Covers:

* The pure decision core (:func:`apply_lifecycle_transitions`) — every
  state-transition rule from hermes ``agent/curator.py:256-296`` plus
  the provenance / pin guards from ``tools/skill_usage.py:154-200``.
* The async idle trigger (:func:`maybe_run_curator`) — interval gate,
  ``paused`` short-circuit, ``force=True`` override, signal emission
  (idle / completed / failed / per-skill unused), and the
  ``mark_run`` writeback that closes the interval window.
* Dry-run isolation: previewing must NOT touch the SKILL.md state field
  on disk and must NOT bump the curator interval window.

The tests build a tiny on-disk skill library inside ``tmp_path``,
construct a :class:`SkillRegistry` over it, and feed in a synthetic
:class:`CuratorState` so the thresholds are explicit per test (no
reliance on the DDL defaults). Time is always injected via ``now=`` so
the assertions stay deterministic.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from corlinman_evolution_store import (
    EVENT_CURATOR_RUN_COMPLETED,
    EVENT_CURATOR_RUN_FAILED,
    EVENT_IDLE_REFLECTION,
    EVENT_SKILL_UNUSED,
    CuratorState,
    CuratorStateRepo,
    EvolutionStore,
    SignalsRepo,
)
from corlinman_skills_registry import SkillRegistry
from corlinman_skills_registry.parse import parse_skill
from corlinman_skills_registry.usage import SkillUsage, write_usage

from corlinman_server.gateway.evolution import (
    CuratorReport,
    CuratorTransition,
    apply_lifecycle_transitions,
    maybe_run_curator,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


UTC = timezone.utc
FIXED_NOW = datetime(2026, 5, 17, 12, 0, 0, tzinfo=UTC)


def _write_skill_md(
    skills_dir: Path,
    *,
    name: str,
    state: str = "active",
    origin: str = "agent-created",
    pinned: bool = False,
    created_at: datetime | None = None,
    last_used_at: datetime | None = None,
) -> Path:
    """Drop a single ``<skills_dir>/<name>/SKILL.md`` plus an optional
    ``.usage.json`` sidecar — the layout the curator reads.
    """
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path = skill_dir / "SKILL.md"
    created = created_at or (FIXED_NOW - timedelta(days=365))
    text = (
        "---\n"
        f"name: {name}\n"
        f"description: {name} test skill\n"
        f"version: 1.0.0\n"
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


def _state(
    profile: str = "default",
    *,
    last_review_at: datetime | None = None,
    paused: bool = False,
    interval_hours: int = 168,
    stale_after_days: int = 30,
    archive_after_days: int = 90,
) -> CuratorState:
    return CuratorState(
        profile_slug=profile,
        last_review_at=last_review_at,
        last_review_duration_ms=None,
        last_review_summary=None,
        run_count=0,
        paused=paused,
        interval_hours=interval_hours,
        stale_after_days=stale_after_days,
        archive_after_days=archive_after_days,
    )


def _reload_state(skill_path: Path) -> str:
    """Re-parse a SKILL.md off disk and return its ``state`` field."""
    return parse_skill(skill_path, skill_path.read_text(encoding="utf-8")).state


# ---------------------------------------------------------------------------
# Pure logic — `apply_lifecycle_transitions`
# ---------------------------------------------------------------------------


def test_active_skill_idle_past_threshold_marked_stale(tmp_path: Path) -> None:
    """35 days idle > 30-day stale threshold → active becomes stale."""
    skill_path = _write_skill_md(
        tmp_path,
        name="research_agent",
        state="active",
        last_used_at=FIXED_NOW - timedelta(days=35),
    )
    registry = SkillRegistry.load_from_dir(tmp_path)
    state = _state(stale_after_days=30, archive_after_days=90)

    transitions = apply_lifecycle_transitions(registry, state, now=FIXED_NOW)

    assert len(transitions) == 1
    t = transitions[0]
    assert isinstance(t, CuratorTransition)
    assert t.skill_name == "research_agent"
    assert t.from_state == "active"
    assert t.to_state == "stale"
    assert t.reason == "stale_threshold"
    assert t.days_idle == pytest.approx(35.0, abs=0.01)
    # File on disk was updated.
    assert _reload_state(skill_path) == "stale"


def test_active_skill_idle_within_threshold_unchanged(tmp_path: Path) -> None:
    """25 days idle < 30-day stale threshold → no transition."""
    _write_skill_md(
        tmp_path,
        name="research_agent",
        state="active",
        last_used_at=FIXED_NOW - timedelta(days=25),
    )
    registry = SkillRegistry.load_from_dir(tmp_path)
    state = _state(stale_after_days=30)

    transitions = apply_lifecycle_transitions(registry, state, now=FIXED_NOW)
    assert transitions == []


def test_stale_skill_past_archive_threshold_archived(tmp_path: Path) -> None:
    """95 days idle > 90-day archive threshold → stale becomes archived."""
    skill_path = _write_skill_md(
        tmp_path,
        name="research_agent",
        state="stale",
        last_used_at=FIXED_NOW - timedelta(days=95),
    )
    registry = SkillRegistry.load_from_dir(tmp_path)
    state = _state(stale_after_days=30, archive_after_days=90)

    transitions = apply_lifecycle_transitions(registry, state, now=FIXED_NOW)

    assert len(transitions) == 1
    t = transitions[0]
    assert t.from_state == "stale"
    assert t.to_state == "archived"
    assert t.reason == "archive_threshold"
    assert _reload_state(skill_path) == "archived"


def test_stale_skill_used_after_review_reactivated(tmp_path: Path) -> None:
    """A stale skill whose ``last_used_at`` is past the last curator
    review timestamp gets bumped back to active."""
    last_review = FIXED_NOW - timedelta(days=10)
    last_used = FIXED_NOW - timedelta(days=2)  # after the last review
    skill_path = _write_skill_md(
        tmp_path,
        name="research_agent",
        state="stale",
        last_used_at=last_used,
    )
    registry = SkillRegistry.load_from_dir(tmp_path)
    state = _state(
        last_review_at=last_review,
        stale_after_days=30,
        archive_after_days=90,
    )

    transitions = apply_lifecycle_transitions(registry, state, now=FIXED_NOW)

    assert len(transitions) == 1
    t = transitions[0]
    assert t.from_state == "stale"
    assert t.to_state == "active"
    assert t.reason == "reactivated"
    assert _reload_state(skill_path) == "active"


def test_pinned_skill_never_transitions(tmp_path: Path) -> None:
    """100 days idle but ``pinned=true`` → still no transition."""
    skill_path = _write_skill_md(
        tmp_path,
        name="pinned_skill",
        state="active",
        pinned=True,
        last_used_at=FIXED_NOW - timedelta(days=100),
    )
    registry = SkillRegistry.load_from_dir(tmp_path)

    transitions = apply_lifecycle_transitions(registry, _state(), now=FIXED_NOW)

    assert transitions == []
    assert _reload_state(skill_path) == "active"


def test_bundled_origin_skill_skipped(tmp_path: Path) -> None:
    """``origin=bundled`` → curator never touches it, regardless of idle."""
    skill_path = _write_skill_md(
        tmp_path,
        name="bundled_skill",
        state="active",
        origin="bundled",
        last_used_at=FIXED_NOW - timedelta(days=100),
    )
    registry = SkillRegistry.load_from_dir(tmp_path)

    transitions = apply_lifecycle_transitions(registry, _state(), now=FIXED_NOW)

    assert transitions == []
    assert _reload_state(skill_path) == "active"


def test_user_requested_origin_skill_skipped(tmp_path: Path) -> None:
    """``origin=user-requested`` → curator never touches it either."""
    skill_path = _write_skill_md(
        tmp_path,
        name="manual_skill",
        state="active",
        origin="user-requested",
        last_used_at=FIXED_NOW - timedelta(days=100),
    )
    registry = SkillRegistry.load_from_dir(tmp_path)

    transitions = apply_lifecycle_transitions(registry, _state(), now=FIXED_NOW)

    assert transitions == []
    assert _reload_state(skill_path) == "active"


def test_dry_run_does_not_mutate_disk(tmp_path: Path) -> None:
    """``dry_run=True`` returns the proposed transitions but leaves the
    SKILL.md ``state`` field on disk unchanged."""
    skill_path = _write_skill_md(
        tmp_path,
        name="research_agent",
        state="active",
        last_used_at=FIXED_NOW - timedelta(days=40),
    )
    registry = SkillRegistry.load_from_dir(tmp_path)

    transitions = apply_lifecycle_transitions(
        registry,
        _state(),
        now=FIXED_NOW,
        dry_run=True,
    )

    assert len(transitions) == 1
    assert transitions[0].to_state == "stale"
    # Disk untouched.
    assert _reload_state(skill_path) == "active"


def test_real_run_persists_state_change(tmp_path: Path) -> None:
    """``dry_run=False`` (the default) writes the state flip through to
    disk so a re-parse picks up the new value."""
    skill_path = _write_skill_md(
        tmp_path,
        name="research_agent",
        state="active",
        last_used_at=FIXED_NOW - timedelta(days=40),
    )
    registry = SkillRegistry.load_from_dir(tmp_path)

    apply_lifecycle_transitions(registry, _state(), now=FIXED_NOW)

    assert _reload_state(skill_path) == "stale"
    # The Markdown body must round-trip verbatim — the curator must not
    # eat the "# research_agent" heading or trailing newline.
    raw = skill_path.read_text(encoding="utf-8")
    assert "# research_agent" in raw
    assert "body for research_agent" in raw


def test_new_skill_with_no_usage_uses_created_at_anchor(tmp_path: Path) -> None:
    """A brand-new agent-created skill with no ``last_used_at`` should
    *not* immediately archive itself — the ``created_at`` anchor keeps
    it active until the stale threshold elapses."""
    _write_skill_md(
        tmp_path,
        name="freshly_made",
        state="active",
        created_at=FIXED_NOW - timedelta(days=2),  # only 2 days old
        last_used_at=None,
    )
    registry = SkillRegistry.load_from_dir(tmp_path)

    transitions = apply_lifecycle_transitions(registry, _state(), now=FIXED_NOW)
    assert transitions == []


# ---------------------------------------------------------------------------
# Idle trigger — `maybe_run_curator`
# ---------------------------------------------------------------------------


@pytest.fixture
async def store(tmp_path: Path):
    """Per-test :class:`EvolutionStore` over a temp sqlite. Used by the
    idle-trigger tests that need real :class:`CuratorStateRepo` and
    :class:`SignalsRepo` instances over the same connection."""
    db_path = tmp_path / "evolution-curator-tests.sqlite"
    s = await EvolutionStore.open(db_path)
    try:
        yield s
    finally:
        await s.close()


def _make_one_active_skill(tmp_path: Path) -> SkillRegistry:
    """Helper: one ``agent-created`` skill, idle 40 days (past stale
    threshold). Used by the trigger tests as the "something to do" base."""
    _write_skill_md(
        tmp_path,
        name="research_agent",
        state="active",
        last_used_at=FIXED_NOW - timedelta(days=40),
    )
    return SkillRegistry.load_from_dir(tmp_path)


async def _all_signals(signals: SignalsRepo, *, limit: int = 100):
    """Read every signal the curator wrote during this test."""
    return await signals.list_since(0, None, limit)


async def test_first_run_with_no_prior_state_executes(store, tmp_path: Path) -> None:
    """Empty curator_state (no ``last_review_at``) → curator runs and
    returns a real :class:`CuratorReport`."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    registry = _make_one_active_skill(skills_dir)
    curator = CuratorStateRepo(store.conn)
    signals = SignalsRepo(store.conn)

    report = await maybe_run_curator(
        profile_slug="default",
        registry=registry,
        curator_repo=curator,
        signals_repo=signals,
        now=FIXED_NOW,
    )

    assert isinstance(report, CuratorReport)
    assert report.marked_stale == 1
    assert report.checked == 1
    assert report.skipped == 0
    # Persisted: the next call within the interval should NOT re-run.
    persisted = await curator.get("default")
    assert persisted.last_review_at == FIXED_NOW
    assert persisted.run_count == 1


async def test_recent_run_within_interval_returns_none(store, tmp_path: Path) -> None:
    """A last_review_at one hour ago + 168h interval → curator declines
    to run again (no signals, no transitions)."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    registry = _make_one_active_skill(skills_dir)
    curator = CuratorStateRepo(store.conn)
    signals = SignalsRepo(store.conn)
    # Seed an in-window last_review_at so the gate trips.
    await curator.upsert(
        _state(last_review_at=FIXED_NOW - timedelta(hours=1), interval_hours=168)
    )

    report = await maybe_run_curator(
        profile_slug="default",
        registry=registry,
        curator_repo=curator,
        signals_repo=signals,
        now=FIXED_NOW,
    )

    assert report is None
    # No signals emitted — the gate runs before any side effect.
    assert await _all_signals(signals) == []


async def test_stale_last_review_runs_again(store, tmp_path: Path) -> None:
    """200h since last_review_at vs 168h interval → curator runs."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    registry = _make_one_active_skill(skills_dir)
    curator = CuratorStateRepo(store.conn)
    signals = SignalsRepo(store.conn)
    await curator.upsert(
        _state(last_review_at=FIXED_NOW - timedelta(hours=200), interval_hours=168)
    )

    report = await maybe_run_curator(
        profile_slug="default",
        registry=registry,
        curator_repo=curator,
        signals_repo=signals,
        now=FIXED_NOW,
    )

    assert report is not None
    assert report.marked_stale == 1
    persisted = await curator.get("default")
    assert persisted.last_review_at == FIXED_NOW
    # run_count incremented from the seed (which was 0) → 1.
    assert persisted.run_count == 1


async def test_paused_state_short_circuits(store, tmp_path: Path) -> None:
    """``paused=True`` → curator returns None even with a stale
    last_review_at, and never emits the idle reflection signal."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    registry = _make_one_active_skill(skills_dir)
    curator = CuratorStateRepo(store.conn)
    signals = SignalsRepo(store.conn)
    await curator.upsert(
        _state(
            last_review_at=FIXED_NOW - timedelta(days=999),  # ancient
            paused=True,
        )
    )

    report = await maybe_run_curator(
        profile_slug="default",
        registry=registry,
        curator_repo=curator,
        signals_repo=signals,
        now=FIXED_NOW,
    )

    assert report is None
    assert await _all_signals(signals) == []


async def test_force_overrides_interval_gate(store, tmp_path: Path) -> None:
    """``force=True`` runs even when ``last_review_at`` is in-window."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    registry = _make_one_active_skill(skills_dir)
    curator = CuratorStateRepo(store.conn)
    signals = SignalsRepo(store.conn)
    await curator.upsert(
        _state(last_review_at=FIXED_NOW - timedelta(minutes=5), interval_hours=168)
    )

    report = await maybe_run_curator(
        profile_slug="default",
        registry=registry,
        curator_repo=curator,
        signals_repo=signals,
        now=FIXED_NOW,
        force=True,
    )

    assert report is not None
    assert report.marked_stale == 1


async def test_run_emits_idle_and_completed_signals(store, tmp_path: Path) -> None:
    """A real run emits at least ``EVENT_IDLE_REFLECTION``,
    ``EVENT_SKILL_UNUSED`` (one per transition), and
    ``EVENT_CURATOR_RUN_COMPLETED`` — in that order."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    registry = _make_one_active_skill(skills_dir)
    curator = CuratorStateRepo(store.conn)
    signals = SignalsRepo(store.conn)

    await maybe_run_curator(
        profile_slug="default",
        registry=registry,
        curator_repo=curator,
        signals_repo=signals,
        now=FIXED_NOW,
    )

    rows = await _all_signals(signals)
    kinds = [r.event_kind for r in rows]
    assert EVENT_IDLE_REFLECTION in kinds
    assert EVENT_SKILL_UNUSED in kinds
    assert EVENT_CURATOR_RUN_COMPLETED in kinds
    # Idle must come before completed.
    assert kinds.index(EVENT_IDLE_REFLECTION) < kinds.index(EVENT_CURATOR_RUN_COMPLETED)
    # The unused signal carries the transition reason in its payload.
    unused = next(r for r in rows if r.event_kind == EVENT_SKILL_UNUSED)
    assert unused.target == "research_agent"
    assert unused.payload_json["from"] == "active"
    assert unused.payload_json["to"] == "stale"
    assert unused.payload_json["reason"] == "stale_threshold"


async def test_run_failure_emits_failed_signal_and_reraises(
    store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the pure transition pass crashes, the curator emits
    ``EVENT_CURATOR_RUN_FAILED`` with the error message in the payload
    and re-raises so the caller sees the failure."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    registry = _make_one_active_skill(skills_dir)
    curator = CuratorStateRepo(store.conn)
    signals = SignalsRepo(store.conn)

    # Force ``apply_lifecycle_transitions`` (looked up via the gateway
    # module so the monkey-patch hits the same symbol :func:`maybe_run_curator`
    # imports) to blow up.
    import corlinman_server.gateway.evolution.curator as curator_mod

    def _boom(*args, **kwargs):
        raise RuntimeError("synthetic curator failure")

    monkeypatch.setattr(curator_mod, "apply_lifecycle_transitions", _boom)

    with pytest.raises(RuntimeError, match="synthetic curator failure"):
        await maybe_run_curator(
            profile_slug="default",
            registry=registry,
            curator_repo=curator,
            signals_repo=signals,
            now=FIXED_NOW,
        )

    rows = await _all_signals(signals)
    kinds = [r.event_kind for r in rows]
    # Idle ran before the crash; completed must NOT be present.
    assert EVENT_IDLE_REFLECTION in kinds
    assert EVENT_CURATOR_RUN_FAILED in kinds
    assert EVENT_CURATOR_RUN_COMPLETED not in kinds
    failed = next(r for r in rows if r.event_kind == EVENT_CURATOR_RUN_FAILED)
    assert "synthetic curator failure" in failed.payload_json["error"]


async def test_dry_run_does_not_mark_run(store, tmp_path: Path) -> None:
    """A dry-run preview must NOT bump ``last_review_at`` — the operator
    may want to apply for real within the same interval window."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    registry = _make_one_active_skill(skills_dir)
    curator = CuratorStateRepo(store.conn)
    signals = SignalsRepo(store.conn)
    seed_review_at = FIXED_NOW - timedelta(hours=200)
    await curator.upsert(_state(last_review_at=seed_review_at, interval_hours=168))

    report = await maybe_run_curator(
        profile_slug="default",
        registry=registry,
        curator_repo=curator,
        signals_repo=signals,
        now=FIXED_NOW,
        dry_run=True,
    )

    assert report is not None
    assert report.marked_stale == 1
    # mark_run NOT called → last_review_at and run_count untouched.
    persisted = await curator.get("default")
    assert persisted.last_review_at == seed_review_at
    assert persisted.run_count == 0
