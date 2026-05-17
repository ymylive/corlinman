"""Tests for :mod:`corlinman_server.gateway.lifecycle.legacy_migration`.

Mirrors the Rust ``corlinman_gateway::legacy_migration::tests`` suite
1:1 — same scenarios, same assertions, so a behaviour drift surfaces in
both languages' CI runs.
"""

from __future__ import annotations

from pathlib import Path

from corlinman_server.gateway.lifecycle.legacy_migration import (
    LEGACY_DB_NAMES,
    migrate_legacy_data_files,
)


def _touch(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


def test_empty_data_dir_is_a_noop(tmp_path: Path) -> None:
    migrate_legacy_data_files(tmp_path)
    # The function creates the target tree unconditionally; no DB files
    # should appear.
    assert (tmp_path / "tenants" / "default").is_dir()
    for name in LEGACY_DB_NAMES:
        assert not (tmp_path / f"{name}.sqlite").exists()
        assert not (
            tmp_path / "tenants" / "default" / f"{name}.sqlite"
        ).exists()


def test_moves_every_legacy_file_into_default_tenant_dir(tmp_path: Path) -> None:
    for name in LEGACY_DB_NAMES:
        _touch(tmp_path / f"{name}.sqlite", f"legacy-{name}".encode())

    migrate_legacy_data_files(tmp_path)

    for name in LEGACY_DB_NAMES:
        # Source gone.
        assert not (tmp_path / f"{name}.sqlite").exists(), name
        # Destination present.
        migrated = tmp_path / "tenants" / "default" / f"{name}.sqlite"
        assert migrated.exists(), name
        assert migrated.read_bytes() == f"legacy-{name}".encode()


def test_already_migrated_path_is_left_in_place(tmp_path: Path) -> None:
    legacy = tmp_path / "evolution.sqlite"
    migrated = tmp_path / "tenants" / "default" / "evolution.sqlite"
    _touch(legacy, b"legacy-evolution")
    _touch(migrated, b"migrated-evolution")

    migrate_legacy_data_files(tmp_path)

    assert legacy.exists()
    assert migrated.exists()
    assert legacy.read_bytes() == b"legacy-evolution"
    assert migrated.read_bytes() == b"migrated-evolution"


def test_idempotent_second_run_after_full_migration(tmp_path: Path) -> None:
    for name in LEGACY_DB_NAMES:
        _touch(tmp_path / f"{name}.sqlite", f"v1-{name}".encode())

    migrate_legacy_data_files(tmp_path)
    # Second invocation: legacy paths are already empty; the loop
    # short-circuits each entry. No errors, no extra moves.
    migrate_legacy_data_files(tmp_path)

    for name in LEGACY_DB_NAMES:
        assert not (tmp_path / f"{name}.sqlite").exists()
        migrated = tmp_path / "tenants" / "default" / f"{name}.sqlite"
        assert migrated.read_bytes() == f"v1-{name}".encode()


def test_partial_legacy_set_only_moves_present_files(tmp_path: Path) -> None:
    _touch(tmp_path / "evolution.sqlite", b"e")
    _touch(tmp_path / "kb.sqlite", b"k")

    migrate_legacy_data_files(tmp_path)

    target_root = tmp_path / "tenants" / "default"
    assert (target_root / "evolution.sqlite").exists()
    assert (target_root / "kb.sqlite").exists()
    for name in ("sessions", "user_model", "agent_state"):
        assert not (target_root / f"{name}.sqlite").exists()
        assert not (tmp_path / f"{name}.sqlite").exists()
