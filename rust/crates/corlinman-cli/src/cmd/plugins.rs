//! `corlinman plugins {list,inspect,invoke,doctor}` (plan §8 B2).
//!
//! All subcommands read the registry built from `CORLINMAN_PLUGIN_DIRS`
//! (colon-separated, `$PATH`-style). `list` and `inspect` are manifest-only —
//! they never exec plugin code. `invoke` is the thin shim that spawns a plugin
//! via `runtime::jsonrpc_stdio::execute` so operators can probe a plugin end
//! to end without standing up the full gateway.

use clap::Subcommand;
use tokio_util::sync::CancellationToken;

use corlinman_plugins::{
    manifest::PluginType,
    roots_from_env_var,
    runtime::{jsonrpc_stdio, PluginOutput},
    Origin, PluginEntry, PluginRegistry, SearchRoot,
};

#[derive(Debug, Subcommand)]
pub enum Cmd {
    /// List every discovered plugin (origin-ranked).
    List {
        /// Emit a compact JSON array instead of a human table.
        #[arg(long)]
        json: bool,
        /// Hide plugins whose origin is not at or above the given source rank.
        /// Accepts `bundled` | `global` | `user` | `workspace` | `config`.
        #[arg(long, value_name = "ORIGIN")]
        source: Option<String>,
        /// Reserved: include only plugins the gateway would enable.
        /// In M3 every discovered plugin is enabled, so this is a no-op today.
        #[arg(long)]
        enabled: bool,
    },
    /// Show the resolved manifest for one plugin.
    Inspect {
        name: String,
        /// Emit the manifest as compact JSON instead of a readable block.
        #[arg(long)]
        json: bool,
    },
    /// Invoke `<plugin>.<tool>` via JSON-RPC 2.0 over stdio.
    ///
    /// The input MUST have the shape `<plugin>.<tool>`. `--args` is a JSON
    /// object merged into the JSON-RPC `params.arguments` field.
    /// Exit codes: `0` success / `1` plugin-error / `2` runtime-error.
    Invoke {
        /// Fully-qualified tool identifier, e.g. `greeter.greet`.
        target: String,
        /// Arguments as compact JSON (object). Defaults to `{}`.
        #[arg(long, value_name = "JSON")]
        args: Option<String>,
        /// Override the manifest timeout (milliseconds).
        #[arg(long, value_name = "MS")]
        timeout: Option<u64>,
    },
    /// Run plugin-specific diagnostics (manifest + entry_point + registry).
    Doctor { name: Option<String> },
}

pub async fn run(cmd: Cmd) -> anyhow::Result<()> {
    match cmd {
        Cmd::List {
            json,
            source,
            enabled: _,
        } => {
            let registry = load_registry();
            list(&registry, json, source.as_deref())
        }
        Cmd::Inspect { name, json } => {
            let registry = load_registry();
            inspect(&registry, &name, json)
        }
        Cmd::Invoke {
            target,
            args,
            timeout,
        } => {
            let registry = load_registry();
            let code = invoke(&registry, &target, args.as_deref(), timeout).await?;
            std::process::exit(u8::from(code) as i32);
        }
        Cmd::Doctor { name } => {
            let registry = load_registry();
            doctor(&registry, name.as_deref())
        }
    }
}

fn load_registry() -> PluginRegistry {
    let mut roots: Vec<SearchRoot> = roots_from_env_var("CORLINMAN_PLUGIN_DIRS", Origin::Config);
    if roots.is_empty() {
        tracing::debug!("CORLINMAN_PLUGIN_DIRS is empty; registry will be empty");
    }
    roots.sort_by(|a, b| a.path.cmp(&b.path));
    roots.dedup_by(|a, b| a.path == b.path);
    PluginRegistry::from_roots(roots)
}

