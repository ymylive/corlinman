"""Shared fixtures for corlinman-skills-registry tests."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def fixtures_dir() -> Path:
    """Directory containing the shared SKILL.md fixtures (copied from the
    Rust crate's ``tests/fixtures`` so both ports exercise the same files).
    """
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def make_dir(tmp_path: Path):
    """Drop a set of ``(name, body)`` files into a fresh tempdir.

    Mirrors the Rust ``make_dir`` test helper so test cases line up 1:1.
    """

    def _make(files: list[tuple[str, str]]) -> Path:
        for name, body in files:
            (tmp_path / name).write_text(body, encoding="utf-8")
        return tmp_path

    return _make
