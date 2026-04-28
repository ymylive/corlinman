//! Memory chunk decay arithmetic (Phase 3 W3-A).
//!
//! Chunks accumulate a `decay_score` column on `chunks`. Each recall
//! event applies a `recall_boost`; reads at query time apply
//! `score * 2^(-age_hours / half_life_hours)` so a chunk's effective
//! relevance fades unless it's actively recalled. Promotion to the
//! `consolidated` namespace makes a chunk immune — `decay_score` stops
//! changing and the read-time decay multiplier collapses to 1.0.
//!
//! Pure functions only — the SqliteStore in [`crate::sqlite`] is the
//! callsite. Keeping the math here means we can unit-test the half-life
//! curve without an SQLite roundtrip.
//!
//! ## Why exponential half-life
//!
//! `2^(-age/half_life)` matches the cognitive science convention (Ebbinghaus
//! forgetting curve) and degrades smoothly: at `age == half_life` the
//! score drops to 50% of its current value, at `2*half_life` to 25%, and
//! so on. A linear decay would make recently-recalled chunks fall off
//! too fast (everything below the recall_boost threshold immediately
//! disappears) and slow-burn memories never resurface.

/// Tunables that drive the decay arithmetic. Mirrors
/// `[memory.decay]` in the workspace TOML; populated from
/// `corlinman_core::config::MemoryDecayConfig` at startup.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct DecayConfig {
    /// Master switch. When `false` [`apply_decay`] returns `score`
    /// unchanged so callers can flip decay on/off without rewiring.
    pub enabled: bool,
    /// Age (in hours since the chunk's last recall) at which the
    /// decayed score is half the current `decay_score`. Default 168h
    /// (1 week).
    pub half_life_hours: f64,
    /// Floor below which the decayed score is clamped — prevents a
    /// long-untouched chunk from collapsing to literally 0.0 and
    /// vanishing from RRF blending. Default 0.05.
    pub floor_score: f32,
    /// Bump applied to `decay_score` on every recall. Default 0.3.
    /// Capped at 1.0 by [`boosted_score`].
    pub recall_boost: f32,
}

impl Default for DecayConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            half_life_hours: 168.0,
            floor_score: 0.05,
            recall_boost: 0.3,
        }
    }
}

/// Namespace that's exempt from decay. Promoted chunks land here via
/// `SqliteStore::promote_to_consolidated`; reads short-circuit decay
/// for any row carrying this namespace.
pub const CONSOLIDATED_NAMESPACE: &str = "consolidated";

/// Apply exponential half-life decay to `score` given the chunk's
/// age (hours since last recall — or since creation, when never recalled)
/// and the `namespace` it lives in.
///
/// Semantics:
/// - `cfg.enabled == false` → return `score` unchanged.
/// - `namespace == "consolidated"` → return `score` unchanged (immune).
/// - Otherwise: `score * 2^(-age/half_life)`, clamped at
///   `cfg.floor_score`.
///
/// Negative or NaN ages are clamped to 0 — callers shouldn't pass
/// them, but defending here keeps the math from going wild on a clock
/// skew.
pub fn apply_decay(score: f32, age_hours: f64, namespace: &str, cfg: &DecayConfig) -> f32 {
    if !cfg.enabled {
        return score;
    }
    if namespace == CONSOLIDATED_NAMESPACE {
        return score;
    }
    let age = if age_hours.is_finite() && age_hours > 0.0 {
        age_hours
    } else {
        0.0
    };
    let half_life = if cfg.half_life_hours.is_finite() && cfg.half_life_hours > 0.0 {
        cfg.half_life_hours
    } else {
        // A misconfigured zero half-life would NaN-infect the whole
        // pipeline; bail to "no decay" instead.
        return score;
    };
    let factor = (-age / half_life * std::f64::consts::LN_2).exp() as f32;
    let decayed = score * factor;
    decayed.max(cfg.floor_score)
}

/// Apply `recall_boost` to a current `decay_score`, capped at 1.0.
///
/// Callers persist this back to `chunks.decay_score` after a successful
/// recall — that's the only write path that touches the column.
pub fn boosted_score(current: f32, recall_boost: f32) -> f32 {
    (current + recall_boost).min(1.0)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg() -> DecayConfig {
        DecayConfig::default()
    }

    #[test]
    fn no_decay_at_age_zero() {
        let out = apply_decay(0.8, 0.0, "general", &cfg());
        assert!((out - 0.8).abs() < 1e-5, "got {out}");
    }

    #[test]
    fn half_life_drops_to_half() {
        let c = cfg();
        let out = apply_decay(0.8, c.half_life_hours, "general", &c);
        // 0.8 * 0.5 = 0.4 — well above the 0.05 floor.
        assert!((out - 0.4).abs() < 1e-4, "got {out}");
    }

    #[test]
    fn two_half_lives_drops_to_quarter() {
        let c = cfg();
        let out = apply_decay(0.8, c.half_life_hours * 2.0, "general", &c);
        assert!((out - 0.2).abs() < 1e-4, "got {out}");
    }

    #[test]
    fn floor_clamps_long_age() {
        let c = cfg();
        // 10x half-life: 0.8 * 2^-10 ≈ 0.00078, well below floor.
        let out = apply_decay(0.8, c.half_life_hours * 10.0, "general", &c);
        assert!(
            (out - c.floor_score).abs() < 1e-6,
            "expected floor {}, got {out}",
            c.floor_score
        );
    }

    #[test]
    fn consolidated_namespace_is_immune() {
        let c = cfg();
        // Even at 100 half-lives the score is unchanged.
        let out = apply_decay(0.8, c.half_life_hours * 100.0, CONSOLIDATED_NAMESPACE, &c);
        assert_eq!(out, 0.8);
    }

    #[test]
    fn disabled_returns_input_unchanged() {
        let mut c = cfg();
        c.enabled = false;
        let out = apply_decay(0.8, 1_000.0, "general", &c);
        assert_eq!(out, 0.8);
    }

    #[test]
    fn negative_age_treated_as_zero() {
        let c = cfg();
        let out = apply_decay(0.7, -42.0, "general", &c);
        assert!((out - 0.7).abs() < 1e-5);
    }

    #[test]
    fn zero_half_life_falls_back_to_no_decay() {
        let mut c = cfg();
        c.half_life_hours = 0.0;
        let out = apply_decay(0.7, 168.0, "general", &c);
        assert_eq!(out, 0.7);
    }

    #[test]
    fn boosted_score_caps_at_one() {
        // 0.8 + 0.3 = 1.1 → cap to 1.0
        assert_eq!(boosted_score(0.8, 0.3), 1.0);
    }

    #[test]
    fn boosted_score_below_cap_just_adds() {
        let out = boosted_score(0.4, 0.3);
        assert!((out - 0.7).abs() < 1e-6, "got {out}");
    }

    #[test]
    fn boosted_score_zero_boost_is_identity() {
        assert_eq!(boosted_score(0.42, 0.0), 0.42);
    }
}