fn list(registry: &PluginRegistry, as_json: bool, source: Option<&str>) -> anyhow::Result<()> {
    let min_origin = source.map(parse_origin).transpose()?;
    let rows: Vec<&PluginEntry> = registry
        .list()
        .into_iter()
        .filter(|entry| match min_origin {
            Some(min) => entry.origin.rank() >= min.rank(),
            None => true,
        })
        .collect();

    if as_json {
        let json: Vec<serde_json::Value> = rows
            .iter()
            .map(|entry| {
                serde_json::json!({
                    "name": entry.manifest.name,
                    "version": entry.manifest.version,
                    "plugin_type": entry.manifest.plugin_type.as_str(),
                    "origin": entry.origin.as_str(),
                    "manifest_path": entry.manifest_path,
                    "shadowed_count": entry.shadowed_count,
                    "tool_count": entry.manifest.capabilities.tools.len(),
                })
            })
            .collect();
        // Compact JSON: byte-identical output for diffing.
        println!("{}", serde_json::to_string(&json)?);
        return Ok(());
    }

    if rows.is_empty() {
        println!("(no plugins discovered; set CORLINMAN_PLUGIN_DIRS)");
        return Ok(());
    }

    println!(
        "{:<32} {:<10} {:<10} {:<10} PATH",
        "NAME", "VERSION", "TYPE", "ORIGIN"
    );
    for entry in rows {
        println!(
            "{:<32} {:<10} {:<10} {:<10} {}",
            truncate(&entry.manifest.name, 32),
            truncate(&entry.manifest.version, 10),
            entry.manifest.plugin_type.as_str(),
            entry.origin.as_str(),
            entry.manifest_path.display()
        );
    }
    if !registry.diagnostics().is_empty() {
        eprintln!();
        eprintln!("{} diagnostics:", registry.diagnostics().len());
        for diag in registry.diagnostics() {
            eprintln!("  - {:?}", diag);
        }
    }
    Ok(())
}

fn inspect(registry: &PluginRegistry, name: &str, as_json: bool) -> anyhow::Result<()> {
    let entry = registry
        .get(name)
        .ok_or_else(|| anyhow::anyhow!("plugin '{name}' not found in registry"))?;
    if as_json {
        let value = serde_json::to_value(&*entry.manifest)?;
        println!("{}", serde_json::to_string(&value)?);
        return Ok(());
    }

    println!("Name:         {}", entry.manifest.name);
    println!("Version:      {}", entry.manifest.version);
    if !entry.manifest.description.is_empty() {
        println!("Description:  {}", entry.manifest.description);
    }
    if !entry.manifest.author.is_empty() {
        println!("Author:       {}", entry.manifest.author);
    }
    println!("Type:         {}", entry.manifest.plugin_type.as_str());
    println!("Origin:       {}", entry.origin.as_str());
    println!("ManifestPath: {}", entry.manifest_path.display());
    println!("EntryPoint:   {}", entry.manifest.entry_point.command);
    if !entry.manifest.entry_point.args.is_empty() {
        println!("  args:       {:?}", entry.manifest.entry_point.args);
    }
    println!(
        "Timeout:      {}ms",
        jsonrpc_stdio::resolve_timeout(&entry.manifest, None)
    );

    if !entry.manifest.capabilities.tools.is_empty() {
        println!("\nTools:");
        for tool in &entry.manifest.capabilities.tools {
            let first_line = tool.description.lines().next().unwrap_or("");
            println!("  - {}: {}", tool.name, truncate(first_line, 72));
        }
    }
    if entry.shadowed_count > 0 {
        println!(
            "\nShadowed: {} lower-rank manifest(s)",
            entry.shadowed_count
        );
    }
    Ok(())
}

/// Exit-code classification for `invoke`. 0 success / 1 plugin-error / 2 runtime-error.
#[derive(Copy, Clone, Debug)]
enum InvokeCode {
    Success = 0,
    PluginError = 1,
    RuntimeError = 2,
}

impl From<InvokeCode> for u8 {
    fn from(c: InvokeCode) -> u8 {
        c as u8
    }
}

