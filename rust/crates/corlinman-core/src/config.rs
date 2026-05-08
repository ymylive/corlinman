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

use std::collections::{BTreeMap, HashMap};
use std::path::{Path, PathBuf};

use corlinman_evolution::EvolutionKind;
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use validator::Validate;

use crate::error::CorlinmanError;

/// Per-entity parameter map.
///
/// Provider-level defaults, per-alias overrides, and per-request overrides all
/// use this shape. Values are `serde_json::Value` so a schema-driven UI can
/// round-trip arbitrary JSON scalars / objects and so the Python side can
/// validate them against the provider's declared JSON Schema. `BTreeMap` is
/// used (not `HashMap`) so the TOML serialiser emits stable key order.
///
/// `serde_json::Value::Null` is not representable in TOML — callers should
/// omit optional fields rather than storing `null`.
pub type ParamsMap = BTreeMap<String, serde_json::Value>;

// ---------------------------------------------------------------------------
// Top-level config
// ---------------------------------------------------------------------------

/// Root TOML schema. Every sub-section defaults to its `Default` so a near-empty
/// `config.toml` is still loadable.
///
/// `Default::default()` is hand-written (not derived) so a fresh install
/// still seeds a single `[providers.openai]` entry with `kind = "openai"`,
/// matching the documented onboarding flow ("export OPENAI_API_KEY and
/// you're done"). All other fields fall through to their per-section
/// defaults.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, Validate)]
#[serde(default, deny_unknown_fields)]
pub struct Config {
    #[validate(nested)]
    pub server: ServerConfig,
    #[validate(nested)]
    pub admin: AdminConfig,
    pub providers: ProvidersConfig,
    #[validate(nested)]
    pub models: ModelsConfig,
    /// Optional embedding provider binding. Absent = embedding disabled
    /// (RAG dense / rerank stages gracefully degrade to BM25-only).
    pub embedding: Option<EmbeddingConfig>,
    pub channels: ChannelsConfig,
    #[validate(nested)]
    pub rag: RagConfig,
    pub approvals: ApprovalsConfig,
    pub scheduler: SchedulerConfig,
    #[validate(nested)]
    pub logging: LoggingConfig,
    // --- B1-BE4 additions; each defaults so existing configs still parse. ---
    #[serde(default)]
    #[validate(nested)]
    pub hooks: HooksConfig,
    #[serde(default)]
    #[validate(nested)]
    pub skills: SkillsConfig,
    #[serde(default)]
    #[validate(nested)]
    pub variables: VariablesConfig,
    #[serde(default)]
    #[validate(nested)]
    pub agents: AgentsConfig,
    #[serde(default)]
    pub tools: ToolsConfig,
    #[serde(default)]
    pub telegram: TelegramConfig,
    #[serde(default)]
    pub vector: VectorConfig,
    #[serde(default)]
    #[validate(nested)]
    pub wstool: WsToolConfig,
    #[serde(default)]
    #[validate(nested)]
    pub canvas: CanvasConfig,
    #[serde(default)]
    #[validate(nested)]
    pub nodebridge: NodeBridgeConfig,
    #[serde(default)]
    #[validate(nested)]
    pub evolution: EvolutionConfig,
    /// Phase 3 W3-A: chunk-decay + consolidation knobs. Defaults are
    /// already useful (decay on / consolidation on with 05:00 UTC
    /// schedule), so an unset section deserialises into the documented
    /// shape.
    #[serde(default)]
    #[validate(nested)]
    pub memory: MemoryConfig,
    /// Phase 3 W3-C: persona decay knobs. Pure config — the runtime
    /// state lives in `agent_state.sqlite`, owned by the Python
    /// `corlinman-persona` package; this struct only mirrors the TOML
    /// shape so `docs/config.example.toml` round-trips through serde.
    #[serde(default)]
    pub persona: PersonaConfig,
    /// Phase 4 W1 4-1A: multi-tenant boundary. Defaults to
    /// `enabled = false` so existing single-tenant deployments parse
    /// any pre-Phase-4 config unchanged and behave exactly as before.
    #[serde(default)]
    pub tenants: TenantsConfig,
    pub meta: Meta,
}

impl Default for Config {
    fn default() -> Self {
        // Seed a single disabled `[providers.openai]` entry so a fresh
        // `config init` round-trips through serde with a recognisable
        // demo entry. The entry is disabled and api-key-less by default
        // (the validator emits a `no_provider_enabled` warning until the
        // operator flips `enabled = true` and supplies an api_key).
        let mut providers = ProvidersConfig::default();
        providers.insert(
            "openai",
            ProviderEntry {
                kind: Some(ProviderKind::Openai),
                api_key: None,
                base_url: None,
                enabled: false,
                params: ParamsMap::new(),
            },
        );
        Self {
            server: ServerConfig::default(),
            admin: AdminConfig::default(),
            providers,
            models: ModelsConfig::default(),
            embedding: None,
            channels: ChannelsConfig::default(),
            rag: RagConfig::default(),
            approvals: ApprovalsConfig::default(),
            scheduler: SchedulerConfig::default(),
            logging: LoggingConfig::default(),
            hooks: HooksConfig::default(),
            skills: SkillsConfig::default(),
            variables: VariablesConfig::default(),
            agents: AgentsConfig::default(),
            tools: ToolsConfig::default(),
            telegram: TelegramConfig::default(),
            vector: VectorConfig::default(),
            wstool: WsToolConfig::default(),
            canvas: CanvasConfig::default(),
            nodebridge: NodeBridgeConfig::default(),
            evolution: EvolutionConfig::default(),
            memory: MemoryConfig::default(),
            persona: PersonaConfig::default(),
            tenants: TenantsConfig::default(),
            meta: Meta::default(),
        }
    }
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

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, Validate)]
#[serde(default, deny_unknown_fields)]
pub struct AdminConfig {
    pub username: Option<String>,
    /// argon2id hash string (`$argon2id$...`). Never a raw password.
    pub password_hash: Option<String>,
    /// Allow-list of `decided_by` values (typically the admin username) that
    /// are permitted to approve / apply meta-evolution proposals
    /// (`MetaWeights`, `MetaTemplate`, `MetaPolicy`). Empty (the default)
    /// means no one can approve meta — operators MUST explicitly opt in.
    /// Kept as a flat string list rather than a new role so existing
    /// authn/authz layers (basic-auth admin user, future SSO subjects) can
    /// drop their identifier in without schema churn.
    #[serde(default)]
    pub meta_approver_users: Vec<String>,
}

impl Default for AdminConfig {
    fn default() -> Self {
        Self {
            username: None,
            password_hash: None,
            meta_approver_users: Vec::new(),
        }
    }
}

// ---------------------------------------------------------------------------
// [providers.*]
// ---------------------------------------------------------------------------

/// Operator-chosen named provider entries.
///
/// Keys are free-form (operators pick them: `[providers.siliconflow]`,
/// `[providers.my-vllm]`, `[providers.openrouter]`, …). Each entry MUST
/// declare an explicit `kind = "..."` discriminator unless the chosen name
/// is a first-party slot name (`anthropic` / `openai` / `google` /
/// `deepseek` / `qwen` / `glm`) — those names continue to infer the kind so
/// pre-refactor configs round-trip unchanged.
///
/// Backed by a [`BTreeMap`] so the TOML serialiser emits stable key order
/// and so any number of `kind = "openai_compatible"` entries (or any other
/// kind) can coexist under different names.
#[derive(Debug, Clone, Default, Serialize, Deserialize, JsonSchema)]
#[serde(transparent)]
pub struct ProvidersConfig(pub BTreeMap<String, ProviderEntry>);

impl ProvidersConfig {
    /// Iterator over `(name, entry)` for every declared provider entry.
    /// Borrowed names are tied to the map's storage, not `'static`.
    pub fn iter(&self) -> impl Iterator<Item = (&str, &ProviderEntry)> {
        self.0.iter().map(|(k, v)| (k.as_str(), v))
    }

    /// Mutable iterator (used by [`Config::redacted`] and the admin upsert
    /// path that needs to flip a single entry's `enabled` flag).
    pub fn iter_mut(&mut self) -> impl Iterator<Item = (&str, &mut ProviderEntry)> {
        self.0.iter_mut().map(|(k, v)| (k.as_str(), v))
    }

    /// Look up a single entry by name.
    pub fn get(&self, name: &str) -> Option<&ProviderEntry> {
        self.0.get(name)
    }

    /// Mutable accessor.
    pub fn get_mut(&mut self, name: &str) -> Option<&mut ProviderEntry> {
        self.0.get_mut(name)
    }

    /// Insert or replace an entry. Returns the displaced entry, if any.
    pub fn insert(
        &mut self,
        name: impl Into<String>,
        entry: ProviderEntry,
    ) -> Option<ProviderEntry> {
        self.0.insert(name.into(), entry)
    }

    /// Remove an entry. Returns the removed entry, if any.
    pub fn remove(&mut self, name: &str) -> Option<ProviderEntry> {
        self.0.remove(name)
    }

    /// True iff an entry exists for `name`.
    pub fn contains_key(&self, name: &str) -> bool {
        self.0.contains_key(name)
    }

    /// Number of declared entries (for tests / metrics).
    pub fn len(&self) -> usize {
        self.0.len()
    }

    /// True iff no entries are declared.
    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }

    /// Names of providers with `enabled = true` and a non-empty api_key.
    /// Sorted alphabetically (BTreeMap order) so the admin UI / docs stay
    /// stable across reloads.
    pub fn enabled_names(&self) -> Vec<String> {
        self.iter()
            .filter(|(_, e)| e.enabled && e.api_key.is_some())
            .map(|(k, _)| k.to_string())
            .collect()
    }

    /// Resolve the kind for an entry, honouring the explicit `kind` field
    /// first and falling back to inferring from a well-known first-party
    /// slot name so legacy configs without `kind` still load.
    pub fn kind_for(&self, name: &str, entry: &ProviderEntry) -> Option<ProviderKind> {
        entry.kind.or_else(|| ProviderKind::from_slot_name(name))
    }
}

impl std::ops::Deref for ProvidersConfig {
    type Target = BTreeMap<String, ProviderEntry>;
    fn deref(&self) -> &Self::Target {
        &self.0
    }
}

impl std::ops::DerefMut for ProvidersConfig {
    fn deref_mut(&mut self) -> &mut Self::Target {
        &mut self.0
    }
}

impl<S: Into<String>> FromIterator<(S, ProviderEntry)> for ProvidersConfig {
    fn from_iter<I: IntoIterator<Item = (S, ProviderEntry)>>(iter: I) -> Self {
        Self(iter.into_iter().map(|(k, v)| (k.into(), v)).collect())
    }
}

#[derive(Debug, Clone, Default, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ProviderEntry {
    /// Provider discriminator. `None` on legacy configs; callers should use
    /// [`ProvidersConfig::kind_for`] which falls back to inferring the kind
    /// from the slot name for first-party providers (feature-c backward
    /// compatibility).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub kind: Option<ProviderKind>,
    /// `{ env = "ANTHROPIC_API_KEY" }` or `{ value = "sk-..." }`.
    pub api_key: Option<SecretRef>,
    pub base_url: Option<String>,
    #[serde(default)]
    pub enabled: bool,
    /// Provider-level default params. Merged under alias.params / request
    /// params before being forwarded to the SDK.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub params: ParamsMap,
}

