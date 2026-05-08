"""Iter 3 tests — importance scorer.

Each weight from the §"Importance scoring" rubric gets a dedicated
test so a re-tune is forced to acknowledge the change. Scores clamp
to ``[0, 1]`` per the design.
"""

from __future__ import annotations

import pytest
from corlinman_episodes import (
    EpisodeKind,
    HistoryRow,
    HookEventRow,
    IdentityMergeRow,
    SignalRow,
    SourceBundle,
    score,
)
from corlinman_episodes.importance import (
    W_APPLY,
    W_AUTO_ROLLBACK,
    W_IDENTITY_UNIFIED,
    W_ONBOARDING_BASELINE,
    W_OPERATOR_ACTION,
    W_SEVERITY_CRITICAL,
    W_SEVERITY_ERROR,
    W_SIGNAL_CAP,
    W_SIGNAL_PER_ROW,
)


def _bundle() -> SourceBundle:
    return SourceBundle(tenant_id="t", session_key="sess-A")


def _signal(severity: str = "warn", sid: int = 0) -> SignalRow:
    return SignalRow(
        id=sid,
        event_kind="any",
        target="t",
        severity=severity,
        payload_json="{}",
        session_id="sess-A",
        observed_at_ms=10,
    )


# ---------------------------------------------------------------------------
# Empty bundle — only kind affects score
# ---------------------------------------------------------------------------


def test_empty_bundle_scores_zero_for_conversation() -> None:
    """Signals + history + hooks empty + non-onboarding kind → 0.0.

    The 0.0 floor is fine — the doc explicitly says episodes still
    get written, they just sort last.
    """
    assert score(_bundle(), EpisodeKind.CONVERSATION) == 0.0


def test_onboarding_kind_alone_yields_baseline() -> None:
    """``ONBOARDING`` adds 0.1 even without any other signal."""
    assert score(_bundle(), EpisodeKind.ONBOARDING) == pytest.approx(
        W_ONBOARDING_BASELINE
    )


# ---------------------------------------------------------------------------
# Per-component weights
# ---------------------------------------------------------------------------


def test_signal_density_weights_one_per_row() -> None:
    """+0.05 per signal, capped at +0.5 for 10 signals."""
    b = _bundle()
    b.signals.extend(_signal(sid=i) for i in range(3))
    expected = 3 * W_SIGNAL_PER_ROW
    assert score(b, EpisodeKind.CONVERSATION) == pytest.approx(expected)


def test_signal_density_caps_at_ten_signals() -> None:
    """The 11th signal does not contribute."""
    b = _bundle()
    b.signals.extend(_signal(sid=i) for i in range(15))
    assert score(b, EpisodeKind.CONVERSATION) == pytest.approx(W_SIGNAL_CAP)


def test_apply_outcome_weight() -> None:
    """+0.2 per applied history row; multiple applies stack."""
    b = _bundle()
    b.history.append(
        HistoryRow(
            id=1, proposal_id="p", kind="x", target="t",
            applied_at_ms=1, rolled_back_at_ms=None, rollback_reason=None,
            signal_ids=(),
        )
    )
    b.history.append(
        HistoryRow(
            id=2, proposal_id="p", kind="x", target="t",
            applied_at_ms=1, rolled_back_at_ms=None, rollback_reason=None,
            signal_ids=(),
        )
    )
    assert score(b, EpisodeKind.CONVERSATION) == pytest.approx(2 * W_APPLY)


def test_auto_rollback_weight_stacks_on_apply() -> None:
    """+0.2 (apply) + +0.1 (auto_rollback marker) when both fire."""
    b = _bundle()
    b.history.append(
        HistoryRow(
            id=1, proposal_id="p", kind="x", target="t",
            applied_at_ms=1, rolled_back_at_ms=2,
            rollback_reason="auto_rollback fired by metrics monitor",
            signal_ids=(),
        )
    )
    assert score(b, EpisodeKind.CONVERSATION) == pytest.approx(
        W_APPLY + W_AUTO_ROLLBACK
    )