async fn invoke(
    registry: &PluginRegistry,
    target: &str,
    args: Option<&str>,
    timeout_ms: Option<u64>,
) -> anyhow::Result<InvokeCode> {
    let (plugin_name, tool_name) = target
        .split_once('.')
        .ok_or_else(|| anyhow::anyhow!("target must be '<plugin>.<tool>' (got '{target}')"))?;
    if plugin_name.is_empty() || tool_name.is_empty() {
        anyhow::bail!("target must be '<plugin>.<tool>' (got '{target}')");
    }

    let entry = registry
        .get(plugin_name)
        .ok_or_else(|| anyhow::anyhow!("plugin '{plugin_name}' not found in registry"))?;

    if entry.manifest.plugin_type == PluginType::Service {
        anyhow::bail!(
            "plugin '{plugin_name}' is a service (gRPC); direct `invoke` is not supported yet"
        );
    }

    let args_bytes = args.unwrap_or("{}").as_bytes().to_vec();
    if serde_json::from_slice::<serde_json::Value>(&args_bytes).is_err() {
        anyhow::bail!("--args is not valid JSON");
    }

    let cancel = CancellationToken::new();
    let request_id = uuid_v4();
    let trace_id = uuid_v4();
    let outcome = jsonrpc_stdio::execute(
        &entry.manifest.name,
        tool_name,
        &entry.plugin_dir(),
        Some(&entry.manifest),
        timeout_ms,
        &args_bytes,
        "cli",
        &request_id,
        &trace_id,
        None,
        &[],
        cancel,
    )
    .await;

    match outcome {
        Ok(PluginOutput::Success { content, .. }) => {
            use std::io::Write;
            std::io::stdout().write_all(&content)?;
            println!();
            Ok(InvokeCode::Success)
        }
        Ok(PluginOutput::Error { code, message, .. }) => {
            let body = serde_json::json!({
                "error": { "code": code, "message": message }
            });
            println!("{}", serde_json::to_string(&body)?);
            Ok(InvokeCode::PluginError)
        }
        Ok(PluginOutput::AcceptedForLater { task_id, .. }) => {
            let body = serde_json::json!({ "task_id": task_id });
            println!("{}", serde_json::to_string(&body)?);
            Ok(InvokeCode::Success)
        }
        Err(e) => {
            eprintln!("runtime failure: {e}");
            Ok(InvokeCode::RuntimeError)
        }
    }
}

fn doctor(registry: &PluginRegistry, name: Option<&str>) -> anyhow::Result<()> {
    let entries: Vec<&PluginEntry> = match name {
        Some(n) => vec![registry
            .get(n)
            .ok_or_else(|| anyhow::anyhow!("plugin '{n}' not found"))?],
        None => registry.list(),
    };
    let mut total_issues = 0usize;
    for entry in entries {
        let mut issues: Vec<String> = Vec::new();
        if entry.manifest.entry_point.command.trim().is_empty() {
            issues.push("entry_point.command is empty".to_string());
        }
        if entry.manifest.version.trim().is_empty() {
            issues.push("manifest is missing `version`".to_string());
        }
        if entry.manifest.capabilities.tools.is_empty()
            && entry.manifest.plugin_type != PluginType::Service
        {
            issues.push("capabilities.tools is empty".to_string());
        }
        println!(
            "[{origin}] {name}  -> {count} issue(s)",
            origin = entry.origin.as_str(),
            name = entry.manifest.name,
            count = issues.len()
        );
        for issue in &issues {
            println!("    - {issue}");
        }
        total_issues += issues.len();
    }
    if total_issues > 0 {
        std::process::exit(1);
    }
    Ok(())
}

fn parse_origin(s: &str) -> anyhow::Result<Origin> {
    Ok(match s.to_ascii_lowercase().as_str() {
        "bundled" => Origin::Bundled,
        // `user` is an alias for `global` — matches the taxonomy described in
        // the CLI spec even though the Origin enum uses `Global` internally.
        "global" | "user" => Origin::Global,
        "workspace" => Origin::Workspace,
        "config" => Origin::Config,
        other => anyhow::bail!(
            "unknown --source '{other}' (expected bundled|global|user|workspace|config)"
        ),
    })
}

fn truncate(s: &str, n: usize) -> String {
    if s.chars().count() <= n {
        s.to_string()
    } else {
        let taken: String = s.chars().take(n.saturating_sub(1)).collect();
        format!("{taken}…")
    }
}

fn uuid_v4() -> String {
    uuid::Uuid::new_v4().to_string()
}
