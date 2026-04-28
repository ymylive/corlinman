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
//! Phase 3-2B ships `memory_op`, `tag_rebalance`, and `skill_update`
//! execution. Remaining kinds (`retry_tuning`, `agent_card`,
//! `prompt_template`, `tool_policy`, `new_skill`) return
//! [`ApplyError::UnsupportedKind`] so callers see an explicit 4xx
//! instead of a silent no-op.

use std::path::PathBuf;
use std::sync::Arc;

use async_trait::async_trait;
use corlinman_auto_rollback::metrics::{capture_snapshot, watched_event_kinds};
use corlinman_auto_rollback::revert::{Applier as AutoRollbackApplier, RevertError};
use corlinman_core::config::AutoRollbackThresholds;
use corlinman_core::metrics::{
    EVOLUTION_CHUNKS_DELETED, EVOLUTION_CHUNKS_MERGED, EVOLUTION_PROPOSALS_APPLIED,
    EVOLUTION_PROPOSALS_ROLLED_BACK,
};
use corlinman_evolution::{
    EvolutionHistory, EvolutionKind, EvolutionStatus, EvolutionStore, HistoryRepo, ProposalId,
    ProposalsRepo, RepoError,
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
    /// No forward handler for this kind yet. Phase 3-2B activated
    /// `memory_op`, `tag_rebalance`, and `skill_update`; new kinds land
    /// here until their handlers ship.
    #[error("kind {0} cannot be applied yet")]
    UnsupportedKind(String),
    /// Proposal id wasn't in `evolution_proposals`.
    #[error("proposal not found: {0}")]
    NotFound(String),
    /// Referenced chunk row missing from `kb.sqlite`.
    #[error("chunk not found: id={0}")]
    ChunkNotFound(i64),
    /// Phase 3-2B: `tag_rebalance` target referenced a `tag_nodes.path`
    /// that isn't in the kb. Carries the requested path.
    #[error("tag node not found: {0}")]
    TagNotFound(String),
    /// Phase 3-2B: `tag_rebalance` aimed at a root `tag_nodes` row
    /// (`parent_id IS NULL`). Operators must not flatten the root —
    /// reject explicitly so a buggy proposer can't wipe the tree.
    #[error("cannot merge root tag node")]
    CannotMergeRoot,
    /// Phase 3-2B: `skill_update` target points at a file that doesn't
    /// exist on disk. Carries the resolved path.
    #[error("skill file missing: {0}")]
    SkillFileMissing(String),
    /// Phase 3-2B: `skill_update` diff used a hunk header that v0.3
    /// doesn't know how to apply (only the `__APPEND__` sentinel from
    /// the Step-1 EvolutionEngine is supported).
    #[error("unsupported diff shape: {0}")]
    UnsupportedDiffShape(String),
    /// `kb.sqlite` mutation failed.
    #[error("kb operation failed: {0}")]
    Kb(#[source] anyhow::Error),
    /// `evolution.sqlite` history insert / proposal flip failed.
    #[error("history write failed: {0}")]
    History(#[source] anyhow::Error),
    /// Repo-level read on the proposal failed.
    #[error("repo error: {0}")]
    Repo(#[from] RepoError),
    /// Phase 3 W1-B revert: proposal isn't in `applied`. Carries the
    /// actual status string. Distinct from `NotApproved` so the monitor
    /// can tell "already rolled back" apart from "never applied".
    #[error("proposal not applied (status={0})")]
    NotApplied(String),
    /// Phase 3 W1-B revert: forward apply succeeded but the audit row
    /// is gone — flag as data corruption rather than silent skip.
    #[error("history row missing for proposal {0}")]
    HistoryMissing(String),
    /// Phase 3 W1-B revert: kind has no inverse handler yet. W1-B ships
    /// `memory_op` only — sibling lines land here as later kinds activate.
    #[error("kind {0} cannot be reverted yet")]
    UnsupportedRevertKind(String),
    /// Phase 3 W1-B revert: `inverse_diff` JSON didn't parse or was
    /// missing required keys. Carries a short reason string.
    #[error("malformed inverse_diff: {0}")]
    MalformedInverseDiff(String),
}

/// Real applier for `memory_op` evolution proposals. Constructed at
/// gateway startup once the kb + evolution stores are open; held inside
/// `AdminState` as `Option<Arc<EvolutionApplier>>` so the apply route
/// can return 503 when either store is missing.
pub struct EvolutionApplier {
    proposals: ProposalsRepo,
    history: HistoryRepo,
    kb_store: Arc<SqliteStore>,
    evolution_store: Arc<EvolutionStore>,
    /// AutoRollback thresholds (Phase 3 W1-B). Owned here so the applier
    /// uses the same `signal_window_secs` for the baseline snapshot as
    /// the monitor uses for the post-apply snapshot — symmetric windows
    /// prevent sample-mismatch false positives.
    auto_rollback_thresholds: AutoRollbackThresholds,
    /// Phase 3-2B: root directory under which `skill_update` proposals
    /// resolve their `skills/<name>.md` targets. Owned (not borrowed)
    /// because the applier outlives any single config snapshot — same
    /// reasoning as the kb store handle.
    skills_dir: PathBuf,
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
    ///
    /// `auto_rollback_thresholds` is consumed at apply time to size the
    /// `metrics_baseline` window. We capture the snapshot regardless of
    /// the master `enabled` flag — populating baselines while the
    /// monitor is off is cheap and gives operators historical data to
    /// flip on later (see `EvolutionAutoRollbackConfig` doc).
    pub fn new(
        evolution_store: Arc<EvolutionStore>,
        kb_store: Arc<SqliteStore>,
        auto_rollback_thresholds: AutoRollbackThresholds,
        skills_dir: PathBuf,
    ) -> Self {
        let proposals = ProposalsRepo::new(evolution_store.pool().clone());
        let history = HistoryRepo::new(evolution_store.pool().clone());
        Self {
            proposals,
            history,
            kb_store,
            evolution_store,
            auto_rollback_thresholds,
            skills_dir,
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

        // 2. Dispatch per-kind. Each handler returns a `MutationOutcome`
        //    so the audit/baseline path below stays kind-agnostic.
        //    `memory_op` carries a parsed plan (`merge_chunks` or
        //    `delete_chunk`) for the counter bump after the audit lands;
        //    the new kinds bump no counters yet (Phase 3-2B doesn't ship
        //    per-kind metrics — operator dashboards read history).
        let mut memory_plan: Option<MemoryOp> = None;
        let mutation = match proposal.kind {
            EvolutionKind::MemoryOp => {
                let plan = MemoryOp::parse(&proposal.target)?;
                let m = self.execute(&plan).await?;
                memory_plan = Some(plan);
                m
            }
            EvolutionKind::TagRebalance => self.apply_tag_rebalance(&proposal.target).await?,
            EvolutionKind::SkillUpdate => {
                self.apply_skill_update(&proposal.target, &proposal.diff)
                    .await?
            }
            other => return Err(ApplyError::UnsupportedKind(other.as_str().to_string())),
        };

        // 3. Persist history + flip proposal.status atomically inside
        //    evolution.sqlite. kb.sqlite is already mutated (single
        //    DELETE statement, atomic by SQLite contract); a TX here
        //    keeps the audit row + status flip in lockstep.
        let now = now_ms();

        // W1-B Step 2: capture per-event-kind signal counts at apply
        // time so AutoRollback (Step 4) can compare against a fresh
        // post-apply snapshot. Empty whitelist (kind not yet wired) →
        // empty baseline so monitor knows to skip — see
        // `watched_event_kinds`.
        let watched = watched_event_kinds(proposal.kind);
        let metrics_baseline = if watched.is_empty() {
            tracing::debug!(
                kind = proposal.kind.as_str(),
                "no AutoRollback whitelist for {} yet; metrics_baseline left empty",
                proposal.kind.as_str(),
            );
            serde_json::Value::Object(serde_json::Map::new())
        } else {
            let snap = capture_snapshot(
                self.evolution_store.pool(),
                &proposal.target,
                watched,
                self.auto_rollback_thresholds.signal_window_secs,
                now,
            )
            .await
            .map_err(|e| ApplyError::History(anyhow::Error::from(e)))?;
            serde_json::to_value(&snap)
                .map_err(|e| ApplyError::History(anyhow::Error::from(e)))?
        };

        let history_row = EvolutionHistory {
            id: None,
            proposal_id: id.clone(),
            kind: proposal.kind,
            target: proposal.target.clone(),
            before_sha: mutation.before_sha,
            after_sha: mutation.after_sha,
            inverse_diff: mutation.inverse_diff,
            metrics_baseline,
            applied_at: now,
            rolled_back_at: None,
            rollback_reason: None,
        };
        let history_id = self
            .commit_evolution_tx(&history_row, id, now)
            .await
            .map_err(|e| ApplyError::History(anyhow::Error::from(e)))?;

        // 4. Bump kb-side counters only after the audit row landed.
        //    `memory_op` has dedicated counters; other kinds rely on
        //    EVOLUTION_PROPOSALS_APPLIED below for now.
        if let Some(plan) = memory_plan {
            match plan {
                MemoryOp::MergeChunks { .. } => EVOLUTION_CHUNKS_MERGED.inc(),
                MemoryOp::DeleteChunk { .. } => EVOLUTION_CHUNKS_DELETED.inc(),
                // Phase 3 W3-A: ConsolidateChunk has no dedicated
                // counter — `EVOLUTION_PROPOSALS_APPLIED` below covers
                // it via the `kind=memory_op` label.
                MemoryOp::ConsolidateChunk { .. } => {}
            }
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
            MemoryOp::ConsolidateChunk { id } => {
                // Snapshot the prior decay state into `inverse_diff` so
                // revert can restore the chunk byte-for-byte without
                // guessing what its namespace/decay_score were before
                // promotion.
                let prior = self
                    .kb_store
                    .get_chunk_decay_state(*id)
                    .await
                    .map_err(ApplyError::Kb)?
                    .ok_or(ApplyError::ChunkNotFound(*id))?;
                if prior.namespace == corlinman_vector::CONSOLIDATED_NAMESPACE {
                    // Already consolidated — surface the mismatch so a
                    // buggy proposer becomes noticeable instead of
                    // silently flipping a no-op into the audit trail.
                    return Err(ApplyError::InvalidTarget(format!(
                        "consolidate_chunk:{id} already consolidated"
                    )));
                }
                let promoted = self
                    .kb_store
                    .promote_to_consolidated(&[*id])
                    .await
                    .map_err(ApplyError::Kb)?;
                if promoted == 0 {
                    return Err(ApplyError::ChunkNotFound(*id));
                }

                let before_sha = sha256_hex(
                    format!(
                        "ns={};decay={};consolidated_at={:?}",
                        prior.namespace, prior.decay_score, prior.consolidated_at
                    )
                    .as_bytes(),
                );
                let after_sha = sha256_hex(format!("ns=consolidated;chunk_id={id}").as_bytes());
                let inverse_diff = json!({
                    "action": "demote_chunk",
                    "chunk_id": id,
                    "prior_namespace": prior.namespace,
                    "prior_decay_score": prior.decay_score,
                })
                .to_string();
                Ok(MutationOutcome {
                    before_sha,
                    after_sha,
                    inverse_diff,
                })
            }
        }
    }

    /// Phase 3-2B: forward apply for `tag_rebalance`. Target shape is
    /// `merge_tag:<path>`; we look up the matching `tag_nodes` row,
    /// reparent its `chunk_tags` rows to the parent, then drop the node.
    /// Captured `inverse_diff` records the deleted node's full row +
    /// the chunk_ids whose `tag_node_id` we rewrote, so revert can
    /// reinsert the node and point those rows back at it.
    async fn apply_tag_rebalance(
        &self,
        target: &str,
    ) -> Result<MutationOutcome, ApplyError> {
        let path = target
            .strip_prefix("merge_tag:")
            .ok_or_else(|| ApplyError::InvalidTarget(target.into()))?;
        if path.is_empty() {
            return Err(ApplyError::InvalidTarget(target.into()));
        }

        let pool = self.kb_store.pool();

        // 1. Fetch the source row by path. `tag_nodes.path` is UNIQUE
        //    per the v6 schema, so a single row or no row.
        let row = sqlx::query(
            "SELECT id, parent_id, name, path, depth, created_at \
             FROM tag_nodes WHERE path = ?1",
        )
        .bind(path)
        .fetch_optional(pool)
        .await
        .map_err(|e| ApplyError::Kb(anyhow::Error::from(e)))?;
        let row = row.ok_or_else(|| ApplyError::TagNotFound(path.into()))?;

        let src_id: i64 = row.get("id");
        let parent_id: Option<i64> = row.get("parent_id");
        let src_name: String = row.get("name");
        let src_path: String = row.get("path");
        let src_depth: i64 = row.get("depth");
        let src_created_at: i64 = row.get("created_at");
        let parent_id =
            parent_id.ok_or(ApplyError::CannotMergeRoot)?;

        // 2. Compute before_sha from the row about to be deleted +
        //    its existing chunk_tags pairs. Locality-only — same
        //    convention the memory_op path follows.
        let before_sha = sha256_tag_state(
            &src_path, src_id, parent_id, &src_name, src_depth, src_created_at,
        );

        // 3. If the kb has the v6 `chunk_tags` table, capture the
        //    chunk_ids about to be rewritten and rewrite them. Older kb
        //    files without the table (defensive — same pattern as the
        //    evolution-side migration helper) fall through to step 4.
        let mut moved_chunk_ids: Vec<i64> = Vec::new();
        let has_chunk_tags = column_exists_sync(pool, "chunk_tags", "chunk_id")
            .await
            .map_err(|e| ApplyError::Kb(anyhow::Error::from(e)))?;
        if has_chunk_tags {
            let rows =
                sqlx::query("SELECT chunk_id FROM chunk_tags WHERE tag_node_id = ?1")
                    .bind(src_id)
                    .fetch_all(pool)
                    .await
                    .map_err(|e| ApplyError::Kb(anyhow::Error::from(e)))?;
            for r in &rows {
                moved_chunk_ids.push(r.get::<i64, _>("chunk_id"));
            }
            // INSERT OR IGNORE on UPDATE: the destination row
            // (chunk_id, parent_id) may already exist. SQLite UPDATE
            // doesn't have OR IGNORE, but the PRIMARY KEY collision
            // would error out — guard with a DELETE-of-conflicts
            // before the UPDATE so the rewrite is idempotent.
            sqlx::query(
                "DELETE FROM chunk_tags WHERE tag_node_id = ?1 \
                 AND chunk_id IN (SELECT chunk_id FROM chunk_tags WHERE tag_node_id = ?2)",
            )
            .bind(src_id)
            .bind(parent_id)
            .execute(pool)
            .await
            .map_err(|e| ApplyError::Kb(anyhow::Error::from(e)))?;
            sqlx::query("UPDATE chunk_tags SET tag_node_id = ?1 WHERE tag_node_id = ?2")
                .bind(parent_id)
                .bind(src_id)
                .execute(pool)
                .await
                .map_err(|e| ApplyError::Kb(anyhow::Error::from(e)))?;
        }

        // 4. Drop the source row. ON DELETE CASCADE on parent_id keeps
        //    descendants in lockstep — by design (operator merging a
        //    subtree means the whole subtree goes).
        let res = sqlx::query("DELETE FROM tag_nodes WHERE id = ?1")
            .bind(src_id)
            .execute(pool)
            .await
            .map_err(|e| ApplyError::Kb(anyhow::Error::from(e)))?;
        if res.rows_affected() == 0 {
            // Race with a concurrent revert — surface the same shape.
            return Err(ApplyError::TagNotFound(path.into()));
        }

        let after_sha = sha256_hex(format!("merged:{src_path}->{parent_id}").as_bytes());
        let inverse_diff = json!({
            "op": "merge_tag",
            "src": {
                "id": src_id,
                "parent_id": parent_id,
                "name": src_name,
                "path": src_path,
                "depth": src_depth,
                "created_at": src_created_at,
            },
            "moved_chunk_tag_ids": moved_chunk_ids,
        })
        .to_string();

        Ok(MutationOutcome {
            before_sha,
            after_sha,
            inverse_diff,
        })
    }

    /// Phase 3-2B: forward apply for `skill_update`. Target shape is
    /// `skills/<name>.md`; v0.3 only supports the `__APPEND__` hunk
    /// header the Step-1 EvolutionEngine emits. We snapshot the full
    /// prior file content into `inverse_diff` (skill files are tiny —
    /// single-digit KB) so revert is byte-for-byte deterministic.
    async fn apply_skill_update(
        &self,
        target: &str,
        diff: &str,
    ) -> Result<MutationOutcome, ApplyError> {
        // 1. Validate target shape. `skills/` prefix is required so a
        //    bug in the proposer can't trick the applier into writing
        //    arbitrary paths under skills_dir.
        if !target.starts_with("skills/") || !target.ends_with(".md") {
            return Err(ApplyError::InvalidTarget(target.into()));
        }
        let basename = &target["skills/".len()..];
        // Defence in depth: reject `..`, `/`, etc inside the basename
        // so resolution can't escape skills_dir.
        if basename.is_empty() || basename.contains('/') || basename.contains("..") {
            return Err(ApplyError::InvalidTarget(target.into()));
        }
        let path = self.skills_dir.join(basename);

        // 2. Read prior content. Missing file is a hard reject — the
        //    proposer must have observed the file before proposing.
        let meta = match tokio::fs::metadata(&path).await {
            Ok(m) => m,
            Err(_) => {
                return Err(ApplyError::SkillFileMissing(target.into()));
            }
        };
        if !meta.is_file() {
            return Err(ApplyError::SkillFileMissing(target.into()));
        }
        let prior_content = tokio::fs::read_to_string(&path)
            .await
            .map_err(|e| ApplyError::Kb(anyhow::Error::from(e)))?;

        // 3. Parse the diff. Step 1 only emits the __APPEND__ sentinel;
        //    we reject anything else so a future engine bug doesn't
        //    silently drop content via a partial unified-diff parse.
        let appended_lines = parse_append_diff(diff)?;
        let mut new_content = prior_content.clone();
        if !new_content.is_empty() && !new_content.ends_with('\n') {
            new_content.push('\n');
        }
        for line in &appended_lines {
            new_content.push_str(line);
            new_content.push('\n');
        }

        // 4. Atomic write: tmp + rename. A crash mid-write leaves the
        //    .tmp orphan but the live skill file untouched.
        let mut tmp = path.clone();
        let mut name = tmp
            .file_name()
            .map(|n| n.to_os_string())
            .unwrap_or_default();
        name.push(".tmp");
        tmp.set_file_name(name);
        tokio::fs::write(&tmp, new_content.as_bytes())
            .await
            .map_err(|e| ApplyError::Kb(anyhow::Error::from(e)))?;
        tokio::fs::rename(&tmp, &path)
            .await
            .map_err(|e| ApplyError::Kb(anyhow::Error::from(e)))?;

        let before_sha = sha256_hex(prior_content.as_bytes());
        let after_sha = sha256_hex(new_content.as_bytes());
        let inverse_diff = json!({
            "op": "skill_update",
            "file": target,
            "prior_content": prior_content,
            "applied_at_ms": now_ms(),
        })
        .to_string();

        Ok(MutationOutcome {
            before_sha,
            after_sha,
            inverse_diff,
        })
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

    /// Phase 3 W1-B: replay a proposal's `inverse_diff` against the kb,
    /// then stamp the rollback audit fields on the proposal + history
    /// rows. Returns the freshly-updated history row (with
    /// `rolled_back_at` / `rollback_reason` populated).
    ///
    /// Same two-DB partial-fail caveat as `apply`: the kb mutation and
    /// the evolution UPDATEs aren't in a shared transaction. Order is
    /// kb → history → proposal so the worst-case is "kb restored but
    /// audit silent" — operators detect via the diff between
    /// `evolution_proposals.status = 'applied'` rows and the actual kb
    /// state, same monitoring path the forward apply already uses.
    pub async fn revert(
        &self,
        id: &ProposalId,
        reason: &str,
    ) -> Result<EvolutionHistory, ApplyError> {
        // 1. Gate on `Applied`. `RolledBack` returns NotApplied so the
        //    monitor can tell idempotent re-fires apart from missing
        //    proposals.
        let proposal = match self.proposals.get(id).await {
            Ok(p) => p,
            Err(RepoError::NotFound(_)) => return Err(ApplyError::NotFound(id.0.clone())),
            Err(other) => return Err(ApplyError::Repo(other)),
        };
        if proposal.status != EvolutionStatus::Applied {
            return Err(ApplyError::NotApplied(
                proposal.status.as_str().to_string(),
            ));
        }

        // 2. Fetch the audit row's inverse_diff. Missing here is data
        //    corruption — forward apply must have written it.
        let history_row = match self.history.latest_for_proposal(id).await {
            Ok(h) => h,
            Err(RepoError::NotFound(_)) => {
                return Err(ApplyError::HistoryMissing(id.0.clone()));
            }
            Err(other) => return Err(ApplyError::Repo(other)),
        };

        // 3. Dispatch per kind. New kinds add a sibling line below.
        match proposal.kind {
            EvolutionKind::MemoryOp => self.revert_memory_op(&history_row).await?,
            EvolutionKind::TagRebalance => self.revert_tag_rebalance(&history_row).await?,
            EvolutionKind::SkillUpdate => self.revert_skill_update(&history_row).await?,
            other => return Err(ApplyError::UnsupportedRevertKind(other.as_str().to_string())),
        }

        // 4. Audit + status flip. Two writes against evolution.sqlite —
        //    not in a shared TX with the kb mutation; see method doc.
        let now = now_ms();
        self.history
            .mark_rolled_back(id, now, reason)
            .await
            .map_err(|e| ApplyError::History(anyhow::Error::from(e)))?;
        self.proposals
            .mark_auto_rolled_back(id, now, reason)
            .await
            .map_err(|e| ApplyError::Repo(e))?;

        EVOLUTION_PROPOSALS_ROLLED_BACK
            .with_label_values(&[proposal.kind.as_str()])
            .inc();

        let mut out = history_row;
        out.rolled_back_at = Some(now);
        out.rollback_reason = Some(reason.to_string());
        Ok(out)
    }

    /// Reverse handler for `memory_op`. Re-INSERT the chunk that the
    /// forward path deleted. `INSERT OR IGNORE` makes a partial
    /// double-revert safe: when an explicit `id` is bound (the
    /// merge_chunks shape carries `loser_id`), a PK collision with a
    /// re-inserted row becomes a no-op rather than a failure.
    ///
    /// `delete_chunk`'s forward inverse_diff doesn't carry the original
    /// chunk id, so its revert lets SQLite assign a fresh autoincrement
    /// id — content is restored, the (file_id, chunk_index, namespace)
    /// metadata is intact, and the proposal status flip below prevents
    /// the monitor from firing the same revert twice.
    async fn revert_memory_op(&self, history: &EvolutionHistory) -> Result<(), ApplyError> {
        let raw: serde_json::Value = serde_json::from_str(&history.inverse_diff)
            .map_err(|e| ApplyError::MalformedInverseDiff(format!("parse: {e}")))?;
        let action = raw
            .get("action")
            .and_then(|v| v.as_str())
            .ok_or_else(|| ApplyError::MalformedInverseDiff("missing 'action'".into()))?;
        match action {
            "restore_chunk" => {
                // Forward path emits two shapes: `merge_chunks` keys
                // are prefixed `loser_*` (winner stays put);
                // `delete_chunk` uses bare `content/file_id/...`.
                // Discriminate on `loser_id`.
                let plan = ChunkRestore::parse(&raw).map_err(ApplyError::MalformedInverseDiff)?;
                plan.execute(self.kb_store.pool())
                    .await
                    .map_err(|e| ApplyError::Kb(anyhow::Error::from(e)))?;
            }
            "demote_chunk" => {
                let chunk_id =
                    pick_i64(&raw, "chunk_id").map_err(ApplyError::MalformedInverseDiff)?;
                let prior_ns =
                    pick_str(&raw, "prior_namespace").map_err(ApplyError::MalformedInverseDiff)?;
                let prior_decay = raw
                    .get("prior_decay_score")
                    .and_then(|v| v.as_f64())
                    .ok_or_else(|| {
                        ApplyError::MalformedInverseDiff("missing 'prior_decay_score'".into())
                    })? as f32;
                self.kb_store
                    .demote_from_consolidated(chunk_id, &prior_ns, prior_decay)
                    .await
                    .map_err(ApplyError::Kb)?;
            }
            other => {
                return Err(ApplyError::MalformedInverseDiff(format!(
                    "unknown action: {other}"
                )));
            }
        }
        Ok(())
    }

    /// Phase 3-2B: reverse handler for `tag_rebalance`. Re-insert the
    /// dropped `tag_nodes` row at its original id (`INSERT OR IGNORE`
    /// makes a partial double-revert idempotent), then point the
    /// captured `chunk_tags.chunk_id` rows back at it. Mirrors the
    /// memory_op contract — INSERT OR IGNORE everywhere a PK could
    /// collide so a re-run after a partial succeeds.
    async fn revert_tag_rebalance(
        &self,
        history: &EvolutionHistory,
    ) -> Result<(), ApplyError> {
        let raw: serde_json::Value = serde_json::from_str(&history.inverse_diff)
            .map_err(|e| ApplyError::MalformedInverseDiff(format!("parse: {e}")))?;
        let op = raw
            .get("op")
            .and_then(|v| v.as_str())
            .ok_or_else(|| ApplyError::MalformedInverseDiff("missing 'op'".into()))?;
        if op != "merge_tag" {
            return Err(ApplyError::MalformedInverseDiff(format!(
                "unknown op: {op}"
            )));
        }
        let src = raw
            .get("src")
            .ok_or_else(|| ApplyError::MalformedInverseDiff("missing 'src'".into()))?;
        let id = pick_i64(src, "id").map_err(ApplyError::MalformedInverseDiff)?;
        let parent_id =
            pick_i64(src, "parent_id").map_err(ApplyError::MalformedInverseDiff)?;
        let name = pick_str(src, "name").map_err(ApplyError::MalformedInverseDiff)?;
        let path = pick_str(src, "path").map_err(ApplyError::MalformedInverseDiff)?;
        let depth = pick_i64(src, "depth").map_err(ApplyError::MalformedInverseDiff)?;
        let created_at =
            pick_i64(src, "created_at").map_err(ApplyError::MalformedInverseDiff)?;

        let moved_ids: Vec<i64> = raw
            .get("moved_chunk_tag_ids")
            .and_then(|v| v.as_array())
            .map(|arr| arr.iter().filter_map(|v| v.as_i64()).collect())
            .unwrap_or_default();

        let pool = self.kb_store.pool();

        // Re-insert the node. INSERT OR IGNORE: a partial double-revert
        // (history rolled_back but proposal still applied → monitor
        // re-fires) hits the unique `path` index and no-ops cleanly.
        sqlx::query(
            "INSERT OR IGNORE INTO tag_nodes(id, parent_id, name, path, depth, created_at) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
        )
        .bind(id)
        .bind(parent_id)
        .bind(&name)
        .bind(&path)
        .bind(depth)
        .bind(created_at)
        .execute(pool)
        .await
        .map_err(|e| ApplyError::Kb(anyhow::Error::from(e)))?;

        // Point the captured chunk_tags rows back. Same defensive
        // schema check the forward path uses — older kb files without
        // the table just skip the rewrite.
        let has_chunk_tags = column_exists_sync(pool, "chunk_tags", "chunk_id")
            .await
            .map_err(|e| ApplyError::Kb(anyhow::Error::from(e)))?;
        if has_chunk_tags && !moved_ids.is_empty() {
            // Same idempotency dance as the forward path: clear any
            // pre-existing (chunk_id, src_id) rows so the UPDATE can't
            // hit the composite-PK uniqueness constraint.
            for chunk_id in &moved_ids {
                sqlx::query(
                    "DELETE FROM chunk_tags WHERE chunk_id = ?1 AND tag_node_id = ?2",
                )
                .bind(chunk_id)
                .bind(id)
                .execute(pool)
                .await
                .map_err(|e| ApplyError::Kb(anyhow::Error::from(e)))?;
                sqlx::query(
                    "UPDATE chunk_tags SET tag_node_id = ?1 \
                     WHERE chunk_id = ?2 AND tag_node_id = ?3",
                )
                .bind(id)
                .bind(chunk_id)
                .bind(parent_id)
                .execute(pool)
                .await
                .map_err(|e| ApplyError::Kb(anyhow::Error::from(e)))?;
            }
        }
        Ok(())
    }

    /// Phase 3-2B: reverse handler for `skill_update`. Write the
    /// captured `prior_content` back to disk via the same atomic
    /// tmp+rename dance the forward path uses. Skill files are small
    /// enough that a full snapshot is the simplest correct path.
    async fn revert_skill_update(
        &self,
        history: &EvolutionHistory,
    ) -> Result<(), ApplyError> {
        let raw: serde_json::Value = serde_json::from_str(&history.inverse_diff)
            .map_err(|e| ApplyError::MalformedInverseDiff(format!("parse: {e}")))?;
        let op = raw
            .get("op")
            .and_then(|v| v.as_str())
            .ok_or_else(|| ApplyError::MalformedInverseDiff("missing 'op'".into()))?;
        if op != "skill_update" {
            return Err(ApplyError::MalformedInverseDiff(format!(
                "unknown op: {op}"
            )));
        }
        let file = pick_str(&raw, "file").map_err(ApplyError::MalformedInverseDiff)?;
        let prior_content =
            pick_str(&raw, "prior_content").map_err(ApplyError::MalformedInverseDiff)?;

        // Re-validate the file path the same way the forward path does
        // — a corrupted inverse_diff shouldn't be a write-anywhere
        // primitive.
        if !file.starts_with("skills/") || !file.ends_with(".md") {
            return Err(ApplyError::MalformedInverseDiff(format!(
                "bad file path: {file}"
            )));
        }
        let basename = &file["skills/".len()..];
        if basename.is_empty() || basename.contains('/') || basename.contains("..") {
            return Err(ApplyError::MalformedInverseDiff(format!(
                "bad file basename: {file}"
            )));
        }
        let path = self.skills_dir.join(basename);

        let mut tmp = path.clone();
        let mut name = tmp
            .file_name()
            .map(|n| n.to_os_string())
            .unwrap_or_default();
        name.push(".tmp");
        tmp.set_file_name(name);
        tokio::fs::write(&tmp, prior_content.as_bytes())
            .await
            .map_err(|e| ApplyError::Kb(anyhow::Error::from(e)))?;
        tokio::fs::rename(&tmp, &path)
            .await
            .map_err(|e| ApplyError::Kb(anyhow::Error::from(e)))?;
        Ok(())
    }
}

/// Internal helper: the fields needed to re-INSERT a chunk row. Two
/// shapes — merge_chunks carries the original chunk id (so the revert
/// can pin idempotency on the PK), delete_chunk doesn't (autoincrement
/// fresh).
enum ChunkRestore {
    /// merge_chunks revert: explicit id from `loser_id`.
    WithId {
        id: i64,
        file_id: i64,
        chunk_index: i64,
        content: String,
        namespace: String,
    },
    /// delete_chunk revert: id auto-assigned.
    WithoutId {
        file_id: i64,
        chunk_index: i64,
        content: String,
        namespace: String,
    },
}

impl ChunkRestore {
    fn parse(v: &serde_json::Value) -> Result<Self, String> {
        if v.get("loser_id").is_some() {
            Ok(Self::WithId {
                id: pick_i64(v, "loser_id")?,
                file_id: pick_i64(v, "loser_file_id")?,
                chunk_index: pick_i64(v, "loser_chunk_index")?,
                content: pick_str(v, "loser_content")?,
                namespace: pick_str(v, "loser_namespace")?,
            })
        } else {
            Ok(Self::WithoutId {
                file_id: pick_i64(v, "file_id")?,
                chunk_index: pick_i64(v, "chunk_index")?,
                content: pick_str(v, "content")?,
                namespace: pick_str(v, "namespace")?,
            })
        }
    }

    async fn execute(&self, pool: &sqlx::SqlitePool) -> Result<(), sqlx::Error> {
        match self {
            // INSERT OR IGNORE: PK collision on re-insert is a no-op so
            // a partial double-revert (history rolled_back but proposal
            // still applied, monitor re-fires) lands cleanly.
            Self::WithId {
                id,
                file_id,
                chunk_index,
                content,
                namespace,
            } => {
                sqlx::query(
                    "INSERT OR IGNORE INTO chunks(id, file_id, chunk_index, content, vector, namespace) \
                     VALUES (?1, ?2, ?3, ?4, NULL, ?5)",
                )
                .bind(id)
                .bind(file_id)
                .bind(chunk_index)
                .bind(content)
                .bind(namespace)
                .execute(pool)
                .await?;
            }
            // No PK to collide on — idempotency comes from the proposal
            // status flip in the caller. Still INSERT OR IGNORE for
            // shape consistency with the merge path.
            Self::WithoutId {
                file_id,
                chunk_index,
                content,
                namespace,
            } => {
                sqlx::query(
                    "INSERT OR IGNORE INTO chunks(file_id, chunk_index, content, vector, namespace) \
                     VALUES (?1, ?2, ?3, NULL, ?4)",
                )
                .bind(file_id)
                .bind(chunk_index)
                .bind(content)
                .bind(namespace)
                .execute(pool)
                .await?;
            }
        }
        Ok(())
    }
}

fn pick_str(v: &serde_json::Value, key: &str) -> Result<String, String> {
    v.get(key)
        .and_then(|x| x.as_str())
        .map(|s| s.to_string())
        .ok_or_else(|| format!("missing '{key}'"))
}

fn pick_i64(v: &serde_json::Value, key: &str) -> Result<i64, String> {
    v.get(key)
        .and_then(|x| x.as_i64())
        .ok_or_else(|| format!("missing '{key}'"))
}

/// Adapter so the AutoRollback monitor can hold an
/// `Arc<dyn AutoRollbackApplier>` without dragging the gateway crate
/// into `corlinman-auto-rollback`. Maps the rich `ApplyError` set into
/// the leaner `RevertError` the monitor cares about.
#[async_trait]
impl AutoRollbackApplier for EvolutionApplier {
    async fn revert(&self, id: &ProposalId, reason: &str) -> Result<(), RevertError> {
        match EvolutionApplier::revert(self, id, reason).await {
            Ok(_) => Ok(()),
            Err(ApplyError::NotFound(s)) => Err(RevertError::NotFound(s)),
            Err(ApplyError::NotApplied(s)) => Err(RevertError::NotApplied(s)),
            Err(ApplyError::HistoryMissing(s)) => Err(RevertError::HistoryMissing(s)),
            Err(ApplyError::UnsupportedRevertKind(s)) => Err(RevertError::UnsupportedKind(s)),
            // Everything else (Kb, History, MalformedInverseDiff, Repo,
            // ...) collapses to Internal — operator inspects gateway
            // logs for the underlying cause.
            Err(other) => Err(RevertError::Internal(format!("{other}"))),
        }
    }
}

/// Internal representation of a parsed `memory_op` target.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum MemoryOp {
    /// `merge_chunks:<a>,<b>` — kept the smaller id, drop the larger.
    MergeChunks { winner: i64, loser: i64 },
    /// `delete_chunk:<id>` — drop one chunk.
    DeleteChunk { id: i64 },
    /// Phase 3 W3-A: `consolidate_chunk:<id>` — flip the chunk's
    /// namespace to `consolidated`, stamp `consolidated_at`, and freeze
    /// `decay_score = 1.0` so the read-time decay multiplier collapses
    /// to 1.0 forever. Filed by the consolidation job; revert restores
    /// the prior namespace + decay_score from `inverse_diff`.
    ConsolidateChunk { id: i64 },
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
        } else if let Some(rest) = target.strip_prefix("consolidate_chunk:") {
            let id: i64 = rest
                .trim()
                .parse()
                .map_err(|_| ApplyError::InvalidTarget(target.into()))?;
            Ok(Self::ConsolidateChunk { id })
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

/// SHA-256 over a stable string-concatenation of a `tag_nodes` row's
/// columns. Used as `before_sha` for `tag_rebalance` apply — locality
/// only, mirrors the memory_op convention.
fn sha256_tag_state(
    path: &str,
    id: i64,
    parent_id: i64,
    name: &str,
    depth: i64,
    created_at: i64,
) -> String {
    let mut h = Sha256::new();
    h.update(path.as_bytes());
    h.update([0u8]);
    h.update(id.to_le_bytes());
    h.update(parent_id.to_le_bytes());
    h.update(name.as_bytes());
    h.update([0u8]);
    h.update(depth.to_le_bytes());
    h.update(created_at.to_le_bytes());
    format!("{:x}", h.finalize())
}

/// Phase 3-2B: defensive `pragma_table_info` lookup against `kb.sqlite`.
/// Mirrors `corlinman-evolution::store::column_exists` — same query
/// shape, just on a different pool. Lives here (not in the vector
/// crate) because the applier's the only caller and pulling it across
/// crates would widen the public API.
async fn column_exists_sync(
    pool: &sqlx::SqlitePool,
    table: &'static str,
    column: &'static str,
) -> Result<bool, sqlx::Error> {
    // pragma_table_info doesn't bind table names, format it in. Both
    // table + column are 'static so no injection surface.
    let sql = format!(
        "SELECT 1 FROM pragma_table_info('{}') WHERE name = ?",
        table.replace('\'', "''")
    );
    let row = sqlx::query(&sql).bind(column).fetch_optional(pool).await?;
    Ok(row.is_some())
}

/// Phase 3-2B: parse the `__APPEND__`-shaped diff the Step-1
/// EvolutionEngine emits for `skill_update`. Returns the appended
/// lines (without the leading `+`). v0.3 only ships this one shape —
/// arbitrary unified diffs would need a real patch engine.
fn parse_append_diff(diff: &str) -> Result<Vec<String>, ApplyError> {
    let mut lines = diff.lines();
    // Skip the `--- a/...` and `+++ b/...` headers if present. The
    // proposer emits them but they're informational — the hunk header
    // is what we gate on.
    let mut found_hunk = false;
    let mut appended: Vec<String> = Vec::new();
    while let Some(line) = lines.next() {
        if line.starts_with("--- ") || line.starts_with("+++ ") {
            continue;
        }
        if line.starts_with("@@") {
            // Sentinel: hunk header must mention __APPEND__. Anything
            // else is a real unified diff we can't apply yet.
            if !line.contains("__APPEND__") {
                return Err(ApplyError::UnsupportedDiffShape(line.to_string()));
            }
            found_hunk = true;
            // Remaining lines are the body.
            for body in lines.by_ref() {
                if let Some(stripped) = body.strip_prefix('+') {
                    appended.push(stripped.to_string());
                } else if body.is_empty() {
                    // Blank trailing line — tolerate.
                    continue;
                } else {
                    return Err(ApplyError::UnsupportedDiffShape(body.to_string()));
                }
            }
            break;
        }
        // A non-blank, non-header line before any hunk = malformed.
        if !line.is_empty() {
            return Err(ApplyError::UnsupportedDiffShape(line.to_string()));
        }
    }
    if !found_hunk {
        return Err(ApplyError::UnsupportedDiffShape("no hunk header".into()));
    }
    Ok(appended)
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
        let skills_dir = tmp.path().join("skills");
        // Skills tests want the dir to exist. Memory-op tests don't
        // touch it, so create_dir_all here keeps both paths happy.
        std::fs::create_dir_all(&skills_dir).unwrap();
        let kb = Arc::new(SqliteStore::open(&kb_path).await.unwrap());
        let evol = Arc::new(EvolutionStore::open(&evol_path).await.unwrap());
        let applier = EvolutionApplier::new(
            evol.clone(),
            kb.clone(),
            AutoRollbackThresholds::default(),
            skills_dir,
        );
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
            eval_run_id: None,
            baseline_metrics_json: None,
            auto_rollback_at: None,
            auto_rollback_reason: None,
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
            eval_run_id: None,
            baseline_metrics_json: None,
            auto_rollback_at: None,
            auto_rollback_reason: None,
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
    async fn apply_rejects_unsupported_kind() {
        let (_tmp, applier, _kb, evol) = fresh_applier().await;
        // Phase 3-2B: tag_rebalance + skill_update are now supported,
        // so this test exercises a kind that still has no handler. Pick
        // `retry_tuning` — it's the next kind on the roadmap and the
        // unsupported-kind branch needs at least one regression pin.
        let pid = ProposalId::new("evol-rt-001");
        let repo = ProposalsRepo::new(evol.pool().clone());
        repo.insert(&EvolutionProposal {
            id: pid.clone(),
            kind: EvolutionKind::RetryTuning,
            target: "retry_policy:foo".into(),
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
            eval_run_id: None,
            baseline_metrics_json: None,
            auto_rollback_at: None,
            auto_rollback_reason: None,
        })
        .await
        .unwrap();
        match applier.apply(&pid).await {
            Err(ApplyError::UnsupportedKind(s)) => assert_eq!(s, "retry_tuning"),
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

    /// W1-B Step 2: applying a memory_op proposal must populate
    /// `metrics_baseline` with the snapshot of recent regression
    /// signals on the proposal's target. Seeds two `tool.call.failed`
    /// rows on the target before applying and asserts the baseline
    /// JSON carries a non-zero count.
    #[tokio::test]
    async fn apply_populates_metrics_baseline_from_signals() {
        let (_tmp, applier, kb, evol) = fresh_applier().await;
        let id = seed_chunk(&kb, "/m", "metric content").await;
        let target = format!("delete_chunk:{id}");

        // Seed signals "now-ish" so they fall inside the default
        // 1800s window. Use the same now() the applier will use —
        // good enough for the assert.
        let now = now_ms();
        for _ in 0..3 {
            sqlx::query(
                r#"INSERT INTO evolution_signals
                     (event_kind, target, severity, payload_json, observed_at)
                   VALUES ('tool.call.failed', ?, 'error', '{}', ?)"#,
            )
            .bind(&target)
            .bind(now - 1_000)
            .execute(evol.pool())
            .await
            .unwrap();
        }

        let pid = seed_approved(&evol, "evol-baseline-001", &target).await;
        let history = applier.apply(&pid).await.unwrap();

        // metrics_baseline shape comes from MetricSnapshot — check we
        // serialised the counts and that the seeded signals show up.
        let baseline = &history.metrics_baseline;
        assert_eq!(baseline["target"], target);
        assert_eq!(baseline["counts"]["tool.call.failed"], 3);
        // search.recall.dropped is in the whitelist but unseeded → 0.
        assert_eq!(baseline["counts"]["search.recall.dropped"], 0);
    }

    /// W1-B Step 3: a forward `merge_chunks` apply followed by a revert
    /// must restore the deleted loser chunk and stamp the proposal +
    /// history rollback fields. Pins both the kb-side restore and the
    /// evolution.sqlite audit trail in one shot.
    #[tokio::test]
    async fn revert_memory_op_restores_deleted_chunk() {
        let (_tmp, applier, kb, evol) = fresh_applier().await;
        let a = seed_chunk(&kb, "/a", "winner content").await;
        let b = seed_chunk(&kb, "/b", "loser content").await;
        let target = format!("merge_chunks:{a},{b}");
        let pid = seed_approved(&evol, "evol-revert-001", &target).await;

        // Forward apply removes the loser.
        applier.apply(&pid).await.unwrap();
        let rows = kb.query_chunks_by_ids(&[a, b]).await.unwrap();
        assert_eq!(rows.len(), 1, "loser deleted by forward apply");

        // Revert restores it (id-stable: same loser_id comes back).
        let reverted = applier
            .revert(&pid, "metrics regression: +200% err signals")
            .await
            .unwrap();
        assert!(reverted.rolled_back_at.is_some());
        assert_eq!(
            reverted.rollback_reason.as_deref(),
            Some("metrics regression: +200% err signals")
        );

        // Loser chunk back at its original id.
        let rows = kb.query_chunks_by_ids(&[a, b]).await.unwrap();
        let ids: Vec<i64> = rows.iter().map(|r| r.id).collect();
        assert_eq!(ids, vec![a, b], "loser restored at original id");
        let loser = rows.iter().find(|r| r.id == b).unwrap();
        assert_eq!(loser.content, "loser content");
        assert_eq!(loser.namespace, "general");

        // Proposal flipped applied → rolled_back with audit columns set.
        let after = ProposalsRepo::new(evol.pool().clone()).get(&pid).await.unwrap();
        assert_eq!(after.status, EvolutionStatus::RolledBack);
        let row: (Option<i64>, Option<String>) = sqlx::query_as(
            "SELECT auto_rollback_at, auto_rollback_reason FROM evolution_proposals WHERE id = ?",
        )
        .bind(pid.as_str())
        .fetch_one(evol.pool())
        .await
        .unwrap();
        assert!(row.0.is_some());
        assert_eq!(
            row.1.as_deref(),
            Some("metrics regression: +200% err signals")
        );
    }

    /// Calling revert on an already-rolled-back proposal must surface
    /// `NotApplied` (status is `RolledBack`, not `Applied`). Pins the
    /// idempotency contract the monitor relies on.
    #[tokio::test]
    async fn revert_idempotent() {
        let (_tmp, applier, kb, evol) = fresh_applier().await;
        let a = seed_chunk(&kb, "/a", "winner").await;
        let b = seed_chunk(&kb, "/b", "loser").await;
        let pid = seed_approved(&evol, "evol-revert-002", &format!("merge_chunks:{a},{b}")).await;
        applier.apply(&pid).await.unwrap();
        applier.revert(&pid, "first").await.unwrap();

        match applier.revert(&pid, "second").await {
            Err(ApplyError::NotApplied(s)) => assert_eq!(s, "rolled_back"),
            other => panic!("expected NotApplied, got {other:?}"),
        }
    }

    /// Reverting a kind that has no inverse handler must short-circuit
    /// with `UnsupportedRevertKind`. Phase 3-2B activated `tag_rebalance`
    /// + `skill_update`, so this test moves to `retry_tuning` — the
    /// next kind without a handler. Hand-seed the rows because the
    /// forward path refuses unsupported kinds, so the revert path
    /// needs to be tested independently.
    #[tokio::test]
    async fn revert_unsupported_kind() {
        let (_tmp, applier, _kb, evol) = fresh_applier().await;
        let pid = ProposalId::new("evol-revert-rt-001");
        let proposals = ProposalsRepo::new(evol.pool().clone());
        proposals
            .insert(&EvolutionProposal {
                id: pid.clone(),
                kind: EvolutionKind::RetryTuning,
                target: "retry_policy:foo".into(),
                diff: String::new(),
                reasoning: String::new(),
                risk: EvolutionRisk::Low,
                budget_cost: 0,
                status: EvolutionStatus::Applied,
                shadow_metrics: None,
                signal_ids: vec![],
                trace_ids: vec![],
                created_at: 1_000,
                decided_at: Some(2_000),
                decided_by: Some("op".into()),
                applied_at: Some(3_000),
                rollback_of: None,
                eval_run_id: None,
                baseline_metrics_json: None,
                auto_rollback_at: None,
                auto_rollback_reason: None,
            })
            .await
            .unwrap();
        // History row: required so the gate after kind-check would
        // theoretically have one to consume — but UnsupportedRevertKind
        // fires before the kb mutation, so we get there only via the
        // status check first. Insert anyway so a future reorder of the
        // checks doesn't masquerade as a regression.
        let history = HistoryRepo::new(evol.pool().clone());
        history
            .insert(&EvolutionHistory {
                id: None,
                proposal_id: pid.clone(),
                kind: EvolutionKind::RetryTuning,
                target: "retry_policy:foo".into(),
                before_sha: "x".into(),
                after_sha: "y".into(),
                inverse_diff: "{}".into(),
                metrics_baseline: serde_json::json!({}),
                applied_at: 3_000,
                rolled_back_at: None,
                rollback_reason: None,
            })
            .await
            .unwrap();

        match applier.revert(&pid, "won't take").await {
            Err(ApplyError::UnsupportedRevertKind(s)) => assert_eq!(s, "retry_tuning"),
            other => panic!("expected UnsupportedRevertKind, got {other:?}"),
        }
        // Still applied — no audit fields written.
        let after = proposals.get(&pid).await.unwrap();
        assert_eq!(after.status, EvolutionStatus::Applied);
    }

    // -----------------------------------------------------------------
    // Phase 3-2B: tag_rebalance + skill_update
    // -----------------------------------------------------------------

    /// Insert a tag node row directly. Returns the tag id. Used to
    /// hand-build the test tag tree without going through ensure_tag_path
    /// (we want explicit control over depth + parent_id assertions).
    async fn seed_tag_node(
        kb: &SqliteStore,
        parent_id: Option<i64>,
        name: &str,
        path: &str,
        depth: i32,
    ) -> i64 {
        let row = sqlx::query(
            "INSERT INTO tag_nodes(parent_id, name, path, depth) \
             VALUES (?1, ?2, ?3, ?4) RETURNING id",
        )
        .bind(parent_id)
        .bind(name)
        .bind(path)
        .bind(depth)
        .fetch_one(kb.pool())
        .await
        .unwrap();
        row.get::<i64, _>("id")
    }

    /// Insert a chunk_tags pair so tag_rebalance has rows to reparent.
    async fn link_chunk_tag(kb: &SqliteStore, chunk_id: i64, tag_node_id: i64) {
        sqlx::query("INSERT INTO chunk_tags(chunk_id, tag_node_id) VALUES (?1, ?2)")
            .bind(chunk_id)
            .bind(tag_node_id)
            .execute(kb.pool())
            .await
            .unwrap();
    }

    /// Insert an Approved proposal of `kind` aimed at `target`. The
    /// existing `seed_approved` helper is locked to MemoryOp; this
    /// generic sibling is needed for the new kinds.
    async fn seed_approved_kind(
        evol: &EvolutionStore,
        id: &str,
        kind: EvolutionKind,
        target: &str,
        diff: &str,
    ) -> ProposalId {
        let pid = ProposalId::new(id);
        let repo = ProposalsRepo::new(evol.pool().clone());
        repo.insert(&EvolutionProposal {
            id: pid.clone(),
            kind,
            target: target.into(),
            diff: diff.into(),
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
            eval_run_id: None,
            baseline_metrics_json: None,
            auto_rollback_at: None,
            auto_rollback_reason: None,
        })
        .await
        .unwrap();
        pid
    }

    #[tokio::test]
    async fn apply_tag_rebalance_merges_node() {
        let (_tmp, applier, kb, evol) = fresh_applier().await;
        // Build a 3-node tree: root → coding → python.
        let root = seed_tag_node(&kb, None, "root", "root", 0).await;
        let coding = seed_tag_node(&kb, Some(root), "coding", "coding", 1).await;
        let python = seed_tag_node(&kb, Some(coding), "python", "coding/python", 2).await;
        // Two chunks tagged with python.
        let c1 = seed_chunk(&kb, "/c1", "doc one").await;
        let c2 = seed_chunk(&kb, "/c2", "doc two").await;
        link_chunk_tag(&kb, c1, python).await;
        link_chunk_tag(&kb, c2, python).await;

        let pid = seed_approved_kind(
            &evol,
            "evol-tag-merge-001",
            EvolutionKind::TagRebalance,
            "merge_tag:coding/python",
            "",
        )
        .await;
        let history = applier.apply(&pid).await.unwrap();
        assert_eq!(history.kind, EvolutionKind::TagRebalance);

        // python row gone.
        let count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM tag_nodes WHERE id = ?")
            .bind(python)
            .fetch_one(kb.pool())
            .await
            .unwrap();
        assert_eq!(count, 0, "python node deleted");

        // chunk_tags re-pointed to coding.
        let parent_links: Vec<(i64, i64)> = sqlx::query_as(
            "SELECT chunk_id, tag_node_id FROM chunk_tags ORDER BY chunk_id ASC",
        )
        .fetch_all(kb.pool())
        .await
        .unwrap();
        assert_eq!(parent_links, vec![(c1, coding), (c2, coding)]);

        // inverse_diff captures the deleted node + moved chunk_ids.
        let inv: serde_json::Value = serde_json::from_str(&history.inverse_diff).unwrap();
        assert_eq!(inv["op"], "merge_tag");
        assert_eq!(inv["src"]["id"], python);
        assert_eq!(inv["src"]["parent_id"], coding);
        assert_eq!(inv["src"]["path"], "coding/python");
        assert_eq!(inv["src"]["depth"], 2);
        let mut moved: Vec<i64> = inv["moved_chunk_tag_ids"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_i64().unwrap())
            .collect();
        moved.sort();
        assert_eq!(moved, vec![c1, c2]);
    }

    #[tokio::test]
    async fn revert_tag_rebalance_restores_node() {
        let (_tmp, applier, kb, evol) = fresh_applier().await;
        let root = seed_tag_node(&kb, None, "root", "root", 0).await;
        let coding = seed_tag_node(&kb, Some(root), "coding", "coding", 1).await;
        let python = seed_tag_node(&kb, Some(coding), "python", "coding/python", 2).await;
        let c1 = seed_chunk(&kb, "/c1", "doc").await;
        link_chunk_tag(&kb, c1, python).await;

        let pid = seed_approved_kind(
            &evol,
            "evol-tag-revert-001",
            EvolutionKind::TagRebalance,
            "merge_tag:coding/python",
            "",
        )
        .await;
        applier.apply(&pid).await.unwrap();
        let reverted = applier
            .revert(&pid, "metrics regression")
            .await
            .unwrap();
        assert!(reverted.rolled_back_at.is_some());

        // python row back at original id.
        let row: (i64, Option<i64>, String, String, i64) = sqlx::query_as(
            "SELECT id, parent_id, name, path, depth FROM tag_nodes WHERE id = ?",
        )
        .bind(python)
        .fetch_one(kb.pool())
        .await
        .unwrap();
        assert_eq!(row, (python, Some(coding), "python".into(), "coding/python".into(), 2));

        // chunk_tags re-pointed to python.
        let link: (i64, i64) = sqlx::query_as(
            "SELECT chunk_id, tag_node_id FROM chunk_tags WHERE chunk_id = ?",
        )
        .bind(c1)
        .fetch_one(kb.pool())
        .await
        .unwrap();
        assert_eq!(link, (c1, python));
    }

    #[tokio::test]
    async fn apply_tag_rebalance_rejects_root() {
        let (_tmp, applier, kb, evol) = fresh_applier().await;
        // Root: parent_id IS NULL — explicitly forbidden.
        let _root = seed_tag_node(&kb, None, "root", "root", 0).await;
        let pid = seed_approved_kind(
            &evol,
            "evol-tag-root-001",
            EvolutionKind::TagRebalance,
            "merge_tag:root",
            "",
        )
        .await;
        match applier.apply(&pid).await {
            Err(ApplyError::CannotMergeRoot) => {}
            other => panic!("expected CannotMergeRoot, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn apply_skill_update_appends_lines() {
        let (tmp, applier, _kb, evol) = fresh_applier().await;
        let path = tmp.path().join("skills").join("web_search.md");
        std::fs::write(&path, "# Web Search\n\nOriginal body.\n").unwrap();
        let diff = "--- a/skills/web_search.md\n\
                    +++ b/skills/web_search.md\n\
                    @@ __APPEND__,0 +__APPEND__,2 @@\n\
                    +## New section\n\
                    +Line two\n";
        let pid = seed_approved_kind(
            &evol,
            "evol-skill-001",
            EvolutionKind::SkillUpdate,
            "skills/web_search.md",
            diff,
        )
        .await;
        let history = applier.apply(&pid).await.unwrap();
        assert_eq!(history.kind, EvolutionKind::SkillUpdate);

        let after = std::fs::read_to_string(&path).unwrap();
        assert_eq!(
            after,
            "# Web Search\n\nOriginal body.\n## New section\nLine two\n"
        );

        let inv: serde_json::Value = serde_json::from_str(&history.inverse_diff).unwrap();
        assert_eq!(inv["op"], "skill_update");
        assert_eq!(inv["file"], "skills/web_search.md");
        assert_eq!(inv["prior_content"], "# Web Search\n\nOriginal body.\n");
    }

    #[tokio::test]
    async fn revert_skill_update_restores_content() {
        let (tmp, applier, _kb, evol) = fresh_applier().await;
        let path = tmp.path().join("skills").join("web_search.md");
        let original = "# Web Search\n\nOriginal body.\n";
        std::fs::write(&path, original).unwrap();
        let diff = "--- a/skills/web_search.md\n\
                    +++ b/skills/web_search.md\n\
                    @@ __APPEND__,0 +__APPEND__,1 @@\n\
                    +Appended line\n";
        let pid = seed_approved_kind(
            &evol,
            "evol-skill-revert-001",
            EvolutionKind::SkillUpdate,
            "skills/web_search.md",
            diff,
        )
        .await;
        applier.apply(&pid).await.unwrap();
        // Sanity: file changed.
        assert_ne!(std::fs::read_to_string(&path).unwrap(), original);

        applier.revert(&pid, "regression").await.unwrap();
        let after = std::fs::read_to_string(&path).unwrap();
        assert_eq!(after, original, "revert restores byte-for-byte");
    }

    #[tokio::test]
    async fn apply_skill_update_rejects_unknown_diff_shape() {
        let (tmp, applier, _kb, evol) = fresh_applier().await;
        let path = tmp.path().join("skills").join("web_search.md");
        std::fs::write(&path, "body\n").unwrap();
        // Real unified-diff hunk header — v0.3 won't apply this.
        let diff = "--- a/skills/web_search.md\n\
                    +++ b/skills/web_search.md\n\
                    @@ -1,3 +1,4 @@\n\
                    +new line\n";
        let pid = seed_approved_kind(
            &evol,
            "evol-skill-bad-001",
            EvolutionKind::SkillUpdate,
            "skills/web_search.md",
            diff,
        )
        .await;
        match applier.apply(&pid).await {
            Err(ApplyError::UnsupportedDiffShape(_)) => {}
            other => panic!("expected UnsupportedDiffShape, got {other:?}"),
        }
        // File untouched.
        assert_eq!(std::fs::read_to_string(&path).unwrap(), "body\n");
    }

    #[tokio::test]
    async fn apply_skill_update_rejects_missing_file() {
        let (_tmp, applier, _kb, evol) = fresh_applier().await;
        let diff = "--- a/skills/missing.md\n\
                    +++ b/skills/missing.md\n\
                    @@ __APPEND__,0 +__APPEND__,1 @@\n\
                    +x\n";
        let pid = seed_approved_kind(
            &evol,
            "evol-skill-missing-001",
            EvolutionKind::SkillUpdate,
            "skills/does_not_exist.md",
            diff,
        )
        .await;
        match applier.apply(&pid).await {
            Err(ApplyError::SkillFileMissing(s)) => {
                assert_eq!(s, "skills/does_not_exist.md");
            }
            other => panic!("expected SkillFileMissing, got {other:?}"),
        }
    }

    // ---- Phase 3 W3-A: consolidate_chunk -----------------------------------

    #[test]
    fn parse_consolidate_chunk_round_trip() {
        let plan = MemoryOp::parse("consolidate_chunk:42").unwrap();
        assert_eq!(plan, MemoryOp::ConsolidateChunk { id: 42 });
    }

    #[test]
    fn parse_consolidate_chunk_rejects_garbage() {
        for bad in [
            "consolidate_chunk:",
            "consolidate_chunk:abc",
            "consolidate_chunk: 1 2",
        ] {
            assert!(
                matches!(MemoryOp::parse(bad), Err(ApplyError::InvalidTarget(_))),
                "expected InvalidTarget for {bad:?}"
            );
        }
    }

    /// Phase 3 W3-A: a `consolidate_chunk:<id>` proposal flips the
    /// chunk's namespace to `consolidated`, stamps `consolidated_at`,
    /// and resets `decay_score` to 1.0. The `inverse_diff` records
    /// the prior namespace + decay_score so revert can restore them.
    #[tokio::test]
    async fn apply_consolidate_chunk_promotes_and_records_inverse_diff() {
        let (_tmp, applier, kb, evol) = fresh_applier().await;
        let id = seed_chunk(&kb, "/c", "important fact").await;

        // Pull the prior decay_score into a known non-default value
        // so we can verify the inverse_diff captures it.
        sqlx::query("UPDATE chunks SET decay_score = 0.72 WHERE id = ?1")
            .bind(id)
            .execute(kb.pool())
            .await
            .unwrap();

        let target = format!("consolidate_chunk:{id}");
        let pid = seed_approved(&evol, "evol-cons-001", &target).await;

        let history = applier.apply(&pid).await.unwrap();
        assert_eq!(history.kind, EvolutionKind::MemoryOp);
        assert_eq!(history.target, target);

        // Chunk now lives in `consolidated` with decay_score reset to 1.0.
        let state = kb.get_chunk_decay_state(id).await.unwrap().unwrap();
        assert_eq!(state.namespace, corlinman_vector::CONSOLIDATED_NAMESPACE);
        assert_eq!(state.decay_score, 1.0);
        assert!(state.consolidated_at.is_some());

        // inverse_diff captures the original namespace + decay_score.
        let inverse: serde_json::Value = serde_json::from_str(&history.inverse_diff).unwrap();
        assert_eq!(inverse["action"], "demote_chunk");
        assert_eq!(inverse["chunk_id"], id);
        assert_eq!(inverse["prior_namespace"], "general");
        assert!(
            (inverse["prior_decay_score"].as_f64().unwrap() - 0.72).abs() < 1e-5,
            "got {}",
            inverse["prior_decay_score"]
        );
    }

    /// Already-consolidated chunks must not be re-promoted — the
    /// applier surfaces InvalidTarget so a buggy proposer becomes
    /// noticeable instead of silently flipping a no-op into the audit
    /// trail.
    #[tokio::test]
    async fn apply_consolidate_chunk_rejects_already_consolidated() {
        let (_tmp, applier, kb, evol) = fresh_applier().await;
        let id = seed_chunk(&kb, "/c", "already locked in").await;
        // Pre-consolidate the chunk so the second apply hits the gate.
        kb.promote_to_consolidated(&[id]).await.unwrap();

        let target = format!("consolidate_chunk:{id}");
        let pid = seed_approved(&evol, "evol-cons-twice-001", &target).await;
        match applier.apply(&pid).await {
            Err(ApplyError::InvalidTarget(s)) => {
                assert!(s.contains(&format!("{id}")));
                assert!(s.contains("already consolidated"));
            }
            other => panic!("expected InvalidTarget, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn apply_consolidate_chunk_chunk_not_found() {
        let (_tmp, applier, _kb, evol) = fresh_applier().await;
        let pid = seed_approved(&evol, "evol-cons-missing-001", "consolidate_chunk:99999").await;
        assert!(matches!(
            applier.apply(&pid).await,
            Err(ApplyError::ChunkNotFound(99999))
        ));
    }

    /// Forward apply followed by a revert must restore the prior
    /// namespace + decay_score byte-for-byte.
    #[tokio::test]
    async fn revert_consolidate_chunk_restores_prior_state() {
        let (_tmp, applier, kb, evol) = fresh_applier().await;
        let id = seed_chunk(&kb, "/c", "rollback me").await;
        sqlx::query("UPDATE chunks SET decay_score = 0.42 WHERE id = ?1")
            .bind(id)
            .execute(kb.pool())
            .await
            .unwrap();
        let target = format!("consolidate_chunk:{id}");
        let pid = seed_approved(&evol, "evol-cons-revert-001", &target).await;

        applier.apply(&pid).await.unwrap();
        let after_apply = kb.get_chunk_decay_state(id).await.unwrap().unwrap();
        assert_eq!(
            after_apply.namespace,
            corlinman_vector::CONSOLIDATED_NAMESPACE
        );
        assert_eq!(after_apply.decay_score, 1.0);

        applier.revert(&pid, "rollback test").await.unwrap();
        let after_revert = kb.get_chunk_decay_state(id).await.unwrap().unwrap();
        assert_eq!(after_revert.namespace, "general");
        assert!(after_revert.consolidated_at.is_none());
        assert!(
            (after_revert.decay_score - 0.42).abs() < 1e-5,
            "got {}",
            after_revert.decay_score
        );
    }
}
