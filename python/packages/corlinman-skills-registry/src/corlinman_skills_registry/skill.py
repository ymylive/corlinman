"""Skill data model. Mirrors openclaw's SKILL.md frontmatter shape.

Implemented as pydantic ``BaseModel`` (v2) for parity with the rest of the
Python plane (see ``corlinman-providers/specs.py``). The Rust crate uses
plain structs because there is no validation runtime — pydantic gives us the
same field-level guarantees with the same field names.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


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


__all__ = ["Skill", "SkillRequirements"]
