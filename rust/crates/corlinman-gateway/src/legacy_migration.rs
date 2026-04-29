//! Phase 4 W1 4-1A Item 5: one-shot boot migration that moves
//! pre-Phase-4 data files from `<data_dir>/<name>.sqlite` to
//! `<data_dir>/tenants/default/<name>.sqlite`, keeping legacy
//! deployments compatible with the new per-tenant directory layout.
//!
//! The migration is gated on `[tenants].enabled = true &&
//! [tenants].migrate_legacy_paths = true`. When both are off (the
//! default for any pre-Phase-4 config), this module is never called
//! and the gateway continues opening data files at the legacy paths.
//!
//! Idempotency rules:
//!
//! - If `<data_dir>/<name>.sqlite` does not exist, the entry is
//!   skipped (clean install or already-migrated boot).
//! - If `<data_dir>/tenants/default/<name>.sqlite` already exists,
//!   the migration **leaves the legacy file in place** rather than
//!   overwriting. Two files claiming to be the same DB is a manual-
//!   reconcile situation; an automatic rename would silently lose
//!   whichever side has fewer rows. The boot logs both paths at
//!   `tracing::warn` so the operator notices.
//! - The actual move uses `std::fs::rename` which is atomic on the
//!   same filesystem (POSIX `rename(2)`). Cross-device moves return
//!   `std::io::ErrorKind::CrossesDevices` and the migration aborts
//!   that entry with a `tracing::error` — operators must move the
//!   data manually in that case.
//!
//! Files migrated (matches the per-tenant SQLite set Phase 3.1 + Phase
//! 4 Item 1 schema-migrated):
//!
//! - `evolution.sqlite`
//! - `kb.sqlite`
//! - `sessions.sqlite`
//! - `user_model.sqlite`
//! - `agent_state.sqlite`

use std::path::Path;

use corlinman_tenant::tenant_db_path;

/// The five per-tenant SQLite filenames Phase 4 migrates from the
/// legacy flat layout into `<data_dir>/tenants/<tenant>/`.
const LEGACY_DB_NAMES: &[&str] = &[
    "evolution",
    "kb",
    "sessions",
    "user_model",
    "agent_state",
];

