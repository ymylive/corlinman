"""Tests for :mod:`corlinman_goals.cli` (iter 7).

Pin the design's CLI test matrix:

- ``cli_set_rejects_cross_tier_parent`` — a short cannot parent a mid.
- ``archive_cascade_walks_one_level`` — direct children archived,
  grandchildren left alone.
- ``seed_idempotency`` — running ``seed`` twice over the same YAML
  inserts only on the first run.
- ``reflect_once_via_cli_writes_evaluation`` — end-to-end CLI shape
  with a stub grader + empty evidence sentinel.
- ``register_grader_factory_swaps_in_provider`` — gateway-boot wiring
  contract: a registered factory produces the grader the CLI uses.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from corlinman_goals.cli import (
    GoalsConfig,
    main,
    register_evidence_factory,
    register_grader_factory,
    run_set_goal,
)
from corlinman_goals.evidence import EvidenceEpisode, StaticEvidence
from corlinman_goals.placeholders import NO_EVIDENCE_SENTINEL
from corlinman_goals.reflection import (
    Grader,
    make_constant_grader,
)
from corlinman_goals.store import GoalStore

_NOW = int(datetime(2026, 5, 9, 14, 0, tzinfo=UTC).timestamp() * 1000)
_DAY_MS = 24 * 3600 * 1000


@pytest.fixture(autouse=True)
def _reset_factories() -> None:
    # Tests must not leak factories between runs — the registry is
    # module-global. Set both to None pre-test; tests that need a real
    # factory register one and rely on this teardown to clear it.
    register_grader_factory(None)
    register_evidence_factory(None)
    yield
    register_grader_factory(None)
    register_evidence_factory(None)


def _db_path(tmp_path: Path) -> Path:
    return tmp_path / "agent_goals.sqlite"


def test_cli_set_inserts_goal_with_tier_derived_target(tmp_path: Path, capsys) -> None:
    db = _db_path(tmp_path)
    rc = main(
        [
            "set",
            "--db",
            str(db),
            "--agent-id",
            "mentor",
            "--tier",
            "mid",
            "--body",
            "Become competent at infrastructure topics",
            "--now-ms",
            str(_NOW),
            "--json",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["agent_id"] == "mentor"
    assert payload["tier"] == "mid"
    assert payload["status"] == "active"
    # Tier-derived target_date is the *following* Monday midnight UTC
    # — which for a Saturday "now" is the Monday that comes after the
    # next ISO Monday. Just assert it's > now.
    assert payload["target_date_ms"] > _NOW


def test_cli_set_rejects_cross_tier_parent(tmp_path: Path) -> None:
    """A short cannot parent a mid (parent must be strictly higher tier)."""
    db = _db_path(tmp_path)

    # Seed: make a short-tier goal first.
    rc = main(
        [
            "set",
            "--db",
            str(db),
            "--agent-id",
            "mentor",
            "--tier",
            "short",
            "--body",
            "Today I will read the postgres docs",
            "--id",
            "goal-short-1",
            "--now-ms",
            str(_NOW),
        ]
    )
    assert rc == 0

    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "set",
                "--db",
                str(db),
                "--agent-id",
                "mentor",
                "--tier",
                "mid",
                "--body",
                "This week I will deepen db knowledge",
                "--parent-goal-id",
                "goal-short-1",
                "--now-ms",
                str(_NOW),
            ]
        )
    msg = str(excinfo.value)
    assert "cross_tier_parent" in msg


def test_cli_set_accepts_long_parent_for_mid(tmp_path: Path) -> None:
    """A mid CAN parent a long (long has strictly higher rank)."""
    db = _db_path(tmp_path)
    main(
        [
            "set",
            "--db",
            str(db),
            "--agent-id",
            "mentor",
            "--tier",
            "long",
            "--body",
            "Quarter goal",
            "--id",
            "goal-long-1",
            "--now-ms",
            str(_NOW),
        ]
    )
    rc = main(
        [
            "set",
            "--db",
            str(db),
            "--agent-id",
            "mentor",
            "--tier",
            "mid",
            "--body",
            "Mid goal under long",
            "--parent-goal-id",
            "goal-long-1",
            "--now-ms",
            str(_NOW),
        ]
    )
    assert rc == 0


def test_cli_archive_cascade_walks_one_level(tmp_path: Path) -> None:
    db = _db_path(tmp_path)
    # parent (mid)
    main(
        [
            "set",
            "--db",
            str(db),
            "--agent-id",
            "mentor",
            "--tier",
            "mid",
            "--body",
            "Mid parent",
            "--id",
            "g-mid",
            "--now-ms",
            str(_NOW),
        ]
    )
    # child (short under mid)
    main(
        [
            "set",
            "--db",
            str(db),
            "--agent-id",
            "mentor",
            "--tier",
            "short",
            "--body",
            "Direct child",
            "--parent-goal-id",
            "g-mid",
            "--id",
            "g-short",
            "--now-ms",
            str(_NOW),
        ]
    )
    rc = main(
        ["archive", "--db", str(db), "--goal-id", "g-mid", "--cascade", "--json"]
    )
    assert rc == 0

    async def _check() -> tuple[str, str]:
        async with GoalStore(db) as store:
            mid = await store.get_goal("g-mid")
            short = await store.get_goal("g-short")
            assert mid is not None and short is not None
            return mid.status, short.status

    mid_status, short_status = asyncio.run(_check())
    assert mid_status == "archived"
    assert short_status == "archived"


def test_cli_seed_yaml_idempotent(tmp_path: Path) -> None:
    db = _db_path(tmp_path)
    yaml_path = tmp_path / "seed.yaml"
    yaml_path.write_text(
        """\
