//! Integration tests for `corlinman vector` subcommands.
//!
//! These spawn the compiled `corlinman` binary via `assert_cmd` and
//! point it at an isolated `CORLINMAN_DATA_DIR` tempdir so the tests
//! never touch the developer's real knowledge base.

use assert_cmd::Command;
use tempfile::TempDir;

/// Spawn `corlinman` with `CORLINMAN_DATA_DIR` set to `tmp`. Uses the
/// `CARGO_BIN_EXE_corlinman` binary that assert_cmd resolves from the
/// integration-test environment.
fn corlinman(tmp: &TempDir) -> Command {
    let mut cmd = Command::cargo_bin("corlinman").expect("compile corlinman bin");
    cmd.env("CORLINMAN_DATA_DIR", tmp.path());
    cmd
}

#[test]
fn stats_on_empty_db_reports_zeroes() {
    let tmp = TempDir::new().unwrap();
    let out = corlinman(&tmp).args(["vector", "stats"]).output().unwrap();
    assert!(
        out.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    let stdout = String::from_utf8_lossy(&out.stdout);
    assert!(stdout.contains("Chunks:  0"), "unexpected stdout: {stdout}");
    assert!(stdout.contains("Files:   0"), "unexpected stdout: {stdout}");
    assert!(stdout.contains("Tags:    0"), "unexpected stdout: {stdout}");
}

#[test]
fn stats_json_shape_matches_schema() {
    let tmp = TempDir::new().unwrap();
    let out = corlinman(&tmp)
        .args(["vector", "stats", "--json"])
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    let stdout = String::from_utf8_lossy(&out.stdout);
    let v: serde_json::Value = serde_json::from_str(stdout.trim())
        .unwrap_or_else(|e| panic!("parse json: {e}\nstdout={stdout}"));
    for key in ["chunks", "files", "tags", "index_bytes"] {
        assert!(v.get(key).is_some(), "missing key {key} in {v}");
    }
    assert_eq!(v["chunks"].as_i64(), Some(0));
    assert_eq!(v["files"].as_i64(), Some(0));
    assert_eq!(v["tags"].as_i64(), Some(0));
    assert_eq!(v["index_bytes"].as_u64(), Some(0));
}

#[test]
fn rebuild_without_confirm_is_a_dry_run() {
    let tmp = TempDir::new().unwrap();
    // Seed a tiny knowledge source so the dry-run has something to report.
    let src = tmp.path().join("knowledge");
    std::fs::create_dir_all(&src).unwrap();
    std::fs::write(
        src.join("one.md"),
        "para one line one\n\npara two line one\n",
    )
    .unwrap();

    let out = corlinman(&tmp)
        .args(["vector", "rebuild"])
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    let stdout = String::from_utf8_lossy(&out.stdout);
    assert!(
        stdout.contains("will rebuild"),
        "expected dry-run banner, got: {stdout}"
    );
    assert!(
        stdout.contains("--confirm"),
        "dry-run should instruct --confirm: {stdout}"
    );

    // No usearch file should have been written.
    let usearch = tmp.path().join("knowledge_base.usearch");
    assert!(!usearch.exists(), "dry-run must not touch {:?}", usearch);
}

#[test]
fn query_without_index_reports_friendly_error() {
    let tmp = TempDir::new().unwrap();
    let out = corlinman(&tmp)
        .args(["vector", "query", "anything", "-k", "3"])
        .output()
        .unwrap();
    // Exit non-zero + message pointing at `rebuild`.
    assert!(
        !out.status.success(),
        "expected failure; stdout={} stderr={}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        stderr.contains("rebuild") || stderr.contains("no index"),
        "stderr should suggest rebuild; got: {stderr}"
    );
}
