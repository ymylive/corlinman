//! Placeholder substitution with `{{namespace.name}}` syntax.
//!
//! Replaces tokens of the shape `{{namespace.name}}` (or bare `{{name}}`,
//! which is treated as `{{default.name}}`). Supports:
//!
//! * **Static keys** — O(1) lookup via `HashMap<String, String>` keyed by the
//!   full `namespace.name`. Back-compat with the original flat-key engine.
//! * **Namespace routing** — resolvers registered under a namespace prefix
//!   handle every `{{prefix.*}}` token; split on the first `.` only.
//! * **Reserved namespaces** — `var`, `sar`, `tar`, `agent`, `session`, `tool`,
//!   `vector`, `skill` are known prefixes even before a resolver is wired.
//! * **Recursive expansion** — if a resolver (or a static value) returns text
//!   containing more `{{…}}` tokens, re-expand up to `max_depth` (default 4).
//! * **Cycle detection** — a `HashSet<String>` of in-flight keys; a repeated
//!   key returns [`PlaceholderError::Cycle`] instead of looping forever.
//! * **Unknown tokens** — left verbatim in the output (typo-friendly).
//! * **Empty tokens** (`{{}}` / `{{ }}`) — preserved verbatim.
//!
//! See plan §B1-BE2 for rationale.

use std::collections::{HashMap, HashSet};
use std::fmt;
use std::sync::Arc;

use async_trait::async_trait;
use once_cell::sync::Lazy;
use regex::Regex;

use crate::error::CorlinmanError;

/// Matches `{{ body }}` where `body` contains no braces. Non-greedy to keep
/// consecutive placeholders distinct.
static PLACEHOLDER_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"\{\{([^{}]*?)\}\}").expect("placeholder regex is a compile-time constant")
});

/// Namespace assumed when a token has no `.` separator (e.g. `{{today}}` is
/// looked up as `default.today`).
const DEFAULT_NAMESPACE: &str = "default";

/// Default maximum recursive expansion depth. We pick 4 to keep prompts cheap
/// while still covering nested-agent-card chains.
pub const DEFAULT_MAX_DEPTH: u32 = 4;

/// Namespace prefixes reserved by the corlinman runtime. Listed here so
/// subsystems (config loader, lint tooling, docs) can share the authoritative
/// set even before a resolver is wired.
///
/// `episodes` is the Phase 4 W4 D1 read surface: tokens like
/// `{{episodes.last_week}}`, `{{episodes.kind(incident)}}`,
/// `{{episodes.about_id(<ulid>)}}` resolve via the
/// `corlinman-gateway::placeholder::episodes` resolver against the
/// per-tenant `episodes.sqlite` written by `corlinman-episodes`.
pub const RESERVED_NAMESPACES: &[&str] = &[
    "var", "sar", "tar", "agent", "session", "tool", "vector", "skill", "episodes",
];

/// Errors unique to the placeholder engine. Converts into [`CorlinmanError`]
/// via `From` so existing fallible APIs keep their return type.
#[derive(Debug, thiserror::Error)]
pub enum PlaceholderError {
    /// A placeholder key re-appeared while it was already being resolved.
    /// Classic in-flight-set cycle guard.
    #[error("placeholder cycle detected at key '{0}'")]
    Cycle(String),

    /// Recursive expansion exceeded [`PlaceholderEngine::max_depth`].
    #[error("placeholder recursion depth {depth} exceeded (max {max})")]
    DepthExceeded { depth: u32, max: u32 },

    /// Propagated from a dynamic resolver.
    #[error("resolver for '{namespace}' failed: {message}")]
    Resolver { namespace: String, message: String },
}

impl From<PlaceholderError> for CorlinmanError {
    fn from(err: PlaceholderError) -> Self {
        CorlinmanError::Parse {
            what: "placeholder",
            message: err.to_string(),
        }
    }
}

/// Context for a single `render` call. Passed to async resolvers so they can
/// key their output by session / model / arbitrary metadata.
#[derive(Debug, Default, Clone)]
pub struct PlaceholderCtx {
    /// Stable conversation / caller identifier.
    pub session_key: String,
    /// Target model id (used by resolvers that vary output by model, e.g.
    /// token-budget-aware vector retrieval).
    pub model_name: Option<String>,
    /// Free-form metadata the caller may thread through (trace id, locale, …).
    pub metadata: HashMap<String, String>,
}

