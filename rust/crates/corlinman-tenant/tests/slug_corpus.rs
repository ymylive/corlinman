//! Phase 4 W1.5 (next-tasks A5): cross-language tenant slug
//! corpus. The accept / reject decisions here MUST match the
//! TypeScript test in `ui/lib/api/tenants.test.ts` line-for-line.
//! Both files anchor on `docs/contracts/tenant-slug.md` as the
//! single source of truth.
//!
//! When a new accept or reject case is added, update both files
//! in the same commit. Reviewers should reject diffs that touch
//! only one side.

use corlinman_tenant::TenantId;

/// Slugs that MUST validate. Mirrors the "Accept" list in
/// `docs/contracts/tenant-slug.md`.
const ACCEPT: &[&str] = &[
    "default",
    "acme",
    "bravo",
    "acme-corp",
    "acme-2",
    "agency-of-record",
    "a",
    "a-b-c",
    // Exactly 63 characters — the documented upper bound.
    // `a-z (26) + 0-9 (10) + - (1) + a-z (26) = 63`.
    "abcdefghijklmnopqrstuvwxyz0123456789-abcdefghijklmnopqrstuvwxyz",
];

/// Slugs that MUST be rejected. Mirrors the "Reject" list.
const REJECT: &[&str] = &[
    "",                       // empty
    "ACME",                   // uppercase
    "Acme",                   // mixed case
    "0acme",                  // leading digit
    "-acme",                  // leading hyphen
    "acme_corp",              // underscore
    "acme.corp",              // dot
    "acme/corp",              // slash
    "acme corp",              // internal space
    " acme",                  // leading whitespace
    "acme!",                  // punctuation
    // 64 characters — over the bound by one.
    "abcdefghijklmnopqrstuvwxyz0123456789-abcdefghijklmnopqrstuvwxyzz",
];

#[test]
fn accept_corpus_round_trips() {
    for slug in ACCEPT {
        let parsed = TenantId::new(*slug)
            .unwrap_or_else(|e| panic!("expected accept for {slug:?}, got {e}"));
        assert_eq!(
            parsed.as_str(),
            *slug,
            "TenantId must round-trip the input bytes"
        );
    }
}

#[test]
fn reject_corpus_is_rejected() {
    for slug in REJECT {
        let result = TenantId::new(*slug);
        assert!(
            result.is_err(),
            "expected reject for {slug:?}, got {:?}",
            result.as_ref().map(|t| t.as_str())
        );
    }
}

#[test]
fn upper_bound_is_63_chars_inclusive() {
    let max = "a".repeat(63);
    assert!(TenantId::new(&max).is_ok(), "63-char slug must accept");
    let over = "a".repeat(64);
    assert!(TenantId::new(&over).is_err(), "64-char slug must reject");
}

/// Pin the public regex string against the documented pattern. A
/// drift here would mean the spec doc and the actual regex
/// disagreed; the test is cheap insurance against a sloppy edit.
#[test]
fn public_regex_string_matches_spec() {
    use corlinman_tenant::TENANT_SLUG_REGEX_STR;
    assert_eq!(TENANT_SLUG_REGEX_STR, r"\A[a-z][a-z0-9-]{0,62}\z");
}
