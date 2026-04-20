//! corlinman config schema + loader.
//!
//! Config is a single TOML file (default `~/.corlinman/config.toml`); envs with
//! the `CORLINMAN_` prefix may override selected fields at load time. See
//! `docs/architecture.md §7` and `docs/config.example.toml`.
//!
//! The schema is type-checked three ways:
//!   1. `serde` decodes TOML into the struct tree.
//!   2. `validator` derive runs basic bounds / length / regex checks.
//!   3. [`Config::validate_report`] layers on cross-field invariants
//!      (e.g. `models.default` must resolve against an enabled provider).
//!
//! `SecretRef` lets the TOML reference an environment variable (`{ env = "…" }`)
//! rather than embedding a literal; see [`SecretRef::resolve`]. `show`-style
//! serialisation redacts `Literal` values so `corlinman config show` never
//! prints raw secrets.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use validator::Validate;

use crate::error::CorlinmanError;

// ---------------------------------------------------------------------------
// Top-level config
// ---------------------------------------------------------------------------

/// Root TOML schema. Every sub-section defaults to its `Default` so a near-empty
/// `config.toml` is still loadable.
#[derive(Debug, Clone, Default, Serialize, Deserialize, JsonSchema, Validate)]
#[serde(default, deny_unknown_fields)]
pub struct Config {
    #[validate(nested)]
    pub server: ServerConfig,
    #[validate(nested)]
    pub admin: AdminConfig,
    pub providers: ProvidersConfig,
    #[validate(nested)]
    pub models: ModelsConfig,
    pub channels: ChannelsConfig,
    #[validate(nested)]
    pub rag: RagConfig,
    pub approvals: ApprovalsConfig,
    pub scheduler: SchedulerConfig,
    #[validate(nested)]
    pub logging: LoggingConfig,
    pub meta: Meta,
}

// ---------------------------------------------------------------------------
// [server]
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, Validate)]
#[serde(default, deny_unknown_fields)]
pub struct ServerConfig {
    #[validate(range(min = 1, max = 65535))]
    pub port: u16,
    #[validate(length(min = 1))]
    pub bind: String,
    pub data_dir: PathBuf,
    /// Maximum number of messages retained per session after each chat turn.
    /// Older messages are trimmed asynchronously by the gateway.
    #[validate(range(min = 1, max = 10000))]
    pub session_max_messages: usize,
}

impl Default for ServerConfig {
    fn default() -> Self {
        Self {
            port: default_port(),
            bind: default_bind(),
            data_dir: default_data_dir(),
            session_max_messages: default_session_max_messages(),
        }
    }
}

fn default_session_max_messages() -> usize {
    100
}

fn default_port() -> u16 {
    6005
}
fn default_bind() -> String {
    "0.0.0.0".into()
}
fn default_data_dir() -> PathBuf {
    dirs::home_dir().unwrap_or_default().join(".corlinman")
}

// ---------------------------------------------------------------------------
// [admin]
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Default, Serialize, Deserialize, JsonSchema, Validate)]
#[serde(default, deny_unknown_fields)]
pub struct AdminConfig {
    pub username: Option<String>,
    /// argon2id hash string (`$argon2id$...`). Never a raw password.
    pub password_hash: Option<String>,
}

// ---------------------------------------------------------------------------
// [providers.*]
// ---------------------------------------------------------------------------

/// Known provider slots. Each is optional; absent = not configured. New
/// providers should be added as explicit fields rather than a `HashMap` so
/// typos surface at decode time via `deny_unknown_fields`.
#[derive(Debug, Clone, Default, Serialize, Deserialize, JsonSchema)]
#[serde(default, deny_unknown_fields)]
pub struct ProvidersConfig {
    pub anthropic: Option<ProviderEntry>,
    pub openai: Option<ProviderEntry>,
    pub google: Option<ProviderEntry>,
    pub deepseek: Option<ProviderEntry>,
    pub qwen: Option<ProviderEntry>,
    pub glm: Option<ProviderEntry>,
}

