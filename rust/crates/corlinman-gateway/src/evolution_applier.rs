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
//! execution. Phase 4 W1 4-1D adds `prompt_template` and `tool_policy`.
//! Remaining kinds (`retry_tuning`, `agent_card`, `new_skill`) return
//! [`ApplyError::UnsupportedKind`] so callers see an explicit 4xx
//! instead of a silent no-op.
//!
//! ## Phase 4 W1 4-1D — `prompt_template` + `tool_policy`
//!
//! ### Prompt segment storage (v1)
//!
//! `prompt_template` proposals carry a dotted segment id as `target`
//! (e.g. `agent.greeting`, `tool.web_search.system`). The applier maps
//! the id to a per-tenant flat file:
//!
//! ```text
//! <data_dir>/tenants/<tenant>/prompt_segments/<dotted>.md
//! ```
//!
//! The file contents are the segment text. Apply reads the existing
//! file (treating absence as the empty string), writes the new content,
//! and captures the *prior* content in `inverse_diff` so revert can
//! restore byte-for-byte. This avoids needing an existing agent-card
//! registry; it ships a usable v1 the operator can integrate with —
//! the `corlinman-persona` Python package will eventually read this
//! directory tree as a prompt-segment override layer. Documented as a
//! deliberate v1 simplification so a later persona-aware mapping can
//! migrate the on-disk layout under the same `inverse_diff` shape.
//!
//! ### Tool policy storage
//!
//! `tool_policy` proposals carry a tool name as `target` (e.g.
//! `web_search`). Each tenant gets a TOML file:
//!
//! ```text
//! <data_dir>/tenants/<tenant>/tool_policy.toml
//! ```
//!
//! with one `[<tool>] mode = "auto"|"prompt"|"deny"` table per tool.
//! Apply confirms the on-disk mode matches `diff.before`
//! ([`ApplyError::DriftMismatch`] otherwise — operator must
//! re-evaluate), writes `diff.after`, and captures the prior mode in
//! `inverse_diff`. This intentionally does NOT modify
//! `corlinman.toml` — that's operator-managed config; the per-tenant
//! `tool_policy.toml` is a separate layer the future tool-router will
//! read alongside the static `[approvals.rules]` config.
//!
//! ### Tenant routing
//!
//! Both kinds support an optional `<tenant>::<rest>` prefix on the
//! `target` field. Example: `acme::agent.greeting` routes to tenant
//! `acme`; bare `agent.greeting` falls back to the default tenant
//! (`"default"`). The convention sidesteps needing a `tenant_id` field
//! on `EvolutionProposal` — the kind enum is locked for Phase 4 and
//! adding a column would require a `corlinman-evolution` migration. A
//! follow-up commit can promote `tenant_id` to a first-class column
//! and the prefix convention becomes a no-op.
//!
//! ### TOCTOU safety
//!
//! Both kinds canonicalise the destination's parent directory
//! immediately before the atomic write and assert containment under
//! `<data_dir>/tenants/<tenant>/`. Same belt-and-suspenders pattern
//! the Phase 3.1 / S-5 `MemoryOpSimulator` uses for skill files.
//! A racing process that swaps a directory for a symlink between the
//! entry-point check and the write surfaces here as
//! [`ApplyError::TenantPathEscape`].

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
    EvolutionHistory, EvolutionKind, EvolutionStatus, EvolutionStore, HistoryRepo, IntentLogRepo,
    ProposalId, ProposalsRepo, RepoError,
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
    /// Phase 3.1: `inverse_diff` parsed structurally but a field
    /// failed the trust whitelist (unknown namespace, non-ASCII path,
    /// out-of-range id, oversized string). Carries a short reason
    /// string. Distinct from `MalformedInverseDiff` so the monitor
    /// can downgrade tamper-suspect proposals to "skip + alert"
    /// instead of retrying the revert in a tight loop.
    #[error("tampered inverse_diff: {0}")]
    Tampered(String),
    /// Phase 4 W1 4-1D: `tool_policy` apply found the on-disk mode no
    /// longer matches `diff.before` — operator changed it manually
    /// since the proposal was authored, or a prior apply for the same
    /// target already landed. Caller must re-evaluate before retrying.
    #[error("tool_policy drift: {target}: expected before={expected:?}, got={actual:?}")]
    DriftMismatch {
        target: String,
        expected: String,
        actual: String,
    },
    /// Phase 4 W1 4-1D: `prompt_template` `target` failed the dotted
    /// segment id whitelist. Carries the rejected target.
    #[error("invalid prompt segment id: {0}")]
    PromptSegmentInvalid(String),
    /// Phase 4 W1 4-1D: `tool_policy` `target` failed the tool name
    /// whitelist. Carries the rejected target.
    #[error("invalid tool name: {0}")]
    ToolNameInvalid(String),
    /// Phase 4 W1 4-1D follow-up: `agent_card` `target` failed the
    /// agent-name whitelist. Same shape as `PromptSegmentInvalid` /
    /// `ToolNameInvalid` — distinct variant so the route layer can map
    /// it to a precise error envelope.
    #[error("invalid agent id: {0}")]
    AgentIdInvalid(String),
    /// Phase 4 W1 4-1D: `tool_policy` `diff.after` (or `diff.before`
    /// during revert) was not one of `auto` / `prompt` / `deny`.
    /// Carries the rejected mode.
    #[error("invalid tool mode: {0}")]
    ToolModeInvalid(String),
    /// Phase 4 W1 4-1D: `proposal.diff` JSON didn't parse or was
    /// missing required keys (`before` / `after` / `rationale` for
    /// prompt_template; `before` / `after` / `rule_id` for
    /// tool_policy). Carries a short reason string.
    #[error("malformed diff: {0}")]
    MalformedDiff(String),
    /// Phase 4 W1 4-1D: a per-tenant write resolved outside its tenant
    /// root after canonicalisation — symlink swap mid-apply, or a
    /// caller passed a `target` containing path traversal we missed
    /// at validation. Carries the rejected path string.
    #[error("tenant path escape: {0}")]
    TenantPathEscape(String),
    /// Phase 4 W1 4-1D: filesystem write or read for the
    /// prompt_template / tool_policy on-disk state failed.
    #[error("tenant state I/O failed: {0}")]
    TenantStateIo(#[source] anyhow::Error),
}

