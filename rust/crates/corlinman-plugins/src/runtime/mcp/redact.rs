//! Env-var passthrough filter + log redaction for the MCP runtime.
//!
//! Three rules, in order (Phase 4 W3 C2 design §"Auth / secrets"):
//!
//! 1. **Allowlist-only**: env vars reach the child only when their
//!    exact name is in `EnvPassthrough.allow`. Empty allow list =
//!    no env vars (the four `REQUIRED_ENV_KEYS` from
//!    `runtime::mcp_stdio` are layered in by the spawn primitive,
//!    not by this filter).
//! 2. **Glob deny over the allow set**: `EnvPassthrough.deny` is a
//!    `globset`-flavoured deny filter. Even if a future operator
//!    writes `allow = ["*"]`, `deny = ["AWS_*"]` keeps the AWS keys
//!    out. We deliberately *don't* let `allow` be a glob — the
//!    "what reaches the child" surface stays exact-match so it's
//!    auditable; only the deny side accepts patterns.
//! 3. **Log redaction**: a value of any allowlisted env var whose
//!    name matches `*_TOKEN | *_KEY | *_SECRET | *_PASSWORD`
//!    (case-insensitive) is masked to
//!    `[REDACTED:<sha256[..8]>]` in tracing field captures. The
//!    redaction salt is per-process so leaked log lines can't
//!    decode back to the secret.
//!
//! This module is dumb plumbing on purpose — no I/O, no async, no
//! globals. The adapter (iter 4) calls `apply_env_passthrough`
//! once at spawn time and threads `redact_field` into its tracing
//! macros wherever an env value would otherwise be printed.

use std::collections::BTreeSet;
use std::sync::OnceLock;

use globset::{Glob, GlobSet, GlobSetBuilder};
use sha2::{Digest, Sha256};

use crate::manifest::EnvPassthrough;

/// Sensitive-name suffixes — case-insensitive. A name whose uppercased
/// form ends with any of these triggers value redaction.
///
/// We match suffix rather than substring to avoid masking unrelated
/// names that merely contain the word ("LOG_KEY_PATH" still gets
/// redacted because it ends in `_PATH`? no — it doesn't match. It
/// matches because of `_KEY`. Yes, that's intentional: a
/// `LOG_KEY_PATH` is also a "key" for our purposes.). The list is
/// intentionally short — operators should add specific names to the
/// deny list if they want broader matching.
pub const SENSITIVE_SUFFIXES: &[&str] = &["_TOKEN", "_KEY", "_SECRET", "_PASSWORD"];

/// Per-process random salt used to compute redaction hashes.
/// Initialised on first read; never serialised. The salt is mixed
/// into `Sha256` before the value, so a leaked redacted log can't be
/// brute-forced without also leaking process memory.
fn redact_salt() -> &'static [u8; 32] {
    static SALT: OnceLock<[u8; 32]> = OnceLock::new();
    SALT.get_or_init(|| {
        // tokio is already on the dep tree; use its time::Instant +
        // process pid as a cheap entropy source. We're not signing
        // anything here — just want non-deterministic output across
        // process restarts so log replays don't equate to value
        // disclosure.
        let mut h = Sha256::new();
        h.update(std::process::id().to_le_bytes());
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        h.update(nanos.to_le_bytes());
        // Stir in the addr of the salt itself for a tiny bit of ASLR
        // entropy on platforms that randomise.
        let addr = (&SALT as *const _) as usize;
        h.update(addr.to_le_bytes());
        let digest = h.finalize();
        let mut out = [0u8; 32];
        out.copy_from_slice(&digest[..32]);
        out
    })
}

/// Returns true iff `name` (compared uppercased) ends with any of
/// [`SENSITIVE_SUFFIXES`].
pub fn is_sensitive_name(name: &str) -> bool {
    let upper = name.to_ascii_uppercase();
    SENSITIVE_SUFFIXES
        .iter()
        .any(|suffix| upper.ends_with(suffix))
}

/// Compute a per-process-stable redaction marker for `value`.
/// The marker is the first 8 hex chars of `Sha256(salt || value)`.
/// Two log lines for the same value within one process produce the
/// same marker — useful for correlating "this token appeared in
/// these N places" without leaking the value.
pub fn redact_marker(value: &str) -> String {
    let salt = redact_salt();
    let mut h = Sha256::new();
    h.update(salt);
    h.update(value.as_bytes());
    let digest = h.finalize();
    // 4 bytes => 8 hex chars; plenty of disambiguation, low log noise.
    let mut s = String::with_capacity(8);
    for b in &digest[..4] {
        s.push_str(&format!("{:02x}", b));
    }
    s
}

