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
use crate::types::{BindingKind, ChannelAlias, UserId, VerificationPhrase};

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
    async fn aliases_for(&self, user_id: &UserId) -> Result<Vec<ChannelAlias>, IdentityError>;

    /// Page through `user_identities`, ordered by `created_at DESC`
    /// so the most-recently-minted user lands on top of the admin
    /// list view. `limit` is clamped to `[1, 200]` at the call site
    /// to keep an unbounded `LIMIT 0` query from being expensive
    /// on a tenant with millions of users.
    ///
    /// Returns `(user_id, display_name, alias_count)` triples — the
    /// `alias_count` is computed via a `LEFT JOIN ... GROUP BY` in
    /// the impl so the admin UI can paint "QQ + Telegram" next to
    /// each row without N+1 follow-up queries.
    async fn list_users(&self, limit: u32, offset: u32) -> Result<Vec<UserSummary>, IdentityError>;

    /// Issue a fresh verification phrase for `user_id` on the
    /// `(channel, channel_user_id)` pair the operator confirmed maps
    /// to that user. The phrase expires in
    /// [`crate::DEFAULT_TTL_MIN`] minutes; the caller echoes it on
    /// the source channel for the human to redeem on the other side.
    ///
    /// Hoisted onto the trait (rather than left on the concrete
    /// `SqliteIdentityStore`) so admin routes can issue phrases
    /// against any future backend through the `Arc<dyn IdentityStore>`
    /// they hold.
    async fn issue_phrase(
        &self,
        user_id: &UserId,
        channel: &str,
        channel_user_id: &str,
    ) -> Result<VerificationPhrase, IdentityError>;

    /// Operator-driven manual merge. Reattributes every alias bound
    /// to `from_user_id` to `into_user_id` with `binding_kind =
    /// 'operator'`, then deletes the orphaned `from_user_id` row.
    /// Audit trail: `decided_by` is the operator's username so a
    /// downstream audit log can record who pushed the merge through.
    ///
    /// Errors:
    /// - [`IdentityError::UserNotFound`] when either user_id is missing.
    /// - [`IdentityError::InvalidInput`] when both ids are equal (no-op
    ///   merges are a programming error, not a degraded path).
    ///
    /// Returns the surviving `into_user_id` so the admin handler can
    /// echo it back without a follow-up read.
    async fn merge_users(
        &self,
        into_user_id: &UserId,
        from_user_id: &UserId,
        decided_by: &str,
    ) -> Result<UserId, IdentityError>;
}

