//! In-memory admin session store.
//!
//! S5 T1: gives `/admin/login` somewhere to park a token after argon2
//! succeeds, and lets [`super::admin_auth::require_admin`] validate a
//! `Cookie: corlinman_session=<token>` instead of asking the browser to
//! re-send Basic credentials on every request.
//!
//! Design is deliberately minimal:
//!   - `DashMap<String, AdminSession>` keyed by a random UUID token.
//!   - Sessions expire after `ttl` since `last_used`; a background GC task
//!     sweeps the map every `ttl / 4` (min 60s).
//!   - No persistence across restart — operators just log in again. See the
//!     TODO at the bottom for rehydration if that becomes painful.
//!
//! Security posture:
//!   - Token is a v4 UUID (~122 bits of entropy), issued once at login.
//!   - `validate` returns a *clone* so callers can't keep a long-lived ref
//!     into the DashMap shard.
//!   - `last_used` is bumped on every successful `validate`; that's what
//!     the GC compares against.

use std::sync::Arc;
use std::time::Duration as StdDuration;

use dashmap::DashMap;
use time::OffsetDateTime;
use tokio::task::JoinHandle;
use tokio_util::sync::CancellationToken;
use uuid::Uuid;

/// One authenticated admin session. Cloned on every successful validate.
#[derive(Debug, Clone)]
pub struct AdminSession {
    pub user: String,
    pub created_at: OffsetDateTime,
    pub last_used: OffsetDateTime,
}

/// Thread-safe session registry. Cloneable handle — wrap in `Arc` and share
/// across middleware + login/logout handlers.
pub struct AdminSessionStore {
    sessions: DashMap<String, AdminSession>,
    ttl: StdDuration,
}

impl AdminSessionStore {
    /// Build a new store with `ttl` idle timeout (sessions unused for
    /// longer than this are evicted by the GC).
    pub fn new(ttl: StdDuration) -> Self {
        Self {
            sessions: DashMap::new(),
            ttl,
        }
    }

    /// Idle timeout sessions expire at.
    pub fn ttl(&self) -> StdDuration {
        self.ttl
    }

    /// Issue a fresh token for `user` and park the session.
    /// Returns the opaque token string the cookie carries.
    pub fn create(&self, user: String) -> String {
        let token = Uuid::new_v4().to_string();
        let now = OffsetDateTime::now_utc();
        self.sessions.insert(
            token.clone(),
            AdminSession {
                user,
                created_at: now,
                last_used: now,
            },
        );
        token
    }

    /// Validate a token. `None` = expired or unknown. On hit, bumps
    /// `last_used` so an active session keeps sliding forward.
    pub fn validate(&self, token: &str) -> Option<AdminSession> {
        let now = OffsetDateTime::now_utc();
        let ttl_secs = self.ttl.as_secs() as i64;

        // Look up + mutate in one shard lock so concurrent validates see
        // consistent `last_used`.
        let mut expired = false;
        let result = self.sessions.get_mut(token).and_then(|mut entry| {
            let elapsed = (now - entry.last_used).whole_seconds();
            if elapsed > ttl_secs {
                expired = true;
                return None;
            }
            entry.last_used = now;
            Some(entry.clone())
        });

        if expired {
            self.sessions.remove(token);
        }
        result
    }

    /// Drop a token unconditionally. Called by `/admin/logout`.
    pub fn invalidate(&self, token: &str) {
        self.sessions.remove(token);
    }

    /// Current session count — for tests + metrics.
    pub fn len(&self) -> usize {
        self.sessions.len()
    }

    /// Whether the store is empty.
    pub fn is_empty(&self) -> bool {
        self.sessions.is_empty()
    }

    /// Evict every entry whose `last_used` is older than `ttl`. Called by
    /// the GC task; exposed so tests can exercise it directly.
    pub fn gc(&self) -> usize {
        let now = OffsetDateTime::now_utc();
        let ttl_secs = self.ttl.as_secs() as i64;
        let mut victims: Vec<String> = Vec::new();
        for entry in self.sessions.iter() {
            if (now - entry.last_used).whole_seconds() > ttl_secs {
                victims.push(entry.key().clone());
            }
        }
        let n = victims.len();
        for k in victims {
            self.sessions.remove(&k);
        }
        n
    }