/// Provider kind discriminator.
///
/// First-party kinds (Anthropic / Openai / Google / Deepseek / Qwen / Glm)
/// have bespoke Python adapters. The remaining kinds — `openai_compatible`
/// plus the seven market kinds added in the free-form-providers refactor
/// (Mistral / Cohere / Together / Groq / Replicate / Bedrock / Azure) — all
/// speak the OpenAI wire format and run through the shared
/// `OpenAICompatibleProvider` Python adapter at runtime. They are surfaced
/// as first-class kinds (instead of "use openai_compatible with a hack")
/// so the admin UI can show them as named choices, configs document the
/// operator's intent, and per-kind quirks (Bedrock SigV4 auth, Azure
/// deployment IDs, etc.) can land later as adapter overrides without a
/// schema change.
///
/// `Bedrock` and `Azure` are declared but the Python runtime currently
/// raises `NotImplementedError` when one is used — a follow-up iteration
/// will wire SigV4 / deployment-routing properly. Operators who need them
/// today should use `kind = "openai_compatible"` with an explicit
/// `base_url` that points at a compatible proxy.
///
/// Wire format is the lowercase snake_case of the variant.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum ProviderKind {
    Anthropic,
    Openai,
    Google,
    Deepseek,
    Qwen,
    Glm,
    OpenaiCompatible,
    /// Mistral La Plateforme (`api.mistral.ai`). OpenAI-wire-format under
    /// the hood; the dedicated kind exists so operators can declare intent.
    Mistral,
    /// Cohere — primarily exposed via its OpenAI-compat endpoint.
    Cohere,
    /// Together AI (`api.together.xyz`) — pure OpenAI-compat.
    Together,
    /// Groq Cloud (`api.groq.com/openai/v1`) — pure OpenAI-compat.
    Groq,
    /// Replicate — OpenAI-compat predictions endpoint.
    Replicate,
    /// AWS Bedrock. OpenAI-compat at the wire level when fronted by a
    /// SigV4-capable proxy. Real adapter pending — runtime currently
    /// raises NotImplementedError.
    Bedrock,
    /// Azure OpenAI Service. Wire-compatible with OpenAI but routes through
    /// `<resource>.openai.azure.com/openai/deployments/<deployment>`.
    /// Real adapter pending — runtime currently raises NotImplementedError.
    Azure,
}

impl ProviderKind {
    /// Lowercase wire name (matches `#[serde(rename_all = "snake_case")]`).
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Anthropic => "anthropic",
            Self::Openai => "openai",
            Self::Google => "google",
            Self::Deepseek => "deepseek",
            Self::Qwen => "qwen",
            Self::Glm => "glm",
            Self::OpenaiCompatible => "openai_compatible",
            Self::Mistral => "mistral",
            Self::Cohere => "cohere",
            Self::Together => "together",
            Self::Groq => "groq",
            Self::Replicate => "replicate",
            Self::Bedrock => "bedrock",
            Self::Azure => "azure",
        }
    }

    /// Every variant in declaration order. Used by the admin router to
    /// populate the "Add provider" modal's kind dropdown without manually
    /// keeping a parallel list in sync.
    pub fn all() -> &'static [Self] {
        &[
            Self::Anthropic,
            Self::Openai,
            Self::Google,
            Self::Deepseek,
            Self::Qwen,
            Self::Glm,
            Self::OpenaiCompatible,
            Self::Mistral,
            Self::Cohere,
            Self::Together,
            Self::Groq,
            Self::Replicate,
            Self::Bedrock,
            Self::Azure,
        ]
    }

    /// Infer a kind from a well-known first-party provider slot name.
    /// Returns `None` for unknown names — operators using a free-form name
    /// (`siliconflow`, `my-vllm`, `openrouter`, …) must set `kind = "..."`
    /// explicitly. The validator surfaces a friendly error pointing at the
    /// offending entry name when this returns `None` and the entry has no
    /// explicit `kind`.
    pub fn from_slot_name(name: &str) -> Option<Self> {
        match name {
            "anthropic" => Some(Self::Anthropic),
            "openai" => Some(Self::Openai),
            "google" => Some(Self::Google),
            "deepseek" => Some(Self::Deepseek),
            "qwen" => Some(Self::Qwen),
            "glm" => Some(Self::Glm),
            _ => None,
        }
    }
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
    /// alias → entry. Values accept either a shorthand string
    /// (`smart = "claude-opus-4-7"`) or a full table with `provider` /
    /// `model` / `params`. See [`AliasEntry`].
    pub aliases: HashMap<String, AliasEntry>,
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

/// One alias entry. Accepts two TOML shapes:
///
/// - **Shorthand**: `smart = "claude-opus-4-7"` — rewrites the requested model
///   string to the literal target. Provider inferred from target at call time;
///   no per-alias params.
/// - **Full**: `[models.aliases.smart]\n model = "claude-opus-4-7"\n
///   provider = "anthropic"\n params = { temperature = 0.7 }` — provider
///   explicit, optional per-alias params merged into the reasoning loop.
///
/// Stored as an untagged enum so existing configs keep working. Use
/// [`AliasEntry::target`] / [`AliasEntry::params`] / [`AliasEntry::provider`]
/// in call sites that just want the resolved values.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(untagged)]
pub enum AliasEntry {
    Shorthand(String),
    Full(AliasSpec),
}

/// Full-form alias entry. See [`AliasEntry`] for the shorthand variant.
#[derive(Debug, Clone, Default, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct AliasSpec {
    /// Upstream model id (e.g. `"claude-opus-4-7"`).
    pub model: String,
    /// Optional explicit provider slot. When absent, the resolver falls back
    /// to the legacy model-prefix table (Python side).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub provider: Option<String>,
    /// Per-alias param overrides. Merged over the provider-level defaults.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub params: ParamsMap,
}

impl AliasEntry {
    /// The upstream model id this alias resolves to.
    pub fn target(&self) -> &str {
        match self {
            Self::Shorthand(s) => s.as_str(),
            Self::Full(spec) => spec.model.as_str(),
        }
    }

    /// The configured provider slot, if any (only set on the full form).
    pub fn provider(&self) -> Option<&str> {
        match self {
            Self::Shorthand(_) => None,
            Self::Full(spec) => spec.provider.as_deref(),
        }
    }

    /// Per-alias param overrides (empty for the shorthand form).
    pub fn params(&self) -> &ParamsMap {
        static EMPTY: once_cell::sync::Lazy<ParamsMap> = once_cell::sync::Lazy::new(ParamsMap::new);
        match self {
            Self::Shorthand(_) => &EMPTY,
            Self::Full(spec) => &spec.params,
        }
    }
}

impl Default for AliasEntry {
    fn default() -> Self {
        Self::Shorthand(String::new())
    }
}

impl From<String> for AliasEntry {
    fn from(s: String) -> Self {
        Self::Shorthand(s)
    }
}

impl From<&str> for AliasEntry {
    fn from(s: &str) -> Self {
        Self::Shorthand(s.to_string())
    }
}

// ---------------------------------------------------------------------------
// [embedding]
// ---------------------------------------------------------------------------

/// Embedding provider binding. One embedder is active at a time; absent
/// section / `enabled = false` degrades RAG to BM25-only.
///
/// `provider` references a key under `[providers.*]`; the Python side asserts
/// the referenced provider is capable of embedding (OpenAI-kind providers
/// and `openai_compatible` usually are; Anthropic is not).
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct EmbeddingConfig {
    /// Provider slot name under `[providers.*]`.
    pub provider: String,
    /// Upstream embedding model id (e.g. `"text-embedding-3-small"`).
    pub model: String,
    /// Declared output dimension; asserted on first successful call so a
    /// mid-life model swap can't silently break stored vectors.
    pub dimension: u32,
    /// Master switch. `true` by default — set to `false` to keep the
    /// section around for reference while disabling dense retrieval.
    #[serde(default = "default_true")]
    pub enabled: bool,
    /// Provider-specific request params (e.g. `encoding_format`).
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub params: ParamsMap,
}

fn default_true() -> bool {
    true
}

// ---------------------------------------------------------------------------
// [channels.*]
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Default, Serialize, Deserialize, JsonSchema)]
#[serde(default, deny_unknown_fields)]
pub struct ChannelsConfig {
    pub qq: Option<QqChannelConfig>,
    pub telegram: Option<TelegramChannelConfig>,
    // discord: reserved for future milestones.
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
    /// Per-group / per-sender token-bucket rate limits. Absent section =
    /// built-in defaults (20/min per group, 5/min per sender); `None` on a
    /// field disables that dimension.
    #[serde(default)]
    pub rate_limit: QqRateLimit,
    /// Base URL of NapCat's webui HTTP API used for scan-login + quick-login
    /// proxying from `/admin/channels/qq/qrcode*` / `/accounts` /
    /// `/quick-login`. `None` means scan-login is disabled (the admin UI
    /// shows "NapCat not configured" and the routes return 503). A typical
    /// local value is `http://127.0.0.1:6099` (NapCat webui default).
    #[serde(default)]
    pub napcat_url: Option<String>,
    /// Optional Bearer token forwarded as `Authorization: Bearer <token>`
    /// on every NapCat webui call. Resolved like other [`SecretRef`]s so
    /// operators can keep it in an env var.
    #[serde(default)]
    pub napcat_access_token: Option<SecretRef>,
}

/// QQ channel rate-limit knobs.
///
/// `group_per_min` / `sender_per_min` map 1:1 onto token buckets inside
/// `corlinman-channels::rate_limit::TokenBucket`:
/// - `Some(n)`: capacity = n, refill = n/60 tokens per second.
/// - `None`: that dimension is disabled (no check performed).
///
/// Both default to conservative values that match the original qqBot.js
/// behaviour of "chatty but not spammy".
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct QqRateLimit {
    #[serde(default)]
    pub group_per_min: Option<u32>,
    #[serde(default)]
    pub sender_per_min: Option<u32>,
}

impl Default for QqRateLimit {
    fn default() -> Self {
        Self {
            group_per_min: Some(20),
            sender_per_min: Some(5),
        }
    }
}

/// Telegram Bot API adapter config (S4 T4, optional).
///
/// We talk to `api.telegram.org` over bare HTTPS — `getUpdates` long-poll
/// inbound, `sendMessage` outbound. See
/// `corlinman_channels::telegram::run_telegram_channel`.
///
/// `bot_token` accepts the same `SecretRef` forms as provider api_keys:
/// `{ env = "TELEGRAM_BOT_TOKEN" }` (preferred) or `{ value = "123:abc" }`.
#[derive(Debug, Clone, Default, Serialize, Deserialize, JsonSchema)]
#[serde(default, deny_unknown_fields)]
pub struct TelegramChannelConfig {
    pub enabled: bool,
    pub bot_token: Option<SecretRef>,
    /// Whitelist of chat ids (group or private). Empty list = all chats allowed.
    pub allowed_chat_ids: Vec<i64>,
    /// Substring keyword filter applied to non-mention group messages.
    /// Case-insensitive. Empty list = no filter (dispatch-all in groups).
    pub keyword_filter: Vec<String>,
    /// When true, group messages are only forwarded if the bot is @mentioned
    /// (mention / text_mention entity targeting the bot). Private chats are
    /// unaffected.
    pub require_mention_in_groups: bool,
    /// Token-bucket rate limits; shape mirrors [`QqRateLimit`].
    pub rate_limit: TelegramRateLimit,
}

