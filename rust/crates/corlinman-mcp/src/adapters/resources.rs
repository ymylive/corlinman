//! `resources` capability adapter — read-only surface over memory
//! hosts + skill bodies.
//!
//! ## URI schemes
//!
//! Iter 7 supports two of the three schemes pinned in the design:
//!
//! | URI scheme                              | Source              | List | Read |
//! |-----------------------------------------|---------------------|------|------|
//! | `corlinman://memory/<host>/<id>`        | `MemoryHost`        | enumerate via top-N empty-query, paginated | `MemoryHost::get(id)` |
//! | `corlinman://skill/<name>`              | `SkillRegistry`     | iter all  | `Skill.body_markdown` |
//!
//! The third design scheme (`corlinman://persona/<user_id>/snapshot`)
//! lives in the Python-side persona service today; no Rust trait
//! exists yet. Iter 7 leaves the scheme unwired and out-of-scope; the
//! adapter constructor accepts a `persona` placeholder pluggable behind
//! the [`PersonaSnapshotProvider`] trait so a later workstream can wire
//! it in additively without changing the call sites.
//!
//! ## Pagination
//!
//! `resources/list` uses a server-issued opaque cursor: a base64-free
//! `<offset>` decimal string ("0", "100", "200" …). The adapter fans
//! out across registered memory hosts + the skill registry in a stable
//! order (memory hosts alphabetical, then `skill`), accumulates the
//! union into one virtual list, and slices `[offset .. offset+page]`.
//! `next_cursor` is set when the slice is full; cleared on the last
//! page. Page size defaults to [`DEFAULT_PAGE_SIZE`] and is configurable
//! via [`ResourcesAdapter::with_page_size`].
//!
//! ## ACL
//!
//! `SessionContext::resources_allowed` is a list of URI-scheme prefixes
//! (`"memory"`, `"skill"`, `"persona"`, or `"*"`). Listing filters to
//! the schemes the token is allowed; reading rejects with -32602 when
//! the URI's scheme isn't allowed.

use std::collections::BTreeMap;
use std::sync::Arc;

use async_trait::async_trait;
use serde_json::{json, Value as JsonValue};

use corlinman_memory_host::MemoryHost;
use corlinman_skills::SkillRegistry;

use crate::adapters::{CapabilityAdapter, SessionContext};
use crate::error::McpError;
use crate::schema::resources::{
    ListParams, ListResult, ReadParams, ReadResult, Resource, ResourceContent,
};

/// MCP method-name constants.
pub const METHOD_LIST: &str = "resources/list";
pub const METHOD_READ: &str = "resources/read";

/// Default cursor page size (mirrors design table).
pub const DEFAULT_PAGE_SIZE: usize = 100;

/// Knob controlling how many memory entries to surface per host in
/// `resources/list`. Lower than `DEFAULT_PAGE_SIZE` because memory hosts
/// can balloon (100k+ chunks); operators can lift this once the design's
/// open question §3 (subscriptions) lands.
pub const DEFAULT_MEMORY_LIST_LIMIT: usize = 50;

/// Pluggable persona-snapshot reader. `PersonaStore` lives in the
/// Python tier today; the trait gives us a forward-compatible seam so
/// iter 9 / a future C1.5 can wire a Rust adapter without touching this
/// file. Iter 7 ships the no-op implementation and leaves the URI
/// scheme unadvertised.
#[async_trait]
pub trait PersonaSnapshotProvider: Send + Sync {
    /// List all known user_ids the token is allowed to read.
    async fn list_user_ids(&self) -> anyhow::Result<Vec<String>>;
    /// Fetch a single user's trait snapshot as canonical JSON.
    async fn read_snapshot(&self, user_id: &str) -> anyhow::Result<Option<JsonValue>>;
}

/// No-op persona provider. Surfaces an empty list and never finds a
/// snapshot. Default when the gateway hasn't wired a real provider.
pub struct NullPersonaProvider;

