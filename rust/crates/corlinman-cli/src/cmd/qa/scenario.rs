//! YAML scenario schema + loader.
//!
//! Scenarios pick a `kind` and fill in the matching payload. Only a handful
//! of kinds are actually implemented — anything else is treated as an error
//! so typos don't silently pass.
//!
//! The top-level shape of every YAML file:
//!
//! ```yaml
//! name: chat-nonstream-happy-path
//! description: |
//!   One-line human description.
//! # Set `true` to require a live environment (real python server, gocq, …).
//! requires_live: false
//! kind: chat_http
//! chat_http:
//!   request:
//!     stream: false
//!     messages:
//!       - role: user
//!         content: hi
//!   frames:
//!     - {kind: token, text: "hello "}
//!     - {kind: token, text: "world"}
//!     - {kind: done, reason: stop}
//!   expect:
//!     status: 200
//!     json_contains:
//!       - path: choices.0.message.content
//!         contains: "hello world"
//! ```

use std::ffi::OsStr;
use std::path::{Path, PathBuf};

use serde::Deserialize;

#[derive(Debug, Clone, Deserialize)]
pub struct Scenario {
    pub name: String,
    /// Unused by the runner — captured so `serde(deny_unknown_fields)` can't
    /// reject scenarios that carry a human-readable narrative.
    #[serde(default)]
    #[allow(dead_code)]
    pub description: String,
    #[serde(default)]
    pub requires_live: bool,
    /// Declared kind + payload union. Left as an opaque `serde_yaml::Value`
    /// so each runner-side handler can pick the fields it needs without
    /// pulling every shape into one enum (keeps the YAML forgiving).
    #[serde(flatten)]
    pub body: ScenarioBody,

    /// Absolute path of the loaded file; used by `read_fixture` etc.
    #[serde(skip, default)]
    pub source_path: PathBuf,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ScenarioBody {
    pub kind: ScenarioKind,
    /// `chat_http`-kind payload.
    #[serde(default)]
    pub chat_http: Option<ChatHttpScenario>,
    /// `plugin_exec_sync`/`plugin_exec_async` payload.
    #[serde(default)]
    pub plugin_exec: Option<PluginExecScenario>,
    /// `rag_hybrid` payload.
    #[serde(default)]
    pub rag_hybrid: Option<RagHybridScenario>,
    /// `live` payload — human-readable notes only.
    #[serde(default)]
    pub live: Option<LiveScenario>,
}

#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ScenarioKind {
    ChatHttp,
    PluginExecSync,
    PluginExecAsync,
    RagHybrid,
    /// Marked as needing a live environment — always skipped by default.
    Live,
}

// ---------------------------------------------------------------------------
// chat_http
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Deserialize)]
pub struct ChatHttpScenario {
    pub request: ChatRequest,
    pub frames: Vec<FrameScript>,
    pub expect: ChatExpect,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ChatRequest {
    #[serde(default = "default_model")]
    pub model: String,
    #[serde(default)]
    pub stream: bool,
    pub messages: Vec<ChatMessage>,
    #[serde(default)]
    pub tools: Option<serde_json::Value>,
}

fn default_model() -> String {
    "claude-sonnet-4-5".into()
}

#[derive(Debug, Clone, Deserialize)]
pub struct ChatMessage {
    pub role: String,
    pub content: String,
}

/// One scripted server frame to replay through the mock backend.
#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum FrameScript {
    Token {
        text: String,
    },
    /// Emits a `ToolCall` frame with the given OpenAI-shaped args JSON.
    ToolCall {
        id: String,
        name: String,
        arguments: serde_json::Value,
    },
    Done {
        #[serde(default = "default_reason")]
        reason: String,
    },
    Error {
        message: String,
    },
}

fn default_reason() -> String {
    "stop".into()
}

#[derive(Debug, Clone, Deserialize)]
pub struct ChatExpect {
    #[serde(default = "default_status")]
    pub status: u16,
    /// For non-stream: jsonpath-ish assertions on the parsed body.
    #[serde(default)]
    pub json_contains: Vec<JsonContains>,
    /// For stream=true: literal substrings that MUST appear in the raw
    /// SSE body (`data: [DONE]`, tokens, etc).
    #[serde(default)]
    pub stream_fragments: Vec<String>,
}

