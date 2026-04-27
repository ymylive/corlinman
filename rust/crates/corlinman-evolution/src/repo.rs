//! Async repos for the three evolution tables.
//!
//! Phase 2 keeps these as concrete `Sqlite*Repo` structs over `SqlitePool`
//! rather than traits — there's exactly one backing store and adding a
//! trait now buys nothing. Make them traits when (if) we federate.
//!
//! Time-handling convention: callers pass `unix_now_ms()` from
//! [`crate::now_ms()`] or supply explicit timestamps for replay/test paths.

use serde_json::Value as Json;
use sqlx::{Row, SqlitePool};

use crate::types::{
    EvolutionHistory, EvolutionKind, EvolutionProposal, EvolutionRisk, EvolutionSignal,
    EvolutionStatus, ProposalId, ShadowMetrics, SignalSeverity,
};

#[derive(Debug, thiserror::Error)]
pub enum RepoError {
    #[error("sqlite: {0}")]
    Sqlite(#[from] sqlx::Error),
    #[error("malformed json column '{column}': {source}")]
    MalformedJson {
        column: &'static str,
        #[source]
        source: serde_json::Error,
    },
    #[error("malformed enum '{column}': {value}")]
    MalformedEnum { column: &'static str, value: String },
    #[error("not found: {0}")]
    NotFound(String),
}

// ---------------------------------------------------------------------------
// Signals
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
pub struct SignalsRepo {
    pool: SqlitePool,
}

impl SignalsRepo {
    pub fn new(pool: SqlitePool) -> Self {
        Self { pool }
    }

    /// Insert one signal. Returns the autoincrement id.
    pub async fn insert(&self, signal: &EvolutionSignal) -> Result<i64, RepoError> {
        let payload = serde_json::to_string(&signal.payload_json).map_err(|source| {
            RepoError::MalformedJson {
                column: "payload_json",
                source,
            }
        })?;
        let row = sqlx::query(
            r#"INSERT INTO evolution_signals
                 (event_kind, target, severity, payload_json, trace_id, session_id, observed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               RETURNING id"#,
        )
        .bind(&signal.event_kind)
        .bind(&signal.target)
        .bind(signal.severity.as_str())
        .bind(payload)
        .bind(&signal.trace_id)
        .bind(&signal.session_id)
        .bind(signal.observed_at)
        .fetch_one(&self.pool)
        .await?;
        Ok(row.get::<i64, _>("id"))
    }

    /// Read signals observed in `[since_ms, now]`, optionally filtered by
    /// `event_kind`. Used by the Python engine when clustering.
    pub async fn list_since(
        &self,
        since_ms: i64,
        event_kind: Option<&str>,
        limit: i64,
    ) -> Result<Vec<EvolutionSignal>, RepoError> {
        let rows = if let Some(kind) = event_kind {
            sqlx::query(
                r#"SELECT id, event_kind, target, severity, payload_json,
                          trace_id, session_id, observed_at
                   FROM evolution_signals
                   WHERE observed_at >= ? AND event_kind = ?
                   ORDER BY observed_at ASC
                   LIMIT ?"#,
            )
            .bind(since_ms)
            .bind(kind)
            .bind(limit)
            .fetch_all(&self.pool)
            .await?
        } else {
            sqlx::query(
                r#"SELECT id, event_kind, target, severity, payload_json,
                          trace_id, session_id, observed_at
                   FROM evolution_signals
                   WHERE observed_at >= ?
                   ORDER BY observed_at ASC
                   LIMIT ?"#,
            )
            .bind(since_ms)
            .bind(limit)
            .fetch_all(&self.pool)
            .await?
        };

        rows.into_iter()
            .map(|r| {
                let severity_raw: String = r.get("severity");
                let severity = severity_raw.parse::<SignalSeverity>().map_err(|_| {
                    RepoError::MalformedEnum {
                        column: "severity",
                        value: severity_raw,
                    }
                })?;
                let payload_str: String = r.get("payload_json");
                let payload_json: Json = serde_json::from_str(&payload_str).map_err(|source| {
                    RepoError::MalformedJson {
                        column: "payload_json",
                        source,
                    }
                })?;
                Ok(EvolutionSignal {
                    id: Some(r.get::<i64, _>("id")),
                    event_kind: r.get("event_kind"),
                    target: r.get("target"),
                    severity,
                    payload_json,
                    trace_id: r.get("trace_id"),
                    session_id: r.get("session_id"),
                    observed_at: r.get("observed_at"),
                })
            })
            .collect()
    }

    /// Delete signals older than `before_ms`. Returns rows affected.
    pub async fn prune_before(&self, before_ms: i64) -> Result<u64, RepoError> {
        let res = sqlx::query("DELETE FROM evolution_signals WHERE observed_at < ?")
            .bind(before_ms)
            .execute(&self.pool)
            .await?;
        Ok(res.rows_affected())
    }
}

// ---------------------------------------------------------------------------
// Proposals
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
pub struct ProposalsRepo {
    pool: SqlitePool,
}

impl ProposalsRepo {
    pub fn new(pool: SqlitePool) -> Self {
        Self { pool }
    }

    pub async fn insert(&self, proposal: &EvolutionProposal) -> Result<(), RepoError> {
        let signal_ids = serde_json::to_string(&proposal.signal_ids).map_err(|source| {
            RepoError::MalformedJson {
                column: "signal_ids",
                source,
            }
        })?;
        let trace_ids = serde_json::to_string(&proposal.trace_ids).map_err(|source| {
            RepoError::MalformedJson {
                column: "trace_ids",
                source,
            }
        })?;
        let shadow_metrics = match &proposal.shadow_metrics {
            Some(m) => {
                Some(
                    serde_json::to_string(m).map_err(|source| RepoError::MalformedJson {
                        column: "shadow_metrics",
                        source,
                    })?,
                )
            }
            None => None,
        };

        sqlx::query(
            r#"INSERT INTO evolution_proposals
                 (id, kind, target, diff, reasoning, risk, budget_cost, status,
                  shadow_metrics, signal_ids, trace_ids,
                  created_at, decided_at, decided_by, applied_at, rollback_of)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"#,
        )
        .bind(proposal.id.as_str())
        .bind(proposal.kind.as_str())
        .bind(&proposal.target)
        .bind(&proposal.diff)
        .bind(&proposal.reasoning)
        .bind(proposal.risk.as_str())
        .bind(proposal.budget_cost as i64)
        .bind(proposal.status.as_str())
        .bind(shadow_metrics)
        .bind(signal_ids)
        .bind(trace_ids)
        .bind(proposal.created_at)
        .bind(proposal.decided_at)
        .bind(&proposal.decided_by)
        .bind(proposal.applied_at)
        .bind(proposal.rollback_of.as_ref().map(|p| p.as_str()))
        .execute(&self.pool)
        .await?;
        Ok(())
    }

    pub async fn get(&self, id: &ProposalId) -> Result<EvolutionProposal, RepoError> {
        let row = sqlx::query(
            r#"SELECT id, kind, target, diff, reasoning, risk, budget_cost, status,
                      shadow_metrics, signal_ids, trace_ids,
                      created_at, decided_at, decided_by, applied_at, rollback_of
               FROM evolution_proposals WHERE id = ?"#,
        )
        .bind(id.as_str())
        .fetch_optional(&self.pool)
        .await?;

        let row = row.ok_or_else(|| RepoError::NotFound(id.0.clone()))?;
        decode_proposal(row)
    }

    pub async fn list_by_status(
        &self,
        status: EvolutionStatus,
        limit: i64,
    ) -> Result<Vec<EvolutionProposal>, RepoError> {
        let rows = sqlx::query(
            r#"SELECT id, kind, target, diff, reasoning, risk, budget_cost, status,
                      shadow_metrics, signal_ids, trace_ids,
                      created_at, decided_at, decided_by, applied_at, rollback_of
               FROM evolution_proposals
               WHERE status = ?
               ORDER BY created_at DESC
               LIMIT ?"#,
        )
        .bind(status.as_str())
        .bind(limit)
        .fetch_all(&self.pool)
        .await?;
        rows.into_iter().map(decode_proposal).collect()
    }

    /// Patch proposal status + decided_at + decided_by atomically. Used by
    /// the admin API on approve/deny.
    pub async fn set_decision(
        &self,
        id: &ProposalId,
        new_status: EvolutionStatus,
        decided_at_ms: i64,
        decided_by: &str,
    ) -> Result<(), RepoError> {
        let res = sqlx::query(
            "UPDATE evolution_proposals
                SET status = ?, decided_at = ?, decided_by = ?
              WHERE id = ?",
        )
        .bind(new_status.as_str())
        .bind(decided_at_ms)
        .bind(decided_by)
        .bind(id.as_str())
        .execute(&self.pool)
        .await?;
        if res.rows_affected() == 0 {
            return Err(RepoError::NotFound(id.0.clone()));
        }
        Ok(())
    }

    /// Patch status + applied_at when the EvolutionApplier finishes.
    pub async fn mark_applied(&self, id: &ProposalId, applied_at_ms: i64) -> Result<(), RepoError> {
        let res = sqlx::query(
            "UPDATE evolution_proposals
                SET status = 'applied', applied_at = ?
              WHERE id = ?",
        )
        .bind(applied_at_ms)
        .bind(id.as_str())
        .execute(&self.pool)
        .await?;
        if res.rows_affected() == 0 {
            return Err(RepoError::NotFound(id.0.clone()));
        }
        Ok(())
    }

    /// Phase 3 W1-B: AutoRollback transition `Applied → RolledBack` plus
    /// audit fields. The `WHERE status = 'applied'` clause makes a
    /// double-revert race surface as `NotFound` instead of a silent
    /// second rollback. Manual operator-initiated rollbacks use a
    /// different path (a fresh proposal with `rollback_of`); this one is
    /// reserved for the monitor's auto-revert.
    pub async fn mark_auto_rolled_back(
        &self,
        id: &ProposalId,
        rolled_back_at_ms: i64,
        reason: &str,
    ) -> Result<(), RepoError> {
        let res = sqlx::query(
            "UPDATE evolution_proposals
                SET status = 'rolled_back',
                    auto_rollback_at = ?,
                    auto_rollback_reason = ?
              WHERE id = ? AND status = 'applied'",
        )
        .bind(rolled_back_at_ms)
        .bind(reason)
        .bind(id.as_str())
        .execute(&self.pool)
        .await?;
        if res.rows_affected() == 0 {
            return Err(RepoError::NotFound(id.0.clone()));
        }
        Ok(())
    }

    /// List proposals applied within `[now_ms - grace_window_hours*3600*1000, now_ms]`
    /// that are still in `Applied` (not yet rolled back). Used by the
    /// AutoRollback monitor to pick candidates.
    ///
    /// `grace_window_hours` lower-bounds the apply timestamp; rows whose
    /// `applied_at` is older than the window — or null entirely — are
    /// excluded so a freshly-rolled-back row can't be re-considered after
    /// the operator manually re-applies hours later.
    pub async fn list_applied_in_grace_window(
        &self,
        now_ms: i64,
        grace_window_hours: u32,
        limit: i64,
    ) -> Result<Vec<EvolutionProposal>, RepoError> {
        let since_ms = now_ms - (grace_window_hours as i64) * 3_600 * 1_000;
        let rows = sqlx::query(
            r#"SELECT id, kind, target, diff, reasoning, risk, budget_cost, status,
                      shadow_metrics, signal_ids, trace_ids,
                      created_at, decided_at, decided_by, applied_at, rollback_of
               FROM evolution_proposals
               WHERE status = 'applied'
                 AND applied_at IS NOT NULL
                 AND applied_at >= ?
                 AND applied_at <= ?
               ORDER BY applied_at DESC
               LIMIT ?"#,
        )
        .bind(since_ms)
        .bind(now_ms)
        .bind(limit)
        .fetch_all(&self.pool)
        .await?;
        rows.into_iter().map(decode_proposal).collect()
    }

    /// List `Pending` proposals for `kind` whose risk is in `risks`,
    /// newest first. Used by the ShadowRunner to pick candidates.
    pub async fn list_pending_for_shadow(
        &self,
        kind: EvolutionKind,
        risks: &[EvolutionRisk],
        limit: i64,
    ) -> Result<Vec<EvolutionProposal>, RepoError> {
        if risks.is_empty() {
            return Ok(Vec::new());
        }
        // sqlx doesn't expand `IN (?)`; build the placeholders inline.
        // Risk strings come from the enum (`'static`), no user input.
        let placeholders = vec!["?"; risks.len()].join(",");
        let sql = format!(
            r#"SELECT id, kind, target, diff, reasoning, risk, budget_cost, status,
                      shadow_metrics, signal_ids, trace_ids,
                      created_at, decided_at, decided_by, applied_at, rollback_of
               FROM evolution_proposals
               WHERE status = 'pending' AND kind = ? AND risk IN ({placeholders})
               ORDER BY created_at DESC
               LIMIT ?"#
        );
        let mut q = sqlx::query(&sql).bind(kind.as_str());
        for r in risks {
            q = q.bind(r.as_str());
        }
        let rows = q.bind(limit).fetch_all(&self.pool).await?;
        rows.into_iter().map(decode_proposal).collect()
    }

    /// Atomically transition a proposal from `Pending` to
    /// `ShadowRunning`. Errors if the row is not in `Pending` (avoids
    /// racing two runners).
    pub async fn claim_for_shadow(&self, id: &ProposalId) -> Result<(), RepoError> {
        let res = sqlx::query(
            "UPDATE evolution_proposals
                SET status = 'shadow_running'
              WHERE id = ? AND status = 'pending'",
        )
        .bind(id.as_str())
        .execute(&self.pool)
        .await?;
        if res.rows_affected() == 0 {
            // Could be missing or already claimed; both look the same to
            // the runner — it skips and moves on.
            return Err(RepoError::NotFound(id.0.clone()));
        }
        Ok(())
    }

    /// Persist shadow run output: `eval_run_id`, `baseline_metrics_json`,
    /// `shadow_metrics`, and transition `ShadowRunning → ShadowDone` in
    /// one UPDATE.
    pub async fn mark_shadow_done(
        &self,
        id: &ProposalId,
        eval_run_id: &str,
        baseline_metrics_json: &serde_json::Value,
        shadow_metrics: &serde_json::Value,
    ) -> Result<(), RepoError> {
        let baseline =
            serde_json::to_string(baseline_metrics_json).map_err(|source| {
                RepoError::MalformedJson {
                    column: "baseline_metrics_json",
                    source,
                }
            })?;
        let shadow =
            serde_json::to_string(shadow_metrics).map_err(|source| RepoError::MalformedJson {
                column: "shadow_metrics",
                source,
            })?;
        let res = sqlx::query(
            "UPDATE evolution_proposals
                SET status = 'shadow_done',
                    eval_run_id = ?,
                    baseline_metrics_json = ?,
                    shadow_metrics = ?
              WHERE id = ?",
        )
        .bind(eval_run_id)
        .bind(baseline)
        .bind(shadow)
        .bind(id.as_str())
        .execute(&self.pool)
        .await?;
        if res.rows_affected() == 0 {
            return Err(RepoError::NotFound(id.0.clone()));
        }
        Ok(())
    }
}

fn decode_proposal(row: sqlx::sqlite::SqliteRow) -> Result<EvolutionProposal, RepoError> {
    let kind_raw: String = row.get("kind");
    let kind = kind_raw
        .parse::<EvolutionKind>()
        .map_err(|_| RepoError::MalformedEnum {
            column: "kind",
            value: kind_raw,
        })?;
    let risk_raw: String = row.get("risk");
    let risk = risk_raw
        .parse::<EvolutionRisk>()
        .map_err(|_| RepoError::MalformedEnum {
            column: "risk",
            value: risk_raw,
        })?;
    let status_raw: String = row.get("status");
    let status = status_raw
        .parse::<EvolutionStatus>()
        .map_err(|_| RepoError::MalformedEnum {
            column: "status",
            value: status_raw,
        })?;

    let signal_ids: Vec<i64> =
        serde_json::from_str(&row.get::<String, _>("signal_ids")).map_err(|source| {
            RepoError::MalformedJson {
                column: "signal_ids",
                source,
            }
        })?;
    let trace_ids: Vec<String> =
        serde_json::from_str(&row.get::<String, _>("trace_ids")).map_err(|source| {
            RepoError::MalformedJson {
                column: "trace_ids",
                source,
            }
        })?;
    let shadow_metrics: Option<ShadowMetrics> = match row.get::<Option<String>, _>("shadow_metrics")
    {
        Some(s) => Some(
            serde_json::from_str(&s).map_err(|source| RepoError::MalformedJson {
                column: "shadow_metrics",
                source,
            })?,
        ),
        None => None,
    };

    Ok(EvolutionProposal {
        id: ProposalId(row.get("id")),
        kind,
        target: row.get("target"),
        diff: row.get("diff"),
        reasoning: row.get("reasoning"),
        risk,
        budget_cost: row.get::<i64, _>("budget_cost") as u32,
        status,
        shadow_metrics,
        signal_ids,
        trace_ids,
        created_at: row.get("created_at"),
        decided_at: row.get("decided_at"),
        decided_by: row.get("decided_by"),
        applied_at: row.get("applied_at"),
        rollback_of: row.get::<Option<String>, _>("rollback_of").map(ProposalId),
    })
}

// ---------------------------------------------------------------------------
// History
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
pub struct HistoryRepo {
    pool: SqlitePool,
}

impl HistoryRepo {
    pub fn new(pool: SqlitePool) -> Self {
        Self { pool }
    }

    pub async fn insert(&self, h: &EvolutionHistory) -> Result<i64, RepoError> {
        let metrics = serde_json::to_string(&h.metrics_baseline).map_err(|source| {
            RepoError::MalformedJson {
                column: "metrics_baseline",
                source,
            }
        })?;
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
        .fetch_one(&self.pool)
        .await?;
        Ok(row.get::<i64, _>("id"))
    }

    /// Most recent history row for a given proposal. Phase 2 only writes
    /// one row per proposal, but a future re-apply path could write more
    /// — `ORDER BY applied_at DESC` keeps the API future-proof. Used by
    /// the AutoRollback revert path to fetch the inverse_diff.
    pub async fn latest_for_proposal(
        &self,
        proposal_id: &ProposalId,
    ) -> Result<EvolutionHistory, RepoError> {
        let row = sqlx::query(
            r#"SELECT id, proposal_id, kind, target, before_sha, after_sha,
                      inverse_diff, metrics_baseline, applied_at,
                      rolled_back_at, rollback_reason
               FROM evolution_history
               WHERE proposal_id = ?
               ORDER BY applied_at DESC, id DESC
               LIMIT 1"#,
        )
        .bind(proposal_id.as_str())
        .fetch_optional(&self.pool)
        .await?;
        let row = row.ok_or_else(|| RepoError::NotFound(proposal_id.0.clone()))?;

        let kind_raw: String = row.get("kind");
        let kind =
            kind_raw
                .parse::<EvolutionKind>()
                .map_err(|_| RepoError::MalformedEnum {
                    column: "kind",
                    value: kind_raw,
                })?;
        let metrics_str: String = row.get("metrics_baseline");
        let metrics_baseline: Json =
            serde_json::from_str(&metrics_str).map_err(|source| RepoError::MalformedJson {
                column: "metrics_baseline",
                source,
            })?;

        Ok(EvolutionHistory {
            id: Some(row.get::<i64, _>("id")),
            proposal_id: ProposalId(row.get("proposal_id")),
            kind,
            target: row.get("target"),
            before_sha: row.get("before_sha"),
            after_sha: row.get("after_sha"),
            inverse_diff: row.get("inverse_diff"),
            metrics_baseline,
            applied_at: row.get("applied_at"),
            rolled_back_at: row.get("rolled_back_at"),
            rollback_reason: row.get("rollback_reason"),
        })
    }

    pub async fn mark_rolled_back(
        &self,
        proposal_id: &ProposalId,
        rolled_back_at_ms: i64,
        reason: &str,
    ) -> Result<(), RepoError> {
        let res = sqlx::query(
            "UPDATE evolution_history
                SET rolled_back_at = ?, rollback_reason = ?
              WHERE proposal_id = ?",
        )
        .bind(rolled_back_at_ms)
        .bind(reason)
        .bind(proposal_id.as_str())
        .execute(&self.pool)
        .await?;
        if res.rows_affected() == 0 {
            return Err(RepoError::NotFound(proposal_id.0.clone()));
        }
        Ok(())
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::EvolutionStore;
    use serde_json::json;
    use tempfile::TempDir;

    async fn fresh_store() -> (TempDir, EvolutionStore) {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("evolution.sqlite");
        let store = EvolutionStore::open(&path).await.unwrap();
        (tmp, store)
    }

    #[tokio::test]
    async fn signals_insert_and_list_round_trip() {
        let (_tmp, store) = fresh_store().await;
        let repo = SignalsRepo::new(store.pool().clone());
        let id = repo
            .insert(&EvolutionSignal {
                id: None,
                event_kind: "tool.call.failed".into(),
                target: Some("web_search".into()),
                severity: SignalSeverity::Error,
                payload_json: json!({"reason": "timeout"}),
                trace_id: Some("t1".into()),
                session_id: Some("s1".into()),
                observed_at: 1_000,
            })
            .await
            .unwrap();
        assert!(id > 0);
        let rows = repo
            .list_since(0, Some("tool.call.failed"), 10)
            .await
            .unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].target.as_deref(), Some("web_search"));
        assert_eq!(rows[0].payload_json["reason"], "timeout");
    }

    #[tokio::test]
    async fn proposals_decision_flow() {
        let (_tmp, store) = fresh_store().await;
        let repo = ProposalsRepo::new(store.pool().clone());
        let id = ProposalId::new("evol-test-001");
        repo.insert(&EvolutionProposal {
            id: id.clone(),
            kind: EvolutionKind::MemoryOp,
            target: "merge_chunks:42,43".into(),
            diff: String::new(),
            reasoning: "two near-duplicate chunks".into(),
            risk: EvolutionRisk::Low,
            budget_cost: 0,
            status: EvolutionStatus::Pending,
            shadow_metrics: None,
            signal_ids: vec![1, 2, 3],
            trace_ids: vec!["t1".into()],
            created_at: 1_000,
            decided_at: None,
            decided_by: None,
            applied_at: None,
            rollback_of: None,
        })
        .await
        .unwrap();

        // Pending list
        let pending = repo
            .list_by_status(EvolutionStatus::Pending, 10)
            .await
            .unwrap();
        assert_eq!(pending.len(), 1);
        assert_eq!(pending[0].id, id);

        // Approve
        repo.set_decision(&id, EvolutionStatus::Approved, 2_000, "operator")
            .await
            .unwrap();
        let after = repo.get(&id).await.unwrap();
        assert_eq!(after.status, EvolutionStatus::Approved);
        assert_eq!(after.decided_at, Some(2_000));
        assert_eq!(after.decided_by.as_deref(), Some("operator"));

        // Apply
        repo.mark_applied(&id, 3_000).await.unwrap();
        let after = repo.get(&id).await.unwrap();
        assert_eq!(after.status, EvolutionStatus::Applied);
        assert_eq!(after.applied_at, Some(3_000));
    }

    /// Helper: insert a minimal pending proposal for the shadow tests.
    async fn insert_pending(
        repo: &ProposalsRepo,
        id: &str,
        kind: EvolutionKind,
        risk: EvolutionRisk,
    ) -> ProposalId {
        let pid = ProposalId::new(id);
        repo.insert(&EvolutionProposal {
            id: pid.clone(),
            kind,
            target: format!("target-{id}"),
            diff: String::new(),
            reasoning: "fixture".into(),
            risk,
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
        pid
    }

    #[tokio::test]
    async fn list_pending_for_shadow_filters_kind_and_risk() {
        let (_tmp, store) = fresh_store().await;
        let repo = ProposalsRepo::new(store.pool().clone());
        // High memory_op (match), low memory_op (skip), high skill_update (skip).
        insert_pending(&repo, "p-high-mem", EvolutionKind::MemoryOp, EvolutionRisk::High).await;
        insert_pending(&repo, "p-low-mem", EvolutionKind::MemoryOp, EvolutionRisk::Low).await;
        insert_pending(
            &repo,
            "p-high-skill",
            EvolutionKind::SkillUpdate,
            EvolutionRisk::High,
        )
        .await;

        let hits = repo
            .list_pending_for_shadow(
                EvolutionKind::MemoryOp,
                &[EvolutionRisk::Medium, EvolutionRisk::High],
                10,
            )
            .await
            .unwrap();
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].id.as_str(), "p-high-mem");
    }

    #[tokio::test]
    async fn claim_for_shadow_transitions_then_fails_on_non_pending() {
        let (_tmp, store) = fresh_store().await;
        let repo = ProposalsRepo::new(store.pool().clone());
        let pid =
            insert_pending(&repo, "p-claim", EvolutionKind::MemoryOp, EvolutionRisk::High).await;
        repo.claim_for_shadow(&pid).await.unwrap();
        let after = repo.get(&pid).await.unwrap();
        assert_eq!(after.status, EvolutionStatus::ShadowRunning);
        // Second claim is the racing runner — must error.
        let err = repo.claim_for_shadow(&pid).await.unwrap_err();
        assert!(matches!(err, RepoError::NotFound(_)), "got {err:?}");
    }

    #[tokio::test]
    async fn mark_shadow_done_persists_metrics_and_eval_id() {
        let (_tmp, store) = fresh_store().await;
        let repo = ProposalsRepo::new(store.pool().clone());
        let pid = insert_pending(&repo, "p-done", EvolutionKind::MemoryOp, EvolutionRisk::High)
            .await;
        repo.claim_for_shadow(&pid).await.unwrap();

        let baseline = json!({"chunks_total": 2});
        let shadow = json!({"chunks_total": 1, "rows_merged": 1});
        repo.mark_shadow_done(&pid, "eval-2026-04-27-abc123", &baseline, &shadow)
            .await
            .unwrap();

        let row: (String, Option<String>, Option<String>, Option<String>) = sqlx::query_as(
            "SELECT status, eval_run_id, baseline_metrics_json, shadow_metrics
                 FROM evolution_proposals WHERE id = ?",
        )
        .bind(pid.as_str())
        .fetch_one(store.pool())
        .await
        .unwrap();
        assert_eq!(row.0, "shadow_done");
        assert_eq!(row.1.as_deref(), Some("eval-2026-04-27-abc123"));
        let baseline_back: serde_json::Value = serde_json::from_str(&row.2.unwrap()).unwrap();
        assert_eq!(baseline_back, baseline);
        let shadow_back: serde_json::Value = serde_json::from_str(&row.3.unwrap()).unwrap();
        assert_eq!(shadow_back, shadow);
    }

    #[tokio::test]
    async fn history_insert_and_rollback() {
        let (_tmp, store) = fresh_store().await;
        // Need a proposal first to satisfy FK.
        let proposals = ProposalsRepo::new(store.pool().clone());
        let pid = ProposalId::new("evol-test-002");
        proposals
            .insert(&EvolutionProposal {
                id: pid.clone(),
                kind: EvolutionKind::TagRebalance,
                target: "tag_tree".into(),
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
                decided_by: Some("auto".into()),
                applied_at: Some(3_000),
                rollback_of: None,
            })
            .await
            .unwrap();

        let history = HistoryRepo::new(store.pool().clone());
        let hid = history
            .insert(&EvolutionHistory {
                id: None,
                proposal_id: pid.clone(),
                kind: EvolutionKind::TagRebalance,
                target: "tag_tree".into(),
                before_sha: "abc".into(),
                after_sha: "def".into(),
                inverse_diff: "noop".into(),
                metrics_baseline: serde_json::json!({"err_rate": 0.02}),
                applied_at: 3_000,
                rolled_back_at: None,
                rollback_reason: None,
            })
            .await
            .unwrap();
        assert!(hid > 0);

        history
            .mark_rolled_back(&pid, 4_000, "metrics regression")
            .await
            .unwrap();
        // No getter yet — verify via raw query
        let row: (Option<i64>, Option<String>) = sqlx::query_as(
            "SELECT rolled_back_at, rollback_reason FROM evolution_history WHERE proposal_id = ?",
        )
        .bind(pid.as_str())
        .fetch_one(store.pool())
        .await
        .unwrap();
        assert_eq!(row.0, Some(4_000));
        assert_eq!(row.1.as_deref(), Some("metrics regression"));
    }

    /// Helper: insert an `applied` proposal so the auto-rollback gate
    /// has something to flip.
    async fn insert_applied(repo: &ProposalsRepo, id: &str) -> ProposalId {
        let pid = ProposalId::new(id);
        repo.insert(&EvolutionProposal {
            id: pid.clone(),
            kind: EvolutionKind::MemoryOp,
            target: format!("delete_chunk:{id}"),
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
            decided_by: Some("auto".into()),
            applied_at: Some(3_000),
            rollback_of: None,
        })
        .await
        .unwrap();
        pid
    }

    #[tokio::test]
    async fn mark_auto_rolled_back_happy_path() {
        let (_tmp, store) = fresh_store().await;
        let repo = ProposalsRepo::new(store.pool().clone());
        let pid = insert_applied(&repo, "evol-ar-001").await;

        repo.mark_auto_rolled_back(&pid, 5_000, "err_signal_count: 4 -> 12 (+200%)")
            .await
            .unwrap();
        let after = repo.get(&pid).await.unwrap();
        assert_eq!(after.status, EvolutionStatus::RolledBack);

        // auto_rollback_at + auto_rollback_reason aren't on the row type
        // yet — verify via raw query so the test pins the column writes.
        let row: (Option<i64>, Option<String>) = sqlx::query_as(
            "SELECT auto_rollback_at, auto_rollback_reason
               FROM evolution_proposals WHERE id = ?",
        )
        .bind(pid.as_str())
        .fetch_one(store.pool())
        .await
        .unwrap();
        assert_eq!(row.0, Some(5_000));
        assert_eq!(row.1.as_deref(), Some("err_signal_count: 4 -> 12 (+200%)"));
    }

    #[tokio::test]
    async fn mark_auto_rolled_back_double_call_is_not_found() {
        let (_tmp, store) = fresh_store().await;
        let repo = ProposalsRepo::new(store.pool().clone());
        let pid = insert_applied(&repo, "evol-ar-002").await;

        repo.mark_auto_rolled_back(&pid, 5_000, "first").await.unwrap();
        // Second call: status is now `rolled_back`, so the WHERE clause
        // misses and we bail with NotFound — keeps a racing pair of
        // monitor passes from double-incrementing or stomping the reason.
        let err = repo
            .mark_auto_rolled_back(&pid, 6_000, "second")
            .await
            .unwrap_err();
        assert!(matches!(err, RepoError::NotFound(_)), "got {err:?}");
    }

    #[tokio::test]
    async fn mark_auto_rolled_back_rejects_non_applied_status() {
        let (_tmp, store) = fresh_store().await;
        let repo = ProposalsRepo::new(store.pool().clone());
        // Pending row — must refuse: the monitor only ever rolls back
        // proposals that already landed on disk.
        let pid = insert_pending(&repo, "evol-ar-003", EvolutionKind::MemoryOp, EvolutionRisk::Low)
            .await;
        let err = repo
            .mark_auto_rolled_back(&pid, 5_000, "won't take")
            .await
            .unwrap_err();
        assert!(matches!(err, RepoError::NotFound(_)), "got {err:?}");
        let after = repo.get(&pid).await.unwrap();
        assert_eq!(after.status, EvolutionStatus::Pending);
    }

    /// Helper: insert one applied proposal with an explicit `applied_at`
    /// so the time-window tests can pin behaviour without flakey clocks.
    async fn insert_applied_at(
        repo: &ProposalsRepo,
        id: &str,
        applied_at_ms: i64,
    ) -> ProposalId {
        let pid = ProposalId::new(id);
        repo.insert(&EvolutionProposal {
            id: pid.clone(),
            kind: EvolutionKind::MemoryOp,
            target: format!("delete_chunk:{id}"),
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
            decided_by: Some("auto".into()),
            applied_at: Some(applied_at_ms),
            rollback_of: None,
        })
        .await
        .unwrap();
        pid
    }

    #[tokio::test]
    async fn list_applied_in_grace_window_filters_by_time() {
        let (_tmp, store) = fresh_store().await;
        let repo = ProposalsRepo::new(store.pool().clone());
        let now: i64 = 100 * 3_600 * 1_000; // pick a base in the integer middle.
        let in_window = now - 1 * 3_600 * 1_000;
        let too_old = now - 100 * 3_600 * 1_000;
        let in_future = now + 5 * 60 * 1_000;
        insert_applied_at(&repo, "evol-grace-in", in_window).await;
        insert_applied_at(&repo, "evol-grace-old", too_old).await;
        insert_applied_at(&repo, "evol-grace-future", in_future).await;

        let hits = repo
            .list_applied_in_grace_window(now, 72, 10)
            .await
            .unwrap();
        let ids: Vec<String> = hits.iter().map(|p| p.id.0.clone()).collect();
        assert_eq!(ids, vec!["evol-grace-in".to_string()]);
    }

    #[tokio::test]
    async fn list_applied_in_grace_window_excludes_rolled_back() {
        let (_tmp, store) = fresh_store().await;
        let repo = ProposalsRepo::new(store.pool().clone());
        let now: i64 = 100 * 3_600 * 1_000;
        let applied_at = now - 1 * 3_600 * 1_000;
        let pid_rolled = insert_applied_at(&repo, "evol-grace-rolled", applied_at).await;
        let _pid_live = insert_applied_at(&repo, "evol-grace-live", applied_at).await;
        // Flip one row to rolled_back; it must drop out of the window list.
        repo.mark_auto_rolled_back(&pid_rolled, now, "test-rollback")
            .await
            .unwrap();

        let hits = repo
            .list_applied_in_grace_window(now, 72, 10)
            .await
            .unwrap();
        let ids: Vec<String> = hits.iter().map(|p| p.id.0.clone()).collect();
        assert_eq!(ids, vec!["evol-grace-live".to_string()]);
    }

    #[tokio::test]
    async fn history_latest_for_proposal_round_trip() {
        let (_tmp, store) = fresh_store().await;
        let proposals = ProposalsRepo::new(store.pool().clone());
        let pid = insert_applied(&proposals, "evol-hist-001").await;
        let history = HistoryRepo::new(store.pool().clone());
        let hid = history
            .insert(&EvolutionHistory {
                id: None,
                proposal_id: pid.clone(),
                kind: EvolutionKind::MemoryOp,
                target: "delete_chunk:42".into(),
                before_sha: "aaa".into(),
                after_sha: "bbb".into(),
                inverse_diff: r#"{"action":"restore_chunk","content":"x","namespace":"general","file_id":1,"chunk_index":0}"#
                    .into(),
                metrics_baseline: serde_json::json!({"target": "delete_chunk:42"}),
                applied_at: 3_000,
                rolled_back_at: None,
                rollback_reason: None,
            })
            .await
            .unwrap();

        let got = history.latest_for_proposal(&pid).await.unwrap();
        assert_eq!(got.id, Some(hid));
        assert_eq!(got.proposal_id, pid);
        assert_eq!(got.kind, EvolutionKind::MemoryOp);
        assert_eq!(got.target, "delete_chunk:42");
        assert_eq!(got.applied_at, 3_000);
        assert!(got.inverse_diff.contains("restore_chunk"));

        // Missing proposal id → NotFound.
        let missing = ProposalId::new("evol-hist-nope");
        let err = history.latest_for_proposal(&missing).await.unwrap_err();
        assert!(matches!(err, RepoError::NotFound(_)), "got {err:?}");
    }
}