/// Format `(name, value)` for a tracing field. Returns the raw
/// `value` for non-sensitive names, and `[REDACTED:<8 hex>]` for
/// sensitive ones.
///
/// The placeholder is intentionally distinct from the empty string
/// so a missing-but-required env var (which produces "") is
/// distinguishable from a redacted one.
pub fn redact_field(name: &str, value: &str) -> String {
    if is_sensitive_name(name) {
        format!("[REDACTED:{}]", redact_marker(value))
    } else {
        value.to_string()
    }
}

/// Outcome of applying an [`EnvPassthrough`] policy to the parent's
/// environment.
#[derive(Debug, Clone)]
pub struct AppliedEnv {
    /// `(key, value)` pairs that survived the allow/deny filter and
    /// should be forwarded to the child. Order matches the
    /// allowlist; absent parent vars are silently dropped (the
    /// design doesn't require us to fail-loud when an allowlisted
    /// var has no value — `npx` servers commonly degrade gracefully).
    pub forwarded: Vec<(String, String)>,

    /// Names that were in `allow` but blocked by the `deny` filter.
    /// Surfaced for log diagnostics; not consumed by the spawner.
    pub denied: BTreeSet<String>,

    /// Names that were in `allow` but absent from the parent env.
    /// Surfaced for log diagnostics.
    pub missing: BTreeSet<String>,
}

/// Compile the `deny` patterns into a [`GlobSet`].
///
/// We expose this separately so the adapter can validate at manifest
/// load time whether the patterns are well-formed (a bad glob is
/// almost certainly a typo and worth a manifest validation error
/// rather than a silent runtime no-op).
pub fn compile_deny_set(patterns: &[String]) -> Result<GlobSet, RedactError> {
    let mut b = GlobSetBuilder::new();
    for p in patterns {
        let glob = Glob::new(p).map_err(|e| RedactError::BadDenyPattern {
            pattern: p.clone(),
            source: e,
        })?;
        b.add(glob);
    }
    b.build().map_err(|e| RedactError::DenySetCompile { source: e })
}

/// Errors surfaced by [`compile_deny_set`].
#[derive(Debug, thiserror::Error)]
pub enum RedactError {
    #[error("invalid env_passthrough.deny glob {pattern:?}: {source}")]
    BadDenyPattern {
        pattern: String,
        #[source]
        source: globset::Error,
    },
    #[error("failed to assemble deny globset: {source}")]
    DenySetCompile {
        #[source]
        source: globset::Error,
    },
}