#[async_trait]
impl PersonaSnapshotProvider for NullPersonaProvider {
    async fn list_user_ids(&self) -> anyhow::Result<Vec<String>> {
        Ok(Vec::new())
    }
    async fn read_snapshot(&self, _user_id: &str) -> anyhow::Result<Option<JsonValue>> {
        Ok(None)
    }
}

/// Adapter that maps a set of memory hosts + the skill registry +
/// (optionally) a persona provider onto MCP's `resources/*` surface.
pub struct ResourcesAdapter {
    /// Memory hosts keyed by their `MemoryHost::name()`. The URI
    /// `corlinman://memory/<host>/<id>` routes by this key.
    memory_hosts: BTreeMap<String, Arc<dyn MemoryHost>>,
    /// Skill bodies under `corlinman://skill/<name>`.
    skills: Arc<SkillRegistry>,
    /// Persona snapshot source. `NullPersonaProvider` until a real
    /// store lands.
    persona: Arc<dyn PersonaSnapshotProvider>,
    /// Page size for `resources/list`.
    page_size: usize,
    /// Soft cap on memory hits returned per host during enumeration.
    memory_list_limit: usize,
}

impl ResourcesAdapter {
    pub fn new(
        memory_hosts: BTreeMap<String, Arc<dyn MemoryHost>>,
        skills: Arc<SkillRegistry>,
    ) -> Self {
        Self {
            memory_hosts,
            skills,
            persona: Arc::new(NullPersonaProvider),
            page_size: DEFAULT_PAGE_SIZE,
            memory_list_limit: DEFAULT_MEMORY_LIST_LIMIT,
        }
    }

    pub fn with_persona(mut self, persona: Arc<dyn PersonaSnapshotProvider>) -> Self {
        self.persona = persona;
        self
    }

    pub fn with_page_size(mut self, n: usize) -> Self {
        self.page_size = n.max(1);
        self
    }

    pub fn with_memory_list_limit(mut self, n: usize) -> Self {
        self.memory_list_limit = n.max(1);
        self
    }

    /// Enumerate all visible resources, then page-slice. Iter 7 takes
    /// the "fan-out, accumulate, slice" approach for clarity; if a
    /// future iteration needs to scale to millions of resources, swap
    /// in per-host streaming with a typed cursor (e.g.
    /// `<scheme>:<host>:<offset>`).
    pub async fn list_resources(
        &self,
        params: ListParams,
        ctx: &SessionContext,
    ) -> Result<ListResult, McpError> {
        let mut all: Vec<Resource> = Vec::new();

        // 1) Memory hosts (alphabetical by name from BTreeMap).
        if ctx.allows_resource_scheme("memory") {
            for (name, host) in &self.memory_hosts {
                // We use a top-N "empty-query" enumeration. The skeleton
                // `LocalSqliteHost::query` returns zero rows for empty
                // text — that's fine; the production store overrides
                // this branch anyway. We additionally peek any small
                // set of hits via a permissive query when the store's
                // contents are visible. The contract here:
                //   "Listing reflects what the host volunteers."
                // Iter 7 falls back on a wildcard-ish query to surface
                // *something*; iter 8+ should add a `MemoryHost::list`
                // method for true enumeration.
                let probe = corlinman_memory_host::MemoryQuery {
                    text: "*".to_string(),
                    top_k: self.memory_list_limit,
                    filters: vec![],
                    namespace: None,
                };
                match host.query(probe).await {
                    Ok(hits) => {
                        for hit in hits {
                            all.push(Resource {
                                uri: format!("corlinman://memory/{name}/{}", hit.id),
                                name: format!("memory:{name}:{}", hit.id),
                                description: short_preview(&hit.content),
                                mime_type: Some("text/plain".to_string()),
                            });
                        }
                    }
                    Err(_e) => {
                        // Non-fatal: a degraded host shouldn't blow up
                        // the list. Iter 9 wires a tracing event here.
                    }
                }
            }
        }

        // 2) Skills.
        if ctx.allows_resource_scheme("skill") {
            for skill in self.skills.iter() {
                all.push(Resource {
                    uri: format!("corlinman://skill/{}", skill.name),
                    name: format!("skill:{}", skill.name),
                    description: if skill.description.is_empty() {
                        None
                    } else {
                        Some(skill.description.clone())
                    },
                    mime_type: Some("text/markdown".to_string()),
                });
            }
        }

        // 3) Persona snapshots.
        if ctx.allows_resource_scheme("persona") {
            match self.persona.list_user_ids().await {
                Ok(ids) => {
                    for uid in ids {
                        all.push(Resource {
                            uri: format!("corlinman://persona/{uid}/snapshot"),
                            name: format!("persona:{uid}"),
                            description: Some(format!("trait snapshot for {uid}")),
                            mime_type: Some("application/json".to_string()),
                        });
                    }
                }
                Err(_) => {
                    // Same fail-soft posture as memory.
                }
            }
        }

        all.sort_by(|a, b| a.uri.cmp(&b.uri));

        // Cursor parse: reject malformed cursors with -32602 so a
        // tampered client gets a clear signal rather than silently
        // restarting at zero.
        let offset: usize = match params.cursor.as_deref() {
            None | Some("") => 0,
            Some(s) => s.parse().map_err(|_| {
                McpError::invalid_params_with(
                    format!("invalid resources cursor '{s}'"),
                    json!({"cursor": s}),
                )
            })?,
        };

        let end = offset.saturating_add(self.page_size).min(all.len());
        let page = if offset >= all.len() {
            Vec::new()
        } else {
            all[offset..end].to_vec()
        };
        let next_cursor = if end < all.len() {
            Some(end.to_string())
        } else {
            None
        };
        Ok(ListResult {
            resources: page,
            next_cursor,
        })
    }

