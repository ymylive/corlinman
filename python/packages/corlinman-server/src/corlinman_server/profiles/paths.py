"""On-disk layout helpers for profile directories.

A *profile* is an isolated agent instance with its own persona, memory,
skills, and state. Borrowed from hermes-agent's ``~/.hermes/profiles/<name>/``
layout — see ``/tmp/hermes-agent-shallow/hermes_constants.py`` and the
plan in ``docs/PLAN_EASY_SETUP.md`` §1.

Layout under ``<data_dir>/profiles/<slug>/``::

    SOUL.md       — persona document (markdown)
    MEMORY.md     — distilled agent memory (markdown, ~2k chars)
    USER.md       — user-facts memory (markdown, ~1k chars)
    state.db      — per-profile SQLite for session / conversation state
    skills/       — per-profile skill directory (mirrors SKILL.md format)

The functions in this module are pure path computations — they do not
perform any I/O except :func:`ensure_profile_dirs`, which creates the
directory tree + empty placeholders. The :class:`ProfileStore` (see
``store.py``) is the *only* mutator of slug→Profile rows; ``paths`` is
deliberately stateless so tests can use it without spinning up SQLite.

Slug validation
---------------

Mirrors the hermes pattern (`web/src/pages/ProfilesPage.tsx` regex):

* lowercase only — no case-folding ambiguity on case-sensitive FSes
* alphanumeric + ``-`` / ``_`` — safe across NTFS / APFS / ext4
* first char must be alphanumeric — avoids leading-dash CLI confusion
* length 1..64 — short enough for paths, long enough for descriptive names

The reserved slug ``"default"`` is enforced separately by the
:class:`ProfileStore` (not in :func:`validate_slug`) so the validator
stays a pure regex test.
"""

from __future__ import annotations

import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Slug validation
# ---------------------------------------------------------------------------

SLUG_REGEX: re.Pattern[str] = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
"""Acceptable profile slug pattern. See module docstring for rationale."""


def validate_slug(slug: str) -> None:
    """Raise ``ValueError`` when ``slug`` doesn't match :data:`SLUG_REGEX`.

    Returns ``None`` on success. The caller layers HTTP / domain
    semantics on top (e.g., :class:`ProfileSlugInvalid` in
    ``store.py``).
    """
    if not isinstance(slug, str):
        raise ValueError(
            f"profile slug must be str, got {type(slug).__name__}"
        )
    if not SLUG_REGEX.match(slug):
        raise ValueError(
            f"profile slug {slug!r} does not match {SLUG_REGEX.pattern!r} "
            "(lowercase alphanumeric + '-'/'_' only, 1..64 chars, "
            "must start with alphanumeric)"
        )


# ---------------------------------------------------------------------------
# Path computations
# ---------------------------------------------------------------------------


def profile_root(data_dir: Path, slug: str) -> Path:
    """Return ``<data_dir>/profiles/<slug>/`` (does not touch disk)."""
    return Path(data_dir) / "profiles" / slug


def profile_soul_path(data_dir: Path, slug: str) -> Path:
    """Persona markdown — ``<profile_root>/SOUL.md``."""
    return profile_root(data_dir, slug) / "SOUL.md"


def profile_memory_path(data_dir: Path, slug: str) -> Path:
    """Agent-distilled memory — ``<profile_root>/MEMORY.md``."""
    return profile_root(data_dir, slug) / "MEMORY.md"


def profile_user_path(data_dir: Path, slug: str) -> Path:
    """User-facts memory — ``<profile_root>/USER.md``."""
    return profile_root(data_dir, slug) / "USER.md"


def profile_state_db(data_dir: Path, slug: str) -> Path:
    """Per-profile session/conversation SQLite — ``<profile_root>/state.db``."""
    return profile_root(data_dir, slug) / "state.db"


def profile_skills_dir(data_dir: Path, slug: str) -> Path:
    """Per-profile skills directory — ``<profile_root>/skills/``."""
    return profile_root(data_dir, slug) / "skills"


# ---------------------------------------------------------------------------
# Filesystem materialisation
# ---------------------------------------------------------------------------

_EMPTY_FILES: tuple[str, ...] = ("SOUL.md", "MEMORY.md", "USER.md")
"""Placeholder markdown files that exist for every profile (even when empty).

Keeping these on disk simplifies the UI: the editor can always open a
file, the agent can always read one. The :class:`ProfileStore.create`
path either copies the parent's content or writes an empty string.
"""


def ensure_profile_dirs(data_dir: Path, slug: str) -> Path:
    """Create the profile directory tree + empty placeholder files.

    Idempotent — re-running on an existing profile is a no-op (does not
    overwrite existing files). Returns the profile root path so callers
    can chain further operations.

    The ``skills/`` subdirectory is created here so tests can rely on
    its existence; placeholder markdown files are created empty so the
    editor surface can always open them. Caller is responsible for slug
    validation upstream (see :func:`validate_slug`).
    """
    root = profile_root(data_dir, slug)
    root.mkdir(parents=True, exist_ok=True)
    profile_skills_dir(data_dir, slug).mkdir(parents=True, exist_ok=True)
    for name in _EMPTY_FILES:
        f = root / name
        if not f.exists():
            f.write_text("", encoding="utf-8")
    return root


__all__ = [
    "SLUG_REGEX",
    "ensure_profile_dirs",
    "profile_memory_path",
    "profile_root",
    "profile_skills_dir",
    "profile_soul_path",
    "profile_state_db",
    "profile_user_path",
    "validate_slug",
]