/// Telegram channel rate-limit knobs. Shape matches [`QqRateLimit`] so the
/// same `TokenBucket` primitive can be reused.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct TelegramRateLimit {
    #[serde(default)]
    pub chat_per_min: Option<u32>,
    #[serde(default)]
    pub sender_per_min: Option<u32>,
}

impl Default for TelegramRateLimit {
    fn default() -> Self {
        Self {
            chat_per_min: Some(20),
            sender_per_min: Some(5),
        }
    }
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
    /// Cross-encoder rerank stage applied after RRF fusion (Sprint 3 T6).
    /// Disabled by default — flip `enabled = true` to hand the fused
    /// candidates to the Python embedding service's rerank client.
    pub rerank: RerankConfig,
    /// Sprint 9 T-B3-BE5: feature flag for EPA re-ranking boost.
    /// When `false` (default) the hybrid searcher's RRF output is
    /// byte-identical to pre-B3-BE5 behaviour. Flip to `true` to
    /// multiply each candidate's fused score by the `dynamic_boost`
    /// derived from its stored `chunk_epa` row (if present).
    #[serde(default)]
    pub epa_enabled: bool,
    /// Base multiplier fed into `dynamic_boost`. `1.0` keeps the boost
    /// centred at the unclamped formula; larger values bias toward the
    /// ceiling of `epa_boost_range`.
    #[serde(default = "default_epa_base_tag_boost")]
    #[validate(range(min = 0.0, max = 100.0))]
    pub epa_base_tag_boost: f32,
    /// Clamp range for the final boost factor `[min, max]`. Defaults
    /// to `[0.5, 2.5]` — i.e. at most a 5× swing between the worst and
    /// best chunks under the same RRF baseline.
    #[serde(default = "default_epa_boost_range")]
    pub epa_boost_range: [f32; 2],
}

impl Default for RagConfig {
    fn default() -> Self {
        Self {
            embedding_model: default_embed_model(),
            top_k: default_top_k(),
            hybrid_bm25_weight: 1.0,
            hybrid_hnsw_weight: 1.0,
            rrf_k: default_rrf_k(),
            rerank: RerankConfig::default(),
            epa_enabled: false,
            epa_base_tag_boost: default_epa_base_tag_boost(),
            epa_boost_range: default_epa_boost_range(),
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
fn default_epa_base_tag_boost() -> f32 {
    1.0
}
fn default_epa_boost_range() -> [f32; 2] {
    [0.5, 2.5]
}

/// Cross-encoder rerank configuration.
///
/// `mode = "local"` runs a sentence-transformers cross-encoder in the Python
/// embedding service (requires its `[local]` extra). `mode = "remote"` POSTs
/// to a cohere/siliconflow-style rerank endpoint (`api_base` + `api_key`).
///
/// When `enabled = false` (the default) the rest of the fields are ignored;
/// the searcher uses the noop reranker and RRF ordering is returned as-is.
#[derive(Debug, Clone, Default, Serialize, Deserialize, JsonSchema)]
#[serde(default, deny_unknown_fields)]
pub struct RerankConfig {
    /// Master switch. `false` (default) ⇒ noop reranker.
    pub enabled: bool,
    /// Backend selector. See [`RerankMode`].
    pub mode: RerankMode,
    /// Model id passed to the reranker provider
    /// (e.g. `BAAI/bge-reranker-v2-m3` for local, `rerank-multilingual-v3.0`
    /// for remote). `None` ⇒ provider default.
    pub model: Option<String>,
    /// Base URL for `mode = "remote"` (e.g. `https://api.siliconflow.cn/v1`).
    /// Ignored for `mode = "local"`.
    pub api_base: Option<String>,
    /// API key for `mode = "remote"`. Ignored for `mode = "local"`.
    pub api_key: Option<SecretRef>,
}

/// Which rerank backend the embedding service should use.
#[derive(Debug, Clone, Copy, Default, Serialize, Deserialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum RerankMode {
    /// Local sentence-transformers cross-encoder running in the Python
    /// embedding service. Requires the `[local]` extra.
    #[default]
    Local,
    /// Remote HTTP rerank endpoint (cohere / siliconflow / OpenAI-compat).
    Remote,
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
    /// Spawn an external program. Used by Phase 2 wave 2-B to schedule the
    /// Python `corlinman-evolution-engine run-once` CLI as a daily job.
    ///
    /// `command` is resolved via `PATH` unless absolute. `args` defaults to
    /// empty. `timeout_secs` defaults to 600 (10 min); the runtime hard-kills
    /// the child once the deadline elapses. `working_dir` is optional;
    /// defaults to the gateway's CWD when unset. `env` is a flat map merged
    /// over the inherited environment so configs can pin DB paths without
    /// exporting them globally.
    Subprocess {
        command: String,
        #[serde(default)]
        args: Vec<String>,
        #[serde(default = "default_subprocess_timeout_secs")]
        timeout_secs: u64,
        #[serde(default)]
        working_dir: Option<std::path::PathBuf>,
        #[serde(default)]
        env: std::collections::BTreeMap<String, String>,
    },
}

fn default_subprocess_timeout_secs() -> u64 {
    600
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
    /// File-sink configuration for the gateway. When `file.path` is empty
    /// the gateway stays stdout-only (back-compat with pre-P0-1 configs).
    #[serde(default)]
    pub file: FileLoggingConfig,
}

impl Default for LoggingConfig {
    fn default() -> Self {
        Self {
            level: default_log_level(),
            format: default_log_format(),
            file_rolling: false,
            file: FileLoggingConfig::default(),
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

/// File-sink configuration for the gateway (`[logging.file]`).
///
/// Consumed by `corlinman-gateway::telemetry` to wire a
/// `tracing_appender::rolling::RollingFileAppender` alongside the existing
/// stdout layer. Every field defaults so old `corlinman.toml` files that
/// omit `[logging.file]` entirely still parse.
///
/// Semantics:
///
/// * `path` is the primary active log file. When empty the file sink is
///   disabled (gateway stays stdout-only).
/// * `max_size_mb` is an advisory ceiling used by the doctor diagnostics
///   and retention task. The rolling appender rotates on wall-clock time,
///   not size.
/// * `retention_days` bounds how long old rotated files are kept. A
///   background task scans the parent directory hourly and deletes
///   mtime-older-than-`retention_days` entries.
/// * `rotation` picks the appender cadence (`daily` | `hourly` |
///   `minutely` | `never`).
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(default, deny_unknown_fields)]
pub struct FileLoggingConfig {
    #[serde(default = "default_log_file_path")]
    pub path: PathBuf,
    #[serde(default = "default_log_max_size_mb")]
    pub max_size_mb: u64,
    #[serde(default = "default_log_retention_days")]
    pub retention_days: u32,
    #[serde(default)]
    pub rotation: RotationKind,
}

impl Default for FileLoggingConfig {
    fn default() -> Self {
        Self {
            path: default_log_file_path(),
            max_size_mb: default_log_max_size_mb(),
            retention_days: default_log_retention_days(),
            rotation: RotationKind::default(),
        }
    }
}

fn default_log_file_path() -> PathBuf {
    PathBuf::from("/data/logs/gateway.log")
}
fn default_log_max_size_mb() -> u64 {
    5
}
fn default_log_retention_days() -> u32 {
    7
}

/// Rotation cadence for the file appender.
///
/// Matches `tracing_appender::rolling::Rotation` variants 1:1 so the
/// gateway can map without an extra lookup table. `Never` disables
/// rotation (single ever-growing file — useful in tests / dev).
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "lowercase")]
pub enum RotationKind {
    #[default]
    Daily,
    Hourly,
    Minutely,
    Never,
}

// ---------------------------------------------------------------------------
// [hooks] — in-process hook bus (B1-BE4).
// ---------------------------------------------------------------------------

/// Capacity + master switch for the in-process hook bus. Consumers (skills,
/// agents, plugins) register synchronous handlers; `capacity` caps the bounded
/// fan-out queue. When `enabled = false` handlers are still registered but the
/// bus short-circuits dispatch.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, Validate)]
#[serde(default, deny_unknown_fields)]
pub struct HooksConfig {
    #[validate(range(min = 1, max = 1_048_576))]
    pub capacity: usize,
    pub enabled: bool,
}

impl Default for HooksConfig {
    fn default() -> Self {
        Self {
            capacity: 1024,
            enabled: true,
        }
    }
}

// ---------------------------------------------------------------------------
// [skills] — filesystem-loaded skill bundles (B1-BE4).
// ---------------------------------------------------------------------------

/// Skills are discovered by walking `dir` relative to the data_dir. With
/// `autoload = true` the runtime indexes them at startup; otherwise they must
/// be requested explicitly.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, Validate)]
#[serde(default, deny_unknown_fields)]
pub struct SkillsConfig {
    #[validate(length(min = 1))]
    pub dir: String,
    pub autoload: bool,
}

impl Default for SkillsConfig {
    fn default() -> Self {
        Self {
            dir: "skills".into(),
            autoload: true,
        }
    }
}

// ---------------------------------------------------------------------------
// [variables] — TVStxt variable stores (tar/var/sar/fixed).
// ---------------------------------------------------------------------------

/// Four on-disk variable stores used by the placeholder engine. Paths are
/// resolved relative to the data_dir. `hot_reload = true` makes the runtime
/// watch the directories and reload on change.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, Validate)]
#[serde(default, deny_unknown_fields)]
pub struct VariablesConfig {
    #[validate(length(min = 1))]
    pub tar_dir: String,
    #[validate(length(min = 1))]
    pub var_dir: String,
    #[validate(length(min = 1))]
    pub sar_dir: String,
    #[validate(length(min = 1))]
    pub fixed_dir: String,
    pub hot_reload: bool,
}

impl Default for VariablesConfig {
    fn default() -> Self {
        Self {
            tar_dir: "TVStxt/tar".into(),
            var_dir: "TVStxt/var".into(),
            sar_dir: "TVStxt/sar".into(),
            fixed_dir: "TVStxt/fixed".into(),
            hot_reload: true,
        }
    }
}

// ---------------------------------------------------------------------------
// [agents] — character-card / {{角色}} registry (B1-BE4).
// ---------------------------------------------------------------------------

/// Agents live under `dir` relative to data_dir. `single_agent_gate = true`
/// enforces classic "first expansion wins" — the first agent invocation in
/// a turn gates subsequent expansions in the same prompt.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, Validate)]
#[serde(default, deny_unknown_fields)]
pub struct AgentsConfig {
    #[validate(length(min = 1))]
    pub dir: String,
    pub single_agent_gate: bool,
}

impl Default for AgentsConfig {
    fn default() -> Self {
        Self {
            dir: "agents".into(),
            single_agent_gate: true,
        }
    }
}

// ---------------------------------------------------------------------------
// [tools] + [tools.block] — dual-track tool invocation (B1-BE4).
// ---------------------------------------------------------------------------

/// Top-level `[tools]` wrapper. Currently holds the block-tool dual-track
/// switch; future tracks (OpenAI function-call parallel mode, etc.) get added
/// here.
#[derive(Debug, Clone, Default, Serialize, Deserialize, JsonSchema)]
#[serde(default, deny_unknown_fields)]
pub struct ToolsConfig {
    pub block: BlockToolsConfig,
}

/// Block-tool protocol opt-in. When `enabled = false`, block-tool expansion is
/// skipped and only the regular function-call track runs. When `enabled = true`
/// and `fallback_to_function_call = true`, agents that don't advertise the
/// block protocol are silently downgraded to function-calling.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(default, deny_unknown_fields)]
pub struct BlockToolsConfig {
    pub enabled: bool,
    pub fallback_to_function_call: bool,
}

impl Default for BlockToolsConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            fallback_to_function_call: true,
        }
    }
}

