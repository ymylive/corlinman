//! Pre-upgrade auth + per-token ACL.
//!
//! Iter 4 shipped a flat `Vec<String>` of accepted bearer tokens. Iter 8
//! widens that into a structured [`TokenAcl`] per token, mirroring the
//! `[[mcp.server.tokens]]` design block:
//!
//! ```toml
//! [[mcp.server.tokens]]
//! token = "<opaque-32-byte-base64>"
//! label = "claude-desktop-laptop"
//! tools_allowlist     = ["web_search", "kb.*"]   # glob patterns
//! resources_allowed   = ["memory", "skill"]      # by URI-scheme prefix
//! prompts_allowed     = ["*"]
//! tenant_id           = "default"
//! ```
//!
//! [`resolve_token`] is the pre-upgrade entry point: given a query-string
//! `token` value, it walks the configured ACL list and returns the matching
//! [`TokenAcl`]. The transport uses this to decide pre-upgrade 401 vs WS
//! upgrade, and to stamp the resolved ACL onto the per-connection
//! [`SessionContext`] so adapters consult the same ACL on every method call.
//!
//! Keep this module dependency-thin: no axum, no tokio. The transport layer
//! and the gateway integration glue both pull from here.

use crate::adapters::SessionContext;

/// Default tenant id when a token is configured without `tenant_id`.
/// Mirrors `corlinman_tenant::DEFAULT_TENANT_ID` ("default") — duplicated
/// here so the schema crate stays free of the tenant dependency for C2's
/// outbound client reuse.
pub const DEFAULT_TENANT_ID: &str = "default";

/// One accepted bearer-token + the per-capability allowlist that bounds
/// what the holder can do on the wire.
///
/// Empty allowlists fail closed (no method allowed). `["*"]` fails open for
/// that one capability. Globs are the same single-`*` shape implemented in
/// [`crate::adapters::glob_match`].
#[derive(Debug, Clone)]
pub struct TokenAcl {
    /// Opaque bearer string. Compared byte-for-byte; no hashing in C1.
    /// (C2-or-later: optional hash-at-rest if the operator config gains
    /// `token_hash` rather than `token`.)
    pub token: String,
    /// Free-form label for logging / metrics ("claude-desktop-laptop").
    /// Never sent over the wire.
    pub label: String,
    /// Glob patterns against `<plugin>:<tool>`.
    pub tools_allowlist: Vec<String>,
    /// URI-scheme prefixes (`"memory"`, `"skill"`, `"persona"`).
    pub resources_allowed: Vec<String>,
    /// Skill-name globs surfaced as MCP prompts.
    pub prompts_allowed: Vec<String>,
    /// Tenant id this token's memory reads route to.
    /// `None` → fallback to [`DEFAULT_TENANT_ID`].
    pub tenant_id: Option<String>,
}

impl TokenAcl {
    /// Build a permissive ACL — every capability set to `["*"]`.
    /// Convenient for tests; production tokens should narrow this.
    pub fn permissive(token: impl Into<String>) -> Self {
        Self {
            token: token.into(),
            label: "permissive".into(),
            tools_allowlist: vec!["*".into()],
            resources_allowed: vec!["*".into()],
            prompts_allowed: vec!["*".into()],
            tenant_id: None,
        }
    }

    /// Resolve the effective tenant id, applying the
    /// [`DEFAULT_TENANT_ID`] fallback.
    pub fn effective_tenant(&self) -> &str {
        match self.tenant_id.as_deref() {
            Some(t) if !t.is_empty() => t,
            _ => DEFAULT_TENANT_ID,
        }
    }

    /// Build the per-session [`SessionContext`] this token grants.
    /// Iter 8: every adapter method receives this context, so any future
    /// ACL fields must extend [`SessionContext`] in lock-step.
    pub fn to_session_context(&self) -> SessionContext {
        SessionContext {
            tools_allowlist: self.tools_allowlist.clone(),
            resources_allowed: self.resources_allowed.clone(),
            prompts_allowed: self.prompts_allowed.clone(),
            tenant_id: Some(self.effective_tenant().to_string()),
            ..Default::default()
        }
    }
}