def test_severity_critical_dominates_error() -> None:
    """Single hit per severity tier; critical beats error.

    Multiple critical signals don't multiply — the rubric explicitly
    says "single hit". Pinned because un-noticing this would inflate
    a 50-critical-row incident to a >1.0 score (clamped, but
    misleading).
    """
    b = _bundle()
    b.signals.append(_signal(severity="critical"))
    b.signals.append(_signal(severity="error"))
    expected = (
        W_SIGNAL_PER_ROW * 2  # density component
        + W_SEVERITY_CRITICAL  # critical wins, error contributes nothing
    )
    assert score(b, EpisodeKind.CONVERSATION) == pytest.approx(expected)


def test_severity_error_when_no_critical() -> None:
    b = _bundle()
    b.signals.append(_signal(severity="error"))
    assert score(b, EpisodeKind.CONVERSATION) == pytest.approx(
        W_SIGNAL_PER_ROW + W_SEVERITY_ERROR
    )


def test_operator_action_weight() -> None:
    """+0.1 if any tool_approved or evolution_applied hook present."""
    b = _bundle()
    b.hooks.append(
        HookEventRow(
            id=1, kind="tool_approved", payload_json="{}",
            session_key="sess-A", occurred_at_ms=10,
        )
    )
    assert score(b, EpisodeKind.CONVERSATION) == pytest.approx(W_OPERATOR_ACTION)


def test_identity_unified_weight() -> None:
    """+0.15 for an identity merge in the bundle."""
    b = _bundle()
    b.identity_merges.append(
        IdentityMergeRow(
            id=1, user_a="u", user_b="v", channel="qq",
            occurred_at_ms=10,
        )
    )
    assert score(b, EpisodeKind.CONVERSATION) == pytest.approx(W_IDENTITY_UNIFIED)


# ---------------------------------------------------------------------------
# Composition + ranking
# ---------------------------------------------------------------------------


def test_score_is_pure_function_of_inputs() -> None:
    """Same bundle + kind → same score across runs.

    Pinned by the design's
    ``importance_score_pure_function_of_inputs`` matrix entry.
    """
    b = _bundle()
    b.signals.extend(_signal(severity="error") for _ in range(2))
    b.identity_merges.append(
        IdentityMergeRow(
            id=1, user_a="u", user_b="v", channel="x",
            occurred_at_ms=10,
        )
    )
    s1 = score(b, EpisodeKind.EVOLUTION)
    s2 = score(b, EpisodeKind.EVOLUTION)
    assert s1 == s2


def test_score_clamps_to_unit_interval() -> None:
    """A maximally-noisy bundle is capped at 1.0."""
    b = _bundle()
    b.signals.extend(_signal(severity="critical", sid=i) for i in range(15))
    for i in range(5):
        b.history.append(
            HistoryRow(
                id=i, proposal_id="p", kind="x", target="t",
                applied_at_ms=1, rolled_back_at_ms=2,
                rollback_reason="auto_rollback",
                signal_ids=(),
            )
        )
    b.identity_merges.append(
        IdentityMergeRow(
            id=1, user_a="u", user_b="v", channel="x",
            occurred_at_ms=10,
        )
    )
    b.hooks.append(
        HookEventRow(
            id=1, kind="evolution_applied", payload_json="{}",
            session_key=None, occurred_at_ms=10,
        )
    )
    s = score(b, EpisodeKind.ONBOARDING)
    assert s == 1.0


def test_incident_outranks_chitchat() -> None:
    """The doc-cited test: an auto_rollback bundle ranks above chat-only.

    Wave 4 acceptance: "{{episodes.last_week}} top-5 by importance"
    must surface incidents above conversations.
    """
    incident = _bundle()
    incident.signals.append(_signal(severity="critical"))
    incident.history.append(
        HistoryRow(
            id=1, proposal_id="p", kind="x", target="t",
            applied_at_ms=1, rolled_back_at_ms=2,
            rollback_reason="auto_rollback",
            signal_ids=(),
        )
    )
    chitchat = _bundle()
    chitchat.signals.extend(_signal(sid=i) for i in range(2))

    s_incident = score(incident, EpisodeKind.INCIDENT)
    s_chitchat = score(chitchat, EpisodeKind.CONVERSATION)
    assert s_incident > s_chitchat
