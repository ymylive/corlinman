//! gRPC wrapper over [`corlinman_core::placeholder::PlaceholderEngine`].
//!
//! Direction: Python client → Rust server (reverse of agent.proto). The
//! Python `context_assembler` dials this service on the UDS path in
//! `$CORLINMAN_UDS_PATH` (default `/tmp/corlinman.sock`) and calls
//! `Render` for every template it wants expanded before a provider call.
//!
//! Scope for B1-BE3 (BRIDGE): this is the plumbing only. The engine
//! registered here has no resolvers yet — real namespace resolvers
//! (skills, variables, agent cards, …) land in Batch 2 (B2-BE4). Tokens
//! with a namespace that has no resolver round-trip back unchanged and
//! are surfaced in [`RenderResponse::unresolved_keys`] for observability.
//!
//! Error mapping preserves the enum shape from
//! [`corlinman_core::placeholder::PlaceholderError`]:
//!
//! | engine error            | `error` string          |
//! |-------------------------|-------------------------|
//! | `Cycle(k)`              | `"cycle:<k>"`           |
//! | `DepthExceeded {..}`    | `"depth_exceeded"`      |
//! | `Resolver {..}`         | `"resolver:<msg>"`      |

use std::path::{Path, PathBuf};
use std::sync::Arc;

use corlinman_core::placeholder::{DynamicResolver, PlaceholderCtx, PlaceholderEngine};
use corlinman_core::CorlinmanError;
use corlinman_memory_host::MemoryHost;
use corlinman_proto::v1::{
    placeholder_server::{Placeholder, PlaceholderServer},
    RenderRequest, RenderResponse,
};
use once_cell::sync::Lazy;
use regex::Regex;
use tokio::net::UnixListener;
use tokio_stream::wrappers::UnixListenerStream;
use tonic::{transport::Server, Request, Response, Status};

/// Default UDS path the Rust side binds for Python→Rust traffic. Kept
/// separate from `/tmp/corlinman-py.sock` so the two sides can be
/// restarted independently without stepping on each other's socket file.
pub const DEFAULT_RUST_SOCKET: &str = "/tmp/corlinman.sock";

/// Env var the Python client honours (and the server respects when set).
/// Matches the contract documented on `PlaceholderClient`.
pub const ENV_RUST_SOCKET: &str = "CORLINMAN_UDS_PATH";

/// Same regex as the engine, used here to harvest unresolved token keys
/// from the post-render output. Kept local because `corlinman-core` does
/// not publicly export its compiled regex.
static TOKEN_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"\{\{([^{}]*?)\}\}").expect("placeholder regex is a compile-time constant")
});

/// gRPC service shell. Wraps a shared [`PlaceholderEngine`] so multiple
/// concurrent `Render` RPCs share the same resolver registry.
pub struct PlaceholderService {
    engine: Arc<PlaceholderEngine>,
}

impl PlaceholderService {
    /// Build a service around an already-populated engine. Production boot
    /// constructs the engine once (with every resolver registered) and
    /// threads the same `Arc` here.
    pub fn new(engine: Arc<PlaceholderEngine>) -> Self {
        Self { engine }
    }

    /// Convenience for tests + the B1-BE3 bridge boot: stand up a service
    /// backed by a brand-new empty engine (no resolvers). Every token
    /// whose namespace is not a registered static key will round-trip
    /// back through `unresolved_keys`.
    pub fn with_empty_engine() -> Self {
        Self::new(Arc::new(PlaceholderEngine::new()))
    }
}

/// Build the production placeholder engine.
///
/// The gateway owns the resolver registrations because both namespaces need
/// gateway-local state: `episodes` reads per-tenant SQLite files under the
/// data dir, while `memory` queries the same [`MemoryHost`] surfaced over MCP
/// and `/memory/*`.
pub fn build_engine(
    data_dir: impl Into<PathBuf>,
    memory_host: Option<Arc<dyn MemoryHost>>,
) -> PlaceholderEngine {
    build_engine_with_episodes(data_dir, memory_host, None)
}

fn build_engine_with_episodes(
    data_dir: impl Into<PathBuf>,
    memory_host: Option<Arc<dyn MemoryHost>>,
    episodes: Option<Arc<dyn DynamicResolver>>,
) -> PlaceholderEngine {
    let mut engine = PlaceholderEngine::new();
    let episodes_resolver = episodes
        .unwrap_or_else(|| crate::placeholder::EpisodesResolver::new(data_dir.into()).into_arc());
    engine.register_namespace("episodes", episodes_resolver);
    if let Some(host) = memory_host {
        engine.register_namespace(
            "memory",
            crate::placeholder::MemoryResolver::new(host).into_arc(),
        );
    }
    engine
}

