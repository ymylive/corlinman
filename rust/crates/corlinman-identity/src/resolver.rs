//! `IdentityStore` trait — the single async surface the gateway
//! middleware and admin routes call into. Backed by
//! [`crate::store::SqliteIdentityStore`].
//!
//! Iteration 3 lands the read/write surface (`resolve_or_create`,
//! `lookup`, `aliases_for`). Verification-phrase methods (`issue_phrase`,
//! `redeem_phrase`) ship in iteration 4.

use async_trait::async_trait;
use sqlx::Row;
use time::format_description::well_known::Rfc3339;
use time::OffsetDateTime;

use crate::error::IdentityError;
use crate::store::SqliteIdentityStore;
use crate::types::{BindingKind, ChannelAlias, UserId};

/// Storage-agnostic surface for identity resolution.
///
/// All three methods are tenant-scoped at the *store* layer — the
/// caller selects which tenant they're operating against by picking
/// which `<data_dir>/tenants/<slug>/user_identity.sqlite` file the
/// store was opened from. Per-call tenant arguments would be
/// redundant and risk drift between the path and the column.
#[async_trait]
pub trait IdentityStore: Send + Sync {
    /// Resolve the canonical [`UserId`] for an incoming message. If
    /// the `(channel, channel_user_id)` pair is already known, returns
    /// the bound `user_id`. If new, mints a fresh `user_id` and
    /// records an [`BindingKind::Auto`] alias for it.
    ///
    /// Idempotent under concurrent first-call races: two simultaneous
    /// callers for the same pair both observe the same `UserId`.
    /// Implementation handles UNIQUE-constraint conflicts internally
    /// by retrying the read path so the winner's row becomes the
    /// loser's return value.
    async fn resolve_or_create(
        &self,
        channel: &str,
        channel_user_id: &str,
        display_name_hint: Option<&str>,
    ) -> Result<UserId, IdentityError>;

    /// Look up without minting. Returns `None` when the alias is
    /// unknown — used by admin surfaces and tooling that want a
    /// "does this alias exist yet?" check without side effects.
    async fn lookup(
        &self,
        channel: &str,
        channel_user_id: &str,
    ) -> Result<Option<UserId>, IdentityError>;

    /// Every alias bound to `user_id`. Used by `/admin/identity/:user_id`
    /// and by the trait-merge job (B2 follow-up) to enumerate channels
    /// for a unified user. Empty vec when the user has no aliases —
    /// rare but possible if all aliases were operator-merged out.
    async fn aliases_for(
        &self,
        user_id: &UserId,
    ) -> Result<Vec<ChannelAlias>, IdentityError>;
}

#[async_trait]
impl IdentityStore for SqliteIdentityStore {
    async fn resolve_or_create(
        &self,
        channel: &str,
        channel_user_id: &str,
        display_name_hint: Option<&str>,
    ) -> Result<UserId, IdentityError> {
        if channel.is_empty() {
            return Err(IdentityError::InvalidInput("channel must be non-empty"));
        }
        if channel_user_id.is_empty() {
            return Err(IdentityError::InvalidInput(
                "channel_user_id must be non-empty",
            ));
        }

        // Fast path: the alias already exists. The vast majority of
        // chat requests hit this path (one mint per human-channel,
        // many lookups thereafter) so it dominates throughput.
        if let Some(existing) = self.lookup(channel, channel_user_id).await? {
            return Ok(existing);
        }

        // Slow path: mint a new user + alias atomically. We retry
        // once on UNIQUE conflict — that race only triggers when two
        // concurrent first-callers for the same pair arrive
        // simultaneously, and the loser's retry just re-reads the
        // winner's freshly-committed row.
        match self
            .insert_new_user_and_alias(channel, channel_user_id, display_name_hint)
            .await
        {
            Ok(user_id) => Ok(user_id),
            Err(IdentityError::Storage { source, .. }) if is_unique_violation(&source) => {
                // Concurrent winner committed first; their row is now
                // visible. Re-read.
                self.lookup(channel, channel_user_id)
                    .await?
                    .ok_or_else(|| {
                        IdentityError::Storage {
                            op: "resolve_or_create_retry",
                            // Synthesize a not-quite-right sqlx error
                            // for the variant; a true racey retry that
                            // sees no row is an integrity violation, so
                            // surfacing it as Storage is more accurate
                            // than InvalidInput.
                            source: sqlx::Error::RowNotFound,
                        }
                    })
            }
            Err(other) => Err(other),
        }
    }

    async fn lookup(
        &self,
        channel: &str,
        channel_user_id: &str,
    ) -> Result<Option<UserId>, IdentityError> {
        let row: Option<String> = sqlx::query_scalar(
            "SELECT user_id FROM user_aliases \
             WHERE channel = ?1 AND channel_user_id = ?2",
        )
        .bind(channel)
        .bind(channel_user_id)
        .fetch_optional(self.pool())
        .await
        .map_err(|e| IdentityError::Storage {
            op: "lookup",
            source: e,
        })?;
        Ok(row.map(UserId::from_str))
    }

