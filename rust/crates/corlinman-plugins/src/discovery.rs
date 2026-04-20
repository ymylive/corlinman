//! Manifest-first plugin discovery (manifest-only scan, no code exec).
//!
//! The discovery phase is intentionally side-effect free: it walks each
//! configured directory looking for `plugin-manifest.toml`, parses each file,
//! and returns `(manifest, origin, path)` tuples. The registry layer applies
//! origin-ranked dedup and reports diagnostics for bad manifests.

use std::path::{Path, PathBuf};

use crate::manifest::{parse_manifest_file, ManifestParseError, PluginManifest, MANIFEST_FILENAME};

/// Where a discovered manifest lives. Higher variants override lower ones at
/// dedup time: bundled defaults first, user config overrides last.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Origin {
    Bundled = 0,
    Global = 1,
    Workspace = 2,
    Config = 3,
}

impl Origin {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Bundled => "bundled",
            Self::Global => "global",
            Self::Workspace => "workspace",
            Self::Config => "config",
        }
    }

    pub fn rank(self) -> u8 {
        self as u8
    }
}

/// A pinned search root + its origin label. Order within `discover` does not
/// matter — the registry re-sorts by `Origin` rank before resolving.
#[derive(Debug, Clone)]
pub struct SearchRoot {
    pub path: PathBuf,
    pub origin: Origin,
}

impl SearchRoot {
    pub fn new(path: impl Into<PathBuf>, origin: Origin) -> Self {
        Self {
            path: path.into(),
            origin,
        }
    }
}

/// One hit from `discover`. The raw `path` of the manifest file is preserved
/// so the registry can relativise working directories, watch for edits, and
/// surface it in `inspect`.
#[derive(Debug, Clone)]
pub struct DiscoveredPlugin {
    pub manifest: PluginManifest,
    pub origin: Origin,
    pub manifest_path: PathBuf,
}

impl DiscoveredPlugin {
    /// Directory containing `plugin-manifest.toml`. Runtime launches child
    /// processes with this as `cwd`.
    pub fn plugin_dir(&self) -> &Path {
        self.manifest_path
            .parent()
            .expect("plugin-manifest.toml always has a parent directory")
    }
}

/// A diagnostic emitted when a manifest fails to parse or a name is
/// ambiguous. The registry surfaces these via `corlinman plugins doctor`.
#[derive(Debug, Clone)]
pub struct DiscoveryDiagnostic {
    pub path: PathBuf,
    pub origin: Origin,
    pub message: String,
}

/// Walk `roots` looking for `*/plugin-manifest.json`. Bad manifests are
/// captured in `diagnostics` and skipped — discovery never aborts because
/// one plugin is broken. Returns (plugins, diagnostics).
pub fn discover(roots: &[SearchRoot]) -> (Vec<DiscoveredPlugin>, Vec<DiscoveryDiagnostic>) {
    let mut plugins = Vec::new();
    let mut diagnostics = Vec::new();

    for root in roots {
        if !root.path.exists() {
            tracing::debug!(path = %root.path.display(), origin = ?root.origin, "search root does not exist; skipping");
            continue;
        }
        // We scan with a fixed depth of 2 (root / plugin_dir / manifest) —
        // expects `<root>/<name>/plugin-manifest.toml`. We also allow
        // root-level manifests (depth 1) for single-plugin test fixtures.
        let walker = walkdir::WalkDir::new(&root.path)
            .max_depth(3)
            .follow_links(false)
            .into_iter()
            .filter_map(|entry| match entry {
                Ok(e) => Some(e),
                Err(err) => {
                    tracing::warn!(error = %err, "walkdir error while discovering plugins");
                    None
                }
            });

        for entry in walker {
            if !entry.file_type().is_file() {
                continue;
            }
            if entry.file_name() != MANIFEST_FILENAME {
                continue;
            }
            let path = entry.into_path();
            match parse_manifest_file(&path) {
                Ok(manifest) => {
                    plugins.push(DiscoveredPlugin {
                        manifest,
                        origin: root.origin,
                        manifest_path: path,
                    });
                }
                Err(err) => {
                    let message = err.to_string();
                    tracing::warn!(path = %path.display(), error = %message, "failed to parse plugin manifest");
                    diagnostics.push(DiscoveryDiagnostic {
                        path,
                        origin: root.origin,
                        message,
                    });
                }
            }
        }
    }

    (plugins, diagnostics)
}