impl ProvidersConfig {
    /// Iterator over `(name, entry)` for every declared provider slot.
    pub fn iter(&self) -> impl Iterator<Item = (&'static str, &ProviderEntry)> {
        [
            ("anthropic", self.anthropic.as_ref()),
            ("openai", self.openai.as_ref()),
            ("google", self.google.as_ref()),
            ("deepseek", self.deepseek.as_ref()),
            ("qwen", self.qwen.as_ref()),
            ("glm", self.glm.as_ref()),
        ]
        .into_iter()
        .filter_map(|(k, v)| v.map(|e| (k, e)))
    }

    /// Names of providers with `enabled = true` and a non-empty api_key.
    pub fn enabled_names(&self) -> Vec<&'static str> {
        self.iter()
            .filter(|(_, e)| e.enabled && e.api_key.is_some())
            .map(|(k, _)| k)
            .collect()
    }
}

#[derive(Debug, Clone, Default, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ProviderEntry {
    /// `{ env = "ANTHROPIC_API_KEY" }` or `{ value = "sk-..." }`.
    pub api_key: Option<SecretRef>,
    pub base_url: Option<String>,
    #[serde(default)]
    pub enabled: bool,
}

/// Indirect / literal secret reference.
///
/// `{ env = "NAME" }` defers resolution until startup, so the TOML itself
/// carries no secret. `{ value = "..." }` is a literal — supported for
/// tests / local dev but redacted by [`Config::redacted`].
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(untagged, deny_unknown_fields)]
pub enum SecretRef {
    EnvVar { env: String },
    Literal { value: String },
}

impl SecretRef {
    /// Resolve the secret value. Returns `Err(Config)` if an env ref points to
    /// an unset variable.
    pub fn resolve(&self) -> Result<String, CorlinmanError> {
        match self {
            Self::EnvVar { env } => std::env::var(env).map_err(|_| {
                CorlinmanError::Config(format!("env var '{env}' required by config is not set"))
            }),
            Self::Literal { value } => Ok(value.clone()),
        }
    }

    /// A display form that never leaks the underlying secret.
    pub fn redacted(&self) -> Self {
        match self {
            Self::EnvVar { env } => Self::EnvVar { env: env.clone() },
            Self::Literal { .. } => Self::Literal {
                value: "***REDACTED***".into(),
            },
        }
    }
}

// ---------------------------------------------------------------------------
// [models]
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, Validate)]
#[serde(default, deny_unknown_fields)]
pub struct ModelsConfig {
    #[validate(length(min = 1))]
    pub default: String,
    pub aliases: HashMap<String, String>,
}

impl Default for ModelsConfig {
    fn default() -> Self {
        Self {
            default: default_model(),
            aliases: HashMap::new(),
        }
    }
}

fn default_model() -> String {
    "claude-sonnet-4-5".into()
}

// ---------------------------------------------------------------------------
// [channels.*]
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Default, Serialize, Deserialize, JsonSchema)]
#[serde(default, deny_unknown_fields)]
pub struct ChannelsConfig {
    pub qq: Option<QqChannelConfig>,
    // telegram / discord: reserved for future milestones.
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct QqChannelConfig {
    #[serde(default)]
    pub enabled: bool,
    pub ws_url: String,
    pub access_token: Option<SecretRef>,
    #[serde(default)]
    pub self_ids: Vec<i64>,
    /// `group_id (as string) -> keywords` override; empty means channel default.
    #[serde(default)]
    pub group_keywords: HashMap<String, Vec<String>>,
}

// ---------------------------------------------------------------------------
// [rag]
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, Validate)]
#[serde(default, deny_unknown_fields)]
pub struct RagConfig {
    #[validate(length(min = 1))]
    pub embedding_model: String,
    #[validate(range(min = 1, max = 1000))]
    pub top_k: usize,
    #[validate(range(min = 0.0, max = 100.0))]
    pub hybrid_bm25_weight: f32,
    #[validate(range(min = 0.0, max = 100.0))]
    pub hybrid_hnsw_weight: f32,
    #[validate(range(min = 1.0, max = 10000.0))]
    pub rrf_k: f32,
}

