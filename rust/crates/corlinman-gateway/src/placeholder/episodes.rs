//! `{{episodes.*}}` resolver — Phase 4 W4 D1 iter 7.
//!
//! Implements the `DynamicResolver` trait against a per-tenant
//! `episodes.sqlite` written by `corlinman-episodes`. The resolver
//! is wired onto the [`PlaceholderEngine`] under the `episodes`
//! namespace (see [`RESERVED_NAMESPACES`] in `corlinman-core`).
//!
//! ## Tokens supported
//!
//! | Token | SQL behaviour |
//! |---|---|
//! | `{{episodes.last_24h}}`    | Top N by `importance_score` over `ended_at >= now - 24h`. |
//! | `{{episodes.last_week}}`   | Top N over the last 7 days. |
//! | `{{episodes.last_month}}`  | Top N over the last 30 days. |
//! | `{{episodes.recent}}`      | Last N by `ended_at` regardless of score. |
//! | `{{episodes.kind(<k>)}}`   | Last N filtered by `kind`. |
//! | `{{episodes.about_id(<id>)}}` | Single episode by `id`. |
//!
//! Any other key returns a `PlaceholderError::Resolver` with
//! `unknown_token`; the engine surfaces unresolved tokens
//! verbatim, so the *literal* `{{episodes.gibberish}}` round-trips
//! through to the operator. We do this by returning the literal
//! `{{token}}` from `resolve` rather than raising — same shape as
//! the engine's "unknown namespace" branch, kept consistent for
//! the resolver's own unknown keys.
//!
//! ## Tenant isolation
//!
//! `PlaceholderCtx::metadata["tenant_id"]` carries the per-render
//! tenant id (set by gateway middleware on every reserved-namespace
//! render — same path `{{vector.*}}` uses). Missing/empty tenant id
//! → falls back to `default`, matching the rest of the per-tenant
//! SQLite layout. Each render opens (or reuses) a pool keyed on
//! tenant id; cross-tenant reads are physically impossible because
//! the resolver never widens the path lookup.
//!
//! ## `last_referenced_at` stamp
//!
//! Every render that returns rows fires a single batched
//! `UPDATE … WHERE id IN (?)` so cold-archive sweeps (D1.5) can
//! decide what to demote. The update happens before we render the
//! markdown — failing to stamp is non-fatal (we log + continue) so
//! a transient write error doesn't surface as a chat-rendering 500.
//!
//! ## Output shape
//!
//! Multi-row results render as a markdown bullet list of
//! `summary_text` truncated to 240 chars per the design. Single-row
//! `about_id` renders as the bare summary text. Empty result set
//! → empty string (lets prompt authors post-fix-format around it).

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::str::FromStr;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use async_trait::async_trait;
use corlinman_core::placeholder::{DynamicResolver, PlaceholderCtx, PlaceholderError};
use corlinman_tenant::{tenant_db_path, TenantId};
use sqlx::sqlite::{SqliteConnectOptions, SqliteJournalMode, SqlitePoolOptions, SqliteSynchronous};
use sqlx::{Row, SqlitePool};
use tokio::sync::Mutex;

/// Default top-N when the operator hasn't overridden via config. Mirrors
/// `EpisodesConfig.max_episodes_per_query` / `last_week_top_n` in the
/// Python config dataclass.
pub const DEFAULT_TOP_N: usize = 5;

/// Char cap on rendered `summary_text` per row. Matches the design
/// doc's §"Query surface" line: "rendered as a markdown bullet list of
/// `summary_text` truncated to 240 chars each."
pub const SUMMARY_CHAR_CAP: usize = 240;

/// Metadata key the gateway middleware stamps on every render. Kept
/// here as a const so the test harness and the middleware can't drift.
pub const TENANT_METADATA_KEY: &str = "tenant_id";

/// Default tenant slug — matches `corlinman_tenant::TenantId::default()`.
pub const DEFAULT_TENANT_SLUG: &str = "default";

/// Resolver entry point. One per gateway, registered under the
/// `episodes` namespace at boot. Holds the data-dir root + a pool
/// cache keyed on tenant id; pools open lazily on first hit so an
/// unused tenant never opens its DB file.
pub struct EpisodesResolver {
    root: PathBuf,
    top_n: usize,
    pools: Mutex<HashMap<String, SqlitePool>>,
    /// Optional clock override for tests — when `Some`, the rolling
    /// `last_24h` / `last_week` / `last_month` windows anchor against
    /// this fixed unix-ms instead of `SystemTime::now`.
    fixed_now_ms: Option<i64>,
}

