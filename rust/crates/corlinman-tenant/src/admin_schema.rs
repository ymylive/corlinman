//! Phase 4 W1 4-1A Item 4: schema + thin CRUD wrapper for the
//! root-level `tenants.sqlite` admin DB. This DB is **not**
//! per-tenant — it lives at `<data_dir>/tenants.sqlite` (singular,
//! no subdirectory) and stores the master list of tenants plus their
//! per-tenant admin credentials. The `corlinman-tenant::TenantPool`
//! and the gateway's tenant-scoping middleware both consult this
//! file at boot to determine which tenants exist and which admins
//! can sign in to each.
//!
//! Two tables, both append-mostly:
//!
//! - `tenants` — the canonical tenant roster. `tenant_id` is the
//!   slug-validated `TenantId` newtype value; `created_at` stamps
//!   the `corlinman tenant create` invocation; `deleted_at` is
//!   reserved for future soft-delete (Wave 2+) and is `NULL` while
//!   the row is active.
//! - `tenant_admins` — argon2id password hashes for every admin
//!   username scoped to a tenant. The primary key is
//!   `(tenant_id, username)` so multiple operators can manage a
//!   single tenant. `ON DELETE CASCADE` on the FK keeps the rows in
//!   sync if the parent tenant is ever hard-deleted.
//!
//! `AdminDb` is the user-facing wrapper. Cheap to clone (holds a
//! `SqlitePool`); the CLI opens it once per command and the gateway
//! will eventually open it once at boot to seed `allowed_tenants`.

use std::path::Path;
use std::str::FromStr;

use sqlx::sqlite::{
    SqliteConnectOptions, SqliteJournalMode, SqlitePoolOptions, SqliteSynchronous,
};
use sqlx::{Row, SqlitePool};

use crate::TenantId;

/// CREATE TABLE script for `tenants.sqlite`. Idempotent: re-applying
/// is safe against an existing file. New columns must land via an
/// idempotent ALTER (mirror the Phase 3.1 / Phase 4 Item 1 pattern in
/// other crates) — append a migration constant if/when the time
/// comes; v1 is column-stable.
pub const SCHEMA_SQL: &str = r#"
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id     TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    created_at    INTEGER NOT NULL,
    deleted_at    INTEGER
);

CREATE TABLE IF NOT EXISTS tenant_admins (
    tenant_id     TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    username      TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at    INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, username)
);

CREATE INDEX IF NOT EXISTS idx_tenants_active
    ON tenants(deleted_at) WHERE deleted_at IS NULL;
"#;

/// One row from `tenants`. `deleted_at` is `Some` only on soft-
/// deleted rows (reserved for Wave 2+); active rows have `None`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TenantRow {
    pub tenant_id: TenantId,
    pub display_name: String,
    pub created_at: i64,
    pub deleted_at: Option<i64>,
}

/// One row from `tenant_admins`. `password_hash` is the full
/// argon2id `$argon2id$...` encoded string — never a raw password.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AdminRow {
    pub tenant_id: TenantId,
    pub username: String,
    pub password_hash: String,
    pub created_at: i64,
}

/// Thin CRUD wrapper over the `tenants.sqlite` admin DB.
///
/// Cloneable: every field is `Arc`-wrapped via `SqlitePool`. The CLI
/// opens it once per invocation; the gateway opens it once at boot.
#[derive(Debug, Clone)]
pub struct AdminDb {
    pool: SqlitePool,
}

#[derive(Debug, thiserror::Error)]
pub enum AdminDbError {
    #[error("invalid sqlite url '{0}': {1}")]
    InvalidUrl(String, sqlx::Error),
    #[error("connect '{0}': {1}")]
    Connect(String, sqlx::Error),
    #[error("apply tenants.sqlite SCHEMA_SQL: {0}")]
    ApplySchema(sqlx::Error),
    #[error("tenant '{0}' already exists")]
    TenantExists(String),
    #[error("admin '{username}' already exists for tenant '{tenant}'")]
    AdminExists { tenant: String, username: String },
    #[error("sqlite: {0}")]
    Sqlx(#[from] sqlx::Error),
}

impl AdminDb {
    /// Open (or create) the admin DB at `path`. Applies [`SCHEMA_SQL`]
    /// idempotently. WAL + `synchronous=NORMAL` + `foreign_keys=ON`
    /// matches the rest of the corlinman SQLite stores.
    pub async fn open(path: &Path) -> Result<Self, AdminDbError> {
        let url = format!("sqlite://{}", path.display());

        let options = SqliteConnectOptions::from_str(&url)
            .map_err(|e| AdminDbError::InvalidUrl(url.clone(), e))?
            .create_if_missing(true)
            .journal_mode(SqliteJournalMode::Wal)
            .synchronous(SqliteSynchronous::Normal)
            .foreign_keys(true);

        let pool = SqlitePoolOptions::new()
            .max_connections(4)
            .connect_with(options)
            .await
            .map_err(|e| AdminDbError::Connect(url, e))?;

        sqlx::raw_sql(SCHEMA_SQL)
            .execute(&pool)
            .await
            .map_err(AdminDbError::ApplySchema)?;

        Ok(Self { pool })
    }

