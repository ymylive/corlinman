//! Phase 3.1 (W-4): cross-language end-to-end for the consolidation
//! pipeline. The Python `corlinman-evolution-engine` package files
//! `consolidate_chunk:<id>` proposals; the Rust `EvolutionApplier`
//! consumes them. Pre-Phase-3.1 this contract was tested only on each
//! side in isolation — neither half exercised the other's row shape,
//! so a divergence between the two would have shipped silently.
//!
//! ## Why we don't subprocess the Python CLI
//!
//! `cargo test` runs in a clean environment that doesn't carry a
//! `uv`-managed venv with the `corlinman-evolution-engine` package
//! installed; spawning the CLI from here would either need a per-CI
//! bootstrap step or rely on PATH side-effects. The acceptance brief
//! explicitly authorises the fallback: "use raw SQL in the Rust test
//! to manually simulate Python writes of the proposal shape". We do
//! exactly that — the `evolution_proposals` INSERT below mirrors
//! `corlinman_evolution_engine.consolidation.consolidation_run_once`
//! verbatim. Drift between the two surfaces will fail the Python-side
//! `test_consolidation_proposal_shape_matches_memory_op_contract` and
//! this test in lockstep, which is the regression net we wanted.
//!
//! ## What the test pins
//!
//! 1. `list_promotion_candidates` excludes never-recalled chunks (B-4
//!    cold-start guard) AND excludes chunks recalled inside the
//!    cooling window — the same SQL guard the Python proposer uses.
//! 2. A proposal with the Python-emitted shape (`kind = memory_op`,
//!    `target = consolidate_chunk:<id>`, `risk = low`, status=pending,
//!    diff="") feeds cleanly into `EvolutionApplier::apply` after the
//!    operator approves.
//! 3. Apply flips `chunks.namespace` to `consolidated`, stamps
//!    `consolidated_at`, freezes `decay_score = 1.0`.
//! 4. Revert restores prior namespace + decay_score + the original
//!    `consolidated_at` round-tripped through `inverse_diff` (B-3
//!    regression).

use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use corlinman_core::config::AutoRollbackThresholds;
use corlinman_evolution::{EvolutionStatus, EvolutionStore, ProposalId, ProposalsRepo};
use corlinman_gateway::evolution_applier::EvolutionApplier;
use corlinman_vector::SqliteStore;
use sqlx::Row;
use tempfile::TempDir;

fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis() as i64
}

/// Stand up tempdir kb + evolution stores. `EvolutionStore::open` runs
/// the canonical schema (matching the Python conftest copy) so we
/// don't need to hand-craft DDL here.
async fn setup() -> (TempDir, Arc<SqliteStore>, Arc<EvolutionStore>) {
    let tmp = TempDir::new().unwrap();
    let kb = Arc::new(
        SqliteStore::open(&tmp.path().join("kb.sqlite"))
            .await
            .unwrap(),
    );
    let evol = Arc::new(
        EvolutionStore::open(&tmp.path().join("evolution.sqlite"))
            .await
            .unwrap(),
    );
    (tmp, kb, evol)
}

/// Insert a chunk and stamp `last_recalled_at` so the Phase 3.1 (B-4)
/// cooling-period guard is satisfied. Mirrors the Python conftest's
/// `_seed_chunk_with_decay` helper.
async fn seed_chunk_with_decay(
    kb: &SqliteStore,
    file_path: &str,
    decay_score: f32,
    last_recalled_ms: Option<i64>,
) -> i64 {
    let file_id = kb
        .insert_file(file_path, "fixture", "h", 0, 0)
        .await
        .unwrap();
    let chunk_id = kb
        .insert_chunk(file_id, 0, "fixture content", None, "general")
        .await
        .unwrap();
    sqlx::query("UPDATE chunks SET decay_score = ?1, last_recalled_at = ?2 WHERE id = ?3")
        .bind(decay_score as f64)
        .bind(last_recalled_ms)
        .bind(chunk_id)
        .execute(kb.pool())
        .await
        .unwrap();
    chunk_id
}

