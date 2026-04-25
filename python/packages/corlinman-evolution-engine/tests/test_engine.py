"""End-to-end black-box tests for ``EvolutionEngine.run_once``."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest
from corlinman_evolution_engine.engine import EngineConfig, EvolutionEngine
from corlinman_evolution_engine.proposals import (
    EvolutionProposal,
    KindHandler,
    ProposalContext,
)

from .conftest import insert_chunk, insert_signal


def _all_proposals(db_path: Path) -> list[dict[str, object]]:
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT id, kind, target, diff, reasoning, risk, budget_cost,
                      status, signal_ids, trace_ids, created_at
               FROM evolution_proposals ORDER BY id ASC"""
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _seed_failure_cluster(db_path: Path, *, count: int = 5) -> None:
    """Insert ``count`` ``tool.call.failed`` signals on the same target."""
    now_ms = int(time.time() * 1_000)
    for i in range(count):
        insert_signal(
            db_path,
            event_kind="tool.call.failed",
            target="web_search",
            severity="error",
            payload_json='{"reason": "timeout"}',
            trace_id=f"trace-{i}",
            session_id="sess-1",
            observed_at=now_ms - 60_000 + i,
        )


async def test_run_once_writes_memory_op_proposal_for_near_duplicate_chunks(
    evolution_db: Path, kb_db: Path
) -> None:
    _seed_failure_cluster(evolution_db, count=5)
    insert_chunk(kb_db, content="alpha beta gamma delta epsilon zeta eta theta")
    insert_chunk(kb_db, content="alpha beta gamma delta epsilon zeta eta theta!")
    insert_chunk(kb_db, content="totally different text on machine learning systems")

    config = EngineConfig(
        db_path=evolution_db,
        kb_path=kb_db,
        lookback_days=1,
        min_cluster_size=3,
    )
    summary = await EvolutionEngine(config).run_once()

    assert summary.signals_loaded == 5
    assert summary.clusters_found == 1
    assert summary.duplicate_pairs_found == 1
    assert summary.proposals_written == 1
    assert summary.skipped_existing == 0

    proposals = _all_proposals(evolution_db)
    assert len(proposals) == 1
    p = proposals[0]
    assert p["kind"] == "memory_op"
    assert p["target"] == "merge_chunks:1,2"
    assert p["risk"] == "low"
    assert p["status"] == "pending"
    assert p["budget_cost"] == 0
    assert p["diff"] == ""
    # id format: evol-YYYY-MM-DD-NNN
    assert isinstance(p["id"], str)
    assert p["id"].startswith("evol-")
    assert p["id"].endswith("-001")
    # signal_ids/trace_ids are JSON-encoded lists
    sig_ids = json.loads(str(p["signal_ids"]))
    assert sig_ids == [1, 2, 3, 4, 5]
    trace_ids = json.loads(str(p["trace_ids"]))
    assert trace_ids == [f"trace-{i}" for i in range(5)]


async def test_run_once_no_signals_writes_no_proposal(
    evolution_db: Path, kb_db: Path
) -> None:
    # Seed kb with duplicates but no signals → no trigger, no proposal.
    insert_chunk(kb_db, content="alpha beta gamma delta epsilon")
    insert_chunk(kb_db, content="alpha beta gamma delta epsilon!")

    config = EngineConfig(db_path=evolution_db, kb_path=kb_db)
    summary = await EvolutionEngine(config).run_once()

    assert summary.clusters_found == 0
    assert summary.proposals_written == 0
    assert _all_proposals(evolution_db) == []


async def test_run_once_below_min_cluster_size_writes_no_proposal(
    evolution_db: Path, kb_db: Path
) -> None:
    _seed_failure_cluster(evolution_db, count=2)  # below default threshold
    insert_chunk(kb_db, content="alpha beta gamma delta epsilon")
    insert_chunk(kb_db, content="alpha beta gamma delta epsilon!")

    config = EngineConfig(
        db_path=evolution_db, kb_path=kb_db, min_cluster_size=3
    )
    summary = await EvolutionEngine(config).run_once()

    assert summary.signals_loaded == 2
    assert summary.clusters_found == 0
    assert summary.proposals_written == 0


async def test_run_once_dedups_against_existing_proposals(
    evolution_db: Path, kb_db: Path
) -> None:
    _seed_failure_cluster(evolution_db, count=5)
    insert_chunk(kb_db, content="alpha beta gamma delta epsilon zeta eta theta")
    insert_chunk(kb_db, content="alpha beta gamma delta epsilon zeta eta theta!")

    config = EngineConfig(
        db_path=evolution_db, kb_path=kb_db, min_cluster_size=3
    )

    # First run files the proposal.
    s1 = await EvolutionEngine(config).run_once()
    assert s1.proposals_written == 1

    # Second run should detect the duplicate again but skip — already filed.
    s2 = await EvolutionEngine(config).run_once()
    assert s2.proposals_written == 0
    assert s2.skipped_existing == 1
    assert len(_all_proposals(evolution_db)) == 1