    /// Borrow the underlying pool. Useful for tests; production code
    /// should prefer the typed methods below.
    pub fn pool(&self) -> &SqlitePool {
        &self.pool
    }

    /// INSERT a new tenant row. Rejects duplicates with
    /// [`AdminDbError::TenantExists`] rather than letting the FK
    /// constraint surface as a generic sqlx error.
    pub async fn create_tenant(
        &self,
        tenant_id: &TenantId,
        display_name: &str,
        created_at: i64,
    ) -> Result<(), AdminDbError> {
        let res = sqlx::query(
            "INSERT INTO tenants (tenant_id, display_name, created_at) VALUES (?1, ?2, ?3)",
        )
        .bind(tenant_id.as_str())
        .bind(display_name)
        .bind(created_at)
        .execute(&self.pool)
        .await;

        match res {
            Ok(_) => Ok(()),
            Err(sqlx::Error::Database(e)) if e.kind() == sqlx::error::ErrorKind::UniqueViolation => {
                Err(AdminDbError::TenantExists(tenant_id.as_str().to_string()))
            }
            Err(e) => Err(AdminDbError::Sqlx(e)),
        }
    }

    /// INSERT a new admin row, scoped to a tenant.
    pub async fn add_admin(
        &self,
        tenant_id: &TenantId,
        username: &str,
        password_hash: &str,
        created_at: i64,
    ) -> Result<(), AdminDbError> {
        let res = sqlx::query(
            "INSERT INTO tenant_admins (tenant_id, username, password_hash, created_at) \
             VALUES (?1, ?2, ?3, ?4)",
        )
        .bind(tenant_id.as_str())
        .bind(username)
        .bind(password_hash)
        .bind(created_at)
        .execute(&self.pool)
        .await;

        match res {
            Ok(_) => Ok(()),
            Err(sqlx::Error::Database(e)) if e.kind() == sqlx::error::ErrorKind::UniqueViolation => {
                Err(AdminDbError::AdminExists {
                    tenant: tenant_id.as_str().to_string(),
                    username: username.to_string(),
                })
            }
            Err(e) => Err(AdminDbError::Sqlx(e)),
        }
    }

    /// All active tenants, ordered by `tenant_id` for stable output.
    pub async fn list_active(&self) -> Result<Vec<TenantRow>, AdminDbError> {
        let rows = sqlx::query(
            "SELECT tenant_id, display_name, created_at, deleted_at \
             FROM tenants WHERE deleted_at IS NULL \
             ORDER BY tenant_id ASC",
        )
        .fetch_all(&self.pool)
        .await?;

        let mut out = Vec::with_capacity(rows.len());
        for r in rows {
            let slug: String = r.get("tenant_id");
            let tenant_id = TenantId::new(slug.clone()).map_err(|e| {
                AdminDbError::Sqlx(sqlx::Error::Decode(format!("invalid tenant_id '{slug}': {e}").into()))
            })?;
            out.push(TenantRow {
                tenant_id,
                display_name: r.get("display_name"),
                created_at: r.get("created_at"),
                deleted_at: r.get("deleted_at"),
            });
        }
        Ok(out)
    }

    /// Fetch a single tenant by slug. Returns `Ok(None)` when no such
    /// row exists rather than an error — soft-deleted rows are
    /// returned as-is so the operator can see why a tenant they
    /// expect is "missing".
    pub async fn get(&self, tenant_id: &TenantId) -> Result<Option<TenantRow>, AdminDbError> {
        let row = sqlx::query(
            "SELECT tenant_id, display_name, created_at, deleted_at \
             FROM tenants WHERE tenant_id = ?1",
        )
        .bind(tenant_id.as_str())
        .fetch_optional(&self.pool)
        .await?;

        Ok(row.map(|r| TenantRow {
            tenant_id: tenant_id.clone(),
            display_name: r.get("display_name"),
            created_at: r.get("created_at"),
            deleted_at: r.get("deleted_at"),
        }))
    }

