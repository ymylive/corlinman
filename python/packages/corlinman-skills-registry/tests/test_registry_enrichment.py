"""Tests for the W4 lifecycle wiring on :class:`SkillRegistry`.

Coverage:
  * ``path_for`` returns the directory holding the SKILL.md
  * ``usage_for`` reads the sidecar (empty default, populated round-trip)
  * ``bump_use`` / ``bump_view`` / ``bump_patch`` route through the right
    sidecar and persist to disk
  * ``bundled=True`` promotes legacy SKILL.md (no explicit origin) to
    ``origin="bundled"``; explicit frontmatter overrides the bundle flag
  * ``created_at`` is back-filled from the sidecar when the SKILL.md
    doesn't carry it
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from corlinman_skills_registry import (
    Skill,
    SkillRegistry,
    SkillUsage,
    bump_use as raw_bump_use,
    read_usage,
    write_usage,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_skill(dir_path: Path, name: str, *, extra_frontmatter: str = "") -> Path:
    """Drop a minimal SKILL.md into ``dir_path/<name>/`` and return the
    skill directory. Mirrors hermes' nested layout so we exercise the
    ``path_for`` resolver against a realistic shape.
    """
    skill_dir = dir_path / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    text = f"---\nname: {name}\ndescription: d\n{extra_frontmatter}---\nbody\n"
    (skill_dir / "SKILL.md").write_text(text, encoding="utf-8")
    return skill_dir


# ---------------------------------------------------------------------------
# path_for
# ---------------------------------------------------------------------------


def test_path_for_returns_skill_directory(tmp_path: Path) -> None:
    """``path_for`` resolves to the *directory* (not the SKILL.md path) so
    the sidecar + sibling assets all anchor off it."""
    skill_dir = _write_skill(tmp_path, "alpha")
    reg = SkillRegistry.load_from_dir(tmp_path)

    assert reg.path_for("alpha") == skill_dir


def test_path_for_unknown_skill_returns_none(tmp_path: Path) -> None:
    """Asking for a skill that isn't in the registry returns ``None`` so
    callers can ignore stale references without raising."""
    reg = SkillRegistry.load_from_dir(tmp_path)
    assert reg.path_for("ghost") is None


# ---------------------------------------------------------------------------
# usage_for
# ---------------------------------------------------------------------------


def test_usage_for_returns_empty_when_no_sidecar(tmp_path: Path) -> None:
    """Fresh skill with no usage → empty SkillUsage. Lifecycle code reads
    every skill on every tick; raising would force defensive try/except."""
    _write_skill(tmp_path, "alpha")
    reg = SkillRegistry.load_from_dir(tmp_path)

    assert reg.usage_for("alpha") == SkillUsage()


def test_usage_for_round_trips_disk_sidecar(tmp_path: Path) -> None:
    """A sidecar written to disk before registry load is visible through
    ``usage_for`` after load."""
    skill_dir = _write_skill(tmp_path, "alpha")
    now = datetime(2026, 5, 17, 9, 0, tzinfo=timezone.utc)
    write_usage(skill_dir, SkillUsage(use_count=7, last_used_at=now, created_at=now))

    reg = SkillRegistry.load_from_dir(tmp_path)
    usage = reg.usage_for("alpha")

    assert usage.use_count == 7
    assert usage.last_used_at == now


def test_usage_for_unknown_skill_returns_empty(tmp_path: Path) -> None:
    reg = SkillRegistry.load_from_dir(tmp_path)
    assert reg.usage_for("ghost") == SkillUsage()


# ---------------------------------------------------------------------------
# bump_* through the registry
# ---------------------------------------------------------------------------


def test_registry_bump_use_persists(tmp_path: Path) -> None:
    """``SkillRegistry.bump_use`` writes through to disk so the next
    ``usage_for`` (or a fresh registry load) sees the new counter."""
    _write_skill(tmp_path, "alpha")
    reg = SkillRegistry.load_from_dir(tmp_path)

    t = datetime(2026, 5, 1, tzinfo=timezone.utc)
    result = reg.bump_use("alpha", now=t)

    assert result is not None
    assert result.use_count == 1
    assert result.last_used_at == t
    # Fresh read from disk sees the same.
    assert reg.usage_for("alpha").use_count == 1


def test_registry_bump_patch_persists(tmp_path: Path) -> None:
    """``bump_patch`` is the curator's hook after a SKILL body rewrite."""
    _write_skill(tmp_path, "alpha")
    reg = SkillRegistry.load_from_dir(tmp_path)

    t = datetime(2026, 5, 17, tzinfo=timezone.utc)
    reg.bump_use("alpha", now=t)
    reg.bump_patch("alpha", now=t)

    usage = reg.usage_for("alpha")
    assert usage.use_count == 1
    assert usage.patch_count == 1
    assert usage.last_patched_at == t


def test_registry_bump_view_persists(tmp_path: Path) -> None:
    _write_skill(tmp_path, "alpha")
    reg = SkillRegistry.load_from_dir(tmp_path)

    t = datetime(2026, 5, 17, tzinfo=timezone.utc)
    reg.bump_view("alpha", now=t)

    usage = reg.usage_for("alpha")
    assert usage.view_count == 1
    assert usage.last_viewed_at == t


