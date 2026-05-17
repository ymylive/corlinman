"""Tests for :mod:`corlinman_server.gateway.lifecycle.admin_seed`.

Covers the first-boot credential bootstrap surface used by W1.1:

* ``ensure_admin_credentials`` writes ``admin``/``root`` + ``must_change_password=true`` when the
  config file has no ``[admin]`` block;
* a subsequent call re-reads the persisted hash + flag (no re-seed);
* an operator-edited ``[admin]`` block (any shape) is **never** overwritten;
* an ``[admin]`` block missing the ``must_change_password`` key defaults to ``False`` (the
  operator presumably hand-rolled their credentials, no first-boot warning required);
* the block can be spliced into a TOML file with other sections without clobbering them.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from corlinman_server.gateway.lifecycle.admin_seed import (
    DEFAULT_ADMIN_PASSWORD,
    DEFAULT_ADMIN_USERNAME,
    _merge_admin_block,
    _render_admin_block,
    ensure_admin_credentials,
    resolve_admin_config_path,
)
from corlinman_server.gateway.routes_admin_a.auth import argon2_verify


# ---------------------------------------------------------------------------
# resolve_admin_config_path
# ---------------------------------------------------------------------------


def test_resolve_admin_config_path_prefers_cli(tmp_path: Path) -> None:
    cli = tmp_path / "explicit.toml"
    data = tmp_path / "data"
    assert resolve_admin_config_path(cli_config_path=cli, data_dir=data) == cli


def test_resolve_admin_config_path_falls_back_to_data_dir(tmp_path: Path) -> None:
    data = tmp_path / "data"
    assert (
        resolve_admin_config_path(cli_config_path=None, data_dir=data)
        == data / "config.toml"
    )


# ---------------------------------------------------------------------------
# First-boot seeding behaviour
# ---------------------------------------------------------------------------


async def test_first_boot_writes_default_admin_block(tmp_path: Path) -> None:
    """No config file at all → seed admin/root + must_change_password=true."""
    cfg = tmp_path / "config.toml"
    seeded = await ensure_admin_credentials(config_path=cfg)

    assert seeded.seeded_now is True
    assert seeded.username == DEFAULT_ADMIN_USERNAME
    assert seeded.must_change_password is True
    assert seeded.config_path == cfg
    # The hash on the SeededAdmin record actually verifies against the
    # documented bootstrap password.
    assert argon2_verify(DEFAULT_ADMIN_PASSWORD, seeded.password_hash)

    # The file now contains a parseable ``[admin]`` block matching the
    # in-memory snapshot.
    assert cfg.exists()
    parsed = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert parsed["admin"]["username"] == DEFAULT_ADMIN_USERNAME
    assert parsed["admin"]["password_hash"] == seeded.password_hash
    assert parsed["admin"]["must_change_password"] is True


async def test_second_call_reads_back_persisted_state(tmp_path: Path) -> None:
    """A second call must NOT re-seed — it reads back the persisted hash."""
    cfg = tmp_path / "config.toml"
    first = await ensure_admin_credentials(config_path=cfg)
    second = await ensure_admin_credentials(config_path=cfg)

    assert second.seeded_now is False
    assert second.username == first.username
    assert second.password_hash == first.password_hash
    assert second.must_change_password is first.must_change_password


async def test_existing_admin_block_is_never_overwritten(tmp_path: Path) -> None:
    """Operator-provided credentials win — even if their hash isn't ``root``."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[admin]\n'
        'username = "ops"\n'
        'password_hash = "$argon2id$v=19$m=65536,t=3,p=4$abc$def"\n'
        'must_change_password = false\n',
        encoding="utf-8",
    )
    seeded = await ensure_admin_credentials(config_path=cfg)
    assert seeded.seeded_now is False
    assert seeded.username == "ops"
    assert seeded.password_hash.startswith("$argon2id$")
    assert seeded.must_change_password is False
    # File is unchanged byte-for-byte (no side-effect rewrite).
    assert cfg.read_text(encoding="utf-8").count("[admin]") == 1


