//! Content-addressed in-memory cache for [`RenderedArtifact`]s.
//!
//! `phase4-w3-c3-design.md` § "Caching" specifies the shape:
//!
//! ```text
//! cache_key = blake3( artifact_kind || canonicalize_json(body)
//!                   || theme_hint || RENDERER_VERSION )
//! ```
//!
//! ## Why blake3
//!
//! - **Pure Rust, no C deps** — matches the rest of the canvas crate.
//! - **Fixed 32-byte digest** — `[u8; 32]` makes a `Copy`-friendly
//!   `LruCache` key with no per-lookup allocation.
//! - **Order of magnitude faster than SHA-256** on the diagram /
//!   table sizes Canvas sees (KB-scale). Cost is negligible relative
//!   to syntect / katex parse time.
//!
//! ## Why include the renderer version in the key
//!
//! When syntect themes, the bundled `mermaid.min.js`, or katex
//! options change, identical inputs produce different output bytes.
//! A version bump invalidates the in-memory cache without any LRU
//! plumbing — every old key naturally misses.
//!
//! ## Why theme is part of the key
//!
//! Code / table / latex / sparkline render byte-identical across
//! themes (class-only HTML), but mermaid post-processing varies
//! stroke colours per theme. Including `theme_hint` in the key keeps
//! mermaid-correct without a per-kind branch in the cache layer.
//!
//! ## Capacity 0 = disabled
//!
//! `[canvas] cache_max_entries = 0` is the operator's kill-switch.
//! [`RenderCache::new(0)`] returns a cache whose [`get`] always
//! misses and [`insert`] is a no-op — no `LruCache` is constructed,
//! so there is no per-render allocation cost on the disabled path.
//!
//! [`get`]: RenderCache::get
//! [`insert`]: RenderCache::insert

use std::num::NonZeroUsize;
use std::sync::{Arc, Mutex};

use lru::LruCache;

use crate::protocol::{ArtifactBody, ArtifactKind, RenderedArtifact, ThemeClass};

/// Bumped whenever rendering output bytes change for the same input
/// (e.g. syntect theme rev, katex option churn, mermaid bundle swap).
/// Old keys instantly miss; the LRU naturally rotates them out.
pub const RENDERER_VERSION: u32 = 1;

/// 32-byte blake3 digest used as both the cache key *and* the
/// `content_hash` echoed in [`RenderedArtifact`].
pub type CacheKey = [u8; 32];

/// Content-addressed LRU. Cheap to clone (single `Arc<Mutex<…>>`).
///
/// Concurrency: a single `Mutex` guards the underlying [`LruCache`];
/// the protected section is two `HashMap` operations on a 32-byte
/// key, dwarfed by the render work it gates. The mutex is internal
/// to keep the `Renderer` `Send + Sync` without bubbling lock
/// generics out to callers.
#[derive(Clone)]
pub struct RenderCache {
    /// `None` ↔ disabled (capacity 0). Constructed once at
    /// `RenderCache::new`; never flips at runtime.
    inner: Option<Arc<Mutex<LruCache<CacheKey, Arc<RenderedArtifact>>>>>,
}

impl std::fmt::Debug for RenderCache {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match &self.inner {
            None => f.debug_struct("RenderCache").field("disabled", &true).finish(),
            Some(c) => {
                let len = c.lock().map(|g| g.len()).unwrap_or(0);
                f.debug_struct("RenderCache")
                    .field("disabled", &false)
                    .field("len", &len)
                    .finish()
            }
        }
    }
}

impl Default for RenderCache {
    /// Disabled cache. Use [`RenderCache::new`] with a non-zero
    /// capacity to actually cache anything; iter 7 keeps the
    /// `Renderer::default()` path cache-free so existing tests are
    /// untouched.
    fn default() -> Self {
        Self { inner: None }
    }
}

impl RenderCache {
    /// Construct a cache with the given capacity. `0` returns a
    /// permanently-disabled cache (no allocation; every [`get`]
    /// misses).
    ///
    /// [`get`]: RenderCache::get
    pub fn new(capacity: usize) -> Self {
        match NonZeroUsize::new(capacity) {
            None => Self { inner: None },
            Some(cap) => Self {
                inner: Some(Arc::new(Mutex::new(LruCache::new(cap)))),
            },
        }
    }

    /// `true` if the cache is the no-op variant.
    pub fn is_disabled(&self) -> bool {
        self.inner.is_none()
    }

