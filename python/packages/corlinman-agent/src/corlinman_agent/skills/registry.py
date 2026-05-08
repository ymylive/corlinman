"""In-memory skill registry — Python mirror of the Rust
``corlinman-skills`` crate's ``SkillRegistry``.

The loader walks a directory tree looking for ``*.md`` files, splits
each on the ``---`` YAML frontmatter fence, parses the frontmatter, and
keeps the Markdown body verbatim. Duplicate ``name`` fields across
files are a hard error — two skills cannot share an identifier.

The ``check_requirements`` path is deliberately synchronous and cheap:
the context assembler calls it once per skill-ref during every prompt
assembly, so any dependency on async I/O here would fan out into every
request.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from corlinman_agent.skills.card import Skill, SkillRequirements


class SkillLoadError(RuntimeError):
    """Raised when a ``*.md`` file under the skills root is unparseable
    or missing a required field. The offending path is included so
    operators can locate it without re-running the loader."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


def _split_frontmatter(text: str) -> tuple[str, str] | None:
    """Split ``text`` into ``(yaml, body)``.

    Returns ``None`` if the file does not start with a ``---`` fence.
    The closing fence is a line that is exactly ``---`` (CR-LF tolerated
    on both delimiters, since Windows checkouts happen). The body is
    everything after the closing fence line, preserved verbatim.
    """
    # Normalise Windows newlines on the opening fence; keep the body
    # with whatever line endings it had so round-tripping stays faithful.
    if text.startswith("---\n"):
        rest = text[len("---\n"):]
    elif text.startswith("---\r\n"):
        rest = text[len("---\r\n"):]
    else:
        return None

    # Scan line-by-line for a closing `---`.
    offset = 0
    for line in rest.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        if stripped == "---":
            yaml_str = rest[:offset]
            body_start = offset + len(line)
            return yaml_str, rest[body_start:]
        offset += len(line)
    return None


