//! Rust → Python config handshake (Feature C last-mile).
//!
//! The Python side reads its `ProviderRegistry` + alias + embedding config
//! from a JSON file whose path is passed via the `CORLINMAN_PY_CONFIG` env
//! var. This module owns serialising the Rust [`Config`] into that JSON
//! shape (matching `corlinman_providers.specs.ProviderSpec` +
//! `AliasEntry` + `EmbeddingSpec`) and writing it atomically.
//!
//! The gateway calls [`write_py_config`] once at boot (so the Python
//! subprocess started by any supervisor sees a non-empty registry) and then
//! again after every admin write that mutates providers / aliases / models
//! / the embedding section.
//!
//! The JSON shape is:
//!
//! ```json
//! {
//!   "providers": [
//!     { "name": "anthropic", "kind": "anthropic",
//!       "api_key": "...", "base_url": null,
//!       "enabled": true, "params": {} }
//!   ],
//!   "aliases": {
//!     "smart": { "provider": "anthropic",
//!                "model": "claude-opus-4-7",
//!                "params": {"temperature": 0.7} }
//!   },
//!   "embedding": {
//!     "provider": "openai", "model": "text-embedding-3-small",
//!     "dimension": 1536, "enabled": true, "params": {}
//!   }
//! }
//! ```
//!
//! Secrets: `api_key` is resolved through [`SecretRef::resolve`] so the
//! JSON carries the concrete key string. This file is written with owner-
//! only permissions by virtue of living under `$CORLINMAN_DATA_DIR` (the
//! same directory as `config.toml`), and is overwritten on every admin
//! change.

use std::path::{Path, PathBuf};

use corlinman_core::config::{AliasEntry, Config, ProviderEntry, SecretRef};
use serde::Serialize;
use serde_json::{json, Value as JsonValue};

/// Env var name the Python side reads to locate the JSON drop.
pub const ENV_PY_CONFIG: &str = "CORLINMAN_PY_CONFIG";

/// Filename under `$CORLINMAN_DATA_DIR`.
const PY_CONFIG_FILENAME: &str = "py-config.json";

/// Default JSON path: `$CORLINMAN_DATA_DIR/py-config.json`, else
/// `/tmp/corlinman-py-config.json`. Mirrors the `/tmp`-friendly fallback
/// the Python side documents for container deployments.
pub fn default_py_config_path() -> PathBuf {
    if let Ok(dir) = std::env::var("CORLINMAN_DATA_DIR") {
        return PathBuf::from(dir).join(PY_CONFIG_FILENAME);
    }
    if let Some(home) = dirs::home_dir() {
        return home.join(".corlinman").join(PY_CONFIG_FILENAME);
    }
    PathBuf::from("/tmp/corlinman-py-config.json")
}

/// Provider spec matching `corlinman_providers.specs.ProviderSpec`.
#[derive(Debug, Serialize)]
struct PyProviderSpec {
    name: String,
    kind: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    api_key: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    base_url: Option<String>,
    enabled: bool,
    params: JsonValue,
}

/// Alias entry matching `corlinman_providers.specs.AliasEntry`.
#[derive(Debug, Serialize)]
struct PyAliasEntry {
    provider: String,
    model: String,
    params: JsonValue,
}

/// Embedding spec matching `corlinman_providers.specs.EmbeddingSpec`.
#[derive(Debug, Serialize)]
struct PyEmbeddingSpec {
    provider: String,
    model: String,
    dimension: u32,
    enabled: bool,
    params: JsonValue,
}

