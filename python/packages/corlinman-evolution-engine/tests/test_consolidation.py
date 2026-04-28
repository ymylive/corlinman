"""Phase 3 W3-A — consolidation pipeline tests.

These exercise :func:`consolidation_run_once` end-to-end against a
freshly-seeded ``kb.sqlite`` + ``evolution.sqlite`` pair. The Rust side
of the pipeline (the EvolutionApplier consuming the proposals we emit)
is covered separately in ``rust/crates/corlinman-gateway`` — this file
asserts the proposal shape + dedup + budget guard rails.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from corlinman_evolution_engine.consolidation import (
    CONSOLIDATED_NAMESPACE,
    ConsolidationConfig,
    consolidation_run_once,
)
from corlinman_evolution_engine.memory_op import KIND_MEMORY_OP

from .conftest import insert_chunk


def _seed_chunk_with_decay(
    kb_path: Path,
    *,
    content: str,
    decay_score: float,
    namespace: str = "general",
) -> int:
    """Insert one chunk and stamp its ``decay_score`` to a fixed value.

    Uses the public ``insert_chunk`` from conftest (which writes a
    placeholder ``files`` row and namespace-default chunk row) then
    flips ``decay_score`` directly. Returns the chunk id.
    """
    chunk_id = insert_chunk(kb_path, content=content, namespace=namespace)
    conn = sqlite3.connect(kb_path)
    try:
        conn.execute(
            "UPDATE chunks SET decay_score = ? WHERE id = ?",
            (decay_score, chunk_id),
        )
        conn.commit()
    finally:
        conn.close()
    return chunk_id


def _list_proposals(evolution_path: Path) -> list[tuple[str, str, str, str]]:
    """Return ``[(id, kind, target, status), ...]`` for every proposal."""
    conn = sqlite3.connect(evolution_path)
    try:
        rows = conn.execute(
            "SELECT id, kind, target, status FROM evolution_proposals "
            "ORDER BY id ASC"
        ).fetchall()
    finally:
        conn.close()
    return [(str(r[0]), str(r[1]), str(r[2]), str(r[3])) for r in rows]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_consolidation_emits_one_proposal_per_high_score_chunk(
    kb_db: Path,
    evolution_db: Path,
) -> None:
    high1 = _seed_chunk_with_decay(kb_db, content="alpha facts", decay_score=0.92)
    high2 = _seed_chunk_with_decay(kb_db, content="beta facts", decay_score=0.85)
    # Below threshold — must NOT yield a proposal.
    _seed_chunk_with_decay(kb_db, content="cold gamma", decay_score=0.40)

    cfg = ConsolidationConfig(promotion_threshold=0.65, max_promotions_per_run=10)
    summary = asyncio.run(
        consolidation_run_once(
            config=cfg,
            kb_db_path=kb_db,
            evolution_db_path=evolution_db,
        )
    )
    assert summary.candidates_found == 2
    assert summary.proposals_written == 2
    assert summary.skipped_existing == 0
    assert not summary.skipped_disabled

    proposals = _list_proposals(evolution_db)
    targets = {p[2] for p in proposals}
    assert targets == {f"consolidate_chunk:{high1}", f"consolidate_chunk:{high2}"}
    # All proposals are memory_op + pending.
    for _id, kind, _target, status in proposals:
        assert kind == KIND_MEMORY_OP
        assert status == "pending"


def test_consolidation_skips_already_consolidated_chunks(
    kb_db: Path,
    evolution_db: Path,
) -> None:
    # Seeded above threshold but lives in the immune namespace ⇒ skip.
    _seed_chunk_with_decay(
        kb_db,
        content="already promoted",
        decay_score=0.95,
        namespace=CONSOLIDATED_NAMESPACE,
    )
    cfg = ConsolidationConfig(promotion_threshold=0.65)
    summary = asyncio.run(
        consolidation_run_once(
            config=cfg,
            kb_db_path=kb_db,
            evolution_db_path=evolution_db,
        )
    )
    assert summary.candidates_found == 0
    assert summary.proposals_written == 0
    assert _list_proposals(evolution_db) == []


def test_consolidation_dedup_against_existing_proposals(
    kb_db: Path,
    evolution_db: Path,
) -> None:
    cid = _seed_chunk_with_decay(kb_db, content="recurring", decay_score=0.9)
    cfg = ConsolidationConfig(promotion_threshold=0.65)

    # First run files one proposal.
    first = asyncio.run(
        consolidation_run_once(
            config=cfg,
            kb_db_path=kb_db,
            evolution_db_path=evolution_db,
        )
    )
    assert first.proposals_written == 1
    assert len(_list_proposals(evolution_db)) == 1

    # Second run on the same chunk must NOT double-file — the existing
    # `consolidate_chunk:<id>` row is the dedup key.
    second = asyncio.run(
        consolidation_run_once(
            config=cfg,
            kb_db_path=kb_db,
            evolution_db_path=evolution_db,
        )
    )
    assert second.candidates_found == 1
    assert second.proposals_written == 0
    assert second.skipped_existing == 1
    assert len(_list_proposals(evolution_db)) == 1
    # And the target stayed the same.
    assert _list_proposals(evolution_db)[0][2] == f"consolidate_chunk:{cid}"


def test_consolidation_respects_max_promotions_per_run(
    kb_db: Path,
    evolution_db: Path,
) -> None:
    # Five eligible chunks, cap to 3 — only the strongest 3 survive.
    seeded: list[tuple[int, float]] = []
    for i in range(5):
        score = 0.70 + i * 0.03  # ordered ascending
        cid = _seed_chunk_with_decay(
            kb_db, content=f"chunk-{i}", decay_score=score
        )
        seeded.append((cid, score))

    cfg = ConsolidationConfig(promotion_threshold=0.65, max_promotions_per_run=3)
    summary = asyncio.run(
        consolidation_run_once(
            config=cfg,
            kb_db_path=kb_db,
            evolution_db_path=evolution_db,
        )
    )
    assert summary.candidates_found == 3
    assert summary.proposals_written == 3

    proposals = _list_proposals(evolution_db)
    written_targets = {p[2] for p in proposals}
    # Top three by score are the last three seeded.
    expected = {f"consolidate_chunk:{cid}" for cid, _ in seeded[-3:]}
    assert written_targets == expected


def test_consolidation_disabled_short_circuits(
    kb_db: Path,
    evolution_db: Path,
) -> None:
    _seed_chunk_with_decay(kb_db, content="would qualify", decay_score=0.95)
    cfg = ConsolidationConfig(enabled=False)
    summary = asyncio.run(
        consolidation_run_once(
            config=cfg,
            kb_db_path=kb_db,
            evolution_db_path=evolution_db,
        )
    )
    assert summary.skipped_disabled
    assert summary.proposals_written == 0
    assert _list_proposals(evolution_db) == []


def test_consolidation_proposal_shape_matches_memory_op_contract(
    kb_db: Path,
    evolution_db: Path,
) -> None:
    """Proposal carries the kind + target shape the Rust EvolutionApplier
    parses on the receiving end. If this drifts the Rust-side
    ``MemoryOp::parse`` test will fail too — pinning both sides.
    """
    cid = _seed_chunk_with_decay(kb_db, content="contract", decay_score=0.88)
    asyncio.run(
        consolidation_run_once(
            config=ConsolidationConfig(),
            kb_db_path=kb_db,
            evolution_db_path=evolution_db,
        )
    )
    conn = sqlite3.connect(evolution_db)
    try:
        row = conn.execute(
            "SELECT kind, target, risk, status, diff, reasoning, budget_cost "
            "FROM evolution_proposals LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    kind, target, risk, status, diff, reasoning, budget_cost = row
    assert kind == KIND_MEMORY_OP
    assert target == f"consolidate_chunk:{cid}"
    assert risk == "low"
    assert status == "pending"
    assert diff == ""
    assert "decay_score=" in reasoning
    assert budget_cost == 0


def test_consolidation_zero_max_returns_empty(
    kb_db: Path,
    evolution_db: Path,
) -> None:
    _seed_chunk_with_decay(kb_db, content="x", decay_score=0.95)
    cfg = ConsolidationConfig(max_promotions_per_run=0)
    summary = asyncio.run(
        consolidation_run_once(
            config=cfg,
            kb_db_path=kb_db,
            evolution_db_path=evolution_db,
        )
    )
    assert summary.candidates_found == 0
    assert summary.proposals_written == 0


@pytest.mark.parametrize("threshold", [0.0, 0.5, 0.99])
def test_consolidation_threshold_filters_kb_rows(
    kb_db: Path,
    evolution_db: Path,
    threshold: float,
) -> None:
    _seed_chunk_with_decay(kb_db, content="low", decay_score=0.20)
    mid = _seed_chunk_with_decay(kb_db, content="mid", decay_score=0.55)
    high = _seed_chunk_with_decay(kb_db, content="high", decay_score=0.99)

    cfg = ConsolidationConfig(promotion_threshold=threshold)
    summary = asyncio.run(
        consolidation_run_once(
            config=cfg,
            kb_db_path=kb_db,
            evolution_db_path=evolution_db,
        )
    )
    if threshold <= 0.20:
        assert summary.candidates_found == 3
    elif threshold <= 0.55:
        assert summary.candidates_found == 2
        ids = {p[2] for p in _list_proposals(evolution_db)}
        assert ids == {f"consolidate_chunk:{mid}", f"consolidate_chunk:{high}"}
    else:  # 0.99
        assert summary.candidates_found == 1
        ids = {p[2] for p in _list_proposals(evolution_db)}
        assert ids == {f"consolidate_chunk:{high}"}
