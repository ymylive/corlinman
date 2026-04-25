//! `EvolutionApplier` — Phase 2 wave 2-A real applier for `memory_op`
//! proposals.
//!
//! Replaces the Phase 2 stub at `POST /admin/evolution/:id/apply` that
//! merely flipped a row's status. The applier:
//!
//! 1. Loads the proposal, validates `kind == memory_op` and
//!    `status == approved`.
//! 2. Parses the `target` (`merge_chunks:<a>,<b>` or
//!    `delete_chunk:<id>`) and reads the affected `chunks` rows from
//!    `kb.sqlite`.
//! 3. Computes `before_sha` / `after_sha` (SHA-256 of the chunk
//!    contents) and an `inverse_diff` JSON describing how to undo the
//!    op (used by Phase 3 `AutoRollback` — Phase 2 just persists it).
//! 4. Issues the kb mutation (a single SQLite `DELETE` — atomic on its
//!    own; the `chunks_ad` trigger keeps `chunks_fts` in sync).
//! 5. Writes the `evolution_history` row and flips
//!    `evolution_proposals.status = 'applied' (+ applied_at)` inside a
//!    single SQLite transaction on `evolution.sqlite`.
//!
//! ## Two-DB partial failure
//!
//! `kb.sqlite` and `evolution.sqlite` are separate files: a transaction
//! cannot span them. The applier orders writes so the partial-fail mode
//! degrades gracefully:
//!
//! - kb mutation fails → no history row, proposal stays `approved`.
//!   Caller can re-`apply` once the underlying issue is fixed.
//! - kb mutation succeeds, evolution TX fails → data is gone but the
//!   audit trail is silent. This is a known Phase 2 limitation; Phase 3
//!   `AutoRollback` will spot the metrics regression and re-issue.
//!
//! ## Supported targets
//!
//! Phase 2 ships only `memory_op` execution. Other kinds (`tag_rebalance`,
//! `skill_update`, ...) return [`ApplyError::UnsupportedKind`] so callers
//! see an explicit 4xx instead of a silent no-op.

use std::sync::Arc;

use corlinman_core::metrics::{
    EVOLUTION_CHUNKS_DELETED, EVOLUTION_CHUNKS_MERGED, EVOLUTION_PROPOSALS_APPLIED,
};
use corlinman_evolution::{
    EvolutionHistory, EvolutionKind, EvolutionStatus, EvolutionStore, ProposalId, ProposalsRepo,
    RepoError,
};
use corlinman_vector::SqliteStore;
use serde_json::json;
use sha2::{Digest, Sha256};
use sqlx::Row;

