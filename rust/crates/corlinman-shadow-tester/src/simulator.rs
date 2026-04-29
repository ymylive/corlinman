//! `KindSimulator` trait + per-kind implementations.
//!
//! A simulator takes one [`EvalCase`] and a path to a tempdir copy of
//! `kb.sqlite` that the [`crate::runner::ShadowRunner`] has already
//! seeded with `case.kb_seed`. It must:
//!
//! 1. Read pre-state from the tempdir DB → `output.baseline`.
//! 2. Apply `case.proposal.target`'s operation to the tempdir DB only.
//! 3. Read post-state → `output.shadow`.
//! 4. Compare against `case.expected` → set `output.passed`.
//! 5. Return [`SimulatorOutput`].
//!
//! The runner aggregates per-case `baseline` maps into the proposal's
//! `baseline_metrics_json` column and per-case `shadow` maps into the
//! `shadow_metrics` column. The split gives the operator a measured
//! delta to review, not just the post-change snapshot.
//!
//! **Sandbox invariant**: simulators never touch any path other than
//! `kb_path`. The runner hands them a tempdir; the prod `kb.sqlite` is
//! never opened. Violations are runner-policy bugs, not simulator
//! ones.

use std::path::Path;
use std::str::FromStr;
use std::time::Instant;

use async_trait::async_trait;
use corlinman_evolution::EvolutionKind;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use sqlx::sqlite::{SqliteConnectOptions, SqlitePoolOptions};
use sqlx::{Row, SqlitePool};

use crate::eval::{EvalCase, ExpectedOutcome};

/// Errors a simulator can surface to the runner. The runner downgrades
/// these into a failed [`SimulatorOutput`] (with `passed = false` +
/// `error = Some(...)`) rather than aborting the whole shadow run, so
/// one bad case doesn't poison the rest of the eval set.
#[derive(Debug, thiserror::Error)]
pub enum SimulatorError {
    /// `case.proposal.target` could not be parsed (e.g. `merge_chunks:`
    /// missing ids, or unknown operation name).
    #[error("invalid target {target:?}: {reason}")]
    InvalidTarget { target: String, reason: String },

    /// Fixture seed or simulated mutation failed against the tempdir DB.
    #[error("sqlite error in {step}: {source}")]
    Sqlite {
        step: &'static str,
        #[source]
        source: sqlx::Error,
    },

    /// Phase 3.1: a path the simulator was asked to operate on
    /// canonicalised outside the tempdir sandbox. Carries the path
    /// the runner provided plus a short reason. Surfaces an explicit
    /// reject instead of writing into whatever symlink target tried
    /// to hijack the sandbox.
    #[error("path rejected {path:?}: {reason}")]
    PathRejected {
        path: std::path::PathBuf,
        reason: String,
    },

    /// Catch-all for unanticipated runtime conditions.
    #[error("simulator runtime: {0}")]
    Runtime(String),
}

/// Outcome of running one [`EvalCase`] through a simulator.
///
/// `baseline` and `shadow` are kept as free-form `serde_json::Map`s so
/// each kind decides its own metric vocabulary (memory_op uses
/// `chunks_total` / `target_chunk_ids`; future kinds like skill_update
/// will use `success_rate` / `p95_latency_ms`). The runner aggregates
/// across cases without inspecting the keys.
#[derive(Debug, Clone)]
pub struct SimulatorOutput {
    pub case_name: String,
    /// True iff post-state matches `case.expected`. Defines the "did
    /// this case pass" bit the runner aggregates into pass_rate.
    pub passed: bool,
    /// Measurements taken *before* applying the proposal. Feeds
    /// `baseline_metrics_json`.
    pub baseline: serde_json::Map<String, serde_json::Value>,
    /// Measurements taken *after* applying the proposal. Feeds
    /// `shadow_metrics`.
    pub shadow: serde_json::Map<String, serde_json::Value>,
    /// Wall-clock simulator latency. The runner uses this to compute
    /// p95 / mean across the eval set.
    pub latency_ms: u64,
    /// Set when the simulator hit `SimulatorError`; `passed` is false
    /// in that case and `baseline` / `shadow` may be empty.
    pub error: Option<String>,
}

/// Pluggable per-kind simulator. The runner holds a registry keyed by
/// [`EvolutionKind`] and dispatches at run time.
#[async_trait]
pub trait KindSimulator: Send + Sync {
    /// Which kind this simulator handles. Must match the
    /// `EvolutionKind` discriminator in the proposals it runs against.
    fn kind(&self) -> EvolutionKind;

    /// Run one case against a sandboxed kb at `kb_path`.
    ///
    /// The runner has already (a) created the tempdir, (b) opened the
    /// SQLite at `kb_path`, (c) replayed `case.kb_seed`. The simulator
    /// only owns steps 1-5 in the module doc.
    async fn simulate(
        &self,
        case: &EvalCase,
        kb_path: &Path,
    ) -> Result<SimulatorOutput, SimulatorError>;
}

// ---------------------------------------------------------------------------
// MemoryOpSimulator
// ---------------------------------------------------------------------------

/// Max chars copied from `chunks.content` into baseline/shadow metrics.
/// Keeps the per-case JSON small enough that the runner can fan-in many
/// cases into one proposal row without blowing past sqlite's TEXT
/// practicality.
const CONTENT_PREVIEW_CHARS: usize = 200;