impl PlaceholderCtx {
    pub fn new(session_key: impl Into<String>) -> Self {
        Self {
            session_key: session_key.into(),
            model_name: None,
            metadata: HashMap::new(),
        }
    }

    pub fn with_model(mut self, model: impl Into<String>) -> Self {
        self.model_name = Some(model.into());
        self
    }

    pub fn with_meta(mut self, key: impl Into<String>, value: impl Into<String>) -> Self {
        self.metadata.insert(key.into(), value.into());
        self
    }
}

/// Back-compat alias for the pre-B1-BE2 `RenderContext<'_>` borrowed struct.
/// New callers should use [`PlaceholderCtx`] directly.
pub type RenderContext<'a> = &'a PlaceholderCtx;

/// Async resolver for a whole namespace. `key` is the portion after the
/// namespace prefix (e.g. `{{weather.beijing}}` with a resolver registered on
/// the `weather` namespace gets `key = "beijing"`).
#[async_trait]
pub trait DynamicResolver: Send + Sync {
    async fn resolve(&self, key: &str, ctx: &PlaceholderCtx) -> Result<String, PlaceholderError>;
}

/// Placeholder engine. Static values are resolved first (O(1) lookup); if
/// absent, the token's namespace is matched against a dynamic resolver.
pub struct PlaceholderEngine {
    values: HashMap<String, String>,
    dynamic: HashMap<String, Arc<dyn DynamicResolver>>,
    max_depth: u32,
}

impl Default for PlaceholderEngine {
    fn default() -> Self {
        Self {
            values: HashMap::new(),
            dynamic: HashMap::new(),
            max_depth: DEFAULT_MAX_DEPTH,
        }
    }
}

impl fmt::Debug for PlaceholderEngine {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("PlaceholderEngine")
            .field("values", &self.values.len())
            .field(
                "dynamic_namespaces",
                &self.dynamic.keys().collect::<Vec<_>>(),
            )
            .field("max_depth", &self.max_depth)
            .finish()
    }
}

impl PlaceholderEngine {
    /// Construct an empty engine with [`DEFAULT_MAX_DEPTH`]. Populate with
    /// [`with_static`] / [`register_namespace`].
    pub fn new() -> Self {
        Self::default()
    }

    /// Override the recursion ceiling. Depth 0 disables recursive expansion
    /// (a single pass only — matches the pre-B1-BE2 behaviour).
    pub fn with_max_depth(mut self, max_depth: u32) -> Self {
        self.max_depth = max_depth;
        self
    }

    /// Current max recursion depth.
    pub fn max_depth(&self) -> u32 {
        self.max_depth
    }

    /// Clone this engine's registrations with a different recursion ceiling.
    ///
    /// Dynamic resolvers are stored behind `Arc`, so this preserves the same
    /// resolver instances without forcing callers to rebuild the registry.
    pub fn clone_with_max_depth(&self, max_depth: u32) -> Self {
        Self {
            values: self.values.clone(),
            dynamic: self.dynamic.clone(),
            max_depth,
        }
    }

    /// Register a static `namespace.name` entry (or bare `name`).
    pub fn with_static(mut self, key: &str, value: impl Into<String>) -> Self {
        self.values.insert(key.to_string(), value.into());
        self
    }

    /// Builder-style sibling of [`register_namespace`]. Handy for one-shot
    /// wiring at call sites.
    pub fn with_dynamic(mut self, namespace: &str, resolver: Arc<dyn DynamicResolver>) -> Self {
        self.dynamic.insert(namespace.to_string(), resolver);
        self
    }

    /// Register (or replace) a dynamic resolver for `prefix`. Reserved
    /// namespaces (see [`RESERVED_NAMESPACES`]) are allowed — the list is
    /// informational, not exclusive. Returns the previous resolver if any.
    pub fn register_namespace(
        &mut self,
        prefix: &str,
        resolver: Arc<dyn DynamicResolver>,
    ) -> Option<Arc<dyn DynamicResolver>> {
        self.dynamic.insert(prefix.to_string(), resolver)
    }

    /// Convenience: whether `prefix` is one of the reserved runtime namespaces.
    pub fn is_reserved_namespace(prefix: &str) -> bool {
        RESERVED_NAMESPACES.contains(&prefix)
    }