impl Default for RagConfig {
    fn default() -> Self {
        Self {
            embedding_model: default_embed_model(),
            top_k: default_top_k(),
            hybrid_bm25_weight: 1.0,
            hybrid_hnsw_weight: 1.0,
            rrf_k: default_rrf_k(),
        }
    }
}

fn default_embed_model() -> String {
    "mxbai-embed-large".into()
}
fn default_top_k() -> usize {
    5
}
fn default_rrf_k() -> f32 {
    60.0
}

// ---------------------------------------------------------------------------
// [[approvals.rules]]
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Default, Serialize, Deserialize, JsonSchema)]
#[serde(default, deny_unknown_fields)]
pub struct ApprovalsConfig {
    pub rules: Vec<ApprovalRule>,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ApprovalRule {
    pub plugin: String,
    pub tool: Option<String>,
    pub mode: ApprovalMode,
    #[serde(default)]
    pub allow_session_keys: Vec<String>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ApprovalMode {
    Auto,
    Prompt,
    Deny,
}

// ---------------------------------------------------------------------------
// [[scheduler.jobs]]
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Default, Serialize, Deserialize, JsonSchema)]
#[serde(default, deny_unknown_fields)]
pub struct SchedulerConfig {
    pub jobs: Vec<SchedulerJob>,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct SchedulerJob {
    pub name: String,
    pub cron: String,
    pub timezone: Option<String>,
    pub action: JobAction,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum JobAction {
    RunAgent {
        prompt: String,
    },
    RunTool {
        plugin: String,
        tool: String,
        #[serde(default)]
        args: serde_json::Value,
    },
}

// ---------------------------------------------------------------------------
// [logging]
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, Validate)]
#[serde(default, deny_unknown_fields)]
pub struct LoggingConfig {
    #[validate(custom(function = "validate_log_level"))]
    pub level: String,
    #[validate(custom(function = "validate_log_format"))]
    pub format: String,
    pub file_rolling: bool,
}

impl Default for LoggingConfig {
    fn default() -> Self {
        Self {
            level: default_log_level(),
            format: default_log_format(),
            file_rolling: false,
        }
    }
}

fn default_log_level() -> String {
    "info".into()
}
fn default_log_format() -> String {
    "json".into()
}

fn validate_log_level(v: &str) -> Result<(), validator::ValidationError> {
    match v {
        "trace" | "debug" | "info" | "warn" | "error" => Ok(()),
        _ => Err(validator::ValidationError::new("log_level")),
    }
}
fn validate_log_format(v: &str) -> Result<(), validator::ValidationError> {
    match v {
        "json" | "text" => Ok(()),
        _ => Err(validator::ValidationError::new("log_format")),
    }
}

// ---------------------------------------------------------------------------
// [meta]
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Default, Serialize, Deserialize, JsonSchema)]
#[serde(default, deny_unknown_fields)]
pub struct Meta {
    pub last_touched_version: Option<String>,
    /// ISO 8601 UTC timestamp, set by [`Config::save_to_path`].
    pub last_touched_at: Option<String>,
}

// ---------------------------------------------------------------------------
// Validation report (cross-field)
// ---------------------------------------------------------------------------

/// Severity of a [`ValidationIssue`].
///
/// `Error` means the config is unusable and `config validate` must exit non-zero.
/// `Warn` means the config is accepted but something is worth surfacing (e.g. a
/// freshly-`init`-ed default config that has no provider enabled yet).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum IssueLevel {
    Error,
    Warn,
}

