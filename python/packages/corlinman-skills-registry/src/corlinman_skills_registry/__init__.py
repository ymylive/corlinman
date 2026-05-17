"""``corlinman-skills-registry`` — registry for openclaw-style skill markdown files.

A **skill** is a small Markdown file with YAML frontmatter describing its
identity, the runtime prerequisites it needs (binaries, config keys, env
vars), and the tools it is permitted to invoke. The body is the prose the
context assembler injects into an agent's prompt when the skill is
referenced by a session manifest.

This package is intentionally passive: it parses files off disk and exposes
lookups. Wiring into the context assembler / gateway happens elsewhere; the
registry here is the data source that step will consume.

Python port of the Rust ``corlinman-skills`` crate. Public surface mirrors
the crate 1:1:

* :class:`Skill`              — parsed SKILL.md file
* :class:`SkillRequirements`  — runtime prerequisite lists
* :class:`SkillRegistry`      — in-memory lookup table loaded from a dir
* :class:`SkillLoadError`     — common base class for every load failure
* :class:`SkillIoError`       — filesystem IO failure
* :class:`YamlParseError`     — malformed YAML frontmatter
* :class:`MissingFieldError`  — required field missing/empty
* :class:`DuplicateNameError` — two files declared the same ``name``

W4 lifecycle additions (curator port from hermes):

* :class:`SkillUsage`         — per-skill ``.usage.json`` sidecar record
* ``SkillOrigin`` / ``SkillState`` — narrow string types for lifecycle
* :func:`render_skill_frontmatter`, :func:`write_skill_md`
                              — round-trip writers for SKILL.md
* :func:`read_usage`, :func:`write_usage`,
  :func:`bump_use`, :func:`bump_view`, :func:`bump_patch`
                              — usage sidecar I/O
"""

from corlinman_skills_registry.errors import (
    DuplicateNameError,
    MissingFieldError,
    SkillIoError,
    SkillLoadError,
    YamlParseError,
)
from corlinman_skills_registry.parse import (
    render_skill_frontmatter,
    write_skill_md,
)
from corlinman_skills_registry.registry import SkillRegistry
from corlinman_skills_registry.skill import (
    Skill,
    SkillOrigin,
    SkillRequirements,
    SkillState,
)
from corlinman_skills_registry.usage import (
    USAGE_FILENAME,
    SkillUsage,
    bump_patch,
    bump_use,
    bump_view,
    read_usage,
    usage_path,
    write_usage,
)

__all__ = [
    "DuplicateNameError",
    "MissingFieldError",
    "Skill",
    "SkillIoError",
    "SkillLoadError",
    "SkillOrigin",
    "SkillRegistry",
    "SkillRequirements",
    "SkillState",
    "SkillUsage",
    "USAGE_FILENAME",
    "YamlParseError",
    "bump_patch",
    "bump_use",
    "bump_view",
    "read_usage",
    "render_skill_frontmatter",
    "usage_path",
    "write_skill_md",
    "write_usage",
]
