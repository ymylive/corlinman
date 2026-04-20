//! Integration test: the shipped `docs/config.example.toml` must stay in sync
//! with the `Config` schema (decode + validate).

use std::path::PathBuf;

use corlinman_core::config::Config;

fn example_path() -> PathBuf {
    // tests/ is two directories below the repo root:
    // repo/rust/crates/corlinman-core/tests/<this file>.
    let manifest_dir = env!("CARGO_MANIFEST_DIR");
    PathBuf::from(manifest_dir)
        .join("../../..")
        .join("docs")
        .join("config.example.toml")
}

#[test]
fn docs_example_parses_cleanly() {
    let p = example_path();
    let cfg = Config::load_from_path(&p)
        .unwrap_or_else(|e| panic!("failed to parse {}: {e}", p.display()));
    // Example has anthropic enabled with an env-ref key → passes the
    // "no_provider_enabled" cross-field rule.
    let issues = cfg.validate_report();
    assert!(
        issues.iter().all(|i| i.code != "no_provider_enabled"),
        "example should keep at least one provider enabled; issues: {issues:?}"
    );
}