    async fn aliases_for(
        &self,
        user_id: &UserId,
    ) -> Result<Vec<ChannelAlias>, IdentityError> {
        let rows = sqlx::query(
            "SELECT channel, channel_user_id, user_id, created_at, binding_kind \
             FROM user_aliases \
             WHERE user_id = ?1 \
             ORDER BY created_at ASC",
        )
        .bind(user_id.as_str())
        .fetch_all(self.pool())
        .await
        .map_err(|e| IdentityError::Storage {
            op: "aliases_for",
            source: e,
        })?;

        let mut out = Vec::with_capacity(rows.len());
        for row in rows {
            let channel: String = row.get("channel");
            let channel_user_id: String = row.get("channel_user_id");
            let user_id_str: String = row.get("user_id");
            let created_at_str: String = row.get("created_at");
            let binding_kind_str: String = row.get("binding_kind");
            let created_at = OffsetDateTime::parse(&created_at_str, &Rfc3339).map_err(|e| {
                tracing::warn!(
                    user_id = %user_id_str,
                    created_at = %created_at_str,
                    error = %e,
                    "aliases_for: skipping row with unparseable created_at",
                );
                IdentityError::Storage {
                    op: "aliases_for_parse_ts",
                    source: sqlx::Error::ColumnDecode {
                        index: "created_at".into(),
                        source: Box::new(e),
                    },
                }
            })?;
            out.push(ChannelAlias {
                channel,
                channel_user_id,
                user_id: UserId::from_str(user_id_str),
                binding_kind: BindingKind::from_str(&binding_kind_str),
                created_at,
            });
        }
        Ok(out)
    }
}

/// Crate-private helper: write a fresh `(user_identities, user_aliases)`
/// pair in one transaction. Errors with the underlying sqlx error so
/// the caller can introspect for UNIQUE-constraint violations and
/// retry the read path.
impl SqliteIdentityStore {
    async fn insert_new_user_and_alias(
        &self,
        channel: &str,
        channel_user_id: &str,
        display_name_hint: Option<&str>,
    ) -> Result<UserId, IdentityError> {
        let user_id = UserId::generate();
        let now = OffsetDateTime::now_utc()
            .format(&Rfc3339)
            .map_err(|e| IdentityError::Storage {
                op: "format_ts",
                source: sqlx::Error::ColumnDecode {
                    index: "now".into(),
                    source: Box::new(e),
                },
            })?;

        let mut tx = self
            .pool()
            .begin()
            .await
            .map_err(|e| IdentityError::Storage {
                op: "begin",
                source: e,
            })?;

        sqlx::query(
            "INSERT INTO user_identities \
             (user_id, display_name, created_at, updated_at, confidence) \
             VALUES (?1, ?2, ?3, ?3, 1.0)",
        )
        .bind(user_id.as_str())
        .bind(display_name_hint)
        .bind(&now)
        .execute(&mut *tx)
        .await
        .map_err(|e| IdentityError::Storage {
            op: "insert_user_identity",
            source: e,
        })?;

        sqlx::query(
            "INSERT INTO user_aliases \
             (channel, channel_user_id, user_id, created_at, binding_kind) \
             VALUES (?1, ?2, ?3, ?4, 'auto')",
        )
        .bind(channel)
        .bind(channel_user_id)
        .bind(user_id.as_str())
        .bind(&now)
        .execute(&mut *tx)
        .await
        .map_err(|e| IdentityError::Storage {
            op: "insert_user_alias",
            source: e,
        })?;

        tx.commit().await.map_err(|e| IdentityError::Storage {
            op: "commit",
            source: e,
        })?;

        Ok(user_id)
    }
}