def _as_str_list(value: Any, field_name: str, path: Path) -> list[str]:
    """Coerce an optional ``list[str]`` frontmatter field; reject
    non-list / non-str values so silent type drift can't smuggle bad
    data into requirements checks."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise SkillLoadError(path, f"{field_name} must be a list of strings")
    out: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            raise SkillLoadError(path, f"{field_name} entries must be strings")
        out.append(entry)
    return out


def _parse_requires(raw: Any, path: Path) -> SkillRequirements:
    """Parse the ``metadata.openclaw.requires`` block. Missing or
    ``None`` means an empty requirements set."""
    if raw is None:
        return SkillRequirements()
    if not isinstance(raw, dict):
        raise SkillLoadError(path, "metadata.openclaw.requires must be a mapping")
    return SkillRequirements(
        bins=_as_str_list(raw.get("bins"), "requires.bins", path),
        # Match the Rust rename: YAML uses camelCase ``anyBins``.
        any_bins=_as_str_list(raw.get("anyBins"), "requires.anyBins", path),
        config=_as_str_list(raw.get("config"), "requires.config", path),
        env=_as_str_list(raw.get("env"), "requires.env", path),
    )


def _parse_skill(path: Path, text: str) -> Skill:
    """Parse one ``SKILL.md`` file's raw text into a :class:`Skill`.

    Mirrors the Rust parser's field layout so the two implementations
    agree on wire format: ``name`` and ``description`` at the top
    level; ``emoji`` / ``requires`` / ``install`` under
    ``metadata.openclaw``; ``allowed-tools`` at the top level.
    """
    split = _split_frontmatter(text)
    if split is None:
        raise SkillLoadError(path, "missing YAML frontmatter (expected leading '---' fence)")
    yaml_str, body = split

    try:
        raw = yaml.safe_load(yaml_str) if yaml_str.strip() else {}
    except yaml.YAMLError as exc:
        raise SkillLoadError(path, f"yaml parse error: {exc}") from exc

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise SkillLoadError(path, "frontmatter must be a mapping")

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise SkillLoadError(path, "name is required and must be a non-empty string")

    description = raw.get("description")
    if not isinstance(description, str) or not description.strip():
        raise SkillLoadError(path, "description is required and must be a non-empty string")

    metadata = raw.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise SkillLoadError(path, "metadata must be a mapping")
    openclaw = metadata.get("openclaw") or {}
    if not isinstance(openclaw, dict):
        raise SkillLoadError(path, "metadata.openclaw must be a mapping")

    emoji = openclaw.get("emoji")
    if emoji is not None and not isinstance(emoji, str):
        raise SkillLoadError(path, "metadata.openclaw.emoji must be a string")

    install = openclaw.get("install")
    if install is not None and not isinstance(install, str):
        raise SkillLoadError(path, "metadata.openclaw.install must be a string")

    requires = _parse_requires(openclaw.get("requires"), path)
    # Rust uses the rename ``allowed-tools``; keep that on the wire.
    allowed_tools = _as_str_list(raw.get("allowed-tools"), "allowed-tools", path)

    return Skill(
        name=name,
        description=description,
        emoji=emoji,
        requires=requires,
        install=install,
        allowed_tools=allowed_tools,
        body_markdown=body,
        source_path=path,
    )


class SkillRegistry:
    """Read-only lookup over skills parsed from disk.

    Duplicate skill names across files raise :class:`SkillLoadError`
    immediately — silent last-wins behaviour produces hard-to-debug
    "why did my skill change?" tickets.
    """

    def __init__(self, skills: dict[str, Skill] | None = None) -> None:
        self._skills: dict[str, Skill] = skills or {}

    @classmethod
    def load_from_dir(cls, root: Path) -> SkillRegistry:
        """Walk ``root`` recursively and parse every ``*.md`` file.

        Non-existent roots yield an empty registry (lets operators start
        with no skills configured). A path that exists but isn't a
        directory is a configuration error and raises.
        """
        skills: dict[str, Skill] = {}
        if not root.exists():
            return cls(skills)
        if not root.is_dir():
            raise SkillLoadError(root, "skills root must be a directory")

        # Deterministic traversal so duplicate errors are reproducible
        # across platforms.
        for path in sorted(root.rglob("*.md")):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            skill = _parse_skill(path, text)
            existing = skills.get(skill.name)
            if existing is not None:
                raise SkillLoadError(
                    path,
                    f"duplicate skill name {skill.name!r} "
                    f"(also defined in {existing.source_path})",
                )
            skills[skill.name] = skill
        return cls(skills)

    def get(self, name: str) -> Skill | None:
        """Return the skill for ``name`` or ``None`` if not registered."""
        return self._skills.get(name)

    def names(self) -> list[str]:
        """Sorted list of all registered skill names."""
        return sorted(self._skills.keys())

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._skills

    def __iter__(self):
        return iter(self._skills.values())

    def __len__(self) -> int:
        return len(self._skills)

    def check_requirements(
        self,
        skill_name: str,
        config_lookup: Callable[[str], str | None],
    ) -> list[str] | None:
        """Verify every requirement for ``skill_name``.

        Returns ``None`` when the skill can run. Otherwise returns a list
        of actionable human-readable problem messages, one per unmet
        requirement.

        ``config_lookup(key)`` should return ``Some(value)`` for a set,
        non-empty config key and ``None`` otherwise.

        Raises a :class:`KeyError`-like problem list if ``skill_name``
        isn't registered — the caller usually already resolved the skill
        via :meth:`get`, but we guard the method too so it stays safe to
        call standalone.
        """
        skill = self._skills.get(skill_name)
        if skill is None:
            return [f"skill '{skill_name}' is not registered"]

        problems: list[str] = []
        req = skill.requires

        for bin_name in req.bins:
            if shutil.which(bin_name) is None:
                problems.append(
                    f"skill '{skill.name}' requires binary '{bin_name}' on $PATH; "
                    "install it first"
                )

        if req.any_bins:
            any_ok = any(shutil.which(b) is not None for b in req.any_bins)
            if not any_ok:
                joined = ", ".join(req.any_bins)
                problems.append(
                    f"skill '{skill.name}' requires one of: {{{joined}}}; none found"
                )

        for key in req.config:
            value = config_lookup(key)
            present = isinstance(value, str) and value.strip() != ""
            if not present:
                problems.append(
                    f"skill '{skill.name}' requires config '{key}' to be set (non-empty)"
                )

        for var in req.env:
            env_val = os.environ.get(var)
            present = env_val is not None and env_val != ""
            if not present:
                problems.append(
                    f"skill '{skill.name}' requires env var '{var}' to be set"
                )

        return problems if problems else None


__all__ = ["SkillLoadError", "SkillRegistry"]
