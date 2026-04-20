//! Per-key token-bucket rate limiter used by [`crate::router::ChannelRouter`].
//!
//! The router consults one bucket per rate-limit dimension (per-group,
//! per-sender). The key is a stable string derived from the channel binding
//! (`"qq:group:<gid>"`, `"qq:sender:<gid>:<uid>"`) so collisions across
//! channels / threads are impossible.
//!
//! Algorithm: classic token bucket with linear refill.
//! - `capacity = per_min` (so a freshly-seen key can burst up to `per_min`
//!   turns instantly).
//! - `refill_per_sec = per_min / 60` (linear, not jittered).
//! - `check`: refill based on wall clock, try to consume 1 token, return
//!   `true` iff the bucket had ≥ 1 token.
//!
//! This is a per-process limiter. See the module-level TODOs for the Redis
//! variant we want once a second gateway replica ships.
//!
//! GC: the internal [`DashMap`] grows unboundedly if we never prune. The
//! [`TokenBucket::start_gc`] task periodically sweeps entries whose
//! `last_refill` is more than an hour old — idle groups / senders drop out
//! and re-appear on the next message at full capacity (semantically fine: a
//! group that hasn't spoken in an hour is not mid-burst anyway).

use std::sync::Arc;
use std::time::{Duration, Instant};

use dashmap::DashMap;
use tokio::task::JoinHandle;
use tokio_util::sync::CancellationToken;

/// Entries older than this are pruned by the background GC sweeper.
const GC_STALE_AFTER: Duration = Duration::from_secs(3600);

/// How often the background sweeper walks the map.
const GC_INTERVAL: Duration = Duration::from_secs(300);

/// A thread-safe, per-key token bucket.
pub struct TokenBucket {
    /// Max tokens per key (equals `per_min`).
    capacity: f64,
    /// Tokens replenished per second (equals `per_min / 60.0`).
    refill_per_sec: f64,
    state: DashMap<String, BucketState>,
}

struct BucketState {
    tokens: f64,
    last_refill: Instant,
}

impl TokenBucket {
    /// Build a bucket that allows `per_min` events per minute per key.
    pub fn per_minute(per_min: u32) -> Self {
        let capacity = per_min as f64;
        let refill_per_sec = capacity / 60.0;
        Self {
            capacity,
            refill_per_sec,
            state: DashMap::new(),
        }
    }

    /// Try to consume 1 token from `key`'s bucket. Returns `true` if the
    /// caller is allowed to proceed. A brand-new key starts at full capacity.
    pub fn check(&self, key: &str) -> bool {
        let now = Instant::now();
        let mut entry = self.state.entry(key.to_string()).or_insert(BucketState {
            tokens: self.capacity,
            last_refill: now,
        });
        let elapsed = now.duration_since(entry.last_refill).as_secs_f64();
        entry.tokens = (entry.tokens + elapsed * self.refill_per_sec).min(self.capacity);
        entry.last_refill = now;
        if entry.tokens >= 1.0 {
            entry.tokens -= 1.0;
            true
        } else {
            false
        }
    }

    /// Number of live keys currently tracked — useful for tests and future
    /// metrics.
    pub fn tracked_keys(&self) -> usize {
        self.state.len()
    }

    /// Spawn a background task that periodically sweeps stale keys
    /// (`last_refill > GC_STALE_AFTER` ago). Returns the join handle so
    /// callers can abort on shutdown. Cancellation via `cancel` stops the
    /// loop cleanly.
    pub fn start_gc(self: Arc<Self>, cancel: CancellationToken) -> JoinHandle<()> {
        tokio::spawn(async move {
            let mut ticker = tokio::time::interval(GC_INTERVAL);
            // Skip the immediate tick — nothing to sweep yet.
            ticker.tick().await;
            loop {
                tokio::select! {
                    biased;
                    _ = cancel.cancelled() => break,
                    _ = ticker.tick() => {
                        self.sweep_stale();
                    }
                }
            }
        })
    }

    /// Remove entries whose `last_refill` is older than [`GC_STALE_AFTER`].
    /// Exposed for tests; the background sweeper calls this on each tick.
    pub fn sweep_stale(&self) {
        let cutoff = Instant::now() - GC_STALE_AFTER;
        self.state.retain(|_, v| v.last_refill >= cutoff);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bucket_allows_within_capacity() {
        let b = TokenBucket::per_minute(20);
        for _ in 0..20 {
            assert!(b.check("g:1"));
        }
    }

    #[test]
    fn bucket_denies_when_empty() {
        let b = TokenBucket::per_minute(20);
        for _ in 0..20 {
            assert!(b.check("g:1"));
        }
        // 21st immediately after exhausting → refill ≪ 1 token → deny.
        assert!(!b.check("g:1"));
    }

    #[test]
    fn bucket_refills_over_time() {
        // 60/min = 1 token per second. Exhaust then wait > 1s for a refill.
        let b = TokenBucket::per_minute(60);
        for _ in 0..60 {
            assert!(b.check("g:1"));
        }
        assert!(!b.check("g:1"));
        std::thread::sleep(Duration::from_millis(1100));
        assert!(b.check("g:1"), "expected refill after 1.1s at 1 token/sec");
    }

    #[test]
    fn bucket_isolates_keys() {
        let b = TokenBucket::per_minute(3);
        for _ in 0..3 {
            assert!(b.check("a"));
        }
        assert!(!b.check("a"));
        // Key "b" has its own bucket, unaffected.
        for _ in 0..3 {
            assert!(b.check("b"));
        }
        assert!(!b.check("b"));
    }

    #[test]
    fn bucket_capacity_caps_refill() {
        // A bucket idle for a long time should not accumulate beyond capacity.
        let b = TokenBucket::per_minute(5);
        // Force-seed an idle key by checking once to create state, then
        // rewind last_refill far enough to accumulate >> capacity worth.
        assert!(b.check("k"));
        {
            let mut s = b.state.get_mut("k").unwrap();
            s.last_refill = Instant::now() - Duration::from_secs(3600);
            s.tokens = 0.0;
        }
        // One refill computation on next check → should cap at capacity,
        // not at refill_per_sec * elapsed = 300.
        assert!(b.check("k"));
        // After consuming 1, the bucket should have exactly capacity-1.
        let remaining = b.state.get("k").unwrap().tokens;
        assert!(
            (remaining - (b.capacity - 1.0)).abs() < 1e-6,
            "expected refill capped at capacity, got {remaining}"
        );
    }

    #[test]
    fn sweep_drops_stale_keys() {
        let b = TokenBucket::per_minute(5);
        assert!(b.check("idle"));
        assert_eq!(b.tracked_keys(), 1);
        // Rewind last_refill past the stale cutoff.
        {
            let mut s = b.state.get_mut("idle").unwrap();
            s.last_refill = Instant::now() - GC_STALE_AFTER - Duration::from_secs(1);
        }
        b.sweep_stale();
        assert_eq!(b.tracked_keys(), 0);
    }
}