/// Identify a UNIQUE-constraint violation in a sqlx error chain. SQLite
/// surfaces these as a database error whose message contains "UNIQUE
/// constraint failed". String-matching is fragile across sqlite versions
/// in principle but pinned in practice — sqlx's wire format hasn't
/// changed for years and the workspace pins sqlx 0.7.
fn is_unique_violation(err: &sqlx::Error) -> bool {
    if let sqlx::Error::Database(db_err) = err {
        let msg = db_err.message();
        return msg.contains("UNIQUE constraint failed");
    }
    false
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::{identity_db_path, SqliteIdentityStore};
    use corlinman_tenant::TenantId;
    use tempfile::TempDir;

    /// Build a fresh store under a tempdir + tenant. Pool size = 1 to
    /// dodge the WAL cross-conn visibility race that the rest of the
    /// workspace's per-tenant stores have hit (matches the
    /// `EvolutionStore::open_with_pool_size(1)` test convention from
    /// commit `26a721e`).
    async fn fresh(tmp: &TempDir) -> SqliteIdentityStore {
        let tenant = TenantId::legacy_default();
        let path = identity_db_path(tmp.path(), &tenant);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        SqliteIdentityStore::open_with_pool_size(&path, 1)
            .await
            .unwrap()
    }

    #[tokio::test]
    async fn resolve_or_create_mints_for_unknown_pair() {
        let tmp = TempDir::new().unwrap();
        let store = fresh(&tmp).await;

        let user_id = store
            .resolve_or_create("qq", "1234", Some("Alice"))
            .await
            .unwrap();
        assert_eq!(user_id.as_str().len(), 26, "ULID is 26 chars");
    }

    #[tokio::test]
    async fn resolve_or_create_returns_same_id_on_repeat() {
        let tmp = TempDir::new().unwrap();
        let store = fresh(&tmp).await;

        let first = store.resolve_or_create("qq", "1234", None).await.unwrap();
        let second = store.resolve_or_create("qq", "1234", None).await.unwrap();
        assert_eq!(first, second, "same pair must resolve to same user_id");
    }

    #[tokio::test]
    async fn resolve_or_create_distinct_for_different_channels() {
        let tmp = TempDir::new().unwrap();
        let store = fresh(&tmp).await;

        let qq = store.resolve_or_create("qq", "1234", None).await.unwrap();
        let tg = store
            .resolve_or_create("telegram", "1234", None)
            .await
            .unwrap();
        // Same channel_user_id, different channels → two distinct
        // humans until verification proves otherwise.
        assert_ne!(qq, tg);
    }

    #[tokio::test]
    async fn resolve_or_create_concurrent_first_calls_yield_one_id() {
        let tmp = TempDir::new().unwrap();
        let store = fresh(&tmp).await;

        // 32 simultaneous first-callers for the same pair. The
        // serialized SQLite write path means at most one INSERT
        // succeeds; the others must observe the winner via the
        // retry path.
        let mut handles = Vec::new();
        for _ in 0..32 {
            let s = store.clone();
            handles.push(tokio::spawn(async move {
                s.resolve_or_create("qq", "1234", None).await.unwrap()
            }));
        }
        let mut ids = Vec::new();
        for h in handles {
            ids.push(h.await.unwrap());
        }
        let first = &ids[0];
        for id in &ids[1..] {
            assert_eq!(id, first, "concurrent first-callers must agree");
        }
    }

    #[tokio::test]
    async fn lookup_returns_none_for_unknown() {
        let tmp = TempDir::new().unwrap();
        let store = fresh(&tmp).await;
        assert!(store.lookup("qq", "missing").await.unwrap().is_none());
    }

    #[tokio::test]
    async fn lookup_returns_user_id_after_resolve() {
        let tmp = TempDir::new().unwrap();
        let store = fresh(&tmp).await;
        let minted = store.resolve_or_create("qq", "777", None).await.unwrap();
        let looked_up = store.lookup("qq", "777").await.unwrap().unwrap();
        assert_eq!(minted, looked_up);
    }

    #[tokio::test]
    async fn aliases_for_returns_all_bindings_in_creation_order() {
        let tmp = TempDir::new().unwrap();
        let store = fresh(&tmp).await;

        let uid = store
            .resolve_or_create("qq", "primary", Some("Alice"))
            .await
            .unwrap();

        // Manually bind a second alias to the same user_id by
        // direct SQL — verification-phrase merging lands in iter 4,
        // but the read path needs to handle multi-alias users now.
        sqlx::query(
            "INSERT INTO user_aliases \
             (channel, channel_user_id, user_id, created_at, binding_kind) \
             VALUES (?1, ?2, ?3, ?4, 'verified')",
        )
        .bind("telegram")
        .bind("9876")
        .bind(uid.as_str())
        .bind(
            OffsetDateTime::now_utc()
                .format(&Rfc3339)
                .unwrap(),
        )
        .execute(store.pool())
        .await
        .unwrap();

        let aliases = store.aliases_for(&uid).await.unwrap();
        assert_eq!(aliases.len(), 2);
        // Ordered by created_at ASC; QQ was first.
        assert_eq!(aliases[0].channel, "qq");
        assert_eq!(aliases[0].binding_kind, BindingKind::Auto);
        assert_eq!(aliases[1].channel, "telegram");
        assert_eq!(aliases[1].binding_kind, BindingKind::Verified);
        // Both share the same canonical user_id.
        assert_eq!(aliases[0].user_id, uid);
        assert_eq!(aliases[1].user_id, uid);
    }

    #[tokio::test]
    async fn aliases_for_unknown_user_returns_empty() {
        let tmp = TempDir::new().unwrap();
        let store = fresh(&tmp).await;
        let phantom = UserId::generate();
        assert!(store.aliases_for(&phantom).await.unwrap().is_empty());
    }

    #[tokio::test]
    async fn resolve_or_create_rejects_empty_channel() {
        let tmp = TempDir::new().unwrap();
        let store = fresh(&tmp).await;
        let err = store
            .resolve_or_create("", "1234", None)
            .await
            .expect_err("empty channel must fail");
        assert!(matches!(err, IdentityError::InvalidInput(_)));
    }

    #[tokio::test]
    async fn resolve_or_create_rejects_empty_channel_user_id() {
        let tmp = TempDir::new().unwrap();
        let store = fresh(&tmp).await;
        let err = store
            .resolve_or_create("qq", "", None)
            .await
            .expect_err("empty channel_user_id must fail");
        assert!(matches!(err, IdentityError::InvalidInput(_)));
    }
}