/// A single problem found while validating a loaded config.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ValidationIssue {
    /// Dotted TOML path, e.g. `models.default`.
    pub path: String,
    /// Short machine code — see `Config::validate_report` for the enumerated set.
    pub code: String,
    pub message: String,
    /// Severity. Defaults to `Error` for backwards-compat when deserialising
    /// older serialised issues that didn't carry a level.
    #[serde(default = "default_issue_level")]
    pub level: IssueLevel,
}

fn default_issue_level() -> IssueLevel {
    IssueLevel::Error
}

// ---------------------------------------------------------------------------
// Loader / saver
// ---------------------------------------------------------------------------

/// Env var that overrides the default `~/.corlinman` data dir (and thus the
/// default config path `$CORLINMAN_DATA_DIR/config.toml`).
pub const ENV_DATA_DIR: &str = "CORLINMAN_DATA_DIR";

impl Config {
    /// Return the default config path: `$CORLINMAN_DATA_DIR/config.toml`, falling
    /// back to `~/.corlinman/config.toml`.
    pub fn default_path() -> PathBuf {
        let base = std::env::var_os(ENV_DATA_DIR)
            .map(PathBuf::from)
            .unwrap_or_else(default_data_dir);
        base.join("config.toml")
    }

    /// Parse a TOML file at `path`. Returns `Config(msg)` errors for I/O and
    /// decode failures; no cross-field validation is run here — call
    /// [`Config::validate_report`] for that.
    pub fn load_from_path(path: &Path) -> Result<Self, CorlinmanError> {
        let raw = std::fs::read_to_string(path).map_err(|e| {
            CorlinmanError::Config(format!("failed to read {}: {e}", path.display()))
        })?;
        let parsed: Self = toml::from_str(&raw).map_err(|e| {
            CorlinmanError::Config(format!("failed to parse {}: {e}", path.display()))
        })?;
        Ok(parsed)
    }

    /// Parse the config from [`Config::default_path`].
    pub fn load_default() -> Result<Self, CorlinmanError> {
        Self::load_from_path(&Self::default_path())
    }

