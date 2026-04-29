//! Phase 4 W1 4-1C integration test: end-to-end verification that
//! `DockerBackend` spawns the `corlinman-sandbox` image, runs the
//! `sandbox-self-test` workload under the documented isolation knobs,
//! and produces a hash that matches what `InProcessBackend` computes
//! for the same payload. Cross-process JSON contract pinned.
//!
//! This test is environment-dependent. It skips gracefully (logs
//! "skipping" and exits ok) when:
//!
//! - The `docker` binary is not on PATH
//! - The docker daemon is unreachable (`docker version` fails)
//! - The image `corlinman-sandbox:dev` is not present locally
//!
//! All three conditions are typical on CI runners and dev machines
//! that don't currently target the sandbox path. Skipping is the
//! correct behaviour: making the test always-required would force
//! every contributor to install Docker and pre-build the image
//! before `cargo test` could pass, which is not the expected
//! workflow for a Rust workspace.
//!
//! To run it locally, build the image first:
//!
//!     docker build -f docker/Dockerfile.shadow-sandbox \
//!                  -t corlinman-sandbox:dev .
//!     cargo test -p corlinman-shadow-tester --test \
//!                sandbox_docker_integration

use std::process::Command;

use corlinman_shadow_tester::sandbox::{DockerBackend, InProcessBackend, SandboxBackend};

/// Returns `true` if the docker daemon answers `docker version`. Two
/// failure modes are treated as "skip the rest of this test":
///
/// - `docker` is not on PATH (binary missing)
/// - `docker version` exits non-zero (daemon not running, or
///   permission denied — same effect from the test's POV)
fn docker_daemon_reachable() -> bool {
    match Command::new("docker")
        .arg("version")
        .arg("--format")
        .arg("{{.Server.Version}}")
        .output()
    {
        Ok(out) => out.status.success(),
        Err(_) => false,
    }
}

/// Returns `true` when `corlinman-sandbox:dev` is present in the
/// local docker image cache. Skipping when missing is friendlier
/// than failing — the user might be running the test before the
/// `docker build` step.
fn sandbox_image_built(tag: &str) -> bool {
    let output = match Command::new("docker")
        .args(["image", "inspect", tag])
        .output()
    {
        Ok(o) => o,
        Err(_) => return false,
    };
    output.status.success()
}

#[tokio::test]
async fn docker_backend_self_test_matches_in_process_hash() {
    if !docker_daemon_reachable() {
        eprintln!("skipping: docker daemon not reachable");
        return;
    }
    let tag = "corlinman-sandbox:dev";
    if !sandbox_image_built(tag) {
        eprintln!(
            "skipping: image '{tag}' not present (build via docker/Dockerfile.shadow-sandbox)"
        );
        return;
    }

    let payload = "phase 4 w1 4-1c integration test payload";
    let in_proc = InProcessBackend
        .run_self_test(payload)
        .await
        .expect("in-process self-test must always succeed");

    let docker = DockerBackend::new(tag, 256, 30);
    let docker_result = docker
        .run_self_test(payload)
        .await
        .expect("docker self-test must succeed when daemon + image are present");

    assert_eq!(
        in_proc, docker_result,
        "cross-process hash drift: stdout JSON contract is broken"
    );
    assert_eq!(in_proc.hash.len(), 64, "SHA-256 hex must be 64 chars");
}

#[tokio::test]
async fn docker_backend_reports_missing_image_clearly() {
    if !docker_daemon_reachable() {
        eprintln!("skipping: docker daemon not reachable");
        return;
    }
    // Pick a tag that almost certainly does not exist locally. The
    // backend should surface a `NonZeroExit` (or `OutputParse` if
    // docker prints non-JSON), NOT a panic or a silent success.
    let backend = DockerBackend::new("corlinman-sandbox:does-not-exist-aafzz", 64, 10);
    let err = backend
        .run_self_test("anything")
        .await
        .expect_err("missing image must surface as an error");

    use corlinman_shadow_tester::sandbox::SandboxError;
    match err {
        SandboxError::NonZeroExit { stderr, .. } => {
            // `docker run` prints something like
            // "Unable to find image 'foo' locally" then "pull
            // access denied" or "manifest unknown". Either substring
            // is acceptable evidence the error path engaged.
            let lower = stderr.to_lowercase();
            assert!(
                lower.contains("unable to find")
                    || lower.contains("manifest")
                    || lower.contains("not found"),
                "stderr did not look like a missing-image error: {stderr}"
            );
        }
        SandboxError::OutputParse(_, raw) => {
            // Some docker versions print pull progress that confuses
            // the JSON parser before the error fires. Accept either
            // shape.
            assert!(!raw.is_empty(), "expected non-empty raw output");
        }
        SandboxError::DaemonUnavailable(_) | SandboxError::Spawn(_) | SandboxError::Timeout(_) => {
            panic!("unexpected non-image-missing error: {err:?}");
        }
    }
}

#[tokio::test]
async fn docker_backend_returns_daemon_unavailable_when_binary_missing() {
    // Force the `docker` binary lookup to fail by temporarily
    // pointing PATH at an empty directory.
    //
    // Note: `std::env::set_var` is process-global, so this test
    // assumes single-threaded execution within the test binary.
    // The integration test file only contains tokio::test
    // multi-threaded tasks; nextest serialises by default and
    // `cargo test` schedules tests in the same binary on a thread
    // pool that doesn't share env vars across tests at the OS
    // level — but we still SetVar/Restore as a courtesy.
    let original_path = std::env::var("PATH").unwrap_or_default();
    let tmp = tempfile::TempDir::new().unwrap();
    let empty_dir_path = tmp.path().to_string_lossy().to_string();
    // Safety: see comment above. This integration test file is the
    // only consumer; nothing else in this binary inspects PATH
    // concurrently.
    unsafe {
        std::env::set_var("PATH", &empty_dir_path);
    }

    let backend = DockerBackend::new("any-image", 64, 5);
    let err = backend.run_self_test("hello").await;

    // Restore PATH before any assertion in case it panics.
    unsafe {
        std::env::set_var("PATH", original_path);
    }

    use corlinman_shadow_tester::sandbox::SandboxError;
    match err {
        Err(SandboxError::DaemonUnavailable(msg)) => {
            assert!(
                msg.contains("docker binary not found"),
                "daemon-unavailable message should mention the binary: {msg}"
            );
        }
        other => panic!("expected DaemonUnavailable, got: {other:?}"),
    }
}