fn default_status() -> u16 {
    200
}

#[derive(Debug, Clone, Deserialize)]
pub struct JsonContains {
    pub path: String,
    #[serde(default)]
    pub contains: Option<String>,
    #[serde(default)]
    pub equals: Option<serde_json::Value>,
}

// ---------------------------------------------------------------------------
// plugin_exec
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Deserialize)]
pub struct PluginExecScenario {
    /// Tool-call arguments JSON object (inlined).
    pub arguments: serde_json::Value,
    /// Expected output mode: `success` / `task` / `error`.
    pub expect: PluginExecExpect,
}

#[derive(Debug, Clone, Deserialize)]
pub struct PluginExecExpect {
    #[serde(default)]
    pub success_json_contains: Vec<JsonContains>,
    #[serde(default)]
    pub accepted_task_id: Option<String>,
    /// Reserved for future `plugin_exec_error` scenarios; currently unused.
    #[serde(default)]
    #[allow(dead_code)]
    pub error_message_contains: Option<String>,
}

// ---------------------------------------------------------------------------
// rag_hybrid
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Deserialize)]
pub struct RagHybridScenario {
    pub corpus: Vec<RagCorpusEntry>,
    pub query: RagQuery,
    pub expect: RagExpect,
}

#[derive(Debug, Clone, Deserialize)]
pub struct RagCorpusEntry {
    pub content: String,
    /// Per-chunk dense vector. Dim must match `vector_dim`.
    pub vector: Vec<f32>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct RagQuery {
    pub text: String,
    pub vector: Vec<f32>,
    #[serde(default = "default_top_k")]
    pub top_k: usize,
}

fn default_top_k() -> usize {
    3
}

#[derive(Debug, Clone, Deserialize)]
pub struct RagExpect {
    /// Minimum number of hits the searcher must return.
    #[serde(default = "default_min_hits")]
    pub min_hits: usize,
    /// The chunk content at position 0 (top hit) must contain this substring.
    pub top_contains: String,
}

fn default_min_hits() -> usize {
    1
}

// ---------------------------------------------------------------------------
// live
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Deserialize)]
pub struct LiveScenario {
    /// Human-readable note about what a real operator has to do.
    pub note: String,
}

// ---------------------------------------------------------------------------
// loader
// ---------------------------------------------------------------------------

/// Load every `*.yaml` / `*.yml` in `dir`, sorted by filename for determinism.
///
/// If `filter` is `Some(substr)`, only files whose stem contains it are
/// kept. Missing directories yield an explicit error so the CLI can surface
/// "did you mean …?" messages.
pub fn load_dir(dir: &Path, filter: Option<&str>) -> anyhow::Result<Vec<Scenario>> {
    if !dir.exists() {
        anyhow::bail!("scenarios dir {} does not exist", dir.display());
    }
    let mut files: Vec<PathBuf> = std::fs::read_dir(dir)
        .map_err(|e| anyhow::anyhow!("read_dir {}: {e}", dir.display()))?
        .filter_map(|r| r.ok().map(|e| e.path()))
        .filter(|p| {
            let ext = p.extension().and_then(OsStr::to_str);
            matches!(ext, Some("yaml") | Some("yml"))
        })
        .collect();
    files.sort();

    let mut out = Vec::with_capacity(files.len());
    for path in files {
        let stem = path
            .file_stem()
            .and_then(OsStr::to_str)
            .unwrap_or("")
            .to_string();
        if let Some(needle) = filter {
            if !stem.contains(needle) {
                continue;
            }
        }
        let body = std::fs::read_to_string(&path)
            .map_err(|e| anyhow::anyhow!("read {}: {e}", path.display()))?;
        let mut sc: Scenario = serde_yaml::from_str(&body)
            .map_err(|e| anyhow::anyhow!("parse {}: {e}", path.display()))?;
        sc.source_path = path;
        out.push(sc);
    }
    Ok(out)
}