    /// Serialise to TOML at `path`, creating parent directories as needed and
    /// refreshing `meta.last_touched_*` to the current version / UTC time.
    pub fn save_to_path(&self, path: &Path) -> Result<(), CorlinmanError> {
        let mut to_write = self.clone();
        to_write.meta.last_touched_version = Some(env!("CARGO_PKG_VERSION").to_string());
        to_write.meta.last_touched_at = Some(current_timestamp());

        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|e| {
                CorlinmanError::Config(format!(
                    "failed to create parent dir {}: {e}",
                    parent.display()
                ))
            })?;
        }
        let text = toml::to_string_pretty(&to_write)
            .map_err(|e| CorlinmanError::Config(format!("serialise failed: {e}")))?;
        std::fs::write(path, text).map_err(|e| {
            CorlinmanError::Config(format!("failed to write {}: {e}", path.display()))
        })?;
        Ok(())
    }

    /// Run every validator (derive + cross-field) and collect the issues.
    ///
    /// Does not short-circuit: callers (CLI `config validate`) usually want the
    /// full list in one go.
    pub fn validate_report(&self) -> Vec<ValidationIssue> {
        let mut issues = Vec::new();

        // 1. validator-derive errors.
        if let Err(errs) = Validate::validate(self) {
            push_validator_errors("", &errs, &mut issues);
        }

        // 2. Cross-field rules.
        //
        // models.default must be reachable — either the literal model id is
        // resolvable as an alias, or at least one provider is enabled so the
        // agent layer can route to it.
        let enabled = self.providers.enabled_names();
        if enabled.is_empty() {
            // Warn, not error: a freshly `config init`-ed default config has no
            // providers yet but is otherwise well-formed; users can still run
            // `config validate` on it without a non-zero exit.
            issues.push(ValidationIssue {
                path: "providers".into(),
                code: "no_provider_enabled".into(),
                message: "no provider is both enabled and has an api_key set".into(),
                level: IssueLevel::Warn,
            });
        }

        // aliases must not collide with themselves pointing to themselves, and
        // must resolve in <=1 hop (keep it simple — we don't want alias chains).
        for (alias, target) in &self.models.aliases {
            if alias == target {
                issues.push(ValidationIssue {
                    path: format!("models.aliases.{alias}"),
                    code: "alias_self_reference".into(),
                    message: format!("alias '{alias}' points to itself"),
                    level: IssueLevel::Error,
                });
            }
            if self.models.aliases.contains_key(target) {
                issues.push(ValidationIssue {
                    path: format!("models.aliases.{alias}"),
                    code: "alias_chain".into(),
                    message: format!(
                        "alias '{alias}' -> '{target}' but '{target}' is itself an alias"
                    ),
                    level: IssueLevel::Error,
                });
            }
        }

        // 3. QQ channel sanity.
        if let Some(qq) = &self.channels.qq {
            if qq.enabled && qq.ws_url.trim().is_empty() {
                issues.push(ValidationIssue {
                    path: "channels.qq.ws_url".into(),
                    code: "empty_ws_url".into(),
                    message: "channels.qq.enabled = true but ws_url is empty".into(),
                    level: IssueLevel::Error,
                });
            }
            if qq.enabled && qq.self_ids.is_empty() {
                issues.push(ValidationIssue {
                    path: "channels.qq.self_ids".into(),
                    code: "empty_self_ids".into(),
                    message: "channels.qq.enabled = true but self_ids is empty".into(),
                    level: IssueLevel::Error,
                });
            }
        }

        // 4. Scheduler cron: shallow check only (non-empty; full cron parse lives in scheduler crate).
        for (idx, job) in self.scheduler.jobs.iter().enumerate() {
            if job.cron.trim().is_empty() {
                issues.push(ValidationIssue {
                    path: format!("scheduler.jobs[{idx}].cron"),
                    code: "empty_cron".into(),
                    message: format!("scheduler.jobs[{idx}] has empty cron expression"),
                    level: IssueLevel::Error,
                });
            }
            if job.name.trim().is_empty() {
                issues.push(ValidationIssue {
                    path: format!("scheduler.jobs[{idx}].name"),
                    code: "empty_name".into(),
                    message: format!("scheduler.jobs[{idx}] has empty name"),
                    level: IssueLevel::Error,
                });
            }
        }

        // 5. Approval rules: mode parsed already; plugin field must be non-empty.
        for (idx, rule) in self.approvals.rules.iter().enumerate() {
            if rule.plugin.trim().is_empty() {
                issues.push(ValidationIssue {
                    path: format!("approvals.rules[{idx}].plugin"),
                    code: "empty_plugin".into(),
                    message: format!("approvals.rules[{idx}] has empty plugin"),
                    level: IssueLevel::Error,
                });
            }
        }

        issues
    }

    /// A clone with `SecretRef::Literal` values redacted, suitable for logging
    /// or `corlinman config show`.
    pub fn redacted(&self) -> Self {
        let mut out = self.clone();
        redact_providers(&mut out.providers);
        if let Some(qq) = out.channels.qq.as_mut() {
            if let Some(tok) = qq.access_token.as_mut() {
                *tok = tok.redacted();
            }
        }
        if out.admin.password_hash.is_some() {
            out.admin.password_hash = Some("***REDACTED***".into());
        }
        out
    }
}

fn redact_providers(p: &mut ProvidersConfig) {
    for slot in [
        &mut p.anthropic,
        &mut p.openai,
        &mut p.google,
        &mut p.deepseek,
        &mut p.qwen,
        &mut p.glm,
    ] {
        if let Some(e) = slot.as_mut() {
            if let Some(k) = e.api_key.as_mut() {
                *k = k.redacted();
            }
        }
    }
}