/// One row in [`IdentityStore::list_users`]. Wire shape: matches the
/// UI's `UserSummary` interface (which iter 7 will define under
/// `ui/lib/api/identity.ts`).
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize, PartialEq, Eq)]
pub struct UserSummary {
    pub user_id: UserId,
    pub display_name: Option<String>,
    /// Number of aliases bound to this user_id at query time.
    pub alias_count: i64,
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
                self.lookup(channel, channel_user_id).await?.ok_or_else(|| {
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
        Ok(row.map(UserId::from))
    }

    async fn list_users(&self, limit: u32, offset: u32) -> Result<Vec<UserSummary>, IdentityError> {
        let limit = limit.clamp(1, 200) as i64;
        let offset = offset as i64;
        let rows = sqlx::query(
            "SELECT u.user_id, u.display_name, COUNT(a.channel) AS alias_count \
             FROM user_identities u \
             LEFT JOIN user_aliases a ON a.user_id = u.user_id \
             GROUP BY u.user_id \
             ORDER BY u.created_at DESC \
             LIMIT ?1 OFFSET ?2",
        )
        .bind(limit)
        .bind(offset)
        .fetch_all(self.pool())
        .await
        .map_err(|e| IdentityError::Storage {
            op: "list_users",
            source: e,
        })?;

        let mut out = Vec::with_capacity(rows.len());
        for row in rows {
            let user_id_str: String = row.get("user_id");
            let display_name: Option<String> = row.get("display_name");
            let alias_count: i64 = row.get("alias_count");
            out.push(UserSummary {
                user_id: UserId::from(user_id_str),
                display_name,
                alias_count,
            });
        }
        Ok(out)
    }

    async fn issue_phrase(
        &self,
        user_id: &UserId,
        channel: &str,
        channel_user_id: &str,
    ) -> Result<VerificationPhrase, IdentityError> {
        // Forward to the inherent method on `SqliteIdentityStore`.
        // The verification module owns the impl; the trait surface
        // just exposes it through the `Arc<dyn IdentityStore>` admin
        // routes hold.
        SqliteIdentityStore::issue_phrase(self, user_id, channel, channel_user_id).await
    }

    async fn merge_users(
        &self,
        into_user_id: &UserId,
        from_user_id: &UserId,
        decided_by: &str,
    ) -> Result<UserId, IdentityError> {
        if into_user_id == from_user_id {
            return Err(IdentityError::InvalidInput(
                "into_user_id and from_user_id must differ",
            ));
        }
        if decided_by.is_empty() {
            return Err(IdentityError::InvalidInput("decided_by must be non-empty"));
        }

        let mut tx = self
            .pool()
            .begin()
            .await
            .map_err(|e| IdentityError::Storage {
                op: "merge_users_begin",
                source: e,
            })?;

        // Both rows must exist before any reattribution. Otherwise an
        // operator typo silently mutates the surviving row's aliases
        // and leaves them dangling.
        let into_exists: i64 =
            sqlx::query_scalar("SELECT COUNT(*) FROM user_identities WHERE user_id = ?1")
                .bind(into_user_id.as_str())
                .fetch_one(&mut *tx)
                .await
                .map_err(|e| IdentityError::Storage {
                    op: "merge_users_check_into",
                    source: e,
                })?;
        if into_exists == 0 {
            return Err(IdentityError::UserNotFound(
                into_user_id.as_str().to_string(),
            ));
        }

        let from_exists: i64 =
            sqlx::query_scalar("SELECT COUNT(*) FROM user_identities WHERE user_id = ?1")
                .bind(from_user_id.as_str())
                .fetch_one(&mut *tx)
                .await
                .map_err(|e| IdentityError::Storage {
                    op: "merge_users_check_from",
                    source: e,
                })?;
        if from_exists == 0 {
            return Err(IdentityError::UserNotFound(
                from_user_id.as_str().to_string(),
            ));
        }

        // Reattribute every alias on the source to the target. Mark
        // them `operator`-bound so the admin UI can flag the manual
        // override distinctly from auto/verified bindings. Mirrors
        // `redeem_phrase`'s reattribution path; the only difference
        // is the `binding_kind` literal.
        sqlx::query(
            "UPDATE user_aliases \
             SET user_id = ?1, binding_kind = 'operator' \
             WHERE user_id = ?2",
        )
        .bind(into_user_id.as_str())
        .bind(from_user_id.as_str())
        .execute(&mut *tx)
        .await
        .map_err(|e| IdentityError::Storage {
            op: "merge_users_reattribute_aliases",
            source: e,
        })?;

        // Drop the orphaned source. ON DELETE CASCADE on user_aliases
        // would otherwise wipe the rows we just moved — we already
        // reattributed every alias above, so nothing cascades here.
        sqlx::query("DELETE FROM user_identities WHERE user_id = ?1")
            .bind(from_user_id.as_str())
            .execute(&mut *tx)
            .await
            .map_err(|e| IdentityError::Storage {
                op: "merge_users_delete_orphan",
                source: e,
            })?;

        // Touch the surviving row's updated_at so a downstream
        // "last-modified" sort surfaces the freshly-merged user.
        let now =
            OffsetDateTime::now_utc()
                .format(&Rfc3339)
                .map_err(|e| IdentityError::Storage {
                    op: "merge_users_format_ts",
                    source: sqlx::Error::ColumnDecode {
                        index: "now".into(),
                        source: Box::new(e),
                    },
                })?;
        sqlx::query("UPDATE user_identities SET updated_at = ?1 WHERE user_id = ?2")
            .bind(&now)
            .bind(into_user_id.as_str())
            .execute(&mut *tx)
            .await
            .map_err(|e| IdentityError::Storage {
                op: "merge_users_touch_into",
                source: e,
            })?;

        tx.commit().await.map_err(|e| IdentityError::Storage {
            op: "merge_users_commit",
            source: e,
        })?;

        // `decided_by` isn't yet persisted — the audit-log surface
        // (Phase 4 W2 follow-up) will pick it up via tracing. For now
        // we log it so an operator-driven merge always leaves a
        // breadcrumb in the gateway logs.
        tracing::info!(
            into_user_id = %into_user_id,
            from_user_id = %from_user_id,
            decided_by = %decided_by,
            "identity: operator-driven merge",
        );

        Ok(into_user_id.clone())
    }

    async fn aliases_for(&self, user_id: &UserId) -> Result<Vec<ChannelAlias>, IdentityError> {
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
                user_id: UserId::from(user_id_str),
                binding_kind: BindingKind::from_db_str(&binding_kind_str),
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
        let now =
            OffsetDateTime::now_utc()
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
        .bind(OffsetDateTime::now_utc().format(&Rfc3339).unwrap())
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
    async fn list_users_returns_descending_by_created_at_with_alias_counts() {
        let tmp = TempDir::new().unwrap();
        let store = fresh(&tmp).await;

        // Mint three users on different (channel, channel_user_id)
        // pairs. Each gets a single auto-bound alias to start.
        let u1 = store.resolve_or_create("qq", "1", None).await.unwrap();
        let u2 = store.resolve_or_create("qq", "2", None).await.unwrap();
        let u3 = store
            .resolve_or_create("qq", "3", Some("Charlie"))
            .await
            .unwrap();

        // Bond a second alias to u1 so its alias_count = 2.
        sqlx::query(
            "INSERT INTO user_aliases \
             (channel, channel_user_id, user_id, created_at, binding_kind) \
             VALUES ('telegram', '999', ?1, ?2, 'verified')",
        )
        .bind(u1.as_str())
        .bind(OffsetDateTime::now_utc().format(&Rfc3339).unwrap())
        .execute(store.pool())
        .await
        .unwrap();

        let users = store.list_users(10, 0).await.unwrap();
        assert_eq!(users.len(), 3);
        // ORDER BY created_at DESC → u3 first.
        assert_eq!(users[0].user_id, u3);
        assert_eq!(users[0].display_name.as_deref(), Some("Charlie"));
        assert_eq!(users[0].alias_count, 1);
        assert_eq!(users[1].user_id, u2);
        assert_eq!(users[1].alias_count, 1);
        // u1 has the bonded telegram alias.
        assert_eq!(users[2].user_id, u1);
        assert_eq!(users[2].alias_count, 2);
    }

    #[tokio::test]
    async fn list_users_paginates_via_limit_offset() {
        let tmp = TempDir::new().unwrap();
        let store = fresh(&tmp).await;
        for i in 0..5 {
            store
                .resolve_or_create("qq", &i.to_string(), None)
                .await
                .unwrap();
        }
        let page1 = store.list_users(2, 0).await.unwrap();
        let page2 = store.list_users(2, 2).await.unwrap();
        let page3 = store.list_users(2, 4).await.unwrap();
        assert_eq!(page1.len(), 2);
        assert_eq!(page2.len(), 2);
        assert_eq!(page3.len(), 1);
        // No overlap between pages.
        let p1_ids: Vec<_> = page1.iter().map(|u| &u.user_id).collect();
        for u in &page2 {
            assert!(!p1_ids.contains(&&u.user_id));
        }
    }

    #[tokio::test]
    async fn list_users_clamps_excessive_limit() {
        let tmp = TempDir::new().unwrap();
        let store = fresh(&tmp).await;
        for i in 0..5 {
            store
                .resolve_or_create("qq", &i.to_string(), None)
                .await
                .unwrap();
        }
        // limit = 0 (would otherwise return zero rows) clamps up to 1;
        // limit = u32::MAX clamps down to 200.
        let with_zero = store.list_users(0, 0).await.unwrap();
        assert_eq!(with_zero.len(), 1);
        let with_max = store.list_users(u32::MAX, 0).await.unwrap();
        assert_eq!(with_max.len(), 5, "5 < 200 → all returned");
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

    #[tokio::test]
    async fn merge_users_reattributes_aliases_and_deletes_source() {
        let tmp = TempDir::new().unwrap();
        let store = fresh(&tmp).await;

        let into = store.resolve_or_create("qq", "1234", None).await.unwrap();
        let from = store
            .resolve_or_create("telegram", "9876", None)
            .await
            .unwrap();
        assert_ne!(into, from);

        let surviving = store
            .merge_users(&into, &from, "operator-alice")
            .await
            .unwrap();
        assert_eq!(surviving, into);

        // Telegram alias now points to `into`.
        let tg_now = store.lookup("telegram", "9876").await.unwrap().unwrap();
        assert_eq!(tg_now, into);

        // The orphan is gone.
        let n: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM user_identities WHERE user_id = ?1")
            .bind(from.as_str())
            .fetch_one(store.pool())
            .await
            .unwrap();
        assert_eq!(n, 0);

        // The reattributed alias is marked `operator`.
        let bk: String = sqlx::query_scalar(
            "SELECT binding_kind FROM user_aliases \
             WHERE channel = 'telegram' AND channel_user_id = '9876'",
        )
        .fetch_one(store.pool())
        .await
        .unwrap();
        assert_eq!(bk, "operator");
    }

    #[tokio::test]
    async fn merge_users_rejects_self_merge() {
        let tmp = TempDir::new().unwrap();
        let store = fresh(&tmp).await;
        let uid = store.resolve_or_create("qq", "1234", None).await.unwrap();
        let err = store
            .merge_users(&uid, &uid, "operator-alice")
            .await
            .expect_err("self-merge must fail");
        assert!(matches!(err, IdentityError::InvalidInput(_)));
    }

    #[tokio::test]
    async fn merge_users_404s_when_into_missing() {
        let tmp = TempDir::new().unwrap();
        let store = fresh(&tmp).await;
        let from = store.resolve_or_create("qq", "1234", None).await.unwrap();
        let phantom = UserId::generate();
        let err = store
            .merge_users(&phantom, &from, "operator-alice")
            .await
            .expect_err("missing into must fail");
        assert!(matches!(err, IdentityError::UserNotFound(_)));
    }

    #[tokio::test]
    async fn merge_users_404s_when_from_missing() {
        let tmp = TempDir::new().unwrap();
        let store = fresh(&tmp).await;
        let into = store.resolve_or_create("qq", "1234", None).await.unwrap();
        let phantom = UserId::generate();
        let err = store
            .merge_users(&into, &phantom, "operator-alice")
            .await
            .expect_err("missing from must fail");
        assert!(matches!(err, IdentityError::UserNotFound(_)));
    }
}
