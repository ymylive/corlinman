"""Errors emitted while loading skill files off disk.

Mirrors the Rust ``SkillLoadError`` enum variants 1:1:

* :class:`SkillIoError`        — wraps ``OSError`` from the filesystem walk
* :class:`YamlParseError`      — YAML frontmatter could not be parsed
* :class:`DuplicateNameError`  — two files declared the same ``name``
* :class:`MissingFieldError`   — required frontmatter field was missing/empty

All four inherit from :class:`SkillLoadError` so callers can either catch the
common base class (the Rust ``Result<_, SkillLoadError>`` analogue) or pattern
match on the specific subclass.
"""

from __future__ import annotations

from pathlib import Path


class SkillLoadError(Exception):
    """Common base class for every failure mode of
    :meth:`corlinman_skills_registry.SkillRegistry.load_from_dir`.

    Catching this class is the Python equivalent of matching on the Rust
    ``SkillLoadError`` enum.
    """


class SkillIoError(SkillLoadError):
    """Filesystem walk or file read failed.

    Wraps the underlying :class:`OSError` (matching Rust's ``Io(io::Error)``
    variant). The original exception is preserved via ``__cause__``.
    """

    def __init__(self, source: OSError) -> None:
        super().__init__(f"skill IO error: {source}")
        self.source: OSError = source


class YamlParseError(SkillLoadError):
    """The YAML frontmatter in ``path`` could not be parsed."""

    def __init__(self, path: Path, err: Exception) -> None:
        super().__init__(f"skill YAML parse failed at {path}: {err}")
        self.path: Path = path
        self.err: Exception = err


class DuplicateNameError(SkillLoadError):
    """Two skill files declared the same ``name``.

    Matches the Rust ``DuplicateName`` variant — we refuse to silently let
    the second occurrence shadow the first, since skill names are addressed
    by manifests downstream and ambiguity would be a footgun.
    """

    def __init__(self, name: str, first: Path, second: Path) -> None:
        super().__init__(
            f"duplicate skill name '{name}': first defined at {first} "
            f"then redefined at {second}"
        )
        self.name: str = name
        self.first: Path = first
        self.second: Path = second


class MissingFieldError(SkillLoadError):
    """Required frontmatter field was missing or empty.

    ``field`` is one of ``"frontmatter"``, ``"name"`` or ``"description"`` —
    matching the Rust crate's ``&'static str`` error tag.
    """

    def __init__(self, path: Path, field: str) -> None:
        super().__init__(f"skill at {path} is missing required field '{field}'")
        self.path: Path = path
        self.field: str = field


__all__ = [
    "DuplicateNameError",
    "MissingFieldError",
    "SkillIoError",
    "SkillLoadError",
    "YamlParseError",
]