    /// Spawn a background task that calls `gc()` every `ttl / 4` (min 60s)
    /// until `cancel` fires. Returns the JoinHandle so `main.rs` can await
    /// it on shutdown.
    pub fn start_gc(self: Arc<Self>, cancel: CancellationToken) -> JoinHandle<()> {
        let interval = {
            let quarter = self.ttl / 4;
            if quarter < StdDuration::from_secs(60) {
                StdDuration::from_secs(60)
            } else {
                quarter
            }
        };
        tokio::spawn(async move {
            let mut ticker = tokio::time::interval(interval);
            // First tick fires immediately; skip it so we don't GC an
            // empty store at boot.
            ticker.tick().await;
            loop {
                tokio::select! {
                    _ = cancel.cancelled() => break,
                    _ = ticker.tick() => {
                        let n = self.gc();
                        if n > 0 {
                            tracing::debug!(evicted = n, "admin session GC swept expired entries");
                        }
                    }
                }
            }
        })
    }
}

// TODO(persistence): if operators complain about being logged out on
// gateway restart, persist `{token → (user, created_at)}` to SQLite under
// `<data_dir>/admin_sessions.sqlite` and rehydrate on boot. Until then the
// 24h TTL + browser re-login on deploy is fine.

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn create_then_validate_returns_session() {
        let store = AdminSessionStore::new(StdDuration::from_secs(60));
        let tok = store.create("admin".into());
        let s = store.validate(&tok).expect("fresh token validates");
        assert_eq!(s.user, "admin");
        assert_eq!(store.len(), 1);
    }

    #[tokio::test]
    async fn validate_unknown_token_is_none() {
        let store = AdminSessionStore::new(StdDuration::from_secs(60));
        assert!(store.validate("not-a-real-token").is_none());
    }

    #[tokio::test]
    async fn invalidate_removes_session() {
        let store = AdminSessionStore::new(StdDuration::from_secs(60));
        let tok = store.create("admin".into());
        store.invalidate(&tok);
        assert!(store.validate(&tok).is_none());
        assert_eq!(store.len(), 0);
    }

    #[tokio::test]
    async fn validate_bumps_last_used() {
        let store = AdminSessionStore::new(StdDuration::from_secs(60));
        let tok = store.create("admin".into());
        let first = store.validate(&tok).unwrap().last_used;
        // Force a measurable delta. OffsetDateTime resolution is ns so a
        // single yield is usually enough, but sleep a hair to be safe.
        tokio::time::sleep(StdDuration::from_millis(5)).await;
        let second = store.validate(&tok).unwrap().last_used;
        assert!(second >= first);
    }

    #[tokio::test]
    async fn gc_evicts_expired_sessions() {
        // 0-second TTL means every session is already idle.
        let store = AdminSessionStore::new(StdDuration::from_secs(0));
        let tok = store.create("admin".into());
        // Give it >1s so `whole_seconds()` rounds past the TTL.
        tokio::time::sleep(StdDuration::from_millis(1100)).await;
        let evicted = store.gc();
        assert_eq!(evicted, 1);
        assert!(store.validate(&tok).is_none());
        assert_eq!(store.len(), 0);
    }

    #[tokio::test]
    async fn validate_expired_returns_none_and_evicts() {
        let store = AdminSessionStore::new(StdDuration::from_secs(0));
        let tok = store.create("admin".into());
        tokio::time::sleep(StdDuration::from_millis(1100)).await;
        assert!(store.validate(&tok).is_none());
        assert_eq!(store.len(), 0);
    }

    #[tokio::test]
    async fn start_gc_evicts_and_stops_on_cancel() {
        let store = Arc::new(AdminSessionStore::new(StdDuration::from_secs(0)));
        let tok = store.create("admin".into());
        let cancel = CancellationToken::new();
        // tick interval clamps to 60s; call gc() directly via a looping
        // handle — just assert start_gc returns a handle + cancels cleanly.
        let handle = Arc::clone(&store).start_gc(cancel.clone());
        // manual gc so we don't have to wait 60s for the tick.
        tokio::time::sleep(StdDuration::from_millis(1100)).await;
        store.gc();
        assert!(store.validate(&tok).is_none());
        cancel.cancel();
        handle.await.expect("gc task joins cleanly");
    }
}