// ---------------------------------------------------------------------------
// [telegram] + [telegram.webhook] — webhook-mode Telegram adapter.
// ---------------------------------------------------------------------------

/// Top-level `[telegram]` wrapper for webhook-mode configuration. The
/// long-poll adapter lives under `[channels.telegram]` and is unaffected.
#[derive(Debug, Clone, Default, Serialize, Deserialize, JsonSchema)]
#[serde(default, deny_unknown_fields)]
pub struct TelegramConfig {
    pub webhook: TelegramWebhookConfig,
}

/// Webhook-mode Telegram bot config. `public_url` is the HTTPS URL Telegram
/// will POST updates to; empty string = webhook disabled. `secret_token` is
/// echoed back in `X-Telegram-Bot-Api-Secret-Token` by Telegram so the
/// handler can authenticate inbound requests.
#[derive(Debug, Clone, Default, Serialize, Deserialize, JsonSchema)]
#[serde(default, deny_unknown_fields)]
pub struct TelegramWebhookConfig {
    pub public_url: String,
    pub secret_token: String,
    pub drop_updates_on_reconnect: bool,
}

// ---------------------------------------------------------------------------
// [vector] + [vector.tags] — opt-in v6 hierarchical tag tree.
// ---------------------------------------------------------------------------

/// Top-level `[vector]` wrapper. Currently holds the hierarchical-tag opt-in;
/// room to grow for future vector-store knobs without another top-level table.
#[derive(Debug, Clone, Default, Serialize, Deserialize, JsonSchema)]
#[serde(default, deny_unknown_fields)]
pub struct VectorConfig {
    pub tags: VectorTagsConfig,
}

/// Hierarchical tag tree for the vector store. Off by default; when
/// `hierarchy_enabled = true`, tags may be dotted paths (`a.b.c`) and queries
/// match prefix subtrees up to `max_depth` levels deep.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, Validate)]
#[serde(default, deny_unknown_fields)]
pub struct VectorTagsConfig {
    pub hierarchy_enabled: bool,
    #[validate(range(min = 1, max = 32))]
    pub max_depth: u32,
}

impl Default for VectorTagsConfig {
    fn default() -> Self {
        Self {
            hierarchy_enabled: false,
            max_depth: 6,
        }
    }
}

// ---------------------------------------------------------------------------
// [wstool] — local WebSocket tool-bus.
// ---------------------------------------------------------------------------

/// Local WebSocket bus for tool plugins that prefer a streaming socket over
/// stdio. `bind` defaults to loopback for safety. `auth_token` is required
/// when `bind` is non-loopback (validated via [`Config::validate`]).
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, Validate)]
#[serde(default, deny_unknown_fields)]
pub struct WsToolConfig {
    #[validate(length(min = 1))]
    pub bind: String,
    pub auth_token: String,
    #[validate(range(min = 1, max = 3600))]
    pub heartbeat_secs: u32,
}

impl Default for WsToolConfig {
    fn default() -> Self {
        Self {
            bind: "127.0.0.1:18790".into(),
            auth_token: String::new(),
            heartbeat_secs: 15,
        }
    }
}

// ---------------------------------------------------------------------------
// [canvas] — host canvas endpoint.
// ---------------------------------------------------------------------------

/// Canvas host endpoint (code / diagram preview). Off by default;
/// `session_ttl_secs` bounds the per-session scratch retention.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, Validate)]
#[serde(default, deny_unknown_fields)]
pub struct CanvasConfig {
    pub host_endpoint_enabled: bool,
    #[validate(range(min = 1, max = 86_400))]
    pub session_ttl_secs: u32,
}

impl Default for CanvasConfig {
    fn default() -> Self {
        Self {
            host_endpoint_enabled: false,
            session_ttl_secs: 1800,
        }
    }
}

// ---------------------------------------------------------------------------
// [nodebridge] — Node.js child-process bridge listener.
// ---------------------------------------------------------------------------

/// Bridge listener for Node.js worker children. `accept_unsigned = false`
/// reserves future signed-payload verification; the switch is live today so
/// migrations later flip to true/false without schema churn.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, Validate)]
#[serde(default, deny_unknown_fields)]
pub struct NodeBridgeConfig {
    #[validate(length(min = 1))]
    pub listen: String,
    pub accept_unsigned: bool,
}

impl Default for NodeBridgeConfig {
    fn default() -> Self {
        Self {
            listen: "127.0.0.1:18788".into(),
            accept_unsigned: false,
        }
    }
}

// ---------------------------------------------------------------------------
// [evolution] — Phase 2 EvolutionLoop master switches.
// ---------------------------------------------------------------------------

/// Top-level evolution config. Each subsystem (observer in the gateway, the
/// Python EvolutionEngine, future ShadowTester) gets its own nested section
/// with an `enabled` master switch so a half-rolled-out feature can be
/// turned off without removing the rest of the wiring.
#[derive(Debug, Clone, Default, Serialize, Deserialize, JsonSchema, Validate)]
#[serde(default, deny_unknown_fields)]
pub struct EvolutionConfig {
    #[validate(nested)]
    pub observer: EvolutionObserverConfig,
    #[validate(nested)]
    pub shadow: EvolutionShadowConfig,
    #[validate(nested)]
    pub auto_rollback: EvolutionAutoRollbackConfig,
    #[validate(nested)]
    pub budget: EvolutionBudgetConfig,
}

/// Tunables for the gateway's `EvolutionObserver` (Phase 2 wave 1-A). It
/// subscribes to the hook bus, adapts the curated event set into
/// `EvolutionSignal`s, and persists them via the `corlinman-evolution`
/// repos.
///
/// * `enabled` — master switch. When `false` the observer is not spawned;
///   the gateway boots otherwise unchanged.
/// * `db_path` — SQLite file backing `evolution_signals` /
///   `evolution_proposals` / `evolution_history`. Default
///   `/data/evolution.sqlite` mirrors the `auto-evolution.md` design doc.
/// * `queue_capacity` — bounded write queue between hook subscription and
///   the SQLite writer. Overflows drop the *oldest* row (so recent context
///   wins) and increment `gateway_evolution_signals_dropped_total`.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, Validate)]
#[serde(default, deny_unknown_fields)]
pub struct EvolutionObserverConfig {
    pub enabled: bool,
    pub db_path: PathBuf,
    #[validate(range(min = 1, max = 1_048_576))]
    pub queue_capacity: usize,
}

impl Default for EvolutionObserverConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            db_path: PathBuf::from("/data/evolution.sqlite"),
            queue_capacity: 10_000,
        }
    }
}

/// Tunables for the ShadowTester (Phase 3 wave 1-A). It picks pending
/// proposals whose `risk` is `medium` or `high`, runs them through an
/// in-process eval set, captures `shadow_metrics` + `baseline_metrics_json`
/// + `eval_run_id`, and transitions the row from `shadow_running` to
///   `shadow_done` so the operator sees a measured delta before approving.
///
/// * `enabled` — master switch. When `false` the ShadowTester job is not
///   scheduled; medium/high-risk proposals stay in `pending` and are
///   approvable directly (Phase 2 behavior — useful while the eval set is
///   still being authored).
/// * `eval_set_dir` — root directory containing per-kind YAML eval cases
///   (`<dir>/<kind>/*.yaml`). Missing or empty subdirs short-circuit the
///   shadow run and emit a warn-level metric so the operator notices.
/// * `sandbox_kind` — isolation strategy. Phase 3 ships `in_process` only;
///   `docker` is reserved for Phase 4 prompt/tool kinds and rejected on
///   load until the runner supports it.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, Validate)]
#[serde(default, deny_unknown_fields)]
pub struct EvolutionShadowConfig {
    pub enabled: bool,
    pub eval_set_dir: PathBuf,
    pub sandbox_kind: ShadowSandboxKind,
    /// Phase 4 W1 4-1C: tunables for the docker-backed sandbox. Only
    /// consulted when `sandbox_kind = "docker"`. The defaults pin the
    /// reserved image tag and conservative resource caps; operators
    /// who run the sandbox in production should pin to a SHA-tagged
    /// image rather than `:v1` once the build pipeline publishes
    /// reproducible digests.
    #[serde(default)]
    pub sandbox: SandboxBackendConfig,
}

/// Which sandbox the ShadowTester runs proposals in. `InProcess` is the
/// Phase 3 default. `Docker` is the Phase 4 W1 4-1C addition; required
/// for the high-risk EvolutionKinds (`prompt_template`, `tool_policy`,
/// `new_skill`) whose evals can call out to a live LLM and therefore
/// need network / cgroup / drop-all-caps isolation that an in-process
/// simulator can't provide.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum ShadowSandboxKind {
    InProcess,
    Docker,
}

impl Default for EvolutionShadowConfig {
    fn default() -> Self {
        // `enabled = false` keeps Phase 2 behavior on rollout: an
        // operator must opt in to shadow gating once they've authored
        // (or accepted the bundled) eval set under `eval_set_dir`.
        Self {
            enabled: false,
            eval_set_dir: PathBuf::from("/data/eval/evolution"),
            sandbox_kind: ShadowSandboxKind::InProcess,
            sandbox: SandboxBackendConfig::default(),
        }
    }
}

/// Phase 4 W1 4-1C: tunables for the docker-backed sandbox.
///
/// `image` is the tag the docker backend spawns. v1 ships
/// `ghcr.io/ymylive/corlinman-sandbox:v1` as the placeholder;
/// operators who want a SHA-pinned reference can override it (the
/// `corlinman-sandbox` binary is the only thing in the image, so
/// rebuilds are deterministic).
///
/// `mem_mb` and `timeout_secs` map directly to `docker run`'s
/// `--memory` and the per-call wall clock. `network` is reserved
/// for the future `--network=host` opt-out — v1 always uses
/// `--network=none` regardless of this value, so the field is
/// declarative documentation only.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(default, deny_unknown_fields)]
pub struct SandboxBackendConfig {
    pub image: String,
    pub network: String,
    pub mem_mb: u64,
    pub timeout_secs: u64,
}

impl Default for SandboxBackendConfig {
    fn default() -> Self {
        Self {
            image: "ghcr.io/ymylive/corlinman-sandbox:v1".to_string(),
            network: "none".to_string(),
            mem_mb: 512,
            timeout_secs: 60,
        }
    }
}

/// Tunables for the AutoRollback monitor (Phase 3 wave 1-B). Periodically
/// scans recently-applied proposals — within the grace window — and
/// compares the per-target signal counts in `evolution_signals` against
/// the `metrics_baseline` snapshot the applier captured at apply time.
/// When the relative delta breaches the configured threshold, the monitor
/// fabricates a new rollback proposal (with `rollback_of` set) and the
/// applier replays the original `inverse_diff` to restore prior state.
///
/// * `enabled` — master switch. When `false` the monitor job is not
///   scheduled; metrics_baseline still gets populated at apply time so
///   you can flip this on later without losing history.
/// * `grace_window_hours` — how long after apply a proposal stays
///   eligible for auto-rollback. 72h matches the roadmap spec — long
///   enough to catch slow-burn regressions but short enough that an
///   ancient apply can't be reverted out from under newer state.
/// * `thresholds` — when a delta counts as "regression". Defaults are
///   conservative; per-kind overrides land in W1-B Step 2.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, Validate)]
#[serde(default, deny_unknown_fields)]
pub struct EvolutionAutoRollbackConfig {
    pub enabled: bool,
    #[validate(range(min = 1, max = 720))]
    pub grace_window_hours: u32,
    #[validate(nested)]
    pub thresholds: AutoRollbackThresholds,
}