    /// Current entry count. `0` for the disabled variant. Useful for
    /// test assertions and `/admin/canvas/stats` if it ships.
    pub fn len(&self) -> usize {
        match &self.inner {
            None => 0,
            Some(c) => c.lock().map(|g| g.len()).unwrap_or(0),
        }
    }

    /// `true` when [`len`] is `0`.
    ///
    /// [`len`]: RenderCache::len
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// Lookup. Returns `None` if disabled or miss; both look identical
    /// to the caller, which is the point.
    pub fn get(&self, key: &CacheKey) -> Option<Arc<RenderedArtifact>> {
        let inner = self.inner.as_ref()?;
        let mut guard = inner.lock().ok()?;
        guard.get(key).cloned()
    }

    /// Insert and return the same `Arc` (so callers can hand it to
    /// the next layer without re-locking). No-op when disabled.
    pub fn insert(
        &self,
        key: CacheKey,
        artifact: Arc<RenderedArtifact>,
    ) -> Arc<RenderedArtifact> {
        if let Some(inner) = self.inner.as_ref() {
            if let Ok(mut guard) = inner.lock() {
                guard.put(key, artifact.clone());
            }
        }
        artifact
    }
}

/// Compute the cache key for a `(kind, body, theme)` triple.
///
/// Body is canonicalised through [`canonical_json_bytes`] so semantically
/// identical inputs hash equal regardless of producer field ordering or
/// whitespace.
///
/// The output is also written into [`RenderedArtifact::content_hash`]
/// (lower-case hex), so clients can dedup network responses without
/// re-hashing the HTML fragment.
pub fn key_for(kind: ArtifactKind, body: &ArtifactBody, theme: ThemeClass) -> CacheKey {
    let mut hasher = blake3::Hasher::new();
    hasher.update(&RENDERER_VERSION.to_le_bytes());
    hasher.update(kind.as_str().as_bytes());
    hasher.update(&[0u8]); // delimiter — keeps `code|x` ≠ `cod|ex`
    hasher.update(theme_tag(theme).as_bytes());
    hasher.update(&[0u8]);
    let body_bytes = canonical_json_bytes(body);
    hasher.update(&body_bytes);
    *hasher.finalize().as_bytes()
}

/// Lower-case hex form of a [`CacheKey`]. 64 chars; fits in
/// [`RenderedArtifact::content_hash`] without allocation churn.
pub fn key_to_hex(key: &CacheKey) -> String {
    let mut out = String::with_capacity(64);
    for b in key {
        use std::fmt::Write;
        let _ = write!(&mut out, "{b:02x}");
    }
    out
}

/// Stable string tag for a [`ThemeClass`] used inside the cache key.
fn theme_tag(theme: ThemeClass) -> &'static str {
    match theme {
        ThemeClass::TpLight => "tp-light",
        ThemeClass::TpDark => "tp-dark",
    }
}

/// Canonicalise the body to a deterministic byte sequence.
///
/// Producer JSON ordering is undefined (`serde_json::Value` preserves
/// insertion order, but two producers may emit `{values, unit}` vs
/// `{unit, values}`). We round-trip through [`serde_json::to_value`]
/// then walk it sorting object keys, so two semantically equal bodies
/// always hash identically.
pub fn canonical_json_bytes(body: &ArtifactBody) -> Vec<u8> {
    // serde_json::to_value never fails for owned simple structs.
    let v = serde_json::to_value(body).unwrap_or(serde_json::Value::Null);
    let canon = canonicalize(&v);
    serde_json::to_vec(&canon).unwrap_or_default()
}

