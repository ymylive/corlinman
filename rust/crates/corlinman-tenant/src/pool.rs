//! `TenantPool` — multi-DB pool wrapper keyed by `(TenantId, db_name)`.
//!
//! Phase 2/3 each persistence module (`EvolutionStore`, `SqliteStore` for
//! kb, `SqliteSessionStore`, …) opened *one* `SqlitePool` against a
//! single fixed path. Phase 4 needs N tenants × M DB files, all opened
//! on demand and cached.
//!
//! `TenantPool` is intentionally **schema-agnostic**. It hands out raw
//! `SqlitePool`s; downstream crates clone-and-bind their own typed
//! repos against them. We do not centralise schema bootstrap here
//! because each crate already owns its `SCHEMA_SQL` + idempotent ALTER
//! block (`evolution::store::open` etc.) — duplicating that logic would
//! couple this crate to schema versions it has no business knowing.
//!
//! Concurrency:
//!   * the inner cache is a `Mutex<HashMap<...>>`, locked only for the
//!     "find or insert" step — pool open itself happens outside the
//!     lock so two simultaneous opens against *different* (tenant, db)
//!     pairs don't serialise on each other,
//!   * the open-race window for the *same* pair is closed by a
//!     "double-checked locking" pattern: re-check the map after the
//!     `connect_with` future resolves, and if another thread won the
//!     race we drop our pool and use theirs.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::str::FromStr;
use std::sync::Arc;
use std::time::Duration;

use sqlx::sqlite::{SqliteConnectOptions, SqliteJournalMode, SqlitePoolOptions, SqliteSynchronous};
use sqlx::SqlitePool;
use tokio::sync::Mutex;

use crate::id::TenantId;
use crate::path::{tenant_db_path, tenant_root_dir};

/// Errors emitted by [`TenantPool::get_or_open`].
#[derive(Debug, thiserror::Error)]
pub enum TenantPoolError {
    /// The tenant's directory could not be created (permission, full
    /// disk, parent missing, etc). Wrap rather than swallow so the
    /// gateway logs surface the underlying `io::Error::kind()`.
    #[error("create tenant dir {path}: {source}")]
    CreateDir {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
    /// `SqliteConnectOptions::from_str` rejected the SQLite URL we
    /// derived from the path — usually means the path contains a
    /// character `sqlx` rejects in URLs (rare since `TenantId` is a
    /// slug, but file-name `name` is also caller-controlled).
    #[error("invalid sqlite url {url}: {source}")]
    InvalidUrl {
        url: String,
        #[source]
        source: sqlx::Error,
    },
    /// Pool open / first-connection failed. Surfaces SQLite's
    /// underlying error so a missing `pragma journal_mode` failure
    /// (read-only mount, etc) reaches the operator.
    #[error("connect sqlite {url}: {source}")]
    Connect {
        url: String,
        #[source]
        source: sqlx::Error,
    },
}

/// Composite key into the inner pool map. We keep this private so the
/// public API takes `(&TenantId, &str)` and doesn't leak the `String`
/// allocation.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
struct Key {
    tenant: TenantId,
    db_name: String,
}

/// Multi-DB pool wrapper. Cheap to clone (single `Arc` bump); every
/// clone shares the same underlying cache.
#[derive(Debug, Clone)]
pub struct TenantPool {
    /// Filesystem root holding `tenants/<tenant>/<name>.sqlite`. Always
    /// the gateway's `data_dir` in production; tempdirs in tests.
    root: Arc<Path>,
    inner: Arc<Mutex<HashMap<Key, SqlitePool>>>,
    /// Connection budget per `(tenant, db_name)` pool. Defaults to 8
    /// (matches `EvolutionStore::open` precedent). Per-pool cap rather
    /// than process-wide because contention bounds are also per-pool.
    max_connections: u32,
}

impl TenantPool {
    /// Build an empty pool wrapper rooted at `root`. Pools open
    /// lazily on first [`Self::get_or_open`] call; empty wrapper is
    /// cheap so tests / stripped-down builds don't pay a connection
    /// cost they won't use.
    pub fn new(root: impl AsRef<Path>) -> Self {
        Self {
            root: Arc::from(root.as_ref()),
            inner: Arc::new(Mutex::new(HashMap::new())),
            max_connections: 8,
        }
    }