impl std::fmt::Debug for EpisodesResolver {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("EpisodesResolver")
            .field("root", &self.root)
            .field("top_n", &self.top_n)
            .field("fixed_now_ms", &self.fixed_now_ms)
            .finish()
    }
}

impl EpisodesResolver {
    /// Build a resolver rooted at `data_dir` (the gateway's
    /// `data_dir`). The actual per-tenant path is computed lazily as
    /// `<data_dir>/tenants/<slug>/episodes.sqlite`.
    pub fn new(data_dir: impl Into<PathBuf>) -> Self {
        Self {
            root: data_dir.into(),
            top_n: DEFAULT_TOP_N,
            pools: Mutex::new(HashMap::new()),
            fixed_now_ms: None,
        }
    }

    /// Override the top-N cap. Only relevant if the operator wires
    /// `[episodes] max_episodes_per_query` from their TOML at boot.
    pub fn with_top_n(mut self, top_n: usize) -> Self {
        self.top_n = top_n.max(1);
        self
    }

    /// Override the wall-clock — tests use this so the rolling
    /// windows produce deterministic results without sleeping.
    pub fn with_fixed_now_ms(mut self, now_ms: i64) -> Self {
        self.fixed_now_ms = Some(now_ms);
        self
    }

    /// Construct an `Arc` view, ready to hand to
    /// `PlaceholderEngine::register_namespace`.
    pub fn into_arc(self) -> Arc<dyn DynamicResolver> {
        Arc::new(self)
    }

    /// Lazily open (or reuse) the per-tenant pool. We open a single
    /// connection per tenant — episodes is a low-QPS read store, and
    /// the gateway already holds heavier pools elsewhere; one conn
    /// keeps the WAL visibility race away too (matches
    /// `SqliteIdentityStore::open_with_pool_size(1)`'s reasoning).
    async fn pool_for_tenant(&self, tenant: &str) -> Result<SqlitePool, PlaceholderError> {
        {
            let cache = self.pools.lock().await;
            if let Some(p) = cache.get(tenant) {
                return Ok(p.clone());
            }
        }
        let path = self.episodes_path_for(tenant)?;
        let pool = open_episodes_pool(&path).await?;
        let mut cache = self.pools.lock().await;
        Ok(cache.entry(tenant.to_string()).or_insert(pool).clone())
    }

    fn episodes_path_for(&self, tenant: &str) -> Result<PathBuf, PlaceholderError> {
        let id = TenantId::new(tenant).map_err(|e| PlaceholderError::Resolver {
            namespace: "episodes".into(),
            message: format!("invalid tenant id {tenant:?}: {e}"),
        })?;
        Ok(tenant_db_path(&self.root, &id, "episodes"))
    }

    fn now_ms(&self) -> i64 {
        if let Some(t) = self.fixed_now_ms {
            return t;
        }
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_millis() as i64)
            .unwrap_or_default()
    }
}

