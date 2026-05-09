"""Iter 3 tests — heuristic ``EpisodeKind`` classifier.

Pin the rule precedence verbatim against the design doc: an
auto-rollback dominates an apply, an apply dominates an operator
hook, an operator hook dominates onboarding, onboarding dominates
the conversation default. Each rule has its own test so a future
refactor can't silently re-order them.
"""

from __future__ import annotations

from corlinman_episodes import (
    EpisodeKind,
    HistoryRow,
    HookEventRow,
    SessionMessage,
    SignalRow,
    SourceBundle,
    classify,
)


def _empty_bundle() -> SourceBundle:
    """Bundle with no rows — falls through to CONVERSATION default."""
    return SourceBundle(tenant_id="t", session_key="sess-A")


def _hook(kind: str, *, ts: int = 100) -> HookEventRow:
    return HookEventRow(
        id=ts, kind=kind, payload_json="{}", session_key="sess-A",
        occurred_at_ms=ts,
    )


def _history(*, rolled_back: bool = False) -> HistoryRow:
    return HistoryRow(
        id=1,
        proposal_id="prop-1",
        kind="skill_update",
        target="web_search",
        applied_at_ms=100,
        rolled_back_at_ms=200 if rolled_back else None,
        rollback_reason="auto_rollback" if rolled_back else None,
        signal_ids=(1,),
    )


def _signal() -> SignalRow:
    return SignalRow(
        id=1,
        event_kind="tool.timeout",
        target="web_search",
        severity="warn",
        payload_json="{}",
        session_id="sess-A",
        observed_at_ms=50,
    )


# ---------------------------------------------------------------------------
# Rule precedence
# ---------------------------------------------------------------------------


def test_auto_rollback_wins_over_apply() -> None:
    """An auto-rollback fire dominates even when an apply is also present."""
    b = _empty_bundle()
    b.history.append(_history(rolled_back=False))
    b.hooks.append(_hook("auto_rollback_fired"))
    assert classify(b) == EpisodeKind.INCIDENT


def test_apply_wins_over_operator_hook() -> None:
    """An apply outranks a tool_approved hook on its own.

    Without this precedence an evolution apply that *also* triggered
    an operator-approved tool would mis-classify as OPERATOR.
    """
    b = _empty_bundle()
    b.history.append(_history())
    b.hooks.append(_hook("tool_approved"))
    assert classify(b) == EpisodeKind.EVOLUTION


def test_operator_hook_wins_over_onboarding() -> None:
    """An operator approve / deny outranks onboarding."""
    b = _empty_bundle()
    b.hooks.append(_hook("tool_approved"))
    assert classify(b, is_onboarding=True) == EpisodeKind.OPERATOR


def test_onboarding_when_runner_says_so() -> None:
    """The classifier itself doesn't read sessions DB — runner injects."""
    b = _empty_bundle()
    b.messages.append(
        SessionMessage(
            session_key="sess-A", seq=0, role="user", content="hi", ts_ms=10
        )
    )
    assert classify(b, is_onboarding=True) == EpisodeKind.ONBOARDING


def test_default_is_conversation() -> None:
    """A signal-bearing window without history / hooks → CONVERSATION."""
    b = _empty_bundle()
    b.signals.append(_signal())
    b.messages.append(
        SessionMessage(
            session_key="sess-A", seq=0, role="user", content="hi", ts_ms=10
        )
    )
    assert classify(b) == EpisodeKind.CONVERSATION


def test_classify_is_pure() -> None:
    """Same bundle → same kind across calls.

    Pinned because the runner relies on it for the iter-2
    natural-key probe (a crashed re-run reproduces the same kind).
    """
    b = _empty_bundle()
    b.history.append(_history())
    first = classify(b)
    second = classify(b)
    third = classify(b)
    assert first == second == third == EpisodeKind.EVOLUTION
