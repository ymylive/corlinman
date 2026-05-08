//! Public type vocabulary for the identity layer.
//!
//! Wire types ([`UserId`], [`ChannelAlias`], [`BindingKind`]) carry
//! `serde::Serialize`/`Deserialize` so they round-trip through the
//! admin REST shape and the inter-tenant federation surfaces (B3, when
//! it lands). Internal helpers stay un-derived.

use std::sync::Arc;

use serde::{Deserialize, Serialize};
use time::OffsetDateTime;
use ulid::Ulid;

/// Opaque canonical handle for one human. ULID-style: 26-character
/// base32 + lexicographically sortable. Internally an `Arc<str>` so
/// passing by value is cheap.
///
/// Construct via [`UserId::generate`] (random) or [`From`] for stored ids
/// (from a stored row). The wire shape is the bare string; serde
/// transparency keeps JSON payloads readable.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct UserId(Arc<str>);

impl UserId {
    /// Mint a fresh ULID-backed user id. Uses the system entropy
    /// source via the `ulid` crate's default RNG path.
    pub fn generate() -> Self {
        Self(Ulid::new().to_string().into())
    }

    /// Wrap an existing user-id string (e.g. one read from SQLite).
    /// Caller is responsible for ensuring the string is the same shape
    /// `generate()` produces — there's no schema check here because
    /// callers writing through the store impl never hand-craft these.
    /// Borrow the raw string for binding/serializing.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl From<String> for UserId {
    fn from(value: String) -> Self {
        Self(value.into())
    }
}

impl From<&str> for UserId {
    fn from(value: &str) -> Self {
        Self(value.into())
    }
}

impl std::fmt::Display for UserId {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

/// How a `(channel, channel_user_id) → user_id` binding was
/// established. Used by the admin UI to flag whether an alias was
/// auto-bound (low confidence) vs. proven via verification (high) vs.
/// operator-decreed (manual override).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BindingKind {
    /// First-seen by `resolve_or_create`. The resolver minted a new
    /// `user_id` and wrote this row. No proof the human is the same
    /// as any other `user_id` — they get their own identity until
    /// verification or operator action says otherwise.
    Auto,
    /// Bound via the verification-phrase protocol — the human
    /// proved they own both ends of the (now-merged) identity.
    Verified,
    /// Bound by operator decision through `/admin/identity`.
    Operator,
}

impl BindingKind {
    /// SQLite text representation. Stable wire shape; both serde and
    /// the store impl use this.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Auto => "auto",
            Self::Verified => "verified",
            Self::Operator => "operator",
        }
    }

    /// Inverse of [`Self::as_str`]. Unknown strings collapse to `Auto`
    /// so a forward-compatible read of an unknown future variant
    /// degrades gracefully rather than 500ing.
    pub fn from_db_str(s: &str) -> Self {
        match s {
            "verified" => Self::Verified,
            "operator" => Self::Operator,
            _ => Self::Auto,
        }
    }
}

/// One row in `user_aliases`. The PK is `(channel, channel_user_id)`,
/// so a single alias maps to exactly one `UserId`; merges work by
/// reattributing rows, not duplicating.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ChannelAlias {
    pub channel: String,
    pub channel_user_id: String,
    pub user_id: UserId,
    pub binding_kind: BindingKind,
    /// RFC-3339 string at the wire boundary; in-memory it's an
    /// `OffsetDateTime` because the time crate's typed format is
    /// safer to compute against.
    #[serde(with = "time::serde::rfc3339")]
    pub created_at: OffsetDateTime,
}

/// One verification phrase issued by an operator and not yet redeemed
/// (or expired). The store owns the lifecycle: created here, transitions
/// to `consumed_at = Some(_)` on redemption, GC'd by a periodic sweep.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct VerificationPhrase {
    pub phrase: String,
    pub user_id: UserId,
    /// The channel the phrase was issued *from*. The redemption must
    /// land on a different channel (the cross-channel proof) to be
    /// useful, but the protocol allows same-channel redemption for
    /// test fixtures.
    pub issued_on_channel: String,
    pub issued_on_channel_user_id: String,
    #[serde(with = "time::serde::rfc3339")]
    pub expires_at: OffsetDateTime,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn user_id_generate_is_unique_per_call() {
        let a = UserId::generate();
        let b = UserId::generate();
        assert_ne!(a, b, "ULIDs must be unique within the same ms tick");
        assert_eq!(a.as_str().len(), 26, "ULID is 26 chars in Crockford base32");
    }

    #[test]
    fn user_id_round_trip_through_string() {
        let original = UserId::generate();
        let s = original.as_str().to_string();
        let restored = UserId::from(s);
        assert_eq!(original, restored);
    }

    #[test]
    fn binding_kind_string_round_trip() {
        for kind in [
            BindingKind::Auto,
            BindingKind::Verified,
            BindingKind::Operator,
        ] {
            assert_eq!(BindingKind::from_db_str(kind.as_str()), kind);
        }
    }

    #[test]
    fn binding_kind_unknown_collapses_to_auto() {
        // Forward-compat: a hypothetical future "federated" variant
        // read off an upgraded DB shouldn't panic.
        assert_eq!(BindingKind::from_db_str("federated"), BindingKind::Auto);
        assert_eq!(BindingKind::from_db_str(""), BindingKind::Auto);
    }

    #[test]
    fn user_id_serializes_as_bare_string() {
        let uid = UserId::from("01HV3K9PQRSTUVWXYZABCDEFGH");
        let json = serde_json::to_string(&uid).unwrap();
        assert_eq!(json, "\"01HV3K9PQRSTUVWXYZABCDEFGH\"");
        let back: UserId = serde_json::from_str(&json).unwrap();
        assert_eq!(back, uid);
    }
}