    /// Override the per-pool connection cap (default 8). Builder
    /// pattern keeps the common `TenantPool::new(root)` form short.
    pub fn with_max_connections(mut self, n: u32) -> Self {
        self.max_connections = n;
        self
    }

    /// Filesystem root the wrapper was opened against. Tests use this
    /// to assert pool paths landed in the expected tempdir.
    pub fn root(&self) -> &Path {
        &self.root
    }

    /// Resolved path for `(tenant, db_name)`. Does **not** create the
    /// file — exposed so admin / migration code can probe whether the
    /// file already exists before deciding to open.
    pub fn db_path(&self, tenant: &TenantId, db_name: &str) -> PathBuf {
        tenant_db_path(&self.root, tenant, db_name)
    }

    /// Open (or return cached) `SqlitePool` for `(tenant, db_name)`.
    ///
    /// Creates the parent directory tree on first open so callers
    /// don't have to mkdir before invoking us. WAL +
    /// `synchronous=NORMAL` matches the rest of the codebase.
    pub async fn get_or_open(
        &self,
        tenant: &TenantId,
        db_name: &str,
    ) -> Result<SqlitePool, TenantPoolError> {
        let key = Key {
            tenant: tenant.clone(),
            db_name: db_name.to_owned(),
        };

        // Fast path: already cached.
        {
            let guard = self.inner.lock().await;
            if let Some(pool) = guard.get(&key) {
                return Ok(pool.clone());
            }
        }

        // Slow path: create dir + open pool *outside* the lock so two
        // first-time opens on different (tenant, db) pairs don't
        // serialise. We re-check inside the lock at the end for the
        // same-pair race.
        let dir = tenant_root_dir(&self.root, tenant);
        if let Err(source) = std::fs::create_dir_all(&dir) {
            return Err(TenantPoolError::CreateDir { path: dir, source });
        }

        let path = tenant_db_path(&self.root, tenant, db_name);
        let url = format!("sqlite://{}", path.display());
        let options = SqliteConnectOptions::from_str(&url)
            .map_err(|source| TenantPoolError::InvalidUrl {
                url: url.clone(),
                source,
            })?
            .create_if_missing(true)
            .journal_mode(SqliteJournalMode::Wal)
            .synchronous(SqliteSynchronous::Normal)
            .foreign_keys(true)
            .busy_timeout(Duration::from_secs(5));

        let pool = SqlitePoolOptions::new()
            .max_connections(self.max_connections)
            .connect_with(options)
            .await
            .map_err(|source| TenantPoolError::Connect { url, source })?;

        // Same-pair race close: another caller may have raced us in.
        // If so, drop our pool and use theirs so we don't leak file
        // descriptors or end up with two pools fighting for the same
        // WAL file.
        let mut guard = self.inner.lock().await;
        if let Some(existing) = guard.get(&key) {
            drop(pool);
            return Ok(existing.clone());
        }
        guard.insert(key, pool.clone());
        Ok(pool)
    }