/// Parse `CORLINMAN_PLUGIN_DIRS` (colon-separated like `$PATH`) into a list of
/// `Config`-origin search roots. Empty values are ignored.
pub fn roots_from_env_var(var: &str, origin: Origin) -> Vec<SearchRoot> {
    match std::env::var(var) {
        Ok(val) => val
            .split(':')
            .filter(|s| !s.trim().is_empty())
            .map(|s| SearchRoot::new(s.trim(), origin))
            .collect(),
        Err(_) => Vec::new(),
    }
}

/// Surface-level error from a discovery call. `CorlinmanError::from` wraps
/// `ManifestParseError` into `Parse { what: "manifest", ... }`.
impl From<ManifestParseError> for corlinman_core::CorlinmanError {
    fn from(err: ManifestParseError) -> Self {
        Self::Parse {
            what: "plugin-manifest",
            message: err.to_string(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn write_manifest(dir: &Path, name: &str, body: &str) {
        let plugin_dir = dir.join(name);
        fs::create_dir_all(&plugin_dir).unwrap();
        fs::write(plugin_dir.join(MANIFEST_FILENAME), body).unwrap();
    }

    fn minimal(name: &str) -> String {
        format!(
            "name = \"{name}\"\nversion = \"0.1.0\"\nplugin_type = \"sync\"\n[entry_point]\ncommand = \"true\"\n"
        )
    }

    #[test]
    fn discovers_well_formed_manifests() {
        let tmp = tempfile::tempdir().unwrap();
        write_manifest(tmp.path(), "alpha", &minimal("alpha"));
        write_manifest(tmp.path(), "beta", &minimal("beta"));

        let roots = vec![SearchRoot::new(tmp.path(), Origin::Workspace)];
        let (plugins, diags) = discover(&roots);

        assert_eq!(plugins.len(), 2);
        assert!(diags.is_empty());
        let names: Vec<_> = plugins.iter().map(|p| p.manifest.name.clone()).collect();
        assert!(names.contains(&"alpha".to_string()));
        assert!(names.contains(&"beta".to_string()));
    }

    #[test]
    fn bad_manifest_becomes_diagnostic_not_panic() {
        let tmp = tempfile::tempdir().unwrap();
        write_manifest(tmp.path(), "good", &minimal("good"));
        write_manifest(tmp.path(), "bad", "not = valid = toml");

        let roots = vec![SearchRoot::new(tmp.path(), Origin::Config)];
        let (plugins, diags) = discover(&roots);

        assert_eq!(plugins.len(), 1);
        assert_eq!(plugins[0].manifest.name, "good");
        assert_eq!(diags.len(), 1);
        assert!(diags[0].message.to_lowercase().contains("toml"));
    }

    #[test]
    fn missing_search_root_is_silent() {
        let roots = vec![SearchRoot::new(
            "/tmp/definitely-does-not-exist-corlinman",
            Origin::Global,
        )];
        let (plugins, diags) = discover(&roots);
        assert!(plugins.is_empty());
        assert!(diags.is_empty());
    }

    #[test]
    fn origin_rank_matches_precedence_order() {
        assert!(Origin::Bundled.rank() < Origin::Global.rank());
        assert!(Origin::Global.rank() < Origin::Workspace.rank());
        assert!(Origin::Workspace.rank() < Origin::Config.rank());
    }

    #[test]
    fn plugin_dir_is_manifest_parent() {
        let tmp = tempfile::tempdir().unwrap();
        write_manifest(tmp.path(), "alpha", &minimal("alpha"));
        let roots = vec![SearchRoot::new(tmp.path(), Origin::Workspace)];
        let (plugins, _) = discover(&roots);
        assert_eq!(plugins[0].plugin_dir(), tmp.path().join("alpha"));
    }
}
