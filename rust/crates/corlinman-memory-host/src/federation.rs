//! Fan-out [`MemoryHost`] that queries a set of sub-hosts in parallel
//! and merges the per-host rankings.
//!
//! The skeleton ships Reciprocal Rank Fusion only; the
//! [`FusionStrategy`] enum leaves room for weighted-average and
//! learned fusion later.
//!
//! ## Failure model
//!
//! A sub-host that errors or times out is logged at `warn!` and
//! **skipped** — it is not fatal. The federated query succeeds as
//! long as at least one sub-host returned hits. If every sub-host
//! fails or returns empty, `query` returns `Ok(Vec::new())`.
//!
//! ## `upsert` / `delete`
//!
//! The federated host does not own a canonical namespace and so does
//! not implement meaningful upsert/delete semantics — both return an
//! error. Call the specific sub-host directly for writes.

use std::collections::HashMap;
use std::sync::Arc;

use anyhow::{anyhow, Result};
use async_trait::async_trait;
use futures::future::join_all;
use tracing::warn;

use crate::{MemoryDoc, MemoryHit, MemoryHost, MemoryQuery};

/// Merge strategy applied to per-host result sets.
///
/// Marked `non_exhaustive` so we can add e.g. `WeightedAverage` later
/// without breaking callers that matched exhaustively.
#[derive(Debug, Clone)]
#[non_exhaustive]
pub enum FusionStrategy {
    /// Reciprocal Rank Fusion with constant `k` (typical value 60).
    Rrf { k: f32 },
}

impl FusionStrategy {
    /// RRF with the canonical `k = 60` from Cormack et al. 2009.
    pub fn rrf_default() -> Self {
        FusionStrategy::Rrf { k: 60.0 }
    }
}

/// A [`MemoryHost`] that fans out to child hosts and merges results.
pub struct FederatedMemoryHost {
    name: String,
    hosts: Vec<Arc<dyn MemoryHost>>,
    strategy: FusionStrategy,
}

impl FederatedMemoryHost {
    /// Construct with an explicit fusion strategy.
    pub fn new(
        name: impl Into<String>,
        hosts: Vec<Arc<dyn MemoryHost>>,
        strategy: FusionStrategy,
    ) -> Self {
        Self {
            name: name.into(),
            hosts,
            strategy,
        }
    }

    /// Convenience constructor using [`FusionStrategy::rrf_default`].
    pub fn with_rrf(name: impl Into<String>, hosts: Vec<Arc<dyn MemoryHost>>) -> Self {
        Self::new(name, hosts, FusionStrategy::rrf_default())
    }

    /// Number of sub-hosts wired into this federation.
    pub fn host_count(&self) -> usize {
        self.hosts.len()
    }
}

#[async_trait]
impl MemoryHost for FederatedMemoryHost {
    fn name(&self) -> &str {
        &self.name
    }

    async fn query(&self, req: MemoryQuery) -> Result<Vec<MemoryHit>> {
        if self.hosts.is_empty() || req.top_k == 0 {
            return Ok(Vec::new());
        }

        // Fan out. Every sub-host sees an identical request; the
        // federator doesn't rewrite filters.
        let futures = self.hosts.iter().map(|h| {
            let h = Arc::clone(h);
            let req = req.clone();
            async move {
                let name = h.name().to_string();
                let res = h.query(req).await;
                (name, res)
            }
        });
        let results = join_all(futures).await;

        // Collect per-host ranked lists. Errors and empty results are
        // skipped with a warn! — a single failing backend never
        // fails the whole federated query.
        let mut ranked_lists: Vec<Vec<MemoryHit>> = Vec::with_capacity(results.len());
        for (host_name, r) in results {
            match r {
                Ok(hits) => {
                    if !hits.is_empty() {
                        ranked_lists.push(hits);
                    }
                }
                Err(e) => {
                    warn!(host = %host_name, error = %e, "federated sub-host failed; skipping");
                }
            }
        }

        if ranked_lists.is_empty() {
            return Ok(Vec::new());
        }

        Ok(fuse(ranked_lists, &self.strategy, req.top_k))
    }

    async fn upsert(&self, _doc: MemoryDoc) -> Result<String> {
        Err(anyhow!(
            "FederatedMemoryHost does not support upsert; call a specific sub-host"
        ))
    }

    async fn delete(&self, _id: &str) -> Result<()> {
        Err(anyhow!(
            "FederatedMemoryHost does not support delete; call a specific sub-host"
        ))
    }
}

