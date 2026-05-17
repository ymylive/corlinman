"""Unit tests for the frontmatter splitter + parser.

Mirrors the Rust ``#[cfg(test)] mod tests`` block in ``parse.rs`` so the two
implementations stay observable from the same spec.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from corlinman_skills_registry.errors import MissingFieldError, YamlParseError
from corlinman_skills_registry.parse import parse_skill, split_frontmatter


def test_split_frontmatter_simple() -> None:
    text = "---\nname: foo\n---\nbody text\n"
    split = split_frontmatter(text)
    assert split is not None
    yaml_str, body = split
    assert yaml_str == "name: foo\n"
    assert body == "body text\n"


def test_split_frontmatter_missing_close() -> None:
    assert split_frontmatter("---\nname: foo\nno close\n") is None


def test_split_frontmatter_no_fence() -> None:
    assert split_frontmatter("hello") is None


def test_split_frontmatter_crlf_open_fence() -> None:
    # Rust strip_prefix handles `---\r\n` too — verify parity.
    text = "---\r\nname: foo\n---\nbody\n"
    split = split_frontmatter(text)
    assert split is not None
    yaml_str, body = split
    assert yaml_str == "name: foo\n"
    assert body == "body\n"


def test_parse_skill_missing_frontmatter_fence() -> None:
    with pytest.raises(MissingFieldError) as excinfo:
        parse_skill(Path("bad.md"), "no fence at all\n")
    assert excinfo.value.field == "frontmatter"


def test_parse_skill_invalid_yaml() -> None:
    bad = "---\n: : : :\n---\nbody\n"
    with pytest.raises(YamlParseError):
        parse_skill(Path("bad.md"), bad)
