//! Sprint 9 T2 — regex-based one-pass renderer for the openclaw-style
//! placeholder dialect the diary / PaperReader / sub-agent memory systems
//! need.
//!
//! This module is **deliberately separate** from [`crate::placeholder`] (the
//! `{{namespace.name}}` async engine). The dialects are different shapes:
//!
//! | Form                      | Meaning                                               |
//! | ------------------------- | ----------------------------------------------------- |
//! | `{{Var:agent_name}}`      | simple lookup from the [`PlaceholderContext`] var map |
//! | `{{TarToday}}`, `{{TarNow}}`, `{{TarIso}}` | built-in date/time tokens             |
//! | `{{<Agent>日记本}}`        | RAG retrieval against `diary:<Agent>` namespace       |
//! | `[[x::TagMemo0.55]]`      | tag-memo placeholder resolved by the RAG wing later   |
//! | `<<x>>`                   | "all-mode" marker: "dump everything in this namespace"|
//!
//! # Design notes
//!
//! - **One pass, no recursion.** A resolver returning text that looks like
//!   another placeholder doesn't get re-expanded. Same contract as
//!   [`crate::placeholder`].
//! - **Unknown placeholders render verbatim.** A missing variable never
//!   kills prompt composition: the token stays in the prompt so humans
//!   see the typo. Resolver *errors* (e.g. RAG backend blew up) are
//!   returned via [`RenderError`] — the caller decides whether to
//!   hard-fail or attach the error as debug metadata.
//! - **No gateway calls.** The renderer accepts abstractions
//!   ([`RagRetriever`], [`NamespaceResolver`], [`TimeSource`]); wiring to
//!   the live gateway happens on the Python side in S9.T3.
//!
//! # Built-in variables
//!
//! - `{{TarToday}}` → YYYY-MM-DD (UTC date from [`TimeSource::now`])
//! - `{{TarNow}}`   → HH:MM (UTC time)
//! - `{{TarIso}}`   → RFC3339 string
//!
//! # Usage
//!
//! ```ignore
//! use corlinman_core::placeholders::{PlaceholderContext, Renderer, SystemTime};
//!
//! let ctx = PlaceholderContext::builder()
//!     .var("agent_name", "Aemeath")
//!     .time(Arc::new(SystemTime))
//!     .build();
//! let out = Renderer::new(ctx).render("hi {{Var:agent_name}} on {{TarToday}}")?;
//! ```

use std::collections::HashMap;
use std::fmt;
use std::sync::Arc;

use once_cell::sync::Lazy;
use regex::Regex;
use time::format_description::well_known::Rfc3339;
use time::macros::format_description;
use time::OffsetDateTime;

use crate::error::CorlinmanError;

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

/// Structured errors the renderer returns instead of panicking / silently
/// dropping failures. The caller (Python `system_prompt` composer for S10,
/// or a Rust subagent helper later) can pattern-match to decide whether
/// to hard-fail composition or attach the error as debug metadata.
#[derive(Debug, thiserror::Error)]
pub enum RenderError {
    /// A RAG-backed placeholder (`{{Agent日记本}}`, `<<ns>>`) failed to
    /// resolve. The underlying error is preserved verbatim — callers that
    /// want the chain call `.source()`.
    #[error("RAG retrieval for namespace '{namespace}' failed: {message}")]
    RagRetrieval { namespace: String, message: String },
    /// Malformed input (e.g. an unterminated `{{` that the regex refused
    /// to match). We never bubble this today — the regex is greedy-safe —
    /// but it's the right slot for future parser tightening.
    #[error("placeholder parse error: {0}")]
    Parse(String),
}

impl From<RenderError> for CorlinmanError {
    fn from(err: RenderError) -> Self {
        match err {
            RenderError::RagRetrieval { namespace, message } => CorlinmanError::Storage(format!(
                "placeholder RAG lookup (ns={namespace}): {message}"
            )),
            RenderError::Parse(msg) => CorlinmanError::Parse {
                what: "placeholder",
                message: msg,
            },
        }
    }
}

