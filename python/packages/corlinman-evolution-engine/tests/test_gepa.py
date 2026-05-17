"""Tests for the v0.7 GEPA-lite Pareto scorer.

The scorer takes (variants, episodes) → per-variant (success_overlap,
token_cost, on_frontier). These tests pin:

1. **Determinism.** Same inputs → same outputs across runs.
2. **Frontier shape.** Strict 2D Pareto: a dominates b iff overlap
   strictly higher AND cost strictly lower. Equal-on-both never
   dominates.
3. **Empty signal.** Zero successful episodes → overlap=0.0 across the
   board; the frontier flag still applies.
4. **Input ordering.** Output preserves input order regardless of
   frontier membership.
"""

from __future__ import annotations

import pytest
from corlinman_evolution_engine.gepa import (
    EpisodeSample,
    score_variants,
)


def _eps(*pairs: tuple[str, bool]) -> list[EpisodeSample]:
    return [EpisodeSample(prompt_text=t, succeeded=s) for t, s in pairs]


# ---------------------------------------------------------------------------
# Determinism & input handling
# ---------------------------------------------------------------------------


def test_score_variants_is_deterministic() -> None:
    """Same inputs → same scores. Locks the test fixture so future
    refactors that touch tokenisation can't silently break callers
    that compare scores across runs."""
    variants = ["alpha bravo charlie", "alpha bravo"]
    episodes = _eps(("alpha bravo charlie", True), ("alpha bravo", True))
    a = score_variants(variants, episodes)
    b = score_variants(variants, episodes)
    assert [v.success_overlap for v in a] == [v.success_overlap for v in b]
    assert [v.token_cost for v in a] == [v.token_cost for v in b]
    assert [v.on_frontier for v in a] == [v.on_frontier for v in b]


def test_score_variants_preserves_input_order() -> None:
    """The output index matches the input index. The operator UI
    reads ``output[i]`` for variant ``i`` — order-shuffling would be a
    bug, frontier flag or not."""
    variants = ["long longer longest text here", "short", "medium length text"]
    episodes = _eps(("short", True))
    scored = score_variants(variants, episodes)
    assert [v.text for v in scored] == variants
    assert [v.index for v in scored] == [0, 1, 2]


def test_empty_variants_returns_empty_list() -> None:
    assert score_variants([], _eps(("anything", True))) == []


# ---------------------------------------------------------------------------
# Success overlap semantics
# ---------------------------------------------------------------------------


def test_overlap_is_zero_when_no_successful_episodes() -> None:
    """Operator should see a clear "no historical signal" floor
    rather than a misleading score. Frontier flags still apply so the
    shorter variant remains preferable."""
    variants = ["short", "longer text variant"]
    episodes = _eps(("anything", False), ("else", False))  # all failures
    scored = score_variants(variants, episodes)
    assert all(v.success_overlap == 0.0 for v in scored)


def test_higher_token_overlap_yields_higher_score() -> None:
    """A variant that lexically matches the successful prompts scores
    higher than one that doesn't. Locks the contract that variants
    resembling-what-worked are preferred."""
    variants = [
        "alpha bravo charlie",     # exact overlap with successful prompt
        "delta echo foxtrot",      # zero overlap
    ]
    episodes = _eps(("alpha bravo charlie", True))
    scored = score_variants(variants, episodes)
    assert scored[0].success_overlap > scored[1].success_overlap
    assert scored[0].success_overlap == 1.0
    assert scored[1].success_overlap == 0.0


def test_failed_episodes_do_not_contribute_to_overlap() -> None:
    """Only ``succeeded=True`` episodes feed into the overlap score.
    A variant that matches the failed prompts but mismatches the
    successes must score lower than one that matches the successes."""
    variants = ["matches success", "matches failure"]
    episodes = _eps(
        ("matches success", True),
        ("matches failure", False),
    )
    scored = score_variants(variants, episodes)
    assert scored[0].success_overlap > scored[1].success_overlap


# ---------------------------------------------------------------------------
# Pareto frontier
# ---------------------------------------------------------------------------


def test_frontier_excludes_strictly_dominated_variants() -> None:
    """Strict dominance: a dominates b iff overlap_a > overlap_b AND
    cost_a < cost_b. The dominated variant is off the frontier."""
    variants = [
        "alpha bravo",           # 2 tokens, high overlap
        "alpha bravo charlie delta",  # 4 tokens, lower-or-equal overlap
    ]
    episodes = _eps(("alpha bravo", True))
    scored = score_variants(variants, episodes)
    # variant 0: overlap=1.0 (alpha,bravo == alpha,bravo), cost=2
    # variant 1: overlap=0.5 (2/4 union), cost=4 → strictly dominated.
    assert scored[0].on_frontier is True
    assert scored[1].on_frontier is False


def test_frontier_keeps_equal_score_variants() -> None:
    """Two variants identical on both axes never dominate each other —
    both stay on the frontier. Mirrors the published GEPA paper's
    tie-handling."""
    variants = ["one two three", "four five six"]  # both 3 tokens
    episodes = _eps(("seven eight nine", True))    # zero overlap for both
    scored = score_variants(variants, episodes)
    assert scored[0].on_frontier is True
    assert scored[1].on_frontier is True


def test_frontier_keeps_tradeoff_variants() -> None:
    """High-overlap-high-cost AND low-overlap-low-cost both stay on
    the frontier — neither strictly dominates the other. This is the
    canonical Pareto case."""
    variants = [
        "alpha bravo charlie delta echo",  # high overlap, high cost
        "zulu",                             # low overlap, low cost
    ]
    episodes = _eps(("alpha bravo charlie delta echo", True))
    scored = score_variants(variants, episodes)
    # Both survive — neither strictly dominates.
    assert scored[0].on_frontier is True
    assert scored[1].on_frontier is True


def test_three_way_frontier_picks_the_dominant() -> None:
    """Three variants where one (v1) is strictly dominated by another
    (v0) and a third (v2) trades off. Frontier = {v0, v2}."""
    variants = [
        "alpha bravo",               # overlap=1.0, cost=2 — winner
        "alpha bravo charlie",       # overlap=0.667, cost=3 — dominated by v0
        "zulu",                      # overlap=0.0, cost=1 — survives by cost
    ]
    episodes = _eps(("alpha bravo", True))
    scored = score_variants(variants, episodes)
    assert scored[0].on_frontier is True
    assert scored[1].on_frontier is False
    assert scored[2].on_frontier is True


# ---------------------------------------------------------------------------
# Tokenisation edge cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("variant", "episode_text", "expected_overlap_floor"),
    [
        # Case-insensitive: ALPHA == alpha.
        ("ALPHA bravo", "alpha BRAVO", 1.0),
        # Punctuation gets stripped — same overlap as without.
        ("alpha, bravo!", "alpha bravo", 1.0),
        # Numbers and underscores survive tokenisation.
        ("step_1 alpha", "step_1 alpha", 1.0),
    ],
)
def test_tokenisation_normalises_case_and_punctuation(
    variant: str, episode_text: str, expected_overlap_floor: float
) -> None:
    scored = score_variants([variant], _eps((episode_text, True)))
    assert scored[0].success_overlap >= expected_overlap_floor