/// Parse a `merge_chunks:<id>,<id>[,<id>...]` target into chunk ids.
///
/// Rejects: missing prefix, fewer than 2 ids, non-integer ids, duplicate
/// ids. Each rejection becomes a `SimulatorError::InvalidTarget` so the
/// runner can downgrade the case to a reportable failure.
fn parse_merge_target(target: &str) -> Result<Vec<i64>, SimulatorError> {
    let Some(rest) = target.strip_prefix("merge_chunks:") else {
        return Err(SimulatorError::InvalidTarget {
            target: target.to_string(),
            reason: "expected prefix 'merge_chunks:'".to_string(),
        });
    };

    let mut ids: Vec<i64> = Vec::new();
    for raw in rest.split(',') {
        let trimmed = raw.trim();
        let id = i64::from_str(trimmed).map_err(|_| SimulatorError::InvalidTarget {
            target: target.to_string(),
            reason: format!("non-integer id '{trimmed}'"),
        })?;
        ids.push(id);
    }

    if ids.len() < 2 {
        return Err(SimulatorError::InvalidTarget {
            target: target.to_string(),
            reason: "merge needs at least 2 chunk ids".to_string(),
        });
    }

    // O(n^2) is fine — N <= a handful of ids in practice.
    for (i, a) in ids.iter().enumerate() {
        if ids[i + 1..].iter().any(|b| a == b) {
            return Err(SimulatorError::InvalidTarget {
                target: target.to_string(),
                reason: format!("duplicate id {a}"),
            });
        }
    }

    Ok(ids)
}

/// Truncate `s` to at most `CONTENT_PREVIEW_CHARS` Unicode scalar values.
/// Char-based (not byte-based) so we never split a UTF-8 codepoint.
fn preview(s: &str) -> String {
    s.chars().take(CONTENT_PREVIEW_CHARS).collect()
}

/// Simulator for `memory_op` proposals: collapses a set of chunk rows
/// into the lowest-id surviving row by deleting the rest, all within the
/// runner's tempdir SQLite. The W1-A scope is purely the deterministic
/// data op — Jaccard / similarity-based "should we even merge?" lives
/// upstream in `EvolutionEngine`. NoOp here means "target ids don't all
/// exist" (parse-or-prep short-circuit), not "content too dissimilar".
pub struct MemoryOpSimulator;

#[async_trait]
impl KindSimulator for MemoryOpSimulator {
    fn kind(&self) -> EvolutionKind {
        EvolutionKind::MemoryOp
    }

    async fn simulate(
        &self,
        case: &EvalCase,
        kb_path: &Path,
    ) -> Result<SimulatorOutput, SimulatorError> {
        let started = Instant::now();

        // Parse-time failures are per-case, not infra: surface as a
        // failed SimulatorOutput so the runner can record + continue.
        let parsed_ids = match parse_merge_target(&case.proposal.target) {
            Ok(ids) => ids,
            Err(e) => {
                return Ok(SimulatorOutput {
                    case_name: case.name.clone(),
                    passed: false,
                    baseline: Map::new(),
                    shadow: Map::new(),
                    latency_ms: started.elapsed().as_millis() as u64,
                    error: Some(e.to_string()),
                });
            }
        };

        let pool = open_pool(kb_path).await?;

        let baseline = capture_baseline(&pool, &parsed_ids).await?;
        let existing_ids = parsed_existing_ids(&baseline);
        let surviving_id = *parsed_ids
            .iter()
            .min()
            .expect("parse_merge_target ensures len>=2");

        // NoOp short-circuit: if any target id is missing from the DB we
        // refuse to apply a partial merge. This matches the runner's
        // "deterministic" contract: shadow only ever runs ops that the
        // source data fully supports.
        let all_present = existing_ids.len() == parsed_ids.len();

        let (rows_merged, surviving_content) = if all_present {
            apply_merge(&pool, surviving_id, &parsed_ids).await?
        } else {
            // No mutation; surviving_content reflects whatever the
            // surviving row is right now (or empty if it too is absent).
            let content = fetch_content(&pool, surviving_id)
                .await?
                .unwrap_or_default();
            (0u32, content)
        };

        let shadow = capture_shadow(&pool, surviving_id, rows_merged, &surviving_content).await?;

        let passed = match &case.expected {
            ExpectedOutcome::Merged {
                rows_merged: expected_rows,
                surviving_chunk_id: expected_surv,
                ..
            } => rows_merged == *expected_rows && surviving_id == *expected_surv,
            ExpectedOutcome::NoOp { .. } => rows_merged == 0,
            // Tag / skill outcomes are checked by their own simulators —
            // a memory_op fixture that uses one of those variants is a
            // mis-categorised case and should fail loudly here rather
            // than silently pass.
            ExpectedOutcome::TagMerged { .. }
            | ExpectedOutcome::TagNoOp { .. }
            | ExpectedOutcome::SkillUpdated { .. }
            | ExpectedOutcome::SkillNoOp { .. } => false,
        };

        Ok(SimulatorOutput {
            case_name: case.name.clone(),
            passed,
            baseline,
            shadow,
            latency_ms: started.elapsed().as_millis() as u64,
            error: None,
        })
    }
}

/// Open the runner-prepared tempdir DB. `create_if_missing(false)` is a
/// guardrail: if the runner forgot to seed, we want a hard error here,
/// not a silent empty DB that "passes" every case.
async fn open_pool(kb_path: &Path) -> Result<SqlitePool, SimulatorError> {
    let opts = SqliteConnectOptions::new()
        .filename(kb_path)
        .create_if_missing(false);
    SqlitePoolOptions::new()
        .max_connections(1)
        .connect_with(opts)
        .await
        .map_err(|e| SimulatorError::Sqlite {
            step: "open_pool",
            source: e,
        })
}