/// Apply an [`EnvPassthrough`] policy to a snapshot of the parent
/// environment.
///
/// Inputs:
///   - `policy`: parsed `[mcp.env_passthrough]` from the manifest.
///   - `lookup`: `(name) -> Option<value>` — usually wraps
///     `std::env::var`, but the test suite injects a deterministic
///     map so the host env doesn't pollute results.
///
/// Behaviour:
///   1. For each `name` in `policy.allow` (in order):
///      - if the deny set matches => record in `denied`, skip.
///      - else if `lookup(name) == None` => record in `missing`, skip.
///      - else push `(name, value)` to `forwarded`.
///   2. Returns the partition; never panics, never logs.
pub fn apply_env_passthrough<F>(policy: &EnvPassthrough, lookup: F) -> Result<AppliedEnv, RedactError>
where
    F: Fn(&str) -> Option<String>,
{
    let deny = compile_deny_set(&policy.deny)?;
    let mut forwarded = Vec::with_capacity(policy.allow.len());
    let mut denied = BTreeSet::new();
    let mut missing = BTreeSet::new();

    for name in &policy.allow {
        if deny.is_match(name) {
            denied.insert(name.clone());
            continue;
        }
        match lookup(name) {
            Some(v) => forwarded.push((name.clone(), v)),
            None => {
                missing.insert(name.clone());
            }
        }
    }

    Ok(AppliedEnv {
        forwarded,
        denied,
        missing,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn lookup_from<'a>(map: &'a [(&'a str, &'a str)]) -> impl Fn(&str) -> Option<String> + 'a {
        move |k: &str| {
            map.iter()
                .find(|(name, _)| *name == k)
                .map(|(_, v)| (*v).to_string())
        }
    }

    /// `env_passthrough_strips_unallowlisted` — names absent from
    /// `allow` never make it to the forwarded set, even when they
    /// exist in the parent env.
    #[test]
    fn env_passthrough_strips_unallowlisted() {
        let policy = EnvPassthrough {
            allow: vec!["GITHUB_TOKEN".into()],
            deny: vec![],
        };
        let env = vec![
            ("GITHUB_TOKEN", "ghp_secret"),
            ("AWS_ACCESS_KEY_ID", "AKIAdeadbeef"),
            ("UNRELATED", "x"),
        ];
        let applied = apply_env_passthrough(&policy, lookup_from(&env)).unwrap();

        let names: Vec<&str> = applied.forwarded.iter().map(|(k, _)| k.as_str()).collect();
        assert_eq!(names, vec!["GITHUB_TOKEN"]);
        let aws_present = applied
            .forwarded
            .iter()
            .any(|(k, _)| k.starts_with("AWS_"));
        assert!(!aws_present, "AWS_* must not leak to child env");
    }

    /// Glob deny over an `allow = ["*"]` style policy must keep
    /// `AWS_*` out — the design's "defence in depth" rule.
    #[test]
    fn env_passthrough_glob_deny_blocks_explicit_allow() {
        let policy = EnvPassthrough {
            allow: vec!["GITHUB_TOKEN".into(), "AWS_ACCESS_KEY_ID".into()],
            deny: vec!["AWS_*".into()],
        };
        let env = vec![
            ("GITHUB_TOKEN", "ghp_secret"),
            ("AWS_ACCESS_KEY_ID", "AKIA"),
        ];
        let applied = apply_env_passthrough(&policy, lookup_from(&env)).unwrap();

        assert!(applied.denied.contains("AWS_ACCESS_KEY_ID"));
        let names: Vec<&str> = applied.forwarded.iter().map(|(k, _)| k.as_str()).collect();
        assert_eq!(names, vec!["GITHUB_TOKEN"]);
    }

    /// Allowlisted-but-missing parent vars produce a `missing` entry,
    /// not a hard error.
    #[test]
    fn env_passthrough_missing_recorded() {
        let policy = EnvPassthrough {
            allow: vec!["NEVER_SET_C2_ITER3".into()],
            deny: vec![],
        };
        let applied =
            apply_env_passthrough(&policy, lookup_from(&[("PATH", "/usr/bin")])).unwrap();
        assert!(applied.forwarded.is_empty());
        assert!(applied.missing.contains("NEVER_SET_C2_ITER3"));
    }

    /// A bad glob fails compilation eagerly. `globset` accepts most
    /// strings as globs (`***` is valid), so we use a known-invalid
    /// pattern: an unbalanced bracket.
    #[test]
    fn bad_deny_glob_errors() {
        let policy = EnvPassthrough {
            allow: vec!["X".into()],
            deny: vec!["[unbalanced".into()],
        };
        let err = apply_env_passthrough(&policy, |_| None).unwrap_err();
        assert!(
            matches!(err, RedactError::BadDenyPattern { .. }),
            "expected BadDenyPattern, got {err:?}"
        );
    }

    /// `is_sensitive_name` matches case-insensitively across every
    /// suffix. Negative case: `LOG_PATH` ends in `_PATH` which is
    /// NOT in `SENSITIVE_SUFFIXES`, so it must NOT redact.
    #[test]
    fn sensitive_name_classification() {
        for sensitive in [
            "GITHUB_TOKEN",
            "github_token",
            "OPENAI_API_KEY",
            "openai_api_key",
            "FOO_SECRET",
            "BAR_PASSWORD",
        ] {
            assert!(
                is_sensitive_name(sensitive),
                "{sensitive} should be sensitive"
            );
        }
        for plain in ["LOG_PATH", "USER", "HOME", "TOKEN_LENGTH"] {
            assert!(!is_sensitive_name(plain), "{plain} should NOT be sensitive");
        }
    }

    /// `env_passthrough_redacts_in_logs` — sensitive-named values
    /// never appear verbatim in `redact_field`'s output, and the
    /// placeholder follows the documented `[REDACTED:<8 hex>]`
    /// shape. Stable within a process: redacting the same value
    /// twice yields the same marker.
    #[test]
    fn env_passthrough_redacts_in_logs() {
        let secret = "ghp_topsecret_value_redacted_by_iter3";
        let line = redact_field("GITHUB_TOKEN", secret);
        assert!(
            !line.contains(secret),
            "secret leaked verbatim into {line:?}"
        );
        assert!(
            line.starts_with("[REDACTED:") && line.ends_with(']'),
            "unexpected placeholder shape: {line:?}"
        );
        // Marker is exactly 8 hex chars.
        let inner = line
            .strip_prefix("[REDACTED:")
            .and_then(|s| s.strip_suffix(']'))
            .unwrap();
        assert_eq!(inner.len(), 8, "marker length: {inner:?}");
        assert!(
            inner.chars().all(|c| c.is_ascii_hexdigit()),
            "marker not hex: {inner:?}"
        );

        // Stable within a process.
        assert_eq!(redact_field("GITHUB_TOKEN", secret), line);

        // Non-sensitive names pass the value through verbatim.
        assert_eq!(redact_field("PATH", "/usr/bin"), "/usr/bin");
    }

    /// `redact_marker` differs across distinct values (with
    /// astronomically high probability) but is stable per-process
    /// for the same value.
    #[test]
    fn redact_marker_distinguishes_values() {
        let a = redact_marker("alpha");
        let b = redact_marker("beta");
        let a2 = redact_marker("alpha");
        assert_ne!(a, b, "different values must yield different markers");
        assert_eq!(a, a2, "same value must yield identical marker");
    }
}
