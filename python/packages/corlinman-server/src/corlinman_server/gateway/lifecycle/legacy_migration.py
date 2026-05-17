"""One-shot boot migration: pre-Phase-4 flat data files → per-tenant layout.

Python port of ``rust/crates/corlinman-gateway/src/legacy_migration.rs``.

Moves ``<data_dir>/<name>.sqlite`` into
``<data_dir>/tenants/default/<name>.sqlite`` so deployments running an
older build can upgrade in place without manual file shuffling. Gated by
``[tenants].enabled = true && [tenants].migrate_legacy_paths = true`` on
the Rust side; the Python entrypoint mirrors the same gate before
invoking :func:`migrate_legacy_data_files`.

Idempotency rules (kept byte-for-byte with the Rust impl):

* missing legacy file ⇒ skip silently (clean install / already migrated);
* destination already populated ⇒ leave both untouched and ``warning`` —
  reconciliation is an operator decision, never a silent overwrite;
* rename uses :func:`os.rename`, which is atomic on the same filesystem
  via POSIX ``rename(2)``. Cross-device renames raise
  :class:`OSError` (``EXDEV``); we log and continue with the remaining
  entries rather than abort the boot.

The Rust version uses ``std::fs::rename`` (sync). We keep the Python port
sync as well — the migration runs once at boot, before the event loop is
spun up, and async file I/O would only buy us pretend-concurrency for a
five-element loop. The function is safe to call from inside an async
context (it doesn't block the loop noticeably).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

import structlog

from corlinman_server.tenancy import TenantId, tenant_db_path

logger = structlog.get_logger(__name__)

#: The per-tenant SQLite filenames Phase 4 migrates from the legacy flat
#: layout. Mirrors ``LEGACY_DB_NAMES`` in the Rust crate; keeping the
#: list here so a future per-tenant DB addition only has to touch one
#: place per language.
LEGACY_DB_NAMES: Final[tuple[str, ...]] = (
    "evolution",
    "kb",
    "sessions",
    "user_model",
    "agent_state",
)


def migrate_legacy_data_files(data_dir: Path | str) -> None:
    """Walk the legacy data files and rename each into the new
    per-tenant layout.

    Idempotent: see module docs for the exact rules. Always creates the
    target ``<data_dir>/tenants/default/`` directory (cheap; reused by
    later boot steps that open per-tenant pools), even when no legacy
    files exist — matches the Rust behaviour and keeps subsequent
    ``tenant_db_path`` opens from racing on ``mkdir``.
    """
    root = Path(data_dir)
    default = TenantId.legacy_default()
    target_root = root / "tenants" / default.as_str()

    try:
        target_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error(
            "legacy_migration.mkdir_failed",
            target=str(target_root),
            error=str(exc),
        )
        raise

    for name in LEGACY_DB_NAMES:
        legacy = root / f"{name}.sqlite"
        migrated = tenant_db_path(root, default, name)

        if not legacy.exists():
            continue

        if migrated.exists():
            logger.warning(
                "legacy_migration.already_migrated",
                legacy=str(legacy),
                migrated=str(migrated),
                detail=(
                    "legacy data file present alongside already-migrated "
                    "copy; leaving both untouched — operator should reconcile"
                ),
            )
            continue

        try:
            os.rename(legacy, migrated)
        except OSError as exc:
            # A single failed entry should not block the remaining four
            # from migrating. EXDEV (cross-device) and EACCES are the
            # usual culprits; both require operator action.
            logger.error(
                "legacy_migration.rename_failed",
                from_path=str(legacy),
                to_path=str(migrated),
                error=str(exc),
                detail=(
                    "phase 4 legacy data file rename failed; operator must "
                    "move the file manually"
                ),
            )
            continue

        logger.info(
            "legacy_migration.moved",
            from_path=str(legacy),
            to_path=str(migrated),
        )


__all__ = ["LEGACY_DB_NAMES", "migrate_legacy_data_files"]