    /// Render `template`, replacing each `{{namespace.name}}` token. Values
    /// produced by resolvers are themselves scanned for placeholders up to
    /// `max_depth`; cycles return [`PlaceholderError::Cycle`].
    ///
    /// Unknown tokens are returned verbatim.
    pub async fn render(
        &self,
        template: &str,
        ctx: &PlaceholderCtx,
    ) -> Result<String, CorlinmanError> {
        let span = tracing::debug_span!(
            "placeholder_render",
            template_len = template.len(),
            depth_used = tracing::field::Empty,
            unresolved_count = tracing::field::Empty,
        );
        let _enter = span.enter();

        let mut in_flight: HashSet<String> = HashSet::new();
        let mut max_depth_used: u32 = 0;
        let result = self
            .render_inner(template, ctx, &mut in_flight, 0, &mut max_depth_used)
            .await;

        // Count unresolved `{{…}}` tokens left in the final output as a
        // proxy for "templates the runtime couldn't fully expand". Cheap
        // (linear scan over the already-allocated result).
        let unresolved_count = match &result {
            Ok(s) => PLACEHOLDER_RE.find_iter(s).count(),
            Err(_) => 0,
        };
        span.record("depth_used", max_depth_used as u64);
        span.record("unresolved_count", unresolved_count as u64);
        result.map_err(Into::into)
    }

    /// Internal recursive render. Kept non-public so the `in_flight` /
    /// `depth` invariants stay inside the module. Returns `PlaceholderError`
    /// directly; the public `render` converts to `CorlinmanError` once.
    async fn render_inner(
        &self,
        template: &str,
        ctx: &PlaceholderCtx,
        in_flight: &mut HashSet<String>,
        depth: u32,
        max_depth_seen: &mut u32,
    ) -> Result<String, PlaceholderError> {
        if depth > *max_depth_seen {
            *max_depth_seen = depth;
        }
        if depth > self.max_depth {
            return Err(PlaceholderError::DepthExceeded {
                depth,
                max: self.max_depth,
            });
        }

        // Fast path: if there are no `{{` at all, skip the regex + allocation.
        if !template.contains("{{") {
            return Ok(template.to_string());
        }

        let mut out = String::with_capacity(template.len());
        let mut cursor = 0usize;

        // Collect matches so we can await inside the loop without borrowing
        // the iterator across await points.
        let matches: Vec<_> = PLACEHOLDER_RE.find_iter(template).collect();
        for m in matches {
            out.push_str(&template[cursor..m.start()]);
            let raw = m.as_str();
            let body = raw[2..raw.len() - 2].trim();

            if body.is_empty() {
                out.push_str(raw);
                cursor = m.end();
                continue;
            }

            match self.resolve_once(body, ctx).await? {
                Some(value) => {
                    if value.contains("{{") && self.max_depth > 0 {
                        let key = body.to_string();
                        if !in_flight.insert(key.clone()) {
                            return Err(PlaceholderError::Cycle(key));
                        }
                        let expanded = Box::pin(self.render_inner(
                            &value,
                            ctx,
                            in_flight,
                            depth + 1,
                            max_depth_seen,
                        ))
                        .await;
                        in_flight.remove(&key);
                        out.push_str(&expanded?);
                    } else {
                        out.push_str(&value);
                    }
                }
                None => out.push_str(raw),
            }
            cursor = m.end();
        }
        out.push_str(&template[cursor..]);
        Ok(out)
    }