/// Walk the legacy data files and rename each into the new
/// `<data_dir>/tenants/default/` layout. Idempotent: see module
/// docs for the rules.
///
/// The function takes `&Path` rather than owning a `PathBuf` so the
/// caller can keep its boot-time `data_dir` value alive across the
/// rest of the runtime construction.
pub fn migrate_legacy_data_files(data_dir: &Path) -> std::io::Result<()> {
    use corlinman_tenant::TenantId;

    let default = TenantId::legacy_default();
    let target_root = data_dir.join("tenants").join(default.as_str());

    // Create the target directory once — `tenant_db_path` does not
    // create parents, and `fs::rename` requires the destination's
    // parent to exist.
    std::fs::create_dir_all(&target_root).map_err(|e| {
        tracing::error!(
            target = %target_root.display(),
            error = %e,
            "failed to create per-tenant data directory; legacy migration aborted",
        );
        e
    })?;

    for name in LEGACY_DB_NAMES {
        let legacy = data_dir.join(format!("{name}.sqlite"));
        let migrated = tenant_db_path(data_dir, &default, name);

        if !legacy.exists() {
            continue;
        }

        if migrated.exists() {
            tracing::warn!(
                legacy = %legacy.display(),
                migrated = %migrated.display(),
                "legacy data file present alongside already-migrated copy; \
                 leaving both untouched — operator should reconcile",
            );
            continue;
        }

        match std::fs::rename(&legacy, &migrated) {
            Ok(()) => {
                tracing::info!(
                    from = %legacy.display(),
                    to = %migrated.display(),
                    "phase 4 legacy data file moved into per-tenant layout",
                );
            }
            Err(e) => {
                tracing::error!(
                    from = %legacy.display(),
                    to = %migrated.display(),
                    error = %e,
                    "phase 4 legacy data file rename failed; \
                     operator must move the file manually",
                );
                // Carry on — a single failed entry should not block
                // the remaining four from migrating.
            }
        }
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use tempfile::TempDir;

    fn touch(path: &Path, body: &[u8]) {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).unwrap();
        }
        fs::write(path, body).unwrap();
    }

    #[test]
    fn empty_data_dir_is_a_noop() {
        let tmp = TempDir::new().unwrap();
        // Nothing should be created or moved.
        migrate_legacy_data_files(tmp.path()).unwrap();
        // The function does create the target directory tree
        // unconditionally (cheap; idempotent), but no DB files
        // should appear.
        assert!(tmp.path().join("tenants").join("default").is_dir());
        for name in LEGACY_DB_NAMES {
            assert!(!tmp.path().join(format!("{name}.sqlite")).exists());
            assert!(!tmp
                .path()
                .join("tenants")
                .join("default")
                .join(format!("{name}.sqlite"))
                .exists());
        }
    }

    #[test]
    fn moves_every_legacy_file_into_default_tenant_dir() {
        let tmp = TempDir::new().unwrap();
        // Bootstrap a pre-Phase-4 layout: every DB sits at the
        // legacy flat path with a unique payload so we can verify
        // identity-after-rename.
        for name in LEGACY_DB_NAMES {
            touch(
                &tmp.path().join(format!("{name}.sqlite")),
                format!("legacy-{name}").as_bytes(),
            );
        }

        migrate_legacy_data_files(tmp.path()).unwrap();

        for name in LEGACY_DB_NAMES {
            // Source gone.
            assert!(
                !tmp.path().join(format!("{name}.sqlite")).exists(),
                "legacy {name}.sqlite should have been moved",
            );
            // Destination present at the per-tenant path with the
            // same content (rename, not copy).
            let migrated = tmp
                .path()
                .join("tenants")
                .join("default")
                .join(format!("{name}.sqlite"));
            assert!(migrated.exists(), "migrated {name}.sqlite should exist");
            let body = fs::read(&migrated).unwrap();
            assert_eq!(body, format!("legacy-{name}").as_bytes());
        }
    }

    #[test]
    fn already_migrated_path_is_left_in_place() {
        let tmp = TempDir::new().unwrap();
        // Half-migrated state: legacy + new both present. The
        // helper should leave both alone (and warn).
        let legacy = tmp.path().join("evolution.sqlite");
        let migrated = tmp
            .path()
            .join("tenants")
            .join("default")
            .join("evolution.sqlite");
        touch(&legacy, b"legacy-evolution");
        touch(&migrated, b"migrated-evolution");

        migrate_legacy_data_files(tmp.path()).unwrap();

        assert!(legacy.exists(), "legacy file must not be deleted");
        assert!(migrated.exists(), "migrated file must not be deleted");
        assert_eq!(fs::read(&legacy).unwrap(), b"legacy-evolution");
        assert_eq!(fs::read(&migrated).unwrap(), b"migrated-evolution");
    }

    #[test]
    fn idempotent_second_run_after_full_migration() {
        let tmp = TempDir::new().unwrap();
        for name in LEGACY_DB_NAMES {
            touch(
                &tmp.path().join(format!("{name}.sqlite")),
                format!("v1-{name}").as_bytes(),
            );
        }
        migrate_legacy_data_files(tmp.path()).unwrap();
        // Second invocation: the legacy paths are already empty, so
        // the loop short-circuits each entry. No errors, no extra
        // moves.
        migrate_legacy_data_files(tmp.path()).unwrap();
        for name in LEGACY_DB_NAMES {
            assert!(!tmp.path().join(format!("{name}.sqlite")).exists());
            let migrated = tmp
                .path()
                .join("tenants")
                .join("default")
                .join(format!("{name}.sqlite"));
            assert_eq!(fs::read(&migrated).unwrap(), format!("v1-{name}").as_bytes());
        }
    }

    #[test]
    fn partial_legacy_set_only_moves_present_files() {
        let tmp = TempDir::new().unwrap();
        // Only `evolution.sqlite` and `kb.sqlite` exist; the rest
        // are absent. The helper should move just those two.
        touch(&tmp.path().join("evolution.sqlite"), b"e");
        touch(&tmp.path().join("kb.sqlite"), b"k");

        migrate_legacy_data_files(tmp.path()).unwrap();

        let target_root = tmp.path().join("tenants").join("default");
        assert!(target_root.join("evolution.sqlite").exists());
        assert!(target_root.join("kb.sqlite").exists());
        for name in ["sessions", "user_model", "agent_state"] {
            assert!(!target_root.join(format!("{name}.sqlite")).exists());
            assert!(!tmp.path().join(format!("{name}.sqlite")).exists());
        }
    }
}