/// Errors surfaced by [`EvolutionApplier::apply`]. Mapped to HTTP status
/// codes by the `/admin/evolution/:id/apply` route handler.
#[derive(Debug, thiserror::Error)]
pub enum ApplyError {
    /// Target string didn't match any supported `memory_op` shape.
    #[error("invalid target: {0}")]
    InvalidTarget(String),
    /// Proposal is not in `approved`. Carries the actual status string.
    #[error("proposal not approved (status={0})")]
    NotApproved(String),
    /// Phase 2 only ships `memory_op`. Other kinds bail here.
    #[error("kind {0} cannot be applied yet (Phase 2 = memory_op only)")]
    UnsupportedKind(String),
    /// Proposal id wasn't in `evolution_proposals`.
    #[error("proposal not found: {0}")]
    NotFound(String),
    /// Referenced chunk row missing from `kb.sqlite`.
    #[error("chunk not found: id={0}")]
    ChunkNotFound(i64),
    /// `kb.sqlite` mutation failed.
    #[error("kb operation failed: {0}")]
    Kb(#[source] anyhow::Error),
    /// `evolution.sqlite` history insert / proposal flip failed.
    #[error("history write failed: {0}")]
    History(#[source] anyhow::Error),
    /// Repo-level read on the proposal failed.
    #[error("repo error: {0}")]
    Repo(#[from] RepoError),
}

/// Real applier for `memory_op` evolution proposals. Constructed at
/// gateway startup once the kb + evolution stores are open; held inside
/// `AdminState` as `Option<Arc<EvolutionApplier>>` so the apply route
/// can return 503 when either store is missing.
pub struct EvolutionApplier {
    proposals: ProposalsRepo,
    kb_store: Arc<SqliteStore>,
    evolution_store: Arc<EvolutionStore>,
}

impl EvolutionApplier {
    /// Build an applier from the shared stores. The caller has already
    /// opened both SQLite files; we just need handles. The `ProposalsRepo`
    /// is a cheap pool-clone wrapper kept as a field so each `apply()`
    /// skips a `Repo::new` per call. The history insert + proposal
    /// status flip share one transaction, so we hold the
    /// `EvolutionStore` directly rather than constructing a `HistoryRepo`
    /// — the repo's `insert` doesn't take a `Transaction`, and adding a
    /// TX-aware variant would touch `corlinman-evolution`.
    pub fn new(evolution_store: Arc<EvolutionStore>, kb_store: Arc<SqliteStore>) -> Self {
        let proposals = ProposalsRepo::new(evolution_store.pool().clone());
        Self {
            proposals,
            kb_store,
            evolution_store,
        }
    }

    /// Apply an approved proposal. Returns the freshly-inserted history
    /// row (with autoincrement id populated). Failures leave the
    /// proposal in `approved` and write no history row — apart from the
    /// known two-DB partial-fail mode documented at the module level.
    pub async fn apply(&self, id: &ProposalId) -> Result<EvolutionHistory, ApplyError> {
        // 1. Load + gate.
        let proposal = match self.proposals.get(id).await {
            Ok(p) => p,
            Err(RepoError::NotFound(_)) => return Err(ApplyError::NotFound(id.0.clone())),
            Err(other) => return Err(ApplyError::Repo(other)),
        };
        if proposal.status != EvolutionStatus::Approved {
            return Err(ApplyError::NotApproved(
                proposal.status.as_str().to_string(),
            ));
        }
        if proposal.kind != EvolutionKind::MemoryOp {
            return Err(ApplyError::UnsupportedKind(
                proposal.kind.as_str().to_string(),
            ));
        }

        // 2. Parse + plan + execute the kb mutation.
        let plan = MemoryOp::parse(&proposal.target)?;
        let mutation = self.execute(&plan).await?;

        // 3. Persist history + flip proposal.status atomically inside
        //    evolution.sqlite. kb.sqlite is already mutated (single
        //    DELETE statement, atomic by SQLite contract); a TX here
        //    keeps the audit row + status flip in lockstep.
        let now = now_ms();
        let history_row = EvolutionHistory {
            id: None,
            proposal_id: id.clone(),
            kind: proposal.kind,
            target: proposal.target.clone(),
            before_sha: mutation.before_sha,
            after_sha: mutation.after_sha,
            inverse_diff: mutation.inverse_diff,
            metrics_baseline: serde_json::Value::Object(serde_json::Map::new()),
            applied_at: now,
            rolled_back_at: None,
            rollback_reason: None,
        };
        let history_id = self
            .commit_evolution_tx(&history_row, id, now)
            .await
            .map_err(|e| ApplyError::History(anyhow::Error::from(e)))?;

        // 4. Bump kb-side counters only after the audit row landed.
        match plan {
            MemoryOp::MergeChunks { .. } => EVOLUTION_CHUNKS_MERGED.inc(),
            MemoryOp::DeleteChunk { .. } => EVOLUTION_CHUNKS_DELETED.inc(),
        }
        EVOLUTION_PROPOSALS_APPLIED
            .with_label_values(&[proposal.kind.as_str(), "ok"])
            .inc();

        let mut out = history_row;
        out.id = Some(history_id);
        Ok(out)
    }

    /// Carry out the kb mutation described by `plan`. On the read path
    /// we use `query_chunks_by_ids` (already part of `SqliteStore`'s
    /// public API); on the write path we use `delete_chunk_by_id` so
    /// the `chunks_ad` trigger keeps `chunks_fts` in sync for free.
    async fn execute(&self, plan: &MemoryOp) -> Result<MutationOutcome, ApplyError> {
        match plan {
            MemoryOp::MergeChunks { winner, loser } => {
                let rows = self
                    .kb_store
                    .query_chunks_by_ids(&[*winner, *loser])
                    .await
                    .map_err(ApplyError::Kb)?;
                let winner_row = rows
                    .iter()
                    .find(|r| r.id == *winner)
                    .ok_or(ApplyError::ChunkNotFound(*winner))?;
                let loser_row = rows
                    .iter()
                    .find(|r| r.id == *loser)
                    .ok_or(ApplyError::ChunkNotFound(*loser))?;

                let before_sha = sha256_concat(&winner_row.content, &loser_row.content);
                let after_sha = sha256_hex(winner_row.content.as_bytes());
                let inverse_diff = json!({
                    "action": "restore_chunk",
                    "loser_id": loser_row.id,
                    "loser_content": loser_row.content,
                    "loser_namespace": loser_row.namespace,
                    "loser_file_id": loser_row.file_id,
                    "loser_chunk_index": loser_row.chunk_index,
                })
                .to_string();

                // SQLite single-statement DELETE is atomic; the
                // `chunks_ad` trigger updates `chunks_fts` in the same
                // implicit transaction.
                let removed = self
                    .kb_store
                    .delete_chunk_by_id(*loser)
                    .await
                    .map_err(ApplyError::Kb)?;
                if removed == 0 {
                    return Err(ApplyError::ChunkNotFound(*loser));
                }

                Ok(MutationOutcome {
                    before_sha,
                    after_sha,
                    inverse_diff,
                })
            }
            MemoryOp::DeleteChunk { id } => {
                let rows = self
                    .kb_store
                    .query_chunks_by_ids(&[*id])
                    .await
                    .map_err(ApplyError::Kb)?;
                let row = rows
                    .into_iter()
                    .find(|r| r.id == *id)
                    .ok_or(ApplyError::ChunkNotFound(*id))?;

                let before_sha = sha256_hex(row.content.as_bytes());
                let after_sha = sha256_hex(b"");
                let inverse_diff = json!({
                    "action": "restore_chunk",
                    "content": row.content,
                    "namespace": row.namespace,
                    "file_id": row.file_id,
                    "chunk_index": row.chunk_index,
                })
                .to_string();

                let removed = self
                    .kb_store
                    .delete_chunk_by_id(*id)
                    .await
                    .map_err(ApplyError::Kb)?;
                if removed == 0 {
                    return Err(ApplyError::ChunkNotFound(*id));
                }

                Ok(MutationOutcome {
                    before_sha,
                    after_sha,
                    inverse_diff,
                })
            }
        }
    }

    /// Insert the history row and flip the proposal status inside a
    /// single `evolution.sqlite` transaction. Returns the inserted
    /// history id on commit. The two repos already serialise their
    /// inputs the same way the public APIs do — we replicate that
    /// here so behaviour stays identical to the non-TX paths.
    async fn commit_evolution_tx(
        &self,
        h: &EvolutionHistory,
        proposal_id: &ProposalId,
        applied_at_ms: i64,
    ) -> Result<i64, sqlx::Error> {
        let metrics = serde_json::to_string(&h.metrics_baseline).unwrap_or_else(|_| "{}".into());
        let mut tx = self.evolution_store.pool().begin().await?;

        let row = sqlx::query(
            r#"INSERT INTO evolution_history
                 (proposal_id, kind, target, before_sha, after_sha,
                  inverse_diff, metrics_baseline, applied_at,
                  rolled_back_at, rollback_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               RETURNING id"#,
        )
        .bind(h.proposal_id.as_str())
        .bind(h.kind.as_str())
        .bind(&h.target)
        .bind(&h.before_sha)
        .bind(&h.after_sha)
        .bind(&h.inverse_diff)
        .bind(metrics)
        .bind(h.applied_at)
        .bind(h.rolled_back_at)
        .bind(&h.rollback_reason)
        .fetch_one(&mut *tx)
        .await?;
        let history_id: i64 = row.get("id");

        let res = sqlx::query(
            "UPDATE evolution_proposals
                SET status = 'applied', applied_at = ?
              WHERE id = ?",
        )
        .bind(applied_at_ms)
        .bind(proposal_id.as_str())
        .execute(&mut *tx)
        .await?;
        if res.rows_affected() == 0 {
            return Err(sqlx::Error::RowNotFound);
        }

        tx.commit().await?;
        Ok(history_id)
    }

    /// Bump the `error` outcome counter. Called by the route handler
    /// when [`apply`] returns a non-`NotApproved`/`NotFound` failure
    /// after a partial mutation already happened — keeps the metric
    /// label cardinality stable so dashboards can compare ok / error
    /// rates per `kind`.
    pub fn observe_failure(kind: EvolutionKind) {
        EVOLUTION_PROPOSALS_APPLIED
            .with_label_values(&[kind.as_str(), "error"])
            .inc();
    }
}

/// Internal representation of a parsed `memory_op` target.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum MemoryOp {
    /// `merge_chunks:<a>,<b>` — kept the smaller id, drop the larger.
    MergeChunks { winner: i64, loser: i64 },
    /// `delete_chunk:<id>` — drop one chunk.
    DeleteChunk { id: i64 },
}

impl MemoryOp {
    fn parse(target: &str) -> Result<Self, ApplyError> {
        if let Some(rest) = target.strip_prefix("merge_chunks:") {
            let mut parts = rest.split(',');
            let a = parts
                .next()
                .ok_or_else(|| ApplyError::InvalidTarget(target.into()))?
                .trim();
            let b = parts
                .next()
                .ok_or_else(|| ApplyError::InvalidTarget(target.into()))?
                .trim();
            if parts.next().is_some() {
                return Err(ApplyError::InvalidTarget(target.into()));
            }
            let a: i64 = a
                .parse()
                .map_err(|_| ApplyError::InvalidTarget(target.into()))?;
            let b: i64 = b
                .parse()
                .map_err(|_| ApplyError::InvalidTarget(target.into()))?;
            if a == b {
                return Err(ApplyError::InvalidTarget(target.into()));
            }
            // "first commit wins" — smaller id is the winner.
            let (winner, loser) = if a < b { (a, b) } else { (b, a) };
            Ok(Self::MergeChunks { winner, loser })
        } else if let Some(rest) = target.strip_prefix("delete_chunk:") {
            let id: i64 = rest
                .trim()
                .parse()
                .map_err(|_| ApplyError::InvalidTarget(target.into()))?;
            Ok(Self::DeleteChunk { id })
        } else {
            Err(ApplyError::InvalidTarget(target.into()))
        }
    }
}

/// Outputs of a kb mutation surfaced into the history row.
struct MutationOutcome {
    before_sha: String,
    after_sha: String,
    inverse_diff: String,
}

fn sha256_hex(bytes: &[u8]) -> String {
    let mut h = Sha256::new();
    h.update(bytes);
    format!("{:x}", h.finalize())
}

/// Hash of `winner.content || 0x00 || loser.content`. The null
/// separator prevents `("ab","c")` and `("a","bc")` from colliding —
/// the chunks table doesn't enforce content uniqueness so the guard is
/// cheap insurance.
fn sha256_concat(winner: &str, loser: &str) -> String {
    let mut h = Sha256::new();
    h.update(winner.as_bytes());
    h.update([0u8]);
    h.update(loser.as_bytes());
    format!("{:x}", h.finalize())
}

/// Unix milliseconds. Local helper rather than a public crate-level
/// helper — only this module + the admin route need it, and they don't
/// share a now() source with anything else.
fn now_ms() -> i64 {
    let nanos = time::OffsetDateTime::now_utc().unix_timestamp_nanos();
    (nanos / 1_000_000) as i64
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use corlinman_evolution::{
        EvolutionProposal, EvolutionRisk, EvolutionStatus, EvolutionStore, ProposalId,
        ProposalsRepo,
    };
    use tempfile::TempDir;

    /// Stand up fresh kb + evolution stores in a tempdir. Returns the
    /// applier plus handles for assertion paths.
    async fn fresh_applier() -> (TempDir, EvolutionApplier, Arc<SqliteStore>, Arc<EvolutionStore>) {
        let tmp = TempDir::new().unwrap();
        let kb_path = tmp.path().join("kb.sqlite");
        let evol_path = tmp.path().join("evolution.sqlite");
        let kb = Arc::new(SqliteStore::open(&kb_path).await.unwrap());
        let evol = Arc::new(EvolutionStore::open(&evol_path).await.unwrap());
        let applier = EvolutionApplier::new(evol.clone(), kb.clone());
        (tmp, applier, kb, evol)
    }

    /// Insert a chunk + its parent file into kb. Returns the chunk id.
    async fn seed_chunk(kb: &SqliteStore, path: &str, content: &str) -> i64 {
        let file_id = kb
            .insert_file(path, "test", "checksum", 0, content.len() as i64)
            .await
            .unwrap();
        kb.insert_chunk(file_id, 0, content, None, "general")
            .await
            .unwrap()
    }

    /// Insert an `approved` `memory_op` proposal aimed at `target`.
    async fn seed_approved(
        evol: &EvolutionStore,
        id: &str,
        target: &str,
    ) -> ProposalId {
        let pid = ProposalId::new(id);
        let repo = ProposalsRepo::new(evol.pool().clone());
        repo.insert(&EvolutionProposal {
            id: pid.clone(),
            kind: EvolutionKind::MemoryOp,
            target: target.into(),
            diff: String::new(),
            reasoning: "test".into(),
            risk: EvolutionRisk::Low,
            budget_cost: 0,
            status: EvolutionStatus::Approved,
            shadow_metrics: None,
            signal_ids: vec![],
            trace_ids: vec![],
            created_at: 1_000,
            decided_at: Some(2_000),
            decided_by: Some("operator".into()),
            applied_at: None,
            rollback_of: None,
        })
        .await
        .unwrap();
        pid
    }

    #[test]
    fn parse_merge_chunks_picks_smaller_id_as_winner() {
        let plan = MemoryOp::parse("merge_chunks:42,43").unwrap();
        assert_eq!(
            plan,
            MemoryOp::MergeChunks {
                winner: 42,
                loser: 43,
            }
        );
        // Order in the target doesn't matter — winner is still the
        // smaller id ("first commit wins").
        let plan = MemoryOp::parse("merge_chunks:43,42").unwrap();
        assert_eq!(
            plan,
            MemoryOp::MergeChunks {
                winner: 42,
                loser: 43,
            }
        );
    }

    #[test]
    fn parse_delete_chunk_round_trip() {
        let plan = MemoryOp::parse("delete_chunk:99").unwrap();
        assert_eq!(plan, MemoryOp::DeleteChunk { id: 99 });
    }

    #[test]
    fn parse_rejects_unknown_prefix() {
        assert!(matches!(
            MemoryOp::parse("rebuild_index:foo"),
            Err(ApplyError::InvalidTarget(_))
        ));
    }

    #[test]
    fn parse_rejects_malformed_inputs() {
        for bad in [
            "merge_chunks:",
            "merge_chunks:1",
            "merge_chunks:1,2,3",
            "merge_chunks:abc,2",
            "merge_chunks:5,5",
            "delete_chunk:",
            "delete_chunk:abc",
        ] {
            assert!(
                matches!(MemoryOp::parse(bad), Err(ApplyError::InvalidTarget(_))),
                "expected InvalidTarget for {bad:?}",
            );
        }
    }

    #[tokio::test]
    async fn apply_merge_chunks_happy_path() {
        let (_tmp, applier, kb, evol) = fresh_applier().await;
        let a = seed_chunk(&kb, "/a", "alpha winner content").await;
        let b = seed_chunk(&kb, "/b", "alpha loser content").await;
        let target = format!("merge_chunks:{a},{b}");
        let pid = seed_approved(&evol, "evol-merge-001", &target).await;

        let history = applier.apply(&pid).await.unwrap();
        assert!(history.id.is_some());
        assert_eq!(history.kind, EvolutionKind::MemoryOp);
        assert_eq!(history.target, target);
        assert_ne!(history.before_sha, history.after_sha);

        // Loser gone, winner still present.
        let rows = kb.query_chunks_by_ids(&[a, b]).await.unwrap();
        let ids: Vec<i64> = rows.iter().map(|r| r.id).collect();
        assert_eq!(ids, vec![a], "loser deleted, winner kept");

        // Proposal flipped to applied with applied_at populated.
        let repo = ProposalsRepo::new(evol.pool().clone());
        let after = repo.get(&pid).await.unwrap();
        assert_eq!(after.status, EvolutionStatus::Applied);
        assert!(after.applied_at.is_some());

        // Inverse diff carries enough to reconstruct the loser.
        let inverse: serde_json::Value = serde_json::from_str(&history.inverse_diff).unwrap();
        assert_eq!(inverse["action"], "restore_chunk");
        assert_eq!(inverse["loser_id"], b);
        assert_eq!(inverse["loser_content"], "alpha loser content");
        assert_eq!(inverse["loser_namespace"], "general");
    }

    #[tokio::test]
    async fn apply_delete_chunk_happy_path() {
        let (_tmp, applier, kb, evol) = fresh_applier().await;
        let id = seed_chunk(&kb, "/d", "doomed content").await;
        let target = format!("delete_chunk:{id}");
        let pid = seed_approved(&evol, "evol-del-001", &target).await;

        let history = applier.apply(&pid).await.unwrap();
        // after_sha is sha256("") for delete_chunk.
        assert_eq!(history.after_sha, sha256_hex(b""));

        let rows = kb.query_chunks_by_ids(&[id]).await.unwrap();
        assert!(rows.is_empty(), "chunk row deleted");

        let inverse: serde_json::Value = serde_json::from_str(&history.inverse_diff).unwrap();
        assert_eq!(inverse["action"], "restore_chunk");
        assert_eq!(inverse["content"], "doomed content");
        assert_eq!(inverse["namespace"], "general");
    }

    #[tokio::test]
    async fn apply_rejects_non_approved_status() {
        let (_tmp, applier, kb, evol) = fresh_applier().await;
        let id = seed_chunk(&kb, "/c", "stays put").await;
        // Insert a *pending* proposal — should refuse to apply.
        let pid = ProposalId::new("evol-pending-001");
        let repo = ProposalsRepo::new(evol.pool().clone());
        repo.insert(&EvolutionProposal {
            id: pid.clone(),
            kind: EvolutionKind::MemoryOp,
            target: format!("delete_chunk:{id}"),
            diff: String::new(),
            reasoning: String::new(),
            risk: EvolutionRisk::Low,
            budget_cost: 0,
            status: EvolutionStatus::Pending,
            shadow_metrics: None,
            signal_ids: vec![],
            trace_ids: vec![],
            created_at: 1_000,
            decided_at: None,
            decided_by: None,
            applied_at: None,
            rollback_of: None,
        })
        .await
        .unwrap();

        match applier.apply(&pid).await {
            Err(ApplyError::NotApproved(s)) => assert_eq!(s, "pending"),
            other => panic!("expected NotApproved, got {other:?}"),
        }
        // Chunk untouched.
        let rows = kb.query_chunks_by_ids(&[id]).await.unwrap();
        assert_eq!(rows.len(), 1);
    }

    #[tokio::test]
    async fn apply_rejects_non_memory_op_kind() {
        let (_tmp, applier, _kb, evol) = fresh_applier().await;
        // Insert an approved proposal with kind != memory_op.
        let pid = ProposalId::new("evol-tag-001");
        let repo = ProposalsRepo::new(evol.pool().clone());
        repo.insert(&EvolutionProposal {
            id: pid.clone(),
            kind: EvolutionKind::TagRebalance,
            target: "tag_tree".into(),
            diff: String::new(),
            reasoning: String::new(),
            risk: EvolutionRisk::Low,
            budget_cost: 0,
            status: EvolutionStatus::Approved,
            shadow_metrics: None,
            signal_ids: vec![],
            trace_ids: vec![],
            created_at: 1_000,
            decided_at: Some(2_000),
            decided_by: Some("op".into()),
            applied_at: None,
            rollback_of: None,
        })
        .await
        .unwrap();
        match applier.apply(&pid).await {
            Err(ApplyError::UnsupportedKind(s)) => assert_eq!(s, "tag_rebalance"),
            other => panic!("expected UnsupportedKind, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn apply_rejects_malformed_target() {
        let (_tmp, applier, _kb, evol) = fresh_applier().await;
        let pid = seed_approved(&evol, "evol-bad-001", "rebuild_everything").await;
        match applier.apply(&pid).await {
            Err(ApplyError::InvalidTarget(s)) => assert_eq!(s, "rebuild_everything"),
            other => panic!("expected InvalidTarget, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn apply_returns_chunk_not_found_when_target_id_missing() {
        let (_tmp, applier, _kb, evol) = fresh_applier().await;
        let pid = seed_approved(&evol, "evol-missing-001", "delete_chunk:99999").await;
        assert!(matches!(
            applier.apply(&pid).await,
            Err(ApplyError::ChunkNotFound(99999))
        ));
    }
}
