"""Tests for :class:`corlinman_server.profiles.ProfileStore`.

Covers:
* Happy-path create → exists → get → list round-trip.
* Duplicate slug → :class:`ProfileExists`.
* Slug validation failures → :class:`ProfileSlugInvalid`.
* The reserved ``default`` slug refusing deletion.
* ``clone_from`` actually copies the parent's SOUL.md content.
* :func:`ensure_profile_dirs` materialises the ``skills/`` subdir.

All tests use ``tmp_path`` so they're parallel-safe.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from corlinman_server.profiles import (
    Profile,
    ProfileExists,
    ProfileNotFound,
    ProfileProtected,
    ProfileSlugInvalid,
    ProfileStore,
    ensure_profile_dirs,
    profile_root,
    profile_skills_dir,
    profile_soul_path,
)


# ---------------------------------------------------------------------------
# paths.ensure_profile_dirs
# ---------------------------------------------------------------------------


def test_ensure_profile_dirs_creates_skills_subdir(tmp_path: Path) -> None:
    """The ``skills/`` subdir is materialised on first create."""
    ensure_profile_dirs(tmp_path, "alpha")
    assert profile_skills_dir(tmp_path, "alpha").is_dir()
    # SOUL/MEMORY/USER placeholders also exist.
    for name in ("SOUL.md", "MEMORY.md", "USER.md"):
        f = profile_root(tmp_path, "alpha") / name
        assert f.is_file()
        assert f.read_text(encoding="utf-8") == ""


def test_ensure_profile_dirs_is_idempotent(tmp_path: Path) -> None:
    """Re-running on an existing profile keeps existing file contents."""
    ensure_profile_dirs(tmp_path, "alpha")
    soul = profile_soul_path(tmp_path, "alpha")
    soul.write_text("# Alpha persona\n", encoding="utf-8")
    ensure_profile_dirs(tmp_path, "alpha")
    assert soul.read_text(encoding="utf-8") == "# Alpha persona\n"


# ---------------------------------------------------------------------------
# ProfileStore happy path
# ---------------------------------------------------------------------------


def test_create_exists_get_list_round_trip(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "profiles")
    try:
        assert store.list() == []
        assert not store.exists("research")

        profile = store.create(slug="research", display_name="Research Bot")
        assert isinstance(profile, Profile)
        assert profile.slug == "research"
        assert profile.display_name == "Research Bot"
        assert profile.parent_slug is None
        assert store.exists("research")

        fetched = store.get("research")
        assert fetched is not None
        assert fetched.slug == "research"
        assert fetched.display_name == "Research Bot"

        assert [p.slug for p in store.list()] == ["research"]
    finally:
        store.close()


def test_get_missing_returns_none(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "profiles")
    try:
        assert store.get("nope") is None
    finally:
        store.close()


def test_create_defaults_display_name_to_slug(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "profiles")
    try:
        p = store.create(slug="alpha")
        assert p.display_name == "alpha"
    finally:
        store.close()


def test_create_materialises_directory(tmp_path: Path) -> None:
    """A successful ``create`` leaves the profile dir + skills subdir
    + empty placeholder files on disk."""
    store = ProfileStore(tmp_path / "profiles")
    try:
        store.create(slug="alpha")
        root = profile_root(store.data_dir, "alpha")
        assert root.is_dir()
        assert (root / "skills").is_dir()
        assert (root / "SOUL.md").is_file()
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_duplicate_create_raises_profile_exists(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "profiles")
    try:
        store.create(slug="dup")
        with pytest.raises(ProfileExists):
            store.create(slug="dup")
    finally:
        store.close()


@pytest.mark.parametrize(
    "bad_slug",
    [
        "UpperCase",       # uppercase letters not allowed
        "with space",      # whitespace
        "",                # empty
        "-leading-dash",   # must start with [a-z0-9]
        "_leading_under",  # must start with [a-z0-9]
        "with.dot",        # dot not allowed
        "x" * 65,          # too long (max 64)
        "ünicode",         # non-ASCII
    ],
)
def test_invalid_slugs_raise_profile_slug_invalid(
    tmp_path: Path, bad_slug: str
) -> None:
    store = ProfileStore(tmp_path / "profiles")
    try:
        with pytest.raises(ProfileSlugInvalid):
            store.create(slug=bad_slug)
    finally:
        store.close()


def test_delete_default_raises_profile_protected(tmp_path: Path) -> None:
    """The reserved ``default`` slug cannot be deleted via the store API."""
    store = ProfileStore(tmp_path / "profiles")
    try:
        store.create(slug="default", display_name="Default")
        with pytest.raises(ProfileProtected):
            store.delete("default")
        # Row still exists.
        assert store.exists("default")
    finally:
        store.close()


def test_delete_missing_returns_false(tmp_path: Path) -> None:
    """Deleting a non-existent slug is idempotent — returns False."""
    store = ProfileStore(tmp_path / "profiles")
    try:
        assert store.delete("never-existed") is False
    finally:
        store.close()


def test_delete_removes_row_and_directory(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "profiles")
    try:
        store.create(slug="ephemeral")
        root = profile_root(store.data_dir, "ephemeral")
        assert root.exists()
        assert store.delete("ephemeral") is True
        assert not store.exists("ephemeral")
        assert not root.exists()
    finally:
        store.close()


def test_clone_from_missing_parent_raises_not_found(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "profiles")
    try:
        with pytest.raises(ProfileNotFound):
            store.create(slug="child", parent_slug="ghost")
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Clone behaviour
# ---------------------------------------------------------------------------


def test_clone_copies_parent_soul_content(tmp_path: Path) -> None:
    """``clone_from`` materialises the child with the parent's SOUL.md
    content (the canonical hermes pattern)."""
    store = ProfileStore(tmp_path / "profiles")
    try:
        store.create(slug="default", display_name="Default")
        parent_soul = profile_soul_path(store.data_dir, "default")
        parent_soul.write_text("# Parent persona\nLine 2\n", encoding="utf-8")

        child = store.create(slug="cloned", parent_slug="default")
        assert child.parent_slug == "default"
        child_soul = profile_soul_path(store.data_dir, "cloned")
        assert child_soul.read_text(encoding="utf-8") == "# Parent persona\nLine 2\n"
    finally:
        store.close()


def test_clone_copies_parent_skills_recursively(tmp_path: Path) -> None:
    """``skills/`` subtree is copied on clone."""
    store = ProfileStore(tmp_path / "profiles")
    try:
        store.create(slug="default")
        parent_skills = profile_skills_dir(store.data_dir, "default")
        skill = parent_skills / "echo"
        skill.mkdir()
        (skill / "SKILL.md").write_text("# echo skill\n", encoding="utf-8")

        store.create(slug="child", parent_slug="default")
        child_skill = profile_skills_dir(store.data_dir, "child") / "echo" / "SKILL.md"
        assert child_skill.is_file()
        assert child_skill.read_text(encoding="utf-8") == "# echo skill\n"
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Update / rename
# ---------------------------------------------------------------------------


def test_rename_updates_display_name(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "profiles")
    try:
        store.create(slug="alpha", display_name="Alpha")
        renamed = store.rename("alpha", "Alpha Prime")
        assert renamed.display_name == "Alpha Prime"
        # Round-trip via get.
        assert store.get("alpha").display_name == "Alpha Prime"  # type: ignore[union-attr]
    finally:
        store.close()


def test_rename_missing_raises_not_found(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "profiles")
    try:
        with pytest.raises(ProfileNotFound):
            store.rename("ghost", "Whatever")
    finally:
        store.close()


def test_update_patches_description_only(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "profiles")
    try:
        store.create(slug="alpha", display_name="Alpha")
        updated = store.update("alpha", description="Now with a blurb")
        assert updated.display_name == "Alpha"
        assert updated.description == "Now with a blurb"
    finally:
        store.close()


def test_update_no_op_returns_current(tmp_path: Path) -> None:
    """An empty patch returns the current row unchanged."""
    store = ProfileStore(tmp_path / "profiles")
    try:
        store.create(slug="alpha", display_name="Alpha")
        result = store.update("alpha")
        assert result.slug == "alpha"
        assert result.display_name == "Alpha"
    finally:
        store.close()
