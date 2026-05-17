"""Per-skill usage telemetry sidecar (``.usage.json``).

Each skill directory may carry a ``.usage.json`` sidecar that records:

* ``use_count``         — number of times the skill was loaded into a prompt
* ``view_count``        — number of times an operator opened the skill in UI
* ``patch_count``       — number of times the curator rewrote the body
* ``last_used_at``      — ISO-8601 timestamp of most recent ``use``
* ``last_viewed_at``    — ISO-8601 timestamp of most recent ``view``
* ``last_patched_at``   — ISO-8601 timestamp of most recent ``patch``
* ``created_at``        — ISO-8601 first-seen timestamp

This is a **sidecar**, not frontmatter — operational telemetry stays out of
user-authored SKILL.md content (so bundled/hub skills don't conflict on
every bump). Field names mirror hermes ``tools/skill_usage.py:62-200``
verbatim so existing tooling and reports continue to apply.

All disk writes are atomic (tempfile + :func:`os.replace`) and best-effort:
a broken sidecar never breaks the registry. Time is injectable via ``now``
arguments so tests can drive lifecycle transitions deterministically.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


USAGE_FILENAME = ".usage.json"


@dataclass
class SkillUsage:
    """Activity counters + timestamps for a single skill.

    Mirrors hermes' usage record (``tools/skill_usage.py:62-200``) field-for-
    field. All timestamps are timezone-aware UTC; counters are non-negative
    integers. The dataclass is mutable so call sites can ``bump`` in place
    before persisting.
    """

    use_count: int = 0
    view_count: int = 0
    patch_count: int = 0
    last_used_at: datetime | None = None
    last_viewed_at: datetime | None = None
    last_patched_at: datetime | None = None
    created_at: datetime | None = None

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict with ISO-8601 timestamps.

        Counters serialise as plain ints; ``None`` timestamps drop out so
        the on-disk file stays small for never-used skills.
        """
        out: dict[str, Any] = {
            "use_count": int(self.use_count),
            "view_count": int(self.view_count),
            "patch_count": int(self.patch_count),
        }
        for key in ("last_used_at", "last_viewed_at", "last_patched_at", "created_at"):
            value = getattr(self, key)
            if isinstance(value, datetime):
                out[key] = value.isoformat()
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SkillUsage:
        """Inverse of :meth:`to_dict`. Tolerates partial / malformed data:
        unknown keys are dropped, unparseable timestamps become ``None``,
        non-int counters fall back to 0. The goal is never to fail a registry
        load just because someone hand-edited the sidecar.
        """
        if not isinstance(data, dict):
            return cls()

        def _int(value: Any) -> int:
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                return 0

        def _dt(value: Any) -> datetime | None:
            if value is None:
                return None
            if isinstance(value, datetime):
                return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
            if not isinstance(value, str):
                return None
            try:
                parsed = datetime.fromisoformat(value)
            except (TypeError, ValueError):
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed

        return cls(
            use_count=_int(data.get("use_count")),
            view_count=_int(data.get("view_count")),
            patch_count=_int(data.get("patch_count")),
            last_used_at=_dt(data.get("last_used_at")),
            last_viewed_at=_dt(data.get("last_viewed_at")),
            last_patched_at=_dt(data.get("last_patched_at")),
            created_at=_dt(data.get("created_at")),
        )


# ---------------------------------------------------------------------------
# Disk helpers
# ---------------------------------------------------------------------------


def usage_path(skill_dir: Path | str) -> Path:
    """Return the sidecar path for ``skill_dir``.

    The skill directory is the directory containing the SKILL.md file; we
    drop ``.usage.json`` alongside it so a single ``rm -r`` of the skill
    dir cleans up cleanly.
    """
    return Path(skill_dir) / USAGE_FILENAME


def read_usage(skill_dir: Path | str) -> SkillUsage:
    """Read the usage sidecar for ``skill_dir``.

    Missing file or unreadable JSON → returns a fresh :class:`SkillUsage`.
    Never raises; the caller can always proceed with empty counters.
    """
    path = usage_path(skill_dir)
    if not path.exists():
        return SkillUsage()
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, json.JSONDecodeError):
        return SkillUsage()
    return SkillUsage.from_dict(data if isinstance(data, dict) else {})


def write_usage(skill_dir: Path | str, usage: SkillUsage) -> None:
    """Atomically write ``usage`` to ``skill_dir/.usage.json``.

    Uses ``tempfile.mkstemp`` + :func:`os.replace` so an interrupted write
    can't leave a half-written sidecar (which :func:`read_usage` would
    silently treat as empty, losing counter history).
    """
    path = usage_path(skill_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = json.dumps(usage.to_dict(), indent=2, sort_keys=True) + "\n"

    fd, tmp_name = tempfile.mkstemp(
        prefix=USAGE_FILENAME + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fp:
            fp.write(payload)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Counter bumps — read-modify-write helpers used by the gateway + curator
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    """Current UTC time as a timezone-aware datetime."""
    return datetime.now(timezone.utc)


def _ensure_created(usage: SkillUsage, now: datetime) -> None:
    """Populate ``created_at`` on first bump so we have an anchor for the
    lifecycle code (``active`` skills with no activity still need a sane
    "how long has this existed?" reference)."""
    if usage.created_at is None:
        usage.created_at = now


def bump_use(skill_dir: Path | str, *, now: datetime | None = None) -> SkillUsage:
    """Increment ``use_count`` and stamp ``last_used_at``.

    Read-modify-write: loads the sidecar, mutates counters in place, writes
    atomically. Returns the updated :class:`SkillUsage` so the caller can
    inspect the post-bump state without re-reading.
    """
    timestamp = now or _utcnow()
    usage = read_usage(skill_dir)
    _ensure_created(usage, timestamp)
    usage.use_count += 1
    usage.last_used_at = timestamp
    write_usage(skill_dir, usage)
    return usage


def bump_view(skill_dir: Path | str, *, now: datetime | None = None) -> SkillUsage:
    """Increment ``view_count`` and stamp ``last_viewed_at``."""
    timestamp = now or _utcnow()
    usage = read_usage(skill_dir)
    _ensure_created(usage, timestamp)
    usage.view_count += 1
    usage.last_viewed_at = timestamp
    write_usage(skill_dir, usage)
    return usage


def bump_patch(skill_dir: Path | str, *, now: datetime | None = None) -> SkillUsage:
    """Increment ``patch_count`` and stamp ``last_patched_at``.

    Called by the curator after a successful skill body rewrite.
    """
    timestamp = now or _utcnow()
    usage = read_usage(skill_dir)
    _ensure_created(usage, timestamp)
    usage.patch_count += 1
    usage.last_patched_at = timestamp
    write_usage(skill_dir, usage)
    return usage


__all__ = [
    "SkillUsage",
    "USAGE_FILENAME",
    "bump_patch",
    "bump_use",
    "bump_view",
    "read_usage",
    "usage_path",
    "write_usage",
]