/// Threshold knobs the monitor uses to decide whether a metrics delta
/// warrants a rollback. The ratios are *relative* to baseline so a
/// chatty target doesn't auto-revert just because absolute counts are
/// large.
///
/// `signal_window_secs` is how far back the metric snapshot looks at
/// apply time *and* how far back the monitor looks when computing the
/// post-apply current snapshot — keeping them symmetric prevents a
/// false positive from sample-window mismatch.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, Validate)]
#[serde(default, deny_unknown_fields)]
pub struct AutoRollbackThresholds {
    /// Maximum percent increase in error-severity signal count over
    /// baseline before triggering rollback (`50.0` = "+50%").
    #[validate(range(min = 0.0, max = 1000.0))]
    pub default_err_rate_delta_pct: f64,
    /// Maximum percent increase in p95 latency signals over baseline.
    /// Reserved for future kinds that emit latency signals; memory_op
    /// today doesn't use it.
    #[validate(range(min = 0.0, max = 1000.0))]
    pub default_p95_latency_delta_pct: f64,
    /// Sliding-window length used both pre-apply (baseline) and
    /// post-apply (current) when counting signals. 30 min = 1800s.
    #[validate(range(min = 60, max = 86_400))]
    pub signal_window_secs: u32,
    /// Minimum baseline count required before a percent delta is
    /// trusted — guards against "0 → 1 = +infinity%" false positives
    /// on quiet targets.
    #[validate(range(min = 0, max = 10_000))]
    pub min_baseline_signals: u32,
}

impl Default for EvolutionAutoRollbackConfig {
    fn default() -> Self {
        // `enabled = false` ships off so applies don't surprise-revert
        // before W1-B is fully wired. metrics_baseline is still
        // captured at apply time (cheap, useful as future audit data)
        // even with the master switch off.
        Self {
            enabled: false,
            grace_window_hours: 72,
            thresholds: AutoRollbackThresholds::default(),
        }
    }
}

impl Default for AutoRollbackThresholds {
    fn default() -> Self {
        Self {
            default_err_rate_delta_pct: 50.0,
            default_p95_latency_delta_pct: 25.0,
            signal_window_secs: 1_800,
            min_baseline_signals: 5,
        }
    }
}

/// Tunables for the proposal-creation budget gate (Phase 3 wave 1-C).
/// Caps how many proposals the engine may file per ISO week — both in
/// total and per-kind — so a runaway clusterer can't flood the operator
/// queue. The Python engine reads these via the JSON drop and aborts the
/// `propose` step when a cap is reached; the gateway's
/// `/admin/evolution/budget` endpoint surfaces the same numbers to the
/// UI gauge.
///
/// * `enabled` — master switch. Off by default so existing deployments
///   don't surprise-block on rollout; an operator opts in once the
///   engine + UI both ship.
/// * `weekly_total` — cap across all kinds inside the current ISO week
///   (Monday 00:00 UTC, inclusive → next Monday 00:00 UTC, exclusive).
/// * `per_kind` — sub-caps per `EvolutionKind`. A missing entry means
///   "no per-kind cap; only `weekly_total` applies". `BTreeMap` (not
///   `HashMap`) so JSON / TOML serialisation keeps deterministic order.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, Validate)]
#[serde(default, deny_unknown_fields)]
pub struct EvolutionBudgetConfig {
    pub enabled: bool,
    #[validate(range(min = 0, max = 100_000))]
    pub weekly_total: u32,
    pub per_kind: BTreeMap<EvolutionKind, u32>,
}

impl Default for EvolutionBudgetConfig {
    fn default() -> Self {
        // Mirrors the documented defaults in the wave 1-C contract. Per-kind
        // entries are populated even with `enabled = false` so the UI gauge
        // can render the configured shape on first boot.
        let mut per_kind = BTreeMap::new();
        per_kind.insert(EvolutionKind::MemoryOp, 5);
        per_kind.insert(EvolutionKind::SkillUpdate, 3);
        per_kind.insert(EvolutionKind::AgentCard, 5);
        per_kind.insert(EvolutionKind::PromptTemplate, 1);
        per_kind.insert(EvolutionKind::ToolPolicy, 1);
        per_kind.insert(EvolutionKind::NewSkill, 2);
        per_kind.insert(EvolutionKind::TagRebalance, 3);
        per_kind.insert(EvolutionKind::RetryTuning, 3);
        Self {
            enabled: false,
            weekly_total: 15,
            per_kind,
        }
    }
}

// ---------------------------------------------------------------------------
// [memory] — Phase 3 W3-A: chunk decay + consolidation pipeline.
// ---------------------------------------------------------------------------

/// Tunables for the memory subsystem (chunk decay + consolidation).
///
/// The two sub-sections are independent: decay is purely the read-time
/// score multiplier on `chunks` (driven by `last_recalled_at` +
/// `decay_score`), while consolidation is the periodic job that
/// promotes high-scoring chunks into the immune `consolidated`
/// namespace via the EvolutionApplier so every kb mutation still flows
/// through the audit trail.
#[derive(Debug, Clone, Default, Serialize, Deserialize, JsonSchema, Validate)]
#[serde(default, deny_unknown_fields)]
pub struct MemoryConfig {
    #[validate(nested)]
    pub decay: MemoryDecayConfig,
    #[validate(nested)]
    pub consolidation: MemoryConsolidationConfig,
}

/// `[memory.decay]` — knobs for the read-time exponential half-life
/// applied to chunk scores. Matches the in-code defaults on
/// `corlinman_vector::DecayConfig` so the read path can hydrate
/// directly from this struct.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, Validate)]
#[serde(default, deny_unknown_fields)]
pub struct MemoryDecayConfig {
    /// Master switch. When `false` the SqliteStore returns scores
    /// unchanged and `record_recall` is a no-op.
    pub enabled: bool,
    /// Hours since last recall at which the multiplier hits 0.5.
    /// 168h = one week, matching the design doc.
    #[validate(range(min = 1.0, max = 8760.0))]
    pub half_life_hours: f64,
    /// Floor below which the read-time decayed score is clamped — keeps
    /// long-untouched chunks visible enough to participate in RRF
    /// fusion instead of vanishing entirely.
    #[validate(range(min = 0.0, max = 1.0))]
    pub floor_score: f32,
    /// Bump added to `decay_score` on every recall (capped at 1.0).
    #[validate(range(min = 0.0, max = 1.0))]
    pub recall_boost: f32,
}

impl Default for MemoryDecayConfig {
    fn default() -> Self {
        // Mirrors `corlinman_vector::DecayConfig::default`. Keep them
        // in lockstep — the gateway hydrates the vector struct from
        // this one at startup.
        Self {
            enabled: true,
            half_life_hours: 168.0,
            floor_score: 0.05,
            recall_boost: 0.3,
        }
    }
}

/// `[memory.consolidation]` — periodic-job knobs for promoting
/// high-scoring chunks into the immune `consolidated` namespace.
///
/// The job itself runs as a Python CLI subcommand
/// (`corlinman-evolution-engine consolidate-once`) wired through the
/// scheduler; this section is what the CLI reads to decide which
/// chunks to file `memory_op` proposals for.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, Validate)]
#[serde(default, deny_unknown_fields)]
pub struct MemoryConsolidationConfig {
    /// Master switch. When `false` the CLI exits with a clear log line
    /// and no proposals are filed; flipping this on later doesn't
    /// require touching the scheduler.
    pub enabled: bool,
    /// Cron expression (6-field corlinman-scheduler dialect) the
    /// scheduler runs the CLI on. Default 05:00 UTC daily lands well
    /// after the 03:00 evolution_engine + 03:30 shadow_tester pair so
    /// any merge proposals from the day's clustering are out of the
    /// way before consolidation files its own.
    pub schedule: String,
    /// Minimum stored `decay_score` for a chunk to qualify for
    /// promotion. 0.65 ≈ "recalled at least twice (0.7 ramp) in the
    /// last week without much decay".
    #[validate(range(min = 0.0, max = 1.0))]
    pub promotion_threshold: f32,
    /// Hard cap on candidates emitted per run — prevents a flood of
    /// memory_op proposals from drowning the operator queue when the
    /// threshold is set too low.
    #[validate(range(min = 1, max = 10_000))]
    pub max_promotions_per_run: u32,
    /// Phase 3.1 (B-4): minimum age (in hours since the last recall)
    /// before a chunk is eligible for promotion. Defends against the
    /// post-W3-A cold-start cliff — every legacy row sits at
    /// `decay_score = 1.0` from the column default, so without this
    /// guard the first cron tick after the migration would promote
    /// `max_promotions_per_run` random chunks. The cooling window
    /// also lets a burst-read chunk's score settle before
    /// consolidation freezes it. 24h matches the typical "diurnal
    /// recall pattern" — anything still hot tomorrow is real signal.
    #[validate(range(min = 0.0, max = 8760.0))]
    pub cooling_period_hours: f64,
}

impl Default for MemoryConsolidationConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            schedule: "0 0 5 * * * *".into(),
            promotion_threshold: 0.65,
            max_promotions_per_run: 50,
            cooling_period_hours: 24.0,
        }
    }
}

// ---------------------------------------------------------------------------
// [persona] — Phase 3 W3-C
// ---------------------------------------------------------------------------
//
// Decay knobs for the Python `corlinman-persona` package. Runtime state
// (mood / fatigue / recent_topics) lives in `agent_state.sqlite`; this
// struct only mirrors the TOML shape so `docs/config.example.toml`
// round-trips through serde and `Config`'s `deny_unknown_fields` doesn't
// reject the section.
//
// All fields default to the Phase 3 roadmap §6 [persona] values; an
// absent section deserialises into `PersonaConfig::default()` (enabled
// = true, the documented decay rates).

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(default, deny_unknown_fields)]
pub struct PersonaConfig {
    pub enabled: bool,
    pub mood_decay_per_hour: f64,
    pub fatigue_recovery_per_hour: f64,
    pub recent_topics_decay_per_day: u32,
}

impl Default for PersonaConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            mood_decay_per_hour: 0.05,
            fatigue_recovery_per_hour: 0.1,
            recent_topics_decay_per_day: 1,
        }
    }
}