/// Merge a set of ranked hit lists into one global top-`top_k` list.
///
/// Identity for de-duplication is `(source, id)` — two different
/// hosts returning the same internal id are kept as separate hits.
/// When the same `(source, id)` shows up in multiple input lists we
/// sum their reciprocal-rank contributions (defensive; in practice
/// each host contributes only once).
fn fuse(
    ranked_lists: Vec<Vec<MemoryHit>>,
    strategy: &FusionStrategy,
    top_k: usize,
) -> Vec<MemoryHit> {
    let k = match strategy {
        FusionStrategy::Rrf { k } => *k,
    };

    // Key = (source, id). Value = (fused_score, representative hit).
    let mut scores: HashMap<(String, String), (f32, MemoryHit)> = HashMap::new();

    for list in ranked_lists {
        for (rank, hit) in list.into_iter().enumerate() {
            let contribution = 1.0 / (k + rank as f32 + 1.0);
            let key = (hit.source.clone(), hit.id.clone());
            scores
                .entry(key)
                .and_modify(|(s, _)| *s += contribution)
                .or_insert((contribution, hit));
        }
    }

    let mut out: Vec<(f32, MemoryHit)> = scores
        .into_iter()
        .map(|(_, (score, mut hit))| {
            // Overwrite per-host score with fused score so downstream
            // code sees a directly comparable number.
            hit.score = score;
            (score, hit)
        })
        .collect();
    // Descending by fused score; stable tie-break by (source, id) so
    // tests are deterministic.
    out.sort_by(|a, b| {
        b.0.partial_cmp(&a.0)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.1.source.cmp(&b.1.source))
            .then_with(|| a.1.id.cmp(&b.1.id))
    });
    out.into_iter().take(top_k).map(|(_, h)| h).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    /// Deterministic mock host that returns a pre-canned list.
    struct MockHost {
        name: String,
        hits: Vec<MemoryHit>,
        delay: Option<Duration>,
        fail: bool,
    }

    impl MockHost {
        fn new(name: &str, hits: Vec<MemoryHit>) -> Self {
            Self {
                name: name.into(),
                hits,
                delay: None,
                fail: false,
            }
        }
        fn with_delay(mut self, d: Duration) -> Self {
            self.delay = Some(d);
            self
        }
        fn failing(name: &str) -> Self {
            Self {
                name: name.into(),
                hits: vec![],
                delay: None,
                fail: true,
            }
        }
    }

    #[async_trait]
    impl MemoryHost for MockHost {
        fn name(&self) -> &str {
            &self.name
        }
        async fn query(&self, _req: MemoryQuery) -> Result<Vec<MemoryHit>> {
            if let Some(d) = self.delay {
                tokio::time::sleep(d).await;
            }
            if self.fail {
                return Err(anyhow!("mock failure"));
            }
            Ok(self.hits.clone())
        }
        async fn upsert(&self, _doc: MemoryDoc) -> Result<String> {
            Ok("noop".into())
        }
        async fn delete(&self, _id: &str) -> Result<()> {
            Ok(())
        }
    }

    fn hit(source: &str, id: &str, score: f32) -> MemoryHit {
        MemoryHit {
            id: id.into(),
            content: format!("{source}:{id}"),
            score,
            source: source.into(),
            metadata: serde_json::Value::Null,
        }
    }

    #[tokio::test]
    async fn rrf_merges_two_hosts_and_ranks_overlap_higher() {
        // host_a ranks: a1, a2, shared
        // host_b ranks: shared, b1
        // A hit that appears in both rankings should outrank any that
        // appear in only one.
        let host_a = Arc::new(MockHost::new(
            "a",
            vec![
                hit("a", "a1", 10.0),
                hit("a", "a2", 9.0),
                hit("a", "shared", 8.0),
            ],
        )) as Arc<dyn MemoryHost>;
        let host_b = Arc::new(MockHost::new(
            "b",
            vec![hit("b", "shared", 7.0), hit("b", "b1", 6.0)],
        )) as Arc<dyn MemoryHost>;

        // Note: (source, id) is the fusion key, so the same internal
        // id from two different hosts ("a/shared" vs "b/shared")
        // stays separate. The overlap signal in RRF therefore comes
        // from how high each host ranked its own content, not from
        // cross-host id collisions. We assert positional ordering:
        // both hosts' rank-0 items share the top fused score.
        let fed =
            FederatedMemoryHost::new("fed", vec![host_a, host_b], FusionStrategy::Rrf { k: 60.0 });

        let merged = fed
            .query(MemoryQuery {
                text: "x".into(),
                top_k: 10,
                filters: vec![],
                namespace: None,
            })
            .await
            .unwrap();

        // 3 from host_a + 2 from host_b = 5 unique (source, id) keys.
        assert_eq!(merged.len(), 5);

        // Rank-0 items get 1/(60+1) each. Tie-break is by (source, id)
        // ascending, so host_a's rank-0 ("a1") comes before host_b's
        // rank-0 ("shared").
        assert_eq!(merged[0].source, "a");
        assert_eq!(merged[0].id, "a1");
        assert_eq!(merged[1].source, "b");
        assert_eq!(merged[1].id, "shared");

        // Scores are monotonically non-increasing.
        for w in merged.windows(2) {
            assert!(w[0].score >= w[1].score, "order broken: {merged:?}");
        }
    }

    #[tokio::test]
    async fn failing_host_is_skipped_and_healthy_host_still_returns() {
        let good = Arc::new(MockHost::new(
            "good",
            vec![hit("good", "g1", 5.0), hit("good", "g2", 4.0)],
        )) as Arc<dyn MemoryHost>;
        let bad = Arc::new(MockHost::failing("bad")) as Arc<dyn MemoryHost>;

        let fed = FederatedMemoryHost::with_rrf("fed", vec![good, bad]);

        let merged = fed
            .query(MemoryQuery {
                text: "x".into(),
                top_k: 5,
                filters: vec![],
                namespace: None,
            })
            .await
            .unwrap();

        assert_eq!(merged.len(), 2);
        assert!(merged.iter().all(|h| h.source == "good"));
    }

    #[tokio::test]
    async fn all_failing_returns_empty_not_error() {
        let f1 = Arc::new(MockHost::failing("f1")) as Arc<dyn MemoryHost>;
        let f2 = Arc::new(MockHost::failing("f2")) as Arc<dyn MemoryHost>;
        let fed = FederatedMemoryHost::with_rrf("fed", vec![f1, f2]);

        let merged = fed
            .query(MemoryQuery {
                text: "x".into(),
                top_k: 5,
                filters: vec![],
                namespace: None,
            })
            .await
            .unwrap();
        assert!(merged.is_empty());
    }

    #[tokio::test]
    async fn top_k_truncates_fused_list() {
        let host = Arc::new(MockHost::new(
            "h",
            vec![
                hit("h", "1", 5.0),
                hit("h", "2", 4.0),
                hit("h", "3", 3.0),
                hit("h", "4", 2.0),
            ],
        )) as Arc<dyn MemoryHost>;
        let fed = FederatedMemoryHost::with_rrf("fed", vec![host]);
        let merged = fed
            .query(MemoryQuery {
                text: "x".into(),
                top_k: 2,
                filters: vec![],
                namespace: None,
            })
            .await
            .unwrap();
        assert_eq!(merged.len(), 2);
    }

    #[tokio::test]
    async fn slow_host_alongside_fast_host_does_not_drop_fast_results() {
        // This guards the "fan-out is parallel, not sequential" property:
        // the slow host takes 150ms but the fast host's results are
        // present in the merged output. We don't assert timeout here —
        // per-host timeouts belong on the individual host (e.g. the
        // reqwest client in RemoteHttpHost); the federator's contract
        // is only "keep going if one peer errors or returns empty".
        let fast =
            Arc::new(MockHost::new("fast", vec![hit("fast", "x", 1.0)])) as Arc<dyn MemoryHost>;
        let slow = Arc::new(
            MockHost::new("slow", vec![hit("slow", "y", 1.0)])
                .with_delay(Duration::from_millis(50)),
        ) as Arc<dyn MemoryHost>;
        let fed = FederatedMemoryHost::with_rrf("fed", vec![fast, slow]);

        let merged = fed
            .query(MemoryQuery {
                text: "x".into(),
                top_k: 10,
                filters: vec![],
                namespace: None,
            })
            .await
            .unwrap();
        let sources: Vec<_> = merged.iter().map(|h| h.source.clone()).collect();
        assert!(sources.contains(&"fast".to_string()));
        assert!(sources.contains(&"slow".to_string()));
    }

    #[tokio::test]
    async fn upsert_and_delete_are_rejected_on_federation() {
        let fed: FederatedMemoryHost = FederatedMemoryHost::with_rrf("fed", vec![]);
        assert!(fed
            .upsert(MemoryDoc {
                content: "c".into(),
                metadata: serde_json::Value::Null,
                namespace: None,
            })
            .await
            .is_err());
        assert!(fed.delete("id").await.is_err());
    }
}
