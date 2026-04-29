//! Phase 4 W1 4-1C: execution sandbox for high-risk EvolutionKinds.
//!
//! The Phase 3 in-process simulators (`MemoryOpSimulator`,
//! `TagRebalanceSimulator`, `SkillUpdateSimulator`) live in
//! [`crate::simulator`] and stay there: each is already TOCTOU-
//! hardened by Phase 3.1 / S-5 and the kind contracts they evaluate
//! never reach across process boundaries.
//!
//! Phase 4 introduces three new kinds — `prompt_template`,
//! `tool_policy`, `new_skill` — whose evals call out to a live LLM
//! or run unverified scripts. Those need stronger isolation than an
//! in-process simulator gives. This module is the abstraction that
//! lets the runner route work to either:
//!
//! - [`InProcessBackend`] — runs the workload directly in the
//!   gateway's process. Suitable for deterministic eval workloads
//!   that don't touch outside resources. Network / cgroup / cap
//!   restrictions are not enforced; this backend is an honest
//!   fallback for development environments without a docker daemon.
//!
//! - [`DockerBackend`] — spawns a frozen `corlinman-sandbox`
//!   container with `--network=none`, `--read-only`,
//!   `--cap-drop=ALL`, `--security-opt=no-new-privileges`,
//!   `--memory=<config>m`, `--pids-limit=64`, a wall-clock timeout,
//!   and `--user=65532:65532`. Stdout is captured and parsed as JSON.
//!   The docker CLI is invoked via `tokio::process::Command` to keep
//!   the dependency surface tight; richer error handling via bollard
//!   is a follow-up.
//!
//! v1 surface is deliberately small: a single `run_self_test`
//! method that accepts a payload and returns its SHA-256. The
//! integration test exercises it end-to-end. Future Phase 4 work
//! will add per-kind methods (`run_prompt_template_eval`,
//! `run_tool_policy_eval`, `run_new_skill_eval`) that share the
//! same backend trait.

mod docker;
mod in_process;

pub use docker::DockerBackend;
pub use in_process::InProcessBackend;

use async_trait::async_trait;

/// Execution backend for sandboxed workloads.
///
/// Cloneable: every implementation is either zero-sized (in-process)
/// or holds plain `String` config (docker). The runner stamps a
/// concrete backend onto its state at boot and clones it into each
/// per-eval future.
#[async_trait]
pub trait SandboxBackend: Send + Sync {
    /// Self-test workload: hash the supplied payload via SHA-256
    /// inside the sandbox and return the result. The integration
    /// test uses this to verify image build, network isolation,
    /// timeout, and stdout capture without needing a real eval set
    /// to land first.
    async fn run_self_test(&self, payload: &str) -> Result<SelfTestResult, SandboxError>;
}

/// JSON shape returned by the `corlinman-shadow-tester sandbox-self-test`
/// subcommand. The `DockerBackend` parses container stdout into this;
/// the `InProcessBackend` constructs it directly.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct SelfTestResult {
    /// Lowercase hex SHA-256 of the payload bytes.
    pub hash: String,
}

#[derive(Debug, thiserror::Error)]
pub enum SandboxError {
    #[error("docker spawn: {0}")]
    Spawn(std::io::Error),
    #[error("docker timeout after {0:?}")]
    Timeout(std::time::Duration),
    #[error("docker exited non-zero (status={status:?}, stderr={stderr})")]
    NonZeroExit { status: Option<i32>, stderr: String },
    #[error("docker stdout was not valid JSON ({0}); raw: {1}")]
    OutputParse(serde_json::Error, String),
    #[error("docker daemon unreachable: {0}")]
    DaemonUnavailable(String),
}

/// Compute the SHA-256 of a UTF-8 payload, formatted as lowercase
/// hex. Shared between the in-process backend and the
/// `sandbox-self-test` subcommand the docker backend invokes inside
/// the container; cross-process consistency is the whole point of
/// the integration test.
pub fn sha256_hex(payload: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    let digest = Sha256::digest(payload);
    let mut out = String::with_capacity(digest.len() * 2);
    for byte in digest {
        use std::fmt::Write;
        let _ = write!(out, "{byte:02x}");
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sha256_hex_matches_known_vector() {
        // Standard NIST test vector for SHA-256 of "abc".
        assert_eq!(
            sha256_hex(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    #[test]
    fn sha256_hex_handles_empty_input() {
        assert_eq!(
            sha256_hex(b""),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
    }
}
