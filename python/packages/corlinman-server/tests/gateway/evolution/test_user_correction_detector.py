"""Tests for :func:`detect_correction` — heuristic user-correction spotter.

The detector is the load-bearing piece of W4.5: it runs inline on every
user chat message, so it MUST be fast, deterministic, and free of false
positives on innocuous text. These tests pin both the positive corpus
(each of the 5 pattern kinds matches a representative sentence) and the
negative corpus (greetings, questions, empty strings).
"""

from __future__ import annotations

import pytest
from corlinman_server.gateway.evolution.signals.user_correction import (
    CorrectionMatch,
    detect_correction,
)


# ─── Positive: one match per kind ─────────────────────────────────────


def test_imperative_stop_matches() -> None:
    match = detect_correction("Stop using bullet points please")
    assert match is not None
    assert match.kind == "imperative"
    assert match.weight == pytest.approx(0.85)
    assert "stop" in match.snippet


def test_imperative_dont_matches() -> None:
    match = detect_correction("Don't format like that.")
    assert match is not None
    assert match.kind == "imperative"


def test_rejection_i_said_matches() -> None:
    match = detect_correction("I already said no markdown")
    assert match is not None
    assert match.kind == "rejection"
    assert match.weight == pytest.approx(0.85)


def test_rejection_no_i_said_matches() -> None:
    match = detect_correction("No, I said to use python not rust")
    assert match is not None
    assert match.kind == "rejection"
    assert match.weight == pytest.approx(0.90)


def test_rejection_thats_not_what_i_matches() -> None:
    match = detect_correction("That's not what I asked for")
    assert match is not None
    assert match.kind == "rejection"


def test_pattern_critique_you_always_matches() -> None:
    match = detect_correction("You always format the code wrong")
    assert match is not None
    assert match.kind == "pattern_critique"
    assert match.weight == pytest.approx(0.80)


def test_pattern_critique_you_keep_matches() -> None:
    match = detect_correction("You keep adding unnecessary comments")
    assert match is not None
    assert match.kind == "pattern_critique"


def test_negative_reaction_i_hate_when_matches() -> None:
    match = detect_correction("I hate when you do that")
    assert match is not None
    assert match.kind == "negative_reaction"
    assert match.weight == pytest.approx(0.75)


def test_negative_reaction_annoying_matches() -> None:
    match = detect_correction("That is really annoying behaviour")
    assert match is not None
    assert match.kind == "negative_reaction"


def test_reformulation_actually_matches() -> None:
    match = detect_correction("Actually, never mind, can you also help with X")
    assert match is not None
    assert match.kind == "reformulation"
    assert match.weight == pytest.approx(0.55)


# ─── Negative: innocuous text must not match ─────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "thanks!",
        "what is the weather",
        "Could you summarise the readme?",
        "Great, that worked.",
        "Here is the diff for review.",
    ],
)
def test_innocuous_text_returns_none(text: str) -> None:
    assert detect_correction(text) is None


def test_empty_string_returns_none() -> None:
    assert detect_correction("") is None


def test_whitespace_only_returns_none() -> None:
    assert detect_correction("   \n\t  ") is None


def test_none_input_returns_none() -> None:
    assert detect_correction(None) is None


# ─── Tie-break behaviour ──────────────────────────────────────────────


def test_higher_weight_wins_on_tie() -> None:
    """Text matching both "actually" (0.55) and "stop" (0.85) should
    return the imperative match because it carries the higher weight.
    """
    text = "Actually, please stop doing that"
    match = detect_correction(text)
    assert match is not None
    assert match.kind == "imperative"
    assert match.weight == pytest.approx(0.85)


def test_rejection_beats_imperative_on_specificity() -> None:
    """"No, I said stop" should resolve to ``rejection`` (0.90), not
    ``imperative`` (0.85), because ``rejection`` is the more specific
    signal of correction.
    """
    match = detect_correction("No, I said stop talking like this")
    assert match is not None
    assert match.kind == "rejection"
    assert match.weight == pytest.approx(0.90)


def test_earliest_position_breaks_tie() -> None:
    """Two equal-weight matches should resolve to the earliest span."""
    # "I said" and "I told you" are both rejection, weight 0.85.
    text = "I told you I said no"
    match = detect_correction(text)
    assert match is not None
    assert match.kind == "rejection"
    # The earliest match's span should start at index 0 ("I told you").
    assert match.span[0] == 0


# ─── Return-shape invariants ──────────────────────────────────────────


def test_match_is_correctionmatch_dataclass() -> None:
    match = detect_correction("Stop it")
    assert isinstance(match, CorrectionMatch)
    assert isinstance(match.matched_pattern, str) and match.matched_pattern
    assert isinstance(match.kind, str) and match.kind
    assert 0.0 <= match.weight <= 1.0
    start, end = match.span
    assert 0 <= start < end


def test_snippet_is_lowercased() -> None:
    """``snippet`` is canonicalised to lowercase so payloads hash stably."""
    match = detect_correction("STOP DOING THAT")
    assert match is not None
    assert match.snippet == match.snippet.lower()


def test_case_insensitive_match() -> None:
    """All patterns use ``re.IGNORECASE``."""
    assert detect_correction("STOP") is not None
    assert detect_correction("Stop") is not None
    assert detect_correction("stop") is not None


def test_detector_is_pure_no_state_carryover() -> None:
    """Re-running the detector on the same input is deterministic."""
    text = "You always do that"
    m1 = detect_correction(text)
    m2 = detect_correction(text)
    assert m1 == m2
