//! Black-box sanity check that the three adapters compose through
//! the [`MemoryHost`] trait object.

use std::sync::Arc;

use corlinman_memory_host::{
    FederatedMemoryHost, LocalSqliteHost, MemoryDoc, MemoryHost, MemoryQuery,
};
use corlinman_vector::SqliteStore;
use tempfile::TempDir;

#[tokio::test]
async fn federation_over_two_local_sqlite_hosts() {
    let tmp = TempDir::new().unwrap();
    let store_a = Arc::new(
        SqliteStore::open(&tmp.path().join("a.sqlite"))
            .await
            .unwrap(),
    );
    let store_b = Arc::new(
        SqliteStore::open(&tmp.path().join("b.sqlite"))
            .await
            .unwrap(),
    );

    let host_a = Arc::new(LocalSqliteHost::new("kb-a", store_a)) as Arc<dyn MemoryHost>;
    let host_b = Arc::new(LocalSqliteHost::new("kb-b", store_b)) as Arc<dyn MemoryHost>;

    host_a
        .upsert(MemoryDoc {
            content: "shared token alpha only in kb-a".into(),
            metadata: serde_json::Value::Null,
            namespace: None,
        })
        .await
        .unwrap();
    host_b
        .upsert(MemoryDoc {
            content: "shared token alpha also in kb-b".into(),
            metadata: serde_json::Value::Null,
            namespace: None,
        })
        .await
        .unwrap();

    let fed = FederatedMemoryHost::with_rrf("fed", vec![host_a, host_b]);
    let hits = fed
        .query(MemoryQuery {
            text: "alpha".into(),
            top_k: 5,
            filters: vec![],
            namespace: None,
        })
        .await
        .unwrap();

    let sources: std::collections::HashSet<_> = hits.iter().map(|h| h.source.clone()).collect();
    assert!(sources.contains("kb-a"));
    assert!(sources.contains("kb-b"));
}