fn canonicalize(v: &serde_json::Value) -> serde_json::Value {
    use serde_json::Value;
    match v {
        Value::Object(map) => {
            let mut sorted: Vec<(&String, &Value)> = map.iter().collect();
            sorted.sort_by(|a, b| a.0.cmp(b.0));
            let mut out = serde_json::Map::with_capacity(sorted.len());
            for (k, vv) in sorted {
                out.insert(k.clone(), canonicalize(vv));
            }
            Value::Object(out)
        }
        Value::Array(items) => Value::Array(items.iter().map(canonicalize).collect()),
        other => other.clone(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::protocol::{ArtifactBody, ArtifactKind, RenderedArtifact, ThemeClass};

    fn art(kind: ArtifactKind, html: &str) -> Arc<RenderedArtifact> {
        Arc::new(RenderedArtifact {
            html_fragment: html.into(),
            theme_class: ThemeClass::TpLight,
            content_hash: String::new(),
            render_kind: kind,
            warnings: vec![],
        })
    }

    fn body_a() -> ArtifactBody {
        ArtifactBody::Code {
            language: "rust".into(),
            source: "fn main() {}".into(),
        }
    }

    fn body_b() -> ArtifactBody {
        ArtifactBody::Code {
            language: "rust".into(),
            source: "fn other() {}".into(),
        }
    }

    #[test]
    fn disabled_cache_always_misses() {
        let cache = RenderCache::new(0);
        assert!(cache.is_disabled());
        let k = key_for(ArtifactKind::Code, &body_a(), ThemeClass::TpLight);
        assert!(cache.get(&k).is_none());
        cache.insert(k, art(ArtifactKind::Code, "<pre/>"));
        assert!(cache.get(&k).is_none(), "insert is a no-op when disabled");
        assert_eq!(cache.len(), 0);
        assert!(cache.is_empty());
    }

    #[test]
    fn enabled_cache_returns_same_arc_on_hit() {
        let cache = RenderCache::new(8);
        assert!(!cache.is_disabled());

        let k = key_for(ArtifactKind::Code, &body_a(), ThemeClass::TpLight);
        let stored = art(ArtifactKind::Code, "<pre>orig</pre>");
        cache.insert(k, stored.clone());

        let hit = cache.get(&k).expect("hit");
        // Same Arc identity, not a clone of the inner data.
        assert!(Arc::ptr_eq(&hit, &stored));
        assert_eq!(cache.len(), 1);
    }

    #[test]
    fn cache_evicts_at_capacity() {
        let cache = RenderCache::new(2);

        let k1 = key_for(ArtifactKind::Code, &body_a(), ThemeClass::TpLight);
        let k2 = key_for(ArtifactKind::Code, &body_b(), ThemeClass::TpLight);
        let k3 = key_for(
            ArtifactKind::Code,
            &ArtifactBody::Code {
                language: "rust".into(),
                source: "fn third() {}".into(),
            },
            ThemeClass::TpLight,
        );

        cache.insert(k1, art(ArtifactKind::Code, "1"));
        cache.insert(k2, art(ArtifactKind::Code, "2"));
        // Touch k1 so k2 becomes the LRU victim.
        let _ = cache.get(&k1);
        cache.insert(k3, art(ArtifactKind::Code, "3"));

        assert_eq!(cache.len(), 2);
        assert!(cache.get(&k1).is_some(), "recently-touched k1 retained");
        assert!(cache.get(&k3).is_some(), "freshly-inserted k3 retained");
        assert!(cache.get(&k2).is_none(), "LRU k2 evicted");
    }

    #[test]
    fn key_is_deterministic() {
        let k1 = key_for(ArtifactKind::Code, &body_a(), ThemeClass::TpLight);
        let k2 = key_for(ArtifactKind::Code, &body_a(), ThemeClass::TpLight);
        assert_eq!(k1, k2);
    }

    #[test]
    fn key_differs_by_kind() {
        let code = key_for(ArtifactKind::Code, &body_a(), ThemeClass::TpLight);
        // Same source bytes, different kind discriminator.
        let table = key_for(
            ArtifactKind::Table,
            &ArtifactBody::Table {
                markdown: Some("|a|\n|-|\n|1|".into()),
                csv: None,
            },
            ThemeClass::TpLight,
        );
        assert_ne!(code, table);
    }

    #[test]
    fn key_differs_by_theme() {
        let light = key_for(ArtifactKind::Code, &body_a(), ThemeClass::TpLight);
        let dark = key_for(ArtifactKind::Code, &body_a(), ThemeClass::TpDark);
        assert_ne!(light, dark);
    }

    #[test]
    fn key_to_hex_is_64_lowercase_hex() {
        let k = key_for(ArtifactKind::Code, &body_a(), ThemeClass::TpLight);
        let hex = key_to_hex(&k);
        assert_eq!(hex.len(), 64);
        assert!(hex.chars().all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()));
    }

    #[test]
    fn canonical_json_sorts_object_keys() {
        // ArtifactBody serialises to fixed field order, but the
        // canonicaliser is generic; sanity-check it on a hand-built
        // out-of-order Value.
        let unsorted = serde_json::json!({"b": 2, "a": 1, "c": {"y": 9, "x": 8}});
        let canon = canonicalize(&unsorted);
        let bytes = serde_json::to_vec(&canon).unwrap();
        let s = String::from_utf8(bytes).unwrap();
        assert_eq!(s, r#"{"a":1,"b":2,"c":{"x":8,"y":9}}"#);
    }
}
