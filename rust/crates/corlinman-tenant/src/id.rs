//! `TenantId` — slug-shaped newtype.
//!
//! Phase 3.1 (Tier 3 / S-2) seeded `user_traits.tenant_id` and
//! `agent_persona_state.tenant_id` as `TEXT NOT NULL DEFAULT 'default'`,
//! so the de-facto wire shape is "slug or the literal 'default'". The
//! Rust newtype enforces that shape at the language boundary so a
//! mistyped string in admin-claim parsing or a `?tenant=` query param
//! can't smuggle SQL fragments / path traversal segments / non-ASCII
//! into the per-tenant directory layout.
//!
//! Shape: `^[a-z][a-z0-9-]{0,62}$`
//!   * starts with a lowercase letter (no leading digit / dash so
//!     filenames sort intuitively and don't collide with reserved
//!     conventions)
//!   * lowercase alphanumeric + ASCII hyphen only — no underscore (looks
//!     like a typo of dash in URLs), no uppercase (case-folding bugs on
//!     filesystems vary by host)
//!   * 1–63 chars total — same upper bound as DNS labels, so a tenant id
//!     drops cleanly into a hostname / cookie segment / S3 prefix
//!     without a separate length cap
//!
//! `default` is the reserved value for legacy single-tenant boots. It
//! still passes the slug regex (it's lowercase letters), but
//! `TenantId::default()` returns it explicitly so call-sites that "just
//! want the legacy tenant" don't have to spell it.

use std::fmt;
use std::str::FromStr;

use once_cell::sync::Lazy;
use regex::Regex;
use schemars::JsonSchema;
use serde::{Deserialize, Deserializer, Serialize, Serializer};

/// Reserved tenant id for legacy / single-tenant deployments.
///
/// Every Phase 3.1 SQLite row was stamped with this value via the
/// column default; the constant is exported here so the rest of the
/// codebase doesn't have to spell the literal string at call sites
/// (typos in a literal slip through compilation).
pub const DEFAULT_TENANT_ID: &str = "default";

/// Compiled once at first use. The regex is static so we don't pay the
/// compile cost on every `TenantId::new` (validation runs in admin auth
/// hot paths).
static TENANT_ID_RE: Lazy<Regex> = Lazy::new(|| {
    // Anchored with `\A` / `\z` rather than `^` / `$` so a multiline
    // string can't sneak through with a slug on its first line. `regex`
    // honours both, but `\A` / `\z` are unambiguous about not matching
    // `\n`.
    Regex::new(r"\A[a-z][a-z0-9-]{0,62}\z").expect("tenant id regex is statically known to compile")
});

/// Validation failure for a candidate tenant id. The variants keep
/// enough context that the operator can fix the input — empty is its
/// own variant because that's the most common operator typo
/// (forgotten `?tenant=` value).
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum TenantIdError {
    /// Input was the empty string.
    #[error("tenant id must not be empty")]
    Empty,
    /// Input is non-empty but didn't match the slug regex. The
    /// offending value is included so logs let the operator copy the
    /// string back into a config.
    #[error(
        "tenant id {0:?} must match ^[a-z][a-z0-9-]{{0,62}}$ \
         (lowercase ASCII alphanumeric + hyphen, 1–63 chars, must \
         start with a letter)"
    )]
    InvalidShape(String),
}

/// Tenant identifier. Cheap to clone (single `String` allocation).
///
/// Constructed only via [`TenantId::new`] / [`FromStr`] / serde
/// deserialisation — every code path runs the same slug check.
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct TenantId(String);

impl TenantId {
    /// Validate `value` and wrap it in a `TenantId`.
    pub fn new<S: Into<String>>(value: S) -> Result<Self, TenantIdError> {
        let s = value.into();
        if s.is_empty() {
            return Err(TenantIdError::Empty);
        }
        if !TENANT_ID_RE.is_match(&s) {
            return Err(TenantIdError::InvalidShape(s));
        }
        Ok(Self(s))
    }

    /// The reserved legacy tenant id.
    ///
    /// This is the value Phase 3.1's `'default'` column stamp used and
    /// the value `Default::default()` returns. Wrapping it in a const
    /// keeps the literal in one place.
    pub fn legacy_default() -> Self {
        // We know `DEFAULT_TENANT_ID` matches the regex at compile time;
        // `expect` here would be misleading because we never get there.
        // A short helper avoids a runtime regex check in the hot
        // `Default::default()` path.
        Self(DEFAULT_TENANT_ID.to_owned())
    }

    /// Borrow the underlying slug string.
    pub fn as_str(&self) -> &str {
        &self.0
    }

    /// Take the inner `String`. Used by serde / ETL paths that want to
    /// hand the raw value to a SQLite bind without an extra allocation.
    pub fn into_inner(self) -> String {
        self.0
    }

    /// True iff this is the reserved legacy value. Callers that want to
    /// keep "single-tenant compat" branches readable should prefer this
    /// over `==` on the underlying string.
    pub fn is_legacy_default(&self) -> bool {
        self.0 == DEFAULT_TENANT_ID
    }
}

impl Default for TenantId {
    fn default() -> Self {
        Self::legacy_default()
    }
}

impl fmt::Display for TenantId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl FromStr for TenantId {
    type Err = TenantIdError;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        Self::new(s.to_owned())
    }
}

impl AsRef<str> for TenantId {
    fn as_ref(&self) -> &str {
        &self.0
    }
}

