//! Eval-case spec + YAML loader for ShadowTester.
//!
//! Cases live as YAML files under `<eval_set_dir>/<kind>/*.yaml`. The
//! per-kind subdir is the contract: it lets [`load_eval_set`] default
//! `kind` from the path so authors don't have to repeat themselves, and
//! makes `ls memory_op/` the way an operator audits coverage.
//!
//! Step 2 of W1-A: types + loader + fixtures only. Step 3 wires these
//! into the simulator.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use corlinman_evolution::{EvolutionKind, EvolutionRisk};
use serde::{Deserialize, Serialize};

/// One YAML-defined test case for one kind.
///
/// `kb_seed` runs raw SQL against a tempdir copy of `kb.sqlite` before
/// the proposal is shadowed. Normally `INSERT INTO chunks ...`; cases
/// that want a from-scratch fixture can include `CREATE TABLE` first.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EvalCase {
    /// Defaults to the YAML file stem in [`load_eval_set`].
    #[serde(default)]
    pub name: String,
    /// Defaults to the directory's kind in [`load_eval_set`].
    #[serde(default)]
    pub kind: Option<EvolutionKind>,
    pub description: String,
    #[serde(default)]
    pub kb_seed: Vec<String>,
    /// `<basename> -> file body` map written into a runner-managed
    /// per-case `<tempdir>/skills/` directory before the simulator runs.
    /// `BTreeMap` keeps YAML serialization stable. Empty for kinds that
    /// don't touch `skills/` (memory_op, tag_rebalance).
    #[serde(default)]
    pub skill_seed: BTreeMap<String, String>,
    pub proposal: ProposalSpec,
    pub expected: ExpectedOutcome,
}

/// Minimum proposal data the simulator needs. Step 3's runner assembles
/// a full `EvolutionProposal` by attaching a generated id + timestamps.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProposalSpec {
    pub target: String,
    pub reasoning: String,
    /// Shadow only fires for medium/high; default High keeps fixtures
    /// in-scope by default.
    #[serde(default = "default_risk")]
    pub risk: EvolutionRisk,
    #[serde(default)]
    pub signal_ids: Vec<i64>,
    /// Unified-diff payload — `skill_update` ships an `__APPEND__`
    /// hunk here. memory_op / tag_rebalance leave it empty (target +
    /// kb_seed are sufficient for those).
    #[serde(default)]
    pub diff: String,
}

fn default_risk() -> EvolutionRisk {
    EvolutionRisk::High
}

/// What the simulator should observe after replaying the proposal.
///
/// `outcome` is the discriminator. New variants extend the enum without
/// breaking existing fixtures because serde tags by field, not order.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(tag = "outcome", rename_all = "snake_case")]
pub enum ExpectedOutcome {
    /// memory_op: a merge that consumed `rows_merged` chunks and kept
    /// `surviving_chunk_id` as the canonical row.
    Merged {
        rows_merged: u32,
        surviving_chunk_id: i64,
        #[serde(default = "default_latency_ms_max")]
        latency_ms_max: u64,
    },
    /// memory_op: simulator detected a bogus / unsafe target.
    NoOp {
        #[serde(default = "default_latency_ms_max")]
        latency_ms_max: u64,
    },
    /// tag_rebalance: `merge_tag` executed — the source tag node is
    /// gone and `parent_id` now owns its `chunk_tags` rows.
    TagMerged {
        src_path: String,
        parent_id: i64,
        moved_chunk_count: u32,
        #[serde(default = "default_latency_ms_max")]
        latency_ms_max: u64,
    },
    /// tag_rebalance: target path didn't resolve to a real `tag_nodes`
    /// row — no rows changed.
    TagNoOp {
        #[serde(default = "default_latency_ms_max")]
        latency_ms_max: u64,
    },
    /// skill_update: file appended; final body must contain
    /// `content_includes` as a substring.
    SkillUpdated {
        file: String,
        content_includes: String,
        #[serde(default = "default_latency_ms_max")]
        latency_ms_max: u64,
    },
    /// skill_update: rejected (unsupported diff shape, missing file,
    /// or invalid target).
    SkillNoOp {
        #[serde(default = "default_latency_ms_max")]
        latency_ms_max: u64,
    },
}

fn default_latency_ms_max() -> u64 {
    500
}

/// All cases loaded from one `<dir>/<kind>/` subdir.
#[derive(Debug, Clone)]
pub struct EvalSet {
    pub kind: EvolutionKind,
    pub cases: Vec<EvalCase>,
    pub loaded_from: PathBuf,
}

