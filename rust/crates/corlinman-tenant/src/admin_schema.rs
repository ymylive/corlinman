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
use std::time::{SystemTime, UNIX_EPOCH};

use sqlx::sqlite::{SqliteConnectOptions, SqliteJournalMode, SqlitePoolOptions, SqliteSynchronous};
use sqlx::{Row, SqlitePool};

use crate::TenantId;

/// Wall-clock unix-millis. Saturates at `i64::MAX` rather than panic
/// if the system clock is set absurdly far in the future; clamps to
/// 0 on pre-1970 clocks. Local helper so the federation API doesn't
/// pull in chrono / time just to stamp a row.
fn unix_now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| i64::try_from(d.as_millis()).unwrap_or(i64::MAX))
        .unwrap_or(0)
}

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

-- Phase 4 W2 B3 iter 1: per-tenant evolution federation opt-in roster.
-- Asymmetric directional peering: a row reads "tenant `peer_tenant_id`
-- accepts federated proposals from tenant `source_tenant_id`". A → B
-- opt-in does NOT imply B → A. Both slugs are TenantId values; the
-- crate enforces shape at the API boundary, not at the SQL layer, to
-- keep this table forward-compatible with future ID shapes.
-- `accepted_by` is the operator (admin username) who accepted on the
-- peer side; nullable so historical / system-seeded rows don't have to
-- pretend a human approved them.
CREATE TABLE IF NOT EXISTS tenant_federation_peers (
    peer_tenant_id   TEXT NOT NULL,
    source_tenant_id TEXT NOT NULL,
    accepted_at_ms   INTEGER NOT NULL,
    accepted_by      TEXT,
    PRIMARY KEY (peer_tenant_id, source_tenant_id)
);
CREATE INDEX IF NOT EXISTS idx_federation_peers_source
    ON tenant_federation_peers(source_tenant_id);

-- Phase 4 W3 C4 iter 2: per-(tenant, username) bearer tokens minted via
-- `POST /admin/api_keys` for native clients. Stores only the sha256 hash
-- of the cleartext token; the operator is shown the cleartext **once**
-- on the response to the mint call. Subsequent listings expose the
-- key id + label + scope + last_used_at, never the cleartext.
--
-- `scope` is a free-form string ("chat" today; future: "chat,admin",
-- "embeddings", etc.). We deliberately keep this textual rather than a
-- typed enum so adding a new scope at gateway layer doesn't require an
-- admin-DB schema migration. The gateway's auth middleware (when wired)
-- splits on comma and matches against the route's required scope.
--
-- `revoked_at` is `NULL` for active rows; populated to a unix-millis
-- stamp once an admin revokes the key. Revoked rows stay in the table
-- so audit trails survive — callers filter on `revoked_at IS NULL` to
-- get the active set.
CREATE TABLE IF NOT EXISTS tenant_api_keys (
    key_id        TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL REFERENCES tenants(tenant_id) ON DELETE CASCADE,
    username      TEXT NOT NULL,
    scope         TEXT NOT NULL,
    label         TEXT,
    token_hash    TEXT NOT NULL UNIQUE,
    created_at_ms INTEGER NOT NULL,
    last_used_at_ms INTEGER,
    revoked_at_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_api_keys_tenant_active
    ON tenant_api_keys(tenant_id) WHERE revoked_at_ms IS NULL;
CREATE INDEX IF NOT EXISTS idx_api_keys_token_hash
    ON tenant_api_keys(token_hash);
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

/// One row from `tenant_federation_peers` (Phase 4 W2 B3 iter 1).
///
/// Reads as: tenant `peer_tenant_id` accepts federated proposals
/// **from** tenant `source_tenant_id`. The opt-in is asymmetric — A
/// accepting from B does not imply B accepts from A.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FederationPeer {
    pub peer_tenant_id: TenantId,
    pub source_tenant_id: TenantId,
    pub accepted_at_ms: i64,
    pub accepted_by: Option<String>,
}