/// Replicate `corlinman_evolution_engine.consolidation.consolidation_run_once`
/// at the SQL layer. Specifically:
///
/// 1. Read candidates via `SqliteStore::list_promotion_candidates`
///    (the Rust function the Python helper mirrors).
/// 2. For each candidate insert a `memory_op` proposal whose target
///    is `consolidate_chunk:<id>` — matches the Python INSERT in
///    `consolidation.py:consolidation_run_once` byte-for-byte.
///
/// Returns the list of proposal ids written. Drift between this
/// function and the Python emitter shows up as a Python-side test
/// regression on `test_consolidation_proposal_shape_matches_memory_op_contract`
/// — that suite covers the row-shape contract; this function covers
/// the Rust applier's tolerance for the same shape.
async fn simulate_python_consolidation_run(
    kb: &SqliteStore,
    evol: &EvolutionStore,
    threshold: f32,
    cooling_hours: f64,
    limit: i64,
    now_ms: i64,
    id_prefix: &str,
) -> Vec<String> {
    let ids = kb
        .list_promotion_candidates(threshold, limit, cooling_hours, now_ms)
        .await
        .unwrap();
    let mut out = Vec::with_capacity(ids.len());
    for (i, chunk_id) in ids.iter().enumerate() {
        let pid = format!("{id_prefix}-{:03}", i + 1);
        let target = format!("consolidate_chunk:{chunk_id}");
        sqlx::query(
            r#"INSERT INTO evolution_proposals
                 (id, kind, target, diff, reasoning, risk, budget_cost, status,
                  shadow_metrics, signal_ids, trace_ids,
                  created_at, decided_at, decided_by, applied_at, rollback_of)
               VALUES (?, 'memory_op', ?, '', ?, 'low', 0, 'pending',
                       NULL, '[]', '[]',
                       ?, NULL, NULL, NULL, NULL)"#,
        )
        .bind(&pid)
        .bind(&target)
        .bind(format!("decay_score sustained on chunk {chunk_id}"))
        .bind(now_ms)
        .execute(evol.pool())
        .await
        .unwrap();
        out.push(pid);
    }
    out
}

/// Flip a proposal `pending` → `approved` so the EvolutionApplier
/// will accept it. Mirrors the operator-approval step the admin route
/// performs in production.
async fn approve(evol: &EvolutionStore, pid: &str) {
    sqlx::query(
        "UPDATE evolution_proposals SET status = 'approved', \
         decided_at = ?, decided_by = 'e2e-test' WHERE id = ?",
    )
    .bind(now_ms())
    .bind(pid)
    .execute(evol.pool())
    .await
    .unwrap();
}

