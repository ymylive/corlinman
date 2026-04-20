//! Single-pass placeholder substitution with `{{namespace.name}}` syntax.
//!
//! Replaces tokens of the shape `{{namespace.name}}` (or bare `{{name}}`,
//! which is treated as `{{default.name}}`) exactly once per occurrence. This
//! is deliberately simple (single-phase):
//!
//! * Single unified syntax — no `::` args, no agent recursion.
//! * Static lookup is O(1) via a `HashMap<String, String>` keyed by the full
//!   `namespace.name`.
//! * Unknown tokens are left **as-is** (typo-friendly; matches user
//!   expectations when a placeholder wasn't registered).
//! * Empty tokens (`{{}}` or `{{ }}`) are preserved verbatim.
//! * One pass only: if a resolver returns a string that itself contains
//!   `{{…}}` tokens, they are **not** re-expanded.
//!
//! See plan `§placeholder` for rationale.

use std::collections::HashMap;
use std::fmt;

use async_trait::async_trait;
use once_cell::sync::Lazy;
use regex::Regex;

use crate::error::CorlinmanError;

/// Matches `{{ body }}` where `body` contains no braces. The body may be
/// empty or whitespace-only — callers treat those as unresolved and preserve
/// the original token. Non-greedy to keep consecutive placeholders distinct.
static PLACEHOLDER_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"\{\{([^{}]*?)\}\}").expect("placeholder regex is a compile-time constant")
});

/// Namespace assumed when a token has no `.` separator (e.g. `{{today}}` is
/// looked up as `default.today`).
const DEFAULT_NAMESPACE: &str = "default";

/// Context for a single `render` call. Passed to async resolvers so they can
/// key their output by session / trace.
#[derive(Debug, Default, Clone, Copy)]
pub struct RenderContext<'a> {
    /// Stable conversation / caller identifier.
    pub session_key: &'a str,
    /// Optional trace identifier for structured logs.
    pub trace_id: Option<&'a str>,
}

/// Async resolver for a whole namespace. `key` is the portion after the
/// namespace prefix (e.g. `{{weather.beijing}}` with a resolver registered on
/// the `weather` namespace gets `key = "beijing"`).
#[async_trait]
pub trait DynamicResolver: Send + Sync {
    async fn resolve(&self, key: &str, ctx: &RenderContext<'_>) -> Result<String, CorlinmanError>;
}

/// Placeholder engine. Static values are resolved first (O(1) lookup); if
/// absent, the token's namespace is matched against a dynamic resolver.
#[derive(Default)]
pub struct PlaceholderEngine {
    values: HashMap<String, String>,
    dynamic: HashMap<String, Box<dyn DynamicResolver>>,
}

impl fmt::Debug for PlaceholderEngine {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("PlaceholderEngine")
            .field("values", &self.values.len())
            .field(
                "dynamic_namespaces",
                &self.dynamic.keys().collect::<Vec<_>>(),
            )
            .finish()
    }
}

impl PlaceholderEngine {
    /// Construct an empty engine. Populate with [`with_static`] and
    /// [`with_dynamic`].
    pub fn new() -> Self {
        Self::default()
    }

    /// Register a static `namespace.name` entry. Keys with no `.` are stored
    /// as-is and matched only when the template also omits the namespace.
    pub fn with_static(mut self, key: &str, value: impl Into<String>) -> Self {
        self.values.insert(key.to_string(), value.into());
        self
    }

    /// Register an async resolver for every token whose namespace matches.
    pub fn with_dynamic(mut self, namespace: &str, resolver: Box<dyn DynamicResolver>) -> Self {
        self.dynamic.insert(namespace.to_string(), resolver);
        self
    }

    /// Render `template`, replacing each `{{namespace.name}}` token at most
    /// once. Unknown tokens are returned verbatim (they survive into the
    /// output so callers / users can spot typos).
    pub async fn render(
        &self,
        template: &str,
        ctx: &RenderContext<'_>,
    ) -> Result<String, CorlinmanError> {
        let mut out = String::with_capacity(template.len());
        let mut cursor = 0usize;

        for m in PLACEHOLDER_RE.find_iter(template) {
            out.push_str(&template[cursor..m.start()]);
            let raw = m.as_str();
            let body = raw[2..raw.len() - 2].trim();

            if body.is_empty() {
                out.push_str(raw);
                cursor = m.end();
                continue;
            }

            match self.resolve(body, ctx).await? {
                Some(value) => out.push_str(&value),
                None => out.push_str(raw),
            }
            cursor = m.end();
        }
        out.push_str(&template[cursor..]);
        Ok(out)
    }

