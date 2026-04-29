//! In-process [`SandboxBackend`] implementation.
//!
//! Runs the workload directly in the gateway's process — no
//! container, no isolation. This backend exists for two reasons:
//!
//! 1. Development environments and CI runners without a docker
//!    daemon need a callable backend so the rest of the runner code
//!    can compile and exercise its happy path. The integration test
//!    that exercises `DockerBackend` skips gracefully when docker is
//!    unavailable; without an in-process fallback the gateway would
//!    hard-fail at boot if `[evolution.shadow].sandbox_kind` was
//!    misconfigured.
//!
//! 2. The deterministic self-test workload (SHA-256 of a payload)
//!    has no isolation requirements — running it in-process is
//!    legitimately equivalent to running it in a container. The
//!    integration test uses both backends and asserts they produce
//!    the same hash for the same payload, pinning the cross-process
//!    JSON contract.

use async_trait::async_trait;

use super::{sha256_hex, SandboxBackend, SandboxError, SelfTestResult};

/// Zero-sized [`SandboxBackend`] that runs work directly in the
/// caller's process. See module docs for when to use it.
#[derive(Debug, Default, Clone)]
pub struct InProcessBackend;

#[async_trait]
impl SandboxBackend for InProcessBackend {
    async fn run_self_test(&self, payload: &str) -> Result<SelfTestResult, SandboxError> {
        Ok(SelfTestResult {
            hash: sha256_hex(payload.as_bytes()),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn run_self_test_returns_payload_sha256() {
        let backend = InProcessBackend;
        let result = backend.run_self_test("abc").await.unwrap();
        assert_eq!(
            result.hash,
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    #[tokio::test]
    async fn run_self_test_is_deterministic_across_calls() {
        let backend = InProcessBackend;
        let a = backend.run_self_test("hello world").await.unwrap();
        let b = backend.run_self_test("hello world").await.unwrap();
        assert_eq!(a, b);
    }
}
