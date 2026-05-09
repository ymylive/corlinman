//! Capability adapters that bridge corlinman primitives onto the MCP
//! capability families (`tools`, `resources`, `prompts`).
//!
//! Iter 5 lands the [`CapabilityAdapter`] trait and the [`tools`]
//! adapter (`Arc<PluginRegistry>` → `tools/list` + `tools/call`).
//! Iter 6 adds [`prompts`]; iter 7 adds [`resources`]. Each adapter is
//! independently testable and can be stubbed in dispatcher tests.
//!
//! The trait is the seam the iter-9 gateway integration layer wires
//! against — `Vec<Arc<dyn CapabilityAdapter>>` is what the dispatcher
//! holds, and "wire up the tools/prompts/resources surface" reduces to
//! "register three adapters."
//!
//! ## Allowlist filter (per-capability ACL placeholder)
//!
//! The design pins a per-token allowlist for `tools_allowlist` /
//! `resources_allowed` / `prompts_allowed`. Iter 8 wires the full
//! [`server::auth`](crate::server) machinery; iter 5–7 ship a
//! self-contained [`SessionContext`] type carrying just the allowlist
//! shape so adapter unit tests can exercise the filter without dragging
//! in the auth crate. The shape is upward-compatible with iter 8.

pub mod tools;

pub use tools::ToolsAdapter;

use async_trait::async_trait;
use serde_json::Value as JsonValue;

use crate::error::McpError;

/// Per-session view passed into every adapter call.
///
/// Iter 5 only needs the allowlist sets; iter 8 widens this to carry
/// `tenant_id`, `client_label`, and a cancellation token. The struct
/// is intentionally `#[non_exhaustive]` so adding fields in iter 8
/// doesn't break adapter call sites authored here.
#[derive(Debug, Clone, Default)]
#[non_exhaustive]
pub struct SessionContext {
    /// Glob patterns the token is allowed to invoke under `tools/*`.
    /// Empty list → no tools allowed (fail-closed). `["*"]` → all
    /// tools allowed (fail-open). Stored as raw strings; matching
    /// happens in [`glob_match`].
    pub tools_allowlist: Vec<String>,
    /// URI-scheme prefixes the token may read under `resources/*`.
    /// Same fail-closed/fail-open semantics as `tools_allowlist`.
    pub resources_allowed: Vec<String>,
    /// Skill-name globs the token may surface as prompts.
    pub prompts_allowed: Vec<String>,
    /// Tenant id passed through to memory-host queries. `None` defaults
    /// to the workspace's default tenant in iter 8 wiring.
    pub tenant_id: Option<String>,
}

impl SessionContext {
    /// Convenience: a context that allows everything. Used by
    /// dispatcher tests that aren't exercising the ACL.
    pub fn permissive() -> Self {
        Self {
            tools_allowlist: vec!["*".into()],
            resources_allowed: vec!["*".into()],
            prompts_allowed: vec!["*".into()],
            tenant_id: None,
        }
    }

    /// Test if `name` is allowed under `allowlist`. Empty allowlist →
    /// always denied (fail-closed). `*` matches anything; `prefix.*`
    /// matches anything starting with `prefix.` (and so on).
    pub fn allows(allowlist: &[String], name: &str) -> bool {
        if allowlist.is_empty() {
            return false;
        }
        allowlist.iter().any(|p| glob_match(p, name))
    }

    pub fn allows_tool(&self, name: &str) -> bool {
        Self::allows(&self.tools_allowlist, name)
    }

    pub fn allows_resource_scheme(&self, scheme: &str) -> bool {
        Self::allows(&self.resources_allowed, scheme)
    }

    pub fn allows_prompt(&self, name: &str) -> bool {
        Self::allows(&self.prompts_allowed, name)
    }
}