def test_registry_bump_unknown_skill_returns_none(tmp_path: Path) -> None:
    """Bumping a non-existent skill returns ``None`` without writing
    anything to disk (no rogue sidecar in the registry root)."""
    reg = SkillRegistry.load_from_dir(tmp_path)
    assert reg.bump_use("ghost") is None
    assert reg.bump_view("ghost") is None
    assert reg.bump_patch("ghost") is None
    # No stray ``.usage.json`` got created.
    assert not (tmp_path / ".usage.json").exists()


# ---------------------------------------------------------------------------
# Bundled inference
# ---------------------------------------------------------------------------


def test_bundled_flag_promotes_legacy_origin(tmp_path: Path) -> None:
    """Skills loaded from a bundled root that don't carry an explicit
    ``origin`` get promoted to ``"bundled"`` so curator filters work
    immediately on legacy fixtures."""
    _write_skill(tmp_path, "shipped")
    reg = SkillRegistry.load_from_dir(tmp_path, bundled=True)

    skill = reg.get("shipped")
    assert skill is not None
    assert skill.origin == "bundled"


def test_bundled_flag_respects_explicit_origin(tmp_path: Path) -> None:
    """Even with ``bundled=True``, a SKILL.md that explicitly declares
    ``origin: agent-created`` wins — the bundle flag is only an inference
    hint, not an override."""
    _write_skill(
        tmp_path,
        "rebellious",
        extra_frontmatter="origin: agent-created\n",
    )
    reg = SkillRegistry.load_from_dir(tmp_path, bundled=True)

    skill = reg.get("rebellious")
    assert skill is not None
    assert skill.origin == "agent-created"


def test_non_bundled_load_keeps_user_requested_default(tmp_path: Path) -> None:
    """Without the ``bundled=True`` opt-in, a legacy SKILL.md defaults to
    ``user-requested`` — the safe assumption for user-authored skills."""
    _write_skill(tmp_path, "mine")
    reg = SkillRegistry.load_from_dir(tmp_path)

    skill = reg.get("mine")
    assert skill is not None
    assert skill.origin == "user-requested"


# ---------------------------------------------------------------------------
# created_at backfill
# ---------------------------------------------------------------------------


def test_created_at_backfilled_from_sidecar(tmp_path: Path) -> None:
    """When SKILL.md doesn't carry ``created_at`` but the sidecar does,
    the registry fills it in so the model has an anchor for lifecycle
    code without needing to rewrite the file."""
    skill_dir = _write_skill(tmp_path, "alpha")
    anchor = datetime(2025, 12, 1, tzinfo=timezone.utc)
    write_usage(skill_dir, SkillUsage(created_at=anchor))

    reg = SkillRegistry.load_from_dir(tmp_path)
    skill = reg.get("alpha")

    assert skill is not None
    assert skill.created_at == anchor


def test_explicit_frontmatter_created_at_wins(tmp_path: Path) -> None:
    """If SKILL.md does carry ``created_at``, the sidecar value isn't used
    to overwrite it — frontmatter is the canonical source."""
    skill_dir = _write_skill(
        tmp_path,
        "alpha",
        extra_frontmatter="created_at: 2026-01-01T00:00:00+00:00\n",
    )
    # Conflicting sidecar value — should be ignored for ``created_at``.
    write_usage(
        skill_dir,
        SkillUsage(created_at=datetime(2025, 1, 1, tzinfo=timezone.utc)),
    )

    reg = SkillRegistry.load_from_dir(tmp_path)
    skill = reg.get("alpha")

    assert skill is not None
    assert skill.created_at == datetime(2026, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Sanity: raw helpers still work alongside registry helpers
# ---------------------------------------------------------------------------


def test_raw_helpers_and_registry_share_sidecar(tmp_path: Path) -> None:
    """The free functions and registry methods point at the same file on
    disk, so background processes (the curator's fork) can bump counters
    without holding a registry handle."""
    skill_dir = _write_skill(tmp_path, "alpha")
    reg = SkillRegistry.load_from_dir(tmp_path)

    t = datetime(2026, 5, 17, tzinfo=timezone.utc)
    raw_bump_use(skill_dir, now=t)

    # Registry sees the raw bump without a reload.
    assert reg.usage_for("alpha").use_count == 1
    # And the raw read agrees with the registry view.
    assert read_usage(skill_dir).use_count == 1


def test_skill_lifecycle_fields_present_on_loaded_skill(tmp_path: Path) -> None:
    """After load, the Skill object exposes every W4 lifecycle field —
    pinned defaults to False, state to active, version to 1.0.0."""
    _write_skill(tmp_path, "alpha")
    reg = SkillRegistry.load_from_dir(tmp_path)

    skill = reg.get("alpha")
    assert isinstance(skill, Skill)
    assert skill.version == "1.0.0"
    assert skill.state == "active"
    assert skill.pinned is False
