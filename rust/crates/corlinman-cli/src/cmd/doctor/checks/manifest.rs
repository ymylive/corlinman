//! Manifest check: scan `data_dir/plugins/` and surface parse errors.
//!
//! Delegates the actual walk + parse to `corlinman_plugins::discover`, which
//! emits one `DiscoveryDiagnostic` per unparseable manifest. This check just
//! classifies the outcome:
//!   * any diagnostic → `Fail` with the first message and count of "more".
//!   * zero plugins + zero diagnostics → `Warn` (empty install).
//!   * otherwise → `Ok` with the count.

use async_trait::async_trait;
use corlinman_plugins::{discover, Origin, SearchRoot};

use super::{DoctorCheck, DoctorContext, DoctorResult};

pub struct ManifestCheck;

impl ManifestCheck {
    pub fn new() -> Self {
        Self
    }
}

impl Default for ManifestCheck {
    fn default() -> Self {
        Self::new()
    }
}

#[async_trait]
impl DoctorCheck for ManifestCheck {
    fn name(&self) -> &str {
        "manifest"
    }

    async fn run(&self, ctx: &DoctorContext) -> DoctorResult {
        let plugins_dir = ctx.data_dir.join("plugins");
        if !plugins_dir.exists() {
            return DoctorResult::Warn {
                message: format!("plugins dir not found at {}", plugins_dir.display()),
                hint: Some("run `corlinman plugins list` after installing a plugin".into()),
            };
        }

        let roots = vec![SearchRoot::new(&plugins_dir, Origin::Global)];
        let (found, diags) = discover(&roots);

        if !diags.is_empty() {
            let first = &diags[0];
            let more = if diags.len() > 1 {
                format!(" (+{} more)", diags.len() - 1)
            } else {
                String::new()
            };
            return DoctorResult::Fail {
                message: format!(
                    "manifest parse error at {}: {}{}",
                    first.path.display(),
                    first.message,
                    more
                ),
                hint: Some("fix or remove the offending plugin-manifest.toml".into()),
            };
        }

        if found.is_empty() {
            return DoctorResult::Warn {
                message: format!("no plugins in {}", plugins_dir.display()),
                hint: Some("run `corlinman plugins list` to confirm".into()),
            };
        }

        DoctorResult::Ok {
            message: format!("{} plugin(s) discovered", found.len()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use tempfile::tempdir;

    fn ctx_for(data_dir: std::path::PathBuf) -> DoctorContext {
        DoctorContext {
            config_path: data_dir.join("config.toml"),
            data_dir,
            config: None,
        }
    }

    #[tokio::test]
    async fn missing_plugins_dir_is_warn() {
        let dir = tempdir().unwrap();
        let ctx = ctx_for(dir.path().to_path_buf());
        let res = ManifestCheck::new().run(&ctx).await;
        assert_eq!(res.status_str(), "warn", "got: {:?}", res);
    }

    #[tokio::test]
    async fn empty_plugins_dir_is_warn() {
        let dir = tempdir().unwrap();
        fs::create_dir_all(dir.path().join("plugins")).unwrap();
        let ctx = ctx_for(dir.path().to_path_buf());
        let res = ManifestCheck::new().run(&ctx).await;
        assert_eq!(res.status_str(), "warn");
    }

    #[tokio::test]
    async fn well_formed_manifest_is_ok() {
        let dir = tempdir().unwrap();
        let plugin_dir = dir.path().join("plugins").join("alpha");
        fs::create_dir_all(&plugin_dir).unwrap();
        fs::write(
            plugin_dir.join("plugin-manifest.toml"),
            "name = \"alpha\"\nversion = \"0.1.0\"\nplugin_type = \"sync\"\n[entry_point]\ncommand = \"true\"\n",
        )
        .unwrap();
        let ctx = ctx_for(dir.path().to_path_buf());
        let res = ManifestCheck::new().run(&ctx).await;
        assert_eq!(res.status_str(), "ok", "got: {:?}", res);
    }

    #[tokio::test]
    async fn parse_error_is_fail() {
        let dir = tempdir().unwrap();
        let plugin_dir = dir.path().join("plugins").join("bad");
        fs::create_dir_all(&plugin_dir).unwrap();
        fs::write(plugin_dir.join("plugin-manifest.toml"), "not valid toml ==").unwrap();
        let ctx = ctx_for(dir.path().to_path_buf());
        let res = ManifestCheck::new().run(&ctx).await;
        assert_eq!(res.status_str(), "fail", "got: {:?}", res);
    }
}
