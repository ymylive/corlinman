//! Verification-phrase exchange protocol.
//!
//! Cross-channel unification by deliberate human action: an operator
//! issues a phrase from one channel, the human pastes it on the other,
//! the server unifies the two `user_id`s into one. The friction is the
//! point — automatic merging based on fuzzy signals (same display name,
//! same timezone) is a privacy hazard. The phrase makes the human prove
//! they own both channels.
//!
//! ## Protocol
//!
//! 1. Operator triggers `issue_phrase(user_id, channel, channel_user_id)`
//!    from the source channel. Server stores the phrase with
//!    `expires_at = now + DEFAULT_TTL_MIN` and returns it.
//! 2. The chat plugin echoes the phrase to the user on the source
//!    channel.
//! 3. User types the phrase on the target channel. The plugin sees the
//!    message and calls `redeem_phrase(phrase, target_channel,
//!    target_channel_user_id)`.
//! 4. Server reattributes the target alias's `user_id` to the source
//!    `user_id`, deletes the orphaned target user (cascade clears any
//!    other aliases bound to it), and marks the phrase consumed.
//!
//! ## Phrase format
//!
//! Three Crockford-base32 syllables joined by hyphens, e.g.
//! `K8M-3PX-Q2R`. ~32^9 ≈ 3.5 × 10^13 combinations; with a 30-min TTL
//! and ~1k phrases/day max, collision risk is negligible. Easier to
//! type on a phone than dictionary words while staying memorable for
//! the ~30-second window between issue and redeem.

use rand::{rngs::OsRng, RngCore};
use sqlx::Row;
use time::format_description::well_known::Rfc3339;
use time::OffsetDateTime;

use crate::error::IdentityError;
use crate::store::SqliteIdentityStore;
use crate::types::{UserId, VerificationPhrase};

/// Phrase TTL in minutes. 30 minutes balances "long enough for a human
/// to switch apps and paste" against "short enough that a leaked
/// phrase isn't a long-lived risk".
pub const DEFAULT_TTL_MIN: i64 = 30;

/// Crockford-base32 alphabet (excludes I, L, O, U for readability).
const PHRASE_ALPHABET: &[u8; 32] = b"0123456789ABCDEFGHJKMNPQRSTVWXYZ";

/// Generate a fresh phrase. 9 chars total (3 × 3-char syllables) split
/// by hyphens — matches the format described at module level.
fn generate_phrase() -> String {
    let mut rng = OsRng;
    let mut bytes = [0u8; 9];
    rng.fill_bytes(&mut bytes);
    let mut out = String::with_capacity(11);
    for (i, b) in bytes.iter().enumerate() {
        out.push(PHRASE_ALPHABET[(*b as usize) & 0x1f] as char);
        if i % 3 == 2 && i != 8 {
            out.push('-');
        }
    }
    out
}

impl SqliteIdentityStore {
    /// Issue a fresh verification phrase for `user_id` on
    /// `(channel, channel_user_id)`. The phrase expires in
    /// [`DEFAULT_TTL_MIN`] minutes. Returns the persisted record so
    /// the caller can echo the phrase back over the chat channel.
    ///
    /// The caller (admin route) is responsible for asserting that
    /// `(channel, channel_user_id)` actually maps to `user_id` — this
    /// method just stores the row.
    pub async fn issue_phrase(
        &self,
        user_id: &UserId,
        channel: &str,
        channel_user_id: &str,
    ) -> Result<VerificationPhrase, IdentityError> {
        if channel.is_empty() || channel_user_id.is_empty() {
            return Err(IdentityError::InvalidInput(
                "channel and channel_user_id must be non-empty",
            ));
        }
        let phrase = generate_phrase();
        let now = OffsetDateTime::now_utc();
        let expires_at = now + time::Duration::minutes(DEFAULT_TTL_MIN);
        let expires_str = expires_at.format(&Rfc3339).map_err(format_err)?;

        sqlx::query(
            "INSERT INTO verification_phrases \
             (phrase, issued_to_user_id, issued_on_channel, \
              issued_on_channel_user_id, expires_at) \
             VALUES (?1, ?2, ?3, ?4, ?5)",
        )
        .bind(&phrase)
        .bind(user_id.as_str())
        .bind(channel)
        .bind(channel_user_id)
        .bind(&expires_str)
        .execute(self.pool())
        .await
        .map_err(|e| IdentityError::Storage {
            op: "issue_phrase",
            source: e,
        })?;

        Ok(VerificationPhrase {
            phrase,
            user_id: user_id.clone(),
            issued_on_channel: channel.to_string(),
            issued_on_channel_user_id: channel_user_id.to_string(),
            expires_at,
        })
    }

