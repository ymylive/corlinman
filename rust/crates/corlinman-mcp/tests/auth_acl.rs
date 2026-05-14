//! Integration tests for iter 8: per-token ACL + tenant scoping.
//!
//! These exercise the full path from `TokenAcl::to_session_context()`
//! through the capability adapters' `handle()` methods. They don't
//! stand up the WebSocket transport (covered by transport unit tests);
//! the goal here is to lock the contract that:
//!
//! 1. `tools/list` filters by `tools_allowlist` end-to-end.
//! 2. `tools/call` rejects with `tool_not_allowed` (-32001) when the
//!    name isn't covered by the allowlist.
//! 3. `resources/list` and `resources/read` are scoped by
//!    `resources_allowed`, *and* a token tagged with one tenant never
//!    sees memory hosts wired for another (cross-tenant isolation).
//! 4. A token without `tenant_id` lands on `DEFAULT_TENANT_ID`
//!    ("default") in its `SessionContext`.
//!
//! Memory hosts are stubbed in-process (the BTreeMap-backed
//! `StubMemoryHost`) — this test verifies adapter behaviour, not the
//! production sqlite path. Real cross-tenant routing through
//! `TenantPool` lands in the gateway integration (iter 9) and is
//! covered there.

use std::collections::BTreeMap;
use std::sync::Arc;

use async_trait::async_trait;
use serde_json::json;

use corlinman_mcp::adapters::{CapabilityAdapter, ResourcesAdapter, ToolsAdapter};
use corlinman_mcp::error::McpError;
use corlinman_mcp::schema::resources::{ListResult as ResListResult, ReadResult};
use corlinman_mcp::schema::tools::{CallResult, ListResult as ToolsListResult};
use corlinman_mcp::server::{TokenAcl, DEFAULT_TENANT_ID};

use bytes::Bytes;
use corlinman_memory_host::{MemoryDoc, MemoryHit, MemoryHost, MemoryQuery};
use corlinman_plugins::registry::PluginRegistry;
use corlinman_plugins::runtime::{PluginInput, PluginOutput, PluginRuntime, ProgressSink};
use corlinman_skills::SkillRegistry;
use tokio_util::sync::CancellationToken;

// -------------------------------------------------------------------------
// Stub MemoryHost (alpha-tenant or beta-tenant)
// -------------------------------------------------------------------------

struct StubMemoryHost {
    name: String,
    rows: std::sync::Mutex<BTreeMap<String, String>>,
}