// ---------------------------------------------------------------------------
// Abstractions
// ---------------------------------------------------------------------------

/// Pluggable time supplier. The renderer treats the returned
/// `OffsetDateTime` as UTC for `{{TarToday}}`/`{{TarNow}}`/`{{TarIso}}`
/// formatting. Tests inject a fixed-clock to keep output deterministic.
pub trait TimeSource: Send + Sync {
    fn now(&self) -> OffsetDateTime;
}

/// Default [`TimeSource`] that reads the system clock.
#[derive(Debug, Default, Clone, Copy)]
pub struct SystemTime;

impl TimeSource for SystemTime {
    fn now(&self) -> OffsetDateTime {
        OffsetDateTime::now_utc()
    }
}

/// Fixed-clock [`TimeSource`] for tests. Returns the supplied instant
/// on every `now()` call.
#[derive(Debug, Clone, Copy)]
pub struct FixedTime(pub OffsetDateTime);

impl TimeSource for FixedTime {
    fn now(&self) -> OffsetDateTime {
        self.0
    }
}

/// Map from an agent short-name to a vector-store namespace string.
///
/// The default implementation is `|agent| format!("diary:{agent}")`;
/// downstream wiring can override for group / shared / public
/// (per §7.5 three-tier isolation).
pub trait NamespaceResolver: Send + Sync {
    fn namespace_for_agent(&self, agent: &str) -> String;
}

/// `agent → "diary:<agent>"` — the default per §7.5.
#[derive(Debug, Default, Clone, Copy)]
pub struct DiaryNamespaceResolver;

impl NamespaceResolver for DiaryNamespaceResolver {
    fn namespace_for_agent(&self, agent: &str) -> String {
        format!("diary:{agent}")
    }
}

/// Abstract RAG retriever. The renderer holds a trait-object so unit
/// tests can plug a fake without depending on `corlinman-vector`.
///
/// Implementations must be cheap enough to call from the synchronous
/// renderer path: the renderer runs in prompt-composition context and
/// can't yield. For Rust callers who need async, wrap the retriever
/// with a blocking adapter upstream.
///
/// All calls are expected to be idempotent and safe to retry; the
/// renderer never retries on its own.
pub trait RagRetriever: Send + Sync {
    /// Retrieve diary-style text for the `{{Agent日记本}}` form.
    /// Returned string is spliced verbatim into the prompt — callers
    /// are responsible for any summarisation / truncation.
    fn retrieve_namespace(&self, namespace: &str) -> Result<String, RenderError>;

    /// Dump the whole namespace (powers the `<<ns>>` all-mode marker).
    /// Default implementation forwards to [`Self::retrieve_namespace`]
    /// so simple retrievers only need to implement one method.
    fn dump_namespace(&self, namespace: &str) -> Result<String, RenderError> {
        self.retrieve_namespace(namespace)
    }
}

// ---------------------------------------------------------------------------
// Context + builder
// ---------------------------------------------------------------------------

/// Everything the renderer needs at `render()` time. Construct via
/// [`PlaceholderContext::builder`].
#[derive(Clone)]
pub struct PlaceholderContext {
    vars: HashMap<String, String>,
    time: Arc<dyn TimeSource>,
    retriever: Option<Arc<dyn RagRetriever>>,
    ns_resolver: Arc<dyn NamespaceResolver>,
}

impl fmt::Debug for PlaceholderContext {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("PlaceholderContext")
            .field("vars", &self.vars.len())
            .field("retriever", &self.retriever.is_some())
            .finish_non_exhaustive()
    }
}

/// Builder for [`PlaceholderContext`] — every field has a sensible
/// default so downstream callers only wire what they need.
pub struct PlaceholderContextBuilder {
    vars: HashMap<String, String>,
    time: Arc<dyn TimeSource>,
    retriever: Option<Arc<dyn RagRetriever>>,
    ns_resolver: Arc<dyn NamespaceResolver>,
}