    /// Read one resource by URI. Returns -32602 for unknown URIs and
    /// for ACL denials (with a distinct message).
    pub async fn read_resource(
        &self,
        params: ReadParams,
        ctx: &SessionContext,
    ) -> Result<ReadResult, McpError> {
        let parsed = ParsedUri::parse(&params.uri).ok_or_else(|| {
            McpError::invalid_params_with(
                format!("not a corlinman resource URI: '{}'", params.uri),
                json!({"uri": params.uri}),
            )
        })?;

        // ACL by scheme prefix.
        if !ctx.allows_resource_scheme(parsed.scheme()) {
            return Err(McpError::invalid_params_with(
                format!(
                    "resource scheme '{}' not allowed by this token",
                    parsed.scheme()
                ),
                json!({"uri": params.uri}),
            ));
        }

        match parsed {
            ParsedUri::Memory { host, id } => {
                let host_arc = self.memory_hosts.get(host).ok_or_else(|| {
                    McpError::invalid_params_with(
                        format!("unknown memory host '{host}'"),
                        json!({"uri": params.uri}),
                    )
                })?;
                let hit = host_arc
                    .get(id)
                    .await
                    .map_err(|e| McpError::Internal(format!("memory host get: {e}")))?;
                let hit = hit.ok_or_else(|| {
                    McpError::invalid_params_with(
                        format!("unknown memory id '{id}'"),
                        json!({"uri": params.uri}),
                    )
                })?;
                Ok(ReadResult {
                    contents: vec![ResourceContent::Text {
                        uri: params.uri.clone(),
                        mime_type: Some("text/plain".to_string()),
                        text: hit.content,
                    }],
                })
            }
            ParsedUri::Skill { name } => {
                let skill = self.skills.get(name).ok_or_else(|| {
                    McpError::invalid_params_with(
                        format!("unknown skill '{name}'"),
                        json!({"uri": params.uri}),
                    )
                })?;
                Ok(ReadResult {
                    contents: vec![ResourceContent::Text {
                        uri: params.uri.clone(),
                        mime_type: Some("text/markdown".to_string()),
                        text: skill.body_markdown.clone(),
                    }],
                })
            }
            ParsedUri::Persona { user_id } => {
                let snap = self
                    .persona
                    .read_snapshot(user_id)
                    .await
                    .map_err(|e| McpError::Internal(format!("persona snapshot: {e}")))?;
                let snap = snap.ok_or_else(|| {
                    McpError::invalid_params_with(
                        format!("unknown persona user '{user_id}'"),
                        json!({"uri": params.uri}),
                    )
                })?;
                Ok(ReadResult {
                    contents: vec![ResourceContent::Text {
                        uri: params.uri.clone(),
                        mime_type: Some("application/json".to_string()),
                        text: serde_json::to_string(&snap).unwrap_or_else(|_| "{}".into()),
                    }],
                })
            }
        }
    }
}

