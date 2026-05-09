"""Tests for :class:`corlinman_goals.placeholders.GoalsResolver` (iter 3).

Covers the five ``placeholder_*`` rows in the design test matrix:

- ``placeholder_today_renders_active_short``
- ``placeholder_weekly_includes_last_week_scores``
- ``placeholder_quarterly_aggregates_weekly_scores``
- ``placeholder_failing_filters_by_recent_score_under_5``
- ``placeholder_unknown_subkey_returns_empty``

Plus the empty-agent guard, the sentinel exclusion, and the
``- … (+N more)`` truncation suffix adopted from open-question §4 of
the design.

Tests stub ``time.time()`` via ``monkeypatch`` so the resolver sees a
deterministic clock without thread-locals — same approach the persona
suite uses (see ``test_decay`` in that package).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from corlinman_goals.placeholders import (
    FAILING_LINE_CAP,
    NO_EVIDENCE_SENTINEL,
    WEEKLY_LINE_CAP,
    GoalsResolver,
)
from corlinman_goals.state import Goal, GoalEvaluation
from corlinman_goals.store import GoalStore

_NOW = int(
    datetime(2026, 5, 9, 14, 0, tzinfo=UTC).timestamp() * 1000
)  # Saturday afternoon UTC
_DAY_MS = 86_400 * 1000
_WEEK_MS = 7 * _DAY_MS


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "agent_goals.sqlite"


@pytest.fixture(autouse=True)
def _freeze_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the resolver's ``_now_ms`` to ``_NOW``. Autouse because every
    placeholder test needs a deterministic clock; threading the value
    through the resolver constructor would balloon the API."""
    monkeypatch.setattr(
        "corlinman_goals.placeholders._now_ms", lambda: _NOW
    )


def _g(
    *,
    goal_id: str,
    tier: str = "short",
    body: str = "do thing",
    status: str = "active",
    target_date_ms: int = _NOW + _DAY_MS,
    created_at_ms: int = _NOW - _DAY_MS,
    parent_goal_id: str | None = None,
    agent_id: str = "mentor",
) -> Goal:
    return Goal(
        id=goal_id,
        agent_id=agent_id,
        tier=tier,
        body=body,
        created_at_ms=created_at_ms,
        target_date_ms=target_date_ms,
        parent_goal_id=parent_goal_id,
        status=status,
        source="operator_cli",
    )


def _ev(
    goal: Goal,
    *,
    evaluated_at_ms: int,
    score: int,
    narrative: str = "...",
    evidence_episode_ids: list[str] | None = None,
) -> GoalEvaluation:
    return GoalEvaluation(
        goal_id=goal.id,
        evaluated_at_ms=evaluated_at_ms,
        score_0_to_10=score,
        narrative=narrative,
        evidence_episode_ids=evidence_episode_ids or [],
        reflection_run_id=f"r-{goal.id}-{evaluated_at_ms}",
    )


# ---------------------------------------------------------------------------
# today — short/active/in-window only, bare bullets, no scores
# ---------------------------------------------------------------------------


async def test_today_renders_active_short_only(db_path: Path) -> None:
    async with GoalStore(db_path) as store:
        # Active short, future-dated → included.
        await store.insert_goal(
            _g(goal_id="s1", body="ship feature", target_date_ms=_NOW + _DAY_MS)
        )
        # Active short, past target_date → excluded (already matured).
        await store.insert_goal(
            _g(
                goal_id="s2-past",
                body="yesterday",
                target_date_ms=_NOW - _DAY_MS,
            )
        )
        # Mid-tier → wrong tier.
        await store.insert_goal(
            _g(goal_id="m1", tier="mid", body="this week", target_date_ms=_NOW + _WEEK_MS)
        )
        # Archived short → wrong status.
        await store.insert_goal(
            _g(
                goal_id="s3-archived",
                body="dropped",
                status="archived",
                target_date_ms=_NOW + _DAY_MS,
            )
        )
        resolver = GoalsResolver(store)
        out = await resolver.resolve("today", "mentor")

    assert out == "- ship feature"


async def test_today_empty_when_no_active_short(db_path: Path) -> None:
    async with GoalStore(db_path) as store:
        # Only mid-tier goals exist for this agent.
        await store.insert_goal(_g(goal_id="m1", tier="mid"))
        resolver = GoalsResolver(store)
        out = await resolver.resolve("today", "mentor")
    assert out == ""