#[tonic::async_trait]
impl Placeholder for PlaceholderService {
    async fn render(
        &self,
        request: Request<RenderRequest>,
    ) -> Result<Response<RenderResponse>, Status> {
        let req = request.into_inner();

        // Re-hydrate the engine context. The proto message allows empty
        // `model_name` to mean "none"; the Rust struct encodes that as
        // `Option::None`, so round-trip the sentinel.
        let mut ctx = PlaceholderCtx::new(
            req.ctx
                .as_ref()
                .map(|c| c.session_key.clone())
                .unwrap_or_default(),
        );
        if let Some(c) = req.ctx.as_ref() {
            if !c.model_name.is_empty() {
                ctx.model_name = Some(c.model_name.clone());
            }
            ctx.metadata = c.metadata.clone();
        }

        // Honour per-call max_depth override. 0 = use engine default
        // (matches the proto docstring).
        let engine = if req.max_depth == 0 {
            self.engine.clone()
        } else {
            Arc::new(self.engine.clone_with_max_depth(req.max_depth))
        };

        match engine.render(&req.template, &ctx).await {
            Ok(rendered) => {
                let unresolved = collect_unresolved(&rendered);
                Ok(Response::new(RenderResponse {
                    rendered,
                    unresolved_keys: unresolved,
                    error: String::new(),
                }))
            }
            Err(err) => Ok(Response::new(RenderResponse {
                rendered: String::new(),
                unresolved_keys: Vec::new(),
                error: encode_error(&err),
            })),
        }
    }
}

/// Encode a [`CorlinmanError::Parse`] that originated from a
/// `PlaceholderError` back into the stable wire form.
///
/// The engine funnels every variant through `CorlinmanError::Parse { what:
/// "placeholder", message }`, and the `Display` impl of `PlaceholderError`
/// produces deterministic prefixes ("placeholder cycle detected at key
/// '...'", "placeholder recursion depth ... exceeded", "resolver for '...'
/// failed: ..."). We match on those prefixes rather than coupling to the
/// `Display` string verbatim — the wire format is an explicit contract.
fn encode_error(err: &CorlinmanError) -> String {
    // The engine returns `CorlinmanError::Parse { what: "placeholder", message }`
    // whose Display is `"parse error (placeholder): <inner>"`. Strip that
    // wrapper so we're matching on `PlaceholderError::Display` directly.
    let raw = err.to_string();
    let inner = raw
        .strip_prefix("parse error (placeholder): ")
        .unwrap_or(&raw);

    // `PlaceholderError::Cycle` → `"placeholder cycle detected at key '<k>'"`
    if let Some(rest) = inner.strip_prefix("placeholder cycle detected at key '") {
        if let Some(key) = rest.strip_suffix('\'') {
            return format!("cycle:{key}");
        }
    }
    if inner.starts_with("placeholder recursion depth ") {
        return "depth_exceeded".into();
    }
    if let Some(rest) = inner.strip_prefix("resolver for '") {
        // rest = "<ns>' failed: <inner>"
        if let Some((_, tail)) = rest.split_once("' failed: ") {
            return format!("resolver:{tail}");
        }
    }
    // Unknown shape — surface verbatim so the Python side can still log
    // something actionable.
    format!("resolver:{inner}")
}

/// Harvest still-literal `{{…}}` tokens from a rendered template. The
/// engine preserves unknown tokens verbatim, so a post-render scan is
/// the cheapest way to surface them without modifying the engine.
fn collect_unresolved(rendered: &str) -> Vec<String> {
    if !rendered.contains("{{") {
        return Vec::new();
    }
    let mut out: Vec<String> = Vec::new();
    for cap in TOKEN_RE.captures_iter(rendered) {
        if let Some(m) = cap.get(1) {
            let body = m.as_str().trim();
            if body.is_empty() {
                continue; // `{{}}` / `{{ }}` are intentionally preserved
            }
            if !out.iter().any(|x| x == body) {
                out.push(body.to_string());
            }
        }
    }
    out
}