#[async_trait]
impl CapabilityAdapter for ResourcesAdapter {
    fn capability_name(&self) -> &'static str {
        "resources"
    }

    async fn handle(
        &self,
        method: &str,
        params: JsonValue,
        ctx: &SessionContext,
    ) -> Result<JsonValue, McpError> {
        match method {
            METHOD_LIST => {
                let parsed: ListParams = if params.is_null() {
                    ListParams { cursor: None }
                } else {
                    serde_json::from_value(params).map_err(|e| {
                        McpError::invalid_params(format!("resources/list: bad params: {e}"))
                    })?
                };
                let list = self.list_resources(parsed, ctx).await?;
                serde_json::to_value(list)
                    .map_err(|e| McpError::Internal(format!("resources/list: serialize: {e}")))
            }
            METHOD_READ => {
                let parsed: ReadParams = serde_json::from_value(params).map_err(|e| {
                    McpError::invalid_params(format!("resources/read: bad params: {e}"))
                })?;
                let result = self.read_resource(parsed, ctx).await?;
                serde_json::to_value(result)
                    .map_err(|e| McpError::Internal(format!("resources/read: serialize: {e}")))
            }
            other => Err(McpError::MethodNotFound(other.to_string())),
        }
    }
}

/// Parsed `corlinman://...` URI. Lifetimes are tied to the input so
/// we don't allocate during the read path.
enum ParsedUri<'a> {
    Memory { host: &'a str, id: &'a str },
    Skill { name: &'a str },
    Persona { user_id: &'a str },
}

impl<'a> ParsedUri<'a> {
    fn parse(uri: &'a str) -> Option<Self> {
        let rest = uri.strip_prefix("corlinman://")?;
        // memory/<host>/<id>
        if let Some(after) = rest.strip_prefix("memory/") {
            let (host, id) = after.split_once('/')?;
            if host.is_empty() || id.is_empty() {
                return None;
            }
            return Some(ParsedUri::Memory { host, id });
        }
        // skill/<name>
        if let Some(name) = rest.strip_prefix("skill/") {
            if name.is_empty() {
                return None;
            }
            return Some(ParsedUri::Skill { name });
        }
        // persona/<user_id>/snapshot
        if let Some(after) = rest.strip_prefix("persona/") {
            let (user_id, tail) = after.split_once('/')?;
            if user_id.is_empty() || tail != "snapshot" {
                return None;
            }
            return Some(ParsedUri::Persona { user_id });
        }
        None
    }

    fn scheme(&self) -> &'static str {
        match self {
            ParsedUri::Memory { .. } => "memory",
            ParsedUri::Skill { .. } => "skill",
            ParsedUri::Persona { .. } => "persona",
        }
    }
}

/// First 80 chars of `s`, with a trailing ellipsis when truncated.
/// Used to populate `Resource.description` cheaply.
fn short_preview(s: &str) -> Option<String> {
    let trimmed = s.trim();
    if trimmed.is_empty() {
        return None;
    }
    let mut out = String::new();
    for (i, ch) in trimmed.char_indices() {
        if i >= 80 {
            out.push('…');
            return Some(out);
        }
        out.push(ch);
    }
    Some(out)
}