goals:
  - id: goal-seed-1
    agent_id: mentor
    tier: mid
    body: Become competent at infra topics
  - id: goal-seed-2
    agent_id: mentor
    tier: short
    body: Read one paper today
""",
        encoding="utf-8",
    )

    rc1 = main(
        ["seed", "--db", str(db), "--yaml", str(yaml_path), "--now-ms", str(_NOW)]
    )
    assert rc1 == 0

    rc2 = main(
        ["seed", "--db", str(db), "--yaml", str(yaml_path), "--now-ms", str(_NOW)]
    )
    assert rc2 == 0

    async def _count() -> int:
        async with GoalStore(db) as store:
            rows = await store.list_goals(agent_id="mentor")
            assert all(g.source == "seed" for g in rows)
            return len(rows)

    assert asyncio.run(_count()) == 2


def test_cli_edit_can_clear_parent(tmp_path: Path) -> None:
    db = _db_path(tmp_path)
    asyncio.run(
        run_set_goal(
            db_path=db,
            agent_id="mentor",
            tier="mid",
            body="parent",
            goal_id="g-parent",
            now_ms=_NOW,
        )
    )
    asyncio.run(
        run_set_goal(
            db_path=db,
            agent_id="mentor",
            tier="short",
            body="child",
            goal_id="g-child",
            parent_goal_id="g-parent",
            now_ms=_NOW,
        )
    )
    rc = main(
        ["edit", "--db", str(db), "--goal-id", "g-child", "--clear-parent"]
    )
    assert rc == 0

    async def _check() -> str | None:
        async with GoalStore(db) as store:
            g = await store.get_goal("g-child")
            assert g is not None
            return g.parent_goal_id

    assert asyncio.run(_check()) is None


def test_cli_list_with_evaluations_includes_latest(tmp_path: Path, capsys) -> None:
    db = _db_path(tmp_path)
    asyncio.run(
        run_set_goal(
            db_path=db,
            agent_id="mentor",
            tier="mid",
            body="goal a",
            goal_id="g-a",
            now_ms=_NOW,
        )
    )
    rc = main(
        [
            "list",
            "--db",
            str(db),
            "--agent-id",
            "mentor",
            "--include-evaluations",
            "--json",
        ]
    )
    assert rc == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["id"] == "g-a"
    assert payload["latest_evaluation"] is None  # not yet reflected


def test_cli_reflect_once_with_stub_evidence_writes_sentinel(tmp_path: Path) -> None:
    """No evidence + no LLM call → sentinel row."""
    db = _db_path(tmp_path)
    asyncio.run(
        run_set_goal(
            db_path=db,
            agent_id="mentor",
            tier="short",
            body="goal a",
            goal_id="g-a",
            now_ms=_NOW,
        )
    )
    rc = main(
        [
            "reflect-once",
            "--db",
            str(db),
            "--tier",
            "short",
            "--agent-id",
            "mentor",
            "--stub-score",
            "7",
            "--stub-evidence",
            "--now-ms",
            str(_NOW),
            "--json",
        ]
    )
    assert rc == 0

    async def _check() -> str:
        async with GoalStore(db) as store:
            evs = await store.list_evaluations("g-a", limit=1)
            assert len(evs) == 1
            return evs[0].narrative

    # Sentinel — even though we passed --stub-score 7, the empty evidence
    # short-circuited the grader call.
    assert asyncio.run(_check()) == NO_EVIDENCE_SENTINEL


def test_register_grader_factory_swaps_in_provider(tmp_path: Path) -> None:
    """The factory contract: ``main()`` looks up the registered factory
    when no ``--stub-score`` is supplied. This is the iter-7 wiring path
    the gateway boot will use to inject a real ``corlinman-providers``
    backed grader.
    """
    db = _db_path(tmp_path)
    asyncio.run(
        run_set_goal(
            db_path=db,
            agent_id="mentor",
            tier="short",
            body="goal a",
            goal_id="g-a",
            now_ms=_NOW,
        )
    )
    seen_aliases: list[str] = []

    def _factory(config: GoalsConfig, alias: str) -> Grader:
        seen_aliases.append(alias)
        return make_constant_grader(score=8, narrative="from-factory")

    register_grader_factory(_factory)

    rc = main(
        [
            "reflect-once",
            "--db",
            str(db),
            "--tier",
            "short",
            "--agent-id",
            "mentor",
            "--stub-evidence",
            "--now-ms",
            str(_NOW),
        ]
    )
    assert rc == 0
    assert seen_aliases == ["default-cheap"]


def test_reflect_once_without_stub_or_factory_errors(tmp_path: Path) -> None:
    db = _db_path(tmp_path)
    with pytest.raises(SystemExit):
        main(
            [
                "reflect-once",
                "--db",
                str(db),
                "--tier",
                "short",
                "--agent-id",
                "mentor",
                "--stub-evidence",
            ]
        )


def test_register_evidence_factory_routes_to_real_path(tmp_path: Path) -> None:
    """The evidence-factory hook lets callers swap in a custom
    :class:`EpisodeEvidence` implementation. We register a fake here
    and assert it gets called with the CLI's args."""
    db = _db_path(tmp_path)
    fake_episodes_db = tmp_path / "episodes.sqlite"
    fake_episodes_db.write_bytes(b"")  # placeholder; factory ignores

    # Goal authored ``yesterday`` so reflection at ``_NOW`` has a real
    # 24h window (created_at < window.start_ms means no partial-window
    # clamp; the runner's normal short window applies).
    asyncio.run(
        run_set_goal(
            db_path=db,
            agent_id="mentor",
            tier="short",
            body="goal a",
            goal_id="g-a",
            now_ms=_NOW - 2 * _DAY_MS,
        )
    )
    captured: dict[str, object] = {}

    def _evidence_factory(config: GoalsConfig, episodes_db: Path, tenant_id: str):
        captured["episodes_db"] = episodes_db
        captured["tenant_id"] = tenant_id
        return StaticEvidence(
            [
                EvidenceEpisode(
                    episode_id="ep-1",
                    started_at_ms=_NOW - 3600 * 1000,
                    ended_at_ms=_NOW - 1000,
                    kind="conversation",
                    summary_text="user asked about postgres",
                    importance_score=0.8,
                )
            ]
        )

    register_evidence_factory(_evidence_factory)
    rc = main(
        [
            "reflect-once",
            "--db",
            str(db),
            "--tier",
            "short",
            "--agent-id",
            "mentor",
            "--episodes-db",
            str(fake_episodes_db),
            "--stub-score",
            "9",
            "--stub-narrative",
            "great work",
            "--now-ms",
            str(_NOW),
        ]
    )
    assert rc == 0
    assert captured["episodes_db"] == fake_episodes_db
    assert captured["tenant_id"] == "default"

    async def _check() -> tuple[int, str]:
        async with GoalStore(db) as store:
            evs = await store.list_evaluations("g-a", limit=1)
            assert len(evs) == 1
            return evs[0].score_0_to_10, evs[0].narrative

    score, narrative = asyncio.run(_check())
    assert score == 9
    assert "great work" in narrative


def test_cli_reflect_once_dry_run_does_not_write(tmp_path: Path) -> None:
    db = _db_path(tmp_path)
    asyncio.run(
        run_set_goal(
            db_path=db,
            agent_id="mentor",
            tier="short",
            body="goal a",
            goal_id="g-a",
            now_ms=_NOW,
        )
    )
    rc = main(
        [
            "reflect-once",
            "--db",
            str(db),
            "--tier",
            "short",
            "--agent-id",
            "mentor",
            "--stub-score",
            "7",
            "--stub-evidence",
            "--dry-run",
            "--now-ms",
            str(_NOW),
        ]
    )
    assert rc == 0

    async def _check() -> int:
        async with GoalStore(db) as store:
            evs = await store.list_evaluations("g-a")
            return len(evs)

    assert asyncio.run(_check()) == 0
