"""Tests for the ``.usage.json`` sidecar I/O layer.

Coverage:
  * read_usage on missing/broken files → empty SkillUsage (never raises)
  * write_usage + read_usage round-trips counters and ISO timestamps
  * bump_use / bump_view / bump_patch increment the right counters and
    stamp the right timestamps, using the injected ``now`` for determinism
  * atomic write doesn't leave temp files behind, even when interrupted
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

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


# ---------------------------------------------------------------------------
# read_usage — defensive defaults
# ---------------------------------------------------------------------------


def test_read_usage_missing_file_returns_empty(tmp_path: Path) -> None:
    """No sidecar on disk → empty SkillUsage with zero counters and no
    timestamps. The lifecycle code needs a sane default; raising would
    force every caller into try/except boilerplate."""
    usage = read_usage(tmp_path)

    assert usage == SkillUsage()
    assert usage.use_count == 0
    assert usage.last_used_at is None


def test_read_usage_malformed_json_returns_empty(tmp_path: Path) -> None:
    """A hand-edited sidecar that breaks JSON syntax mustn't wedge the
    registry — silently fall back so the curator can re-seed on next bump."""
    (tmp_path / USAGE_FILENAME).write_text("{not valid json", encoding="utf-8")

    assert read_usage(tmp_path) == SkillUsage()


def test_read_usage_non_dict_payload_returns_empty(tmp_path: Path) -> None:
    """A sidecar that parses to a list/scalar instead of a dict is also
    treated as 'no usable data' rather than raising."""
    (tmp_path / USAGE_FILENAME).write_text("[1, 2, 3]", encoding="utf-8")

    assert read_usage(tmp_path) == SkillUsage()


# ---------------------------------------------------------------------------
# write_usage + round-trip
# ---------------------------------------------------------------------------


def test_write_then_read_roundtrip(tmp_path: Path) -> None:
    """A full ``SkillUsage`` survives a serialise → file → deserialise
    cycle byte-for-byte (after parsing back into the dataclass)."""
    now = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    written = SkillUsage(
        use_count=12,
        view_count=4,
        patch_count=2,
        last_used_at=now,
        last_viewed_at=now - timedelta(days=1),
        last_patched_at=now - timedelta(days=3),
        created_at=now - timedelta(days=30),
    )

    write_usage(tmp_path, written)
    read_back = read_usage(tmp_path)

    assert read_back == written


def test_write_usage_emits_pretty_json_with_iso_dates(tmp_path: Path) -> None:
    """Sidecar JSON should be readable by a human: indented, sorted keys,
    ISO-8601 timestamps. Lets operators eyeball the file without tooling."""
    now = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    write_usage(tmp_path, SkillUsage(use_count=1, last_used_at=now, created_at=now))

    raw = (tmp_path / USAGE_FILENAME).read_text(encoding="utf-8")
    payload = json.loads(raw)

    assert payload["use_count"] == 1
    assert payload["last_used_at"] == "2026-05-17T12:00:00+00:00"
    assert payload["created_at"] == "2026-05-17T12:00:00+00:00"
    # ``view_count``/``patch_count`` are 0 (default) and still emitted so
    # the on-disk schema is stable for downstream consumers.
    assert payload["view_count"] == 0
    assert payload["patch_count"] == 0


def test_write_usage_drops_none_timestamps(tmp_path: Path) -> None:
    """Never-used skills don't pollute the sidecar with ``null`` timestamps
    — keeps the file minimal until a real bump records activity."""
    write_usage(tmp_path, SkillUsage(use_count=0))
    payload = json.loads((tmp_path / USAGE_FILENAME).read_text(encoding="utf-8"))

    assert "last_used_at" not in payload
    assert "last_viewed_at" not in payload
    assert "last_patched_at" not in payload
    assert "created_at" not in payload


# ---------------------------------------------------------------------------
# bump_* — counters and timestamps via injected ``now``
# ---------------------------------------------------------------------------


def test_bump_use_increments_and_stamps(tmp_path: Path) -> None:
    """Two consecutive ``bump_use`` calls increment ``use_count`` by 1 each
    and update ``last_used_at`` to the injected timestamp."""
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t1 = datetime(2026, 2, 1, tzinfo=timezone.utc)

    after_first = bump_use(tmp_path, now=t0)
    assert after_first.use_count == 1
    assert after_first.last_used_at == t0
    # ``created_at`` is filled on first bump so lifecycle code has an
    # anchor for never-active skills.
    assert after_first.created_at == t0

    after_second = bump_use(tmp_path, now=t1)
    assert after_second.use_count == 2
    assert after_second.last_used_at == t1
    # ``created_at`` is preserved on subsequent bumps.
    assert after_second.created_at == t0


def test_bump_view_only_touches_view_counters(tmp_path: Path) -> None:
    """A view bump must not move use/patch counters or their timestamps."""
    t = datetime(2026, 3, 14, tzinfo=timezone.utc)
    write_usage(tmp_path, SkillUsage(use_count=5, last_used_at=t, created_at=t))

    result = bump_view(tmp_path, now=t)

    assert result.view_count == 1
    assert result.last_viewed_at == t
    assert result.use_count == 5
    assert result.last_used_at == t
    assert result.patch_count == 0


def test_bump_patch_increments_patch_counter(tmp_path: Path) -> None:
    """The curator's patch flow bumps ``patch_count`` + ``last_patched_at``."""
    t = datetime(2026, 4, 1, tzinfo=timezone.utc)
    result = bump_patch(tmp_path, now=t)

    assert result.patch_count == 1
    assert result.last_patched_at == t
    assert result.use_count == 0
    assert result.view_count == 0