    /// Admins for a tenant, ordered by username.
    pub async fn list_admins(&self, tenant_id: &TenantId) -> Result<Vec<AdminRow>, AdminDbError> {
        let rows = sqlx::query(
            "SELECT tenant_id, username, password_hash, created_at \
             FROM tenant_admins WHERE tenant_id = ?1 ORDER BY username ASC",
        )
        .bind(tenant_id.as_str())
        .fetch_all(&self.pool)
        .await?;

        Ok(rows
            .into_iter()
            .map(|r| AdminRow {
                tenant_id: tenant_id.clone(),
                username: r.get("username"),
                password_hash: r.get("password_hash"),
                created_at: r.get("created_at"),
            })
            .collect())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    async fn fresh() -> (AdminDb, TempDir) {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("tenants.sqlite");
        let db = AdminDb::open(&path).await.unwrap();
        (db, tmp)
    }

    #[tokio::test]
    async fn open_creates_tables_idempotently() {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("tenants.sqlite");
        // Open twice; second open must not error and must observe an
        // empty roster.
        let _first = AdminDb::open(&path).await.unwrap();
        let second = AdminDb::open(&path).await.unwrap();
        assert!(second.list_active().await.unwrap().is_empty());
    }

    #[tokio::test]
    async fn create_tenant_round_trips() {
        let (db, _tmp) = fresh().await;
        let acme = TenantId::new("acme").unwrap();
        db.create_tenant(&acme, "Acme Corp", 1_700_000_000).await.unwrap();

        let row = db.get(&acme).await.unwrap().expect("just created");
        assert_eq!(row.tenant_id, acme);
        assert_eq!(row.display_name, "Acme Corp");
        assert_eq!(row.created_at, 1_700_000_000);
        assert_eq!(row.deleted_at, None);

        let listed = db.list_active().await.unwrap();
        assert_eq!(listed.len(), 1);
        assert_eq!(listed[0].tenant_id, acme);
    }

    #[tokio::test]
    async fn create_tenant_rejects_duplicate_slug() {
        let (db, _tmp) = fresh().await;
        let acme = TenantId::new("acme").unwrap();
        db.create_tenant(&acme, "First", 1).await.unwrap();
        let err = db
            .create_tenant(&acme, "Second", 2)
            .await
            .expect_err("duplicate must fail");
        assert!(matches!(err, AdminDbError::TenantExists(_)));
    }

    #[tokio::test]
    async fn add_admin_round_trips_and_lists_in_username_order() {
        let (db, _tmp) = fresh().await;
        let acme = TenantId::new("acme").unwrap();
        db.create_tenant(&acme, "Acme Corp", 1).await.unwrap();
        db.add_admin(&acme, "bob", "$argon2id$v=19$m=...$bobhash", 10)
            .await
            .unwrap();
        db.add_admin(&acme, "alice", "$argon2id$v=19$m=...$alicehash", 11)
            .await
            .unwrap();

        let admins = db.list_admins(&acme).await.unwrap();
        assert_eq!(admins.len(), 2);
        assert_eq!(admins[0].username, "alice");
        assert_eq!(admins[1].username, "bob");
        assert!(admins[0].password_hash.starts_with("$argon2id$"));
    }

    #[tokio::test]
    async fn add_admin_rejects_duplicate_username_per_tenant() {
        let (db, _tmp) = fresh().await;
        let acme = TenantId::new("acme").unwrap();
        db.create_tenant(&acme, "Acme Corp", 1).await.unwrap();
        db.add_admin(&acme, "alice", "$argon2id$h1", 1).await.unwrap();
        let err = db
            .add_admin(&acme, "alice", "$argon2id$h2", 2)
            .await
            .expect_err("duplicate username must fail");
        assert!(matches!(err, AdminDbError::AdminExists { .. }));
    }

    #[tokio::test]
    async fn add_admin_fails_when_parent_tenant_missing() {
        let (db, _tmp) = fresh().await;
        let ghost = TenantId::new("ghost").unwrap();
        // No `create_tenant` first → FK violation.
        let err = db
            .add_admin(&ghost, "alice", "$argon2id$h", 1)
            .await
            .expect_err("missing parent must fail");
        assert!(matches!(err, AdminDbError::Sqlx(_)));
    }

    #[tokio::test]
    async fn get_returns_none_for_unknown_tenant() {
        let (db, _tmp) = fresh().await;
        let nope = TenantId::new("never-existed").unwrap();
        assert!(db.get(&nope).await.unwrap().is_none());
    }

    #[tokio::test]
    async fn list_active_excludes_soft_deleted() {
        let (db, _tmp) = fresh().await;
        let acme = TenantId::new("acme").unwrap();
        let bravo = TenantId::new("bravo").unwrap();
        db.create_tenant(&acme, "Acme", 1).await.unwrap();
        db.create_tenant(&bravo, "Bravo", 2).await.unwrap();

        // Soft-delete `bravo` directly via SQL — there's no public
        // delete API in v1 (Wave 2+), but the partial index +
        // `deleted_at IS NULL` filter must already DTRT.
        sqlx::query("UPDATE tenants SET deleted_at = 99 WHERE tenant_id = ?1")
            .bind(bravo.as_str())
            .execute(db.pool())
            .await
            .unwrap();

        let active = db.list_active().await.unwrap();
        assert_eq!(active.len(), 1);
        assert_eq!(active[0].tenant_id, acme);

        // `get` still surfaces soft-deleted rows so operators can
        // diagnose why an expected tenant is missing from `list`.
        let bravo_row = db.get(&bravo).await.unwrap().unwrap();
        assert_eq!(bravo_row.deleted_at, Some(99));
    }
}