/// Render the Rust [`Config`] as the Python JSON shape.
///
/// Invariants:
/// * Providers without a resolvable `kind` are dropped (legacy slots with
///   neither explicit `kind` nor an inferred name — should never happen in
///   practice because `from_slot_name` covers every first-party name).
/// * Providers whose `api_key.resolve()` fails (env var unset) still
///   serialise with `api_key: null`, and Python treats that as "no auth"
///   (valid for local gateways) — we don't block config write just because
///   a dev forgot to export the key. Caller logs can mention the failure.
/// * Shorthand aliases (`smart = "claude-opus-4-7"`) become alias entries
///   with an *empty* `provider` field, because the Python `AliasEntry`
///   model requires a non-optional `provider`. Since the Python side also
///   supports legacy-prefix resolution, we omit shorthand aliases entirely
///   — the Python legacy-prefix fallback will route raw model ids like
///   `claude-*` to the right adapter on its own.
/// * Full-form aliases without an explicit `provider` are likewise omitted
///   — same reasoning.
pub fn render_py_config(cfg: &Config) -> JsonValue {
    let mut providers: Vec<PyProviderSpec> = Vec::new();
    for (name, entry) in cfg.providers.iter() {
        let Some(kind) = cfg.providers.kind_for(name, entry) else {
            continue;
        };
        providers.push(PyProviderSpec {
            name: name.to_string(),
            kind: kind.as_str(),
            api_key: resolve_api_key(entry),
            base_url: entry.base_url.clone(),
            enabled: entry.enabled,
            params: params_to_json(&entry.params),
        });
    }

    let mut aliases = serde_json::Map::new();
    for (alias_name, alias_entry) in cfg.models.aliases.iter() {
        // Only full-form aliases with an explicit provider are representable
        // as a Python AliasEntry. Shorthand / provider-less entries go
        // through the Python legacy-prefix fallback.
        let (provider, model, params) = match alias_entry {
            AliasEntry::Full(spec) => {
                let Some(p) = spec.provider.as_ref() else {
                    continue;
                };
                (p.clone(), spec.model.clone(), params_to_json(&spec.params))
            }
            AliasEntry::Shorthand(_) => continue,
        };
        aliases.insert(
            alias_name.clone(),
            serde_json::to_value(PyAliasEntry {
                provider,
                model,
                params,
            })
            .unwrap_or(JsonValue::Null),
        );
    }

    let embedding = cfg.embedding.as_ref().map(|emb| PyEmbeddingSpec {
        provider: emb.provider.clone(),
        model: emb.model.clone(),
        dimension: emb.dimension,
        enabled: emb.enabled,
        params: params_to_json(&emb.params),
    });

    json!({
        "providers": providers,
        "aliases": JsonValue::Object(aliases),
        "embedding": embedding,
    })
}

/// Write the Python-side JSON to `path` atomically (tmp + rename).
///
/// Creates parent dirs as needed. Callers should invoke this at boot
/// (after config load) and after every admin write that mutates the
/// relevant sections.
pub async fn write_py_config(cfg: &Config, path: &Path) -> std::io::Result<()> {
    let payload = render_py_config(cfg);
    let bytes = serde_json::to_vec_pretty(&payload).map_err(std::io::Error::other)?;
    if let Some(parent) = path.parent() {
        tokio::fs::create_dir_all(parent).await?;
    }
    let mut tmp = path.to_path_buf();
    tmp.as_mut_os_string().push(".new");
    tokio::fs::write(&tmp, &bytes).await?;
    tokio::fs::rename(&tmp, path).await?;
    Ok(())
}

/// Synchronous boot-path variant — used before any tokio runtime is
/// available (e.g. inside `fn build_runtime_with_logs` which is already
/// async but the write needs to happen before `set_var` and the Python
/// subprocess spawn). Having a sync version keeps the call sites shorter.
pub fn write_py_config_sync(cfg: &Config, path: &Path) -> std::io::Result<()> {
    let payload = render_py_config(cfg);
    let bytes = serde_json::to_vec_pretty(&payload).map_err(std::io::Error::other)?;
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let mut tmp = path.to_path_buf();
    tmp.as_mut_os_string().push(".new");
    std::fs::write(&tmp, &bytes)?;
    std::fs::rename(&tmp, path)?;
    Ok(())
}

fn resolve_api_key(entry: &ProviderEntry) -> Option<String> {
    match entry.api_key.as_ref()? {
        SecretRef::Literal { value } => Some(value.clone()),
        SecretRef::EnvVar { env } => std::env::var(env).ok(),
    }
}

fn params_to_json(params: &corlinman_core::config::ParamsMap) -> JsonValue {
    // ParamsMap is `BTreeMap<String, serde_json::Value>`; direct serialise.
    serde_json::to_value(params).unwrap_or_else(|_| json!({}))
}

#[cfg(test)]
mod tests {
    use super::*;
    use corlinman_core::config::{
        AliasEntry, AliasSpec, Config, EmbeddingConfig, ParamsMap, ProviderEntry, ProviderKind,
        SecretRef,
    };

