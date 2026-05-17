"""Tests for the W4 lifecycle metadata on the :class:`Skill` model.

Coverage:
  * default values for every new field land where the curator expects them
  * parsing a SKILL.md with explicit lifecycle frontmatter round-trips into
    the model verbatim (no silent coercion to defaults)
  * legacy SKILL.md files (no lifecycle keys) still parse cleanly with
    benign defaults — backwards compat is non-negotiable
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from corlinman_skills_registry import Skill
from corlinman_skills_registry.parse import parse_skill


def test_skill_defaults_match_curator_expectations() -> None:
    """Spawning a Skill with the minimum required fields produces the
    "fresh, active, user-authored" baseline the curator looks for."""
    skill = Skill(
        name="x",
        description="d",
        source_path=Path("/tmp/x.md"),
    )

    assert skill.version == "1.0.0"
    assert skill.origin == "user-requested"
    assert skill.state == "active"
    assert skill.pinned is False
    assert skill.created_at is None


def test_parse_skill_reads_explicit_lifecycle_keys() -> None:
    """SKILL.md frontmatter with the W4 keys round-trips into the model
    without coercion — important so the curator's "did anything change"
    diff doesn't false-positive."""
    text = (
        "---\n"
        "name: agent_created_one\n"
        "description: a curator-authored skill\n"
        "version: 1.4.2\n"
        "origin: agent-created\n"
        "state: stale\n"
        "pinned: true\n"
        "created_at: 2026-01-15T10:30:00+00:00\n"
        "---\n"
        "body content\n"
    )
    skill = parse_skill(Path("/tmp/s.md"), text)

    assert skill.version == "1.4.2"
    assert skill.origin == "agent-created"
    assert skill.state == "stale"
    assert skill.pinned is True
    assert skill.created_at == datetime(2026, 1, 15, 10, 30, tzinfo=timezone.utc)


def test_parse_skill_legacy_file_uses_defaults() -> None:
    """SKILL.md without any lifecycle keys still parses — and lands on the
    same defaults the model declares."""
    text = "---\nname: legacy\ndescription: d\n---\nold body\n"
    skill = parse_skill(Path("/tmp/legacy.md"), text)

    assert skill.version == "1.0.0"
    assert skill.origin == "user-requested"
    assert skill.state == "active"
    assert skill.pinned is False
    assert skill.created_at is None


def test_parse_skill_rejects_unknown_origin_silently() -> None:
    """An origin value not in the literal set falls back to the default
    instead of raising — we don't want a hand-edit typo to wedge load."""
    text = (
        "---\n"
        "name: typo\n"
        "description: d\n"
        "origin: agent_created\n"  # underscore, not dash — invalid
        "---\n"
        "body\n"
    )
    skill = parse_skill(Path("/tmp/t.md"), text)

    assert skill.origin == "user-requested"


def test_parse_skill_rejects_unknown_state_silently() -> None:
    text = (
        "---\n"
        "name: typo_state\n"
        "description: d\n"
        "state: zombie\n"
        "---\n"
        "body\n"
    )
    skill = parse_skill(Path("/tmp/t.md"), text)

    assert skill.state == "active"


def test_parse_skill_malformed_created_at_falls_back_to_none() -> None:
    """A garbage ``created_at`` value mustn't block load — we'll fill from
    the sidecar (or current time on next write) instead."""
    text = (
        "---\n"
        "name: bad_ts\n"
        "description: d\n"
        "created_at: not-a-timestamp\n"
        "---\n"
        "body\n"
    )
    skill = parse_skill(Path("/tmp/t.md"), text)

    assert skill.created_at is None
