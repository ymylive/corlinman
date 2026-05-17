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
"""

from corlinman_skills_registry.errors import (
    DuplicateNameError,
    MissingFieldError,
    SkillIoError,
    SkillLoadError,
    YamlParseError,
)
from corlinman_skills_registry.registry import SkillRegistry
from corlinman_skills_registry.skill import Skill, SkillRequirements

__all__ = [
    "DuplicateNameError",
    "MissingFieldError",
    "Skill",
    "SkillIoError",
    "SkillLoadError",
    "SkillRegistry",
    "SkillRequirements",
    "YamlParseError",
]
