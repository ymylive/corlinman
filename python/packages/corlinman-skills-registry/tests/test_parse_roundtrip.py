"""Round-trip tests for the SKILL.md writer (W4 curator dependency).

The curator rewrites SKILL.md when it transitions lifecycle state or
applies user-correction patches. For its diff check to be reliable, a
parse → render → write → parse cycle must preserve every field we care
about exactly. These tests cover the round-trip without depending on the
curator code itself.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from corlinman_skills_registry import (
    Skill,
    SkillRequirements,
    render_skill_frontmatter,
    write_skill_md,
)
from corlinman_skills_registry.parse import parse_skill, split_frontmatter


def _make_skill(**overrides) -> Skill:
    """Spawn a fully-populated Skill so the round-trip exercises every field
    (no silent reliance on defaults). Overrides let individual tests tweak
    one field without rewriting the whole payload."""
    base = dict(
        name="alpha",
        description="round trip target",
        emoji="\U0001f527",
        requires=SkillRequirements(
            bins=["jq"],
            any_bins=["curl", "wget"],
            config=["providers.brave.api_key"],
            env=["BRAVE_TOKEN"],
        ),
        install="brew install jq",
        allowed_tools=["web.search", "web.fetch"],
        body_markdown="# Heading\n\nbody paragraph\n",
        source_path=Path("/tmp/alpha.md"),
        version="2.0.0",
        origin="agent-created",
        state="stale",
        pinned=True,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return Skill(**base)


# ---------------------------------------------------------------------------
# render_skill_frontmatter — schema visible in YAML
# ---------------------------------------------------------------------------


def test_render_emits_lifecycle_keys_at_tail() -> None:
    """The lifecycle keys (version/origin/state/pinned/created_at) trail
    the YAML block so hand-edited frontmatter keeps its visual shape."""
    skill = _make_skill()
    yaml_str = render_skill_frontmatter(skill)

    # Walk lines and find the first index where each key appears.
    lines = yaml_str.splitlines()
    indices = {key: -1 for key in ("name", "version", "origin", "state", "pinned")}
    for i, line in enumerate(lines):
        for key in indices:
            if line.startswith(f"{key}:") and indices[key] == -1:
                indices[key] = i

    # Every key was emitted.
    assert all(v >= 0 for v in indices.values())
    # Lifecycle keys land after ``name``.
    assert indices["name"] < indices["version"]
    assert indices["name"] < indices["origin"]
    assert indices["name"] < indices["state"]


def test_render_uses_iso_datetime_for_created_at() -> None:
    """PyYAML's default datetime serialiser is non-ISO; we override so
    hermes' ISO-8601 parser round-trips cleanly."""
    skill = _make_skill(created_at=datetime(2026, 5, 17, 9, 30, tzinfo=timezone.utc))
    yaml_str = render_skill_frontmatter(skill)

    assert "created_at: '2026-05-17T09:30:00+00:00'" in yaml_str


# ---------------------------------------------------------------------------
# Full round-trip: write → parse → mutate → write → parse
# ---------------------------------------------------------------------------


def test_full_round_trip_preserves_every_field(tmp_path: Path) -> None:
    """Write a fully-populated Skill, parse it back, assert equality on
    every field except ``source_path`` (which the parser rewrites to the
    actual file path)."""
    original = _make_skill()
    path = tmp_path / "alpha" / "SKILL.md"
    write_skill_md(path, original)

    text = path.read_text(encoding="utf-8")
    parsed = parse_skill(path, text)

    assert parsed.name == original.name
    assert parsed.description == original.description
    assert parsed.emoji == original.emoji
    assert parsed.requires.bins == original.requires.bins
    assert parsed.requires.any_bins == original.requires.any_bins
    assert parsed.requires.config == original.requires.config
    assert parsed.requires.env == original.requires.env
    assert parsed.install == original.install
    assert parsed.allowed_tools == original.allowed_tools
    assert parsed.body_markdown == original.body_markdown
    assert parsed.version == original.version
    assert parsed.origin == original.origin
    assert parsed.state == original.state
    assert parsed.pinned == original.pinned
    assert parsed.created_at == original.created_at


def test_state_mutation_round_trips(tmp_path: Path) -> None:
    """The curator's lifecycle transition: load → set state to 'stale' →
    write → load → state is still 'stale'. This is the core curator
    operation; if it doesn't round-trip, the curator can't do its job."""
    original_text = (
        "---\n"
        "name: cycle\n"
        "description: d\n"
        "version: 1.0.0\n"
        "origin: agent-created\n"
        "state: active\n"
        "pinned: false\n"
        "---\n"
        "body\n"
    )
    path = tmp_path / "cycle" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(original_text, encoding="utf-8")

    # 1) Load.
    skill = parse_skill(path, original_text)
    assert skill.state == "active"

    # 2) Mutate (the curator's transition).
    skill.state = "stale"

    # 3) Write back.
    write_skill_md(path, skill)

    # 4) Reload.
    reloaded_text = path.read_text(encoding="utf-8")
    reloaded = parse_skill(path, reloaded_text)
    assert reloaded.state == "stale"
    # Other lifecycle fields aren't disturbed.
    assert reloaded.origin == "agent-created"
    assert reloaded.version == "1.0.0"


def test_write_skill_md_is_atomic(tmp_path: Path) -> None:
    """After a successful write, the only files in the target directory
    are the SKILL.md itself (no leftover ``*.tmp`` files)."""
    skill = _make_skill()
    path = tmp_path / "alpha" / "SKILL.md"
    write_skill_md(path, skill)

    files = sorted(p.name for p in path.parent.iterdir())
    assert files == ["SKILL.md"]


def test_write_skill_md_creates_parent_dirs(tmp_path: Path) -> None:
    """The writer mkdir-p's the parent so callers don't need to bootstrap
    the directory before persisting a curator-created skill."""
    skill = _make_skill()
    path = tmp_path / "deeply" / "nested" / "alpha" / "SKILL.md"
    write_skill_md(path, skill)

    assert path.exists()
    parsed = parse_skill(path, path.read_text(encoding="utf-8"))
    assert parsed.name == "alpha"


def test_write_skill_md_uses_fence(tmp_path: Path) -> None:
    """The output starts with ``---\\n`` and contains a closing ``---``
    fence — so it parses with our existing splitter without surprises."""
    skill = _make_skill()
    path = tmp_path / "alpha" / "SKILL.md"
    write_skill_md(path, skill)

    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert split_frontmatter(text) is not None


def test_write_skill_md_omits_empty_requires(tmp_path: Path) -> None:
    """A skill with no runtime requirements shouldn't emit a sea of empty
    list keys — keeps the frontmatter readable for the 95% case."""
    skill = _make_skill(requires=SkillRequirements())
    path = tmp_path / "alpha" / "SKILL.md"
    write_skill_md(path, skill)

    text = path.read_text(encoding="utf-8")
    assert "requires:" not in text