async fn capture_baseline(
    pool: &SqlitePool,
    parsed_ids: &[i64],
) -> Result<Map<String, Value>, SimulatorError> {
    let chunks_total: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM chunks")
        .fetch_one(pool)
        .await
        .map_err(|e| SimulatorError::Sqlite {
            step: "baseline.count",
            source: e,
        })?;

    let mut existing_ids: Vec<i64> = Vec::new();
    let mut target_contents: Map<String, Value> = Map::new();
    for id in parsed_ids {
        if let Some(content) = fetch_content(pool, *id).await? {
            existing_ids.push(*id);
            target_contents.insert(id.to_string(), Value::String(preview(&content)));
        }
    }

    let surviving_id_candidate = *parsed_ids.iter().min().expect("len>=2");

    let mut baseline = Map::new();
    baseline.insert("chunks_total".into(), json!(chunks_total));
    baseline.insert(
        "target_chunk_ids".into(),
        Value::Array(existing_ids.iter().map(|i| json!(i)).collect()),
    );
    baseline.insert("target_contents".into(), Value::Object(target_contents));
    baseline.insert(
        "surviving_id_candidate".into(),
        json!(surviving_id_candidate),
    );
    Ok(baseline)
}

/// Pull the existing-id list back out of a baseline map. We round-trip
/// through the map (rather than passing a separate Vec) so the captured
/// metric and the dispatch decision can never disagree.
fn parsed_existing_ids(baseline: &Map<String, Value>) -> Vec<i64> {
    baseline
        .get("target_chunk_ids")
        .and_then(|v| v.as_array())
        .map(|arr| arr.iter().filter_map(|v| v.as_i64()).collect())
        .unwrap_or_default()
}

async fn fetch_content(pool: &SqlitePool, id: i64) -> Result<Option<String>, SimulatorError> {
    let row = sqlx::query("SELECT content FROM chunks WHERE id = ?1")
        .bind(id)
        .fetch_optional(pool)
        .await
        .map_err(|e| SimulatorError::Sqlite {
            step: "fetch_content",
            source: e,
        })?;
    Ok(row.map(|r| r.get::<String, _>(0)))
}

/// Delete every parsed id except `surviving_id`. Returns
/// `(rows_merged, surviving_content)`. `rows_merged` is `N-1` on a clean
/// run; the actual `rows_affected` from sqlite is what we report so a
/// drift between baseline and apply (someone else mutating the tempdir
/// mid-run, schema oddity) shows up in the shadow metric.
async fn apply_merge(
    pool: &SqlitePool,
    surviving_id: i64,
    parsed_ids: &[i64],
) -> Result<(u32, String), SimulatorError> {
    let to_delete: Vec<i64> = parsed_ids
        .iter()
        .copied()
        .filter(|id| *id != surviving_id)
        .collect();

    // Build "?,?,?" placeholders dynamically — sqlx doesn't expand Vec
    // bindings for IN(...).
    let placeholders = to_delete.iter().map(|_| "?").collect::<Vec<_>>().join(",");
    let sql = format!("DELETE FROM chunks WHERE id IN ({placeholders})");
    let mut q = sqlx::query(&sql);
    for id in &to_delete {
        q = q.bind(id);
    }
    let result = q.execute(pool).await.map_err(|e| SimulatorError::Sqlite {
        step: "apply_merge.delete",
        source: e,
    })?;

    let rows_merged = result.rows_affected() as u32;
    let surviving_content = fetch_content(pool, surviving_id).await?.unwrap_or_default();
    Ok((rows_merged, surviving_content))
}

async fn capture_shadow(
    pool: &SqlitePool,
    surviving_id: i64,
    rows_merged: u32,
    surviving_content: &str,
) -> Result<Map<String, Value>, SimulatorError> {
    let chunks_total: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM chunks")
        .fetch_one(pool)
        .await
        .map_err(|e| SimulatorError::Sqlite {
            step: "shadow.count",
            source: e,
        })?;

    let mut shadow = Map::new();
    shadow.insert("chunks_total".into(), json!(chunks_total));
    shadow.insert("surviving_chunk_id".into(), json!(surviving_id));
    shadow.insert("rows_merged".into(), json!(rows_merged));
    shadow.insert(
        "surviving_content".into(),
        Value::String(preview(surviving_content)),
    );
    Ok(shadow)
}

// ---------------------------------------------------------------------------
// TagRebalanceSimulator
// ---------------------------------------------------------------------------

/// Simulator for `tag_rebalance` proposals: re-points `chunk_tags` rows
/// from a leaf `tag_nodes` row to its parent and drops the leaf, mirroring
/// the gateway applier's `apply_tag_rebalance` SQL inline (no gateway
/// dep). NoOp = target path didn't resolve to a node, so nothing moved.
pub struct TagRebalanceSimulator;

#[async_trait]
impl KindSimulator for TagRebalanceSimulator {
    fn kind(&self) -> EvolutionKind {
        EvolutionKind::TagRebalance
    }