def test_bump_use_persists_to_disk(tmp_path: Path) -> None:
    """Each bump writes through to disk so a crash between two bumps never
    silently loses counter history."""
    t = datetime(2026, 5, 1, tzinfo=timezone.utc)
    bump_use(tmp_path, now=t)
    bump_use(tmp_path, now=t)
    bump_patch(tmp_path, now=t)

    reread = read_usage(tmp_path)
    assert reread.use_count == 2
    assert reread.patch_count == 1


# ---------------------------------------------------------------------------
# Atomicity — no leftover temp files
# ---------------------------------------------------------------------------


def test_atomic_write_leaves_no_temp_files(tmp_path: Path) -> None:
    """``write_usage`` uses tempfile + os.replace; after a successful write
    the only file in the directory should be the canonical ``.usage.json``."""
    write_usage(tmp_path, SkillUsage(use_count=1))

    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == [USAGE_FILENAME]


def test_atomic_write_cleans_up_on_failure(tmp_path: Path, monkeypatch) -> None:
    """If ``os.replace`` fails (simulated), the temp file must be removed
    so we don't leak junk into the skill dir on every crash."""
    import corlinman_skills_registry.usage as usage_mod

    def boom(*_a, **_kw):  # noqa: ANN002, ANN003
        raise OSError("simulated replace failure")

    monkeypatch.setattr(usage_mod.os, "replace", boom)

    with pytest.raises(OSError):
        write_usage(tmp_path, SkillUsage(use_count=1))

    # No ``.usage.json``, no orphan ``.tmp`` file either.
    leftovers = [p.name for p in tmp_path.iterdir()]
    assert leftovers == []


def test_usage_path_returns_sidecar_filename(tmp_path: Path) -> None:
    """The sidecar lives at ``<skill_dir>/.usage.json`` so an ``rm -r`` of
    the skill dir cleans up cleanly."""
    assert usage_path(tmp_path) == tmp_path / USAGE_FILENAME


def test_skill_usage_from_dict_drops_unknown_keys() -> None:
    """Hand-edited sidecars with extra/typo keys still load — unknown keys
    are silently dropped instead of raising."""
    usage = SkillUsage.from_dict(
        {"use_count": 3, "future_field": "ignored", "last_used_at": "garbage"}
    )

    assert usage.use_count == 3
    assert usage.last_used_at is None  # unparseable string → None


def test_skill_usage_from_dict_clamps_negative_counts() -> None:
    """Counters can't go negative — hand-edits that try are clamped to 0."""
    usage = SkillUsage.from_dict({"use_count": -5, "view_count": "abc"})

    assert usage.use_count == 0
    assert usage.view_count == 0
