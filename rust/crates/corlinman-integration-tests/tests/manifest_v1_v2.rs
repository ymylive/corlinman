//! Test 4 — `manifest_v1_migrates_and_v2_loads`.
//!
//! Three scenarios against `parse_manifest_file`:
//!   a) A pure-v1 manifest (no `manifest_version`, no `protocols`/`hooks`/
//!      `skill_refs`) loads with `manifest_version == 2` in memory and the
//!      documented defaults for the new fields.
//!   b) A v2 manifest with every new field populated parses and validates.
//!   c) A manifest declaring `protocols = ["invalid_proto"]` is rejected.

use std::path::PathBuf;

use corlinman_plugins::manifest::{parse_manifest_file, ManifestParseError};
use tempfile::TempDir;

fn write_manifest(body: &str) -> (TempDir, PathBuf) {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("plugin-manifest.toml");
    std::fs::write(&path, body).expect("write manifest");
    (dir, path)
}

#[test]
fn v1_manifest_migrates_to_v2_in_memory() {
    // Pure v1: no manifest_version, no new fields.
    let body = r#"
name = "legacy"
version = "0.1.0"
description = "a legacy v1 plugin"
plugin_type = "sync"

[entry_point]
command = "true"
"#;
    let (_dir, path) = write_manifest(body);
    let m = parse_manifest_file(&path).expect("v1 manifest must parse");

    // C2 (Phase 4 W3) raised MAX_SUPPORTED_MANIFEST_VERSION to 3 to host
    // the new `[mcp]` table; both v1 and v2 manifests now migrate
    // forward to v3 in-memory. Test renamed only in spirit — the
    // round-trip target is the latest manifest version in tree.
    assert_eq!(m.manifest_version, 3, "v1 must migrate to current manifest version in-memory");
    assert_eq!(m.protocols, vec!["openai_function".to_string()]);
    assert!(m.hooks.is_empty());
    assert!(m.skill_refs.is_empty());
    assert_eq!(m.name, "legacy");
}

#[test]
fn v2_manifest_round_trips_all_new_fields() {
    let body = r#"
manifest_version = 2
name = "modern"
version = "0.2.0"
plugin_type = "sync"
protocols = ["openai_function", "block"]
hooks = ["message.received"]
skill_refs = ["search"]

[entry_point]
command = "python"
args = ["main.py"]
"#;
    let (_dir, path) = write_manifest(body);
    let m = parse_manifest_file(&path).expect("v2 manifest must parse");

    // Same migration story as v1: explicit v2 also forward-migrates to
    // v3 once parsed, so all v1+v2 manifests converge on the current
    // schema version regardless of the source declaration.
    assert_eq!(m.manifest_version, 3);
    assert_eq!(m.protocols, vec!["openai_function", "block"]);
    assert_eq!(m.hooks, vec!["message.received"]);
    assert_eq!(m.skill_refs, vec!["search"]);
}

#[test]
fn invalid_protocol_is_rejected_at_validate() {
    let body = r#"
manifest_version = 2
name = "rogue"
version = "0.1.0"
plugin_type = "sync"
protocols = ["invalid_proto"]

[entry_point]
command = "true"
"#;
    let (_dir, path) = write_manifest(body);
    let err = parse_manifest_file(&path).expect_err("unknown protocol must fail validation");
    match err {
        ManifestParseError::Validation { message, .. } => {
            assert!(
                message.contains("unknown protocol"),
                "unexpected validation message: {message}"
            );
        }
        other => {
            panic!("expected ManifestParseError::Validation for unknown protocol, got {other:?}")
        }
    }
}