/// Step-3 fills in the `metrics` shape per simulator. Kept free-form
/// (`serde_json::Map`) so memory_op vs skill_update don't need a shared
/// schema.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EvalRunResult {
    pub case_name: String,
    pub passed: bool,
    pub metrics: serde_json::Map<String, serde_json::Value>,
    pub error: Option<String>,
}

/// Loader errors. Empty-set is intentionally an error: a misconfigured
/// path that silently shadows zero cases would look "green" forever.
#[derive(Debug, thiserror::Error)]
pub enum EvalLoadError {
    #[error("eval-set dir missing: {0}")]
    MissingDir(PathBuf),
    #[error("io error reading {path}: {source}")]
    Io {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
    #[error("parse failure in {file}: {reason}")]
    ParseFailure { file: PathBuf, reason: String },
    #[error("kind mismatch in {file}: expected {expected:?}, found '{found}'")]
    KindMismatch {
        file: PathBuf,
        expected: EvolutionKind,
        found: String,
    },
    #[error("no eval cases found for kind {kind:?} under {dir}")]
    EmptySet { dir: PathBuf, kind: EvolutionKind },
}

/// Load every `*.yaml`/`*.yml` file from `<eval_set_dir>/<kind>/`.
///
/// Files prefixed with `_` are skipped (drafts). Non-recursive. Cases
/// are sorted by `name` for deterministic ordering across runs.
pub async fn load_eval_set(
    eval_set_dir: &Path,
    kind: EvolutionKind,
) -> Result<EvalSet, EvalLoadError> {
    let dir = eval_set_dir.join(kind.as_str());
    if !tokio::fs::try_exists(&dir)
        .await
        .map_err(|e| EvalLoadError::Io {
            path: dir.clone(),
            source: e,
        })?
    {
        return Err(EvalLoadError::MissingDir(dir));
    }

    let mut entries = tokio::fs::read_dir(&dir)
        .await
        .map_err(|e| EvalLoadError::Io {
            path: dir.clone(),
            source: e,
        })?;
    let mut yaml_files: Vec<PathBuf> = Vec::new();
    while let Some(entry) = entries.next_entry().await.map_err(|e| EvalLoadError::Io {
        path: dir.clone(),
        source: e,
    })? {
        let path = entry.path();
        let Some(file_name) = path.file_name().and_then(|n| n.to_str()) else {
            continue;
        };
        if file_name.starts_with('_') {
            continue;
        }
        let is_yaml = matches!(
            path.extension().and_then(|e| e.to_str()),
            Some("yaml") | Some("yml")
        );
        if !is_yaml {
            continue;
        }
        if !entry
            .file_type()
            .await
            .map_err(|e| EvalLoadError::Io {
                path: path.clone(),
                source: e,
            })?
            .is_file()
        {
            continue;
        }
        yaml_files.push(path);
    }

    let mut cases = Vec::with_capacity(yaml_files.len());
    for file in yaml_files {
        let text = tokio::fs::read_to_string(&file)
            .await
            .map_err(|e| EvalLoadError::Io {
                path: file.clone(),
                source: e,
            })?;
        let mut case: EvalCase =
            serde_yaml::from_str(&text).map_err(|e| EvalLoadError::ParseFailure {
                file: file.clone(),
                reason: e.to_string(),
            })?;

        // Reject explicit-but-wrong kinds; default the unset case to the dir's kind.
        match case.kind {
            Some(found) if found != kind => {
                return Err(EvalLoadError::KindMismatch {
                    file: file.clone(),
                    expected: kind,
                    found: found.as_str().to_string(),
                });
            }
            _ => {
                case.kind = Some(kind);
            }
        }

        if case.name.is_empty() {
            case.name = file
                .file_stem()
                .and_then(|s| s.to_str())
                .unwrap_or("unnamed")
                .to_string();
        }
        cases.push(case);
    }

    if cases.is_empty() {
        return Err(EvalLoadError::EmptySet { dir, kind });
    }

    cases.sort_by(|a, b| a.name.cmp(&b.name));
    Ok(EvalSet {
        kind,
        cases,
        loaded_from: dir,
    })
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn fixtures_root() -> PathBuf {
        // crate-relative: <crate>/tests/fixtures/eval
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("tests")
            .join("fixtures")
            .join("eval")
    }

    async fn write(dir: &Path, name: &str, body: &str) {
        tokio::fs::write(dir.join(name), body).await.unwrap();
    }

    #[tokio::test]
    async fn load_eval_set_returns_missing_dir_when_path_absent() {
        let tmp = TempDir::new().unwrap();
        let err = load_eval_set(tmp.path(), EvolutionKind::MemoryOp)
            .await
            .unwrap_err();
        assert!(matches!(err, EvalLoadError::MissingDir(_)), "got {err:?}");
    }

    #[tokio::test]
    async fn load_eval_set_parses_real_fixtures() {
        let set = load_eval_set(&fixtures_root(), EvolutionKind::MemoryOp)
            .await
            .expect("fixtures should load");
        assert_eq!(set.kind, EvolutionKind::MemoryOp);
        assert_eq!(set.cases.len(), 4, "expected 4 memory_op cases");

        let names: Vec<&str> = set.cases.iter().map(|c| c.name.as_str()).collect();
        assert_eq!(
            names,
            vec![
                "case-001-near-duplicate-merge",
                "case-002-distinct-no-op",
                "case-003-identical-content",
                "case-004-three-way-cluster",
            ]
        );
        assert!(matches!(
            set.cases[0].expected,
            ExpectedOutcome::Merged { .. }
        ));
    }

    #[tokio::test]
    async fn load_eval_set_rejects_malformed_yaml() {
        let tmp = TempDir::new().unwrap();
        let kind_dir = tmp.path().join("memory_op");
        tokio::fs::create_dir_all(&kind_dir).await.unwrap();
        // Broken indentation under `proposal:` keeps it from parsing as a
        // mapping; serde_yaml surfaces the error.
        write(
            &kind_dir,
            "broken.yaml",
            "description: bad\nproposal:\n  target: x\n   reasoning: y\n",
        )
        .await;
        let err = load_eval_set(tmp.path(), EvolutionKind::MemoryOp)
            .await
            .unwrap_err();
        assert!(
            matches!(err, EvalLoadError::ParseFailure { .. }),
            "got {err:?}"
        );
    }

    #[tokio::test]
    async fn load_eval_set_rejects_kind_mismatch() {
        let tmp = TempDir::new().unwrap();
        let kind_dir = tmp.path().join("memory_op");
        tokio::fs::create_dir_all(&kind_dir).await.unwrap();
        write(
            &kind_dir,
            "wrong.yaml",
            r#"
kind: skill_update
description: wrong kind
proposal:
  target: irrelevant
  reasoning: irrelevant
expected:
  outcome: no_op
"#,
        )
        .await;
        let err = load_eval_set(tmp.path(), EvolutionKind::MemoryOp)
            .await
            .unwrap_err();
        assert!(
            matches!(
                err,
                EvalLoadError::KindMismatch { ref found, .. } if found == "skill_update"
            ),
            "got {err:?}"
        );
    }

    #[tokio::test]
    async fn load_eval_set_rejects_empty_dir() {
        let tmp = TempDir::new().unwrap();
        tokio::fs::create_dir_all(tmp.path().join("memory_op"))
            .await
            .unwrap();
        let err = load_eval_set(tmp.path(), EvolutionKind::MemoryOp)
            .await
            .unwrap_err();
        assert!(matches!(err, EvalLoadError::EmptySet { .. }), "got {err:?}");
    }

    #[tokio::test]
    async fn load_eval_set_skips_underscore_prefixed() {
        let tmp = TempDir::new().unwrap();
        let kind_dir = tmp.path().join("memory_op");
        tokio::fs::create_dir_all(&kind_dir).await.unwrap();
        let valid = r#"
description: real case
proposal:
  target: merge_chunks:1,2
  reasoning: dupes
expected:
  outcome: no_op
"#;
        write(&kind_dir, "real.yaml", valid).await;
        // `_draft.yaml` is intentionally bogus; loader must skip it.
        write(&kind_dir, "_draft.yaml", "this is not yaml :::").await;
        let set = load_eval_set(tmp.path(), EvolutionKind::MemoryOp)
            .await
            .unwrap();
        assert_eq!(set.cases.len(), 1);
        assert_eq!(set.cases[0].name, "real");
    }

    #[tokio::test]
    async fn load_eval_set_sorts_cases_by_name() {
        let tmp = TempDir::new().unwrap();
        let kind_dir = tmp.path().join("memory_op");
        tokio::fs::create_dir_all(&kind_dir).await.unwrap();
        let body = |name: &str| {
            format!(
                r#"
name: {name}
description: ordering check
proposal:
  target: t
  reasoning: r
expected:
  outcome: no_op
"#
            )
        };
        // Write out of order on disk; loader must sort by `name`.
        write(&kind_dir, "z.yaml", &body("zebra")).await;
        write(&kind_dir, "a.yaml", &body("alpha")).await;
        write(&kind_dir, "m.yaml", &body("mango")).await;
        let set = load_eval_set(tmp.path(), EvolutionKind::MemoryOp)
            .await
            .unwrap();
        let names: Vec<&str> = set.cases.iter().map(|c| c.name.as_str()).collect();
        assert_eq!(names, vec!["alpha", "mango", "zebra"]);
    }
}