    async fn simulate(
        &self,
        case: &EvalCase,
        kb_path: &Path,
    ) -> Result<SimulatorOutput, SimulatorError> {
        let started = Instant::now();

        // Parse target shape — anything other than `merge_tag:<path>` is
        // a per-case failure, not a runner crash.
        let path = match case.proposal.target.strip_prefix("merge_tag:") {
            Some(p) if !p.is_empty() => p.to_string(),
            _ => {
                return Ok(SimulatorOutput {
                    case_name: case.name.clone(),
                    passed: false,
                    baseline: Map::new(),
                    shadow: Map::new(),
                    latency_ms: started.elapsed().as_millis() as u64,
                    error: Some(format!(
                        "invalid target {:?}: expected 'merge_tag:<path>'",
                        case.proposal.target
                    )),
                });
            }
        };

        let pool = open_pool(kb_path).await?;

        // Baseline: total nodes, target+parent ids, and chunk_tags count
        // pointing at the target. `target_node_id == None` means the
        // path doesn't exist — the runner-determined NoOp branch.
        let tag_nodes_total: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM tag_nodes")
            .fetch_one(&pool)
            .await
            .map_err(|e| SimulatorError::Sqlite {
                step: "tag_baseline.count",
                source: e,
            })?;

        let target_row: Option<(i64, Option<i64>)> =
            sqlx::query_as("SELECT id, parent_id FROM tag_nodes WHERE path = ?1")
                .bind(&path)
                .fetch_optional(&pool)
                .await
                .map_err(|e| SimulatorError::Sqlite {
                    step: "tag_baseline.lookup",
                    source: e,
                })?;

        let (target_id, parent_id_opt) = match target_row {
            Some((id, parent)) => (Some(id), parent),
            None => (None, None),
        };

        let chunks_under_target: i64 = if let Some(tid) = target_id {
            sqlx::query_scalar("SELECT COUNT(*) FROM chunk_tags WHERE tag_node_id = ?1")
                .bind(tid)
                .fetch_one(&pool)
                .await
                .map_err(|e| SimulatorError::Sqlite {
                    step: "tag_baseline.chunks",
                    source: e,
                })?
        } else {
            0
        };

        let mut baseline = Map::new();
        baseline.insert("tag_nodes_total".into(), json!(tag_nodes_total));
        baseline.insert("target_path".into(), json!(path));
        baseline.insert("target_node_id".into(), json!(target_id));
        baseline.insert("parent_id".into(), json!(parent_id_opt));
        baseline.insert("chunk_tags_under_target".into(), json!(chunks_under_target));

        // Apply: only when target exists AND has a parent. Root merges
        // (parent NULL) and missing nodes both fall through to NoOp.
        let mut moved_chunk_count: u32 = 0;
        let mut node_deleted = false;
        if let (Some(tid), Some(pid)) = (target_id, parent_id_opt) {
            // Conflict-DELETE before UPDATE — same idempotence guard the
            // gateway applier uses (chunk_tags PK is (chunk_id,tag_node_id)).
            sqlx::query(
                "DELETE FROM chunk_tags WHERE tag_node_id = ?1 \
                 AND chunk_id IN (SELECT chunk_id FROM chunk_tags WHERE tag_node_id = ?2)",
            )
            .bind(tid)
            .bind(pid)
            .execute(&pool)
            .await
            .map_err(|e| SimulatorError::Sqlite {
                step: "tag_apply.dedupe",
                source: e,
            })?;
            let upd = sqlx::query("UPDATE chunk_tags SET tag_node_id = ?1 WHERE tag_node_id = ?2")
                .bind(pid)
                .bind(tid)
                .execute(&pool)
                .await
                .map_err(|e| SimulatorError::Sqlite {
                    step: "tag_apply.reparent",
                    source: e,
                })?;
            moved_chunk_count = upd.rows_affected() as u32;

            let del = sqlx::query("DELETE FROM tag_nodes WHERE id = ?1")
                .bind(tid)
                .execute(&pool)
                .await
                .map_err(|e| SimulatorError::Sqlite {
                    step: "tag_apply.delete",
                    source: e,
                })?;
            node_deleted = del.rows_affected() > 0;
        }

        // Shadow: post-state counts so the operator UI can diff.
        let post_total: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM tag_nodes")
            .fetch_one(&pool)
            .await
            .map_err(|e| SimulatorError::Sqlite {
                step: "tag_shadow.count",
                source: e,
            })?;
        let chunks_under_parent: i64 = if let Some(pid) = parent_id_opt {
            sqlx::query_scalar("SELECT COUNT(*) FROM chunk_tags WHERE tag_node_id = ?1")
                .bind(pid)
                .fetch_one(&pool)
                .await
                .map_err(|e| SimulatorError::Sqlite {
                    step: "tag_shadow.chunks",
                    source: e,
                })?
        } else {
            0
        };

        let mut shadow = Map::new();
        shadow.insert("tag_nodes_total".into(), json!(post_total));
        shadow.insert(
            "target_node_present".into(),
            json!(target_id.is_some() && !node_deleted),
        );
        shadow.insert("moved_chunk_count".into(), json!(moved_chunk_count));
        shadow.insert("chunks_now_under_parent".into(), json!(chunks_under_parent));

        let (passed, error) = match &case.expected {
            ExpectedOutcome::TagMerged {
                src_path,
                parent_id: expected_parent,
                moved_chunk_count: expected_moved,
                ..
            } => {
                let ok = node_deleted
                    && src_path == &path
                    && parent_id_opt == Some(*expected_parent)
                    && moved_chunk_count == *expected_moved;
                (ok, None)
            }
            ExpectedOutcome::TagNoOp { .. } => (
                target_id.is_none() && moved_chunk_count == 0 && !node_deleted,
                None,
            ),
            _ => (
                false,
                Some("expected outcome shape mismatch for kind".to_string()),
            ),
        };

        Ok(SimulatorOutput {
            case_name: case.name.clone(),
            passed,
            baseline,
            shadow,
            latency_ms: started.elapsed().as_millis() as u64,
            error,
        })
    }
}

// ---------------------------------------------------------------------------
// SkillUpdateSimulator
// ---------------------------------------------------------------------------

