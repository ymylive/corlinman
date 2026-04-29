//! Filesystem-path helpers for per-tenant SQLite layout.
//!
//! All per-tenant DBs live under `<root>/tenants/<tenant>/<name>.sqlite`.
//! Centralising the layout in one function lets us:
//!
//! * grep for path leaks at audit time (any `format!("…/{name}.sqlite")`
//!   in another crate is a bug),
//! * change the layout in one place (e.g. add a hash-prefix shard if a
//!   single deployment ever sees more tenants than ext4 likes per dir),
//! * keep `?tenant=` query injection from escaping the data dir — the
//!   `TenantId` slug regex already excludes `/` and `.`, but plumbing
//!   the path build through `Path::join` rather than `format!` adds a
//!   second layer of "this can't traverse" defence.

use std::path::{Path, PathBuf};

use crate::id::TenantId;

/// Absolute (or root-relative) path to the directory holding all
/// per-tenant data files for `tenant`. Layout:
///
/// ```text
/// <root>/tenants/<tenant_id>/
/// ```
///
/// The tenants subdir is the boundary — every per-tenant file (SQLite
/// or otherwise) sits underneath it. Single-tenant legacy deployments
/// run as `<root>/tenants/default/`.
pub fn tenant_root_dir(root: &Path, tenant: &TenantId) -> PathBuf {
    root.join("tenants").join(tenant.as_str())
}

/// Full path for the per-tenant SQLite file named `name`, e.g.
/// `tenant_db_path(root, &acme, "evolution")` →
/// `<root>/tenants/acme/evolution.sqlite`.
///
/// `name` is taken bare (no `.sqlite` suffix) so call-sites read like
/// the legacy single-tenant constants (`evolution_db_path("evolution")`),
/// and so a future `name = "agent_state.bak"` couldn't accidentally
/// produce a double-suffix path.
pub fn tenant_db_path(root: &Path, tenant: &TenantId, name: &str) -> PathBuf {
    tenant_root_dir(root, tenant).join(format!("{name}.sqlite"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    #[test]
    fn root_dir_under_tenants_subdir() {
        let root = PathBuf::from("/data");
        let tenant = TenantId::new("acme").unwrap();
        assert_eq!(
            tenant_root_dir(&root, &tenant),
            PathBuf::from("/data/tenants/acme"),
        );
    }

    #[test]
    fn db_path_appends_sqlite_suffix_once() {
        let root = PathBuf::from("/data");
        let tenant = TenantId::new("acme").unwrap();
        assert_eq!(
            tenant_db_path(&root, &tenant, "evolution"),
            PathBuf::from("/data/tenants/acme/evolution.sqlite"),
        );
        assert_eq!(
            tenant_db_path(&root, &tenant, "kb"),
            PathBuf::from("/data/tenants/acme/kb.sqlite"),
        );
    }

    #[test]
    fn legacy_default_layout_is_predictable() {
        // Single-tenant compat: the legacy data-dir layout (e.g. a
        // sibling worktree built before Phase 4) becomes
        // `<root>/tenants/default/<name>.sqlite`. Migration from the
        // pre-Phase-4 flat layout is a separate boot step (see
        // `corlinman-gateway` migrate_legacy_data_files).
        let root = PathBuf::from("/data");
        let tenant = TenantId::default();
        assert_eq!(
            tenant_db_path(&root, &tenant, "evolution"),
            PathBuf::from("/data/tenants/default/evolution.sqlite"),
        );
    }

    #[test]
    fn relative_root_works_for_tests() {
        // Integration tests pass `tempdir().path()` which is absolute,
        // but unit tests / fixtures sometimes pass `Path::new(".")`.
        let root = PathBuf::from(".");
        let tenant = TenantId::new("acme").unwrap();
        let p = tenant_db_path(&root, &tenant, "evolution");
        // The join is structural — the leading `.` is preserved.
        assert!(p.ends_with("tenants/acme/evolution.sqlite"));
    }
}