impl PlaceholderContextBuilder {
    /// Set a single variable (overwrites existing).
    pub fn var(mut self, key: &str, value: impl Into<String>) -> Self {
        self.vars.insert(key.to_string(), value.into());
        self
    }

    /// Bulk-merge variables; later inserts win over earlier ones.
    pub fn vars(mut self, map: HashMap<String, String>) -> Self {
        self.vars.extend(map);
        self
    }

    /// Swap the time source (tests use [`FixedTime`]).
    pub fn time(mut self, source: Arc<dyn TimeSource>) -> Self {
        self.time = source;
        self
    }

    /// Attach a RAG retriever. `None` (the default) means
    /// `{{Agent日记本}}` + `<<ns>>` will render verbatim because there's
    /// nothing to ask.
    pub fn retriever(mut self, retriever: Arc<dyn RagRetriever>) -> Self {
        self.retriever = Some(retriever);
        self
    }

    /// Swap the agent→namespace mapper. Default maps
    /// `<agent>` → `diary:<agent>`.
    pub fn namespace_resolver(mut self, resolver: Arc<dyn NamespaceResolver>) -> Self {
        self.ns_resolver = resolver;
        self
    }

    pub fn build(self) -> PlaceholderContext {
        PlaceholderContext {
            vars: self.vars,
            time: self.time,
            retriever: self.retriever,
            ns_resolver: self.ns_resolver,
        }
    }
}

impl PlaceholderContext {
    pub fn builder() -> PlaceholderContextBuilder {
        PlaceholderContextBuilder {
            vars: HashMap::new(),
            time: Arc::new(SystemTime),
            retriever: None,
            ns_resolver: Arc::new(DiaryNamespaceResolver),
        }
    }
}

// ---------------------------------------------------------------------------
// Renderer
// ---------------------------------------------------------------------------

/// Matches every placeholder form in a single pass.
///
/// Alternatives (leftmost wins):
/// 1. `<<body>>`      — all-mode marker (body = namespace or agent short-name)
/// 2. `[[body]]`      — tag-memo placeholder (resolved by the RAG wing later)
/// 3. `{{body}}`      — variable / Tar* built-in / agent-diary
///
/// Bodies cannot themselves contain the closing delimiter; we use
/// non-greedy matches so adjacent placeholders don't fuse.
static PLACEHOLDER_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"<<([^<>]*?)>>|\[\[([^\[\]]*?)\]\]|\{\{([^{}]*?)\}\}")
        .expect("placeholder regex is a compile-time constant")
});

/// One-pass renderer for the S9 placeholder dialect.
pub struct Renderer {
    ctx: PlaceholderContext,
}

impl Renderer {
    /// Build a renderer bound to the supplied context.
    pub fn new(ctx: PlaceholderContext) -> Self {
        Self { ctx }
    }

    /// Render `input`, replacing every recognised placeholder. Unknown
    /// forms render verbatim (the token stays in the output); resolver
    /// errors propagate via [`RenderError`].
    pub fn render(&self, input: &str) -> Result<String, RenderError> {
        let mut out = String::with_capacity(input.len());
        let mut cursor = 0_usize;

        for caps in PLACEHOLDER_RE.captures_iter(input) {
            let m = caps
                .get(0)
                .expect("captures_iter always yields a full match");
            out.push_str(&input[cursor..m.start()]);
            cursor = m.end();

            let raw = m.as_str();
            let replacement = if let Some(body) = caps.get(1) {
                self.render_all_mode(body.as_str(), raw)?
            } else if caps.get(2).is_some() {
                // `[[…::TagMemo…]]` placeholders are resolved by the RAG
                // wing at retrieval time (S10). For S9 we preserve the
                // marker verbatim so downstream layers can spot + expand it.
                raw.to_string()
            } else if let Some(body) = caps.get(3) {
                self.render_curly(body.as_str(), raw)?
            } else {
                raw.to_string()
            };
            out.push_str(&replacement);
        }
        out.push_str(&input[cursor..]);
        Ok(out)
    }