/// Real applier for `memory_op` evolution proposals. Constructed at
/// gateway startup once the kb + evolution stores are open; held inside
/// `AdminState` as `Option<Arc<EvolutionApplier>>` so the apply route
/// can return 503 when either store is missing.
pub struct EvolutionApplier {
    proposals: ProposalsRepo,
    history: HistoryRepo,
    /// Phase 3.1: writes to `apply_intent_log` so a crash between the
    /// kb mutation and the evolution audit row gets surfaced on
    /// startup. Same pool as the other evolution repos — single
    /// SQLite file, no extra connection budget.
    intent_log: IntentLogRepo,
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
        let intent_log = IntentLogRepo::new(evolution_store.pool().clone());
        Self {
            proposals,
            history,
            intent_log,
            kb_store,
            evolution_store,
            auto_rollback_thresholds,
            skills_dir,
        }
    }

    /// Phase 3.1 startup hook: scan `apply_intent_log` for half-
    /// committed forward applies (a kb mutation that was started but
    /// neither committed nor failed in the audit table). Each row
    /// emits a `tracing::warn!` line so operators see them in the
    /// boot log, plus the count is returned so the caller can wire a
    /// metric / hook event without re-querying.
    ///
    /// We deliberately do **not** auto-revert: the kb may already
    /// reflect the change, and the operator needs to decide whether
    /// to retry forward, run a manual revert, or accept the partial
    /// state. Auto-restoring without that human in the loop is the
    /// scenario that turns a bug into a data-loss incident.
    pub async fn scan_half_committed(&self) -> Result<usize, RepoError> {
        let outstanding = self.intent_log.list_uncommitted().await?;
        for intent in &outstanding {
            tracing::warn!(
                intent_id = intent.id,
                proposal_id = %intent.proposal_id,
                kind = %intent.kind,
                target = %intent.target,
                intent_at = intent.intent_at,
                "evolution_apply.half_committed: forward apply did not stamp \
                 committed_at/failed_at — manual inspection required"
            );
        }
        Ok(outstanding.len())
    }

    /// Apply an approved proposal. Returns the freshly-inserted history
    /// row (with autoincrement id populated). Failures leave the
    /// proposal in `approved` and write no history row — apart from the
    /// known two-DB partial-fail mode documented at the module level.
    ///
    /// Phase 3.1 wraps the entire forward path in an `apply_intent_log`
    /// row: opened before the kb mutation, stamped `committed_at` after
    /// the audit lands, stamped `failed_at` on any clean error. A crash
    /// between the two writes leaves a row with both stamps NULL — the
    /// gateway scans those at startup so operators see half-committed
    /// applies in the boot log.
    pub async fn apply(&self, id: &ProposalId) -> Result<EvolutionHistory, ApplyError> {
        // 1. Load + gate. We do these *before* opening the intent log
        //    so a NotFound / NotApproved doesn't litter the table with
        //    rows that were never going to mutate the kb.
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

        // 2. Open the intent log. Only proposals that survived the
        //    gates above land here; the row carries enough info
        //    (proposal_id + kind + target + intent_at) for the
        //    half-committed scan to be useful without a join back
        //    into evolution_proposals.
        let intent_at = now_ms();
        let intent_id = self
            .intent_log
            .record_intent(
                id.as_str(),
                proposal.kind.as_str(),
                &proposal.target,
                intent_at,
            )
            .await
            .map_err(ApplyError::Repo)?;

        // 3. Run the actual apply pipeline. On any error we stamp
        //    `failed_at` and return — the partial-index-backed scan
        //    will not surface this row, so operators only see the
        //    truly-stuck applies. On success we stamp `committed_at`
        //    via the same path.
        match self.apply_inner(id, &proposal).await {
            Ok(out) => {
                if let Err(e) = self.intent_log.mark_committed(intent_id, now_ms()).await {
                    // Log-only: the apply itself succeeded; failing to
                    // stamp committed_at just means the half-committed
                    // scan will surface this row at next boot. The
                    // operator can clear it by inspecting the audit
                    // trail, which is intact.
                    tracing::warn!(
                        intent_id,
                        error = %e,
                        "apply_intent_log.mark_committed failed; row will appear in next startup scan"
                    );
                }
                Ok(out)
            }
            Err(err) => {
                // Cheap reason string — keep it short, the column is
                // operator-facing not a debugger trace.
                let reason = format!("{err}");
                if let Err(e) = self
                    .intent_log
                    .mark_failed(intent_id, now_ms(), &reason)
                    .await
                {
                    tracing::warn!(
                        intent_id,
                        error = %e,
                        "apply_intent_log.mark_failed failed; row will appear in next startup scan"
                    );
                }
                Err(err)
            }
        }
    }

    /// The original Phase 2 apply pipeline, factored out so the
    /// Phase 3.1 intent-log wrapper above can pin success/failure on
    /// a single Result<...>.
    async fn apply_inner(
        &self,
        id: &ProposalId,
        proposal: &corlinman_evolution::EvolutionProposal,
    ) -> Result<EvolutionHistory, ApplyError> {
        // Dispatch per-kind. Each handler returns a `MutationOutcome`
        // so the audit/baseline path below stays kind-agnostic.
        // `memory_op` carries a parsed plan (`merge_chunks` or
        // `delete_chunk`) for the counter bump after the audit lands;
        // the new kinds bump no counters yet (Phase 3-2B doesn't ship
        // per-kind metrics — operator dashboards read history).
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
            EvolutionKind::PromptTemplate => {
                self.apply_prompt_template(&proposal.target, &proposal.diff)
                    .await?
            }
            EvolutionKind::ToolPolicy => {
                self.apply_tool_policy(&proposal.target, &proposal.diff)
                    .await?
            }
            EvolutionKind::AgentCard => {
                self.apply_agent_card(&proposal.target, &proposal.diff)
                    .await?
            }
            other => return Err(ApplyError::UnsupportedKind(other.as_str().to_string())),
        };

        // Persist history + flip proposal.status atomically inside
        // evolution.sqlite. kb.sqlite is already mutated (single
        // DELETE statement, atomic by SQLite contract); a TX here
        // keeps the audit row + status flip in lockstep.
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
            serde_json::to_value(&snap).map_err(|e| ApplyError::History(anyhow::Error::from(e)))?
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

        // Bump kb-side counters only after the audit row landed.
        // `memory_op` has dedicated counters; other kinds rely on
        // EVOLUTION_PROPOSALS_APPLIED below for now.
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
                // Phase 3.1 (B-3): persist `prior_consolidated_at` so a
                // demote→re-promote→demote cycle keeps the original
                // first-promotion timestamp. The previous
                // `inverse_diff` shape lost it (revert hard-coded NULL),
                // which silently corrupted the audit trail any time a
                // chunk bounced through consolidation more than once.
                let inverse_diff = json!({
                    "action": "demote_chunk",
                    "chunk_id": id,
                    "prior_namespace": prior.namespace,
                    "prior_decay_score": prior.decay_score,
                    "prior_consolidated_at": prior.consolidated_at,
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
    async fn apply_tag_rebalance(&self, target: &str) -> Result<MutationOutcome, ApplyError> {
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
        let parent_id = parent_id.ok_or(ApplyError::CannotMergeRoot)?;

        // 2. Compute before_sha from the row about to be deleted +
        //    its existing chunk_tags pairs. Locality-only — same
        //    convention the memory_op path follows.
        let before_sha = sha256_tag_state(
            &src_path,
            src_id,
            parent_id,
            &src_name,
            src_depth,
            src_created_at,
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
            let rows = sqlx::query("SELECT chunk_id FROM chunk_tags WHERE tag_node_id = ?1")
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

    /// Phase 4 W1 4-1D: forward apply for `prompt_template`. `target`
    /// carries an optional `<tenant>::` prefix followed by a dotted
    /// segment id (e.g. `acme::agent.greeting`, bare `agent.greeting`
    /// = default tenant). `diff` is the JSON shape the Python
    /// proposer emits: `{ before: "", after: <new>, rationale: <str> }`.
    /// `before` is intentionally empty — the applier reads the live
    /// segment from disk to capture the inverse.
    ///
    /// On disk:
    /// `<data_dir>/tenants/<tenant>/prompt_segments/<dotted>.md`
    ///
    /// `inverse_diff` shape:
    /// ```json
    /// {
    ///   "op": "prompt_template",
    ///   "tenant": "<tenant>",
    ///   "segment": "<dotted>",
    ///   "before": "<prior content>",
    ///   "before_present": true|false
    /// }
    /// ```
    ///
    /// `before_present` distinguishes "file existed and was empty" from
    /// "file did not exist" so revert can choose between writing an
    /// empty file and removing the segment file entirely. The forward
    /// path treats absence as the empty string for the apply itself
    /// (we always write the new content), but the inverse cares about
    /// the original presence.
    async fn apply_prompt_template(
        &self,
        target: &str,
        diff: &str,
    ) -> Result<MutationOutcome, ApplyError> {
        let (tenant, segment) = split_target_with_tenant(target);
        validate_tenant_id(tenant)?;
        validate_prompt_segment_id(segment)?;

        let parsed: serde_json::Value = serde_json::from_str(diff)
            .map_err(|e| ApplyError::MalformedDiff(format!("prompt_template parse: {e}")))?;
        let after = parsed
            .get("after")
            .and_then(|v| v.as_str())
            .ok_or_else(|| ApplyError::MalformedDiff("prompt_template: missing 'after'".into()))?
            .to_string();
        // `before` and `rationale` are part of the wire shape but the
        // applier doesn't trust `before` — we read the live segment.
        // Validate they're present so a buggy proposer still surfaces.
        if parsed.get("before").is_none() {
            return Err(ApplyError::MalformedDiff(
                "prompt_template: missing 'before'".into(),
            ));
        }
        if parsed.get("rationale").is_none() {
            return Err(ApplyError::MalformedDiff(
                "prompt_template: missing 'rationale'".into(),
            ));
        }

        // Resolve + canonicalise. The tenant root must exist before
        // canonicalisation; create the directory tree first, then
        // canonicalise the parent and assert containment.
        let tenants_root = self.tenants_root_dir();
        tokio::fs::create_dir_all(&tenants_root)
            .await
            .map_err(|e| ApplyError::TenantStateIo(anyhow::Error::from(e)))?;
        let segments_dir = tenants_root.join(tenant).join("prompt_segments");
        tokio::fs::create_dir_all(&segments_dir)
            .await
            .map_err(|e| ApplyError::TenantStateIo(anyhow::Error::from(e)))?;
        let path = segments_dir.join(format!("{segment}.md"));

        // Pre-write canonicalise of the parent — same belt-and-suspenders
        // pattern Phase 3.1 / S-5 uses in MemoryOpSimulator. Re-checked
        // immediately before the rename below.
        assert_under_tenants_root(&segments_dir, &tenants_root)?;

        // Read prior content. Missing file is allowed — first-time
        // segment creation is the common case for prompt_template.
        let (prior_content, before_present) = match tokio::fs::read_to_string(&path).await {
            Ok(s) => (s, true),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => (String::new(), false),
            Err(e) => return Err(ApplyError::TenantStateIo(anyhow::Error::from(e))),
        };

        // Atomic write: tmp + rename. Same shape as skill_update.
        let mut tmp = path.clone();
        let mut name = tmp
            .file_name()
            .map(|n| n.to_os_string())
            .unwrap_or_default();
        name.push(".tmp");
        tmp.set_file_name(name);
        tokio::fs::write(&tmp, after.as_bytes())
            .await
            .map_err(|e| ApplyError::TenantStateIo(anyhow::Error::from(e)))?;

        // TOCTOU re-check: a racing process could have swapped the
        // parent dir for a symlink between the create_dir_all above
        // and the rename. Re-canonicalise and assert.
        assert_under_tenants_root(&segments_dir, &tenants_root)?;

        tokio::fs::rename(&tmp, &path)
            .await
            .map_err(|e| ApplyError::TenantStateIo(anyhow::Error::from(e)))?;

        let before_sha = sha256_hex(prior_content.as_bytes());
        let after_sha = sha256_hex(after.as_bytes());
        let inverse_diff = json!({
            "op": "prompt_template",
            "tenant": tenant,
            "segment": segment,
            "before": prior_content,
            "before_present": before_present,
        })
        .to_string();

        Ok(MutationOutcome {
            before_sha,
            after_sha,
            inverse_diff,
        })
    }

    /// Phase 4 W1 4-1D follow-up: forward apply for `agent_card`.
    /// Mirrors `apply_prompt_template` shape but writes to the
    /// per-tenant `agent_cards/<name>.md` layout. The Python proposer
    /// emits `target = "<agent_name>"` (optionally `<tenant>::<name>`)
    /// and `diff = { before: "", after: <new_content>, rationale }`.
    /// `before` is intentionally empty on the wire — this applier
    /// reads the live agent-card file at apply time and stores the
    /// resolved prior content in the inverse_diff.
    ///
    /// Layout decision: `<data_dir>/tenants/<tenant>/agent_cards/<name>.md`
    /// — flat per-tenant directory, one file per agent. Mirrors
    /// `prompt_segments/` from `apply_prompt_template`. The
    /// `corlinman-persona` Python package will eventually read this
    /// directory tree as a per-tenant character-card override layer.
    async fn apply_agent_card(
        &self,
        target: &str,
        diff: &str,
    ) -> Result<MutationOutcome, ApplyError> {
        let (tenant, agent) = split_target_with_tenant(target);
        validate_tenant_id(tenant)?;
        validate_agent_id(agent)?;

        let parsed: serde_json::Value = serde_json::from_str(diff)
            .map_err(|e| ApplyError::MalformedDiff(format!("agent_card parse: {e}")))?;
        let after = parsed
            .get("after")
            .and_then(|v| v.as_str())
            .ok_or_else(|| ApplyError::MalformedDiff("agent_card: missing 'after'".into()))?
            .to_string();
        if parsed.get("before").is_none() {
            return Err(ApplyError::MalformedDiff(
                "agent_card: missing 'before'".into(),
            ));
        }
        if parsed.get("rationale").is_none() {
            return Err(ApplyError::MalformedDiff(
                "agent_card: missing 'rationale'".into(),
            ));
        }

        let tenants_root = self.tenants_root_dir();
        tokio::fs::create_dir_all(&tenants_root)
            .await
            .map_err(|e| ApplyError::TenantStateIo(anyhow::Error::from(e)))?;
        let cards_dir = tenants_root.join(tenant).join("agent_cards");
        tokio::fs::create_dir_all(&cards_dir)
            .await
            .map_err(|e| ApplyError::TenantStateIo(anyhow::Error::from(e)))?;
        let path = cards_dir.join(format!("{agent}.md"));

        // Pre-write canonicalise (Phase 3.1 / S-5 TOCTOU pattern).
        assert_under_tenants_root(&cards_dir, &tenants_root)?;

        let (prior_content, before_present) = match tokio::fs::read_to_string(&path).await {
            Ok(s) => (s, true),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => (String::new(), false),
            Err(e) => return Err(ApplyError::TenantStateIo(anyhow::Error::from(e))),
        };

        // Atomic tmp + rename, same as prompt_template.
        let mut tmp = path.clone();
        let mut name = tmp
            .file_name()
            .map(|n| n.to_os_string())
            .unwrap_or_default();
        name.push(".tmp");
        tmp.set_file_name(name);
        tokio::fs::write(&tmp, after.as_bytes())
            .await
            .map_err(|e| ApplyError::TenantStateIo(anyhow::Error::from(e)))?;

        // Re-canonicalise post-tmp-write to defeat parent-dir swaps.
        assert_under_tenants_root(&cards_dir, &tenants_root)?;

        tokio::fs::rename(&tmp, &path)
            .await
            .map_err(|e| ApplyError::TenantStateIo(anyhow::Error::from(e)))?;

        let before_sha = sha256_hex(prior_content.as_bytes());
        let after_sha = sha256_hex(after.as_bytes());
        let inverse_diff = json!({
            "op": "agent_card",
            "tenant": tenant,
            "agent": agent,
            "before": prior_content,
            "before_present": before_present,
        })
        .to_string();

        Ok(MutationOutcome {
            before_sha,
            after_sha,
            inverse_diff,
        })
    }

    /// Phase 4 W1 4-1D: forward apply for `tool_policy`. `target` is
    /// optionally `<tenant>::<tool>`; `diff` is the JSON shape the
    /// Python proposer emits:
    /// `{ before: <mode>, after: <mode>, rule_id: <str> }`.
    ///
    /// On disk:
    /// `<data_dir>/tenants/<tenant>/tool_policy.toml`
    ///
    /// with one `[<tool>] mode = "<auto|prompt|deny>"` table per tool.
    /// Apply:
    /// 1. Reads existing toml (missing file = `{}`)
    /// 2. Confirms current `[<tool>] mode` matches `diff.before` —
    ///    [`ApplyError::DriftMismatch`] otherwise
    /// 3. Writes `diff.after`, atomic tmp + rename
    /// 4. Captures prior mode in `inverse_diff`
    ///
    /// `inverse_diff` shape:
    /// ```json
    /// {
    ///   "op": "tool_policy",
    ///   "tenant": "<tenant>",
    ///   "tool": "<tool>",
    ///   "before_mode": "<auto|prompt|deny>",
    ///   "before_present": true|false,
    ///   "rule_id": "<rule>"
    /// }
    /// ```
    async fn apply_tool_policy(
        &self,
        target: &str,
        diff: &str,
    ) -> Result<MutationOutcome, ApplyError> {
        let (tenant, tool) = split_target_with_tenant(target);
        validate_tenant_id(tenant)?;
        validate_tool_name(tool)?;

        let parsed: serde_json::Value = serde_json::from_str(diff)
            .map_err(|e| ApplyError::MalformedDiff(format!("tool_policy parse: {e}")))?;
        let before_mode = parsed
            .get("before")
            .and_then(|v| v.as_str())
            .ok_or_else(|| ApplyError::MalformedDiff("tool_policy: missing 'before'".into()))?
            .to_string();
        let after_mode = parsed
            .get("after")
            .and_then(|v| v.as_str())
            .ok_or_else(|| ApplyError::MalformedDiff("tool_policy: missing 'after'".into()))?
            .to_string();
        let rule_id = parsed
            .get("rule_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| ApplyError::MalformedDiff("tool_policy: missing 'rule_id'".into()))?
            .to_string();
        validate_tool_mode(&before_mode)?;
        validate_tool_mode(&after_mode)?;
        validate_rule_id(&rule_id)?;

        let tenants_root = self.tenants_root_dir();
        tokio::fs::create_dir_all(&tenants_root)
            .await
            .map_err(|e| ApplyError::TenantStateIo(anyhow::Error::from(e)))?;
        let tenant_dir = tenants_root.join(tenant);
        tokio::fs::create_dir_all(&tenant_dir)
            .await
            .map_err(|e| ApplyError::TenantStateIo(anyhow::Error::from(e)))?;
        let path = tenant_dir.join("tool_policy.toml");
        assert_under_tenants_root(&tenant_dir, &tenants_root)?;

        // Read existing toml. Missing file is allowed; treat as {}.
        let prior_text = match tokio::fs::read_to_string(&path).await {
            Ok(s) => s,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => String::new(),
            Err(e) => return Err(ApplyError::TenantStateIo(anyhow::Error::from(e))),
        };

        let mut doc: toml::Table = if prior_text.is_empty() {
            toml::Table::new()
        } else {
            prior_text.parse().map_err(|e| {
                ApplyError::TenantStateIo(anyhow::Error::msg(format!(
                    "tool_policy.toml parse: {e}"
                )))
            })?
        };

        // Drift-detect. Three cases:
        // 1. table present + mode matches `before` → proceed
        // 2. table present + mode mismatch → DriftMismatch
        // 3. table absent → treat absent as drift unless before_mode
        //    happens to be the implicit default. We don't pretend
        //    absence equals any mode — operator must inspect.
        let (existing_mode, before_present) = read_existing_mode(&doc, tool)?;
        match existing_mode.as_deref() {
            Some(m) if m == before_mode => {}
            Some(m) => {
                return Err(ApplyError::DriftMismatch {
                    target: target.to_string(),
                    expected: before_mode,
                    actual: m.to_string(),
                })
            }
            None => {
                return Err(ApplyError::DriftMismatch {
                    target: target.to_string(),
                    expected: before_mode,
                    actual: "<absent>".into(),
                });
            }
        }

        // Mutate doc in place — replace just the `[tool]` table.
        let mut tool_table = toml::Table::new();
        tool_table.insert("mode".into(), toml::Value::String(after_mode.clone()));
        tool_table.insert("rule_id".into(), toml::Value::String(rule_id.clone()));
        doc.insert(tool.to_string(), toml::Value::Table(tool_table));

        let new_text = toml::to_string_pretty(&doc).map_err(|e| {
            ApplyError::TenantStateIo(anyhow::Error::msg(format!(
                "tool_policy.toml serialize: {e}"
            )))
        })?;

        // Atomic write.
        let mut tmp = path.clone();
        let mut name = tmp
            .file_name()
            .map(|n| n.to_os_string())
            .unwrap_or_default();
        name.push(".tmp");
        tmp.set_file_name(name);
        tokio::fs::write(&tmp, new_text.as_bytes())
            .await
            .map_err(|e| ApplyError::TenantStateIo(anyhow::Error::from(e)))?;
        assert_under_tenants_root(&tenant_dir, &tenants_root)?;
        tokio::fs::rename(&tmp, &path)
            .await
            .map_err(|e| ApplyError::TenantStateIo(anyhow::Error::from(e)))?;

        let before_sha = sha256_hex(prior_text.as_bytes());
        let after_sha = sha256_hex(new_text.as_bytes());
        let inverse_diff = json!({
            "op": "tool_policy",
            "tenant": tenant,
            "tool": tool,
            "before_mode": before_mode,
            "before_present": before_present,
            "rule_id": rule_id,
        })
        .to_string();

        Ok(MutationOutcome {
            before_sha,
            after_sha,
            inverse_diff,
        })
    }

    /// Phase 4 W1 4-1D: reverse handler for `prompt_template`. Reads
    /// `inverse_diff.before` and writes it back to the segment file
    /// (or removes the file if `before_present == false`).
    async fn revert_prompt_template(&self, history: &EvolutionHistory) -> Result<(), ApplyError> {
        let raw: serde_json::Value = serde_json::from_str(&history.inverse_diff)
            .map_err(|e| ApplyError::MalformedInverseDiff(format!("parse: {e}")))?;
        let op = raw
            .get("op")
            .and_then(|v| v.as_str())
            .ok_or_else(|| ApplyError::MalformedInverseDiff("missing 'op'".into()))?;
        if op != "prompt_template" {
            return Err(ApplyError::MalformedInverseDiff(format!(
                "unknown op: {op}"
            )));
        }
        let tenant = pick_str(&raw, "tenant").map_err(ApplyError::MalformedInverseDiff)?;
        let segment = pick_str(&raw, "segment").map_err(ApplyError::MalformedInverseDiff)?;
        let before = pick_str(&raw, "before").map_err(ApplyError::MalformedInverseDiff)?;
        let before_present = raw
            .get("before_present")
            .and_then(|v| v.as_bool())
            .ok_or_else(|| ApplyError::MalformedInverseDiff("missing 'before_present'".into()))?;

        // Trust gates — the same revert-side validation the other
        // kinds run before binding untrusted history values to disk.
        validate_tenant_id_revert(history, &tenant)?;
        validate_prompt_segment_id_revert(history, &segment)?;

        let tenants_root = self.tenants_root_dir();
        // The directory may have been removed since apply (e.g.
        // operator wiped tenants/). Re-create on the revert path —
        // restoring "no segment" is a legitimate outcome.
        tokio::fs::create_dir_all(&tenants_root)
            .await
            .map_err(|e| ApplyError::TenantStateIo(anyhow::Error::from(e)))?;
        let segments_dir = tenants_root.join(&tenant).join("prompt_segments");
        tokio::fs::create_dir_all(&segments_dir)
            .await
            .map_err(|e| ApplyError::TenantStateIo(anyhow::Error::from(e)))?;
        let path = segments_dir.join(format!("{segment}.md"));
        assert_under_tenants_root(&segments_dir, &tenants_root)?;

        if before_present {
            // Restore the prior content. Atomic tmp + rename so a
            // crash mid-revert doesn't truncate the segment.
            let mut tmp = path.clone();
            let mut name = tmp
                .file_name()
                .map(|n| n.to_os_string())
                .unwrap_or_default();
            name.push(".tmp");
            tmp.set_file_name(name);
            tokio::fs::write(&tmp, before.as_bytes())
                .await
                .map_err(|e| ApplyError::TenantStateIo(anyhow::Error::from(e)))?;
            assert_under_tenants_root(&segments_dir, &tenants_root)?;
            tokio::fs::rename(&tmp, &path)
                .await
                .map_err(|e| ApplyError::TenantStateIo(anyhow::Error::from(e)))?;
        } else {
            // Forward apply created the segment from scratch — revert
            // removes it. Idempotent on a re-fire (NotFound is OK).
            match tokio::fs::remove_file(&path).await {
                Ok(()) => {}
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
                Err(e) => return Err(ApplyError::TenantStateIo(anyhow::Error::from(e))),
            }
        }
        Ok(())
    }

    /// Phase 4 W1 4-1D follow-up: reverse handler for `agent_card`.
    /// Mirrors `revert_prompt_template` — atomic tmp + rename when
    /// the file existed before apply, otherwise removes the file.
    async fn revert_agent_card(&self, history: &EvolutionHistory) -> Result<(), ApplyError> {
        let raw: serde_json::Value = serde_json::from_str(&history.inverse_diff)
            .map_err(|e| ApplyError::MalformedInverseDiff(format!("parse: {e}")))?;
        let op = raw
            .get("op")
            .and_then(|v| v.as_str())
            .ok_or_else(|| ApplyError::MalformedInverseDiff("missing 'op'".into()))?;
        if op != "agent_card" {
            return Err(ApplyError::MalformedInverseDiff(format!(
                "unknown op: {op}"
            )));
        }
        let tenant = pick_str(&raw, "tenant").map_err(ApplyError::MalformedInverseDiff)?;
        let agent = pick_str(&raw, "agent").map_err(ApplyError::MalformedInverseDiff)?;
        let before = pick_str(&raw, "before").map_err(ApplyError::MalformedInverseDiff)?;
        let before_present = raw
            .get("before_present")
            .and_then(|v| v.as_bool())
            .ok_or_else(|| ApplyError::MalformedInverseDiff("missing 'before_present'".into()))?;

        validate_tenant_id_revert(history, &tenant)?;
        validate_agent_id_revert(history, &agent)?;

        let tenants_root = self.tenants_root_dir();
        tokio::fs::create_dir_all(&tenants_root)
            .await
            .map_err(|e| ApplyError::TenantStateIo(anyhow::Error::from(e)))?;
        let cards_dir = tenants_root.join(&tenant).join("agent_cards");
        tokio::fs::create_dir_all(&cards_dir)
            .await
            .map_err(|e| ApplyError::TenantStateIo(anyhow::Error::from(e)))?;
        let path = cards_dir.join(format!("{agent}.md"));
        assert_under_tenants_root(&cards_dir, &tenants_root)?;

        if before_present {
            let mut tmp = path.clone();
            let mut name = tmp
                .file_name()
                .map(|n| n.to_os_string())
                .unwrap_or_default();
            name.push(".tmp");
            tmp.set_file_name(name);
            tokio::fs::write(&tmp, before.as_bytes())
                .await
                .map_err(|e| ApplyError::TenantStateIo(anyhow::Error::from(e)))?;
            assert_under_tenants_root(&cards_dir, &tenants_root)?;
            tokio::fs::rename(&tmp, &path)
                .await
                .map_err(|e| ApplyError::TenantStateIo(anyhow::Error::from(e)))?;
        } else {
            match tokio::fs::remove_file(&path).await {
                Ok(()) => {}
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
                Err(e) => return Err(ApplyError::TenantStateIo(anyhow::Error::from(e))),
            }
        }
        Ok(())
    }

    /// Phase 4 W1 4-1D: reverse handler for `tool_policy`. Writes the
    /// captured `before_mode` back to the toml table for the tool. If
    /// the table didn't exist before apply (`before_present == false`),
    /// we remove the table outright.
    async fn revert_tool_policy(&self, history: &EvolutionHistory) -> Result<(), ApplyError> {
        let raw: serde_json::Value = serde_json::from_str(&history.inverse_diff)
            .map_err(|e| ApplyError::MalformedInverseDiff(format!("parse: {e}")))?;
        let op = raw
            .get("op")
            .and_then(|v| v.as_str())
            .ok_or_else(|| ApplyError::MalformedInverseDiff("missing 'op'".into()))?;
        if op != "tool_policy" {
            return Err(ApplyError::MalformedInverseDiff(format!(
                "unknown op: {op}"
            )));
        }
        let tenant = pick_str(&raw, "tenant").map_err(ApplyError::MalformedInverseDiff)?;
        let tool = pick_str(&raw, "tool").map_err(ApplyError::MalformedInverseDiff)?;
        let before_mode =
            pick_str(&raw, "before_mode").map_err(ApplyError::MalformedInverseDiff)?;
        let before_present = raw
            .get("before_present")
            .and_then(|v| v.as_bool())
            .ok_or_else(|| ApplyError::MalformedInverseDiff("missing 'before_present'".into()))?;
        let rule_id = pick_str(&raw, "rule_id").map_err(ApplyError::MalformedInverseDiff)?;

        validate_tenant_id_revert(history, &tenant)?;
        validate_tool_name_revert(history, &tool)?;
        // Mode/rule_id come from operator-written history rows — apply
        // the same whitelist.
        if !is_valid_tool_mode(&before_mode) {
            return Err(tampered(
                history,
                format!("before_mode rejected: {before_mode:?}"),
            ));
        }
        validate_inverse_diff_path(history, "rule_id", &rule_id)?;

        let tenants_root = self.tenants_root_dir();
        tokio::fs::create_dir_all(&tenants_root)
            .await
            .map_err(|e| ApplyError::TenantStateIo(anyhow::Error::from(e)))?;
        let tenant_dir = tenants_root.join(&tenant);
        tokio::fs::create_dir_all(&tenant_dir)
            .await
            .map_err(|e| ApplyError::TenantStateIo(anyhow::Error::from(e)))?;
        let path = tenant_dir.join("tool_policy.toml");
        assert_under_tenants_root(&tenant_dir, &tenants_root)?;

        // Read the current state of the toml so we can mutate just the
        // single table without losing sibling tools' rows.
        let current_text = match tokio::fs::read_to_string(&path).await {
            Ok(s) => s,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => String::new(),
            Err(e) => return Err(ApplyError::TenantStateIo(anyhow::Error::from(e))),
        };
        let mut doc: toml::Table = if current_text.is_empty() {
            toml::Table::new()
        } else {
            current_text.parse().map_err(|e| {
                ApplyError::TenantStateIo(anyhow::Error::msg(format!(
                    "tool_policy.toml parse: {e}"
                )))
            })?
        };

        if before_present {
            let mut tool_table = toml::Table::new();
            tool_table.insert("mode".into(), toml::Value::String(before_mode.clone()));
            tool_table.insert("rule_id".into(), toml::Value::String(rule_id.clone()));
            doc.insert(tool.clone(), toml::Value::Table(tool_table));
        } else {
            // Forward apply created the table — revert deletes it.
            doc.remove(&tool);
        }

        let new_text = toml::to_string_pretty(&doc).map_err(|e| {
            ApplyError::TenantStateIo(anyhow::Error::msg(format!(
                "tool_policy.toml serialize: {e}"
            )))
        })?;

        let mut tmp = path.clone();
        let mut name = tmp
            .file_name()
            .map(|n| n.to_os_string())
            .unwrap_or_default();
        name.push(".tmp");
        tmp.set_file_name(name);
        tokio::fs::write(&tmp, new_text.as_bytes())
            .await
            .map_err(|e| ApplyError::TenantStateIo(anyhow::Error::from(e)))?;
        assert_under_tenants_root(&tenant_dir, &tenants_root)?;
        tokio::fs::rename(&tmp, &path)
            .await
            .map_err(|e| ApplyError::TenantStateIo(anyhow::Error::from(e)))?;
        Ok(())
    }

    /// Phase 4 W1 4-1D: per-tenant root directory for prompt_segment
    /// files + tool_policy.toml. Derived as a sibling of `skills_dir`
    /// because both live under `<data_dir>` and the applier already
    /// owns `skills_dir`. A future signature change can promote this
    /// to a first-class field; for now `skills_dir.parent()` keeps the
    /// constructor surface identical so server.rs doesn't need to
    /// thread a new argument.
    fn tenants_root_dir(&self) -> PathBuf {
        self.skills_dir
            .parent()
            .map(|p| p.join("tenants"))
            .unwrap_or_else(|| self.skills_dir.join("tenants"))
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
            return Err(ApplyError::NotApplied(proposal.status.as_str().to_string()));
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
            EvolutionKind::PromptTemplate => self.revert_prompt_template(&history_row).await?,
            EvolutionKind::ToolPolicy => self.revert_tool_policy(&history_row).await?,
            EvolutionKind::AgentCard => self.revert_agent_card(&history_row).await?,
            other => {
                return Err(ApplyError::UnsupportedRevertKind(
                    other.as_str().to_string(),
                ))
            }
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
            .map_err(ApplyError::Repo)?;

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
    ///
    /// Phase 3.1 trust gate: every untrusted field read from
    /// `inverse_diff` is validated against
    /// [`validate_inverse_diff_namespace`] / [`validate_inverse_diff_id`]
    /// before it touches sqlx::bind. The history table is shared with
    /// every kind, and W2-C will let high-risk proposals slip through;
    /// a tampered row must not become a "write any namespace" or
    /// "write any chunk_id" primitive on the revert path.
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
                plan.validate(history)?;
                plan.execute(self.kb_store.pool())
                    .await
                    .map_err(|e| ApplyError::Kb(anyhow::Error::from(e)))?;
            }
            "demote_chunk" => {
                let chunk_id =
                    pick_i64(&raw, "chunk_id").map_err(ApplyError::MalformedInverseDiff)?;
                validate_inverse_diff_id(history, "chunk_id", chunk_id)?;
                let prior_ns =
                    pick_str(&raw, "prior_namespace").map_err(ApplyError::MalformedInverseDiff)?;
                validate_inverse_diff_namespace(history, &prior_ns)?;
                let prior_decay = raw
                    .get("prior_decay_score")
                    .and_then(|v| v.as_f64())
                    .ok_or_else(|| {
                        ApplyError::MalformedInverseDiff("missing 'prior_decay_score'".into())
                    })? as f32;
                // Phase 3.1 (B-3): `prior_consolidated_at` joined the
                // inverse_diff shape so a chunk that was previously
                // consolidated, demoted, then re-consolidated keeps
                // its first-promotion timestamp on the second demote.
                // Old (pre-3.1) history rows don't carry the field —
                // fall back to the legacy NULL behaviour and warn so
                // operators can trace a missed bit if they ever audit
                // consolidated_at history.
                let prior_consolidated_at: Option<i64> = match raw.get("prior_consolidated_at") {
                    Some(serde_json::Value::Null) => None,
                    Some(v) => Some(v.as_i64().ok_or_else(|| {
                        ApplyError::MalformedInverseDiff(
                            "'prior_consolidated_at' must be integer or null".into(),
                        )
                    })?),
                    None => {
                        tracing::warn!(
                            chunk_id,
                            "consolidate_chunk revert: legacy inverse_diff missing \
                             'prior_consolidated_at'; falling back to NULL (v0.x history)",
                        );
                        None
                    }
                };
                self.kb_store
                    .demote_from_consolidated(
                        chunk_id,
                        &prior_ns,
                        prior_decay,
                        prior_consolidated_at,
                    )
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
    async fn revert_tag_rebalance(&self, history: &EvolutionHistory) -> Result<(), ApplyError> {
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
        let parent_id = pick_i64(src, "parent_id").map_err(ApplyError::MalformedInverseDiff)?;
        let name = pick_str(src, "name").map_err(ApplyError::MalformedInverseDiff)?;
        let path = pick_str(src, "path").map_err(ApplyError::MalformedInverseDiff)?;
        let depth = pick_i64(src, "depth").map_err(ApplyError::MalformedInverseDiff)?;
        let created_at = pick_i64(src, "created_at").map_err(ApplyError::MalformedInverseDiff)?;

        // Phase 3.1 trust gate: every untrusted field read from the
        // history row gets validated before sqlx binds it. A tampered
        // row could otherwise turn this revert into a "write any
        // tag_node row" primitive against kb.sqlite. We accept only
        // ASCII path-like strings and positive 32-bit-ish ids — same
        // shape the forward apply emits in practice.
        validate_inverse_diff_id(history, "src.id", id)?;
        validate_inverse_diff_id(history, "src.parent_id", parent_id)?;
        validate_inverse_diff_id(history, "src.depth", depth)?;
        validate_inverse_diff_id(history, "src.created_at", created_at)?;
        validate_inverse_diff_path(history, "src.name", &name)?;
        validate_inverse_diff_path(history, "src.path", &path)?;

        let moved_ids: Vec<i64> = raw
            .get("moved_chunk_tag_ids")
            .and_then(|v| v.as_array())
            .map(|arr| arr.iter().filter_map(|v| v.as_i64()).collect())
            .unwrap_or_default();
        for chunk_id in &moved_ids {
            validate_inverse_diff_id(history, "moved_chunk_tag_ids[]", *chunk_id)?;
        }

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
                sqlx::query("DELETE FROM chunk_tags WHERE chunk_id = ?1 AND tag_node_id = ?2")
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
    async fn revert_skill_update(&self, history: &EvolutionHistory) -> Result<(), ApplyError> {
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

    /// Phase 3.1 trust gate: every untrusted field that the execute
    /// path will bind against kb.sqlite must pass the inverse_diff
    /// whitelist. Content is intentionally **not** validated here —
    /// it's the chunk body, free-form text, and the worst it can do
    /// is land in a sqlx-bound TEXT column. Namespaces, ids, and
    /// path-shaped metadata are the dangerous fields.
    fn validate(&self, history: &EvolutionHistory) -> Result<(), ApplyError> {
        match self {
            Self::WithId {
                id,
                file_id,
                chunk_index,
                namespace,
                content: _,
            } => {
                validate_inverse_diff_id(history, "loser_id", *id)?;
                validate_inverse_diff_id(history, "loser_file_id", *file_id)?;
                validate_inverse_diff_id(history, "loser_chunk_index", *chunk_index)?;
                validate_inverse_diff_namespace(history, namespace)?;
            }
            Self::WithoutId {
                file_id,
                chunk_index,
                namespace,
                content: _,
            } => {
                validate_inverse_diff_id(history, "file_id", *file_id)?;
                validate_inverse_diff_id(history, "chunk_index", *chunk_index)?;
                validate_inverse_diff_namespace(history, namespace)?;
            }
        }
        Ok(())
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

// ---------------------------------------------------------------------------
// Phase 4 W1 4-1D — prompt_template / tool_policy helpers.
// ---------------------------------------------------------------------------

/// Default tenant id for proposals that don't carry a `<tenant>::`
/// prefix on their target. Mirrors the `'default'` literal the Phase
/// 3.1 persona schema uses on its `tenant_id` column.
const DEFAULT_TENANT_ID: &str = "default";

/// Cap on tenant id / segment id / tool name lengths. 64 chars covers
/// realistic operator naming and keeps path lengths bounded — matches
/// the `MAX_INVERSE_DIFF_STRING_LEN` discipline.
const MAX_TENANT_PART_LEN: usize = 64;

/// Cap on segment id length (dotted form). 128 chars is long enough
/// for `tool.<name>.system.<sub>` shapes and short enough that
/// concatenated paths stay well under common filesystem limits.
const MAX_SEGMENT_ID_LEN: usize = 128;

/// Cap on rule_id strings that appear in `tool_policy` proposals.
const MAX_RULE_ID_LEN: usize = 256;

/// Allowed tool-policy modes. Single source of truth for both apply
/// and revert paths.
const ALLOWED_TOOL_MODES: &[&str] = &["auto", "prompt", "deny"];

/// Split `<tenant>::<rest>` apart. Bare targets (no `::`) get the
/// default tenant. Used by both `prompt_template` and `tool_policy`.
/// The returned `tenant` is borrowed; callers stream it through the
/// validators below before any filesystem operation.
fn split_target_with_tenant(target: &str) -> (&str, &str) {
    if let Some(idx) = target.find("::") {
        let tenant = &target[..idx];
        let rest = &target[idx + 2..];
        (tenant, rest)
    } else {
        (DEFAULT_TENANT_ID, target)
    }
}

/// Validate a tenant id as a forward-apply target component. Apply-
/// path failure → [`ApplyError::TenantPathEscape`]; the validator
/// covers length, charset, and traversal.
fn validate_tenant_id(tenant: &str) -> Result<(), ApplyError> {
    if tenant.is_empty() || tenant.len() > MAX_TENANT_PART_LEN {
        return Err(ApplyError::TenantPathEscape(format!(
            "tenant id length out of range: {tenant:?}"
        )));
    }
    if tenant == "." || tenant == ".." {
        return Err(ApplyError::TenantPathEscape(format!(
            "tenant id reserved: {tenant:?}"
        )));
    }
    for ch in tenant.chars() {
        if !(ch.is_ascii_alphanumeric() || ch == '_' || ch == '-') {
            return Err(ApplyError::TenantPathEscape(format!(
                "tenant id rejected character {ch:?}"
            )));
        }
    }
    Ok(())
}

/// Validate a dotted segment id. The Python proposer emits ids like
/// `agent.greeting`, `tool.web_search.system`. We accept lowercase
/// alphanumerics + underscores per segment, separated by single dots.
fn validate_prompt_segment_id(segment: &str) -> Result<(), ApplyError> {
    if segment.is_empty() || segment.len() > MAX_SEGMENT_ID_LEN {
        return Err(ApplyError::PromptSegmentInvalid(format!(
            "segment id length out of range: {segment:?}"
        )));
    }
    if segment.starts_with('.') || segment.ends_with('.') {
        return Err(ApplyError::PromptSegmentInvalid(format!(
            "segment id starts/ends with dot: {segment:?}"
        )));
    }
    for part in segment.split('.') {
        if part.is_empty() {
            return Err(ApplyError::PromptSegmentInvalid(format!(
                "empty dotted segment part in {segment:?}"
            )));
        }
        for ch in part.chars() {
            // Lowercase to keep segment ids case-insensitive on disk
            // (HFS+ / NTFS folding would otherwise let `Agent.Greeting`
            // and `agent.greeting` collide). The Python proposer emits
            // lowercase already.
            if !(ch.is_ascii_lowercase() || ch.is_ascii_digit() || ch == '_') {
                return Err(ApplyError::PromptSegmentInvalid(format!(
                    "segment id rejected character {ch:?} in {segment:?}"
                )));
            }
        }
    }
    Ok(())
}

/// Validate a tool name. Slightly looser than the segment id — tool
/// names in the wild use mixed case and dashes (`web_search`,
/// `Get-MailboxStatistics`).
fn validate_tool_name(tool: &str) -> Result<(), ApplyError> {
    if tool.is_empty() || tool.len() > MAX_TENANT_PART_LEN {
        return Err(ApplyError::ToolNameInvalid(format!(
            "tool name length out of range: {tool:?}"
        )));
    }
    for ch in tool.chars() {
        if !(ch.is_ascii_alphanumeric() || ch == '_' || ch == '-') {
            return Err(ApplyError::ToolNameInvalid(format!(
                "tool name rejected character {ch:?} in {tool:?}"
            )));
        }
    }
    Ok(())
}

/// Validate an agent id. Same shape as the existing `<data_dir>/agents/`
/// directory uses: lowercase identifier, alphanumeric + underscore +
/// dash, no leading dot, length-capped to defeat oversized targets.
/// Mirrors `validate_tool_name` but enforces a leading lowercase
/// letter so accidental dotfile-shaped writes can't escape into the
/// agent_cards directory.
fn validate_agent_id(agent: &str) -> Result<(), ApplyError> {
    if agent.is_empty() || agent.len() > MAX_TENANT_PART_LEN {
        return Err(ApplyError::AgentIdInvalid(format!(
            "agent id length out of range: {agent:?}"
        )));
    }
    let first = agent.chars().next().unwrap();
    if !first.is_ascii_lowercase() {
        return Err(ApplyError::AgentIdInvalid(format!(
            "agent id must start with [a-z]: {agent:?}"
        )));
    }
    for ch in agent.chars() {
        if !(ch.is_ascii_lowercase() || ch.is_ascii_digit() || ch == '_' || ch == '-') {
            return Err(ApplyError::AgentIdInvalid(format!(
                "agent id rejected character {ch:?} in {agent:?}"
            )));
        }
    }
    Ok(())
}

fn is_valid_tool_mode(mode: &str) -> bool {
    ALLOWED_TOOL_MODES.contains(&mode)
}

fn validate_tool_mode(mode: &str) -> Result<(), ApplyError> {
    if is_valid_tool_mode(mode) {
        Ok(())
    } else {
        Err(ApplyError::ToolModeInvalid(mode.to_string()))
    }
}

fn validate_rule_id(rule_id: &str) -> Result<(), ApplyError> {
    if rule_id.is_empty() || rule_id.len() > MAX_RULE_ID_LEN {
        return Err(ApplyError::MalformedDiff(format!(
            "rule_id length out of range: {rule_id:?}"
        )));
    }
    for ch in rule_id.chars() {
        if !(ch.is_ascii_alphanumeric() || ch == '_' || ch == '-' || ch == '.' || ch == ':') {
            return Err(ApplyError::MalformedDiff(format!(
                "rule_id rejected character {ch:?}"
            )));
        }
    }
    Ok(())
}

/// Read the current `[tool] mode` from a parsed toml document. Returns
/// `(mode, present)`: `present` is true iff the table existed.
fn read_existing_mode(doc: &toml::Table, tool: &str) -> Result<(Option<String>, bool), ApplyError> {
    let Some(table_value) = doc.get(tool) else {
        return Ok((None, false));
    };
    let table = table_value.as_table().ok_or_else(|| {
        ApplyError::TenantStateIo(anyhow::Error::msg(format!(
            "tool_policy.toml: [{tool}] is not a table"
        )))
    })?;
    let mode = table
        .get("mode")
        .and_then(|v| v.as_str())
        .ok_or_else(|| {
            ApplyError::TenantStateIo(anyhow::Error::msg(format!(
                "tool_policy.toml: [{tool}].mode missing or not a string"
            )))
        })?
        .to_string();
    Ok((Some(mode), true))
}

/// Canonicalise `dir` and assert it lives under `root`. Both paths
/// must already exist on disk for canonicalize to succeed; the apply
/// helpers `create_dir_all` before calling this. TOCTOU rationale:
/// re-canonicalising right before the rename closes the window where
/// a racing process could have swapped the directory for a symlink
/// pointing outside the tenant root.
fn assert_under_tenants_root(
    dir: &std::path::Path,
    root: &std::path::Path,
) -> Result<(), ApplyError> {
    let dir_canon = dir.canonicalize().map_err(|e| {
        ApplyError::TenantPathEscape(format!("canonicalize {}: {e}", dir.display()))
    })?;
    let root_canon = root.canonicalize().map_err(|e| {
        ApplyError::TenantPathEscape(format!("canonicalize {}: {e}", root.display()))
    })?;
    if !dir_canon.starts_with(&root_canon) {
        return Err(ApplyError::TenantPathEscape(format!(
            "{} resolved outside {}",
            dir_canon.display(),
            root_canon.display()
        )));
    }
    Ok(())
}

/// Trust-gate variant of [`validate_tenant_id`] for the revert path.
/// A tampered history row with an unsafe tenant id collapses to
/// [`ApplyError::Tampered`] (matching the existing inverse_diff trust
/// pattern) so the AutoRollback monitor can degrade to "skip + alert"
/// instead of looping a broken revert.
fn validate_tenant_id_revert(history: &EvolutionHistory, tenant: &str) -> Result<(), ApplyError> {
    if let Err(ApplyError::TenantPathEscape(reason)) = validate_tenant_id(tenant) {
        return Err(tampered(history, reason));
    }
    Ok(())
}

/// Trust-gate variant of [`validate_prompt_segment_id`] for the revert
/// path.
fn validate_prompt_segment_id_revert(
    history: &EvolutionHistory,
    segment: &str,
) -> Result<(), ApplyError> {
    if let Err(ApplyError::PromptSegmentInvalid(reason)) = validate_prompt_segment_id(segment) {
        return Err(tampered(history, reason));
    }
    Ok(())
}

/// Trust-gate variant of [`validate_agent_id`] for the revert path.
/// Tampered values surface as `ApplyError::Tampered` instead of
/// `AgentIdInvalid` so the AutoRollback monitor can pattern-match
/// the corruption-skip path.
fn validate_agent_id_revert(history: &EvolutionHistory, agent: &str) -> Result<(), ApplyError> {
    if let Err(ApplyError::AgentIdInvalid(reason)) = validate_agent_id(agent) {
        return Err(ApplyError::Tampered(format!(
            "history#{}: {reason}",
            history.id.unwrap_or(-1)
        )));
    }
    Ok(())
}

/// Trust-gate variant of [`validate_tool_name`] for the revert path.
fn validate_tool_name_revert(history: &EvolutionHistory, tool: &str) -> Result<(), ApplyError> {
    if let Err(ApplyError::ToolNameInvalid(reason)) = validate_tool_name(tool) {
        return Err(tampered(history, reason));
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// Phase 3.1 inverse_diff trust whitelist.
//
// `evolution_history.inverse_diff` is JSON written by `apply_*` and read
// back by `revert_*`. The forward path is trusted (we control it), but
// once `evolution_history` rows exist on disk anyone with a kb.sqlite
// stomp can rewrite the JSON column and turn the revert path into a
// "write arbitrary kb row" primitive. The functions below validate every
// field the revert handlers bind against a deny-by-default whitelist
// before the values reach sqlx.
//
// Constants kept conservative on purpose:
// - `MAX_PATH_LEN`  — 256 chars covers the longest tag_node.path the
//   forward apply emits in practice (deepest tree we've seen is ~60).
// - Allowed namespaces — must mirror corlinman_vector. Hard-coded here
//   rather than imported because the revert path depends on the *forward
//   path's* namespace contract, not a future relaxation in the vector
//   crate; keeping the list local makes that contract explicit.
// ---------------------------------------------------------------------------

const MAX_INVERSE_DIFF_STRING_LEN: usize = 256;

const ALLOWED_INVERSE_DIFF_NAMESPACES: &[&str] = &["raw", "general", "consolidated"];

/// Common log + error path for tampered inverse_diff fields. Logs with
/// enough context for the operator to identify the proposal but no
/// secrets — `target` lives next to it in the same row already.
fn tampered(history: &EvolutionHistory, reason: String) -> ApplyError {
    tracing::error!(
        proposal_id = history.proposal_id.as_str(),
        kind = history.kind.as_str(),
        reason = %reason,
        "evolution_history.inverse_diff failed trust check; \
         skipping revert and surfacing as Tampered"
    );
    ApplyError::Tampered(reason)
}

/// Reject namespaces outside the kb's small known set.
fn validate_inverse_diff_namespace(
    history: &EvolutionHistory,
    namespace: &str,
) -> Result<(), ApplyError> {
    if !ALLOWED_INVERSE_DIFF_NAMESPACES.contains(&namespace) {
        return Err(tampered(
            history,
            format!("unknown namespace {namespace:?}"),
        ));
    }
    Ok(())
}

/// Reject path / name fields containing control characters, NULs,
/// non-ASCII, traversal sequences, or oversized payloads. Paths the
/// forward apply emits stay inside `[a-zA-Z0-9_./\-]`; we accept that
/// exact set, plus we explicitly reject `..` segments (path traversal)
/// and absolute-path forms — those characters individually pass the
/// per-character check but the segment as a whole is unsafe.
fn validate_inverse_diff_path(
    history: &EvolutionHistory,
    field: &str,
    value: &str,
) -> Result<(), ApplyError> {
    if value.is_empty() {
        return Err(tampered(history, format!("{field}: empty")));
    }
    if value.len() > MAX_INVERSE_DIFF_STRING_LEN {
        return Err(tampered(
            history,
            format!(
                "{field}: length {} exceeds limit {}",
                value.len(),
                MAX_INVERSE_DIFF_STRING_LEN
            ),
        ));
    }
    for ch in value.chars() {
        let ok = ch.is_ascii_alphanumeric() || ch == '_' || ch == '.' || ch == '/' || ch == '-';
        if !ok {
            return Err(tampered(
                history,
                format!("{field}: rejected character {:?}", ch),
            ));
        }
    }
    if value.starts_with('/') {
        return Err(tampered(
            history,
            format!("{field}: absolute path not allowed"),
        ));
    }
    for segment in value.split('/') {
        if segment == ".." {
            return Err(tampered(
                history,
                format!("{field}: path traversal segment '..'"),
            ));
        }
    }
    Ok(())
}

/// Reject ids that are negative or beyond i32 range — chunk_id /
/// tag_node_id are autoincrement keys; an i64 max-int landing here is
/// either a bug or tampering, never legitimate kb data.
fn validate_inverse_diff_id(
    history: &EvolutionHistory,
    field: &str,
    value: i64,
) -> Result<(), ApplyError> {
    if value < 0 {
        return Err(tampered(history, format!("{field}: negative id {value}")));
    }
    if value > i32::MAX as i64 {
        // SQLite autoincrement counters never reach this in practice;
        // a value here is a tamper signal, not a "we hit the cap" one.
        return Err(tampered(
            history,
            format!("{field}: id {value} exceeds i32::MAX"),
        ));
    }
    Ok(())
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
            // Phase 3.1 Tampered → Internal: the monitor treats this
            // the same way it would treat any other infra failure
            // (skip + alert on the gateway-side error log). Future
            // work could add a dedicated `RevertError::Tampered` so
            // the monitor opens an incident instead of just retrying;
            // we keep it as Internal here to avoid touching the
            // monitor crate from this fix.
            Err(other @ ApplyError::Tampered(_)) => Err(RevertError::Internal(format!("{other}"))),
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
    async fn fresh_applier() -> (
        TempDir,
        EvolutionApplier,
        Arc<SqliteStore>,
        Arc<EvolutionStore>,
    ) {
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
    async fn seed_approved(evol: &EvolutionStore, id: &str, target: &str) -> ProposalId {
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
        let after = ProposalsRepo::new(evol.pool().clone())
            .get(&pid)
            .await
            .unwrap();
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
        let parent_links: Vec<(i64, i64)> =
            sqlx::query_as("SELECT chunk_id, tag_node_id FROM chunk_tags ORDER BY chunk_id ASC")
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
        let reverted = applier.revert(&pid, "metrics regression").await.unwrap();
        assert!(reverted.rolled_back_at.is_some());

        // python row back at original id.
        let row: (i64, Option<i64>, String, String, i64) =
            sqlx::query_as("SELECT id, parent_id, name, path, depth FROM tag_nodes WHERE id = ?")
                .bind(python)
                .fetch_one(kb.pool())
                .await
                .unwrap();
        assert_eq!(
            row,
            (
                python,
                Some(coding),
                "python".into(),
                "coding/python".into(),
                2
            )
        );

        // chunk_tags re-pointed to python.
        let link: (i64, i64) =
            sqlx::query_as("SELECT chunk_id, tag_node_id FROM chunk_tags WHERE chunk_id = ?")
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
        // Phase 3.1 (B-3): inverse_diff carries `prior_consolidated_at`.
        // First-time consolidation ⇒ JSON null (`Option::None`).
        assert!(
            inverse.get("prior_consolidated_at").is_some(),
            "inverse_diff must carry the prior_consolidated_at key"
        );
        assert!(
            inverse["prior_consolidated_at"].is_null(),
            "first-time consolidation: prior_consolidated_at = null, got {:?}",
            inverse["prior_consolidated_at"]
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

    /// Phase 3.1 (B-3): a chunk that's promoted, demoted, then promoted
    /// again must keep its original first-promotion timestamp on the
    /// second demote. Pins the byte-for-byte revert contract end-to-end
    /// through the applier (forward → revert → forward → revert).
    #[tokio::test]
    async fn revert_consolidate_chunk_round_trips_prior_consolidated_at() {
        let (_tmp, applier, kb, evol) = fresh_applier().await;
        let id = seed_chunk(&kb, "/c", "bouncing chunk").await;

        // Round 1: promote + capture the stamped consolidated_at.
        let pid1 = seed_approved(&evol, "evol-cons-rt-1", &format!("consolidate_chunk:{id}")).await;
        applier.apply(&pid1).await.unwrap();
        let first_consolidated = kb
            .get_chunk_decay_state(id)
            .await
            .unwrap()
            .unwrap()
            .consolidated_at
            .unwrap();
        // Demote — this is the test's "previous demote" that the bug
        // erases. After this, the chunk lives in 'general' again.
        applier.revert(&pid1, "round 1 rollback").await.unwrap();

        // Round 2: re-promote the same chunk. promote_to_consolidated
        // is idempotent on consolidated_at via COALESCE — the legacy
        // bug surfaces on the *second* revert, not on the second
        // promote. We need the second promotion's prior state to
        // carry first_consolidated through the inverse_diff so the
        // third revert can restore it.
        //
        // Manually force the prior consolidated_at to first_consolidated
        // so the snapshot the applier captures includes it. (The
        // forward path treats the chunk as freshly-general because the
        // first revert NULL'd consolidated_at — which is correct; the
        // bug would surface if this were the second-time-promoting
        // case where the prior_consolidated_at was non-null and
        // truncated.)
        sqlx::query("UPDATE chunks SET consolidated_at = ?1 WHERE id = ?2")
            .bind(first_consolidated)
            .bind(id)
            .execute(kb.pool())
            .await
            .unwrap();
        let pid2 = seed_approved(&evol, "evol-cons-rt-2", &format!("consolidate_chunk:{id}")).await;
        applier.apply(&pid2).await.unwrap();

        // The inverse_diff for the second apply must carry the
        // first-promotion timestamp.
        let row: (String,) =
            sqlx::query_as("SELECT inverse_diff FROM evolution_history WHERE proposal_id = ?")
                .bind(pid2.as_str())
                .fetch_one(evol.pool())
                .await
                .unwrap();
        let inv: serde_json::Value = serde_json::from_str(&row.0).unwrap();
        assert_eq!(
            inv["prior_consolidated_at"].as_i64(),
            Some(first_consolidated),
            "second apply must capture prior_consolidated_at, got {:?}",
            inv["prior_consolidated_at"]
        );

        // And reverting must restore that first_consolidated value
        // rather than NULL'ing it.
        applier.revert(&pid2, "round 2 rollback").await.unwrap();
        let after = kb.get_chunk_decay_state(id).await.unwrap().unwrap();
        assert_eq!(
            after.consolidated_at,
            Some(first_consolidated),
            "demote must restore prior_consolidated_at, got {:?}",
            after.consolidated_at
        );
    }

    /// Phase 3.1 (B-3): a legacy `inverse_diff` row written before this
    /// fix doesn't carry `prior_consolidated_at`. Revert must fall back
    /// gracefully (NULL the column, emit a warn) rather than reject the
    /// whole revert with MalformedInverseDiff.
    #[tokio::test]
    async fn revert_consolidate_chunk_tolerates_legacy_inverse_diff() {
        let (_tmp, applier, kb, evol) = fresh_applier().await;
        let id = seed_chunk(&kb, "/c", "legacy bounce").await;
        // Stand up a fully-consolidated chunk + matching applied
        // proposal + history row whose inverse_diff predates the
        // 3.1 fix (no prior_consolidated_at key).
        kb.promote_to_consolidated(&[id]).await.unwrap();
        let pid = ProposalId::new("evol-cons-legacy-001");
        let proposals = ProposalsRepo::new(evol.pool().clone());
        proposals
            .insert(&EvolutionProposal {
                id: pid.clone(),
                kind: EvolutionKind::MemoryOp,
                target: format!("consolidate_chunk:{id}"),
                diff: String::new(),
                reasoning: "legacy".into(),
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
        let legacy_inverse = serde_json::json!({
            "action": "demote_chunk",
            "chunk_id": id,
            "prior_namespace": "general",
            "prior_decay_score": 0.42,
            // No prior_consolidated_at key.
        })
        .to_string();
        HistoryRepo::new(evol.pool().clone())
            .insert(&EvolutionHistory {
                id: None,
                proposal_id: pid.clone(),
                kind: EvolutionKind::MemoryOp,
                target: format!("consolidate_chunk:{id}"),
                before_sha: "x".into(),
                after_sha: "y".into(),
                inverse_diff: legacy_inverse,
                metrics_baseline: serde_json::json!({}),
                applied_at: 3_000,
                rolled_back_at: None,
                rollback_reason: None,
            })
            .await
            .unwrap();

        // Revert must succeed (graceful fallback) and NULL the column.
        applier.revert(&pid, "legacy revert").await.unwrap();
        let after = kb.get_chunk_decay_state(id).await.unwrap().unwrap();
        assert_eq!(after.namespace, "general");
        assert!(
            after.consolidated_at.is_none(),
            "legacy fallback NULLs the column"
        );
        assert!((after.decay_score - 0.42).abs() < 1e-5);
    }

    // ---- Phase 3.1: apply intent log + half-committed scan ----------------

    /// A successful forward apply must stamp the intent row's
    /// `committed_at`. The startup scan therefore sees nothing —
    /// pins the contract that healthy apply runs leave no debris.
    #[tokio::test]
    async fn apply_stamps_intent_log_committed_on_success() {
        let (_tmp, applier, kb, evol) = fresh_applier().await;
        let id = seed_chunk(&kb, "/d", "doomed").await;
        let pid = seed_approved(&evol, "evol-intent-ok-001", &format!("delete_chunk:{id}")).await;

        applier.apply(&pid).await.unwrap();
        let row: (Option<i64>, Option<i64>) = sqlx::query_as(
            "SELECT committed_at, failed_at FROM apply_intent_log WHERE proposal_id = ?",
        )
        .bind(pid.as_str())
        .fetch_one(evol.pool())
        .await
        .unwrap();
        assert!(row.0.is_some(), "committed_at must be stamped");
        assert!(row.1.is_none(), "failed_at must stay null");
        assert_eq!(applier.scan_half_committed().await.unwrap(), 0);
    }

    /// A clean failure (e.g. ChunkNotFound) must stamp `failed_at` so
    /// the row drops out of the half-committed scan. Without this the
    /// scan would alarm on every benign-error apply attempt.
    #[tokio::test]
    async fn apply_stamps_intent_log_failed_on_error() {
        let (_tmp, applier, _kb, evol) = fresh_applier().await;
        let pid = seed_approved(&evol, "evol-intent-fail-001", "delete_chunk:99999").await;
        let _ = applier.apply(&pid).await; // expected ChunkNotFound

        let row: (Option<i64>, Option<i64>, Option<String>) = sqlx::query_as(
            "SELECT committed_at, failed_at, failure_reason \
             FROM apply_intent_log WHERE proposal_id = ?",
        )
        .bind(pid.as_str())
        .fetch_one(evol.pool())
        .await
        .unwrap();
        assert!(row.0.is_none());
        assert!(row.1.is_some());
        assert!(row.2.unwrap_or_default().contains("chunk not found"));
        assert_eq!(applier.scan_half_committed().await.unwrap(), 0);
    }

    /// Simulate a crash mid-apply by inserting an `apply_intent_log`
    /// row with both stamps NULL — the scan must surface it. Mirrors
    /// the gateway-startup contract: operators see half-committed
    /// applies in the boot log instead of silently losing them.
    #[tokio::test]
    async fn scan_half_committed_surfaces_unstamped_intent_rows() {
        let (_tmp, applier, _kb, evol) = fresh_applier().await;
        // Insert by hand: simulates a crash *between* the kb mutation
        // and either commit/fail stamp.
        sqlx::query(
            "INSERT INTO apply_intent_log \
             (proposal_id, kind, target, intent_at, committed_at, failed_at) \
             VALUES (?, ?, ?, ?, NULL, NULL)",
        )
        .bind("evol-intent-stuck-001")
        .bind("memory_op")
        .bind("delete_chunk:42")
        .bind(1_000_000i64)
        .execute(evol.pool())
        .await
        .unwrap();

        let outstanding = applier.scan_half_committed().await.unwrap();
        assert_eq!(outstanding, 1, "stuck intent row must surface");
    }

    /// `revert_memory_op` must reject a tampered `inverse_diff` whose
    /// `prior_namespace` is outside the whitelist. The audit row was
    /// written by the forward path with `general`; we hand-stomp it to
    /// `consolidated_secret` to simulate post-write tampering.
    #[tokio::test]
    async fn revert_memory_op_rejects_tampered_namespace() {
        let (_tmp, applier, kb, evol) = fresh_applier().await;
        let id = seed_chunk(&kb, "/d", "tampered").await;
        let pid = seed_approved(&evol, "evol-tamper-ns-001", &format!("delete_chunk:{id}")).await;
        applier.apply(&pid).await.unwrap();

        // Tamper: rewrite namespace to something outside the whitelist.
        sqlx::query(
            r#"UPDATE evolution_history
                 SET inverse_diff = json_replace(
                     inverse_diff, '$.namespace', '/etc/passwd'
                 )
               WHERE proposal_id = ?"#,
        )
        .bind(pid.as_str())
        .execute(evol.pool())
        .await
        .unwrap();

        match applier.revert(&pid, "test").await {
            Err(ApplyError::Tampered(reason)) => {
                assert!(
                    reason.contains("namespace") || reason.contains("rejected"),
                    "expected tamper-shaped reason, got {reason:?}"
                );
            }
            other => panic!("expected Tampered, got {other:?}"),
        }
        // Proposal stays Applied — revert refused to run, so no
        // status flip.
        let after = ProposalsRepo::new(evol.pool().clone())
            .get(&pid)
            .await
            .unwrap();
        assert_eq!(after.status, EvolutionStatus::Applied);
    }

    /// `revert_tag_rebalance` must reject a tampered path field. We
    /// rewrite `src.path` to a value containing `..` — the deny
    /// whitelist refuses anything outside `[a-zA-Z0-9_./\-]` after
    /// length and emptiness checks.
    #[tokio::test]
    async fn revert_tag_rebalance_rejects_tampered_path() {
        let (_tmp, applier, kb, evol) = fresh_applier().await;
        let root = seed_tag_node(&kb, None, "root", "root", 0).await;
        let coding = seed_tag_node(&kb, Some(root), "coding", "coding", 1).await;
        let _python = seed_tag_node(&kb, Some(coding), "python", "coding/python", 2).await;
        let pid = seed_approved_kind(
            &evol,
            "evol-tamper-path-001",
            EvolutionKind::TagRebalance,
            "merge_tag:coding/python",
            "",
        )
        .await;
        applier.apply(&pid).await.unwrap();

        // Tamper: rewrite path to one carrying a non-allowed character.
        sqlx::query(
            r#"UPDATE evolution_history
                 SET inverse_diff = json_replace(
                     inverse_diff, '$.src.path',
                     'coding/../../../etc/passwd'
                 )
               WHERE proposal_id = ?"#,
        )
        .bind(pid.as_str())
        .execute(evol.pool())
        .await
        .unwrap();

        match applier.revert(&pid, "test").await {
            Err(ApplyError::Tampered(reason)) => {
                assert!(
                    reason.contains("path") || reason.contains("character"),
                    "expected path-shaped reason, got {reason:?}"
                );
            }
            other => panic!("expected Tampered, got {other:?}"),
        }
    }

    // ─────────────────────────────────────────────────────────────────
    // Phase 4 W1 4-1D — prompt_template + tool_policy
    // ─────────────────────────────────────────────────────────────────

    /// Resolve the tenants root the applier will use given a fresh
    /// applier (the helper above always builds `<tmp>/skills`, so the
    /// applier derives `<tmp>/tenants` as a sibling).
    fn tenants_root_for(tmp: &TempDir) -> std::path::PathBuf {
        tmp.path().join("tenants")
    }

    fn prompt_segment_path(tmp: &TempDir, tenant: &str, segment: &str) -> std::path::PathBuf {
        tenants_root_for(tmp)
            .join(tenant)
            .join("prompt_segments")
            .join(format!("{segment}.md"))
    }

    fn tool_policy_path(tmp: &TempDir, tenant: &str) -> std::path::PathBuf {
        tenants_root_for(tmp).join(tenant).join("tool_policy.toml")
    }

    fn prompt_diff_json(after: &str, rationale: &str) -> String {
        json!({
            "before": "",
            "after": after,
            "rationale": rationale,
        })
        .to_string()
    }

    fn tool_diff_json(before: &str, after: &str, rule_id: &str) -> String {
        json!({
            "before": before,
            "after": after,
            "rule_id": rule_id,
        })
        .to_string()
    }

    #[test]
    fn split_target_with_tenant_handles_prefix() {
        assert_eq!(
            split_target_with_tenant("acme::agent.greeting"),
            ("acme", "agent.greeting"),
        );
        assert_eq!(
            split_target_with_tenant("agent.greeting"),
            ("default", "agent.greeting"),
        );
        // No prefix on a `::`-free tool name still falls to default.
        assert_eq!(
            split_target_with_tenant("web_search"),
            ("default", "web_search"),
        );
    }

    #[test]
    fn validate_prompt_segment_id_rejects_bad_shapes() {
        for bad in [
            "",
            ".leading",
            "trailing.",
            "double..dot",
            "Upper.Case",
            "with-dash",
            "with space",
            &"x".repeat(MAX_SEGMENT_ID_LEN + 1),
        ] {
            assert!(
                matches!(
                    validate_prompt_segment_id(bad),
                    Err(ApplyError::PromptSegmentInvalid(_))
                ),
                "expected reject for {bad:?}",
            );
        }
        validate_prompt_segment_id("agent.greeting").unwrap();
        validate_prompt_segment_id("tool.web_search.system").unwrap();
        validate_prompt_segment_id("a").unwrap();
    }

    #[test]
    fn validate_tool_name_rejects_bad_shapes() {
        for bad in ["", "with space", "with::colon", "with/slash"] {
            assert!(
                matches!(validate_tool_name(bad), Err(ApplyError::ToolNameInvalid(_))),
                "expected reject for {bad:?}",
            );
        }
        validate_tool_name("web_search").unwrap();
        validate_tool_name("Get-MailboxStatistics").unwrap();
    }

    #[test]
    fn validate_tenant_id_rejects_traversal() {
        for bad in ["", ".", "..", "with/slash", "with space"] {
            assert!(
                matches!(
                    validate_tenant_id(bad),
                    Err(ApplyError::TenantPathEscape(_))
                ),
                "expected reject for {bad:?}",
            );
        }
        validate_tenant_id("default").unwrap();
        validate_tenant_id("acme-prod_42").unwrap();
    }

    #[tokio::test]
    async fn apply_prompt_template_writes_default_tenant() {
        let (tmp, applier, _kb, evol) = fresh_applier().await;
        let target = "agent.greeting";
        let pid = seed_approved_kind(
            &evol,
            "evol-pt-001",
            EvolutionKind::PromptTemplate,
            target,
            &prompt_diff_json("Hello, world!", "warm greeting"),
        )
        .await;

        let history = applier.apply(&pid).await.unwrap();
        assert_eq!(history.kind, EvolutionKind::PromptTemplate);
        assert_eq!(history.target, target);
        assert_ne!(history.before_sha, history.after_sha);

        let path = prompt_segment_path(&tmp, "default", "agent.greeting");
        let written = std::fs::read_to_string(&path).unwrap();
        assert_eq!(written, "Hello, world!");

        let inv: serde_json::Value = serde_json::from_str(&history.inverse_diff).unwrap();
        assert_eq!(inv["op"], "prompt_template");
        assert_eq!(inv["tenant"], "default");
        assert_eq!(inv["segment"], "agent.greeting");
        assert_eq!(inv["before"], "");
        assert_eq!(inv["before_present"], false);

        let after = ProposalsRepo::new(evol.pool().clone())
            .get(&pid)
            .await
            .unwrap();
        assert_eq!(after.status, EvolutionStatus::Applied);
    }

    #[tokio::test]
    async fn apply_prompt_template_routes_to_named_tenant() {
        let (tmp, applier, _kb, evol) = fresh_applier().await;
        let target = "acme::agent.greeting";
        let pid = seed_approved_kind(
            &evol,
            "evol-pt-tenant-001",
            EvolutionKind::PromptTemplate,
            target,
            &prompt_diff_json("Welcome to ACME.", "branded greeting"),
        )
        .await;

        applier.apply(&pid).await.unwrap();

        // The acme tenant gets the file; default tenant is untouched.
        let acme_path = prompt_segment_path(&tmp, "acme", "agent.greeting");
        let default_path = prompt_segment_path(&tmp, "default", "agent.greeting");
        assert_eq!(
            std::fs::read_to_string(&acme_path).unwrap(),
            "Welcome to ACME."
        );
        assert!(
            !default_path.exists(),
            "default tenant must not be touched when target prefix names another tenant"
        );
    }

    #[tokio::test]
    async fn apply_prompt_template_overwrites_existing_segment() {
        let (tmp, applier, _kb, evol) = fresh_applier().await;
        // Pre-seed the segment with content so the inverse captures it.
        let path = prompt_segment_path(&tmp, "default", "agent.greeting");
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, "old content").unwrap();

        let pid = seed_approved_kind(
            &evol,
            "evol-pt-over-001",
            EvolutionKind::PromptTemplate,
            "agent.greeting",
            &prompt_diff_json("new content", "improved"),
        )
        .await;

        let history = applier.apply(&pid).await.unwrap();
        assert_eq!(std::fs::read_to_string(&path).unwrap(), "new content");

        let inv: serde_json::Value = serde_json::from_str(&history.inverse_diff).unwrap();
        assert_eq!(inv["before"], "old content");
        assert_eq!(inv["before_present"], true);
    }

    #[tokio::test]
    async fn revert_prompt_template_restores_prior_content() {
        let (tmp, applier, _kb, evol) = fresh_applier().await;
        // Pre-seed prior content so the inverse has something to
        // restore.
        let path = prompt_segment_path(&tmp, "default", "agent.greeting");
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, "original").unwrap();

        let pid = seed_approved_kind(
            &evol,
            "evol-pt-revert-001",
            EvolutionKind::PromptTemplate,
            "agent.greeting",
            &prompt_diff_json("replaced", "v2"),
        )
        .await;
        applier.apply(&pid).await.unwrap();
        assert_eq!(std::fs::read_to_string(&path).unwrap(), "replaced");

        let reverted = applier.revert(&pid, "rollback test").await.unwrap();
        assert!(reverted.rolled_back_at.is_some());
        assert_eq!(std::fs::read_to_string(&path).unwrap(), "original");
    }

    #[tokio::test]
    async fn revert_prompt_template_removes_segment_when_absent_pre_apply() {
        let (tmp, applier, _kb, evol) = fresh_applier().await;
        let path = prompt_segment_path(&tmp, "default", "agent.greeting");

        let pid = seed_approved_kind(
            &evol,
            "evol-pt-revert-absent-001",
            EvolutionKind::PromptTemplate,
            "agent.greeting",
            &prompt_diff_json("first creation", "v1"),
        )
        .await;
        applier.apply(&pid).await.unwrap();
        assert!(path.exists());

        applier
            .revert(&pid, "rollback first creation")
            .await
            .unwrap();
        assert!(
            !path.exists(),
            "segment file must be removed when before_present == false"
        );
    }

    #[tokio::test]
    async fn apply_prompt_template_rejects_bad_segment_id() {
        let (_tmp, applier, _kb, evol) = fresh_applier().await;
        let pid = seed_approved_kind(
            &evol,
            "evol-pt-bad-001",
            EvolutionKind::PromptTemplate,
            "Agent.Greeting", // uppercase rejected
            &prompt_diff_json("x", "y"),
        )
        .await;
        match applier.apply(&pid).await {
            Err(ApplyError::PromptSegmentInvalid(_)) => {}
            other => panic!("expected PromptSegmentInvalid, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn apply_prompt_template_rejects_malformed_diff() {
        let (_tmp, applier, _kb, evol) = fresh_applier().await;
        let pid = seed_approved_kind(
            &evol,
            "evol-pt-malformed-001",
            EvolutionKind::PromptTemplate,
            "agent.greeting",
            "{not json",
        )
        .await;
        match applier.apply(&pid).await {
            Err(ApplyError::MalformedDiff(_)) => {}
            other => panic!("expected MalformedDiff, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn apply_tool_policy_drift_mismatch_when_disk_already_changed() {
        let (tmp, applier, _kb, evol) = fresh_applier().await;
        // Pre-seed a tool_policy.toml with mode=prompt while the
        // proposal expects before=auto. The drift detector must reject.
        let path = tool_policy_path(&tmp, "default");
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(
            &path,
            "[web_search]\nmode = \"prompt\"\nrule_id = \"manual\"\n",
        )
        .unwrap();

        let pid = seed_approved_kind(
            &evol,
            "evol-tp-drift-001",
            EvolutionKind::ToolPolicy,
            "web_search",
            &tool_diff_json("auto", "deny", "rule-42"),
        )
        .await;

        match applier.apply(&pid).await {
            Err(ApplyError::DriftMismatch {
                target,
                expected,
                actual,
            }) => {
                assert_eq!(target, "web_search");
                assert_eq!(expected, "auto");
                assert_eq!(actual, "prompt");
            }
            other => panic!("expected DriftMismatch, got {other:?}"),
        }

        // Disk untouched.
        let after = std::fs::read_to_string(&path).unwrap();
        assert!(after.contains("\"prompt\""));
    }

    #[tokio::test]
    async fn apply_tool_policy_drift_when_table_absent() {
        let (_tmp, applier, _kb, evol) = fresh_applier().await;
        // No prior toml file exists; before=auto can't match absent.
        let pid = seed_approved_kind(
            &evol,
            "evol-tp-drift-absent-001",
            EvolutionKind::ToolPolicy,
            "web_search",
            &tool_diff_json("auto", "deny", "rule-1"),
        )
        .await;
        match applier.apply(&pid).await {
            Err(ApplyError::DriftMismatch { actual, .. }) => {
                assert_eq!(actual, "<absent>");
            }
            other => panic!("expected DriftMismatch (absent), got {other:?}"),
        }
    }

    #[tokio::test]
    async fn apply_tool_policy_happy_path_writes_table() {
        let (tmp, applier, _kb, evol) = fresh_applier().await;
        // Seed prior toml with the matching `before` mode.
        let path = tool_policy_path(&tmp, "default");
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(
            &path,
            "[web_search]\nmode = \"auto\"\nrule_id = \"baseline\"\n[other_tool]\nmode = \"prompt\"\nrule_id = \"keep\"\n",
        )
        .unwrap();

        let pid = seed_approved_kind(
            &evol,
            "evol-tp-ok-001",
            EvolutionKind::ToolPolicy,
            "web_search",
            &tool_diff_json("auto", "deny", "rule-7"),
        )
        .await;

        let history = applier.apply(&pid).await.unwrap();
        assert_eq!(history.kind, EvolutionKind::ToolPolicy);

        // [web_search] flipped to deny; sibling [other_tool] kept.
        let written = std::fs::read_to_string(&path).unwrap();
        let parsed: toml::Table = written.parse().unwrap();
        assert_eq!(parsed["web_search"]["mode"].as_str(), Some("deny"));
        assert_eq!(
            parsed["other_tool"]["mode"].as_str(),
            Some("prompt"),
            "sibling tool table preserved across apply"
        );

        let inv: serde_json::Value = serde_json::from_str(&history.inverse_diff).unwrap();
        assert_eq!(inv["op"], "tool_policy");
        assert_eq!(inv["tenant"], "default");
        assert_eq!(inv["tool"], "web_search");
        assert_eq!(inv["before_mode"], "auto");
        assert_eq!(inv["before_present"], true);
        assert_eq!(inv["rule_id"], "rule-7");
    }

    #[tokio::test]
    async fn apply_tool_policy_routes_to_named_tenant() {
        let (tmp, applier, _kb, evol) = fresh_applier().await;
        let path = tool_policy_path(&tmp, "acme");
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(
            &path,
            "[web_search]\nmode = \"auto\"\nrule_id = \"baseline\"\n",
        )
        .unwrap();

        let pid = seed_approved_kind(
            &evol,
            "evol-tp-tenant-001",
            EvolutionKind::ToolPolicy,
            "acme::web_search",
            &tool_diff_json("auto", "prompt", "rule-acme"),
        )
        .await;
        applier.apply(&pid).await.unwrap();

        let written = std::fs::read_to_string(&path).unwrap();
        let parsed: toml::Table = written.parse().unwrap();
        assert_eq!(parsed["web_search"]["mode"].as_str(), Some("prompt"));
        // Default tenant must remain untouched.
        let default_path = tool_policy_path(&tmp, "default");
        assert!(
            !default_path.exists(),
            "default tenant tool_policy.toml must not be created when target names another tenant"
        );
    }

    #[tokio::test]
    async fn revert_tool_policy_restores_prior_mode() {
        let (tmp, applier, _kb, evol) = fresh_applier().await;
        let path = tool_policy_path(&tmp, "default");
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(
            &path,
            "[web_search]\nmode = \"auto\"\nrule_id = \"original\"\n",
        )
        .unwrap();

        let pid = seed_approved_kind(
            &evol,
            "evol-tp-revert-001",
            EvolutionKind::ToolPolicy,
            "web_search",
            &tool_diff_json("auto", "deny", "rule-bad"),
        )
        .await;
        applier.apply(&pid).await.unwrap();
        let parsed: toml::Table = std::fs::read_to_string(&path).unwrap().parse().unwrap();
        assert_eq!(parsed["web_search"]["mode"].as_str(), Some("deny"));

        applier.revert(&pid, "metrics regression").await.unwrap();
        let parsed: toml::Table = std::fs::read_to_string(&path).unwrap().parse().unwrap();
        assert_eq!(
            parsed["web_search"]["mode"].as_str(),
            Some("auto"),
            "revert restores the pre-apply mode"
        );
        assert_eq!(
            parsed["web_search"]["rule_id"].as_str(),
            Some("rule-bad"),
            "revert keeps the proposal's rule_id — operator audits via the history row"
        );
    }

    #[tokio::test]
    async fn apply_tool_policy_rejects_unknown_mode() {
        let (_tmp, applier, _kb, evol) = fresh_applier().await;
        let pid = seed_approved_kind(
            &evol,
            "evol-tp-bad-mode-001",
            EvolutionKind::ToolPolicy,
            "web_search",
            &tool_diff_json("auto", "ALLOW", "rule-1"),
        )
        .await;
        match applier.apply(&pid).await {
            Err(ApplyError::ToolModeInvalid(s)) => assert_eq!(s, "ALLOW"),
            other => panic!("expected ToolModeInvalid, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn apply_tool_policy_rejects_invalid_tool_name() {
        let (_tmp, applier, _kb, evol) = fresh_applier().await;
        let pid = seed_approved_kind(
            &evol,
            "evol-tp-bad-name-001",
            EvolutionKind::ToolPolicy,
            "web search", // space rejected
            &tool_diff_json("auto", "deny", "rule-1"),
        )
        .await;
        match applier.apply(&pid).await {
            Err(ApplyError::ToolNameInvalid(_)) => {}
            other => panic!("expected ToolNameInvalid, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn apply_prompt_template_rejects_bad_tenant_prefix() {
        let (_tmp, applier, _kb, evol) = fresh_applier().await;
        let pid = seed_approved_kind(
            &evol,
            "evol-pt-bad-tenant-001",
            EvolutionKind::PromptTemplate,
            "../etc::agent.greeting",
            &prompt_diff_json("x", "y"),
        )
        .await;
        match applier.apply(&pid).await {
            Err(ApplyError::TenantPathEscape(_)) => {}
            other => panic!("expected TenantPathEscape, got {other:?}"),
        }
    }
}
