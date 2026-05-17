"""Hand-rolled splitter for ``---`` YAML frontmatter + Markdown body.

We deliberately avoid a dedicated frontmatter library: the format is trivial
and we want **verbatim body preservation** (leading/trailing whitespace
intact) for downstream prompt injection. Mirrors the Rust ``parse`` module
behaviour byte-for-byte so the test suites can share fixtures.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .errors import MissingFieldError, YamlParseError
from .skill import Skill, SkillRequirements


def split_frontmatter(text: str) -> tuple[str, str] | None:
    """Split ``text`` into ``(yaml_str, body_str)``.

    Returns ``None`` if the file does not start with a ``---`` frontmatter
    fence. Recognised fence: a line that is exactly ``---`` (optionally
    followed by ``\\r``). The opening fence MUST be the very first line of
    the file — same rule as the Rust implementation.
    """
    if text.startswith("---\n"):
        rest = text[len("---\n") :]
    elif text.startswith("---\r\n"):
        rest = text[len("---\r\n") :]
    else:
        return None

    # Walk lines (keeping their terminators) looking for a closing `---`.
    offset = 0
    # ``splitlines(keepends=True)`` preserves \n / \r\n / etc. on each line,
    # which is what we need to track byte offsets the way the Rust
    # ``split_inclusive('\n')`` iterator does.
    for line in rest.splitlines(keepends=True):
        trimmed = line.rstrip("\r\n")
        if trimmed == "---":
            yaml_str = rest[:offset]
            body_start = offset + len(line)
            body = rest[body_start:]
            return yaml_str, body
        offset += len(line)
    return None


def _required_non_empty(value: Any, path: Path, field: str) -> str:
    """Return ``value`` if it is a non-empty/non-whitespace string; otherwise
    raise :class:`MissingFieldError` with the same wording the Rust crate
    emits.
    """
    if isinstance(value, str) and value.strip():
        return value
    raise MissingFieldError(path=path, field=field)


def _coerce_str_list(value: Any) -> list[str]:
    """Lenient coercion for YAML list fields.

    The Rust code uses serde defaults which silently fall back to ``vec![]``
    when a key is missing; we mirror that for missing/``None`` here and
    fail-soft (empty list) for non-list shapes — invalid YAML structures are
    rejected upstream by :func:`yaml.safe_load`.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def parse_skill(source_path: Path, text: str) -> Skill:
    """Parse a single skill file's raw text into a :class:`Skill`.

    Raises :class:`MissingFieldError` if the frontmatter fence is absent or
    a required field (``name`` / ``description``) is missing/empty.
    Raises :class:`YamlParseError` if the frontmatter is malformed YAML.
    """
    split = split_frontmatter(text)
    if split is None:
        raise MissingFieldError(path=source_path, field="frontmatter")
    yaml_str, body = split

    try:
        raw: Any = yaml.safe_load(yaml_str) if yaml_str.strip() else {}
    except yaml.YAMLError as err:
        raise YamlParseError(path=source_path, err=err) from err

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        # The Rust deserializer would reject this as "invalid type";
        # surface it as a YAML parse error for the same callsite shape.
        raise YamlParseError(
            path=source_path,
            err=TypeError(f"frontmatter must be a mapping, got {type(raw).__name__}"),
        )

    name = _required_non_empty(raw.get("name"), source_path, "name")
    description = _required_non_empty(raw.get("description"), source_path, "description")

    metadata = raw.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    openclaw = metadata.get("openclaw") or {}
    if not isinstance(openclaw, dict):
        openclaw = {}

    requires_raw = openclaw.get("requires") or {}
    if not isinstance(requires_raw, dict):
        requires_raw = {}

    requires = SkillRequirements(
        bins=_coerce_str_list(requires_raw.get("bins")),
        # Rust uses ``rename = "anyBins"`` — accept the camelCase YAML key.
        any_bins=_coerce_str_list(requires_raw.get("anyBins")),
        config=_coerce_str_list(requires_raw.get("config")),
        env=_coerce_str_list(requires_raw.get("env")),
    )

    emoji_raw = openclaw.get("emoji")
    emoji: str | None = emoji_raw if isinstance(emoji_raw, str) else None

    install_raw = openclaw.get("install")
    install: str | None = install_raw if isinstance(install_raw, str) else None

    # Rust uses ``rename = "allowed-tools"`` — accept the kebab-case YAML key.
    allowed_tools = _coerce_str_list(raw.get("allowed-tools"))

    return Skill(
        name=name,
        description=description,
        emoji=emoji,
        requires=requires,
        install=install,
        allowed_tools=allowed_tools,
        body_markdown=body,
        source_path=source_path,
    )


__all__ = ["parse_skill", "split_frontmatter"]