    // ---- individual forms -------------------------------------------------

    fn render_curly(&self, body: &str, raw: &str) -> Result<String, RenderError> {
        let trimmed = body.trim();
        if trimmed.is_empty() {
            return Ok(raw.to_string()); // preserve `{{}}` / `{{  }}`.
        }

        // Built-in date/time tokens. `TarToday`, `TarNow`, `TarIso`.
        if let Some(v) = self.render_tar_builtin(trimmed) {
            return Ok(v);
        }

        // `Var:key` form — explicit variable lookup.
        if let Some(key) = trimmed.strip_prefix("Var:") {
            return Ok(match self.ctx.vars.get(key.trim()) {
                Some(v) => v.clone(),
                None => raw.to_string(), // missing var → verbatim.
            });
        }

        // `<Agent>日记本` — diary RAG retrieval. The 日记本 suffix is
        // UTF-8 so we match on the literal three-character sequence.
        if let Some(agent) = trimmed.strip_suffix("日记本") {
            let agent = agent.trim();
            if agent.is_empty() {
                return Ok(raw.to_string());
            }
            let ns = self.ctx.ns_resolver.namespace_for_agent(agent);
            return match &self.ctx.retriever {
                Some(r) => r.retrieve_namespace(&ns),
                None => Ok(raw.to_string()), // no retriever → verbatim.
            };
        }

        // Unknown `{{…}}` → verbatim (survives into prompt for humans).
        Ok(raw.to_string())
    }

    fn render_all_mode(&self, body: &str, raw: &str) -> Result<String, RenderError> {
        let trimmed = body.trim();
        if trimmed.is_empty() {
            return Ok(raw.to_string());
        }
        // `<<x>>` where `x` is either a namespace literal (e.g. "general")
        // or an agent short-name we map through the resolver. Heuristic:
        // bodies containing a colon are treated as literal namespaces,
        // everything else is resolved as an agent name.
        let namespace = if trimmed.contains(':') {
            trimmed.to_string()
        } else {
            self.ctx.ns_resolver.namespace_for_agent(trimmed)
        };
        match &self.ctx.retriever {
            Some(r) => r.dump_namespace(&namespace),
            None => Ok(raw.to_string()),
        }
    }