async def test_today_orders_by_created_at(db_path: Path) -> None:
    """Insertion order is decoupled from display order — the design
    pins "ordered by created_at" so two prompt renders stay stable."""
    async with GoalStore(db_path) as store:
        await store.insert_goal(
            _g(goal_id="late", body="late", created_at_ms=_NOW - 60)
        )
        await store.insert_goal(
            _g(goal_id="early", body="early", created_at_ms=_NOW - 7200)
        )
        resolver = GoalsResolver(store)
        out = await resolver.resolve("today", "mentor")
    assert out == "- early\n- late"


# ---------------------------------------------------------------------------
# weekly — mid bodies + previous-week scored bullets, capped at 8
# ---------------------------------------------------------------------------


async def test_weekly_includes_last_week_scores(db_path: Path) -> None:
    """Mid goal with an evaluation in [now - 2w, now - 1w] gets a scored
    bullet; one without falls back to a bare body."""
    async with GoalStore(db_path) as store:
        m1 = _g(goal_id="m1", tier="mid", body="learn rust")
        m2 = _g(goal_id="m2", tier="mid", body="ship docs")
        await store.insert_goal(m1)
        await store.insert_goal(m2)

        prev_week_mid = _NOW - int(1.5 * _WEEK_MS)
        await store.insert_evaluation(
            _ev(m1, evaluated_at_ms=prev_week_mid, score=7, narrative="solid week")
        )
        # m2 has no eval in the previous-week window → bare body.

        resolver = GoalsResolver(store)
        out = await resolver.resolve("weekly", "mentor")

    assert out.splitlines() == [
        "- learn rust: score 7 — solid week",
        "- ship docs",
    ]


async def test_weekly_skips_no_evidence_sentinel(db_path: Path) -> None:
    """Sentinel evaluations are no-signal — they shouldn't displace a
    bare-body fallback. The resolver treats them as if they didn't
    exist for previous-week lookup."""
    async with GoalStore(db_path) as store:
        m1 = _g(goal_id="m1", tier="mid", body="goal-A")
        await store.insert_goal(m1)
        # Sentinel eval inside the previous-week window.
        await store.insert_evaluation(
            _ev(
                m1,
                evaluated_at_ms=_NOW - int(1.5 * _WEEK_MS),
                score=0,
                narrative=NO_EVIDENCE_SENTINEL,
            )
        )
        resolver = GoalsResolver(store)
        out = await resolver.resolve("weekly", "mentor")
    # Bare body, not a "score 0 — no_evidence" line.
    assert out == "- goal-A"


async def test_weekly_caps_at_eight_lines_with_truncation_suffix(
    db_path: Path,
) -> None:
    """``WEEKLY_LINE_CAP`` lines max; the cap-th slot is reserved for the
    ``(+N more)`` suffix so the prompt knows truncation happened."""
    async with GoalStore(db_path) as store:
        for i in range(WEEKLY_LINE_CAP + 3):
            await store.insert_goal(
                _g(
                    goal_id=f"m{i}",
                    tier="mid",
                    body=f"goal-{i}",
                    created_at_ms=_NOW - 1000 + i,
                )
            )
        resolver = GoalsResolver(store)
        out = await resolver.resolve("weekly", "mentor")
    lines = out.splitlines()
    assert len(lines) == WEEKLY_LINE_CAP
    # Last line is the truncation suffix; remaining count = total - kept.
    assert lines[-1] == "- … (+4 more)"


async def test_weekly_empty_when_no_active_mid_goals(db_path: Path) -> None:
    async with GoalStore(db_path) as store:
        await store.insert_goal(_g(goal_id="s1"))
        resolver = GoalsResolver(store)
        out = await resolver.resolve("weekly", "mentor")
    assert out == ""


# ---------------------------------------------------------------------------
# quarterly — long bodies + trailing-12-week roll-up
# ---------------------------------------------------------------------------