/// Bind a tonic `Server` onto `socket_path` and serve the `Placeholder`
/// service until `shutdown` resolves. Removes the socket file on exit so
/// subsequent boots can rebind cleanly.
///
/// Non-fatal: returns `Err` so `main.rs` can log-and-continue if binding
/// fails (e.g. permission denied on a read-only fs); the axum side keeps
/// serving `/v1/*` regardless.
pub async fn serve<F>(
    socket_path: impl AsRef<Path>,
    service: PlaceholderService,
    shutdown: F,
) -> anyhow::Result<()>
where
    F: std::future::Future<Output = ()> + Send + 'static,
{
    let path: PathBuf = socket_path.as_ref().to_path_buf();
    // Best-effort cleanup of a stale socket — a previous crash may have
    // left the file behind.
    let _ = tokio::fs::remove_file(&path).await;
    if let Some(parent) = path.parent() {
        tokio::fs::create_dir_all(parent).await.ok();
    }
    let listener = UnixListener::bind(&path)?;
    tracing::info!(socket = %path.display(), "placeholder gRPC bound");

    let incoming = UnixListenerStream::new(listener);
    let result = Server::builder()
        .add_service(PlaceholderServer::new(service))
        .serve_with_incoming_shutdown(incoming, shutdown)
        .await;

    let _ = tokio::fs::remove_file(&path).await;
    result.map_err(|e| anyhow::anyhow!("placeholder gRPC server exited: {e}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use corlinman_core::placeholder::PlaceholderEngine;
    use corlinman_proto::v1::PlaceholderCtx as PbCtx;

    fn service_with(engine: PlaceholderEngine) -> PlaceholderService {
        PlaceholderService::new(Arc::new(engine))
    }

    fn request(template: &str) -> Request<RenderRequest> {
        Request::new(RenderRequest {
            template: template.into(),
            ctx: Some(PbCtx {
                session_key: "s1".into(),
                model_name: "test-model".into(),
                metadata: Default::default(),
            }),
            max_depth: 0,
        })
    }

    fn request_with_max_depth(template: &str, max_depth: u32) -> Request<RenderRequest> {
        Request::new(RenderRequest {
            template: template.into(),
            ctx: Some(PbCtx {
                session_key: "s1".into(),
                model_name: "test-model".into(),
                metadata: Default::default(),
            }),
            max_depth,
        })
    }

    #[tokio::test]
    async fn static_hit_is_rendered() {
        let eng = PlaceholderEngine::new().with_static("date.today", "2026-04-22");
        let svc = service_with(eng);
        let resp = svc
            .render(request("today is {{date.today}}"))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(resp.rendered, "today is 2026-04-22");
        assert!(resp.unresolved_keys.is_empty());
        assert!(resp.error.is_empty());
    }

    #[tokio::test]
    async fn unresolved_token_round_trips() {
        let svc = PlaceholderService::with_empty_engine();
        let resp = svc
            .render(request("hi {{var.user_name}} — {{session.id}}"))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(resp.rendered, "hi {{var.user_name}} — {{session.id}}");
        assert_eq!(
            resp.unresolved_keys,
            vec!["var.user_name".to_string(), "session.id".to_string()]
        );
        assert!(resp.error.is_empty());
    }

    #[tokio::test]
    async fn empty_tokens_are_not_listed_as_unresolved() {
        let svc = PlaceholderService::with_empty_engine();
        let resp = svc
            .render(request("a {{}} b {{ }} c"))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(resp.rendered, "a {{}} b {{ }} c");
        assert!(resp.unresolved_keys.is_empty());
    }

    #[tokio::test]
    async fn ctx_fields_round_trip_into_engine_context() {
        // Register a tiny dynamic resolver that echoes back a metadata value
        // so we can prove the Python-provided ctx reaches the engine.
        use async_trait::async_trait;
        use corlinman_core::placeholder::{DynamicResolver, PlaceholderError};

        struct Echo;
        #[async_trait]
        impl DynamicResolver for Echo {
            async fn resolve(
                &self,
                key: &str,
                ctx: &corlinman_core::placeholder::PlaceholderCtx,
            ) -> Result<String, PlaceholderError> {
                Ok(format!(
                    "{key}|{}|{}|{}",
                    ctx.session_key,
                    ctx.model_name.clone().unwrap_or_default(),
                    ctx.metadata.get("trace").cloned().unwrap_or_default(),
                ))
            }
        }

        let eng = PlaceholderEngine::new().with_dynamic("echo", Arc::new(Echo));
        let svc = service_with(eng);
        let mut meta = std::collections::HashMap::new();
        meta.insert("trace".to_string(), "t-42".to_string());
        let req = Request::new(RenderRequest {
            template: "{{echo.x}}".into(),
            ctx: Some(PbCtx {
                session_key: "sess-a".into(),
                model_name: "gpt".into(),
                metadata: meta,
            }),
            max_depth: 0,
        });
        let resp = svc.render(req).await.unwrap().into_inner();
        assert_eq!(resp.rendered, "x|sess-a|gpt|t-42");
        assert!(resp.error.is_empty());
    }

    #[tokio::test]
    async fn max_depth_override_preserves_registered_resolvers() {
        use async_trait::async_trait;
        use corlinman_core::placeholder::{DynamicResolver, PlaceholderError};

        struct Echo;
        #[async_trait]
        impl DynamicResolver for Echo {
            async fn resolve(
                &self,
                key: &str,
                _ctx: &corlinman_core::placeholder::PlaceholderCtx,
            ) -> Result<String, PlaceholderError> {
                Ok(format!("resolved:{key}"))
            }
        }

        let eng = PlaceholderEngine::new().with_dynamic("echo", Arc::new(Echo));
        let svc = service_with(eng);
        let resp = svc
            .render(request_with_max_depth("{{echo.memory}}", 2))
            .await
            .unwrap()
            .into_inner();

        assert_eq!(resp.rendered, "resolved:memory");
        assert!(resp.unresolved_keys.is_empty());
        assert!(resp.error.is_empty());
    }

    #[tokio::test]
    async fn production_engine_registers_episodes_and_memory_resolvers() {
        use async_trait::async_trait;
        use corlinman_core::placeholder::{DynamicResolver, PlaceholderError};
        use corlinman_memory_host::{MemoryDoc, MemoryHit, MemoryHost, MemoryQuery};

        struct StubEpisodes;
        #[async_trait]
        impl DynamicResolver for StubEpisodes {
            async fn resolve(
                &self,
                key: &str,
                _ctx: &corlinman_core::placeholder::PlaceholderCtx,
            ) -> Result<String, PlaceholderError> {
                Ok(format!("episodes:{key}"))
            }
        }

        struct StubMemory;
        #[async_trait]
        impl MemoryHost for StubMemory {
            fn name(&self) -> &str {
                "stub-memory"
            }

            async fn query(&self, req: MemoryQuery) -> anyhow::Result<Vec<MemoryHit>> {
                assert_eq!(req.text, "backend");
                assert_eq!(req.namespace.as_deref(), Some("agent-brain"));
                Ok(vec![MemoryHit {
                    id: "m1".into(),
                    content: "memory hit".into(),
                    score: 1.0,
                    source: "stub-memory".into(),
                    metadata: serde_json::Value::Null,
                }])
            }

            async fn upsert(&self, _doc: MemoryDoc) -> anyhow::Result<String> {
                anyhow::bail!("not used")
            }

            async fn delete(&self, _id: &str) -> anyhow::Result<()> {
                anyhow::bail!("not used")
            }
        }

        let engine = build_engine_with_episodes(
            PathBuf::from("."),
            Some(Arc::new(StubMemory)),
            Some(Arc::new(StubEpisodes)),
        );

        let out = engine
            .render(
                "{{episodes.recent}}\n{{memory.backend}}",
                &corlinman_core::placeholder::PlaceholderCtx::new("s"),
            )
            .await
            .unwrap();

        assert!(out.contains("episodes:recent"));
        assert!(out.contains("memory hit"));
        assert!(out.contains("stub-memory:m1"));
    }

    #[tokio::test]
    async fn cycle_error_is_encoded_with_key() {
        use async_trait::async_trait;
        use corlinman_core::placeholder::{DynamicResolver, PlaceholderError};

        struct SelfRef;
        #[async_trait]
        impl DynamicResolver for SelfRef {
            async fn resolve(
                &self,
                key: &str,
                _ctx: &corlinman_core::placeholder::PlaceholderCtx,
            ) -> Result<String, PlaceholderError> {
                // Every lookup returns a placeholder pointing to the same key,
                // triggering the engine's cycle guard on re-expansion.
                Ok(format!("{{{{loop.{key}}}}}"))
            }
        }

        let eng = PlaceholderEngine::new().with_dynamic("loop", Arc::new(SelfRef));
        let svc = service_with(eng);
        let resp = svc
            .render(request("{{loop.x}}"))
            .await
            .unwrap()
            .into_inner();
        assert!(resp.rendered.is_empty());
        assert_eq!(resp.error, "cycle:loop.x");
    }

    #[tokio::test]
    async fn resolver_error_is_encoded() {
        use async_trait::async_trait;
        use corlinman_core::placeholder::{DynamicResolver, PlaceholderError};

        struct Boom;
        #[async_trait]
        impl DynamicResolver for Boom {
            async fn resolve(
                &self,
                _key: &str,
                _ctx: &corlinman_core::placeholder::PlaceholderCtx,
            ) -> Result<String, PlaceholderError> {
                Err(PlaceholderError::Resolver {
                    namespace: "boom".into(),
                    message: "kaboom".into(),
                })
            }
        }

        let eng = PlaceholderEngine::new().with_dynamic("boom", Arc::new(Boom));
        let svc = service_with(eng);
        let resp = svc
            .render(request("{{boom.x}}"))
            .await
            .unwrap()
            .into_inner();
        assert!(resp.rendered.is_empty());
        assert_eq!(resp.error, "resolver:kaboom");
    }
}
