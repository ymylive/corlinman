//! Plugin registry: deduped, origin-ranked view of discovered manifests.
//!
//! M3 vanguard scope: synchronous discover-and-populate. Hot reload via
//! `notify` is deferred — the scaffold lives here but the watcher loop is
//! gated behind `Registry::start_watcher` which callers opt into.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;

use crate::discovery::{discover, DiscoveredPlugin, DiscoveryDiagnostic, Origin, SearchRoot};
use crate::manifest::PluginManifest;

/// One resolved plugin entry. The registry stores these by `name` (the winning
/// name after origin-rank dedup).
#[derive(Debug, Clone)]
pub struct PluginEntry {
    pub manifest: Arc<PluginManifest>,
    pub origin: Origin,
    pub manifest_path: PathBuf,
    /// Whether another manifest with the same name was shadowed by this one.
    pub shadowed_count: usize,
}

impl PluginEntry {
    pub fn plugin_dir(&self) -> PathBuf {
        self.manifest_path
            .parent()
            .map(Path::to_path_buf)
            .unwrap_or_default()
    }
}

use std::path::Path;

/// Diagnostic types surfaced via `Registry::diagnostics`.
#[derive(Debug, Clone)]
pub enum Diagnostic {
    /// Manifest failed to parse.
    ParseError {
        path: PathBuf,
        origin: Origin,
        message: String,
    },
    /// Two manifests claim the same plugin name. `loser` was dropped.
    NameCollision {
        name: String,
        winner: PathBuf,
        winner_origin: Origin,
        loser: PathBuf,
        loser_origin: Origin,
    },
}

/// Read-only plugin registry populated from a fixed list of search roots.
///
/// For M3 the registry is immutable after construction; hot reload via
/// `notify` arrives with the service runtime work.
#[derive(Debug, Clone, Default)]
pub struct PluginRegistry {
    entries: HashMap<String, PluginEntry>,
    diagnostics: Vec<Diagnostic>,
    roots: Vec<SearchRoot>,
}

impl PluginRegistry {
    /// Construct from a set of search roots, running discovery eagerly.
    pub fn from_roots(roots: Vec<SearchRoot>) -> Self {
        let (plugins, parse_diags) = discover(&roots);
        let (entries, dedup_diags) = resolve(plugins);
        let mut diagnostics: Vec<_> = parse_diags
            .into_iter()
            .map(
                |DiscoveryDiagnostic {
                     path,
                     origin,
                     message,
                 }| Diagnostic::ParseError {
                    path,
                    origin,
                    message,
                },
            )
            .collect();
        diagnostics.extend(dedup_diags);
        Self {
            entries,
            diagnostics,
            roots,
        }
    }

    /// All registered plugins sorted alphabetically by name (stable output
    /// for CLI + snapshot tests).
    pub fn list(&self) -> Vec<&PluginEntry> {
        let mut v: Vec<&PluginEntry> = self.entries.values().collect();
        v.sort_by(|a, b| a.manifest.name.cmp(&b.manifest.name));
        v
    }

    pub fn get(&self, name: &str) -> Option<&PluginEntry> {
        self.entries.get(name)
    }

    pub fn diagnostics(&self) -> &[Diagnostic] {
        &self.diagnostics
    }

    pub fn roots(&self) -> &[SearchRoot] {
        &self.roots
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }
}

/// Apply origin-rank dedup. On equal rank, the manifest discovered first
/// wins — "last write wins within the same origin" by virtue of our walk
/// order being stable.
fn resolve(mut plugins: Vec<DiscoveredPlugin>) -> (HashMap<String, PluginEntry>, Vec<Diagnostic>) {
    // Sort by origin rank *descending* so higher-rank manifests are inserted
    // first; duplicates coming after them are losers.
    plugins.sort_by_key(|p| std::cmp::Reverse(p.origin.rank()));

    let mut out: HashMap<String, PluginEntry> = HashMap::new();
    let mut diags = Vec::new();

    for p in plugins {
        let name = p.manifest.name.clone();
        match out.get_mut(&name) {
            Some(existing) => {
                existing.shadowed_count += 1;
                diags.push(Diagnostic::NameCollision {
                    name: name.clone(),
                    winner: existing.manifest_path.clone(),
                    winner_origin: existing.origin,
                    loser: p.manifest_path.clone(),
                    loser_origin: p.origin,
                });
            }
            None => {
                out.insert(
                    name,
                    PluginEntry {
                        manifest: Arc::new(p.manifest),
                        origin: p.origin,
                        manifest_path: p.manifest_path,
                        shadowed_count: 0,
                    },
                );
            }
        }
    }

    (out, diags)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn scratch_manifest(dir: &std::path::Path, plugin: &str, body: &str) {
        let p = dir.join(plugin);
        fs::create_dir_all(&p).unwrap();
        fs::write(p.join(crate::manifest::MANIFEST_FILENAME), body).unwrap();
    }

    fn body(name: &str, version: &str) -> String {
        format!(
            "name = \"{name}\"\nversion = \"{version}\"\nplugin_type = \"sync\"\n[entry_point]\ncommand = \"true\"\n"
        )
    }

    #[test]
    fn higher_origin_wins_lower_becomes_collision_diag() {
        let low = tempfile::tempdir().unwrap();
        let high = tempfile::tempdir().unwrap();

        scratch_manifest(low.path(), "shared", &body("shared", "0.0.1"));
        scratch_manifest(high.path(), "shared", &body("shared", "9.9.9"));

        let roots = vec![
            SearchRoot::new(low.path(), Origin::Bundled),
            SearchRoot::new(high.path(), Origin::Config),
        ];
        let reg = PluginRegistry::from_roots(roots);

        let entry = reg.get("shared").unwrap();
        assert_eq!(entry.manifest.version, "9.9.9");
        assert_eq!(entry.origin, Origin::Config);
        assert_eq!(reg.diagnostics().len(), 1);
        match &reg.diagnostics()[0] {
            Diagnostic::NameCollision {
                name, loser_origin, ..
            } => {
                assert_eq!(name, "shared");
                assert_eq!(*loser_origin, Origin::Bundled);
            }
            _ => panic!("expected collision"),
        }
    }
}