// Borrow lets `HashMap<TenantId, _>::get` accept `&str` without
// allocating a wrapper, which matters when looking up a tenant from an
// inbound HTTP query string.
impl std::borrow::Borrow<str> for TenantId {
    fn borrow(&self) -> &str {
        &self.0
    }
}

// ---------------------------------------------------------------------------
// serde
// ---------------------------------------------------------------------------

impl Serialize for TenantId {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&self.0)
    }
}

impl<'de> Deserialize<'de> for TenantId {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        // Deserialize as `String` then re-validate. The error path uses
        // serde's `custom` so the operator gets the same shape-error
        // text in TOML / JSON payloads.
        let raw = String::deserialize(deserializer)?;
        TenantId::new(raw).map_err(serde::de::Error::custom)
    }
}

// `JsonSchema` lets us embed `TenantId` in `Config` (the gateway's live
// config snapshot is `JsonSchema`-derived for `corlinman config show`).
// The schema declares the shape pattern so an admin UI driving a TOML
// editor can validate inline without round-tripping the loader.
impl JsonSchema for TenantId {
    fn schema_name() -> String {
        "TenantId".to_owned()
    }

    fn json_schema(_gen: &mut schemars::gen::SchemaGenerator) -> schemars::schema::Schema {
        use schemars::schema::{InstanceType, Schema, SchemaObject, StringValidation};
        Schema::Object(SchemaObject {
            instance_type: Some(InstanceType::String.into()),
            string: Some(Box::new(StringValidation {
                min_length: Some(1),
                max_length: Some(63),
                pattern: Some("^[a-z][a-z0-9-]{0,62}$".to_owned()),
            })),
            ..Default::default()
        })
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn legacy_default_is_valid() {
        let t = TenantId::legacy_default();
        assert_eq!(t.as_str(), "default");
        assert!(t.is_legacy_default());
        // Default impl agrees.
        assert_eq!(TenantId::default(), TenantId::legacy_default());
    }

    #[test]
    fn accepts_valid_slugs() {
        for ok in [
            "default",
            "acme",
            "acme-corp",
            "tenant1",
            "a",
            "ymylive-prod",
            // 63 chars: max allowed.
            "a234567890123456789012345678901234567890123456789012345678901bc",
        ] {
            let t: TenantId = ok.parse().unwrap_or_else(|e| panic!("{ok:?}: {e}"));
            assert_eq!(t.as_str(), ok);
        }
    }

    #[test]
    fn rejects_invalid_slugs() {
        // Empty.
        assert_eq!(TenantId::from_str(""), Err(TenantIdError::Empty));
        // Bad shapes — every variant must surface as InvalidShape.
        for bad in [
            "1leading-digit",
            "-leading-dash",
            "Acme",         // uppercase
            "acme_corp",    // underscore
            "acme corp",    // space
            "acme.corp",    // dot
            "acme/corp",    // slash (path traversal vector)
            "acme\\corp",   // backslash
            "acme\ncorp",   // newline (multi-line bypass guard)
            "acme\u{00e9}", // non-ASCII
            // 64 chars: one over the cap.
            "a234567890123456789012345678901234567890123456789012345678901bcd",
        ] {
            match TenantId::from_str(bad) {
                Err(TenantIdError::InvalidShape(s)) => assert_eq!(s, bad),
                other => panic!("expected InvalidShape for {bad:?}, got {other:?}"),
            }
        }
    }

    #[test]
    fn empty_is_distinct_error_from_invalid() {
        // Operator typing `?tenant=` with an empty value is the most
        // common mistake — we keep it as its own variant so the
        // gateway middleware can return a more helpful 400 message.
        assert_eq!(TenantId::from_str(""), Err(TenantIdError::Empty));
        assert!(matches!(
            TenantId::from_str(" "),
            Err(TenantIdError::InvalidShape(_))
        ));
    }

    #[test]
    fn display_round_trips_through_from_str() {
        let t = TenantId::new("acme-corp").unwrap();
        let s = t.to_string();
        assert_eq!(s, "acme-corp");
        assert_eq!(TenantId::from_str(&s).unwrap(), t);
    }

    #[test]
    fn serde_round_trips() {
        let t = TenantId::new("acme-corp").unwrap();
        let json = serde_json::to_string(&t).unwrap();
        assert_eq!(json, "\"acme-corp\"");
        let back: TenantId = serde_json::from_str(&json).unwrap();
        assert_eq!(back, t);
    }

    #[test]
    fn serde_rejects_invalid_shape_at_deserialize() {
        // Integration-level guard: a malicious config file must not
        // smuggle a path-traversal segment through the deserializer.
        let err = serde_json::from_str::<TenantId>("\"../etc\"").unwrap_err();
        let msg = err.to_string();
        assert!(
            msg.contains("must match"),
            "unexpected error message: {msg}"
        );
    }

    #[test]
    fn ordering_is_lexicographic_on_underlying_str() {
        // BTreeMap users (config rendering, deterministic test fixtures)
        // depend on this. Flipping the impl would silently break TOML
        // diff stability.
        let mut ids = [
            TenantId::new("charlie").unwrap(),
            TenantId::new("acme").unwrap(),
            TenantId::new("bravo").unwrap(),
        ];
        ids.sort();
        let names: Vec<_> = ids.iter().map(|t| t.as_str().to_owned()).collect();
        assert_eq!(names, vec!["acme", "bravo", "charlie"]);
    }
}