    /// Redeem a phrase issued on a different channel. Reattributes
    /// `(redeemed_on_channel, redeemed_on_channel_user_id)` to the
    /// phrase's issuing `user_id`, deletes the orphaned redeemer
    /// `user_id` row (cascade clears its other aliases), and marks
    /// the phrase consumed.
    ///
    /// Errors:
    /// - [`IdentityError::PhraseUnknown`] — phrase doesn't match any row.
    /// - [`IdentityError::PhraseExpired`] — past `expires_at`.
    /// - [`IdentityError::PhraseAlreadyConsumed`] — `consumed_at IS NOT NULL`.
    ///
    /// Returns the surviving (issuer's) `UserId` so the caller can
    /// echo "identity unified — your QQ traits now apply on Telegram".
    pub async fn redeem_phrase(
        &self,
        phrase: &str,
        redeemed_on_channel: &str,
        redeemed_on_channel_user_id: &str,
    ) -> Result<UserId, IdentityError> {
        if phrase.is_empty() {
            return Err(IdentityError::InvalidInput("phrase must be non-empty"));
        }
        if redeemed_on_channel.is_empty() || redeemed_on_channel_user_id.is_empty() {
            return Err(IdentityError::InvalidInput(
                "redeemed_on_channel and redeemed_on_channel_user_id must be non-empty",
            ));
        }

        let mut tx = self
            .pool()
            .begin()
            .await
            .map_err(|e| IdentityError::Storage {
                op: "begin_redeem",
                source: e,
            })?;

        let row = sqlx::query(
            "SELECT issued_to_user_id, expires_at, consumed_at \
             FROM verification_phrases WHERE phrase = ?1",
        )
        .bind(phrase)
        .fetch_optional(&mut *tx)
        .await
        .map_err(|e| IdentityError::Storage {
            op: "redeem_lookup",
            source: e,
        })?
        .ok_or(IdentityError::PhraseUnknown)?;

        let issued_to_user_id: String = row.get("issued_to_user_id");
        let expires_at_str: String = row.get("expires_at");
        let consumed_at: Option<String> = row.get("consumed_at");

        if consumed_at.is_some() {
            return Err(IdentityError::PhraseAlreadyConsumed);
        }
        let expires_at = OffsetDateTime::parse(&expires_at_str, &Rfc3339).map_err(parse_err)?;
        if OffsetDateTime::now_utc() >= expires_at {
            return Err(IdentityError::PhraseExpired);
        }

        let now_str = OffsetDateTime::now_utc()
            .format(&Rfc3339)
            .map_err(format_err)?;

        // Find the redeemer's current user_id (created lazily during
        // their first message on the target channel). It may not
        // exist yet — if so, we just bind a fresh alias to the issuer.
        let redeemer_user_id: Option<String> = sqlx::query_scalar(
            "SELECT user_id FROM user_aliases \
             WHERE channel = ?1 AND channel_user_id = ?2",
        )
        .bind(redeemed_on_channel)
        .bind(redeemed_on_channel_user_id)
        .fetch_optional(&mut *tx)
        .await
        .map_err(|e| IdentityError::Storage {
            op: "redeem_find_redeemer",
            source: e,
        })?;

        match redeemer_user_id {
            Some(redeemer_id) if redeemer_id == issued_to_user_id => {
                // Already unified — nothing to merge. Mark phrase
                // consumed and return.
            }
            Some(redeemer_id) => {
                // Reattribute every alias on the redeemer to the
                // issuer's user_id. ON DELETE CASCADE on the
                // user_identities row would also drop these, but
                // doing it explicitly preserves the alias rows
                // (with new `user_id` + `binding_kind = 'verified'`).
                sqlx::query(
                    "UPDATE user_aliases \
                     SET user_id = ?1, binding_kind = 'verified' \
                     WHERE user_id = ?2",
                )
                .bind(&issued_to_user_id)
                .bind(&redeemer_id)
                .execute(&mut *tx)
                .await
                .map_err(|e| IdentityError::Storage {
                    op: "redeem_reattribute_aliases",
                    source: e,
                })?;

                // Delete the orphaned user_identities row. Its
                // aliases are already reattributed so the cascade
                // has nothing to clear; the row deletion itself is
                // the clean-up.
                sqlx::query("DELETE FROM user_identities WHERE user_id = ?1")
                    .bind(&redeemer_id)
                    .execute(&mut *tx)
                    .await
                    .map_err(|e| IdentityError::Storage {
                        op: "redeem_delete_orphan_user",
                        source: e,
                    })?;
            }
            None => {
                // Redeemer hasn't ever chatted on the target channel —
                // bind a fresh `verified` alias straight to the
                // issuer. The chat plugin called us before any
                // resolve_or_create did.
                sqlx::query(
                    "INSERT INTO user_aliases \
                     (channel, channel_user_id, user_id, created_at, binding_kind) \
                     VALUES (?1, ?2, ?3, ?4, 'verified')",
                )
                .bind(redeemed_on_channel)
                .bind(redeemed_on_channel_user_id)
                .bind(&issued_to_user_id)
                .bind(&now_str)
                .execute(&mut *tx)
                .await
                .map_err(|e| IdentityError::Storage {
                    op: "redeem_bind_fresh_alias",
                    source: e,
                })?;
            }
        }

        // Mark the phrase consumed. The `WHERE consumed_at IS NULL`
        // guard makes a duplicate-redeem race deterministic — only
        // one transaction's UPDATE matches; the other's UPDATE
        // affects 0 rows and we'd notice. Since we already checked
        // above, this is a belt-and-suspenders.
        let consumed = sqlx::query(
            "UPDATE verification_phrases \
             SET consumed_at = ?1, consumed_on_channel = ?2, \
                 consumed_on_channel_user_id = ?3 \
             WHERE phrase = ?4 AND consumed_at IS NULL",
        )
        .bind(&now_str)
        .bind(redeemed_on_channel)
        .bind(redeemed_on_channel_user_id)
        .bind(phrase)
        .execute(&mut *tx)
        .await
        .map_err(|e| IdentityError::Storage {
            op: "redeem_mark_consumed",
            source: e,
        })?;
        if consumed.rows_affected() == 0 {
            // Concurrent redeemer beat us. Roll back and surface the
            // already-consumed error so the caller can show the
            // appropriate UX.
            return Err(IdentityError::PhraseAlreadyConsumed);
        }

        tx.commit().await.map_err(|e| IdentityError::Storage {
            op: "redeem_commit",
            source: e,
        })?;

        Ok(UserId::from(issued_to_user_id))
    }