    /// Resolve a single trimmed token body. Returns `Ok(None)` for unknown
    /// tokens; the caller preserves the original text.
    async fn resolve(
        &self,
        body: &str,
        ctx: &RenderContext<'_>,
    ) -> Result<Option<String>, CorlinmanError> {
        // Phase 1: exact static lookup on the trimmed body (covers both
        // `{{date.today}}` and namespace-less `{{today}}`).
        if let Some(v) = self.values.get(body) {
            return Ok(Some(v.clone()));
        }

        // Split into (namespace, key). A bare token like `{{today}}` becomes
        // `(default, today)`.
        let (namespace, key) = match body.split_once('.') {
            Some((ns, k)) => (ns, k),
            None => (DEFAULT_NAMESPACE, body),
        };

        // Phase 1b: also try the synthesised `default.<name>` static form so
        // callers who registered `with_static("default.today", …)` can match
        // either `{{today}}` or `{{default.today}}`.
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

    fn ctx() -> RenderContext<'static> {
        RenderContext {
            session_key: "test",
            trace_id: Some("trace-1"),
        }
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
    async fn unknown_placeholder_preserved_verbatim() {
        let eng = PlaceholderEngine::new().with_static("date.today", "X");
        let out = eng
            .render("{{mystery.thing}} and {{date.today}}", &ctx())
            .await
            .unwrap();
        assert_eq!(out, "{{mystery.thing}} and X");
    }

    #[tokio::test]
    async fn empty_token_preserved() {
        let eng = PlaceholderEngine::new();
        let out = eng.render("a {{}} b {{ }} c", &ctx()).await.unwrap();
        assert_eq!(out, "a {{}} b {{ }} c");
    }

    // ----- UTF-8 -------------------------------------------------------------

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

    // ----- Dynamic resolver --------------------------------------------------

    struct UpperResolver;

    #[async_trait]
    impl DynamicResolver for UpperResolver {
        async fn resolve(
            &self,
            key: &str,
            _ctx: &RenderContext<'_>,
        ) -> Result<String, CorlinmanError> {
            Ok(key.to_uppercase())
        }
    }

    struct FailingResolver;

    #[async_trait]
    impl DynamicResolver for FailingResolver {
        async fn resolve(
            &self,
            _key: &str,
            _ctx: &RenderContext<'_>,
        ) -> Result<String, CorlinmanError> {
            Err(CorlinmanError::Parse {
                what: "placeholder",
                message: "resolver blew up".into(),
            })
        }
    }

    #[tokio::test]
    async fn dynamic_resolver_handles_namespace() {
        let eng = PlaceholderEngine::new().with_dynamic("upper", Box::new(UpperResolver));
        let out = eng
            .render("{{upper.hello}} / {{upper.world}}", &ctx())
            .await
            .unwrap();
        assert_eq!(out, "HELLO / WORLD");
    }

    #[tokio::test]
    async fn resolver_error_propagates() {
        let eng = PlaceholderEngine::new().with_dynamic("boom", Box::new(FailingResolver));
        let err = eng.render("{{boom.x}}", &ctx()).await.unwrap_err();
        match err {
            CorlinmanError::Parse { what, message } => {
                assert_eq!(what, "placeholder");
                assert!(message.contains("blew up"));
            }
            other => panic!("expected Parse error, got {other:?}"),
        }
    }

    // ----- No recursion ------------------------------------------------------

    #[tokio::test]
    async fn nested_tokens_not_re_expanded() {
        // The stored value contains another `{{…}}` shape; the engine must
        // leave it alone (single-pass contract).
        let eng = PlaceholderEngine::new()
            .with_static("outer.ref", "{{inner.value}}")
            .with_static("inner.value", "LEAKED");
        let out = eng.render("X = {{outer.ref}}", &ctx()).await.unwrap();
        assert_eq!(out, "X = {{inner.value}}");
    }

    // ----- Whitespace tolerance ---------------------------------------------

    #[tokio::test]
    async fn whitespace_inside_braces_is_trimmed() {
        let eng = PlaceholderEngine::new().with_static("date.today", "2026-04-20");
        let out = eng
            .render("{{ date.today }} / {{  date.today}}", &ctx())
            .await
            .unwrap();
        assert_eq!(out, "2026-04-20 / 2026-04-20");
    }

    // ----- Namespace discrimination -----------------------------------------

    #[tokio::test]
    async fn same_key_different_namespace_resolves_independently() {
        let eng = PlaceholderEngine::new()
            .with_static("date.today", "2026-04-20")
            .with_static("date.tomorrow", "2026-04-21");
        let out = eng
            .render("{{date.today}} -> {{date.tomorrow}}", &ctx())
            .await
            .unwrap();
        assert_eq!(out, "2026-04-20 -> 2026-04-21");
    }

    // ----- Bare (namespace-less) tokens --------------------------------------

    #[tokio::test]
    async fn bare_token_matches_default_namespace() {
        let eng = PlaceholderEngine::new().with_static("default.today", "2026-04-20");
        let out = eng.render("today={{today}}", &ctx()).await.unwrap();
        assert_eq!(out, "today=2026-04-20");
    }

    // ----- Static beats dynamic ----------------------------------------------

    #[tokio::test]
    async fn static_wins_over_dynamic() {
        let eng = PlaceholderEngine::new()
            .with_static("upper.hello", "static-wins")
            .with_dynamic("upper", Box::new(UpperResolver));
        let out = eng.render("{{upper.hello}}", &ctx()).await.unwrap();
        assert_eq!(out, "static-wins");
    }
}