/// Simulator for `skill_update` proposals: replays the `__APPEND__`
/// hunk against the runner-prepared per-case `<tempdir>/skills/` dir.
/// The simulator never touches the production `skills_dir` — only
/// `kb_path.parent().join("skills")`, which the runner owns.
pub struct SkillUpdateSimulator;

#[async_trait]
impl KindSimulator for SkillUpdateSimulator {
    fn kind(&self) -> EvolutionKind {
        EvolutionKind::SkillUpdate
    }

    async fn simulate(
        &self,
        case: &EvalCase,
        kb_path: &Path,
    ) -> Result<SimulatorOutput, SimulatorError> {
        let started = Instant::now();

        // Phase 3.1 sandbox enforcement.
        //
        // The runner hands us a tempdir-rooted `kb_path`, but `kb_path`
        // itself is untrusted from the simulator's perspective: a
        // racing process could pre-create / symlink the parent
        // directory before the runner gets to it. Canonicalise the
        // tempdir root *and* the system temp_dir, then assert
        // containment — TOCTOU dodges between the assert and the
        // write are closed by re-canonicalising the parent right
        // before the write below.
        let kb_path_canonical =
            kb_path
                .canonicalize()
                .map_err(|e| SimulatorError::PathRejected {
                    path: kb_path.to_path_buf(),
                    reason: format!("canonicalize kb_path: {e}"),
                })?;
        let temp_root =
            std::env::temp_dir()
                .canonicalize()
                .map_err(|e| SimulatorError::PathRejected {
                    path: kb_path.to_path_buf(),
                    reason: format!("canonicalize temp_dir: {e}"),
                })?;
        if !kb_path_canonical.starts_with(&temp_root) {
            return Err(SimulatorError::PathRejected {
                path: kb_path.to_path_buf(),
                reason: format!("kb_path canonicalises outside temp_dir {temp_root:?}"),
            });
        }

        // Runner contract: skills tempdir is a sibling of kb.sqlite. We
        // never resolve outside it, and we never read prod paths.
        let skills_dir = kb_path_canonical
            .parent()
            .unwrap_or_else(|| Path::new("."))
            .join("skills");

        // Validate target shape — same rules the gateway applier uses.
        let target = &case.proposal.target;
        let basename = match target.strip_prefix("skills/") {
            Some(b)
                if b.ends_with(".md") && !b.is_empty() && !b.contains('/') && !b.contains("..") =>
            {
                b.to_string()
            }
            _ => {
                let out = failed_skill_output(
                    case,
                    started,
                    format!("invalid target {target:?}: expected 'skills/<name>.md'"),
                );
                return Ok(out);
            }
        };

        let path = skills_dir.join(&basename);

        // Baseline read; missing file → SkillNoOp branch.
        let prior_meta = tokio::fs::metadata(&path).await;
        let prior_content = match prior_meta {
            Ok(m) if m.is_file() => match tokio::fs::read_to_string(&path).await {
                Ok(s) => Some(s),
                Err(e) => {
                    return Ok(failed_skill_output(
                        case,
                        started,
                        format!("read prior: {e}"),
                    ));
                }
            },
            _ => None,
        };

        let baseline_size = prior_content.as_ref().map(|s| s.len() as u64).unwrap_or(0);
        let prior_sha = prior_content
            .as_deref()
            .map(sha256_short)
            .unwrap_or_else(|| "absent".to_string());

        let mut baseline = Map::new();
        baseline.insert("file".into(), json!(target));
        baseline.insert("file_present".into(), json!(prior_content.is_some()));
        baseline.insert("file_size".into(), json!(baseline_size));
        baseline.insert("byte_count".into(), json!(baseline_size));
        baseline.insert("prior_content_sha".into(), json!(prior_sha));

        // Missing file: SkillNoOp without diff parse — matches the
        // applier's SkillFileMissing reject.
        let Some(prior) = prior_content else {
            let mut shadow = Map::new();
            shadow.insert("file".into(), json!(target));
            shadow.insert("applied".into(), json!(false));
            shadow.insert("file_size".into(), json!(0u64));
            shadow.insert("byte_count".into(), json!(0u64));
            shadow.insert("appended_bytes".into(), json!(0u64));
            let passed = matches!(case.expected, ExpectedOutcome::SkillNoOp { .. });
            return Ok(SimulatorOutput {
                case_name: case.name.clone(),
                passed,
                baseline,
                shadow,
                latency_ms: started.elapsed().as_millis() as u64,
                error: if passed {
                    None
                } else {
                    Some("skill file missing — expected SkillNoOp".to_string())
                },
            });
        };

        // Parse the diff. v0.3 only ships `__APPEND__`; anything else
        // collapses to a SkillNoOp.
        let appended_lines = match parse_append_diff(&case.proposal.diff) {
            Ok(v) => v,
            Err(reason) => {
                let mut shadow = Map::new();
                shadow.insert("file".into(), json!(target));
                shadow.insert("applied".into(), json!(false));
                shadow.insert("file_size".into(), json!(baseline_size));
                shadow.insert("byte_count".into(), json!(baseline_size));
                shadow.insert("appended_bytes".into(), json!(0u64));
                shadow.insert("reject_reason".into(), json!(reason));
                let passed = matches!(case.expected, ExpectedOutcome::SkillNoOp { .. });
                return Ok(SimulatorOutput {
                    case_name: case.name.clone(),
                    passed,
                    baseline,
                    shadow,
                    latency_ms: started.elapsed().as_millis() as u64,
                    error: if passed {
                        None
                    } else {
                        Some(format!("diff rejected: {reason}"))
                    },
                });
            }
        };

        let mut new_content = prior.clone();
        if !new_content.is_empty() && !new_content.ends_with('\n') {
            new_content.push('\n');
        }
        for line in &appended_lines {
            new_content.push_str(line);
            new_content.push('\n');
        }

        // Re-validate the parent dir canonicalises under temp_root
        // *immediately* before the write. If a racing process
        // swapped `<tempdir>/skills` for a symlink to `/etc` between
        // the entry-point check and now, the second canonicalise
        // surfaces it and we reject. Belt-and-suspenders to the
        // entry-point check above.
        if let Some(parent) = path.parent() {
            if let Ok(parent_canon) = parent.canonicalize() {
                if !parent_canon.starts_with(&temp_root) {
                    return Err(SimulatorError::PathRejected {
                        path: path.clone(),
                        reason: format!(
                            "parent dir {parent_canon:?} escaped temp_root {temp_root:?}"
                        ),
                    });
                }
            }
        }
        if let Err(e) = tokio::fs::write(&path, new_content.as_bytes()).await {
            return Ok(failed_skill_output(case, started, format!("write: {e}")));
        }

        let new_size = new_content.len() as u64;
        let appended_bytes = new_size.saturating_sub(baseline_size);

        let mut shadow = Map::new();
        shadow.insert("file".into(), json!(target));
        shadow.insert("applied".into(), json!(true));
        shadow.insert("file_size".into(), json!(new_size));
        shadow.insert("byte_count".into(), json!(new_size));
        shadow.insert("appended_bytes".into(), json!(appended_bytes));

        let (passed, error) = match &case.expected {
            ExpectedOutcome::SkillUpdated {
                file,
                content_includes,
                ..
            } => {
                let basename_match = file
                    .strip_prefix("skills/")
                    .map(|f| f == basename)
                    .unwrap_or(false);
                let ok = basename_match && new_content.contains(content_includes.as_str());
                (ok, None)
            }
            ExpectedOutcome::SkillNoOp { .. } => (new_size == baseline_size, None),
            _ => (
                false,
                Some("expected outcome shape mismatch for kind".to_string()),
            ),
        };

        Ok(SimulatorOutput {
            case_name: case.name.clone(),
            passed,
            baseline,
            shadow,
            latency_ms: started.elapsed().as_millis() as u64,
            error,
        })
    }
}