    /// Garbage-collect expired, unconsumed phrases. Returns the number
    /// of rows removed. Operators can wire this into a cron via
    /// `corlinman-scheduler` once that surface is on this branch;
    /// the crate ships the helper so consumers don't need to know
    /// the schema.
    pub async fn sweep_expired_phrases(&self) -> Result<u64, IdentityError> {
        let now_str = OffsetDateTime::now_utc()
            .format(&Rfc3339)
            .map_err(format_err)?;
        let res = sqlx::query(
            "DELETE FROM verification_phrases \
             WHERE consumed_at IS NULL AND expires_at < ?1",
        )
        .bind(&now_str)
        .execute(self.pool())
        .await
        .map_err(|e| IdentityError::Storage {
            op: "sweep_expired_phrases",
            source: e,
        })?;
        Ok(res.rows_affected())
    }
}

fn parse_err(e: time::error::Parse) -> IdentityError {
    IdentityError::Storage {
        op: "parse_ts",
        source: sqlx::Error::ColumnDecode {
            index: "expires_at".into(),
            source: Box::new(e),
        },
    }
}

fn format_err(e: time::error::Format) -> IdentityError {
    IdentityError::Storage {
        op: "format_ts",
        source: sqlx::Error::ColumnDecode {
            index: "now".into(),
            source: Box::new(e),
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::resolver::IdentityStore;
    use crate::store::{identity_db_path, SqliteIdentityStore};
    use corlinman_tenant::TenantId;
    use std::collections::HashSet;
    use tempfile::TempDir;

    async fn fresh(tmp: &TempDir) -> SqliteIdentityStore {
        let tenant = TenantId::legacy_default();
        let path = identity_db_path(tmp.path(), &tenant);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        SqliteIdentityStore::open_with_pool_size(&path, 1)
            .await
            .unwrap()
    }

    #[test]
    fn generate_phrase_format_is_3x3_with_hyphens() {
        let p = generate_phrase();
        assert_eq!(p.len(), 11, "9 chars + 2 hyphens");
        let parts: Vec<&str> = p.split('-').collect();
        assert_eq!(parts.len(), 3);
        for part in parts {
            assert_eq!(part.len(), 3);
            for c in part.chars() {
                assert!(
                    c.is_ascii_uppercase() || c.is_ascii_digit(),
                    "phrase chars must be Crockford base32"
                );
                // I, L, O, U deliberately excluded for readability.
                assert!(!"ILOU".contains(c), "ambiguous char {c} must not appear");
            }
        }
    }

    #[test]
    fn generate_phrase_is_unique_across_many_calls() {
        let mut seen = HashSet::new();
        for _ in 0..1024 {
            assert!(seen.insert(generate_phrase()));
        }
    }

    #[tokio::test]
    async fn issue_phrase_persists_row_and_returns_record() {
        let tmp = TempDir::new().unwrap();
        let store = fresh(&tmp).await;
        let uid = store.resolve_or_create("qq", "1234", None).await.unwrap();

        let p = store.issue_phrase(&uid, "qq", "1234").await.unwrap();
        assert_eq!(p.user_id, uid);
        assert_eq!(p.issued_on_channel, "qq");
        assert!(p.expires_at > OffsetDateTime::now_utc());

        // Round-trip via SQL: phrase is in the DB.
        let n: i64 =
            sqlx::query_scalar("SELECT COUNT(*) FROM verification_phrases WHERE phrase = ?1")
                .bind(&p.phrase)
                .fetch_one(store.pool())
                .await
                .unwrap();
        assert_eq!(n, 1);
    }

    #[tokio::test]
    async fn redeem_phrase_unifies_two_existing_users() {
        let tmp = TempDir::new().unwrap();
        let store = fresh(&tmp).await;

        // Two distinct humans, one on each channel.
        let qq_uid = store.resolve_or_create("qq", "1234", None).await.unwrap();
        let tg_uid = store
            .resolve_or_create("telegram", "9876", None)
            .await
            .unwrap();
        assert_ne!(qq_uid, tg_uid);

        // Operator issues phrase from QQ side.
        let p = store.issue_phrase(&qq_uid, "qq", "1234").await.unwrap();

        // Human pastes on Telegram.
        let surviving = store
            .redeem_phrase(&p.phrase, "telegram", "9876")
            .await
            .unwrap();
        assert_eq!(surviving, qq_uid, "issuer's user_id wins");

        // Telegram alias now resolves to qq_uid.
        let tg_now = store.lookup("telegram", "9876").await.unwrap().unwrap();
        assert_eq!(tg_now, qq_uid);

        // The orphaned tg_uid is gone.
        let n: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM user_identities WHERE user_id = ?1")
            .bind(tg_uid.as_str())
            .fetch_one(store.pool())
            .await
            .unwrap();
        assert_eq!(n, 0);

        // The reattributed alias has binding_kind=verified.
        let bk: String = sqlx::query_scalar(
            "SELECT binding_kind FROM user_aliases \
             WHERE channel = 'telegram' AND channel_user_id = '9876'",
        )
        .fetch_one(store.pool())
        .await
        .unwrap();
        assert_eq!(bk, "verified");
    }

    #[tokio::test]
    async fn redeem_phrase_binds_fresh_alias_when_redeemer_has_none() {
        let tmp = TempDir::new().unwrap();
        let store = fresh(&tmp).await;
        let qq_uid = store.resolve_or_create("qq", "1234", None).await.unwrap();
        let p = store.issue_phrase(&qq_uid, "qq", "1234").await.unwrap();

        // Telegram alias didn't pre-exist — redeemer's first action
        // on the target channel IS pasting the phrase.
        let surviving = store
            .redeem_phrase(&p.phrase, "telegram", "9876")
            .await
            .unwrap();
        assert_eq!(surviving, qq_uid);

        let tg_now = store.lookup("telegram", "9876").await.unwrap().unwrap();
        assert_eq!(tg_now, qq_uid);
    }

    #[tokio::test]
    async fn redeem_phrase_unknown_errors() {
        let tmp = TempDir::new().unwrap();
        let store = fresh(&tmp).await;
        let err = store
            .redeem_phrase("XXX-XXX-XXX", "qq", "1234")
            .await
            .expect_err("unknown phrase must fail");
        assert!(matches!(err, IdentityError::PhraseUnknown));
    }

    #[tokio::test]
    async fn redeem_phrase_already_consumed_errors() {
        let tmp = TempDir::new().unwrap();
        let store = fresh(&tmp).await;
        let qq_uid = store.resolve_or_create("qq", "1234", None).await.unwrap();
        let p = store.issue_phrase(&qq_uid, "qq", "1234").await.unwrap();

        store
            .redeem_phrase(&p.phrase, "telegram", "9876")
            .await
            .unwrap();
        let err = store
            .redeem_phrase(&p.phrase, "telegram", "9876")
            .await
            .expect_err("second redeem must fail");
        assert!(matches!(err, IdentityError::PhraseAlreadyConsumed));
    }

    #[tokio::test]
    async fn redeem_phrase_expired_errors() {
        let tmp = TempDir::new().unwrap();
        let store = fresh(&tmp).await;
        let qq_uid = store.resolve_or_create("qq", "1234", None).await.unwrap();

        // Issue and then forcibly age the row past its expiry by
        // direct SQL — short-circuits the 30-min wait.
        let p = store.issue_phrase(&qq_uid, "qq", "1234").await.unwrap();
        let past = OffsetDateTime::now_utc() - time::Duration::minutes(1);
        sqlx::query("UPDATE verification_phrases SET expires_at = ?1 WHERE phrase = ?2")
            .bind(past.format(&Rfc3339).unwrap())
            .bind(&p.phrase)
            .execute(store.pool())
            .await
            .unwrap();

        let err = store
            .redeem_phrase(&p.phrase, "telegram", "9876")
            .await
            .expect_err("expired phrase must fail");
        assert!(matches!(err, IdentityError::PhraseExpired));
    }

    #[tokio::test]
    async fn sweep_expired_removes_only_unconsumed_past_phrases() {
        let tmp = TempDir::new().unwrap();
        let store = fresh(&tmp).await;
        let uid = store.resolve_or_create("qq", "1234", None).await.unwrap();

        // Three phrases: live, expired-unconsumed, expired-consumed.
        let live = store.issue_phrase(&uid, "qq", "1234").await.unwrap();
        let expired_unconsumed = store.issue_phrase(&uid, "qq", "1234").await.unwrap();
        let expired_consumed = store.issue_phrase(&uid, "qq", "1234").await.unwrap();
        let past = (OffsetDateTime::now_utc() - time::Duration::minutes(1))
            .format(&Rfc3339)
            .unwrap();
        sqlx::query("UPDATE verification_phrases SET expires_at = ?1 WHERE phrase = ?2")
            .bind(&past)
            .bind(&expired_unconsumed.phrase)
            .execute(store.pool())
            .await
            .unwrap();
        sqlx::query(
            "UPDATE verification_phrases SET expires_at = ?1, consumed_at = ?1 WHERE phrase = ?2",
        )
        .bind(&past)
        .bind(&expired_consumed.phrase)
        .execute(store.pool())
        .await
        .unwrap();

        let removed = store.sweep_expired_phrases().await.unwrap();
        assert_eq!(removed, 1, "only expired-unconsumed should be removed");

        // Live + expired-consumed survive.
        let survivors: i64 = sqlx::query_scalar(
            "SELECT COUNT(*) FROM verification_phrases WHERE phrase IN (?1, ?2)",
        )
        .bind(&live.phrase)
        .bind(&expired_consumed.phrase)
        .fetch_one(store.pool())
        .await
        .unwrap();
        assert_eq!(survivors, 2);
    }
}