async def test_run_once_respects_max_proposals_per_run(
    evolution_db: Path, kb_db: Path
) -> None:
    _seed_failure_cluster(evolution_db, count=5)
    # Three independent near-duplicate pairs.
    insert_chunk(kb_db, content="aaa bbb ccc ddd eee fff ggg hhh")
    insert_chunk(kb_db, content="aaa bbb ccc ddd eee fff ggg hhh!")
    insert_chunk(kb_db, content="iii jjj kkk lll mmm nnn ooo ppp")
    insert_chunk(kb_db, content="iii jjj kkk lll mmm nnn ooo ppp!")
    insert_chunk(kb_db, content="qqq rrr sss ttt uuu vvv www xxx")
    insert_chunk(kb_db, content="qqq rrr sss ttt uuu vvv www xxx!")

    config = EngineConfig(
        db_path=evolution_db,
        kb_path=kb_db,
        min_cluster_size=3,
        max_proposals_per_run=2,
    )
    summary = await EvolutionEngine(config).run_once()

    assert summary.duplicate_pairs_found == 3
    assert summary.proposals_written == 2
    assert summary.truncated_by_cap is True
    assert len(_all_proposals(evolution_db)) == 2


async def test_run_once_proposal_ids_have_sequential_three_digit_suffix(
    evolution_db: Path, kb_db: Path
) -> None:
    _seed_failure_cluster(evolution_db, count=3)
    insert_chunk(kb_db, content="aaa bbb ccc ddd eee fff ggg hhh")
    insert_chunk(kb_db, content="aaa bbb ccc ddd eee fff ggg hhh!")
    insert_chunk(kb_db, content="iii jjj kkk lll mmm nnn ooo ppp")
    insert_chunk(kb_db, content="iii jjj kkk lll mmm nnn ooo ppp!")

    config = EngineConfig(
        db_path=evolution_db, kb_path=kb_db, min_cluster_size=3
    )
    summary = await EvolutionEngine(config).run_once()

    assert summary.proposals_written == 2
    ids = [str(p["id"]) for p in _all_proposals(evolution_db)]
    suffixes = sorted(i.rsplit("-", 1)[-1] for i in ids)
    assert suffixes == ["001", "002"]


# ---------------------------------------------------------------------------
# Strategy hook — verifies a Phase 3 handler can be plugged in without
# touching the engine itself. The fake handler emits a fixed proposal of an
# unknown ``kind``; we just want to see that the dispatch path is generic.
# ---------------------------------------------------------------------------


class _FakeSkillHandler:
    """Stand-in for a future ``SkillExtractionHandler``.

    Emits a single ``skill_update`` proposal regardless of context — just
    enough to prove the engine routes through the ``KindHandler`` protocol
    rather than a hard-coded memory_op path.
    """

    @property
    def kind(self) -> str:
        return "skill_update"

    async def existing_targets(self, conn: object) -> set[str]:
        return set()

    async def propose(self, ctx: ProposalContext) -> list[EvolutionProposal]:
        return [
            EvolutionProposal(
                kind=self.kind,
                target="skills/web_search.md",
                diff="--- before\n+++ after\n",
                reasoning="fake handler for strategy-hook test",
                risk="medium",
                budget_cost=3,
                signal_ids=[c.signal_ids[0] for c in ctx.clusters],
                trace_ids=[],
            )
        ]


async def test_run_once_dispatches_through_kind_handlers(
    evolution_db: Path, kb_db: Path
) -> None:
    _seed_failure_cluster(evolution_db, count=3)

    handler: KindHandler = _FakeSkillHandler()
    config = EngineConfig(
        db_path=evolution_db,
        kb_path=kb_db,
        min_cluster_size=3,
        enabled_kinds=("skill_update",),
    )
    summary = await EvolutionEngine(config, handlers=[handler]).run_once()

    assert summary.proposals_written == 1
    assert summary.proposals_by_kind == {"skill_update": 1}
    proposals = _all_proposals(evolution_db)
    assert len(proposals) == 1
    assert proposals[0]["kind"] == "skill_update"
    assert proposals[0]["risk"] == "medium"
    assert proposals[0]["budget_cost"] == 3


async def test_run_once_unknown_enabled_kind_raises(
    evolution_db: Path, kb_db: Path
) -> None:
    config = EngineConfig(
        db_path=evolution_db,
        kb_path=kb_db,
        enabled_kinds=("nonexistent_kind",),
    )
    with pytest.raises(ValueError, match="no KindHandler registered"):
        EvolutionEngine(config)