/// Phase 3.1 e2e: Python-shaped proposals → Rust applier → revert
/// round-trip including the new `prior_consolidated_at` field.
#[tokio::test]
async fn python_consolidation_proposals_apply_via_rust_and_revert_cleanly() {
    let (_tmp, kb, evol) = setup().await;
    let now = now_ms();

    // Mix of seeded chunks:
    //   eligible — high score, recall comfortably outside cooling.
    //   fresh    — high score, recall *inside* cooling window (24h).
    //   never    — high score, no recall stamp at all.
    //   low      — recall age fine, but score below threshold.
    let eligible =
        seed_chunk_with_decay(&kb, "/eligible.md", 0.9, Some(now - 30 * 3_600_000)).await;
    let fresh = seed_chunk_with_decay(&kb, "/fresh.md", 0.9, Some(now - 60_000)).await;
    let never = seed_chunk_with_decay(&kb, "/never.md", 0.9, None).await;
    let low = seed_chunk_with_decay(&kb, "/low.md", 0.4, Some(now - 30 * 3_600_000)).await;

    // Run the simulated Python proposer. Only `eligible` should get a
    // proposal — fresh / never blocked by the cooling+recall guard,
    // low blocked by threshold.
    let proposal_ids =
        simulate_python_consolidation_run(&kb, &evol, 0.65, 24.0, 50, now, "evol-cons-e2e-r1")
            .await;
    assert_eq!(
        proposal_ids.len(),
        1,
        "exactly one chunk eligible: {eligible} (fresh={fresh}, never={never}, low={low})",
    );

    // Verify the proposal carries the Python-emitted shape — kind,
    // target, status, risk, budget_cost, diff are all what the
    // EvolutionApplier expects.
    let row = sqlx::query(
        "SELECT id, kind, target, status, risk, budget_cost, diff \
         FROM evolution_proposals WHERE id = ?",
    )
    .bind(&proposal_ids[0])
    .fetch_one(evol.pool())
    .await
    .unwrap();
    assert_eq!(row.get::<String, _>("kind"), "memory_op");
    assert_eq!(
        row.get::<String, _>("target"),
        format!("consolidate_chunk:{eligible}")
    );
    assert_eq!(row.get::<String, _>("status"), "pending");
    assert_eq!(row.get::<String, _>("risk"), "low");
    assert_eq!(row.get::<i64, _>("budget_cost"), 0);
    assert_eq!(row.get::<String, _>("diff"), "");

    // Operator approves; applier executes.
    approve(&evol, &proposal_ids[0]).await;
    let skills_dir = std::env::temp_dir().join("corlinman-itest-cons-skills");
    std::fs::create_dir_all(&skills_dir).unwrap();
    let applier = EvolutionApplier::new(
        evol.clone(),
        kb.clone(),
        AutoRollbackThresholds::default(),
        skills_dir,
    );
    let history = applier
        .apply(&ProposalId::new(&proposal_ids[0]))
        .await
        .expect("forward apply must succeed for the e2e contract");

    // kb side: chunk now consolidated.
    let state = kb.get_chunk_decay_state(eligible).await.unwrap().unwrap();
    assert_eq!(state.namespace, corlinman_vector::CONSOLIDATED_NAMESPACE);
    assert_eq!(state.decay_score, 1.0);
    assert!(state.consolidated_at.is_some(), "consolidated_at stamped");
    let first_consolidated = state.consolidated_at.unwrap();

    // Phase 3.1 (B-3): inverse_diff carries `prior_consolidated_at`
    // (null on first promotion).
    let inv: serde_json::Value = serde_json::from_str(&history.inverse_diff).unwrap();
    assert_eq!(inv["action"], "demote_chunk");
    assert_eq!(inv["chunk_id"], eligible);
    assert_eq!(inv["prior_namespace"], "general");
    assert!(inv["prior_consolidated_at"].is_null());

    // Revert restores the chunk byte-for-byte.
    let reverted = applier
        .revert(&ProposalId::new(&proposal_ids[0]), "e2e revert")
        .await
        .unwrap();
    assert!(reverted.rolled_back_at.is_some());
    let after = kb.get_chunk_decay_state(eligible).await.unwrap().unwrap();
    assert_eq!(after.namespace, "general");
    assert!(after.consolidated_at.is_none());
    assert!(
        (after.decay_score - 0.9).abs() < 1e-5,
        "prior_decay_score restored, got {}",
        after.decay_score
    );

    // Proposal is now RolledBack — Python proposer (B-2) would skip
    // its target on the next run, but that's covered by the Python
    // suite. Here we just confirm the Rust applier flipped status.
    let proposals = ProposalsRepo::new(evol.pool().clone());
    let after_status = proposals
        .get(&ProposalId::new(&proposal_ids[0]))
        .await
        .unwrap();
    assert_eq!(after_status.status, EvolutionStatus::RolledBack);

    // Phase 3.1 (B-3) round-trip: re-promote the chunk after manually
    // restoring the original consolidated_at to simulate the
    // "promoted before, demoted, now promoting again" path. The
    // second revert must restore first_consolidated rather than
    // NULLing the column — which is the exact bit the legacy
    // inverse_diff dropped.
    sqlx::query("UPDATE chunks SET consolidated_at = ? WHERE id = ?")
        .bind(first_consolidated)
        .bind(eligible)
        .execute(kb.pool())
        .await
        .unwrap();
    // Drop the cooling guard for round 2 so we don't have to fudge
    // timestamps just to re-elect the chunk. last_recalled_at is
    // still set from seed, so the IS NOT NULL guard is satisfied.
    let proposal_ids_round2 =
        simulate_python_consolidation_run(&kb, &evol, 0.0, 0.0, 50, now, "evol-cons-e2e-r2").await;
    let pid_r2 = proposal_ids_round2
        .first()
        .cloned()
        .expect("round-2 proposer must emit at least one proposal");

    // Re-resolve from the DB by target so the assertion isn't tied
    // to whichever id `simulate_python_consolidation_run` happened
    // to assign (round 2 re-runs the proposer over the full kb).
    let target_eligible = format!("consolidate_chunk:{eligible}");
    let pid_eligible_round2: String = sqlx::query_scalar(
        "SELECT id FROM evolution_proposals WHERE target = ? AND status = 'pending'",
    )
    .bind(&target_eligible)
    .fetch_one(evol.pool())
    .await
    .unwrap_or(pid_r2);

    approve(&evol, &pid_eligible_round2).await;
    let history_r2 = applier
        .apply(&ProposalId::new(&pid_eligible_round2))
        .await
        .expect("round-2 apply must succeed");
    let inv_r2: serde_json::Value = serde_json::from_str(&history_r2.inverse_diff).unwrap();
    assert_eq!(
        inv_r2["prior_consolidated_at"].as_i64(),
        Some(first_consolidated),
        "round-2 inverse_diff must carry first-promotion timestamp",
    );
    applier
        .revert(&ProposalId::new(&pid_eligible_round2), "round 2 revert")
        .await
        .unwrap();
    let final_state = kb.get_chunk_decay_state(eligible).await.unwrap().unwrap();
    assert_eq!(
        final_state.consolidated_at,
        Some(first_consolidated),
        "B-3 regression: prior_consolidated_at must round-trip through revert"
    );
    assert_eq!(final_state.namespace, "general");
}
