//! Shared manifest metadata types (plugin-manifest + agent-manifest).
//!
//! Only cross-cutting fields live here. The full `PluginManifest` struct lives
//! in `corlinman-plugins::manifest` where it is consumed; this crate is a
//! leaf dependency so everything that needs the common shape can import it.

use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use time::OffsetDateTime;

/// openclaw's "last touched" convention: UI writes back on save so the
/// registry can surface drift between on-disk manifests and the running
/// gateway version.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct Meta {
    /// Gateway version string at the last UI save.
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub last_touched_version: Option<String>,

    /// RFC 3339 timestamp of the last UI save.
    #[serde(
        skip_serializing_if = "Option::is_none",
        default,
        with = "time::serde::rfc3339::option"
    )]
    #[schemars(with = "Option<String>")]
    pub last_touched_at: Option<OffsetDateTime>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn meta_roundtrip_is_camel_case() {
        let m = Meta {
            last_touched_version: Some("0.1.0".into()),
            last_touched_at: None,
        };
        let v = serde_json::to_value(&m).unwrap();
        assert!(v.get("lastTouchedVersion").is_some());
        assert!(v.get("lastTouchedAt").is_none(), "None is skipped");
    }

    #[test]
    fn meta_accepts_missing_fields() {
        let raw = "{}";
        let m: Meta = serde_json::from_str(raw).unwrap();
        assert!(m.last_touched_version.is_none());
        assert!(m.last_touched_at.is_none());
    }
}