/// First 16 hex chars of SHA-256 over the bytes — short enough to fit
/// the per-case JSON without bloating the row.
fn sha256_short(s: &str) -> String {
    let mut h = Sha256::new();
    h.update(s.as_bytes());
    let digest = h.finalize();
    let mut out = String::with_capacity(16);
    for b in digest.iter().take(8) {
        use std::fmt::Write as _;
        let _ = write!(out, "{b:02x}");
    }
    out
}

/// Build a parse-failure / IO-failure SkillUpdate output up-front.
fn failed_skill_output(case: &EvalCase, started: Instant, error: String) -> SimulatorOutput {
    SimulatorOutput {
        case_name: case.name.clone(),
        passed: false,
        baseline: Map::new(),
        shadow: Map::new(),
        latency_ms: started.elapsed().as_millis() as u64,
        error: Some(error),
    }
}

/// Parse the `__APPEND__`-shaped diff the Step-1 EvolutionEngine emits.
/// Mirrors the gateway applier's `parse_append_diff` byte-for-byte —
/// kept inline because the shadow-tester crate has no gateway dep.
fn parse_append_diff(diff: &str) -> Result<Vec<String>, String> {
    let mut lines = diff.lines();
    let mut found_hunk = false;
    let mut appended: Vec<String> = Vec::new();
    while let Some(line) = lines.next() {
        if line.starts_with("--- ") || line.starts_with("+++ ") {
            continue;
        }
        if line.starts_with("@@") {
            if !line.contains("__APPEND__") {
                return Err(format!("unsupported hunk header: {line}"));
            }
            found_hunk = true;
            for body in lines.by_ref() {
                if let Some(stripped) = body.strip_prefix('+') {
                    appended.push(stripped.to_string());
                } else if body.is_empty() {
                    continue;
                } else {
                    return Err(format!("non-append body line: {body}"));
                }
            }
            break;
        }
        if !line.is_empty() {
            return Err(format!("non-header line before hunk: {line}"));
        }
    }
    if !found_hunk {
        return Err("no hunk header".to_string());
    }
    Ok(appended)
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::eval::ProposalSpec;
    use corlinman_evolution::EvolutionRisk;
    use sqlx::sqlite::SqliteConnectOptions;
    use std::path::PathBuf;
    use tempfile::TempDir;

    // ---- parse_merge_target ----

    #[test]
    fn parse_merge_target_happy_path() {
        let ids = parse_merge_target("merge_chunks:1,2,3").unwrap();
        assert_eq!(ids, vec![1, 2, 3]);
    }

    #[test]
    fn parse_merge_target_rejects_missing_prefix() {
        let err = parse_merge_target("not_a_merge:1,2").unwrap_err();
        assert!(
            matches!(err, SimulatorError::InvalidTarget { .. }),
            "got {err:?}"
        );
    }

    #[test]
    fn parse_merge_target_rejects_single_id() {
        let err = parse_merge_target("merge_chunks:1").unwrap_err();
        assert!(
            matches!(err, SimulatorError::InvalidTarget { .. }),
            "got {err:?}"
        );
    }

    #[test]
    fn parse_merge_target_rejects_non_integer() {
        let err = parse_merge_target("merge_chunks:1,abc").unwrap_err();
        assert!(
            matches!(err, SimulatorError::InvalidTarget { .. }),
            "got {err:?}"
        );
    }

    #[test]
    fn parse_merge_target_rejects_duplicates() {
        let err = parse_merge_target("merge_chunks:1,1").unwrap_err();
        assert!(
            matches!(err, SimulatorError::InvalidTarget { .. }),
            "got {err:?}"
        );
    }

    // ---- simulate ----

    /// Build a tempdir SQLite with the v0.3 chunks/files schema (minus
    /// FTS triggers — simulator never touches FTS) and seed it via the
    /// caller's SQL list. Returns `(tmp, kb_path)`; the tmp guard must
    /// outlive the test.
    async fn make_kb(seed: &[&str]) -> (TempDir, PathBuf) {
        let tmp = TempDir::new().unwrap();
        let path = tmp.path().join("kb.sqlite");
        let opts = SqliteConnectOptions::new()
            .filename(&path)
            .create_if_missing(true);
        let pool = SqlitePoolOptions::new()
            .max_connections(1)
            .connect_with(opts)
            .await
            .unwrap();

        let bootstrap = [
            "CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT, diary_name TEXT, checksum TEXT, mtime INTEGER, size INTEGER);",
            "CREATE TABLE chunks (id INTEGER PRIMARY KEY, file_id INTEGER, chunk_index INTEGER, content TEXT, namespace TEXT DEFAULT 'general');",
            "INSERT INTO files VALUES (1, 'fx.md', 'fixture', 'h', 0, 0);",
        ];
        for s in bootstrap.iter().chain(seed.iter()) {
            sqlx::query(s).execute(&pool).await.unwrap();
        }
        pool.close().await;
        (tmp, path)
    }

    fn case(name: &str, target: &str, expected: ExpectedOutcome) -> EvalCase {
        EvalCase {
            name: name.to_string(),
            kind: Some(EvolutionKind::MemoryOp),
            description: "test".into(),
            kb_seed: vec![],
            skill_seed: Default::default(),
            proposal: ProposalSpec {
                target: target.to_string(),
                reasoning: "test".into(),
                risk: EvolutionRisk::High,
                signal_ids: vec![],
                diff: String::new(),
            },
            expected,
        }
    }

    #[tokio::test]
    async fn simulate_returns_merged_for_existing_chunks() {
        let (_tmp, kb) = make_kb(&[
            "INSERT INTO chunks(id, file_id, chunk_index, content, namespace) VALUES (1, 1, 0, 'alpha', 'general');",
            "INSERT INTO chunks(id, file_id, chunk_index, content, namespace) VALUES (2, 1, 1, 'beta', 'general');",
        ])
        .await;

        let c = case(
            "merged",
            "merge_chunks:1,2",
            ExpectedOutcome::Merged {
                rows_merged: 1,
                surviving_chunk_id: 1,
                latency_ms_max: 500,
            },
        );
        let out = MemoryOpSimulator.simulate(&c, &kb).await.unwrap();
        assert!(out.passed, "expected pass; out={out:?}");
        assert_eq!(
            out.shadow.get("rows_merged").and_then(|v| v.as_u64()),
            Some(1)
        );
        assert_eq!(
            out.shadow
                .get("surviving_chunk_id")
                .and_then(|v| v.as_i64()),
            Some(1)
        );
        assert!(out.error.is_none());
    }

    #[tokio::test]
    async fn simulate_returns_noop_when_target_missing() {
        let (_tmp, kb) = make_kb(&[
            "INSERT INTO chunks(id, file_id, chunk_index, content, namespace) VALUES (1, 1, 0, 'only', 'general');",
        ])
        .await;

        let c = case(
            "noop",
            "merge_chunks:1,99",
            ExpectedOutcome::NoOp {
                latency_ms_max: 500,
            },
        );
        let out = MemoryOpSimulator.simulate(&c, &kb).await.unwrap();
        assert!(out.passed, "expected pass; out={out:?}");
        assert_eq!(
            out.shadow.get("rows_merged").and_then(|v| v.as_u64()),
            Some(0)
        );
    }

    #[tokio::test]
    async fn simulate_invalid_target_marks_failed() {
        let (_tmp, kb) = make_kb(&[
            "INSERT INTO chunks(id, file_id, chunk_index, content, namespace) VALUES (1, 1, 0, 'x', 'general');",
        ])
        .await;

        let c = case(
            "bad",
            "not_a_merge:1,2",
            ExpectedOutcome::NoOp {
                latency_ms_max: 500,
            },
        );
        let out = MemoryOpSimulator.simulate(&c, &kb).await.unwrap();
        assert!(!out.passed);
        assert!(out.error.is_some(), "expected error string, got {out:?}");
        assert!(out.baseline.is_empty());
        assert!(out.shadow.is_empty());
    }

    #[tokio::test]
    async fn simulate_records_baseline_and_shadow_keys() {
        let (_tmp, kb) = make_kb(&[
            "INSERT INTO chunks(id, file_id, chunk_index, content, namespace) VALUES (1, 1, 0, 'a', 'general');",
            "INSERT INTO chunks(id, file_id, chunk_index, content, namespace) VALUES (2, 1, 1, 'b', 'general');",
        ])
        .await;

        let c = case(
            "keys",
            "merge_chunks:1,2",
            ExpectedOutcome::Merged {
                rows_merged: 1,
                surviving_chunk_id: 1,
                latency_ms_max: 500,
            },
        );
        let out = MemoryOpSimulator.simulate(&c, &kb).await.unwrap();
        for k in [
            "chunks_total",
            "target_chunk_ids",
            "target_contents",
            "surviving_id_candidate",
        ] {
            assert!(out.baseline.contains_key(k), "baseline missing {k}");
        }
        for k in [
            "chunks_total",
            "surviving_chunk_id",
            "rows_merged",
            "surviving_content",
        ] {
            assert!(out.shadow.contains_key(k), "shadow missing {k}");
        }
    }

    // ---- Phase 3.1: skill-update sandbox enforcement ---------------------

    /// SkillUpdateSimulator must reject a `kb_path` whose canonicalised
    /// form lands outside `std::env::temp_dir()`. Build a tempdir that
    /// looks valid, then symlink `<tempdir>/skills` to a non-temp
    /// directory before running the simulator. The pre-write
    /// re-canonicalise should catch the escape and return PathRejected
    /// instead of issuing the write.
    ///
    /// Skipped on Windows because `std::os::unix::fs::symlink` isn't
    /// available there; the rest of the simulator is unaffected.
    #[cfg(unix)]
    #[tokio::test]
    async fn simulate_rejects_symlinked_skills_dir_escape() {
        use crate::eval::ProposalSpec;
        use std::os::unix::fs::symlink;

        // Outside-of-temp target: a freshly-made tempdir is itself
        // under temp_dir(), so we use the workspace's parent dir
        // — which lives under `/Users/.../...` on macOS, well outside
        // `/tmp` and `/var/folders/.../`. We don't write into it; the
        // assertion is just on the canonicalize boundary.
        let outside = std::env::current_dir().unwrap();
        let outside = outside.canonicalize().unwrap();

        let temp_root = std::env::temp_dir().canonicalize().unwrap();
        // Sanity guard for the test itself: if the workspace
        // happens to live under temp_root (rare CI shape), skip.
        if outside.starts_with(&temp_root) {
            eprintln!("skipping: workspace dir is under temp_root");
            return;
        }

        // Build a kb_path whose tempdir-sibling `skills` directory
        // is a symlink pointing outside temp_root.
        let tmp = TempDir::new().unwrap();
        let kb_path = tmp.path().join("kb.sqlite");
        // Create the kb file so canonicalize succeeds for kb_path.
        std::fs::write(&kb_path, b"").unwrap();
        // Plant the symlink: tempdir/skills -> outside/.
        let skills_link = tmp.path().join("skills");
        symlink(&outside, &skills_link).unwrap();

        // Diff is otherwise valid; the per-write canonicalize is
        // what should reject.
        let case = EvalCase {
            name: "symlink-escape".into(),
            kind: Some(EvolutionKind::SkillUpdate),
            description: "test".into(),
            kb_seed: vec![],
            skill_seed: Default::default(),
            proposal: ProposalSpec {
                target: "skills/web_search.md".into(),
                reasoning: "test".into(),
                risk: EvolutionRisk::High,
                signal_ids: vec![],
                diff: "--- a/skills/web_search.md\n+++ b/skills/web_search.md\n@@ __APPEND__,0 +__APPEND__,1 @@\n+x\n".into(),
            },
            expected: ExpectedOutcome::SkillUpdated {
                file: "skills/web_search.md".into(),
                content_includes: "x".into(),
                latency_ms_max: 500,
            },
        };
        // Pre-seed the symlinked target file *outside* the tempdir
        // so the simulator's read path doesn't bail early on a
        // missing file — that's a different code path. We don't
        // care if the prior file already exists; we only assert the
        // simulator refuses to write through the symlink.
        let prior_path = outside.join("web_search.md");
        let pre_existed = prior_path.exists();
        if !pre_existed {
            std::fs::write(&prior_path, b"prior\n").unwrap();
        }

        let result = SkillUpdateSimulator.simulate(&case, &kb_path).await;
        // Cleanup before assertions so a panic doesn't leave the
        // test fixture in `outside` for the next run.
        if !pre_existed {
            let _ = std::fs::remove_file(&prior_path);
        }

        match result {
            Err(SimulatorError::PathRejected { path: _, reason }) => {
                assert!(
                    reason.contains("temp_root") || reason.contains("temp_dir"),
                    "expected sandbox-boundary message, got {reason:?}"
                );
            }
            other => panic!("expected PathRejected, got {other:?}"),
        }
    }

    /// Happy path: a normal tempdir-only kb_path simulator run still
    /// passes the canonicalize boundary check. Pins that the new
    /// validation isn't gating legit cases.
    #[tokio::test]
    async fn simulate_accepts_clean_tempdir_kb_path() {
        use crate::eval::ProposalSpec;

        let tmp = TempDir::new().unwrap();
        let kb_path = tmp.path().join("kb.sqlite");
        std::fs::write(&kb_path, b"").unwrap();
        let skills_dir = tmp.path().join("skills");
        std::fs::create_dir_all(&skills_dir).unwrap();
        std::fs::write(skills_dir.join("web_search.md"), b"prior\n").unwrap();

        let case = EvalCase {
            name: "clean".into(),
            kind: Some(EvolutionKind::SkillUpdate),
            description: "test".into(),
            kb_seed: vec![],
            skill_seed: Default::default(),
            proposal: ProposalSpec {
                target: "skills/web_search.md".into(),
                reasoning: "test".into(),
                risk: EvolutionRisk::High,
                signal_ids: vec![],
                diff: "--- a/skills/web_search.md\n+++ b/skills/web_search.md\n@@ __APPEND__,0 +__APPEND__,1 @@\n+x\n".into(),
            },
            expected: ExpectedOutcome::SkillUpdated {
                file: "skills/web_search.md".into(),
                content_includes: "x".into(),
                latency_ms_max: 500,
            },
        };
        let out = SkillUpdateSimulator
            .simulate(&case, &kb_path)
            .await
            .unwrap();
        assert!(out.passed, "clean tempdir kb_path must pass; out={out:?}");
    }
}
