"""Integration tests for :class:`corlinman_skills_registry.SkillRegistry`.

These mirror, 1:1, the seven Rust integration tests in
``rust/crates/corlinman-skills/tests/load.rs`` — same fixtures, same
assertions, same wording-checks on error messages. Keeping the suites
aligned is how we know the Python port hasn't drifted.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from corlinman_skills_registry import (
    DuplicateNameError,
    MissingFieldError,
    SkillRegistry,
)


MakeDir = Callable[[list[tuple[str, str]]], Path]


# ---------------------------------------------------------------------------
# 1. loads_happy_path_skill
# ---------------------------------------------------------------------------
def test_loads_happy_path_skill(fixtures_dir: Path, make_dir: MakeDir) -> None:
    body = (fixtures_dir / "web_search.md").read_text(encoding="utf-8")
    root = make_dir([("web_search.md", body)])

    reg = SkillRegistry.load_from_dir(root)
    skill = reg.get("web_search")
    assert skill is not None

    assert skill.name == "web_search"
    assert skill.description == "Search the web via Brave Search API"
    assert skill.emoji == "\U0001f50d"  # 🔍
    assert skill.requires.config == ["providers.brave.api_key"]
    assert skill.requires.bins == []
    assert skill.install == "Get an API key at https://brave.com/search/api/"
    assert skill.allowed_tools == ["web.search", "web.fetch"]
    assert "Use the `web.search` tool" in skill.body_markdown


# ---------------------------------------------------------------------------
# 2. loads_dir_with_multiple_skills
# ---------------------------------------------------------------------------
def test_loads_dir_with_multiple_skills(fixtures_dir: Path, make_dir: MakeDir) -> None:
    root = make_dir(
        [
            ("a.md", (fixtures_dir / "web_search.md").read_text(encoding="utf-8")),
            ("b.md", (fixtures_dir / "shell_runner.md").read_text(encoding="utf-8")),
            ("c.md", (fixtures_dir / "code_reviewer.md").read_text(encoding="utf-8")),
        ]
    )

    reg = SkillRegistry.load_from_dir(root)
    assert reg.names() == ["code_reviewer", "shell_runner", "web_search"]
    assert sum(1 for _ in reg.iter()) == 3
    assert len(reg) == 3
    assert "web_search" in reg


# ---------------------------------------------------------------------------
# 3. missing_name_field_fails
# ---------------------------------------------------------------------------
def test_missing_name_field_fails(make_dir: MakeDir) -> None:
    bad = "---\ndescription: no name here\n---\nbody\n"
    root = make_dir([("bad.md", bad)])

    with pytest.raises(MissingFieldError) as excinfo:
        SkillRegistry.load_from_dir(root)
    assert excinfo.value.field == "name"
    assert excinfo.value.path.name == "bad.md"


# ---------------------------------------------------------------------------
# 4. duplicate_name_fails
# ---------------------------------------------------------------------------
def test_duplicate_name_fails(make_dir: MakeDir) -> None:
    body = "---\nname: web_search\ndescription: dup\n---\nhi\n"
    root = make_dir([("first.md", body), ("second.md", body)])

    with pytest.raises(DuplicateNameError) as excinfo:
        SkillRegistry.load_from_dir(root)

    err = excinfo.value
    assert err.name == "web_search"
    names = {err.first.name, err.second.name}
    # Iteration order isn't stable — just require both filenames show up.
    assert "first.md" in names
    assert "second.md" in names


# ---------------------------------------------------------------------------
# 5. check_requirements_bin_missing
# ---------------------------------------------------------------------------
def test_check_requirements_bin_missing(make_dir: MakeDir) -> None:
    body = (
        "---\n"
        "name: needs_bin\n"
        "description: d\n"
        "metadata:\n"
        "  openclaw:\n"
        "    requires:\n"
        '      bins: ["this-bin-does-not-exist-xyzzy"]\n'
        "---\n"
        "body\n"
    )
    root = make_dir([("s.md", body)])
    reg = SkillRegistry.load_from_dir(root)

    problems = reg.check_requirements("needs_bin", lambda _key: None)
    assert len(problems) == 1
    msg = problems[0]
    assert "needs_bin" in msg
    assert "this-bin-does-not-exist-xyzzy" in msg
    assert "install it first" in msg


# ---------------------------------------------------------------------------
# 6. check_requirements_config_empty
# ---------------------------------------------------------------------------
def test_check_requirements_config_empty(make_dir: MakeDir) -> None:
    body = (
        "---\n"
        "name: needs_cfg\n"
        "description: d\n"
        "metadata:\n"
        "  openclaw:\n"
        "    requires:\n"
        '      config: ["providers.brave.api_key"]\n'
        "---\n"
        "body\n"
    )
    root = make_dir([("s.md", body)])
    reg = SkillRegistry.load_from_dir(root)

    # Lookup returns None → unset.
    problems = reg.check_requirements("needs_cfg", lambda _key: None)
    assert len(problems) == 1
    assert "providers.brave.api_key" in problems[0]
    assert "non-empty" in problems[0]

    # Whitespace-only counts as empty too.
    problems_ws = reg.check_requirements("needs_cfg", lambda _key: "   ")
    assert len(problems_ws) == 1

    # Non-empty value satisfies the requirement.
    assert reg.check_requirements("needs_cfg", lambda _key: "secret") == []


# ---------------------------------------------------------------------------
# 7. body_markdown_captured_after_frontmatter
# ---------------------------------------------------------------------------
def test_body_markdown_captured_after_frontmatter(make_dir: MakeDir) -> None:
    raw = (
        "---\nname: verbatim\ndescription: d\n---\n"
        "\n# Heading\n\nparagraph one\n\n   trailing-spaces   \n"
    )
    root = make_dir([("v.md", raw)])
    reg = SkillRegistry.load_from_dir(root)
    skill = reg.get("verbatim")
    assert skill is not None

    expected = "\n# Heading\n\nparagraph one\n\n   trailing-spaces   \n"
    assert skill.body_markdown == expected


# ---------------------------------------------------------------------------
# Additional Python-side coverage: empty/missing dir + nested walk + env check
# ---------------------------------------------------------------------------
def test_missing_dir_yields_empty_registry(tmp_path: Path) -> None:
    """Matches the Rust ``debug!`` + ``return Ok(Self { skills })`` path."""
    reg = SkillRegistry.load_from_dir(tmp_path / "does-not-exist")
    assert list(reg.iter()) == []
    assert reg.names() == []
    assert len(reg) == 0


def test_walk_recurses_into_subdirs(tmp_path: Path) -> None:
    nested = tmp_path / "sub" / "deeper"
    nested.mkdir(parents=True)
    (nested / "x.md").write_text(
        "---\nname: nested\ndescription: d\n---\nbody\n",
        encoding="utf-8",
    )

    reg = SkillRegistry.load_from_dir(tmp_path)
    assert reg.names() == ["nested"]


def test_check_requirements_env_var(make_dir: MakeDir, monkeypatch) -> None:
    body = (
        "---\n"
        "name: needs_env\n"
        "description: d\n"
        "metadata:\n"
        "  openclaw:\n"
        "    requires:\n"
        '      env: ["CORLINMAN_SKILLS_REG_TEST_VAR"]\n'
        "---\n"
        "body\n"
    )
    root = make_dir([("s.md", body)])
    reg = SkillRegistry.load_from_dir(root)

    monkeypatch.delenv("CORLINMAN_SKILLS_REG_TEST_VAR", raising=False)
    problems = reg.check_requirements("needs_env", lambda _k: None)
    assert len(problems) == 1
    assert "CORLINMAN_SKILLS_REG_TEST_VAR" in problems[0]

    monkeypatch.setenv("CORLINMAN_SKILLS_REG_TEST_VAR", "set")
    assert reg.check_requirements("needs_env", lambda _k: None) == []


def test_check_requirements_unknown_skill(make_dir: MakeDir) -> None:
    root = make_dir([])
    reg = SkillRegistry.load_from_dir(root)
    problems = reg.check_requirements("nope", lambda _k: None)
    assert problems == ["skill 'nope' is not registered"]


def test_check_requirements_any_bins_one_present(make_dir: MakeDir) -> None:
    # ``ls`` exists on every POSIX dev box & CI; pair with a definitely-
    # missing binary to prove ``any_bins`` is satisfied by a single hit.
    body = (
        "---\n"
        "name: any_ok\n"
        "description: d\n"
        "metadata:\n"
        "  openclaw:\n"
        "    requires:\n"
        '      anyBins: ["this-bin-xyzzy-missing", "ls"]\n'
        "---\n"
        "body\n"
    )
    root = make_dir([("s.md", body)])
    reg = SkillRegistry.load_from_dir(root)
    assert reg.check_requirements("any_ok", lambda _k: None) == []