// ---------------------------------------------------------------------------
// [tenants] — Phase 4 W1 4-1A
// ---------------------------------------------------------------------------
//
// Multi-tenant boundary. The Phase 3.1 Tier 3 / S-2 schema migration
// already added `tenant_id NOT NULL DEFAULT 'default'` to user_traits +
// agent_persona_state; Phase 4 Item 1 extended the same column to every
// other stateful SQLite. This struct is the runtime switch that
// turns the schema-level scoping into actual middleware-level isolation.
//
// Backwards compatibility is the load-bearing goal here: pre-Phase-4
// configs have no `[tenants]` section, so `Default` must keep them
// running as a single-tenant deployment with `default` as the implicit
// tenant for every request.
//
// Slug shape is validated by `corlinman-tenant::TenantId::new` at the
// boundary where slugs cross from config into the runtime; storing as
// String here avoids pulling corlinman-tenant into the corlinman-core
// dep graph.

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(default, deny_unknown_fields)]
pub struct TenantsConfig {
    /// Master switch. False = legacy single-tenant deployment; every
    /// operation runs as the reserved `default` tenant and the
    /// gateway's tenant-scoping middleware is mounted as a no-op.
    /// True = multi-tenant: middleware enforces scoping on admin
    /// routes and per-tenant SQLite layout under
    /// `<data_dir>/tenants/<tenant_id>/`.
    pub enabled: bool,
    /// Reserved fallback tenant id. The Phase 3.1 schema migration
    /// already defaults legacy rows to this slug; do not change
    /// without a paired data migration.
    pub default: String,
    /// Operator-allowed tenant slugs. The middleware rejects any
    /// session claim or `?tenant=` query whose slug is not in this
    /// list (or in `tenants.sqlite`). `default` is implicitly always
    /// allowed when `enabled = false`.
    pub allowed: Vec<String>,
    /// One-shot boot migration: when `true` *and* `enabled = true`,
    /// gateway main rename-migrates any legacy
    /// `<data_dir>/{evolution,kb,sessions,user_model,agent_state}.sqlite`
    /// files into `<data_dir>/tenants/default/` on startup. Once the
    /// new layout exists the probe is a no-op. Set to `false` to keep
    /// migration manual.
    pub migrate_legacy_paths: bool,
}