    /// True iff the pool for `(tenant, db_name)` is already cached.
    /// Tests use this to assert lazy-open behaviour without forcing a
    /// connection. Public so the gateway's `tenant create` path can
    /// short-circuit a re-open after migrations.
    pub async fn is_cached(&self, tenant: &TenantId, db_name: &str) -> bool {
        let key = Key {
            tenant: tenant.clone(),
            db_name: db_name.to_owned(),
        };
        let guard = self.inner.lock().await;
        guard.contains_key(&key)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn acme() -> TenantId {
        TenantId::new("acme").unwrap()
    }

    fn bravo() -> TenantId {
        TenantId::new("bravo").unwrap()
    }

    #[tokio::test]
    async fn opens_pool_lazily_and_caches() {
        let tmp = TempDir::new().unwrap();
        let pool = TenantPool::new(tmp.path());

        let tenant = acme();

        assert!(!pool.is_cached(&tenant, "evolution").await);

        let p1 = pool.get_or_open(&tenant, "evolution").await.unwrap();
        assert!(pool.is_cached(&tenant, "evolution").await);

        let p2 = pool.get_or_open(&tenant, "evolution").await.unwrap();
        // SqlitePool wraps an Arc — two clones of the same underlying
        // pool share state; we sanity-check by issuing a query through
        // both.
        sqlx::query("SELECT 1").execute(&p1).await.unwrap();
        sqlx::query("SELECT 1").execute(&p2).await.unwrap();
    }

    #[tokio::test]
    async fn isolates_pools_per_tenant() {
        let tmp = TempDir::new().unwrap();
        let pool = TenantPool::new(tmp.path());

        // Each tenant gets its own DB file.
        let _a = pool.get_or_open(&acme(), "evolution").await.unwrap();
        let _b = pool.get_or_open(&bravo(), "evolution").await.unwrap();

        let p_acme = tmp.path().join("tenants/acme/evolution.sqlite");
        let p_bravo = tmp.path().join("tenants/bravo/evolution.sqlite");
        assert!(p_acme.exists(), "acme db should exist at {p_acme:?}");
        assert!(p_bravo.exists(), "bravo db should exist at {p_bravo:?}");

        // Distinct files: writing to one does not appear in the other.
        let acme_pool = pool.get_or_open(&acme(), "evolution").await.unwrap();
        sqlx::query("CREATE TABLE marker (x INTEGER)")
            .execute(&acme_pool)
            .await
            .unwrap();
        let bravo_pool = pool.get_or_open(&bravo(), "evolution").await.unwrap();
        // The marker table is acme-only; sqlite_master on bravo must
        // not contain it.
        let row: Option<(String,)> =
            sqlx::query_as("SELECT name FROM sqlite_master WHERE type='table' AND name='marker'")
                .fetch_optional(&bravo_pool)
                .await
                .unwrap();
        assert!(row.is_none(), "bravo db must not see acme marker table");
    }

    #[tokio::test]
    async fn isolates_pools_per_db_name() {
        let tmp = TempDir::new().unwrap();
        let pool = TenantPool::new(tmp.path());
        let tenant = acme();

        let _evol = pool.get_or_open(&tenant, "evolution").await.unwrap();
        let _kb = pool.get_or_open(&tenant, "kb").await.unwrap();

        assert!(tmp.path().join("tenants/acme/evolution.sqlite").exists());
        assert!(tmp.path().join("tenants/acme/kb.sqlite").exists());
    }

    #[tokio::test]
    async fn db_path_does_not_create_file() {
        let tmp = TempDir::new().unwrap();
        let pool = TenantPool::new(tmp.path());
        let tenant = acme();

        // Probing the path is allowed before open; it must not touch
        // the filesystem (admin / migration code relies on this).
        let p = pool.db_path(&tenant, "evolution");
        assert_eq!(p, tmp.path().join("tenants/acme/evolution.sqlite"));
        assert!(!p.exists());
    }

    #[tokio::test]
    async fn concurrent_first_open_does_not_panic_or_deadlock() {
        // Two tasks racing on the same (tenant, db) pair. The
        // double-checked-lock pattern in `get_or_open` should make one
        // win and the other observe the cached pool.
        let tmp = TempDir::new().unwrap();
        let pool = TenantPool::new(tmp.path());
        let p1 = pool.clone();
        let p2 = pool.clone();
        let t = acme();
        let t1 = t.clone();
        let t2 = t.clone();

        let h1 = tokio::spawn(async move { p1.get_or_open(&t1, "evolution").await.is_ok() });
        let h2 = tokio::spawn(async move { p2.get_or_open(&t2, "evolution").await.is_ok() });
        assert!(h1.await.unwrap());
        assert!(h2.await.unwrap());

        // Exactly one cached entry.
        assert!(pool.is_cached(&t, "evolution").await);
    }
}