    fn cfg_with_everything() -> Config {
        let mut cfg = Config::default();
        std::env::set_var("PY_CONFIG_TEST_KEY", "sk-test-xyz");
        cfg.providers.anthropic = Some(ProviderEntry {
            kind: None, // inferred from slot name
            api_key: Some(SecretRef::EnvVar {
                env: "PY_CONFIG_TEST_KEY".into(),
            }),
            base_url: None,
            enabled: true,
            params: {
                let mut p = ParamsMap::new();
                p.insert("temperature".into(), serde_json::json!(0.7));
                p
            },
        });
        cfg.providers.openai = Some(ProviderEntry {
            kind: Some(ProviderKind::Openai),
            api_key: Some(SecretRef::Literal {
                value: "sk-literal".into(),
            }),
            base_url: Some("https://api.openai.com/v1".into()),
            enabled: true,
            params: ParamsMap::new(),
        });
        cfg.models.aliases.insert(
            "smart".into(),
            AliasEntry::Full(AliasSpec {
                model: "claude-opus-4-7".into(),
                provider: Some("anthropic".into()),
                params: {
                    let mut p = ParamsMap::new();
                    p.insert("temperature".into(), serde_json::json!(0.5));
                    p
                },
            }),
        );
        // Shorthand alias — should be omitted from JSON.
        cfg.models
            .aliases
            .insert("bare".into(), AliasEntry::Shorthand("gpt-4o".into()));
        cfg.embedding = Some(EmbeddingConfig {
            provider: "openai".into(),
            model: "text-embedding-3-small".into(),
            dimension: 1536,
            enabled: true,
            params: ParamsMap::new(),
        });
        cfg
    }

    #[test]
    fn render_matches_python_schema() {
        let cfg = cfg_with_everything();
        let v = render_py_config(&cfg);

        let providers = v["providers"].as_array().unwrap();
        assert_eq!(providers.len(), 2);
        // Anthropic — kind inferred, api_key resolved from env.
        let anthropic = providers.iter().find(|p| p["name"] == "anthropic").unwrap();
        assert_eq!(anthropic["kind"], "anthropic");
        assert_eq!(anthropic["api_key"], "sk-test-xyz");
        assert_eq!(anthropic["enabled"], true);
        assert_eq!(anthropic["params"]["temperature"], 0.7);
        // OpenAI — literal key, explicit kind + base_url.
        let openai = providers.iter().find(|p| p["name"] == "openai").unwrap();
        assert_eq!(openai["kind"], "openai");
        assert_eq!(openai["api_key"], "sk-literal");
        assert_eq!(openai["base_url"], "https://api.openai.com/v1");

        let aliases = v["aliases"].as_object().unwrap();
        // Only the full-form alias made it in.
        assert!(aliases.contains_key("smart"));
        assert!(!aliases.contains_key("bare"));
        assert_eq!(aliases["smart"]["provider"], "anthropic");
        assert_eq!(aliases["smart"]["model"], "claude-opus-4-7");
        assert_eq!(aliases["smart"]["params"]["temperature"], 0.5);

        let embedding = &v["embedding"];
        assert_eq!(embedding["provider"], "openai");
        assert_eq!(embedding["model"], "text-embedding-3-small");
        assert_eq!(embedding["dimension"], 1536);
        assert_eq!(embedding["enabled"], true);

        std::env::remove_var("PY_CONFIG_TEST_KEY");
    }

    #[test]
    fn write_py_config_sync_produces_parseable_file() {
        let tmp = tempfile::TempDir::new().unwrap();
        let path = tmp.path().join("py-config.json");
        let cfg = cfg_with_everything();

        write_py_config_sync(&cfg, &path).expect("write ok");
        let text = std::fs::read_to_string(&path).unwrap();
        let parsed: JsonValue = serde_json::from_str(&text).unwrap();
        assert!(parsed["providers"].is_array());
        assert!(parsed["aliases"].is_object());
        assert!(parsed["embedding"].is_object());
        // No stale .new sidecar.
        let mut stale = path.to_path_buf();
        stale.as_mut_os_string().push(".new");
        assert!(!stale.exists());

        std::env::remove_var("PY_CONFIG_TEST_KEY");
    }

    #[test]
    fn missing_env_var_leaves_api_key_null() {
        let mut cfg = Config::default();
        std::env::remove_var("PY_CONFIG_TEST_MISSING");
        cfg.providers.anthropic = Some(ProviderEntry {
            kind: None,
            api_key: Some(SecretRef::EnvVar {
                env: "PY_CONFIG_TEST_MISSING".into(),
            }),
            base_url: None,
            enabled: true,
            params: ParamsMap::new(),
        });
        let v = render_py_config(&cfg);
        let anthropic = &v["providers"][0];
        assert_eq!(anthropic["name"], "anthropic");
        assert!(
            anthropic["api_key"].is_null() || anthropic.get("api_key").is_none(),
            "unset env should render as null/absent, got {anthropic}"
        );
    }

    #[test]
    fn empty_config_renders_empty_sections() {
        let cfg = Config::default();
        let v = render_py_config(&cfg);
        assert!(v["providers"].as_array().unwrap().is_empty());
        assert!(v["aliases"].as_object().unwrap().is_empty());
        assert!(v["embedding"].is_null());
    }
}