async def test_quarterly_aggregates_weekly_scores(db_path: Path) -> None:
    """Trailing 12 weeks of mid-tier scores: avg/min/count_failing.
    Sentinel rows excluded from the aggregate."""
    async with GoalStore(db_path) as store:
        long_g = _g(goal_id="L1", tier="long", body="own infra")
        await store.insert_goal(long_g)
        m1 = _g(goal_id="M1", tier="mid", body="mid-A")
        m2 = _g(goal_id="M2", tier="mid", body="mid-B")
        await store.insert_goal(m1)
        await store.insert_goal(m2)

        # Three in-window mid-tier scores: 8, 4, 2 → avg 4.67, min 2,
        # count_failing 2.
        await store.insert_evaluation(
            _ev(m1, evaluated_at_ms=_NOW - 1 * _WEEK_MS, score=8)
        )
        await store.insert_evaluation(
            _ev(m1, evaluated_at_ms=_NOW - 5 * _WEEK_MS, score=4)
        )
        await store.insert_evaluation(
            _ev(m2, evaluated_at_ms=_NOW - 10 * _WEEK_MS, score=2)
        )
        # Out-of-window (older than 12w) → excluded.
        await store.insert_evaluation(
            _ev(m1, evaluated_at_ms=_NOW - 13 * _WEEK_MS, score=10)
        )
        # Sentinel → excluded.
        await store.insert_evaluation(
            _ev(
                m2,
                evaluated_at_ms=_NOW - 2 * _WEEK_MS,
                score=0,
                narrative=NO_EVIDENCE_SENTINEL,
            )
        )

        resolver = GoalsResolver(store)
        out = await resolver.resolve("quarterly", "mentor")

    lines = out.splitlines()
    assert lines[0] == "- own infra"
    # Roll-up line shape: "- mid-tier last 12w: avg X.X, min Y, count_failing Z"
    rollup = lines[-1]
    assert "mid-tier last 12w" in rollup
    assert "avg 4.7" in rollup
    assert "min 2" in rollup
    assert "count_failing 2" in rollup


async def test_quarterly_empty_when_no_long_goal(db_path: Path) -> None:
    """No long-tier anchor → empty placeholder, even if mid scores exist
    (the prompt would be a header-only fragment otherwise)."""
    async with GoalStore(db_path) as store:
        m1 = _g(goal_id="M1", tier="mid", body="mid")
        await store.insert_goal(m1)
        await store.insert_evaluation(
            _ev(m1, evaluated_at_ms=_NOW - _WEEK_MS, score=8)
        )
        resolver = GoalsResolver(store)
        out = await resolver.resolve("quarterly", "mentor")
    assert out == ""


async def test_quarterly_long_only_when_no_mid_evaluations(
    db_path: Path,
) -> None:
    """Long bullet stays even if there are no mid scores — the goal
    body is itself useful prompt context. No roll-up line is added."""
    async with GoalStore(db_path) as store:
        await store.insert_goal(_g(goal_id="L1", tier="long", body="own infra"))
        resolver = GoalsResolver(store)
        out = await resolver.resolve("quarterly", "mentor")
    assert out == "- own infra"


# ---------------------------------------------------------------------------
# failing — active goals whose latest score < 5, capped at 5
# ---------------------------------------------------------------------------


async def test_failing_filters_by_recent_score_under_5(db_path: Path) -> None:
    async with GoalStore(db_path) as store:
        # Failing across tiers — the design says "regardless of tier".
        s_fail = _g(goal_id="s-fail", tier="short", body="short failing")
        m_fail = _g(goal_id="m-fail", tier="mid", body="mid failing")
        m_ok = _g(goal_id="m-ok", tier="mid", body="mid ok")
        archived = _g(
            goal_id="archived-fail",
            tier="mid",
            body="archived",
            status="archived",
        )
        for g in (s_fail, m_fail, m_ok, archived):
            await store.insert_goal(g)

        await store.insert_evaluation(
            _ev(s_fail, evaluated_at_ms=_NOW - 100, score=2, narrative="rough day")
        )
        await store.insert_evaluation(
            _ev(m_fail, evaluated_at_ms=_NOW - 200, score=4, narrative="off")
        )
        await store.insert_evaluation(
            _ev(m_ok, evaluated_at_ms=_NOW - 300, score=8, narrative="solid")
        )
        # Archived goal: even with a failing score, must not surface
        # (status filter excludes it from the active set).
        await store.insert_evaluation(
            _ev(
                archived, evaluated_at_ms=_NOW - 400, score=1, narrative="broken"
            )
        )

        resolver = GoalsResolver(store)
        out = await resolver.resolve("failing", "mentor")

    lines = out.splitlines()
    # Order = created_at ASC, then id ASC. Both fail rows share
    # ``created_at = _NOW - _DAY_MS`` so id ASC ("m-fail" < "s-fail")
    # decides — pin that explicitly so a future sort-key change surfaces
    # in CI.
    assert lines == [
        "- mid failing: score 4 — off",
        "- short failing: score 2 — rough day",
    ]