// ---------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use std::sync::Arc;

    use anyhow::Result as AhResult;
    use corlinman_memory_host::{MemoryDoc, MemoryHit, MemoryHost, MemoryQuery};

    /// In-memory `MemoryHost` stub keyed by id → content.
    struct StubMemoryHost {
        name: String,
        rows: std::sync::Mutex<std::collections::BTreeMap<String, String>>,
    }

    impl StubMemoryHost {
        fn make(name: &str, seed: &[(&str, &str)]) -> Arc<dyn MemoryHost> {
            let m: std::collections::BTreeMap<String, String> = seed
                .iter()
                .map(|(k, v)| ((*k).to_string(), (*v).to_string()))
                .collect();
            Arc::new(Self {
                name: name.to_string(),
                rows: std::sync::Mutex::new(m),
            })
        }
    }

    #[async_trait]
    impl MemoryHost for StubMemoryHost {
        fn name(&self) -> &str {
            &self.name
        }
        async fn query(&self, _req: MemoryQuery) -> AhResult<Vec<MemoryHit>> {
            let rows = self.rows.lock().unwrap();
            Ok(rows
                .iter()
                .map(|(id, content)| MemoryHit {
                    id: id.clone(),
                    content: content.clone(),
                    score: 0.5,
                    source: self.name.clone(),
                    metadata: serde_json::Value::Null,
                })
                .collect())
        }
        async fn upsert(&self, _doc: MemoryDoc) -> AhResult<String> {
            unimplemented!()
        }
        async fn delete(&self, _id: &str) -> AhResult<()> {
            unimplemented!()
        }
        async fn get(&self, id: &str) -> AhResult<Option<MemoryHit>> {
            let rows = self.rows.lock().unwrap();
            Ok(rows.get(id).map(|content| MemoryHit {
                id: id.to_string(),
                content: content.clone(),
                score: 1.0,
                source: self.name.clone(),
                metadata: serde_json::Value::Null,
            }))
        }
    }

    /// Stub persona provider for the persona-scheme tests.
    struct StubPersona {
        ids: Vec<String>,
        snap: BTreeMap<String, JsonValue>,
    }

    #[async_trait]
    impl PersonaSnapshotProvider for StubPersona {
        async fn list_user_ids(&self) -> AhResult<Vec<String>> {
            Ok(self.ids.clone())
        }
        async fn read_snapshot(&self, user_id: &str) -> AhResult<Option<JsonValue>> {
            Ok(self.snap.get(user_id).cloned())
        }
    }

    fn make_skills(skills: &[(&str, &str, &str)]) -> (Arc<SkillRegistry>, tempfile::TempDir) {
        let tmp = tempfile::tempdir().unwrap();
        for (name, desc, body) in skills {
            let path = tmp.path().join(format!("{name}.md"));
            let mut f = std::fs::File::create(&path).unwrap();
            let frontmatter = format!("---\nname: {name}\ndescription: {desc}\n---\n{body}");
            f.write_all(frontmatter.as_bytes()).unwrap();
        }
        let reg = SkillRegistry::load_from_dir(tmp.path()).expect("skill registry load");
        (Arc::new(reg), tmp)
    }

    fn make_adapter(
        hosts: BTreeMap<String, Arc<dyn MemoryHost>>,
        skills: Arc<SkillRegistry>,
    ) -> ResourcesAdapter {
        ResourcesAdapter::new(hosts, skills)
    }

    // ----- list -----

    #[tokio::test]
    async fn list_returns_skills_and_memory_uris() {
        let mut hosts: BTreeMap<String, Arc<dyn MemoryHost>> = BTreeMap::new();
        hosts.insert(
            "kb".into(),
            StubMemoryHost::make("kb", &[("1", "first"), ("2", "second")]),
        );
        let (skills, _tmp) = make_skills(&[("foo", "foo desc", "Body F")]);
        let adapter = make_adapter(hosts, skills);

        let res = adapter
            .list_resources(ListParams { cursor: None }, &SessionContext::permissive())
            .await
            .unwrap();
        let uris: Vec<_> = res.resources.iter().map(|r| r.uri.clone()).collect();
        assert!(uris.contains(&"corlinman://memory/kb/1".to_string()));
        assert!(uris.contains(&"corlinman://memory/kb/2".to_string()));
        assert!(uris.contains(&"corlinman://skill/foo".to_string()));
    }

    #[tokio::test]
    async fn list_paginates_with_server_issued_cursor() {
        let mut hosts: BTreeMap<String, Arc<dyn MemoryHost>> = BTreeMap::new();
        let seed: Vec<(String, String)> = (0..150)
            .map(|i| (format!("{i:03}"), format!("doc-{i}")))
            .collect();
        let seed_refs: Vec<(&str, &str)> =
            seed.iter().map(|(a, b)| (a.as_str(), b.as_str())).collect();
        hosts.insert("kb".into(), StubMemoryHost::make("kb", &seed_refs));
        let (skills, _tmp) = make_skills(&[]);
        let adapter = ResourcesAdapter::new(hosts, skills)
            .with_page_size(50)
            .with_memory_list_limit(200);

        // Page 1.
        let p1 = adapter
            .list_resources(ListParams { cursor: None }, &SessionContext::permissive())
            .await
            .unwrap();
        assert_eq!(p1.resources.len(), 50);
        let cursor = p1.next_cursor.expect("must have a next cursor");
        assert_eq!(cursor, "50");

        // Page 2.
        let p2 = adapter
            .list_resources(
                ListParams {
                    cursor: Some(cursor),
                },
                &SessionContext::permissive(),
            )
            .await
            .unwrap();
        assert_eq!(p2.resources.len(), 50);
        assert_eq!(p2.next_cursor.as_deref(), Some("100"));

        // Page 3 (last).
        let p3 = adapter
            .list_resources(
                ListParams {
                    cursor: Some("100".into()),
                },
                &SessionContext::permissive(),
            )
            .await
            .unwrap();
        assert_eq!(p3.resources.len(), 50);
        assert!(p3.next_cursor.is_none());
    }

    #[tokio::test]
    async fn list_invalid_cursor_returns_invalid_params() {
        let hosts: BTreeMap<String, Arc<dyn MemoryHost>> = BTreeMap::new();
        let (skills, _tmp) = make_skills(&[]);
        let adapter = make_adapter(hosts, skills);
        let err = adapter
            .list_resources(
                ListParams {
                    cursor: Some("not-a-number".into()),
                },
                &SessionContext::permissive(),
            )
            .await
            .expect_err("must error");
        assert_eq!(err.jsonrpc_code(), -32602);
    }

    #[tokio::test]
    async fn list_filters_by_scheme_allowlist() {
        let mut hosts: BTreeMap<String, Arc<dyn MemoryHost>> = BTreeMap::new();
        hosts.insert("kb".into(), StubMemoryHost::make("kb", &[("1", "x")]));
        let (skills, _tmp) = make_skills(&[("foo", "stub desc", "body")]);
        let adapter = make_adapter(hosts, skills);

        let ctx = SessionContext {
            resources_allowed: vec!["skill".into()],
            ..Default::default()
        };
        let res = adapter
            .list_resources(ListParams { cursor: None }, &ctx)
            .await
            .unwrap();
        for r in &res.resources {
            assert!(r.uri.starts_with("corlinman://skill/"), "got {}", r.uri);
        }
    }

    // ----- read -----

    #[tokio::test]
    async fn read_skill_returns_body_markdown_verbatim() {
        let hosts: BTreeMap<String, Arc<dyn MemoryHost>> = BTreeMap::new();
        let (skills, _tmp) = make_skills(&[("foo", "foo desc", "Step1.\nStep2.")]);
        let adapter = make_adapter(hosts, skills);
        let res = adapter
            .read_resource(
                ReadParams {
                    uri: "corlinman://skill/foo".into(),
                },
                &SessionContext::permissive(),
            )
            .await
            .unwrap();
        match &res.contents[0] {
            ResourceContent::Text { uri, text, .. } => {
                assert_eq!(uri, "corlinman://skill/foo");
                assert!(text.contains("Step1."));
                assert!(text.contains("Step2."));
            }
            other => panic!("expected text content, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn read_memory_routes_to_named_host() {
        let mut hosts: BTreeMap<String, Arc<dyn MemoryHost>> = BTreeMap::new();
        hosts.insert(
            "kb".into(),
            StubMemoryHost::make("kb", &[("42", "memory body")]),
        );
        let (skills, _tmp) = make_skills(&[]);
        let adapter = make_adapter(hosts, skills);
        let res = adapter
            .read_resource(
                ReadParams {
                    uri: "corlinman://memory/kb/42".into(),
                },
                &SessionContext::permissive(),
            )
            .await
            .unwrap();
        match &res.contents[0] {
            ResourceContent::Text { text, .. } => {
                assert_eq!(text, "memory body");
            }
            other => panic!("expected text content, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn read_persona_routes_to_provider() {
        let hosts: BTreeMap<String, Arc<dyn MemoryHost>> = BTreeMap::new();
        let (skills, _tmp) = make_skills(&[]);
        let mut snap = BTreeMap::new();
        snap.insert("alice".to_string(), json!({"trait": "curious"}));
        let persona = Arc::new(StubPersona {
            ids: vec!["alice".into()],
            snap,
        });
        let adapter = ResourcesAdapter::new(hosts, skills).with_persona(persona);
        let res = adapter
            .read_resource(
                ReadParams {
                    uri: "corlinman://persona/alice/snapshot".into(),
                },
                &SessionContext::permissive(),
            )
            .await
            .unwrap();
        match &res.contents[0] {
            ResourceContent::Text {
                text, mime_type, ..
            } => {
                let parsed: JsonValue = serde_json::from_str(text).unwrap();
                assert_eq!(parsed, json!({"trait": "curious"}));
                assert_eq!(mime_type.as_deref(), Some("application/json"));
            }
            other => panic!("expected text content, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn read_unknown_uri_returns_invalid_params() {
        let hosts: BTreeMap<String, Arc<dyn MemoryHost>> = BTreeMap::new();
        let (skills, _tmp) = make_skills(&[]);
        let adapter = make_adapter(hosts, skills);
        let err = adapter
            .read_resource(
                ReadParams {
                    uri: "https://example.com/foo".into(),
                },
                &SessionContext::permissive(),
            )
            .await
            .expect_err("must error");
        assert_eq!(err.jsonrpc_code(), -32602);
    }

    #[tokio::test]
    async fn read_unknown_memory_id_returns_invalid_params() {
        let mut hosts: BTreeMap<String, Arc<dyn MemoryHost>> = BTreeMap::new();
        hosts.insert("kb".into(), StubMemoryHost::make("kb", &[("1", "x")]));
        let (skills, _tmp) = make_skills(&[]);
        let adapter = make_adapter(hosts, skills);
        let err = adapter
            .read_resource(
                ReadParams {
                    uri: "corlinman://memory/kb/9999".into(),
                },
                &SessionContext::permissive(),
            )
            .await
            .expect_err("must error");
        assert_eq!(err.jsonrpc_code(), -32602);
    }

    #[tokio::test]
    async fn read_disallowed_scheme_returns_invalid_params() {
        let mut hosts: BTreeMap<String, Arc<dyn MemoryHost>> = BTreeMap::new();
        hosts.insert("kb".into(), StubMemoryHost::make("kb", &[("1", "x")]));
        let (skills, _tmp) = make_skills(&[]);
        let adapter = make_adapter(hosts, skills);

        let ctx = SessionContext {
            resources_allowed: vec!["skill".into()],
            ..Default::default()
        };
        let err = adapter
            .read_resource(
                ReadParams {
                    uri: "corlinman://memory/kb/1".into(),
                },
                &ctx,
            )
            .await
            .expect_err("must error");
        assert_eq!(err.jsonrpc_code(), -32602);
        match err {
            McpError::InvalidParams { message, .. } => {
                assert!(message.contains("not allowed"), "got {message:?}");
            }
            other => panic!("expected InvalidParams, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn read_isolates_hosts_by_name() {
        // Two hosts; reading from one should never serve the other's
        // content even if ids collide. This is the "tenant isolation"
        // surrogate at the adapter layer — full tenant ACL is iter 8.
        let mut hosts: BTreeMap<String, Arc<dyn MemoryHost>> = BTreeMap::new();
        hosts.insert(
            "alpha".into(),
            StubMemoryHost::make("alpha", &[("1", "ALPHA")]),
        );
        hosts.insert("beta".into(), StubMemoryHost::make("beta", &[("1", "BETA")]));
        let (skills, _tmp) = make_skills(&[]);
        let adapter = make_adapter(hosts, skills);

        let alpha = adapter
            .read_resource(
                ReadParams {
                    uri: "corlinman://memory/alpha/1".into(),
                },
                &SessionContext::permissive(),
            )
            .await
            .unwrap();
        let beta = adapter
            .read_resource(
                ReadParams {
                    uri: "corlinman://memory/beta/1".into(),
                },
                &SessionContext::permissive(),
            )
            .await
            .unwrap();
        match (&alpha.contents[0], &beta.contents[0]) {
            (ResourceContent::Text { text: a, .. }, ResourceContent::Text { text: b, .. }) => {
                assert_eq!(a, "ALPHA");
                assert_eq!(b, "BETA");
            }
            _ => panic!("expected text contents on both"),
        }
    }

    // ----- handle -----

    #[tokio::test]
    async fn handle_routes_through_capability_adapter_trait() {
        let mut hosts: BTreeMap<String, Arc<dyn MemoryHost>> = BTreeMap::new();
        hosts.insert("kb".into(), StubMemoryHost::make("kb", &[("1", "x")]));
        let (skills, _tmp) = make_skills(&[("foo", "stub desc", "body")]);
        let adapter = make_adapter(hosts, skills);
        assert_eq!(adapter.capability_name(), "resources");

        let value = adapter
            .handle(
                "resources/list",
                JsonValue::Null,
                &SessionContext::permissive(),
            )
            .await
            .unwrap();
        let parsed: ListResult = serde_json::from_value(value).unwrap();
        assert!(!parsed.resources.is_empty());

        let err = adapter
            .handle(
                "resources/bogus",
                JsonValue::Null,
                &SessionContext::permissive(),
            )
            .await
            .expect_err("unknown method");
        assert!(matches!(err, McpError::MethodNotFound(_)));
    }

    #[test]
    fn parse_uri_recognises_three_schemes_and_rejects_others() {
        match ParsedUri::parse("corlinman://memory/kb/abc").unwrap() {
            ParsedUri::Memory { host, id } => {
                assert_eq!(host, "kb");
                assert_eq!(id, "abc");
            }
            _ => panic!(),
        }
        match ParsedUri::parse("corlinman://skill/foo").unwrap() {
            ParsedUri::Skill { name } => assert_eq!(name, "foo"),
            _ => panic!(),
        }
        match ParsedUri::parse("corlinman://persona/u1/snapshot").unwrap() {
            ParsedUri::Persona { user_id } => assert_eq!(user_id, "u1"),
            _ => panic!(),
        }
        // Wrong tail on persona.
        assert!(ParsedUri::parse("corlinman://persona/u1/other").is_none());
        // Missing id.
        assert!(ParsedUri::parse("corlinman://memory/kb/").is_none());
        // Bad scheme.
        assert!(ParsedUri::parse("https://example.com/x").is_none());
    }
}