    /// Resolve a single trimmed token body (one hop, no recursion). Returns
    /// `Ok(None)` for unknown tokens; the caller preserves the original text.
    async fn resolve_once(
        &self,
        body: &str,
        ctx: &PlaceholderCtx,
    ) -> Result<Option<String>, PlaceholderError> {
        // Phase 1: flat static lookup — covers the legacy "register keys
        // without a namespace-aware resolver" usage.
        if let Some(v) = self.values.get(body) {
            return Ok(Some(v.clone()));
        }

        // Split into (namespace, key). A bare token like `{{today}}` becomes
        // `(default, today)`.
        let (namespace, key) = match body.split_once('.') {
            Some((ns, k)) => (ns, k),
            None => (DEFAULT_NAMESPACE, body),
        };

        // Phase 1b: synthesised `default.<name>` form.
        if !body.contains('.') {
            let synth = format!("{DEFAULT_NAMESPACE}.{body}");
            if let Some(v) = self.values.get(&synth) {
                return Ok(Some(v.clone()));
            }
        }

        // Phase 2: dynamic namespace resolver.
        if let Some(resolver) = self.dynamic.get(namespace) {
            let rendered = resolver.resolve(key, ctx).await?;
            return Ok(Some(rendered));
        }

        // Unknown → preserve verbatim.
        Ok(None)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    fn ctx() -> PlaceholderCtx {
        PlaceholderCtx::new("test").with_meta("trace", "trace-1")
    }

    // ----- Static values -----------------------------------------------------

    #[tokio::test]
    async fn static_hit_replaces_token() {
        let eng = PlaceholderEngine::new().with_static("date.today", "2026-04-20");
        let out = eng.render("today is {{date.today}}", &ctx()).await.unwrap();
        assert_eq!(out, "today is 2026-04-20");
    }

    #[tokio::test]
    async fn multiple_tokens_all_replaced() {
        let eng = PlaceholderEngine::new()
            .with_static("date.today", "2026-04-20")
            .with_static("system.port", "6005")
            .with_static("user.name", "Nova");
        let out = eng
            .render(
                "{{user.name}} @ {{date.today}} on port {{system.port}}",
                &ctx(),
            )
            .await
            .unwrap();
        assert_eq!(out, "Nova @ 2026-04-20 on port 6005");
    }

    #[tokio::test]
    async fn empty_token_preserved() {
        let eng = PlaceholderEngine::new();
        let out = eng.render("a {{}} b {{ }} c", &ctx()).await.unwrap();
        assert_eq!(out, "a {{}} b {{ }} c");
    }

    #[tokio::test]
    async fn utf8_values_and_templates_roundtrip() {
        let eng = PlaceholderEngine::new()
            .with_static("user.name", "小克🐱")
            .with_static("greeting.cn", "你好，世界🌏");
        let out = eng
            .render("{{greeting.cn}} — 我是 {{user.name}}", &ctx())
            .await
            .unwrap();
        assert_eq!(out, "你好，世界🌏 — 我是 小克🐱");
    }

    #[tokio::test]
    async fn whitespace_inside_braces_is_trimmed() {
        let eng = PlaceholderEngine::new().with_static("date.today", "2026-04-20");
        let out = eng
            .render("{{ date.today }} / {{  date.today}}", &ctx())
            .await
            .unwrap();
        assert_eq!(out, "2026-04-20 / 2026-04-20");
    }

    #[tokio::test]
    async fn bare_token_matches_default_namespace() {
        let eng = PlaceholderEngine::new().with_static("default.today", "2026-04-20");
        let out = eng.render("today={{today}}", &ctx()).await.unwrap();
        assert_eq!(out, "today=2026-04-20");
    }

    // ----- Dynamic resolver --------------------------------------------------

    struct UpperResolver;

    #[async_trait]
    impl DynamicResolver for UpperResolver {
        async fn resolve(
            &self,
            key: &str,
            _ctx: &PlaceholderCtx,
        ) -> Result<String, PlaceholderError> {
            Ok(key.to_uppercase())
        }
    }

    /// Records every key it was asked to resolve, so tests can prove routing.
    struct RecordingResolver {
        tag: &'static str,
        seen: Mutex<Vec<String>>,
    }

    #[async_trait]
    impl DynamicResolver for RecordingResolver {
        async fn resolve(
            &self,
            key: &str,
            _ctx: &PlaceholderCtx,
        ) -> Result<String, PlaceholderError> {
            self.seen.lock().unwrap().push(key.to_string());
            Ok(format!("{}:{key}", self.tag))
        }
    }

    #[tokio::test]
    async fn dynamic_resolver_handles_namespace() {
        let eng = PlaceholderEngine::new().with_dynamic("upper", Arc::new(UpperResolver));
        let out = eng
            .render("{{upper.hello}} / {{upper.world}}", &ctx())
            .await
            .unwrap();
        assert_eq!(out, "HELLO / WORLD");
    }

    // ----- Required B1-BE2 tests --------------------------------------------

    #[tokio::test]
    async fn test_namespace_routing() {
        // Two namespaces, one template: each resolver must see only its own keys.
        let agent = Arc::new(RecordingResolver {
            tag: "agent",
            seen: Mutex::new(Vec::new()),
        });
        let var = Arc::new(RecordingResolver {
            tag: "var",
            seen: Mutex::new(Vec::new()),
        });
        let mut eng = PlaceholderEngine::new();
        eng.register_namespace("agent", agent.clone());
        eng.register_namespace("var", var.clone());

        let out = eng
            .render("{{agent.mentor}} {{var.foo}} {{agent.peer}}", &ctx())
            .await
            .unwrap();
        assert_eq!(out, "agent:mentor var:foo agent:peer");

        let agent_seen = agent.seen.lock().unwrap().clone();
        let var_seen = var.seen.lock().unwrap().clone();
        assert_eq!(agent_seen, vec!["mentor".to_string(), "peer".to_string()]);
        assert_eq!(var_seen, vec!["foo".to_string()]);
    }

    #[tokio::test]
    async fn test_recursion_expand() {
        // {{a}} → "{{b}}" → "final"
        let eng = PlaceholderEngine::new()
            .with_static("a", "{{b}}")
            .with_static("b", "final");
        let out = eng.render("value={{a}}", &ctx()).await.unwrap();
        assert_eq!(out, "value=final");
    }

    #[tokio::test]
    async fn test_cycle_detection() {
        // {{a}} → "{{b}}" → "{{a}}" — must bail out, not stack-overflow.
        let eng = PlaceholderEngine::new()
            .with_static("a", "{{b}}")
            .with_static("b", "{{a}}");
        let err = eng.render("{{a}}", &ctx()).await.unwrap_err();
        // Cycle surfaces via CorlinmanError::Parse with a message mentioning the key.
        match err {
            CorlinmanError::Parse { what, message } => {
                assert_eq!(what, "placeholder");
                assert!(
                    message.contains("cycle")
                        && (message.contains("'a'") || message.contains("'b'")),
                    "unexpected cycle message: {message}"
                );
            }
            other => panic!("expected Parse(cycle) error, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn test_flat_backcompat() {
        // Old API: static keys registered with the full namespaced form, plus
        // same-key-different-namespace discrimination.
        let eng = PlaceholderEngine::new()
            .with_static("date.today", "2026-04-20")
            .with_static("date.tomorrow", "2026-04-21")
            .with_static("system.port", "6005");
        let out = eng
            .render(
                "{{date.today}} -> {{date.tomorrow}} @ {{system.port}}",
                &ctx(),
            )
            .await
            .unwrap();
        assert_eq!(out, "2026-04-20 -> 2026-04-21 @ 6005");
    }

    #[tokio::test]
    async fn test_unknown_key_passthrough() {
        let eng = PlaceholderEngine::new().with_static("date.today", "X");
        let out = eng
            .render("{{mystery.thing}} and {{date.today}} and {{xyz}}", &ctx())
            .await
            .unwrap();
        assert_eq!(out, "{{mystery.thing}} and X and {{xyz}}");
    }

    // ----- Ancillary coverage -----------------------------------------------

    #[tokio::test]
    async fn static_wins_over_dynamic() {
        let eng = PlaceholderEngine::new()
            .with_static("upper.hello", "static-wins")
            .with_dynamic("upper", Arc::new(UpperResolver));
        let out = eng.render("{{upper.hello}}", &ctx()).await.unwrap();
        assert_eq!(out, "static-wins");
    }

    #[tokio::test]
    async fn reserved_namespaces_listed() {
        for ns in [
            "var", "sar", "tar", "agent", "session", "tool", "vector", "skill", "episodes",
        ] {
            assert!(
                PlaceholderEngine::is_reserved_namespace(ns),
                "{ns} should be reserved"
            );
        }
        assert!(!PlaceholderEngine::is_reserved_namespace("upper"));
    }

    #[tokio::test]
    async fn depth_zero_disables_recursion() {
        // With max_depth = 0, a returned `{{b}}` is left as literal text.
        let eng = PlaceholderEngine::new()
            .with_static("a", "{{b}}")
            .with_static("b", "final")
            .with_max_depth(0);
        let out = eng.render("{{a}}", &ctx()).await.unwrap();
        assert_eq!(out, "{{b}}");
    }

    #[tokio::test]
    async fn depth_limit_errors_out() {
        // Each expansion introduces a fresh key, so cycle detection doesn't
        // fire — only the depth guard does. max_depth=2 allows 2 recursive
        // expansions then refuses.
        let eng = PlaceholderEngine::new()
            .with_static("l0", "{{l1}}")
            .with_static("l1", "{{l2}}")
            .with_static("l2", "{{l3}}")
            .with_static("l3", "{{l4}}")
            .with_static("l4", "{{l5}}")
            .with_max_depth(2);
        let err = eng.render("{{l0}}", &ctx()).await.unwrap_err();
        match err {
            CorlinmanError::Parse { message, .. } => {
                assert!(message.contains("recursion depth"), "got: {message}");
            }
            other => panic!("expected Parse(depth) error, got {other:?}"),
        }
    }
}
