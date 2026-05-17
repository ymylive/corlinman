"""In-memory skill registry loaded from a directory tree.

Python port of the Rust ``SkillRegistry``. Public surface mirrors the
crate 1:1:

* :meth:`SkillRegistry.load_from_dir`     — walk + parse a directory tree
* :meth:`SkillRegistry.get`               — lookup by ``name``
* :meth:`SkillRegistry.__iter__` /
  :meth:`SkillRegistry.iter`              — iterate all loaded skills
* :meth:`SkillRegistry.names`             — sorted list of names (for CLIs)
* :meth:`SkillRegistry.check_requirements` — verify a skill's prerequisites

W4 additions (curator port):

* :meth:`SkillRegistry.path_for`          — resolve the directory of a skill
* :meth:`SkillRegistry.usage_for`         — read ``.usage.json`` sidecar
* :meth:`SkillRegistry.bump_use` /
  :meth:`SkillRegistry.bump_patch`        — convenience telemetry hooks
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

import structlog

from .errors import DuplicateNameError, SkillIoError
from .parse import parse_skill
from .skill import Skill
from .usage import SkillUsage, bump_patch, bump_use, bump_view, read_usage

log = structlog.get_logger(__name__)


class SkillRegistry:
    """Owns the set of skills loaded from disk and provides lookups plus
    runtime requirement checks.

    Equivalent to the Rust ``SkillRegistry``. Instances are cheap to clone
    (skills are shared by reference), and the type is intentionally passive:
    it parses files off disk and exposes lookups.
    """

    __slots__ = ("_skills",)

    def __init__(self, skills: dict[str, Skill] | None = None) -> None:
        self._skills: dict[str, Skill] = dict(skills) if skills else {}

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def load_from_dir(
        cls,
        root: str | os.PathLike[str],
        *,
        bundled: bool = False,
    ) -> SkillRegistry:
        """Walk ``root`` recursively and parse every ``*.md`` file into a
        :class:`Skill`.

        Duplicate ``name`` fields are a hard error: the second occurrence
        wins nothing, we refuse to load at all (matching the Rust crate's
        ``DuplicateName`` semantics).

        A non-existent ``root`` is treated as "no skills" and yields an
        empty registry — same as the Rust ``debug!`` + return path. This
        is what the context assembler relies on when the skills dir hasn't
        been provisioned yet.

        :param bundled: when ``True``, skills loaded from this root that
            don't carry an explicit ``origin`` in their frontmatter default
            to ``"bundled"`` instead of ``"user-requested"``. Lets callers
            distinguish the in-repo seed skills from user-authored ones
            without touching every SKILL.md upfront (W4 — hermes
            ``tools/skill_usage.py:155-200``).

        :raises SkillIoError: filesystem walk or read failed.
        :raises YamlParseError: a frontmatter block was malformed YAML.
        :raises MissingFieldError: a required field was absent/empty.
        :raises DuplicateNameError: two files declared the same ``name``.
        """
        root_path = Path(root)
        skills: dict[str, Skill] = {}

        if not root_path.exists():
            log.debug(
                "skills directory does not exist; empty registry",
                path=str(root_path),
            )
            return cls(skills)

        # Iterative DFS to mirror the Rust ``stack: Vec<PathBuf>`` walk.
        # We deliberately do NOT use ``Path.rglob`` so the traversal order
        # and error surface match the Rust implementation: each readdir /
        # stat / read error becomes a ``SkillIoError``.
        stack: list[Path] = [root_path]
        while stack:
            current = stack.pop()
            try:
                entries = list(os.scandir(current))
            except OSError as err:
                raise SkillIoError(err) from err

            for entry in entries:
                entry_path = Path(entry.path)
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                    is_file = entry.is_file(follow_symlinks=False)
                except OSError as err:
                    raise SkillIoError(err) from err

                if is_dir:
                    stack.append(entry_path)
                    continue
                if not is_file:
                    continue
                if entry_path.suffix != ".md":
                    continue

                try:
                    text = entry_path.read_text(encoding="utf-8")
                except OSError as err:
                    raise SkillIoError(err) from err

                skill = parse_skill(entry_path, text)

                # ------------------------------------------------------
                # Lifecycle inference (W4)
                # ------------------------------------------------------
                # Legacy SKILL.md files don't carry ``origin`` /
                # ``created_at``. Fill them from the load context + sidecar
                # without rewriting the file — the caller decides when to
                # persist (W4.3 curator transitions, W4.4 background fork).
                if bundled and skill.origin == "user-requested":
                    # ``parse_skill`` returns the default ``user-requested``
                    # for missing frontmatter; promote to ``bundled`` when
                    # this root was declared bundled.
                    if not _frontmatter_has_origin(text):
                        skill.origin = "bundled"

                if skill.created_at is None:
                    # Prefer the timestamp recorded in the sidecar (it's
                    # the actual first-seen moment from a prior load).
                    usage = read_usage(entry_path.parent)
                    if usage.created_at is not None:
                        skill.created_at = usage.created_at

                existing = skills.get(skill.name)
                if existing is not None:
                    raise DuplicateNameError(
                        name=skill.name,
                        first=existing.source_path,
                        second=entry_path,
                    )
                log.debug("loaded skill", name=skill.name, path=str(entry_path))
                skills[skill.name] = skill

        return cls(skills)

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    def get(self, name: str) -> Skill | None:
        """Look up a skill by its ``name`` field. Returns ``None`` if it is
        not registered (matches the Rust ``Option<&Arc<Skill>>`` shape).
        """
        return self._skills.get(name)

    def iter(self) -> Iterator[Skill]:
        """Iterate over all loaded skills in unspecified order.

        Provided for naming parity with the Rust ``SkillRegistry::iter``.
        Python callers may equivalently use ``iter(registry)``.
        """
        return iter(self._skills.values())

    def __iter__(self) -> Iterator[Skill]:
        return iter(self._skills.values())

    def __len__(self) -> int:
        return len(self._skills)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._skills

    def names(self) -> list[str]:
        """Sorted list of all skill names, handy for CLI listings."""
        return sorted(self._skills.keys())

    # ------------------------------------------------------------------
    # W4: usage telemetry + skill-directory resolution
    # ------------------------------------------------------------------

    def path_for(self, skill_name: str) -> Path | None:
        """Return the **directory** that contains ``skill_name``'s SKILL.md.

        Hermes lays out skills as ``skills/<name>/SKILL.md`` with arbitrary
        siblings (``references/``, ``scripts/``, ``.usage.json``); openclaw
        + corlinman support both that nested layout and flat ``*.md`` files.
        Either way, the directory containing the file is the right anchor
        for the sidecar.

        Returns ``None`` if the skill isn't registered.
        """
        skill = self._skills.get(skill_name)
        if skill is None:
            return None
        return skill.source_path.parent

    def usage_for(self, skill_name: str) -> SkillUsage:
        """Read the ``.usage.json`` sidecar for ``skill_name``.

        Returns an empty :class:`SkillUsage` if the skill isn't registered,
        the sidecar is missing, or the JSON is malformed — never raises,
        because lifecycle code wants a sane default to compare against.
        """
        skill_dir = self.path_for(skill_name)
        if skill_dir is None:
            return SkillUsage()
        return read_usage(skill_dir)

    def bump_use(
        self,
        skill_name: str,
        *,
        now: datetime | None = None,
    ) -> SkillUsage | None:
        """Increment ``use_count`` for ``skill_name``.

        Called by the agent runtime when a skill is selected into the prompt
        context. Returns ``None`` if the skill isn't registered (so the
        caller can ignore stale references without raising).
        """
        skill_dir = self.path_for(skill_name)
        if skill_dir is None:
            return None
        return bump_use(skill_dir, now=now)

    def bump_view(
        self,
        skill_name: str,
        *,
        now: datetime | None = None,
    ) -> SkillUsage | None:
        """Increment ``view_count`` for ``skill_name``.

        Called by the admin UI when an operator opens a skill detail page.
        """
        skill_dir = self.path_for(skill_name)
        if skill_dir is None:
            return None
        return bump_view(skill_dir, now=now)

    def bump_patch(
        self,
        skill_name: str,
        *,
        now: datetime | None = None,
    ) -> SkillUsage | None:
        """Increment ``patch_count`` for ``skill_name``.

        Called by the curator's background-fork patch flow after the SKILL
        body is rewritten and the model bumped to the new version.
        """
        skill_dir = self.path_for(skill_name)
        if skill_dir is None:
            return None
        return bump_patch(skill_dir, now=now)

    # ------------------------------------------------------------------
    # Validity checks
    # ------------------------------------------------------------------

    def check_requirements(
        self,
        skill_name: str,
        config_lookup: Callable[[str], str | None],
    ) -> list[str]:
        """Verify every requirement for ``skill_name``.

        Returns an **empty list** if the skill can run; otherwise a list of
        actionable messages, one per unmet requirement. The empty-list
        success sentinel is the idiomatic Python equivalent of the Rust
        ``Result<(), Vec<String>>`` shape — callers can ``if problems:`` to
        branch.

        ``config_lookup(key)`` should return the string value for a set,
        non-empty config key and ``None`` otherwise. Whitespace-only values
        are treated as empty (matching Rust's ``.trim().is_empty()`` check).

        If ``skill_name`` is not registered, the returned list contains a
        single ``"skill '<name>' is not registered"`` message — same wording
        as the Rust crate.
        """
        skill = self._skills.get(skill_name)
        if skill is None:
            return [f"skill '{skill_name}' is not registered"]

        problems: list[str] = []
        req = skill.requires

        for binary in req.bins:
            if shutil.which(binary) is None:
                problems.append(
                    f"skill '{skill.name}' requires binary '{binary}' on $PATH; "
                    f"install it first"
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
            present = value is not None and value.strip() != ""
            if not present:
                problems.append(
                    f"skill '{skill.name}' requires config '{key}' to be set (non-empty)"
                )

        for var in req.env:
            env_value = os.environ.get(var)
            present = env_value is not None and env_value != ""
            if not present:
                problems.append(
                    f"skill '{skill.name}' requires env var '{var}' to be set"
                )

        return problems

    def __repr__(self) -> str:
        return f"SkillRegistry(skills={len(self._skills)})"


def _frontmatter_has_origin(text: str) -> bool:
    """Cheap probe: did the raw SKILL.md frontmatter explicitly carry an
    ``origin`` key? Used by :meth:`SkillRegistry.load_from_dir` to decide
    whether the ``bundled`` flag should override the parsed default.

    We avoid re-running the YAML parser — the parser already ran successfully
    in :func:`parse_skill` and would just produce the post-default value. A
    line-scan for ``origin:`` in the fenced region is enough for inference;
    false positives (e.g. ``origin:`` inside the body) don't matter because
    we only consult this when the parsed origin is the default.
    """
    if not text.startswith("---"):
        return False
    end = text.find("\n---", 3)
    if end == -1:
        return False
    fence = text[3:end]
    # Look for a top-level "origin:" key (no leading whitespace = not nested
    # under another mapping). Tolerate both LF and CRLF line endings.
    for line in fence.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("origin:") and stripped == line:
            return True
    return False


__all__ = ["SkillRegistry"]