/// Tiny glob matcher: `*` is the only wildcard, matches any run of
/// characters (including the empty string). No character classes, no
/// `?`. Mirrors the design's "glob patterns" intent without pulling in
/// a full glob crate for this one use site.
pub fn glob_match(pattern: &str, name: &str) -> bool {
    // Fast paths.
    if pattern == "*" {
        return true;
    }
    if !pattern.contains('*') {
        return pattern == name;
    }
    // Generic case: split on `*` and match each piece in order. The
    // first piece must be a prefix; the last piece must be a suffix
    // (when the pattern doesn't end in `*`); intermediate pieces
    // must appear in order.
    let parts: Vec<&str> = pattern.split('*').collect();
    let mut cursor = 0usize;
    let last = parts.len() - 1;
    for (i, piece) in parts.iter().enumerate() {
        if piece.is_empty() {
            continue;
        }
        if i == 0 {
            if !name[cursor..].starts_with(piece) {
                return false;
            }
            cursor += piece.len();
        } else if i == last && !pattern.ends_with('*') {
            if !name[cursor..].ends_with(piece) {
                return false;
            }
            // Match anchored at the end — done.
            return name.len() >= cursor + piece.len();
        } else {
            match name[cursor..].find(piece) {
                Some(rel) => cursor += rel + piece.len(),
                None => return false,
            }
        }
    }
    true
}

/// One capability family's worth of MCP method routing.
///
/// The dispatcher (iter 4 stub today, iter 9 final) holds a
/// `Vec<Arc<dyn CapabilityAdapter>>` and dispatches by method-prefix:
/// methods starting with `<adapter.capability_name()>/` go to that
/// adapter. Adapters return a typed JSON value that the dispatcher
/// lifts into a `JsonRpcResponse::Result`.
#[async_trait]
pub trait CapabilityAdapter: Send + Sync {
    /// Capability family this adapter handles. One of `"tools"`,
    /// `"resources"`, `"prompts"`.
    fn capability_name(&self) -> &'static str;

    /// Handle one method call. `method` is the full MCP method (e.g.
    /// `"tools/list"`); `params` is the JSON-RPC `params` field as
    /// received. Implementations parse `params` themselves so they can
    /// emit precise [`McpError::InvalidParams`] payloads.
    async fn handle(
        &self,
        method: &str,
        params: JsonValue,
        ctx: &SessionContext,
    ) -> Result<JsonValue, McpError>;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn glob_star_matches_anything() {
        assert!(glob_match("*", "anything"));
        assert!(glob_match("*", ""));
    }

    #[test]
    fn glob_exact_requires_exact() {
        assert!(glob_match("kb.search", "kb.search"));
        assert!(!glob_match("kb.search", "kb.searcher"));
    }

    #[test]
    fn glob_prefix_star_matches_prefix() {
        assert!(glob_match("kb.*", "kb.search"));
        assert!(glob_match("kb.*", "kb."));
        assert!(!glob_match("kb.*", "other.search"));
    }

    #[test]
    fn glob_star_suffix_matches_suffix() {
        assert!(glob_match("*.json", "doc.json"));
        assert!(!glob_match("*.json", "doc.json.bak"));
    }

    #[test]
    fn glob_middle_star_threads_substring() {
        assert!(glob_match("foo*bar", "foozzbar"));
        assert!(glob_match("foo*bar", "foobar"));
        assert!(!glob_match("foo*bar", "foobaz"));
    }

    #[test]
    fn empty_allowlist_denies_everything() {
        let ctx = SessionContext::default();
        assert!(!ctx.allows_tool("anything"));
        assert!(!ctx.allows_resource_scheme("memory"));
        assert!(!ctx.allows_prompt("any"));
    }

    #[test]
    fn permissive_allows_everything() {
        let ctx = SessionContext::permissive();
        assert!(ctx.allows_tool("kb:search"));
        assert!(ctx.allows_resource_scheme("memory"));
        assert!(ctx.allows_prompt("any-skill"));
    }
}
