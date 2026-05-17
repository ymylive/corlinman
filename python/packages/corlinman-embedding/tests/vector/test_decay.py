"""Decay arithmetic — mirror of `decay.rs` unit tests."""

from __future__ import annotations

import math

from corlinman_embedding.vector.decay import (
    CONSOLIDATED_NAMESPACE,
    DecayConfig,
    apply_decay,
    boosted_score,
)


def cfg() -> DecayConfig:
    return DecayConfig()


def test_no_decay_at_age_zero() -> None:
    out = apply_decay(0.8, 0.0, "general", cfg())
    assert math.isclose(out, 0.8, abs_tol=1e-5)


def test_half_life_drops_to_half() -> None:
    c = cfg()
    out = apply_decay(0.8, c.half_life_hours, "general", c)
    assert math.isclose(out, 0.4, abs_tol=1e-4)


def test_two_half_lives_drops_to_quarter() -> None:
    c = cfg()
    out = apply_decay(0.8, c.half_life_hours * 2.0, "general", c)
    assert math.isclose(out, 0.2, abs_tol=1e-4)


def test_floor_clamps_long_age() -> None:
    c = cfg()
    out = apply_decay(0.8, c.half_life_hours * 10.0, "general", c)
    assert math.isclose(out, c.floor_score, abs_tol=1e-6)


def test_consolidated_namespace_is_immune() -> None:
    c = cfg()
    out = apply_decay(0.8, c.half_life_hours * 100.0, CONSOLIDATED_NAMESPACE, c)
    assert out == 0.8


def test_disabled_returns_input_unchanged() -> None:
    c = DecayConfig(enabled=False)
    out = apply_decay(0.8, 1_000.0, "general", c)
    assert out == 0.8


def test_negative_age_treated_as_zero() -> None:
    out = apply_decay(0.7, -42.0, "general", cfg())
    assert math.isclose(out, 0.7, abs_tol=1e-5)


def test_zero_half_life_falls_back_to_no_decay() -> None:
    c = DecayConfig(half_life_hours=0.0)
    out = apply_decay(0.7, 168.0, "general", c)
    assert out == 0.7


def test_boosted_score_caps_at_one() -> None:
    assert boosted_score(0.8, 0.3) == 1.0


def test_boosted_score_below_cap_just_adds() -> None:
    assert math.isclose(boosted_score(0.4, 0.3), 0.7, abs_tol=1e-6)


def test_boosted_score_zero_boost_is_identity() -> None:
    assert boosted_score(0.42, 0.0) == 0.42