/// Look up the [`TokenAcl`] matching `presented`. Empty `acls` fails
/// closed (no token resolves) — same posture as the iter-4 transport.
pub fn resolve_token<'a>(acls: &'a [TokenAcl], presented: &str) -> Option<&'a TokenAcl> {
    if presented.is_empty() {
        return None;
    }
    acls.iter().find(|a| a.token == presented)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture() -> Vec<TokenAcl> {
        vec![
            TokenAcl {
                token: "alpha-token".into(),
                label: "alpha-laptop".into(),
                tools_allowlist: vec!["kb:*".into()],
                resources_allowed: vec!["skill".into()],
                prompts_allowed: vec!["*".into()],
                tenant_id: Some("alpha".into()),
            },
            TokenAcl {
                token: "beta-token".into(),
                label: "beta-server".into(),
                tools_allowlist: vec!["web_search".into()],
                resources_allowed: vec!["*".into()],
                prompts_allowed: vec![],
                tenant_id: None, // → default
            },
        ]
    }

    #[test]
    fn resolve_returns_matching_acl() {
        let acls = fixture();
        let acl = resolve_token(&acls, "alpha-token").expect("must match");
        assert_eq!(acl.label, "alpha-laptop");
        assert_eq!(acl.tenant_id.as_deref(), Some("alpha"));
    }

    #[test]
    fn resolve_returns_none_for_unknown_token() {
        let acls = fixture();
        assert!(resolve_token(&acls, "ghost").is_none());
    }

    #[test]
    fn empty_string_token_never_resolves() {
        let acls = fixture();
        assert!(resolve_token(&acls, "").is_none());
    }

    #[test]
    fn empty_acl_list_resolves_nothing_fail_closed() {
        let acls: Vec<TokenAcl> = Vec::new();
        assert!(resolve_token(&acls, "alpha-token").is_none());
    }

    #[test]
    fn missing_tenant_falls_back_to_default_constant() {
        let acls = fixture();
        let acl = resolve_token(&acls, "beta-token").unwrap();
        assert_eq!(acl.effective_tenant(), DEFAULT_TENANT_ID);
        assert_eq!(acl.effective_tenant(), "default");
    }

    #[test]
    fn empty_tenant_string_also_falls_back_to_default() {
        let mut acl = TokenAcl::permissive("t");
        acl.tenant_id = Some(String::new());
        assert_eq!(acl.effective_tenant(), DEFAULT_TENANT_ID);
    }

    #[test]
    fn to_session_context_carries_allowlists_and_tenant() {
        let acls = fixture();
        let alpha = resolve_token(&acls, "alpha-token").unwrap();
        let ctx = alpha.to_session_context();
        assert_eq!(ctx.tools_allowlist, vec!["kb:*".to_string()]);
        assert_eq!(ctx.resources_allowed, vec!["skill".to_string()]);
        assert_eq!(ctx.prompts_allowed, vec!["*".to_string()]);
        assert_eq!(ctx.tenant_id.as_deref(), Some("alpha"));

        // Empty prompts list → closed (the adapter denies on any name).
        let beta = resolve_token(&acls, "beta-token").unwrap();
        let bctx = beta.to_session_context();
        assert!(bctx.prompts_allowed.is_empty());
        assert!(!bctx.allows_prompt("any-name"));
        // tenant fallback materialises into the context.
        assert_eq!(bctx.tenant_id.as_deref(), Some("default"));
    }

    #[test]
    fn permissive_helper_grants_all_capabilities() {
        let acl = TokenAcl::permissive("dev");
        let ctx = acl.to_session_context();
        assert!(ctx.allows_tool("anything:any"));
        assert!(ctx.allows_resource_scheme("memory"));
        assert!(ctx.allows_prompt("any-skill"));
        assert_eq!(ctx.tenant_id.as_deref(), Some("default"));
    }
}