/// One row from `tenant_api_keys` (Phase 4 W3 C4 iter 2).
///
/// `token_hash` is the hex-encoded sha256 of the cleartext token —
/// **never** the cleartext itself. The cleartext is returned once from
/// [`AdminDb::mint_api_key`] and never persisted anywhere we can read
/// back; subsequent listings are hash-only.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ApiKeyRow {
    pub key_id: String,
    pub tenant_id: TenantId,
    pub username: String,
    pub scope: String,
    pub label: Option<String>,
    pub token_hash: String,
    pub created_at_ms: i64,
    pub last_used_at_ms: Option<i64>,
    pub revoked_at_ms: Option<i64>,
}

/// Result of [`AdminDb::mint_api_key`]. Carries the cleartext bearer
/// token in `token` — surface it to the operator immediately and drop
/// the struct; the row in the DB only retains the sha256 hash.
#[derive(Debug, Clone)]
pub struct MintedApiKey {
    pub row: ApiKeyRow,
    pub token: String,
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
            Err(sqlx::Error::Database(e))
                if e.kind() == sqlx::error::ErrorKind::UniqueViolation =>
            {
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
            Err(sqlx::Error::Database(e))
                if e.kind() == sqlx::error::ErrorKind::UniqueViolation =>
            {
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
                AdminDbError::Sqlx(sqlx::Error::Decode(
                    format!("invalid tenant_id '{slug}': {e}").into(),
                ))
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

    /// Phase 4 W2 B3 iter 1: register that `peer` accepts federated
    /// proposals from `source`. `accepted_at_ms` is sampled from
    /// `SystemTime::now()` at insert time so callers don't have to
    /// thread a clock — federation opt-in is an operator action, not
    /// a replayable signal. Idempotent via the composite primary
    /// key: adding the same `(peer, source)` pair twice is a no-op
    /// at the row level (the existing row's timestamp / `accepted_by`
    /// are preserved). Callers that want last-writer-wins semantics
    /// should `remove_federation_peer` first.
    pub async fn add_federation_peer(
        &self,
        peer: &TenantId,
        source: &TenantId,
        accepted_by: &str,
    ) -> Result<(), AdminDbError> {
        let accepted_at_ms = unix_now_ms();
        sqlx::query(
            "INSERT OR IGNORE INTO tenant_federation_peers \
             (peer_tenant_id, source_tenant_id, accepted_at_ms, accepted_by) \
             VALUES (?1, ?2, ?3, ?4)",
        )
        .bind(peer.as_str())
        .bind(source.as_str())
        .bind(accepted_at_ms)
        .bind(accepted_by)
        .execute(&self.pool)
        .await?;
        Ok(())
    }

    /// Revoke a federation opt-in. Returns `true` when a row was
    /// actually deleted, `false` when no matching row existed (so
    /// callers can distinguish idempotent revoke vs operator typo
    /// without a separate existence check).
    pub async fn remove_federation_peer(
        &self,
        peer: &TenantId,
        source: &TenantId,
    ) -> Result<bool, AdminDbError> {
        let res = sqlx::query(
            "DELETE FROM tenant_federation_peers \
             WHERE peer_tenant_id = ?1 AND source_tenant_id = ?2",
        )
        .bind(peer.as_str())
        .bind(source.as_str())
        .execute(&self.pool)
        .await?;
        Ok(res.rows_affected() > 0)
    }

    /// "What does tenant `peer` accept from?" — returns every row
    /// where this tenant is the receiving side, ordered by
    /// `source_tenant_id` for stable output.
    pub async fn list_federation_sources_for(
        &self,
        peer: &TenantId,
    ) -> Result<Vec<FederationPeer>, AdminDbError> {
        let rows = sqlx::query(
            "SELECT peer_tenant_id, source_tenant_id, accepted_at_ms, accepted_by \
             FROM tenant_federation_peers \
             WHERE peer_tenant_id = ?1 \
             ORDER BY source_tenant_id ASC",
        )
        .bind(peer.as_str())
        .fetch_all(&self.pool)
        .await?;
        Self::decode_federation_rows(rows)
    }

    /// "Who accepts from tenant `source`?" — returns every row where
    /// this tenant is the publishing side, ordered by
    /// `peer_tenant_id` for stable output. Used at rebroadcast time
    /// to fan a source apply out to interested peers (driven by the
    /// `idx_federation_peers_source` index).
    pub async fn list_federation_peers_of(
        &self,
        source: &TenantId,
    ) -> Result<Vec<FederationPeer>, AdminDbError> {
        let rows = sqlx::query(
            "SELECT peer_tenant_id, source_tenant_id, accepted_at_ms, accepted_by \
             FROM tenant_federation_peers \
             WHERE source_tenant_id = ?1 \
             ORDER BY peer_tenant_id ASC",
        )
        .bind(source.as_str())
        .fetch_all(&self.pool)
        .await?;
        Self::decode_federation_rows(rows)
    }

    fn decode_federation_rows(
        rows: Vec<sqlx::sqlite::SqliteRow>,
    ) -> Result<Vec<FederationPeer>, AdminDbError> {
        let mut out = Vec::with_capacity(rows.len());
        for r in rows {
            let peer_slug: String = r.get("peer_tenant_id");
            let source_slug: String = r.get("source_tenant_id");
            let peer_tenant_id = TenantId::new(peer_slug.clone()).map_err(|e| {
                AdminDbError::Sqlx(sqlx::Error::Decode(
                    format!("invalid peer_tenant_id '{peer_slug}': {e}").into(),
                ))
            })?;
            let source_tenant_id = TenantId::new(source_slug.clone()).map_err(|e| {
                AdminDbError::Sqlx(sqlx::Error::Decode(
                    format!("invalid source_tenant_id '{source_slug}': {e}").into(),
                ))
            })?;
            out.push(FederationPeer {
                peer_tenant_id,
                source_tenant_id,
                accepted_at_ms: r.get("accepted_at_ms"),
                accepted_by: r.get("accepted_by"),
            });
        }
        Ok(out)
    }

    /* ---------------- Phase 4 W3 C4 iter 2: tenant_api_keys ---------------- */

    /// Mint a new bearer token for `(tenant, username, scope)`.
    ///
    /// Generates a cryptographically random cleartext (`ck_` prefix +
    /// two `uuid::Uuid::new_v4().simple()` blobs concatenated → 67-char
    /// total token), stores its sha256 hash, and returns the [`MintedApiKey`]
    /// envelope. The cleartext lives only in the return value — once
    /// the caller drops it, recovery is impossible (modulo the hash
    /// inversion problem). Callers must surface it to the operator
    /// immediately.
    ///
    /// `key_id` is a separate uuid (also `simple()`-formatted) so the
    /// caller has a stable handle for `revoke_api_key` / `list_api_keys`
    /// without ever needing to re-display the cleartext token.
    ///
    /// Errors:
    ///   - [`AdminDbError::Sqlx`] on FK violation (tenant doesn't exist),
    ///   - [`AdminDbError::Sqlx`] wrapping `UniqueViolation` on the
    ///     near-impossible token-hash collision.
    pub async fn mint_api_key(
        &self,
        tenant_id: &TenantId,
        username: &str,
        scope: &str,
        label: Option<&str>,
    ) -> Result<MintedApiKey, AdminDbError> {
        let key_id = uuid::Uuid::new_v4().simple().to_string();
        let token = format!(
            "ck_{}{}",
            uuid::Uuid::new_v4().simple(),
            uuid::Uuid::new_v4().simple()
        );
        let token_hash = hash_api_key_token(&token);
        let created_at_ms = unix_now_ms();

        sqlx::query(
            "INSERT INTO tenant_api_keys \
             (key_id, tenant_id, username, scope, label, token_hash, created_at_ms) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
        )
        .bind(&key_id)
        .bind(tenant_id.as_str())
        .bind(username)
        .bind(scope)
        .bind(label)
        .bind(&token_hash)
        .bind(created_at_ms)
        .execute(&self.pool)
        .await?;

        Ok(MintedApiKey {
            row: ApiKeyRow {
                key_id,
                tenant_id: tenant_id.clone(),
                username: username.to_string(),
                scope: scope.to_string(),
                label: label.map(str::to_string),
                token_hash,
                created_at_ms,
                last_used_at_ms: None,
                revoked_at_ms: None,
            },
            token,
        })
    }

    /// List active (`revoked_at_ms IS NULL`) keys for a tenant, ordered
    /// by `created_at_ms DESC` so the UI's "most recent first" view is
    /// natural. Revoked rows stay in the table for audit but are
    /// excluded here; callers that need the full set should query the
    /// pool directly.
    pub async fn list_api_keys(
        &self,
        tenant_id: &TenantId,
    ) -> Result<Vec<ApiKeyRow>, AdminDbError> {
        let rows = sqlx::query(
            "SELECT key_id, tenant_id, username, scope, label, token_hash, \
                    created_at_ms, last_used_at_ms, revoked_at_ms \
             FROM tenant_api_keys \
             WHERE tenant_id = ?1 AND revoked_at_ms IS NULL \
             ORDER BY created_at_ms DESC",
        )
        .bind(tenant_id.as_str())
        .fetch_all(&self.pool)
        .await?;

        let mut out = Vec::with_capacity(rows.len());
        for r in rows {
            let slug: String = r.get("tenant_id");
            let row_tenant_id = TenantId::new(slug.clone()).map_err(|e| {
                AdminDbError::Sqlx(sqlx::Error::Decode(
                    format!("invalid tenant_id '{slug}': {e}").into(),
                ))
            })?;
            out.push(ApiKeyRow {
                key_id: r.get("key_id"),
                tenant_id: row_tenant_id,
                username: r.get("username"),
                scope: r.get("scope"),
                label: r.get("label"),
                token_hash: r.get("token_hash"),
                created_at_ms: r.get("created_at_ms"),
                last_used_at_ms: r.get("last_used_at_ms"),
                revoked_at_ms: r.get("revoked_at_ms"),
            });
        }
        Ok(out)
    }

    /// Revoke a key by `key_id`. Returns `true` when a row was actually
    /// flipped (active → revoked); `false` when the row was already
    /// revoked or doesn't exist. Idempotent.
    pub async fn revoke_api_key(&self, key_id: &str) -> Result<bool, AdminDbError> {
        let now = unix_now_ms();
        let res = sqlx::query(
            "UPDATE tenant_api_keys SET revoked_at_ms = ?1 \
             WHERE key_id = ?2 AND revoked_at_ms IS NULL",
        )
        .bind(now)
        .bind(key_id)
        .execute(&self.pool)
        .await?;
        Ok(res.rows_affected() > 0)
    }

    /// Verify a cleartext token. Returns the matching active row when
    /// the hash matches and `revoked_at_ms IS NULL`; `None` otherwise.
    /// Constant-time comparison is **not** required at this layer
    /// because we look up by hash directly — the SQL index makes the
    /// match an O(1) hash equality on indexed bytes. Updates
    /// `last_used_at_ms` on hit so the UI's "last used" column stays
    /// fresh.
    pub async fn verify_api_key(&self, token: &str) -> Result<Option<ApiKeyRow>, AdminDbError> {
        let hash = hash_api_key_token(token);
        let row = sqlx::query(
            "SELECT key_id, tenant_id, username, scope, label, token_hash, \
                    created_at_ms, last_used_at_ms, revoked_at_ms \
             FROM tenant_api_keys \
             WHERE token_hash = ?1 AND revoked_at_ms IS NULL",
        )
        .bind(&hash)
        .fetch_optional(&self.pool)
        .await?;

        let Some(r) = row else { return Ok(None) };
        let slug: String = r.get("tenant_id");
        let row_tenant_id = TenantId::new(slug.clone()).map_err(|e| {
            AdminDbError::Sqlx(sqlx::Error::Decode(
                format!("invalid tenant_id '{slug}': {e}").into(),
            ))
        })?;
        let key_id: String = r.get("key_id");

        // Best-effort `last_used_at_ms` bump. Failure here logs a warn
        // but does not deny verification — the operator's chat request
        // shouldn't fail on a stats column.
        let now = unix_now_ms();
        if let Err(err) =
            sqlx::query("UPDATE tenant_api_keys SET last_used_at_ms = ?1 WHERE key_id = ?2")
                .bind(now)
                .bind(&key_id)
                .execute(&self.pool)
                .await
        {
            tracing::warn!(error = %err, key_id = %key_id, "tenant_api_keys: last_used_at_ms bump failed");
        }

        Ok(Some(ApiKeyRow {
            key_id,
            tenant_id: row_tenant_id,
            username: r.get("username"),
            scope: r.get("scope"),
            label: r.get("label"),
            token_hash: r.get("token_hash"),
            created_at_ms: r.get("created_at_ms"),
            last_used_at_ms: Some(now),
            revoked_at_ms: r.get("revoked_at_ms"),
        }))
    }
}

/// Hash an api-key cleartext to its hex-encoded sha256 digest. Public
/// for the gateway's auth middleware so it can pre-hash tokens before
/// any DB call — verifying a token is then a simple equality check
/// over an indexed column.
pub fn hash_api_key_token(token: &str) -> String {
    use sha2::{Digest, Sha256};
    let mut h = Sha256::new();
    h.update(token.as_bytes());
    let digest = h.finalize();
    let mut s = String::with_capacity(digest.len() * 2);
    for b in digest {
        // Inline two-char hex emit; pulling in `hex` for one call is
        // not worth a workspace dep when sha256 outputs are 32 bytes.
        s.push(HEX[(b >> 4) as usize]);
        s.push(HEX[(b & 0x0f) as usize]);
    }
    s
}

const HEX: &[char; 16] = &[
    '0', '1', '2', '3', '4', '5', '6', '7', '8', '9', 'a', 'b', 'c', 'd', 'e', 'f',
];

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
        db.create_tenant(&acme, "Acme Corp", 1_700_000_000)
            .await
            .unwrap();

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
        db.add_admin(&acme, "alice", "$argon2id$h1", 1)
            .await
            .unwrap();
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

    // ----- Phase 4 W2 B3 iter 1: tenant_federation_peers -----

    #[tokio::test]
    async fn add_then_list_round_trip() {
        let (db, _tmp) = fresh().await;
        let acme = TenantId::new("acme").unwrap();
        let bravo = TenantId::new("bravo").unwrap();

        db.add_federation_peer(&acme, &bravo, "alice")
            .await
            .unwrap();

        let sources = db.list_federation_sources_for(&acme).await.unwrap();
        assert_eq!(sources.len(), 1);
        assert_eq!(sources[0].peer_tenant_id, acme);
        assert_eq!(sources[0].source_tenant_id, bravo);
        assert_eq!(sources[0].accepted_by.as_deref(), Some("alice"));
        // Stamp must be a sane unix-millis (post-2001) without us
        // having to thread a clock.
        assert!(sources[0].accepted_at_ms > 1_000_000_000_000);

        let peers = db.list_federation_peers_of(&bravo).await.unwrap();
        assert_eq!(peers.len(), 1);
        assert_eq!(peers[0].peer_tenant_id, acme);
        assert_eq!(peers[0].source_tenant_id, bravo);
    }

    #[tokio::test]
    async fn add_is_idempotent_via_unique_pk() {
        let (db, _tmp) = fresh().await;
        let acme = TenantId::new("acme").unwrap();
        let bravo = TenantId::new("bravo").unwrap();

        db.add_federation_peer(&acme, &bravo, "alice")
            .await
            .unwrap();
        // Second add must not error and must not duplicate the row.
        db.add_federation_peer(&acme, &bravo, "bob").await.unwrap();

        let sources = db.list_federation_sources_for(&acme).await.unwrap();
        assert_eq!(sources.len(), 1, "INSERT OR IGNORE must not duplicate");
        // First-writer-wins on idempotent re-add: the original
        // `accepted_by` is preserved so callers can rely on the stored
        // value being the operator who actually accepted.
        assert_eq!(sources[0].accepted_by.as_deref(), Some("alice"));
    }

    #[tokio::test]
    async fn remove_returns_true_on_hit_false_on_miss() {
        let (db, _tmp) = fresh().await;
        let acme = TenantId::new("acme").unwrap();
        let bravo = TenantId::new("bravo").unwrap();
        let charlie = TenantId::new("charlie").unwrap();

        db.add_federation_peer(&acme, &bravo, "alice")
            .await
            .unwrap();

        // Hit: row exists, gets deleted.
        let hit = db.remove_federation_peer(&acme, &bravo).await.unwrap();
        assert!(hit, "first remove must report rows_affected > 0");

        // Miss: row already gone.
        let miss_repeat = db.remove_federation_peer(&acme, &bravo).await.unwrap();
        assert!(!miss_repeat, "second remove on same pair must be false");

        // Miss: pair never existed.
        let miss_unknown = db.remove_federation_peer(&acme, &charlie).await.unwrap();
        assert!(!miss_unknown, "remove on never-added pair must be false");

        // Post-condition: no rows remain.
        assert!(db
            .list_federation_sources_for(&acme)
            .await
            .unwrap()
            .is_empty());
    }

    #[tokio::test]
    async fn asymmetry_holds() {
        // A → B opt-in (A accepts from B) must NOT show up when
        // listing what B accepts from. Asymmetric directional
        // peering is the entire point of the schema.
        let (db, _tmp) = fresh().await;
        let a = TenantId::new("alpha").unwrap();
        let b = TenantId::new("bravo").unwrap();

        db.add_federation_peer(&a, &b, "alice").await.unwrap();

        // A's perspective: yes, accepts from B.
        let a_sources = db.list_federation_sources_for(&a).await.unwrap();
        assert_eq!(a_sources.len(), 1);
        assert_eq!(a_sources[0].source_tenant_id, b);

        // B's perspective: accepts from nobody.
        let b_sources = db.list_federation_sources_for(&b).await.unwrap();
        assert!(b_sources.is_empty(), "B must not inherit A's opt-in");

        // From B's publishing side: A is a peer.
        let b_peers = db.list_federation_peers_of(&b).await.unwrap();
        assert_eq!(b_peers.len(), 1);
        assert_eq!(b_peers[0].peer_tenant_id, a);

        // From A's publishing side: nobody listens.
        let a_peers = db.list_federation_peers_of(&a).await.unwrap();
        assert!(a_peers.is_empty(), "A is not a source for anyone");
    }

    #[tokio::test]
    async fn accepted_by_is_recorded() {
        let (db, _tmp) = fresh().await;
        let acme = TenantId::new("acme").unwrap();
        let bravo = TenantId::new("bravo").unwrap();
        let charlie = TenantId::new("charlie").unwrap();

        db.add_federation_peer(&acme, &bravo, "alice-the-operator")
            .await
            .unwrap();
        db.add_federation_peer(&acme, &charlie, "bob-the-operator")
            .await
            .unwrap();

        let sources = db.list_federation_sources_for(&acme).await.unwrap();
        // Ordered by source_tenant_id ASC: bravo before charlie.
        assert_eq!(sources.len(), 2);
        assert_eq!(sources[0].source_tenant_id, bravo);
        assert_eq!(
            sources[0].accepted_by.as_deref(),
            Some("alice-the-operator")
        );
        assert_eq!(sources[1].source_tenant_id, charlie);
        assert_eq!(sources[1].accepted_by.as_deref(), Some("bob-the-operator"));
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

    /* ---------------- Phase 4 W3 C4 iter 2: tenant_api_keys ---------------- */

    #[tokio::test]
    async fn mint_api_key_returns_cleartext_then_hashes_in_db() {
        let (db, _tmp) = fresh().await;
        let acme = TenantId::new("acme").unwrap();
        db.create_tenant(&acme, "Acme", 1).await.unwrap();

        let minted = db
            .mint_api_key(&acme, "alice", "chat", Some("MacBook"))
            .await
            .unwrap();

        // Cleartext shape: `ck_` prefix + 64 hex chars.
        assert!(minted.token.starts_with("ck_"));
        assert_eq!(minted.token.len(), 67);

        // Stored row has hash, NOT cleartext.
        assert_ne!(minted.row.token_hash, minted.token);
        assert_eq!(minted.row.token_hash.len(), 64); // sha256 hex
        assert_eq!(minted.row.token_hash, hash_api_key_token(&minted.token));
        assert_eq!(minted.row.username, "alice");
        assert_eq!(minted.row.scope, "chat");
        assert_eq!(minted.row.label.as_deref(), Some("MacBook"));
        assert_eq!(minted.row.tenant_id, acme);
        assert!(minted.row.last_used_at_ms.is_none());
        assert!(minted.row.revoked_at_ms.is_none());
    }

    #[tokio::test]
    async fn mint_api_key_rejects_unknown_tenant_via_fk() {
        let (db, _tmp) = fresh().await;
        let ghost = TenantId::new("ghost").unwrap();
        // No `create_tenant` first — FK violation surfaces as Sqlx err.
        let err = db
            .mint_api_key(&ghost, "alice", "chat", None)
            .await
            .expect_err("missing parent tenant must reject mint");
        assert!(matches!(err, AdminDbError::Sqlx(_)));
    }

    #[tokio::test]
    async fn list_api_keys_orders_by_created_desc_and_excludes_revoked() {
        let (db, _tmp) = fresh().await;
        let acme = TenantId::new("acme").unwrap();
        db.create_tenant(&acme, "Acme", 1).await.unwrap();

        let k1 = db
            .mint_api_key(&acme, "alice", "chat", Some("first"))
            .await
            .unwrap();
        // Sleep 2ms so created_at_ms advances; with millisecond precision
        // back-to-back inserts can land in the same tick.
        tokio::time::sleep(std::time::Duration::from_millis(2)).await;
        let k2 = db
            .mint_api_key(&acme, "bob", "chat", Some("second"))
            .await
            .unwrap();
        tokio::time::sleep(std::time::Duration::from_millis(2)).await;
        let k3 = db
            .mint_api_key(&acme, "carol", "chat", Some("third"))
            .await
            .unwrap();

        // Most recent first.
        let listed = db.list_api_keys(&acme).await.unwrap();
        assert_eq!(listed.len(), 3);
        assert_eq!(listed[0].key_id, k3.row.key_id);
        assert_eq!(listed[1].key_id, k2.row.key_id);
        assert_eq!(listed[2].key_id, k1.row.key_id);

        // Revoke the middle one — list excludes it.
        let flipped = db.revoke_api_key(&k2.row.key_id).await.unwrap();
        assert!(flipped, "first revoke must report a hit");

        let listed_after = db.list_api_keys(&acme).await.unwrap();
        assert_eq!(listed_after.len(), 2);
        assert_eq!(listed_after[0].key_id, k3.row.key_id);
        assert_eq!(listed_after[1].key_id, k1.row.key_id);

        // Re-revoking is a no-op miss (idempotent).
        let again = db.revoke_api_key(&k2.row.key_id).await.unwrap();
        assert!(!again, "second revoke on same key_id must be false");
    }

    #[tokio::test]
    async fn verify_api_key_round_trip_and_bumps_last_used() {
        let (db, _tmp) = fresh().await;
        let acme = TenantId::new("acme").unwrap();
        db.create_tenant(&acme, "Acme", 1).await.unwrap();
        let minted = db.mint_api_key(&acme, "alice", "chat", None).await.unwrap();

        // Sentinel value before verify so we can assert the bump moved it.
        assert!(minted.row.last_used_at_ms.is_none());

        let verified = db
            .verify_api_key(&minted.token)
            .await
            .unwrap()
            .expect("freshly minted token must verify");
        assert_eq!(verified.key_id, minted.row.key_id);
        assert_eq!(verified.tenant_id, acme);
        assert!(verified.last_used_at_ms.is_some());

        // List view also sees the bump (re-read from DB, not from
        // the verify return value).
        let listed = db.list_api_keys(&acme).await.unwrap();
        assert_eq!(listed.len(), 1);
        assert!(listed[0].last_used_at_ms.is_some());
    }

    #[tokio::test]
    async fn verify_api_key_rejects_unknown_token() {
        let (db, _tmp) = fresh().await;
        let acme = TenantId::new("acme").unwrap();
        db.create_tenant(&acme, "Acme", 1).await.unwrap();
        let _ = db.mint_api_key(&acme, "alice", "chat", None).await.unwrap();

        let none = db.verify_api_key("ck_does_not_exist_12345").await.unwrap();
        assert!(none.is_none());
    }

    #[tokio::test]
    async fn verify_api_key_rejects_revoked_token() {
        let (db, _tmp) = fresh().await;
        let acme = TenantId::new("acme").unwrap();
        db.create_tenant(&acme, "Acme", 1).await.unwrap();
        let minted = db.mint_api_key(&acme, "alice", "chat", None).await.unwrap();
        // Sanity check: pre-revoke verify hits.
        assert!(db.verify_api_key(&minted.token).await.unwrap().is_some());
        // Revoke + post-revoke verify must miss even though the hash is
        // still present in the table.
        assert!(db.revoke_api_key(&minted.row.key_id).await.unwrap());
        assert!(db.verify_api_key(&minted.token).await.unwrap().is_none());
    }

    #[test]
    fn hash_api_key_token_matches_known_sha256() {
        // sha256("hello") = 2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824
        // — pinned so a stray hashing-impl swap surfaces here.
        let h = hash_api_key_token("hello");
        assert_eq!(
            h,
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        );
    }
}