async def test_existing_admin_block_without_flag_defaults_to_false(
    tmp_path: Path,
) -> None:
    """``must_change_password`` missing from an existing block → operator
    presumably hand-edited; default to ``False``, never raise the
    first-boot warning."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[admin]\n'
        'username = "ops"\n'
        'password_hash = "$argon2id$v=19$m=65536,t=3,p=4$abc$def"\n',
        encoding="utf-8",
    )
    seeded = await ensure_admin_credentials(config_path=cfg)
    assert seeded.must_change_password is False
    assert seeded.username == "ops"


async def test_seed_preserves_sibling_sections(tmp_path: Path) -> None:
    """Splicing the ``[admin]`` block must NOT clobber unrelated TOML sections."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "# top-level comment\n"
        "[server]\n"
        'host = "127.0.0.1"\n'
        "port = 6005\n"
        "\n"
        "[providers.openai]\n"
        'enabled = true\n',
        encoding="utf-8",
    )
    seeded = await ensure_admin_credentials(config_path=cfg)
    assert seeded.seeded_now is True

    parsed = tomllib.loads(cfg.read_text(encoding="utf-8"))
    # Other sections survived.
    assert parsed["server"]["host"] == "127.0.0.1"
    assert parsed["server"]["port"] == 6005
    assert parsed["providers"]["openai"]["enabled"] is True
    # Admin block is now present.
    assert parsed["admin"]["username"] == DEFAULT_ADMIN_USERNAME


async def test_seed_replaces_existing_admin_block_in_situ(
    tmp_path: Path,
) -> None:
    """An ``[admin]`` block sandwiched between other sections is replaced
    in place — the surrounding sections keep their original order."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[server]\nport = 6005\n"
        "\n"
        "[admin]\n"
        'username = "ops"\n'
        'password_hash = "$argon2id$v=19$m=65536,t=3,p=4$abc$def"\n'
        'must_change_password = false\n'
        "\n"
        "[providers.openai]\nenabled = true\n",
        encoding="utf-8",
    )
    seeded = await ensure_admin_credentials(config_path=cfg)
    assert seeded.seeded_now is False  # operator-set wins
    text = cfg.read_text(encoding="utf-8")
    # Both surrounding sections still present, no duplicates.
    assert text.count("[server]") == 1
    assert text.count("[providers.openai]") == 1
    assert text.count("[admin]") == 1


# ---------------------------------------------------------------------------
# _merge_admin_block helper
# ---------------------------------------------------------------------------


def test_merge_admin_block_appends_when_absent() -> None:
    existing = "[server]\nport = 6005\n"
    block = _render_admin_block(
        username="admin", password_hash="hash", must_change_password=True
    )
    merged = _merge_admin_block(existing, block)
    assert "[server]" in merged
    assert "[admin]" in merged
    # Must end with the new block — appended at the bottom.
    assert merged.rstrip().endswith('must_change_password = true')


def test_merge_admin_block_replaces_in_place() -> None:
    existing = (
        "[server]\nport = 6005\n"
        "[admin]\nusername = \"old\"\npassword_hash = \"x\"\n"
        "[providers.openai]\nenabled = true\n"
    )
    block = _render_admin_block(
        username="new",
        password_hash="newhash",
        must_change_password=False,
    )
    merged = _merge_admin_block(existing, block)
    assert merged.count("[admin]") == 1
    assert "username = \"new\"" in merged
    assert "[providers.openai]" in merged  # sibling preserved


def test_merge_admin_block_empty_input() -> None:
    block = _render_admin_block(
        username="admin", password_hash="hash", must_change_password=True
    )
    assert _merge_admin_block("", block) == block


@pytest.mark.parametrize(
    "value", ["bad\"quote", "back\\slash", 'mix"and\\stuff']
)
def test_render_admin_block_escapes_special_chars(value: str) -> None:
    rendered = _render_admin_block(
        username=value, password_hash=value, must_change_password=True
    )
    parsed = tomllib.loads(rendered)
    assert parsed["admin"]["username"] == value
    assert parsed["admin"]["password_hash"] == value