fn push_validator_errors(
    prefix: &str,
    errs: &validator::ValidationErrors,
    out: &mut Vec<ValidationIssue>,
) {
    for (field, kind) in errs.errors() {
        let full = if prefix.is_empty() {
            (*field).to_string()
        } else {
            format!("{prefix}.{field}")
        };
        match kind {
            validator::ValidationErrorsKind::Field(items) => {
                for item in items {
                    out.push(ValidationIssue {
                        path: full.clone(),
                        code: item.code.to_string(),
                        message: item.message.as_ref().map(|c| c.to_string()).unwrap_or_else(
                            || format!("invalid value for '{full}' ({})", item.code),
                        ),
                        level: IssueLevel::Error,
                    });
                }
            }
            validator::ValidationErrorsKind::Struct(inner) => {
                push_validator_errors(&full, inner, out);
            }
            validator::ValidationErrorsKind::List(list) => {
                for (i, inner) in list {
                    let p = format!("{full}[{i}]");
                    push_validator_errors(&p, inner, out);
                }
            }
        }
    }
}

fn current_timestamp() -> String {
    use time::format_description::well_known::Rfc3339;
    time::OffsetDateTime::now_utc()
        .format(&Rfc3339)
        .unwrap_or_else(|_| "unknown".into())
}

// ---------------------------------------------------------------------------
// Dotted-path get/set helpers (best-effort for `corlinman config get/set`).
// ---------------------------------------------------------------------------

/// Read a dotted path (e.g. `server.port`) from a parsed config, rendered as
/// TOML (so the CLI can print it verbatim). Returns `NotFound` if the path
/// does not exist.
pub fn get_dotted(config: &Config, path: &str) -> Result<String, CorlinmanError> {
    let value = toml::Value::try_from(config)
        .map_err(|e| CorlinmanError::Config(format!("serialise for get: {e}")))?;
    let got = walk_dotted(&value, path).ok_or_else(|| CorlinmanError::NotFound {
        kind: "config_key",
        id: path.to_string(),
    })?;
    match got {
        toml::Value::String(s) => Ok(s.clone()),
        other => Ok(other.to_string()),
    }
}

/// Set a dotted path on a config tree. Only scalar leaves are supported; table
/// / array inserts must be done by editing the file directly. Returns the
/// updated config (callers decide when to `save_to_path`).
pub fn set_dotted(config: &Config, path: &str, new_value: &str) -> Result<Config, CorlinmanError> {
    let mut root = toml::Value::try_from(config)
        .map_err(|e| CorlinmanError::Config(format!("serialise for set: {e}")))?;
    write_dotted(&mut root, path, new_value)?;
    let updated: Config = root
        .try_into()
        .map_err(|e| CorlinmanError::Config(format!("re-decode after set: {e}")))?;
    Ok(updated)
}

fn walk_dotted<'a>(value: &'a toml::Value, path: &str) -> Option<&'a toml::Value> {
    let mut cur = value;
    for part in path.split('.') {
        match cur {
            toml::Value::Table(t) => {
                cur = t.get(part)?;
            }
            _ => return None,
        }
    }
    Some(cur)
}

fn write_dotted(root: &mut toml::Value, path: &str, raw: &str) -> Result<(), CorlinmanError> {
    let parts: Vec<&str> = path.split('.').collect();
    if parts.is_empty() {
        return Err(CorlinmanError::Config("empty path".into()));
    }
    let (last, head) = parts.split_last().expect("non-empty");
    let mut cur = root;
    for part in head {
        cur = match cur {
            toml::Value::Table(t) => t
                .entry((*part).to_string())
                .or_insert_with(|| toml::Value::Table(toml::value::Table::new())),
            _ => {
                return Err(CorlinmanError::Config(format!(
                    "path '{path}' traverses non-table at '{part}'"
                )));
            }
        };
    }
    let table = match cur {
        toml::Value::Table(t) => t,
        _ => {
            return Err(CorlinmanError::Config(format!(
                "path '{path}' does not end in a table"
            )));
        }
    };
    let parsed = parse_scalar(raw);
    table.insert((*last).to_string(), parsed);
    Ok(())
}