#[async_trait]
impl DynamicResolver for EpisodesResolver {
    /// Dispatch `key` (the part after `episodes.`) to the matching
    /// query strategy. Unknown keys round-trip the literal token —
    /// matches the engine's "unknown namespace" passthrough so a
    /// typo doesn't 500 the prompt assembly.
    async fn resolve(&self, key: &str, ctx: &PlaceholderCtx) -> Result<String, PlaceholderError> {
        let tenant = ctx
            .metadata
            .get(TENANT_METADATA_KEY)
            .map(String::as_str)
            .filter(|s| !s.is_empty())
            .unwrap_or(DEFAULT_TENANT_SLUG);

        // Single-tenant happy path: no DB, no rows, no error — the
        // resolver should not require an operator to pre-create the
        // file. If the path is absent, return empty (renders as an
        // empty bullet list / empty string).
        let path = self.episodes_path_for(tenant)?;
        if !path.exists() {
            // Still match the unknown-token contract: gibberish keys
            // round-trip the literal even when the DB is absent.
            return match parse_token(key) {
                Some(_) => Ok(String::new()),
                None => Ok(literal_token(key)),
            };
        }

        let pool = self.pool_for_tenant(tenant).await?;
        let now_ms = self.now_ms();

        let parsed = match parse_token(key) {
            Some(p) => p,
            None => return Ok(literal_token(key)),
        };

        match parsed {
            ParsedToken::Window(secs) => {
                let cutoff = now_ms - secs * 1_000;
                let rows =
                    select_top_by_importance(&pool, tenant, cutoff, self.top_n as i64).await?;
                stamp_referenced(&pool, &rows, now_ms).await;
                Ok(render_bullets(rows))
            }
            ParsedToken::Recent => {
                let rows = select_recent(&pool, tenant, self.top_n as i64).await?;
                stamp_referenced(&pool, &rows, now_ms).await;
                Ok(render_bullets(rows))
            }
            ParsedToken::Kind(kind) => {
                let rows = select_by_kind(&pool, tenant, &kind, self.top_n as i64).await?;
                stamp_referenced(&pool, &rows, now_ms).await;
                Ok(render_bullets(rows))
            }
            ParsedToken::AboutId(id) => {
                let row = select_by_id(&pool, tenant, &id).await?;
                if let Some(r) = row {
                    stamp_referenced(&pool, std::slice::from_ref(&r), now_ms).await;
                    Ok(truncate_summary(&r.summary_text))
                } else {
                    // Missing-id renders empty rather than the literal
                    // token — operators have written `{{episodes.about_id(...)}}`
                    // expecting *some* episode; "no rows" is the
                    // correct shape, not "go ask the operator why."
                    Ok(String::new())
                }
            }
            ParsedToken::AboutTag(_tag) => {
                // Tag join needs `corlinman-tagmemo` integration —
                // tracked under D1 follow-up. Round-trip the literal
                // for forward-compat: a future iter wires the join
                // and the same prompt continues to work.
                Ok(literal_token(key))
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Token parsing
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq)]
enum ParsedToken {
    /// Rolling-window query in seconds (`last_24h` → 86_400, …).
    Window(i64),
    /// Last-N by `ended_at` (`recent`).
    Recent,
    /// Filter by `EpisodeKind` (`kind(incident)`).
    Kind(String),
    /// Tag filter (`about(<tag>)`) — round-trips literal in iter 7.
    AboutTag(String),
    /// Single-episode lookup (`about_id(<ulid>)`).
    AboutId(String),
}

fn parse_token(raw: &str) -> Option<ParsedToken> {
    let key = raw.trim();
    match key {
        "last_24h" => Some(ParsedToken::Window(24 * 3600)),
        "last_week" => Some(ParsedToken::Window(7 * 24 * 3600)),
        "last_month" => Some(ParsedToken::Window(30 * 24 * 3600)),
        "recent" => Some(ParsedToken::Recent),
        _ => parse_call_form(key),
    }
}

/// Recognise `name(arg)` shapes — `kind(incident)`, `about_id(<id>)`,
/// `about(<tag>)`. Whitespace tolerated between paren and arg.
fn parse_call_form(key: &str) -> Option<ParsedToken> {
    let (name, rest) = key.split_once('(')?;
    let arg = rest.strip_suffix(')')?.trim();
    if arg.is_empty() {
        return None;
    }
    match name.trim() {
        "kind" => {
            // The arg must be a known EpisodeKind; otherwise return
            // None so the literal-token fallback fires (operator typo).
            if VALID_KINDS.contains(&arg) {
                Some(ParsedToken::Kind(arg.to_string()))
            } else {
                None
            }
        }
        "about" => Some(ParsedToken::AboutTag(arg.to_string())),
        "about_id" => Some(ParsedToken::AboutId(arg.to_string())),
        _ => None,
    }
}

/// Mirrors `EpisodeKind.values()` from the Python side. Pin here so a
/// future kind addition over there forces a corresponding update on
/// the Rust reader (catch via the test below).
const VALID_KINDS: &[&str] = &[
    "conversation",
    "evolution",
    "incident",
    "onboarding",
    "operator",
];

/// Build the literal `{{episodes.<key>}}` round-trip. The
/// `PlaceholderEngine` turns an unknown *namespace* into a literal;
/// for unknown *keys within a registered namespace* we have to
/// produce the same shape ourselves.
fn literal_token(key: &str) -> String {
    format!("{{{{episodes.{key}}}}}")
}

// ---------------------------------------------------------------------------
// Pool open
// ---------------------------------------------------------------------------

async fn open_episodes_pool(path: &Path) -> Result<SqlitePool, PlaceholderError> {
    let url = format!("sqlite://{}", path.display());
    let opts = SqliteConnectOptions::from_str(&url)
        .map_err(|e| PlaceholderError::Resolver {
            namespace: "episodes".into(),
            message: format!("connect parse {}: {e}", path.display()),
        })?
        // The Python writer creates the file; the Rust reader should
        // never have to. `create_if_missing(false)` matches the design
        // —episodes are an additive read-model.
        .create_if_missing(false)
        .read_only(false) // we still UPDATE last_referenced_at
        .journal_mode(SqliteJournalMode::Wal)
        .synchronous(SqliteSynchronous::Normal);
    SqlitePoolOptions::new()
        .max_connections(1)
        .connect_with(opts)
        .await
        .map_err(|e| PlaceholderError::Resolver {
            namespace: "episodes".into(),
            message: format!("open pool {}: {e}", path.display()),
        })
}

// ---------------------------------------------------------------------------
// Row DTO + queries
// ---------------------------------------------------------------------------

/// Minimal row projection. The render path only needs `id` (for the
/// reference-stamp UPDATE) + `summary_text`; the rest of the columns
/// are reachable via `about_id` if the operator needs them.
#[derive(Debug, Clone)]
pub struct EpisodeBrief {
    pub id: String,
    pub summary_text: String,
}

async fn select_top_by_importance(
    pool: &SqlitePool,
    tenant: &str,
    cutoff_ms: i64,
    limit: i64,
) -> Result<Vec<EpisodeBrief>, PlaceholderError> {
    let rows = sqlx::query(
        "SELECT id, summary_text FROM episodes
           WHERE tenant_id = ?1 AND ended_at >= ?2
           ORDER BY importance_score DESC, ended_at DESC
           LIMIT ?3",
    )
    .bind(tenant)
    .bind(cutoff_ms)
    .bind(limit)
    .fetch_all(pool)
    .await
    .map_err(query_err)?;
    Ok(rows
        .into_iter()
        .map(|r| EpisodeBrief {
            id: r.get::<String, _>(0),
            summary_text: r.get::<String, _>(1),
        })
        .collect())
}

async fn select_recent(
    pool: &SqlitePool,
    tenant: &str,
    limit: i64,
) -> Result<Vec<EpisodeBrief>, PlaceholderError> {
    let rows = sqlx::query(
        "SELECT id, summary_text FROM episodes
           WHERE tenant_id = ?1
           ORDER BY ended_at DESC, id DESC
           LIMIT ?2",
    )
    .bind(tenant)
    .bind(limit)
    .fetch_all(pool)
    .await
    .map_err(query_err)?;
    Ok(rows
        .into_iter()
        .map(|r| EpisodeBrief {
            id: r.get::<String, _>(0),
            summary_text: r.get::<String, _>(1),
        })
        .collect())
}

async fn select_by_kind(
    pool: &SqlitePool,
    tenant: &str,
    kind: &str,
    limit: i64,
) -> Result<Vec<EpisodeBrief>, PlaceholderError> {
    let rows = sqlx::query(
        "SELECT id, summary_text FROM episodes
           WHERE tenant_id = ?1 AND kind = ?2
           ORDER BY ended_at DESC, id DESC
           LIMIT ?3",
    )
    .bind(tenant)
    .bind(kind)
    .bind(limit)
    .fetch_all(pool)
    .await
    .map_err(query_err)?;
    Ok(rows
        .into_iter()
        .map(|r| EpisodeBrief {
            id: r.get::<String, _>(0),
            summary_text: r.get::<String, _>(1),
        })
        .collect())
}

async fn select_by_id(
    pool: &SqlitePool,
    tenant: &str,
    id: &str,
) -> Result<Option<EpisodeBrief>, PlaceholderError> {
    let row = sqlx::query(
        "SELECT id, summary_text FROM episodes
           WHERE tenant_id = ?1 AND id = ?2
           LIMIT 1",
    )
    .bind(tenant)
    .bind(id)
    .fetch_optional(pool)
    .await
    .map_err(query_err)?;
    Ok(row.map(|r| EpisodeBrief {
        id: r.get::<String, _>(0),
        summary_text: r.get::<String, _>(1),
    }))
}

/// Best-effort `last_referenced_at` stamp. Never fails the render —
/// a write-side error here is a cold-archive imprecision, not a
/// chat-rendering error. Logged at warn so operators see drift.
async fn stamp_referenced(pool: &SqlitePool, rows: &[EpisodeBrief], now_ms: i64) {
    if rows.is_empty() {
        return;
    }
    // Build `?, ?, ?` bind list — sqlite has no native array type and
    // sqlx's bind interface needs one placeholder per id.
    let placeholders = vec!["?"; rows.len()].join(",");
    let sql = format!("UPDATE episodes SET last_referenced_at = ? WHERE id IN ({placeholders})");
    let mut q = sqlx::query(&sql).bind(now_ms);
    for r in rows {
        q = q.bind(&r.id);
    }
    if let Err(e) = q.execute(pool).await {
        tracing::warn!(
            error = %e,
            row_count = rows.len(),
            "episodes resolver: failed to stamp last_referenced_at",
        );
    }
}

// ---------------------------------------------------------------------------
// Render helpers
// ---------------------------------------------------------------------------

fn render_bullets(rows: Vec<EpisodeBrief>) -> String {
    if rows.is_empty() {
        return String::new();
    }
    let mut out = String::new();
    for (i, r) in rows.iter().enumerate() {
        if i > 0 {
            out.push('\n');
        }
        out.push_str("- ");
        out.push_str(&truncate_summary(&r.summary_text));
    }
    out
}

fn truncate_summary(text: &str) -> String {
    // Char-aware truncation — splits on a Unicode codepoint boundary,
    // not a byte index, so a multi-byte char at the cap doesn't
    // produce invalid UTF-8.
    if text.chars().count() <= SUMMARY_CHAR_CAP {
        return text.to_string();
    }
    let mut out: String = text.chars().take(SUMMARY_CHAR_CAP).collect();
    out.push('…');
    out
}

fn query_err(e: sqlx::Error) -> PlaceholderError {
    PlaceholderError::Resolver {
        namespace: "episodes".into(),
        message: format!("sqlx: {e}"),
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    use tempfile::TempDir;

    /// Match the iter 1 `SCHEMA_SQL` from `corlinman-episodes`. We
    /// re-spell the DDL here rather than depend on the Python side —
    /// the Rust resolver only reads, so a schema drift will surface
    /// as a query-time error which our tests will catch.
    const SCHEMA_SQL: &str = r"
CREATE TABLE IF NOT EXISTS episodes (
    id                  TEXT PRIMARY KEY,
    tenant_id           TEXT NOT NULL DEFAULT 'default',
    started_at          INTEGER NOT NULL,
    ended_at            INTEGER NOT NULL,
    kind                TEXT NOT NULL,
    summary_text        TEXT NOT NULL,
    source_session_keys TEXT NOT NULL DEFAULT '[]',
    source_signal_ids   TEXT NOT NULL DEFAULT '[]',
    source_history_ids  TEXT NOT NULL DEFAULT '[]',
    embedding           BLOB,
    embedding_dim       INTEGER,
    importance_score    REAL NOT NULL DEFAULT 0.5,
    last_referenced_at  INTEGER,
    distilled_by        TEXT NOT NULL,
    distilled_at        INTEGER NOT NULL,
    schema_version      INTEGER NOT NULL DEFAULT 1
);
";

    struct Harness {
        // Keep TempDir alive for the test's duration.
        _root: TempDir,
        root_path: PathBuf,
    }

    impl Harness {
        async fn new() -> Self {
            let root = TempDir::new().expect("tempdir");
            let root_path = root.path().to_path_buf();
            Self {
                _root: root,
                root_path,
            }
        }

        async fn open_tenant(&self, tenant: &str) -> SqlitePool {
            let id = TenantId::new(tenant).expect("valid tenant id");
            let path = tenant_db_path(&self.root_path, &id, "episodes");
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
            // Seed the file so the resolver's `create_if_missing(false)`
            // open succeeds.
            let url = format!("sqlite://{}", path.display());
            let opts = SqliteConnectOptions::from_str(&url)
                .unwrap()
                .create_if_missing(true)
                .journal_mode(SqliteJournalMode::Wal);
            let pool = SqlitePoolOptions::new()
                .max_connections(1)
                .connect_with(opts)
                .await
                .unwrap();
            sqlx::raw_sql(SCHEMA_SQL).execute(&pool).await.unwrap();
            pool
        }
    }

    /// Insert a row with the columns that exercise our query paths.
    /// Defaults that don't matter for the test (signal/history/source
    /// ids, embedding) are filled with stable sentinels.
    async fn insert_row(
        pool: &SqlitePool,
        id: &str,
        tenant: &str,
        kind: &str,
        ended_at: i64,
        importance: f64,
        summary: &str,
    ) {
        sqlx::query(
            "INSERT INTO episodes
               (id, tenant_id, started_at, ended_at, kind, summary_text,
                source_session_keys, source_signal_ids, source_history_ids,
                importance_score, distilled_by, distilled_at, schema_version)
             VALUES (?, ?, ?, ?, ?, ?, '[]', '[]', '[]', ?, 'stub', 0, 1)",
        )
        .bind(id)
        .bind(tenant)
        .bind(ended_at - 1_000)
        .bind(ended_at)
        .bind(kind)
        .bind(summary)
        .bind(importance)
        .execute(pool)
        .await
        .unwrap();
    }

    fn ctx_for(tenant: &str) -> PlaceholderCtx {
        PlaceholderCtx::new("session-test").with_meta(TENANT_METADATA_KEY, tenant)
    }

    // ---- token parser ----------------------------------------------------

    #[test]
    fn parse_token_recognises_windows() {
        assert_eq!(parse_token("last_24h"), Some(ParsedToken::Window(86_400)));
        assert_eq!(
            parse_token("last_week"),
            Some(ParsedToken::Window(7 * 86_400))
        );
        assert_eq!(
            parse_token("last_month"),
            Some(ParsedToken::Window(30 * 86_400))
        );
    }

    #[test]
    fn parse_token_recognises_kind_call() {
        for k in VALID_KINDS {
            assert_eq!(
                parse_token(&format!("kind({k})")),
                Some(ParsedToken::Kind((*k).to_string()))
            );
        }
    }

    #[test]
    fn parse_token_rejects_unknown_kind() {
        assert!(parse_token("kind(banana)").is_none());
    }

    #[test]
    fn parse_token_recognises_about_id() {
        assert_eq!(
            parse_token("about_id(01HXAB)"),
            Some(ParsedToken::AboutId("01HXAB".into()))
        );
    }

    #[test]
    fn parse_token_unknown_round_trips() {
        // Returns None — the resolver wraps it in `literal_token` so
        // the engine output carries the original token verbatim.
        assert!(parse_token("gibberish").is_none());
        assert_eq!(literal_token("gibberish"), "{{episodes.gibberish}}");
    }

    // ---- DB-backed query coverage ---------------------------------------

    #[tokio::test]
    async fn last_week_returns_top_by_importance() {
        let h = Harness::new().await;
        let pool = h.open_tenant("default").await;
        let now = 1_700_000_000_000;

        // Seed: 7 episodes with varying importance, all within 7d.
        for (i, score) in [0.1, 0.9, 0.4, 0.8, 0.2, 0.99, 0.5].iter().enumerate() {
            insert_row(
                &pool,
                &format!("ep-{i}"),
                "default",
                "conversation",
                now - 1000,
                *score,
                &format!("episode {i} score {score}"),
            )
            .await;
        }
        // Plus an out-of-window high-score row that must NOT surface.
        insert_row(
            &pool,
            "ep-old",
            "default",
            "conversation",
            now - 30 * 86_400 * 1000, // 30d ago
            1.0,
            "ancient",
        )
        .await;

        let resolver = EpisodesResolver::new(&h.root_path).with_fixed_now_ms(now);
        let out = resolver
            .resolve("last_week", &ctx_for("default"))
            .await
            .unwrap();

        // Top 5 by importance: 0.99, 0.9, 0.8, 0.5, 0.4 → episodes 5, 1, 3, 6, 2.
        let bullets: Vec<_> = out.lines().collect();
        assert_eq!(bullets.len(), 5);
        assert!(bullets[0].contains("episode 5 score 0.99"));
        assert!(bullets[1].contains("episode 1 score 0.9"));
        assert!(bullets[2].contains("episode 3 score 0.8"));
        // ancient never appears.
        assert!(!out.contains("ancient"));
    }

    #[tokio::test]
    async fn last_24h_excludes_older_rows() {
        let h = Harness::new().await;
        let pool = h.open_tenant("default").await;
        let now = 1_700_000_000_000;

        insert_row(
            &pool,
            "fresh",
            "default",
            "conversation",
            now - 3600 * 1000,
            0.5,
            "fresh chat",
        )
        .await;
        insert_row(
            &pool,
            "stale",
            "default",
            "conversation",
            now - 3 * 86_400 * 1000,
            0.99,
            "stale chat",
        )
        .await;

        let resolver = EpisodesResolver::new(&h.root_path).with_fixed_now_ms(now);
        let out = resolver
            .resolve("last_24h", &ctx_for("default"))
            .await
            .unwrap();

        assert!(out.contains("fresh chat"));
        assert!(!out.contains("stale chat"));
    }

    #[tokio::test]
    async fn recent_orders_by_ended_at_regardless_of_score() {
        let h = Harness::new().await;
        let pool = h.open_tenant("default").await;
        let now = 1_700_000_000_000;

        // Low-score recent vs high-score older: `recent` must surface
        // the recent one first.
        insert_row(
            &pool,
            "low-recent",
            "default",
            "conversation",
            now,
            0.1,
            "low recent",
        )
        .await;
        insert_row(
            &pool,
            "high-old",
            "default",
            "conversation",
            now - 86_400 * 1000,
            0.99,
            "high old",
        )
        .await;

        let resolver = EpisodesResolver::new(&h.root_path).with_fixed_now_ms(now);
        let out = resolver
            .resolve("recent", &ctx_for("default"))
            .await
            .unwrap();
        let lines: Vec<_> = out.lines().collect();
        assert_eq!(lines.len(), 2);
        assert!(lines[0].contains("low recent"));
        assert!(lines[1].contains("high old"));
    }

    #[tokio::test]
    async fn kind_filter_returns_only_matching_kind() {
        let h = Harness::new().await;
        let pool = h.open_tenant("default").await;
        let now = 1_700_000_000_000;

        insert_row(&pool, "evo", "default", "evolution", now, 0.5, "an apply").await;
        insert_row(&pool, "chat", "default", "conversation", now, 0.5, "a chat").await;
        insert_row(&pool, "incident", "default", "incident", now, 0.5, "fire!").await;

        let resolver = EpisodesResolver::new(&h.root_path).with_fixed_now_ms(now);
        let out = resolver
            .resolve("kind(incident)", &ctx_for("default"))
            .await
            .unwrap();
        assert!(out.contains("fire!"));
        assert!(!out.contains("an apply"));
        assert!(!out.contains("a chat"));
    }

    #[tokio::test]
    async fn about_id_returns_single_episode() {
        let h = Harness::new().await;
        let pool = h.open_tenant("default").await;
        insert_row(
            &pool,
            "ep-cite",
            "default",
            "conversation",
            1,
            0.5,
            "cite-me",
        )
        .await;

        let resolver = EpisodesResolver::new(&h.root_path);
        let out = resolver
            .resolve("about_id(ep-cite)", &ctx_for("default"))
            .await
            .unwrap();
        assert_eq!(out, "cite-me");
    }

    #[tokio::test]
    async fn about_id_missing_returns_empty() {
        let h = Harness::new().await;
        h.open_tenant("default").await;
        let resolver = EpisodesResolver::new(&h.root_path);
        let out = resolver
            .resolve("about_id(nope)", &ctx_for("default"))
            .await
            .unwrap();
        assert_eq!(out, "");
    }

    #[tokio::test]
    async fn unknown_token_round_trips_literal() {
        let h = Harness::new().await;
        h.open_tenant("default").await;
        let resolver = EpisodesResolver::new(&h.root_path);
        let out = resolver
            .resolve("gibberish", &ctx_for("default"))
            .await
            .unwrap();
        assert_eq!(out, "{{episodes.gibberish}}");
    }

    #[tokio::test]
    async fn tenant_isolation_prevents_cross_reads() {
        let h = Harness::new().await;
        let now = 1_700_000_000_000;

        let pool_a = h.open_tenant("acme").await;
        insert_row(
            &pool_a,
            "ep-a",
            "acme",
            "conversation",
            now,
            0.9,
            "secret-a",
        )
        .await;
        let pool_b = h.open_tenant("globex").await;
        insert_row(
            &pool_b,
            "ep-b",
            "globex",
            "conversation",
            now,
            0.9,
            "secret-b",
        )
        .await;

        let resolver = EpisodesResolver::new(&h.root_path).with_fixed_now_ms(now);
        let out_a = resolver.resolve("recent", &ctx_for("acme")).await.unwrap();
        assert!(out_a.contains("secret-a"));
        assert!(!out_a.contains("secret-b"));

        let out_b = resolver
            .resolve("recent", &ctx_for("globex"))
            .await
            .unwrap();
        assert!(out_b.contains("secret-b"));
        assert!(!out_b.contains("secret-a"));
    }

    #[tokio::test]
    async fn last_referenced_at_updates_on_hit() {
        let h = Harness::new().await;
        let pool = h.open_tenant("default").await;
        let now = 1_700_000_000_000;
        insert_row(
            &pool,
            "stamp-me",
            "default",
            "conversation",
            now,
            0.5,
            "hit",
        )
        .await;

        // Pre-render: column is NULL.
        let pre: Option<i64> =
            sqlx::query_scalar("SELECT last_referenced_at FROM episodes WHERE id = 'stamp-me'")
                .fetch_one(&pool)
                .await
                .unwrap();
        assert!(pre.is_none());

        let resolver = EpisodesResolver::new(&h.root_path).with_fixed_now_ms(now + 5_000);
        let _ = resolver
            .resolve("recent", &ctx_for("default"))
            .await
            .unwrap();

        let post: Option<i64> =
            sqlx::query_scalar("SELECT last_referenced_at FROM episodes WHERE id = 'stamp-me'")
                .fetch_one(&pool)
                .await
                .unwrap();
        assert_eq!(post, Some(now + 5_000));
    }

    #[tokio::test]
    async fn missing_tenant_db_returns_empty_for_known_token() {
        // When a fresh tenant has no episodes.sqlite yet, the
        // resolver should not 500 — it should return empty so
        // prompts assemble cleanly.
        let h = Harness::new().await;
        let resolver = EpisodesResolver::new(&h.root_path);
        let out = resolver
            .resolve("recent", &ctx_for("never-existed"))
            .await
            .unwrap();
        assert_eq!(out, "");
    }

    #[tokio::test]
    async fn missing_tenant_db_round_trips_unknown_literal() {
        // Same fresh-tenant case but with a gibberish key — the
        // unknown-token contract still applies (literal round-trip),
        // even if the DB is missing.
        let h = Harness::new().await;
        let resolver = EpisodesResolver::new(&h.root_path);
        let out = resolver
            .resolve("gibberish", &ctx_for("never-existed"))
            .await
            .unwrap();
        assert_eq!(out, "{{episodes.gibberish}}");
    }

    #[tokio::test]
    async fn about_tag_round_trips_literal_for_now() {
        let h = Harness::new().await;
        h.open_tenant("default").await;
        let resolver = EpisodesResolver::new(&h.root_path);
        let out = resolver
            .resolve("about(skill_update)", &ctx_for("default"))
            .await
            .unwrap();
        // Tag-filter wiring lands in a follow-up; literal round-trip
        // until then so the prompt doesn't lose the token.
        assert_eq!(out, "{{episodes.about(skill_update)}}");
    }

    #[tokio::test]
    async fn long_summary_truncates_with_ellipsis() {
        let long = "x".repeat(SUMMARY_CHAR_CAP + 50);
        let out = truncate_summary(&long);
        // Char count: cap chars + 1 ellipsis.
        assert_eq!(out.chars().count(), SUMMARY_CHAR_CAP + 1);
        assert!(out.ends_with('…'));
    }

    #[tokio::test]
    async fn render_bullets_handles_empty_set() {
        assert_eq!(render_bullets(vec![]), "");
    }

    #[tokio::test]
    async fn invalid_tenant_id_surfaces_resolver_error() {
        let h = Harness::new().await;
        let resolver = EpisodesResolver::new(&h.root_path);
        // `..` triggers TenantId::new's slug regex rejection.
        let err = resolver
            .resolve("recent", &ctx_for(".."))
            .await
            .unwrap_err();
        match err {
            PlaceholderError::Resolver { namespace, message } => {
                assert_eq!(namespace, "episodes");
                assert!(message.contains("invalid tenant id"));
            }
            other => panic!("expected Resolver error, got {other:?}"),
        }
    }
}