async def test_failing_excludes_no_evidence_sentinel(db_path: Path) -> None:
    """``no_evidence`` ≠ "actively failing" — the design pins this.
    Goals with score 0 + sentinel narrative must not appear."""
    async with GoalStore(db_path) as store:
        g = _g(goal_id="g1", tier="mid", body="quiet goal")
        await store.insert_goal(g)
        await store.insert_evaluation(
            _ev(
                g,
                evaluated_at_ms=_NOW - 100,
                score=0,
                narrative=NO_EVIDENCE_SENTINEL,
            )
        )
        resolver = GoalsResolver(store)
        out = await resolver.resolve("failing", "mentor")
    assert out == ""


async def test_failing_uses_only_most_recent_evaluation(
    db_path: Path,
) -> None:
    """A goal whose old score was 2 but whose latest is 9 is *not*
    failing. The placeholder reads ``LIMIT 1`` so this case is the one
    the test pins explicitly."""
    async with GoalStore(db_path) as store:
        g = _g(goal_id="g1", tier="mid", body="recovered")
        await store.insert_goal(g)
        await store.insert_evaluation(
            _ev(g, evaluated_at_ms=_NOW - 1000, score=2, narrative="bad")
        )
        await store.insert_evaluation(
            _ev(g, evaluated_at_ms=_NOW - 100, score=9, narrative="great")
        )
        resolver = GoalsResolver(store)
        out = await resolver.resolve("failing", "mentor")
    assert out == ""


async def test_failing_caps_at_five_with_truncation_suffix(
    db_path: Path,
) -> None:
    async with GoalStore(db_path) as store:
        for i in range(FAILING_LINE_CAP + 2):
            g = _g(
                goal_id=f"g{i}",
                tier="mid",
                body=f"goal-{i}",
                created_at_ms=_NOW - 1000 + i,
            )
            await store.insert_goal(g)
            await store.insert_evaluation(
                _ev(g, evaluated_at_ms=_NOW - 100 - i, score=1, narrative="bad")
            )
        resolver = GoalsResolver(store)
        out = await resolver.resolve("failing", "mentor")
    lines = out.splitlines()
    assert len(lines) == FAILING_LINE_CAP
    assert lines[-1] == "- … (+3 more)"


# ---------------------------------------------------------------------------
# unknown sub-key + missing agent → "" (typo-tolerant)
# ---------------------------------------------------------------------------


async def test_unknown_subkey_returns_empty(db_path: Path) -> None:
    """A typo in the prompt template must not raise — same posture as
    ``corlinman_persona.placeholders``. The resolver returns ``""`` so
    the rest of the prompt renders normally."""
    async with GoalStore(db_path) as store:
        await store.insert_goal(_g(goal_id="g1"))
        resolver = GoalsResolver(store)
        assert await resolver.resolve("bogus", "mentor") == ""
        assert await resolver.resolve("", "mentor") == ""


async def test_missing_agent_returns_empty(db_path: Path) -> None:
    async with GoalStore(db_path) as store:
        resolver = GoalsResolver(store)
        assert await resolver.resolve("today", "") == ""


async def test_resolver_respects_tenant_scope(db_path: Path) -> None:
    """Per-tenant resolvers don't leak: a tenant-A resolver renders only
    tenant-A goals even when tenant-B has matching rows."""
    async with GoalStore(db_path) as store:
        await store.insert_goal(_g(goal_id="a1", body="A"), tenant_id="tenant-a")
        await store.insert_goal(_g(goal_id="b1", body="B"), tenant_id="tenant-b")
        resolver_a = GoalsResolver(store, tenant_id="tenant-a")
        resolver_b = GoalsResolver(store, tenant_id="tenant-b")
        assert await resolver_a.resolve("today", "mentor") == "- A"
        assert await resolver_b.resolve("today", "mentor") == "- B"