impl StubMemoryHost {
    fn new(name: &str, seed: &[(&str, &str)]) -> Arc<dyn MemoryHost> {
        let m: BTreeMap<String, String> = seed
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
    async fn query(&self, _req: MemoryQuery) -> anyhow::Result<Vec<MemoryHit>> {
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
    async fn upsert(&self, _doc: MemoryDoc) -> anyhow::Result<String> {
        unimplemented!()
    }
    async fn delete(&self, _id: &str) -> anyhow::Result<()> {
        unimplemented!()
    }
    async fn get(&self, id: &str) -> anyhow::Result<Option<MemoryHit>> {
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

// -------------------------------------------------------------------------
// Stub PluginRuntime (echoes a fixed body)
// -------------------------------------------------------------------------

struct EchoRuntime;

#[async_trait]
impl PluginRuntime for EchoRuntime {
    async fn execute(
        &self,
        _input: PluginInput,
        _progress: Option<Arc<dyn ProgressSink>>,
        _cancel: CancellationToken,
    ) -> Result<PluginOutput, corlinman_core::CorlinmanError> {
        Ok(PluginOutput::success(Bytes::from_static(b"\"ok\""), 1))
    }
    fn kind(&self) -> &'static str {
        "echo"
    }
}

fn make_two_tool_registry(tmp: &tempfile::TempDir) -> Arc<PluginRegistry> {
    use std::io::Write;
    // Plugin "kb" with two tools: kb:search + kb:get
    let dir = tmp.path().join("kb");
    std::fs::create_dir_all(&dir).unwrap();
    let body = r#"name = "kb"
version = "0.1.0"
plugin_type = "sync"
[entry_point]
command = "true"
[communication]
timeout_ms = 2000
[[capabilities.tools]]
name = "search"
description = "search kb"
[capabilities.tools.parameters]
type = "object"
[[capabilities.tools]]
name = "get"
description = "fetch by id"
[capabilities.tools.parameters]
type = "object"
"#;
    std::fs::File::create(dir.join("plugin-manifest.toml"))
        .unwrap()
        .write_all(body.as_bytes())
        .unwrap();

    // Plugin "web" with one tool: web:fetch — used to prove allowlist
    // crosses plugin boundaries.
    let dir2 = tmp.path().join("web");
    std::fs::create_dir_all(&dir2).unwrap();
    let body2 = r#"name = "web"
version = "0.1.0"
plugin_type = "sync"
[entry_point]
command = "true"
[communication]
timeout_ms = 2000
[[capabilities.tools]]
name = "fetch"
description = "fetch URL"
[capabilities.tools.parameters]
type = "object"
"#;
    std::fs::File::create(dir2.join("plugin-manifest.toml"))
        .unwrap()
        .write_all(body2.as_bytes())
        .unwrap();

    let roots = vec![corlinman_plugins::discovery::SearchRoot::new(
        tmp.path(),
        corlinman_plugins::discovery::Origin::Workspace,
    )];
    Arc::new(PluginRegistry::from_roots(roots))
}

fn make_skills(tmp: &tempfile::TempDir, names: &[&str]) -> Arc<SkillRegistry> {
    use std::io::Write;
    for n in names {
        let mut f = std::fs::File::create(tmp.path().join(format!("{n}.md"))).unwrap();
        let txt = format!("---\nname: {n}\ndescription: stub\n---\nbody for {n}\n");
        f.write_all(txt.as_bytes()).unwrap();
    }
    Arc::new(SkillRegistry::load_from_dir(tmp.path()).expect("skills"))
}

// -------------------------------------------------------------------------
// Tests
// -------------------------------------------------------------------------

#[tokio::test]
async fn tools_list_filters_by_acl_allowlist_end_to_end() {
    let tmp = tempfile::tempdir().unwrap();
    let reg = make_two_tool_registry(&tmp);
    let runtime: Arc<dyn PluginRuntime> = Arc::new(EchoRuntime);
    let adapter = ToolsAdapter::with_runtime(reg, runtime);

    let acl = TokenAcl {
        token: "t".into(),
        label: "limited".into(),
        tools_allowlist: vec!["kb:*".into()], // only kb:*
        resources_allowed: vec!["*".into()],
        prompts_allowed: vec!["*".into()],
        tenant_id: Some("alpha".into()),
    };
    let ctx = acl.to_session_context();

    let value = adapter
        .handle("tools/list", serde_json::Value::Null, &ctx)
        .await
        .unwrap();
    let parsed: ToolsListResult = serde_json::from_value(value).unwrap();
    let names: Vec<_> = parsed.tools.iter().map(|t| t.name.clone()).collect();
    // web:fetch is filtered out; kb:* survives.
    assert_eq!(names, vec!["kb:get".to_string(), "kb:search".to_string()]);
}

#[tokio::test]
async fn tools_call_rejected_when_disallowed_by_acl() {
    let tmp = tempfile::tempdir().unwrap();
    let reg = make_two_tool_registry(&tmp);
    let runtime: Arc<dyn PluginRuntime> = Arc::new(EchoRuntime);
    let adapter = ToolsAdapter::with_runtime(reg, runtime);

    let acl = TokenAcl {
        token: "t".into(),
        label: "limited".into(),
        tools_allowlist: vec!["kb:*".into()],
        resources_allowed: vec!["*".into()],
        prompts_allowed: vec!["*".into()],
        tenant_id: None,
    };
    let ctx = acl.to_session_context();

    let err = adapter
        .handle(
            "tools/call",
            json!({"name": "web:fetch", "arguments": {}}),
            &ctx,
        )
        .await
        .expect_err("must reject");
    match err {
        McpError::ToolNotAllowed(name) => assert_eq!(name, "web:fetch"),
        other => panic!("expected ToolNotAllowed, got {other:?}"),
    }
    // -32001 is the corlinman extension for ACL denials.
    let code = match adapter
        .handle(
            "tools/call",
            json!({"name": "web:fetch", "arguments": {}}),
            &ctx,
        )
        .await
    {
        Err(e) => e.jsonrpc_code(),
        Ok(_) => panic!("must err"),
    };
    assert_eq!(code, -32001);

    // And the allowed branch still works (returns CallResult, not error).
    let ok = adapter
        .handle(
            "tools/call",
            json!({"name": "kb:search", "arguments": {}}),
            &ctx,
        )
        .await
        .unwrap();
    let parsed: CallResult = serde_json::from_value(ok).unwrap();
    assert!(!parsed.is_error);
}

#[tokio::test]
async fn resources_list_filters_by_scheme_allowlist_end_to_end() {
    let tmpd = tempfile::tempdir().unwrap();
    let mut hosts: BTreeMap<String, Arc<dyn MemoryHost>> = BTreeMap::new();
    hosts.insert(
        "alpha".into(),
        StubMemoryHost::new("alpha", &[("1", "ALPHA-1")]),
    );
    let skills = make_skills(&tmpd, &["foo"]);
    let adapter = ResourcesAdapter::new(hosts, skills);

    // Token sees skills only.
    let acl = TokenAcl {
        token: "t".into(),
        label: "skills-only".into(),
        tools_allowlist: vec!["*".into()],
        resources_allowed: vec!["skill".into()],
        prompts_allowed: vec!["*".into()],
        tenant_id: Some("alpha".into()),
    };
    let ctx = acl.to_session_context();
    let value = adapter
        .handle("resources/list", serde_json::Value::Null, &ctx)
        .await
        .unwrap();
    let parsed: ResListResult = serde_json::from_value(value).unwrap();
    for r in &parsed.resources {
        assert!(
            r.uri.starts_with("corlinman://skill/"),
            "ACL leak: {}",
            r.uri
        );
    }
    assert!(!parsed.resources.is_empty(), "skill list must be non-empty");

    // Read of a memory uri is rejected with -32602 + 'not allowed'.
    let err = adapter
        .handle(
            "resources/read",
            json!({"uri": "corlinman://memory/alpha/1"}),
            &ctx,
        )
        .await
        .expect_err("must reject");
    assert_eq!(err.jsonrpc_code(), -32602);
}

#[tokio::test]
async fn cross_tenant_read_returns_empty_or_unknown_host() {
    // Two memory hosts ("alpha" and "beta") wired side-by-side; a token
    // tagged tenant=alpha must never read beta's content. The
    // ResourcesAdapter routes by host name, and the host map for the
    // adapter is the alpha-only view — that's the iter-8 contract:
    // pre-adapter wiring filters memory hosts by `ctx.tenant_id`. We
    // simulate that by constructing the adapter with only the alpha
    // host visible (the gateway integration in iter 9 does this via
    // `TenantPool::pool_for(tenant, "kb")`).
    let tmpd = tempfile::tempdir().unwrap();
    let mut alpha_only_hosts: BTreeMap<String, Arc<dyn MemoryHost>> = BTreeMap::new();
    alpha_only_hosts.insert(
        "alpha".into(),
        StubMemoryHost::new("alpha", &[("1", "ALPHA-1")]),
    );
    let skills = make_skills(&tmpd, &[]);
    let adapter = ResourcesAdapter::new(alpha_only_hosts, skills);

    let acl = TokenAcl {
        token: "t".into(),
        label: "alpha-token".into(),
        tools_allowlist: vec!["*".into()],
        resources_allowed: vec!["*".into()],
        prompts_allowed: vec!["*".into()],
        tenant_id: Some("alpha".into()),
    };
    let ctx = acl.to_session_context();

    // The 'alpha' tenant id is plumbed onto the context.
    assert_eq!(ctx.tenant_id.as_deref(), Some("alpha"));

    // Reading a known beta URI must fail because the host is not in
    // the alpha-scoped adapter map (the gateway prunes it). Error is
    // -32602 'unknown memory host'.
    let err = adapter
        .handle(
            "resources/read",
            json!({"uri": "corlinman://memory/beta/1"}),
            &ctx,
        )
        .await
        .expect_err("cross-tenant read must reject");
    assert_eq!(err.jsonrpc_code(), -32602);

    // List exposes only the alpha host's IDs.
    let lvalue = adapter
        .handle("resources/list", serde_json::Value::Null, &ctx)
        .await
        .unwrap();
    let lparsed: ResListResult = serde_json::from_value(lvalue).unwrap();
    let uris: Vec<_> = lparsed.resources.iter().map(|r| r.uri.clone()).collect();
    assert!(uris
        .iter()
        .any(|u| u.starts_with("corlinman://memory/alpha/")));
    assert!(
        !uris
            .iter()
            .any(|u| u.starts_with("corlinman://memory/beta/")),
        "beta must not leak; got {uris:?}"
    );
}

#[tokio::test]
async fn missing_tenant_falls_back_to_default_constant() {
    let acl = TokenAcl {
        token: "t".into(),
        label: "no-tenant".into(),
        tools_allowlist: vec!["*".into()],
        resources_allowed: vec!["*".into()],
        prompts_allowed: vec!["*".into()],
        tenant_id: None,
    };
    let ctx = acl.to_session_context();
    assert_eq!(ctx.tenant_id.as_deref(), Some(DEFAULT_TENANT_ID));
    assert_eq!(ctx.tenant_id.as_deref(), Some("default"));
}

#[tokio::test]
async fn empty_acl_lists_fail_closed_at_adapter_layer() {
    // ACL with empty lists everywhere → context denies every method.
    let acl = TokenAcl {
        token: "t".into(),
        label: "empty".into(),
        tools_allowlist: vec![],
        resources_allowed: vec![],
        prompts_allowed: vec![],
        tenant_id: None,
    };
    let ctx = acl.to_session_context();

    // Run through tools/list — must surface an empty list, not an
    // unfiltered one.
    let tmp = tempfile::tempdir().unwrap();
    let reg = make_two_tool_registry(&tmp);
    let runtime: Arc<dyn PluginRuntime> = Arc::new(EchoRuntime);
    let adapter = ToolsAdapter::with_runtime(reg, runtime);
    let value = adapter
        .handle("tools/list", serde_json::Value::Null, &ctx)
        .await
        .unwrap();
    let parsed: ToolsListResult = serde_json::from_value(value).unwrap();
    assert!(
        parsed.tools.is_empty(),
        "empty allowlist must yield empty list"
    );

    // And calling kb:search is rejected (unallowed under empty list).
    let err = adapter
        .handle(
            "tools/call",
            json!({"name": "kb:search", "arguments": {}}),
            &ctx,
        )
        .await
        .expect_err("call must be denied");
    assert_eq!(err.jsonrpc_code(), -32001);

    // ResourcesAdapter — read of any URI is denied at scheme-ACL.
    let tmpd = tempfile::tempdir().unwrap();
    let mut hosts: BTreeMap<String, Arc<dyn MemoryHost>> = BTreeMap::new();
    hosts.insert("kb".into(), StubMemoryHost::new("kb", &[("1", "x")]));
    let skills = make_skills(&tmpd, &["foo"]);
    let res_adapter = ResourcesAdapter::new(hosts, skills);
    let lvalue = res_adapter
        .handle("resources/list", serde_json::Value::Null, &ctx)
        .await
        .unwrap();
    let lparsed: ResListResult = serde_json::from_value(lvalue).unwrap();
    assert!(lparsed.resources.is_empty());
    let _ = ReadResult { contents: vec![] }; // silence unused import on success branch
}
