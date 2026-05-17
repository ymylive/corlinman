"""Skill data model. Mirrors openclaw's SKILL.md frontmatter shape.

Implemented as pydantic ``BaseModel`` (v2) for parity with the rest of the
Python plane (see ``corlinman-providers/specs.py``). The Rust crate uses
plain structs because there is no validation runtime — pydantic gives us the
same field-level guarantees with the same field names.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# Public type aliases for lifecycle vocab. Kept narrow so static checkers
# can catch typos in callers; the hermes vocabulary is the source of truth
# (see ``tools/skill_usage.py:52-55`` and ``tools/skill_provenance.py``).
SkillOrigin = Literal["bundled", "user-requested", "agent-created"]
SkillState = Literal["active", "stale", "archived"]


class SkillRequirements(BaseModel):
    """Runtime prerequisites a skill needs before it can execute.

    Mirrors the Rust ``SkillRequirements`` struct field-for-field. All four
    lists default to empty so callers can omit any subset in YAML; an unmet
    item yields a human-readable message from
    :meth:`corlinman_skills_registry.SkillRegistry.check_requirements`.
    """

    model_config = ConfigDict(frozen=False, extra="ignore")

    bins: list[str] = Field(default_factory=list)
    """Every binary in this list must be found on ``$PATH``."""

    any_bins: list[str] = Field(default_factory=list)
    """At least one binary in this list must be found on ``$PATH``."""

    config: list[str] = Field(default_factory=list)
    """Dotted config keys (e.g. ``providers.brave.api_key``) that must
    resolve to a non-empty string via the caller-supplied lookup."""

    env: list[str] = Field(default_factory=list)
    """Environment variables that must be set to a non-empty value."""


class Skill(BaseModel):
    """A single skill parsed from a SKILL.md file on disk.

    Mirrors the Rust ``Skill`` struct: same field names, same semantics.
    ``source_path`` is always absolute when the registry constructs the
    instance (we resolve the walk root before recursing).
    """

    model_config = ConfigDict(frozen=False, extra="ignore", arbitrary_types_allowed=True)

    name: str
    """Unique identifier. Used to look the skill up from a manifest's
    ``skill_refs``."""

    description: str
    """Short human summary shown in listings."""

    emoji: str | None = None
    """Optional glyph used by the CLI/UI."""

    requires: SkillRequirements = Field(default_factory=SkillRequirements)
    """Runtime prerequisites."""

    install: str | None = None
    """Optional install hint surfaced when ``requires`` isn't satisfied."""

    allowed_tools: list[str] = Field(default_factory=list)
    """Tools this skill is allowed to invoke at runtime. Enforcement happens
    elsewhere; we just carry the list."""

    body_markdown: str = ""
    """The Markdown body (everything after the closing ``---`` of the
    frontmatter), preserved verbatim."""

    source_path: Path
    """Absolute path to the file this skill was loaded from."""

    # ------------------------------------------------------------------
    # Lifecycle metadata (W4 — hermes curator port)
    # ------------------------------------------------------------------
    # These fields ride in the SKILL.md frontmatter when present, but every
    # default is benign so legacy files load unchanged. The curator surface
    # is what actually mutates them; this package only carries the data and
    # round-trips it on write.

    version: str = "1.0.0"
    """SemVer version of this skill. Bumped on substantive edits by the
    curator's patch flow (see hermes ``agent/curator.py``)."""

    origin: SkillOrigin = "user-requested"
    """Provenance — only ``agent-created`` skills are eligible for the
    curator's autonomous lifecycle transitions. ``bundled`` skills ship
    with the repo, ``user-requested`` are hand-authored. Mirrors
    hermes ``tools/skill_usage.py:154-200``."""

    state: SkillState = "active"
    """Lifecycle state. Curator transitions: active → stale (30d idle) →
    archived (90d idle); stale → active on any re-use. See hermes
    ``agent/curator.py:256-296``."""

    pinned: bool = False
    """Operator can pin a skill so the curator never archives or rewrites
    it. Useful for hand-written skills that look unused-but-important."""

    created_at: datetime | None = None
    """ISO-8601 first-seen timestamp. Populated by the registry on initial
    load if SKILL.md doesn't carry it; persisted on next write. Stored as
    ``datetime`` in-memory and serialised as ISO-8601 by pydantic v2."""


__all__ = ["Skill", "SkillOrigin", "SkillRequirements", "SkillState"]