    fn render_tar_builtin(&self, body: &str) -> Option<String> {
        let now = self.ctx.time.now();
        match body {
            "TarToday" => {
                let fmt = format_description!("[year]-[month]-[day]");
                now.format(fmt).ok()
            }
            "TarNow" => {
                let fmt = format_description!("[hour]:[minute]");
                now.format(fmt).ok()
            }
            "TarIso" => now.format(&Rfc3339).ok(),
            _ => None,
        }
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    fn fixed_time() -> Arc<FixedTime> {
        // 2026-04-21T09:30:00Z — matches the current-date hint so diffs stay readable.
        let ts = OffsetDateTime::parse("2026-04-21T09:30:00Z", &Rfc3339).unwrap();
        Arc::new(FixedTime(ts))
    }

    fn basic_ctx() -> PlaceholderContext {
        PlaceholderContext::builder()
            .var("agent_name", "Aemeath")
            .var("theme", "rust")
            .time(fixed_time())
            .build()
    }

    // ---- Var: form --------------------------------------------------------

    #[test]
    fn var_hit_replaces_token() {
        let r = Renderer::new(basic_ctx());
        let out = r.render("hello {{Var:agent_name}}").unwrap();
        assert_eq!(out, "hello Aemeath");
    }

    #[test]
    fn var_miss_falls_back_to_verbatim() {
        let r = Renderer::new(basic_ctx());
        let out = r.render("who is {{Var:unknown}}?").unwrap();
        assert_eq!(out, "who is {{Var:unknown}}?");
    }

    #[test]
    fn var_whitespace_inside_key_tolerated() {
        let r = Renderer::new(basic_ctx());
        let out = r.render("x={{Var: theme }}").unwrap();
        assert_eq!(out, "x=rust");
    }

    // ---- Tar* built-ins ---------------------------------------------------

    #[test]
    fn tar_today_renders_yyyy_mm_dd() {
        let r = Renderer::new(basic_ctx());
        let out = r.render("d={{TarToday}}").unwrap();
        assert_eq!(out, "d=2026-04-21");
    }

    #[test]
    fn tar_now_renders_hh_mm() {
        let r = Renderer::new(basic_ctx());
        let out = r.render("t={{TarNow}}").unwrap();
        assert_eq!(out, "t=09:30");
    }

    #[test]
    fn tar_iso_renders_rfc3339() {
        let r = Renderer::new(basic_ctx());
        let out = r.render("s={{TarIso}}").unwrap();
        assert!(out.starts_with("s=2026-04-21T09:30:00"), "got {out}");
    }

    #[test]
    fn tar_unknown_is_verbatim() {
        // `TarNotARealThing` must not match any built-in; stays in text.
        let r = Renderer::new(basic_ctx());
        let out = r.render("{{TarNotARealThing}}").unwrap();
        assert_eq!(out, "{{TarNotARealThing}}");
    }

    // ---- Diary RAG form ---------------------------------------------------

    struct MockRetriever {
        calls: Arc<AtomicUsize>,
        last_ns: std::sync::Mutex<String>,
    }

    impl MockRetriever {
        fn new() -> Arc<Self> {
            Arc::new(Self {
                calls: Arc::new(AtomicUsize::new(0)),
                last_ns: std::sync::Mutex::new(String::new()),
            })
        }
    }

    impl RagRetriever for MockRetriever {
        fn retrieve_namespace(&self, ns: &str) -> Result<String, RenderError> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            *self.last_ns.lock().unwrap() = ns.to_string();
            Ok(format!("<<diary of {ns}>>"))
        }

        fn dump_namespace(&self, ns: &str) -> Result<String, RenderError> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            *self.last_ns.lock().unwrap() = ns.to_string();
            Ok(format!("ALL({ns})"))
        }
    }

    fn ctx_with_retriever(retriever: Arc<MockRetriever>) -> PlaceholderContext {
        PlaceholderContext::builder()
            .var("agent_name", "Aemeath")
            .time(fixed_time())
            .retriever(retriever)
            .build()
    }

    #[test]
    fn diary_form_asks_retriever_with_mapped_namespace() {
        let ret = MockRetriever::new();
        let r = Renderer::new(ctx_with_retriever(ret.clone()));
        let out = r.render("ctx: {{Aemeath日记本}}").unwrap();
        // Diary result is spliced verbatim; nested `<<…>>` is NOT re-expanded
        // (one-pass contract).
        assert_eq!(out, "ctx: <<diary of diary:Aemeath>>");
        assert_eq!(ret.calls.load(Ordering::SeqCst), 1);
        assert_eq!(*ret.last_ns.lock().unwrap(), "diary:Aemeath");
    }

    #[test]
    fn diary_without_retriever_renders_verbatim() {
        // Context has no retriever — the `{{Foo日记本}}` form must not
        // break the prompt; it survives as-is.
        let r = Renderer::new(basic_ctx());
        let out = r.render("ctx: {{Aemeath日记本}}").unwrap();
        assert_eq!(out, "ctx: {{Aemeath日记本}}");
    }

    #[test]
    fn diary_retriever_error_propagates() {
        struct FailingRetriever;
        impl RagRetriever for FailingRetriever {
            fn retrieve_namespace(&self, ns: &str) -> Result<String, RenderError> {
                Err(RenderError::RagRetrieval {
                    namespace: ns.to_string(),
                    message: "backend down".into(),
                })
            }
        }
        let ctx = PlaceholderContext::builder()
            .time(fixed_time())
            .retriever(Arc::new(FailingRetriever))
            .build();
        let r = Renderer::new(ctx);
        let err = r.render("x {{Aemeath日记本}} y").unwrap_err();
        match err {
            RenderError::RagRetrieval { namespace, message } => {
                assert_eq!(namespace, "diary:Aemeath");
                assert!(message.contains("backend down"));
            }
            other => panic!("expected RagRetrieval, got {other:?}"),
        }
    }

    // ---- [[x::TagMemo0.55]] — preserved verbatim -------------------------

    #[test]
    fn tag_memo_marker_kept_verbatim_for_rag_wing() {
        let r = Renderer::new(basic_ctx());
        let out = r.render("inject [[now::TagMemo0.55]] here").unwrap();
        assert_eq!(out, "inject [[now::TagMemo0.55]] here");
    }

    // ---- <<x>> all-mode ---------------------------------------------------

    #[test]
    fn all_mode_with_agent_name_uses_diary_namespace() {
        let ret = MockRetriever::new();
        let r = Renderer::new(ctx_with_retriever(ret.clone()));
        let out = r.render("dump: <<Aemeath>>").unwrap();
        assert_eq!(out, "dump: ALL(diary:Aemeath)");
    }

    #[test]
    fn all_mode_with_literal_namespace_bypasses_resolver() {
        let ret = MockRetriever::new();
        let r = Renderer::new(ctx_with_retriever(ret.clone()));
        let out = r.render("dump: <<papers:q1>>").unwrap();
        // Body with `:` → treated as literal namespace, no resolver pass.
        assert_eq!(out, "dump: ALL(papers:q1)");
    }

    #[test]
    fn all_mode_without_retriever_renders_verbatim() {
        let r = Renderer::new(basic_ctx());
        let out = r.render("dump: <<Aemeath>>").unwrap();
        assert_eq!(out, "dump: <<Aemeath>>");
    }

    // ---- Combined / edge cases -------------------------------------------

    #[test]
    fn one_pass_no_recursion_on_var_values() {
        let ctx = PlaceholderContext::builder()
            .var("leak", "{{TarToday}}")
            .time(fixed_time())
            .build();
        let r = Renderer::new(ctx);
        let out = r.render("{{Var:leak}}").unwrap();
        // The rendered `{{TarToday}}` stays intact (no double-expansion).
        assert_eq!(out, "{{TarToday}}");
    }

    #[test]
    fn multiple_placeholders_all_replaced_in_single_pass() {
        let ret = MockRetriever::new();
        let r = Renderer::new(ctx_with_retriever(ret.clone()));
        let out = r
            .render("{{Var:agent_name}} @ {{TarToday}} | {{Aemeath日记本}}")
            .unwrap();
        assert_eq!(out, "Aemeath @ 2026-04-21 | <<diary of diary:Aemeath>>");
    }

    #[test]
    fn plain_text_unchanged() {
        let r = Renderer::new(basic_ctx());
        assert_eq!(r.render("hello world").unwrap(), "hello world");
        assert_eq!(r.render("").unwrap(), "");
    }

    #[test]
    fn empty_placeholder_bodies_preserved() {
        let r = Renderer::new(basic_ctx());
        assert_eq!(
            r.render("a {{}} b <<>> c [[]]").unwrap(),
            "a {{}} b <<>> c [[]]"
        );
    }

    #[test]
    fn render_error_converts_to_corlinman_error() {
        let err = RenderError::Parse("boom".into());
        let corl: CorlinmanError = err.into();
        match corl {
            CorlinmanError::Parse { what, message } => {
                assert_eq!(what, "placeholder");
                assert_eq!(message, "boom");
            }
            other => panic!("expected Parse, got {other:?}"),
        }
    }
}