impl Default for TenantsConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            default: "default".to_string(),
            allowed: Vec::new(),
            migrate_legacy_paths: true,
        }
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
        // 2a. Every named provider entry must have a resolvable kind — either
        //     an explicit `kind = "..."` field or a first-party slot name
        //     (anthropic / openai / google / deepseek / qwen / glm). Free-form
        //     names without a kind produce a hard error pointing at the
        //     offending entry so the operator knows exactly where to add
        //     `kind = "..."`.
        for (name, entry) in self.providers.iter() {
            if entry.kind.is_none() && ProviderKind::from_slot_name(name).is_none() {
                let valid: Vec<&str> = ProviderKind::all().iter().map(|k| k.as_str()).collect();
                issues.push(ValidationIssue {
                    path: format!("providers.{name}.kind"),
                    code: "missing_kind".into(),
                    message: format!(
                        "provider '{name}' has no `kind` field and the name is not a first-party \
slot. Set `kind = \"...\"` explicitly. Valid kinds: {}",
                        valid.join(", ")
                    ),
                    level: IssueLevel::Error,
                });
            }
        }

        // 2b. models.default must be reachable — either the literal model id is
        //     resolvable as an alias, or at least one provider is enabled so the
        //     agent layer can route to it.
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
        for (alias, entry) in &self.models.aliases {
            let target = entry.target();
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

        // Embedding section: if present + enabled, `provider` must reference
        // a declared slot.
        if let Some(emb) = &self.embedding {
            if emb.enabled {
                if emb.provider.trim().is_empty() {
                    issues.push(ValidationIssue {
                        path: "embedding.provider".into(),
                        code: "embedding_provider_empty".into(),
                        message: "embedding.enabled = true but provider is empty".into(),
                        level: IssueLevel::Error,
                    });
                } else if !self.providers.contains_key(emb.provider.as_str()) {
                    issues.push(ValidationIssue {
                        path: "embedding.provider".into(),
                        code: "embedding_provider_missing".into(),
                        message: format!(
                            "embedding.provider = '{}' but no [providers.{}] block is declared",
                            emb.provider, emb.provider
                        ),
                        level: IssueLevel::Error,
                    });
                }
                if emb.model.trim().is_empty() {
                    issues.push(ValidationIssue {
                        path: "embedding.model".into(),
                        code: "embedding_model_empty".into(),
                        message: "embedding.model must be non-empty".into(),
                        level: IssueLevel::Error,
                    });
                }
                if emb.dimension == 0 {
                    issues.push(ValidationIssue {
                        path: "embedding.dimension".into(),
                        code: "embedding_dimension_zero".into(),
                        message: "embedding.dimension must be > 0".into(),
                        level: IssueLevel::Error,
                    });
                }
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
                // Warn — not an error — because the QQ adapter can still
                // function in keyword-filter / private-chat mode without
                // knowing the bot's own UIN. The only feature self_ids
                // unlocks is `@mention` detection in groups (which falls
                // back to keyword matching when self_ids is empty). Letting
                // the operator toggle `enabled = true` in the admin UI
                // even before the QQ login completes is the friendlier
                // path; once napcat scan-login lands a self_id, the next
                // `corlinman config show` shows it populated and the
                // warning self-clears.
                issues.push(ValidationIssue {
                    path: "channels.qq.self_ids".into(),
                    code: "empty_self_ids".into(),
                    message:
                        "channels.qq.enabled = true but self_ids is empty — \
                         @mention detection disabled until populated. Run \
                         `corlinman config set channels.qq.self_ids` or \
                         complete the napcat scan-login flow."
                            .into(),
                    level: IssueLevel::Warn,
                });
            }
        }

        // 3b. Telegram channel sanity.
        if let Some(tg) = &self.channels.telegram {
            if tg.enabled && tg.bot_token.is_none() {
                issues.push(ValidationIssue {
                    path: "channels.telegram.bot_token".into(),
                    code: "empty_bot_token".into(),
                    message: "channels.telegram.enabled = true but bot_token is missing".into(),
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

    /// Human-readable convenience wrapper around [`Config::validate_report`].
    ///
    /// Returns `Ok(())` iff the report contains no `Error`-level issues;
    /// otherwise `Err(Vec<String>)` with one line per error, formatted as
    /// `"<path>: <code>: <message>"`. Warnings are intentionally dropped here
    /// (callers that care about warnings should use `validate_report`).
    pub fn validate(&self) -> Result<(), Vec<String>> {
        // Cross-field rule specific to this facade: if wstool binds to a
        // non-loopback address, require an auth_token. Purely a defence-in-
        // depth hint; `validate_report` doesn't emit this today so network-
        // exposed wstool sockets without a token still boot, which is not
        // what we want for the "did you configure this safely?" facade.
        let mut extra: Vec<String> = Vec::new();
        if !is_loopback_bind(&self.wstool.bind) && self.wstool.auth_token.is_empty() {
            extra.push(format!(
                "wstool.auth_token: wstool_token_required: wstool.bind = '{}' is non-loopback but auth_token is empty",
                self.wstool.bind
            ));
        }

        let errors: Vec<String> = self
            .validate_report()
            .into_iter()
            .filter(|i| i.level == IssueLevel::Error)
            .map(|i| format!("{}: {}: {}", i.path, i.code, i.message))
            .chain(extra)
            .collect();
        if errors.is_empty() {
            Ok(())
        } else {
            Err(errors)
        }
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
        if let Some(tg) = out.channels.telegram.as_mut() {
            if let Some(tok) = tg.bot_token.as_mut() {
                *tok = tok.redacted();
            }
        }
        if out.admin.password_hash.is_some() {
            out.admin.password_hash = Some("***REDACTED***".into());
        }
        out
    }
}

/// Return `true` iff `bind` is a loopback `host:port` (`127.0.0.0/8` or `::1`).
/// Any unparseable or non-loopback address returns `false`. Best-effort; the
/// goal is just to gate the "non-loopback without auth_token" warning.
fn is_loopback_bind(bind: &str) -> bool {
    // Accept `host:port` only. Strip the port then parse the host as an IP.
    // We intentionally don't try to resolve hostnames — "localhost" isn't
    // auto-trusted because an operator overriding /etc/hosts shouldn't change
    // the security posture of the config schema.
    let host = match bind.rsplit_once(':') {
        Some((h, _)) => h.trim_start_matches('[').trim_end_matches(']'),
        None => return false,
    };
    match host.parse::<std::net::IpAddr>() {
        Ok(ip) => ip.is_loopback(),
        Err(_) => false,
    }
}

fn redact_providers(p: &mut ProvidersConfig) {
    for entry in p.0.values_mut() {
        if let Some(k) = entry.api_key.as_mut() {
            *k = k.redacted();
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

[rag.rerank]
enabled = false
mode = "local"

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
        assert_eq!(cfg.providers.enabled_names(), vec!["anthropic".to_string()]);
        assert_eq!(cfg.channels.qq.as_ref().unwrap().self_ids, vec![123456789]);
        assert_eq!(cfg.scheduler.jobs.len(), 1);
        // [rag.rerank] defaults propagate when unspecified.
        assert!(!cfg.rag.rerank.enabled);
        assert_eq!(cfg.rag.rerank.mode, RerankMode::Local);
    }

    #[test]
    fn rag_rerank_remote_block_parses() {
        let toml = r#"
[rag.rerank]
enabled = true
mode = "remote"
model = "rerank-multilingual-v3.0"
api_base = "https://api.example.com/v1"
api_key = { env = "EXAMPLE_RERANK_KEY" }
"#;
        let cfg: Config = toml::from_str(toml).unwrap();
        assert!(cfg.rag.rerank.enabled);
        assert_eq!(cfg.rag.rerank.mode, RerankMode::Remote);
        assert_eq!(
            cfg.rag.rerank.model.as_deref(),
            Some("rerank-multilingual-v3.0")
        );
        assert!(matches!(
            cfg.rag.rerank.api_key,
            Some(SecretRef::EnvVar { .. })
        ));
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
        cfg.providers.insert(
            "anthropic",
            ProviderEntry {
                api_key: Some(SecretRef::EnvVar {
                    env: "ANTHROPIC_API_KEY".into(),
                }),
                base_url: None,
                enabled: true,
                ..Default::default()
            },
        );
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
        cfg.providers.insert(
            "openai",
            ProviderEntry {
                api_key: Some(SecretRef::Literal {
                    value: "sk-top-secret".into(),
                }),
                base_url: None,
                enabled: true,
                ..Default::default()
            },
        );
        cfg.admin.password_hash = Some("$argon2id$v=19$m=...".into());
        let red = cfg.redacted();
        let openai = red.providers.get("openai").unwrap().clone();
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
        // Default config seeds a disabled `openai` entry; replace it so the
        // round-trip enables a single named slot.
        let mut cfg = Config::default();
        cfg.providers.remove("openai");
        cfg.providers.insert(
            "anthropic",
            ProviderEntry {
                api_key: Some(SecretRef::EnvVar {
                    env: "ANTHROPIC_API_KEY".into(),
                }),
                base_url: None,
                enabled: true,
                ..Default::default()
            },
        );
        cfg.save_to_path(&p).unwrap();
        let loaded = Config::load_from_path(&p).unwrap();
        assert!(loaded.meta.last_touched_at.is_some());
        assert_eq!(
            loaded.meta.last_touched_version.as_deref(),
            Some(env!("CARGO_PKG_VERSION"))
        );
        assert_eq!(loaded.server.port, cfg.server.port);
        assert_eq!(
            loaded.providers.enabled_names(),
            vec!["anthropic".to_string()]
        );
    }

    #[test]
    fn get_and_set_dotted_scalars() {
        let mut cfg = Config::default();
        cfg.providers.insert(
            "anthropic",
            ProviderEntry {
                api_key: Some(SecretRef::EnvVar {
                    env: "ANTHROPIC_API_KEY".into(),
                }),
                base_url: None,
                enabled: true,
                ..Default::default()
            },
        );

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

    // -----------------------------------------------------------------------
    // B1-BE4: new top-level sections.
    // -----------------------------------------------------------------------

    #[test]
    fn empty_toml_populates_all_new_sections_with_defaults() {
        let cfg: Config = toml::from_str("").unwrap();

        assert_eq!(cfg.hooks.capacity, 1024);
        assert!(cfg.hooks.enabled);

        assert_eq!(cfg.skills.dir, "skills");
        assert!(cfg.skills.autoload);

        assert_eq!(cfg.variables.tar_dir, "TVStxt/tar");
        assert_eq!(cfg.variables.var_dir, "TVStxt/var");
        assert_eq!(cfg.variables.sar_dir, "TVStxt/sar");
        assert_eq!(cfg.variables.fixed_dir, "TVStxt/fixed");
        assert!(cfg.variables.hot_reload);

        assert_eq!(cfg.agents.dir, "agents");
        assert!(cfg.agents.single_agent_gate);

        assert!(!cfg.tools.block.enabled);
        assert!(cfg.tools.block.fallback_to_function_call);

        assert_eq!(cfg.telegram.webhook.public_url, "");
        assert_eq!(cfg.telegram.webhook.secret_token, "");
        assert!(!cfg.telegram.webhook.drop_updates_on_reconnect);

        assert!(!cfg.vector.tags.hierarchy_enabled);
        assert_eq!(cfg.vector.tags.max_depth, 6);

        assert_eq!(cfg.wstool.bind, "127.0.0.1:18790");
        assert_eq!(cfg.wstool.auth_token, "");
        assert_eq!(cfg.wstool.heartbeat_secs, 15);

        assert!(!cfg.canvas.host_endpoint_enabled);
        assert_eq!(cfg.canvas.session_ttl_secs, 1800);

        assert_eq!(cfg.nodebridge.listen, "127.0.0.1:18788");
        assert!(!cfg.nodebridge.accept_unsigned);
    }

    #[test]
    fn existing_full_toml_still_parses_with_new_sections_absent() {
        // The pre-B1-BE4 full_toml fixture mentions none of the new sections;
        // this is the back-compat guarantee — it must still load untouched.
        let cfg: Config = toml::from_str(&full_toml()).unwrap();
        assert_eq!(cfg.hooks.capacity, 1024);
        assert_eq!(cfg.variables.tar_dir, "TVStxt/tar");
        assert_eq!(cfg.wstool.bind, "127.0.0.1:18790");
    }

    #[test]
    fn docs_example_toml_still_parses() {
        // `docs/config.example.toml` is the source of truth the README points
        // readers at. Load it by path so any addition to the example is
        // forced through `Config`'s deny-unknown-fields gate.
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("..")
            .join("..")
            .join("..")
            .join("docs")
            .join("config.example.toml");
        if !path.exists() {
            // The example file is optional — if it's missing we skip rather
            // than failing. This keeps the test portable across checkouts
            // that strip the docs/ tree.
            return;
        }
        let text = std::fs::read_to_string(&path).unwrap();
        let cfg: Config = toml::from_str(&text).expect("docs/config.example.toml must parse");
        // spot-check: defaults still present for new sections if the example
        // doesn't override them.
        assert!(cfg.hooks.enabled);
    }

    #[test]
    fn hooks_fragment_parses() {
        let frag = r#"
[hooks]
capacity = 4096
enabled = false
"#;
        let cfg: Config = toml::from_str(frag).unwrap();
        assert_eq!(cfg.hooks.capacity, 4096);
        assert!(!cfg.hooks.enabled);
    }

    /// Phase 4 W1 4-1A: `[tenants]` defaults preserve legacy
    /// single-tenant behaviour — a config that omits the section parses
    /// to `enabled = false`, `default = "default"`, no allowed slugs,
    /// and the migration probe armed.
    #[test]
    fn tenants_defaults_preserve_legacy_single_tenant() {
        let cfg: Config = toml::from_str("").unwrap();
        assert!(!cfg.tenants.enabled);
        assert_eq!(cfg.tenants.default, "default");
        assert!(cfg.tenants.allowed.is_empty());
        assert!(cfg.tenants.migrate_legacy_paths);
    }

    /// Phase 4 W1 4-1A: explicit `[tenants]` block round-trips through
    /// serde with the documented field set; deny_unknown_fields catches
    /// typos.
    #[test]
    fn tenants_fragment_parses() {
        let frag = r#"
[tenants]
enabled              = true
default              = "default"
allowed              = ["acme", "bravo"]
migrate_legacy_paths = false
"#;
        let cfg: Config = toml::from_str(frag).unwrap();
        assert!(cfg.tenants.enabled);
        assert_eq!(cfg.tenants.default, "default");
        assert_eq!(
            cfg.tenants.allowed,
            vec!["acme".to_string(), "bravo".to_string()]
        );
        assert!(!cfg.tenants.migrate_legacy_paths);
    }

    #[test]
    fn tenants_unknown_field_rejected() {
        let frag = r#"
[tenants]
enabled = true
weight  = 42
"#;
        let err = toml::from_str::<Config>(frag).unwrap_err();
        assert!(
            err.to_string().contains("weight"),
            "deny_unknown_fields should flag the typo: {err}"
        );
    }

    #[test]
    fn skills_fragment_parses() {
        let frag = r#"
[skills]
dir = "my-skills"
autoload = false
"#;
        let cfg: Config = toml::from_str(frag).unwrap();
        assert_eq!(cfg.skills.dir, "my-skills");
        assert!(!cfg.skills.autoload);
    }

    #[test]
    fn variables_fragment_parses() {
        let frag = r#"
[variables]
tar_dir = "a"
var_dir = "b"
sar_dir = "c"
fixed_dir = "d"
hot_reload = false
"#;
        let cfg: Config = toml::from_str(frag).unwrap();
        assert_eq!(cfg.variables.tar_dir, "a");
        assert_eq!(cfg.variables.fixed_dir, "d");
        assert!(!cfg.variables.hot_reload);
    }

    #[test]
    fn agents_fragment_parses() {
        let frag = r#"
[agents]
dir = "./agents-custom"
single_agent_gate = false
"#;
        let cfg: Config = toml::from_str(frag).unwrap();
        assert_eq!(cfg.agents.dir, "./agents-custom");
        assert!(!cfg.agents.single_agent_gate);
    }

    #[test]
    fn tools_block_fragment_parses() {
        let frag = r#"
[tools.block]
enabled = true
fallback_to_function_call = false
"#;
        let cfg: Config = toml::from_str(frag).unwrap();
        assert!(cfg.tools.block.enabled);
        assert!(!cfg.tools.block.fallback_to_function_call);
    }

    #[test]
    fn telegram_webhook_fragment_parses() {
        let frag = r#"
[telegram.webhook]
public_url = "https://bot.example.com/telegram/webhook"
secret_token = "sekret"
drop_updates_on_reconnect = true
"#;
        let cfg: Config = toml::from_str(frag).unwrap();
        assert_eq!(
            cfg.telegram.webhook.public_url,
            "https://bot.example.com/telegram/webhook"
        );
        assert_eq!(cfg.telegram.webhook.secret_token, "sekret");
        assert!(cfg.telegram.webhook.drop_updates_on_reconnect);
    }

    #[test]
    fn vector_tags_fragment_parses() {
        let frag = r#"
[vector.tags]
hierarchy_enabled = true
max_depth = 4
"#;
        let cfg: Config = toml::from_str(frag).unwrap();
        assert!(cfg.vector.tags.hierarchy_enabled);
        assert_eq!(cfg.vector.tags.max_depth, 4);
    }

    #[test]
    fn wstool_fragment_parses() {
        let frag = r#"
[wstool]
bind = "0.0.0.0:19000"
auth_token = "tok"
heartbeat_secs = 30
"#;
        let cfg: Config = toml::from_str(frag).unwrap();
        assert_eq!(cfg.wstool.bind, "0.0.0.0:19000");
        assert_eq!(cfg.wstool.auth_token, "tok");
        assert_eq!(cfg.wstool.heartbeat_secs, 30);
    }

    #[test]
    fn canvas_fragment_parses() {
        let frag = r#"
[canvas]
host_endpoint_enabled = true
session_ttl_secs = 600
"#;
        let cfg: Config = toml::from_str(frag).unwrap();
        assert!(cfg.canvas.host_endpoint_enabled);
        assert_eq!(cfg.canvas.session_ttl_secs, 600);
    }

    #[test]
    fn nodebridge_fragment_parses() {
        let frag = r#"
[nodebridge]
listen = "127.0.0.1:19001"
accept_unsigned = true
"#;
        let cfg: Config = toml::from_str(frag).unwrap();
        assert_eq!(cfg.nodebridge.listen, "127.0.0.1:19001");
        assert!(cfg.nodebridge.accept_unsigned);
    }

    fn cfg_with_one_provider() -> Config {
        let mut cfg = Config::default();
        // Default seeds a disabled `openai` entry — drop it so the helper
        // returns "exactly one enabled provider", matching the older
        // pre-refactor expectation.
        cfg.providers.remove("openai");
        cfg.providers.insert(
            "anthropic",
            ProviderEntry {
                api_key: Some(SecretRef::EnvVar {
                    env: "ANTHROPIC_API_KEY".into(),
                }),
                base_url: None,
                enabled: true,
                ..Default::default()
            },
        );
        cfg
    }

    #[test]
    fn validate_ok_on_minimal_config_with_provider() {
        // Default config (no provider) produces a `no_provider_enabled` warn,
        // not an error; with one enabled provider, validate() should be Ok.
        cfg_with_one_provider()
            .validate()
            .expect("validate returned errors");
    }

    #[test]
    fn validate_flags_wstool_nonloopback_without_token() {
        let mut cfg = cfg_with_one_provider();
        cfg.wstool.bind = "0.0.0.0:18790".into();
        cfg.wstool.auth_token = String::new();
        let errs = cfg.validate().expect_err("expected a wstool error");
        assert!(
            errs.iter().any(|e| e.contains("wstool_token_required")),
            "expected wstool_token_required, got: {errs:?}"
        );
    }

    #[test]
    fn validate_wstool_loopback_without_token_is_ok() {
        // default bind = 127.0.0.1:18790, empty token is fine on loopback.
        cfg_with_one_provider()
            .validate()
            .expect("loopback + empty token must be ok");
    }

    #[test]
    fn validate_wstool_ipv6_loopback_is_ok() {
        let mut cfg = cfg_with_one_provider();
        cfg.wstool.bind = "[::1]:18790".into();
        cfg.validate().expect("[::1] is loopback");
    }

    #[test]
    fn validate_wstool_nonloopback_with_token_is_ok() {
        let mut cfg = cfg_with_one_provider();
        cfg.wstool.bind = "0.0.0.0:18790".into();
        cfg.wstool.auth_token = "tok".into();
        cfg.validate()
            .expect("non-loopback with a token must be accepted");
    }

    #[test]
    fn validate_propagates_validator_derive_errors() {
        let mut cfg = cfg_with_one_provider();
        // hooks.capacity = 0 fails the validator range(min=1).
        cfg.hooks.capacity = 0;
        let errs = cfg.validate().expect_err("expected validator error");
        assert!(
            errs.iter().any(|e| e.contains("hooks.capacity")),
            "expected hooks.capacity error, got: {errs:?}"
        );
    }

    // -----------------------------------------------------------------------
    // P0-1: [logging.file] — back-compat + schema coverage.
    // -----------------------------------------------------------------------

    #[test]
    fn old_logging_toml_without_file_section_still_parses() {
        // Pre-P0-1 configs only have `level` / `format` / `file_rolling` —
        // the back-compat guarantee is that the file sub-section is
        // populated from defaults, not required.
        let frag = r#"
[logging]
level = "info"
format = "json"
file_rolling = false
"#;
        let cfg: Config = toml::from_str(frag).expect("old logging block must parse");
        assert_eq!(cfg.logging.level, "info");
        assert_eq!(cfg.logging.format, "json");
        assert!(!cfg.logging.file_rolling);
        // Defaults land for the new block.
        assert_eq!(
            cfg.logging.file.path,
            PathBuf::from("/data/logs/gateway.log")
        );
        assert_eq!(cfg.logging.file.max_size_mb, 5);
        assert_eq!(cfg.logging.file.retention_days, 7);
        assert_eq!(cfg.logging.file.rotation, RotationKind::Daily);
    }

    #[test]
    fn new_logging_file_toml_parses_every_field() {
        let frag = r#"
[logging.file]
path = "/var/log/corlinman/gateway.log"
max_size_mb = 25
retention_days = 14
rotation = "hourly"
"#;
        let cfg: Config = toml::from_str(frag).expect("new logging.file block must parse");
        assert_eq!(
            cfg.logging.file.path,
            PathBuf::from("/var/log/corlinman/gateway.log")
        );
        assert_eq!(cfg.logging.file.max_size_mb, 25);
        assert_eq!(cfg.logging.file.retention_days, 14);
        assert_eq!(cfg.logging.file.rotation, RotationKind::Hourly);
        // Outer fields still default.
        assert_eq!(cfg.logging.level, "info");
    }

    #[test]
    fn logging_file_rotation_accepts_every_variant() {
        for (raw, want) in [
            ("daily", RotationKind::Daily),
            ("hourly", RotationKind::Hourly),
            ("minutely", RotationKind::Minutely),
            ("never", RotationKind::Never),
        ] {
            let frag = format!("[logging.file]\nrotation = \"{raw}\"\n");
            let cfg: Config = toml::from_str(&frag).unwrap();
            assert_eq!(cfg.logging.file.rotation, want, "rotation = {raw}");
        }
    }

    #[test]
    fn empty_toml_populates_logging_file_defaults() {
        let cfg: Config = toml::from_str("").unwrap();
        assert_eq!(
            cfg.logging.file.path,
            PathBuf::from("/data/logs/gateway.log")
        );
        assert_eq!(cfg.logging.file.max_size_mb, 5);
        assert_eq!(cfg.logging.file.retention_days, 7);
        assert_eq!(cfg.logging.file.rotation, RotationKind::Daily);
    }

    // -----------------------------------------------------------------------
    // Free-form named providers (operator-chosen keys).
    // -----------------------------------------------------------------------

    #[test]
    fn parses_named_providers_with_explicit_kind() {
        // siliconflow is a free-form name (not a first-party slot); the
        // explicit `kind = "openai_compatible"` makes it parse cleanly and
        // the round-trip preserves the key + kind + base_url.
        let frag = r#"
[providers.siliconflow]
kind = "openai_compatible"
base_url = "https://api.siliconflow.cn/v1"
api_key = { env = "SILICONFLOW_API_KEY" }
enabled = true
"#;
        let cfg: Config = toml::from_str(frag).expect("siliconflow entry must parse");
        let entry = cfg
            .providers
            .get("siliconflow")
            .expect("siliconflow entry should be present");
        assert_eq!(entry.kind, Some(ProviderKind::OpenaiCompatible));
        assert_eq!(
            entry.base_url.as_deref(),
            Some("https://api.siliconflow.cn/v1")
        );
        assert!(entry.enabled);
        // Round-trip: serialise then re-parse.
        let serialised = toml::to_string_pretty(&cfg).unwrap();
        let reparsed: Config = toml::from_str(&serialised).unwrap();
        assert!(reparsed.providers.contains_key("siliconflow"));
        // validate() should be Ok — providers has an explicit kind + api_key.
        // (The test config has no admin auth so wstool loopback default keeps
        // it loopback-safe.)
        reparsed
            .validate()
            .expect("named provider with explicit kind validates");
    }

    #[test]
    fn multiple_openai_compatible_entries_coexist() {
        // Three named entries all sharing kind = openai_compatible — the
        // motivating scenario for free-form keys (one operator wires up
        // SiliconFlow + OpenRouter + a local vLLM gateway side by side).
        let frag = r#"
[providers.siliconflow]
kind = "openai_compatible"
base_url = "https://api.siliconflow.cn/v1"
api_key = { env = "SILICONFLOW_API_KEY" }
enabled = true

[providers.openrouter]
kind = "openai_compatible"
base_url = "https://openrouter.ai/api/v1"
api_key = { env = "OPENROUTER_API_KEY" }
enabled = true

[providers.local-vllm]
kind = "openai_compatible"
base_url = "http://127.0.0.1:8000/v1"
enabled = true
"#;
        let cfg: Config = toml::from_str(frag).expect("three named entries must coexist");
        assert!(cfg.providers.contains_key("siliconflow"));
        assert!(cfg.providers.contains_key("openrouter"));
        assert!(cfg.providers.contains_key("local-vllm"));
        // enabled_names() reports the two with api_keys; the local-vllm
        // entry is enabled but key-less so it doesn't count as "enabled
        // and reachable".
        let mut enabled = cfg.providers.enabled_names();
        enabled.sort();
        assert_eq!(
            enabled,
            vec!["openrouter".to_string(), "siliconflow".to_string()]
        );
    }

    #[test]
    fn legacy_slot_names_still_load() {
        // Pre-refactor config: `[providers.openai]` with no `kind` field.
        // The slot name infers the kind so the entry remains valid.
        let frag = r#"
[providers.openai]
api_key = { env = "OPENAI_API_KEY" }
enabled = true
"#;
        let cfg: Config = toml::from_str(frag).expect("legacy slot must parse");
        let entry = cfg.providers.get("openai").expect("openai slot present");
        // No explicit kind on disk.
        assert!(entry.kind.is_none());
        // …but kind_for() infers it from the slot name.
        assert_eq!(
            cfg.providers.kind_for("openai", entry),
            Some(ProviderKind::Openai)
        );
        // validate() accepts the legacy shape (no `missing_kind` issue).
        let issues = cfg.validate_report();
        assert!(
            !issues.iter().any(|i| i.code == "missing_kind"),
            "legacy slot must not raise missing_kind, got: {issues:?}"
        );
    }

    #[test]
    fn missing_kind_errors_with_helpful_message() {
        // Free-form name + no `kind` field → validator raises a friendly
        // error that names the offending entry and lists the valid kinds.
        let frag = r#"
[providers.mystery-llm]
api_key = { env = "MYSTERY_API_KEY" }
enabled = true
"#;
        let cfg: Config = toml::from_str(frag).expect("toml parses (validator runs later)");
        let issues = cfg.validate_report();
        let kind_issue = issues
            .iter()
            .find(|i| i.code == "missing_kind")
            .expect("expected missing_kind issue");
        assert_eq!(kind_issue.path, "providers.mystery-llm.kind");
        assert!(
            kind_issue.message.contains("mystery-llm"),
            "message must name the offending entry, got: {}",
            kind_issue.message
        );
        // The error enumerates valid kinds so the operator can copy/paste.
        assert!(
            kind_issue.message.contains("openai_compatible"),
            "message must list valid kinds, got: {}",
            kind_issue.message
        );
        assert_eq!(kind_issue.level, IssueLevel::Error);
    }

    #[test]
    fn embedding_can_reference_any_named_provider() {
        // The embedding section binds `provider = "<name>"`; with free-form
        // keys that name can be anything an operator declared, not just
        // one of the six legacy slot names.
        let frag = r#"
[providers.siliconflow]
kind = "openai_compatible"
base_url = "https://api.siliconflow.cn/v1"
api_key = { env = "SILICONFLOW_API_KEY" }
enabled = true

[embedding]
provider = "siliconflow"
model = "BAAI/bge-large-zh-v1.5"
dimension = 1024
enabled = true
"#;
        let cfg: Config = toml::from_str(frag).expect("named provider + embedding must parse");
        let issues = cfg.validate_report();
        // No `embedding_provider_missing` — the named provider was found.
        assert!(
            !issues
                .iter()
                .any(|i| i.code == "embedding_provider_missing"),
            "embedding ref to named provider must resolve, got: {issues:?}"
        );
    }

    #[test]
    fn every_new_provider_kind_round_trips() {
        // One round-trip per new ProviderKind to lock the snake_case wire
        // shape so any future rename surfaces here, not three crates over.
        for (kind, wire) in [
            (ProviderKind::Mistral, "mistral"),
            (ProviderKind::Cohere, "cohere"),
            (ProviderKind::Together, "together"),
            (ProviderKind::Groq, "groq"),
            (ProviderKind::Replicate, "replicate"),
            (ProviderKind::Bedrock, "bedrock"),
            (ProviderKind::Azure, "azure"),
        ] {
            assert_eq!(kind.as_str(), wire, "as_str() for {kind:?}");
            let frag = format!(
                r#"
[providers.my-{wire}]
kind = "{wire}"
api_key = {{ env = "X" }}
enabled = true
"#
            );
            let cfg: Config =
                toml::from_str(&frag).unwrap_or_else(|e| panic!("kind={wire}: parse failed: {e}"));
            let entry = cfg
                .providers
                .get(&format!("my-{wire}"))
                .unwrap_or_else(|| panic!("kind={wire}: entry missing"));
            assert_eq!(entry.kind, Some(kind), "kind round-trip for {wire}");
            // Re-serialise and re-parse to lock TOML round-trip.
            let serialised = toml::to_string_pretty(&cfg).unwrap();
            let reparsed: Config = toml::from_str(&serialised)
                .unwrap_or_else(|e| panic!("kind={wire}: re-parse failed: {e}"));
            assert_eq!(
                reparsed.providers.get(&format!("my-{wire}")).unwrap().kind,
                Some(kind),
                "round-trip preserves kind for {wire}"
            );
        }
    }

    #[test]
    fn default_config_seeds_openai_entry() {
        // A fresh `Config::default()` must include a single `[providers.openai]`
        // entry with `kind = "openai"` so onboarding docs stay accurate
        // ("export OPENAI_API_KEY and you're done").
        let cfg = Config::default();
        let entry = cfg
            .providers
            .get("openai")
            .expect("default config must seed [providers.openai]");
        assert_eq!(entry.kind, Some(ProviderKind::Openai));
        // The seeded entry is disabled + key-less — flipping `enabled = true`
        // and supplying an api_key is the operator's job.
        assert!(!entry.enabled);
        assert!(entry.api_key.is_none());
        // Round-trip through TOML so we know the seeded entry survives a
        // save/load cycle.
        let toml_text = toml::to_string_pretty(&cfg).unwrap();
        assert!(
            toml_text.contains("[providers.openai]"),
            "default config TOML must include [providers.openai]; got:\n{toml_text}"
        );
        let reparsed: Config = toml::from_str(&toml_text).unwrap();
        assert!(reparsed.providers.contains_key("openai"));
    }
}