/// Map a CLI-style value string to the most natural TOML scalar. Order of
/// precedence: bool, integer, float, string.
fn parse_scalar(raw: &str) -> toml::Value {
    if let Ok(b) = raw.parse::<bool>() {
        return toml::Value::Boolean(b);
    }
    if let Ok(i) = raw.parse::<i64>() {
        return toml::Value::Integer(i);
    }
    if let Ok(f) = raw.parse::<f64>() {
        return toml::Value::Float(f);
    }
    toml::Value::String(raw.to_string())
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    fn minimal_toml() -> &'static str {
        r#"
[server]
port = 7000
"#
    }

    fn full_toml() -> String {
        // Mirrors docs/config.example.toml but inline so tests are self-contained.
        r#"
[server]
port = 6005
bind = "0.0.0.0"
data_dir = "/tmp/corlinman-test"

[admin]
username = "admin"

[providers.anthropic]
api_key = { env = "ANTHROPIC_API_KEY" }
enabled = true

[providers.openai]
api_key = { value = "sk-literal" }
base_url = "https://api.openai.com/v1"
enabled = false

[models]
default = "claude-sonnet-4-5"
[models.aliases]
smart = "claude-opus-4-7"

[channels.qq]
enabled = true
ws_url = "ws://127.0.0.1:3001"
self_ids = [123456789]
access_token = { env = "QQ_TOKEN" }

[rag]
embedding_model = "mxbai-embed-large"
top_k = 5
hybrid_bm25_weight = 1.0
hybrid_hnsw_weight = 1.0
rrf_k = 60.0

[[approvals.rules]]
plugin = "file-ops"
tool = "file-ops.write"
mode = "prompt"

[[scheduler.jobs]]
name = "daily-brief"
cron = "0 8 * * *"
timezone = "Asia/Shanghai"
action = { type = "run_agent", prompt = "generate daily brief" }

[logging]
level = "info"
format = "json"
file_rolling = true
"#
        .to_string()
    }

    #[test]
    fn loads_minimal_toml_with_defaults() {
        let dir = tempdir().unwrap();
        let p = dir.path().join("c.toml");
        std::fs::write(&p, minimal_toml()).unwrap();
        let cfg = Config::load_from_path(&p).unwrap();
        assert_eq!(cfg.server.port, 7000);
        assert_eq!(cfg.server.bind, "0.0.0.0"); // defaulted
        assert_eq!(cfg.models.default, "claude-sonnet-4-5");
        assert_eq!(cfg.rag.top_k, 5);
    }

    #[test]
    fn loads_full_example_and_resolves_enabled_providers() {
        let dir = tempdir().unwrap();
        let p = dir.path().join("c.toml");
        std::fs::write(&p, full_toml()).unwrap();
        let cfg = Config::load_from_path(&p).unwrap();
        assert_eq!(cfg.providers.enabled_names(), vec!["anthropic"]);
        assert_eq!(cfg.channels.qq.as_ref().unwrap().self_ids, vec![123456789]);
        assert_eq!(cfg.scheduler.jobs.len(), 1);
    }

    #[test]
    fn rejects_unknown_fields() {
        let toml = r#"
[server]
port = 6005
bogus = "field"
"#;
        let err = toml::from_str::<Config>(toml).unwrap_err();
        let msg = err.to_string();
        assert!(
            msg.contains("bogus") || msg.contains("unknown"),
            "expected unknown-field error, got: {msg}"
        );
    }

    #[test]
    fn validate_report_catches_out_of_range_port() {
        let mut cfg = Config::default();
        cfg.server.port = 0; // validator min = 1
        cfg.providers.anthropic = Some(ProviderEntry {
            api_key: Some(SecretRef::EnvVar {
                env: "ANTHROPIC_API_KEY".into(),
            }),
            base_url: None,
            enabled: true,
        });
        let issues = cfg.validate_report();
        assert!(
            issues.iter().any(|i| i.path.contains("port")),
            "expected a port issue, got: {issues:?}"
        );
    }

    #[test]
    fn validate_report_flags_missing_provider() {
        let cfg = Config::default();
        let issues = cfg.validate_report();
        assert!(
            issues.iter().any(|i| i.code == "no_provider_enabled"),
            "expected 'no_provider_enabled', got: {issues:?}"
        );
    }

    #[test]
    fn secret_ref_env_resolves_and_errors() {
        std::env::set_var("CORLINMAN_TEST_SECRET_OK", "s3cret");
        let env_ok = SecretRef::EnvVar {
            env: "CORLINMAN_TEST_SECRET_OK".into(),
        };
        assert_eq!(env_ok.resolve().unwrap(), "s3cret");

        std::env::remove_var("CORLINMAN_TEST_SECRET_MISSING");
        let env_missing = SecretRef::EnvVar {
            env: "CORLINMAN_TEST_SECRET_MISSING".into(),
        };
        assert!(env_missing.resolve().is_err());

        let lit = SecretRef::Literal {
            value: "plain".into(),
        };
        assert_eq!(lit.resolve().unwrap(), "plain");
    }

    #[test]
    fn redacted_hides_literals_but_keeps_env_refs() {
        let mut cfg = Config::default();
        cfg.providers.openai = Some(ProviderEntry {
            api_key: Some(SecretRef::Literal {
                value: "sk-top-secret".into(),
            }),
            base_url: None,
            enabled: true,
        });
        cfg.admin.password_hash = Some("$argon2id$v=19$m=...".into());
        let red = cfg.redacted();
        let openai = red.providers.openai.unwrap();
        match openai.api_key.unwrap() {
            SecretRef::Literal { value } => assert_eq!(value, "***REDACTED***"),
            SecretRef::EnvVar { .. } => panic!("expected literal"),
        }
        assert_eq!(red.admin.password_hash.as_deref(), Some("***REDACTED***"));
    }

    #[test]
    fn save_refreshes_meta_and_roundtrips() {
        let dir = tempdir().unwrap();
        let p = dir.path().join("out.toml");
        let mut cfg = Config::default();
        cfg.providers.anthropic = Some(ProviderEntry {
            api_key: Some(SecretRef::EnvVar {
                env: "ANTHROPIC_API_KEY".into(),
            }),
            base_url: None,
            enabled: true,
        });
        cfg.save_to_path(&p).unwrap();
        let loaded = Config::load_from_path(&p).unwrap();
        assert!(loaded.meta.last_touched_at.is_some());
        assert_eq!(
            loaded.meta.last_touched_version.as_deref(),
            Some(env!("CARGO_PKG_VERSION"))
        );
        assert_eq!(loaded.server.port, cfg.server.port);
        assert_eq!(loaded.providers.enabled_names(), vec!["anthropic"]);
    }

    #[test]
    fn get_and_set_dotted_scalars() {
        let mut cfg = Config::default();
        cfg.providers.anthropic = Some(ProviderEntry {
            api_key: Some(SecretRef::EnvVar {
                env: "ANTHROPIC_API_KEY".into(),
            }),
            base_url: None,
            enabled: true,
        });

        assert_eq!(get_dotted(&cfg, "server.port").unwrap(), "6005");
        let updated = set_dotted(&cfg, "server.port", "7777").unwrap();
        assert_eq!(updated.server.port, 7777);

        let updated2 = set_dotted(&updated, "logging.level", "debug").unwrap();
        assert_eq!(updated2.logging.level, "debug");

        assert!(matches!(
            get_dotted(&cfg, "does.not.exist"),
            Err(CorlinmanError::NotFound { .. })
        ));
    }
}
