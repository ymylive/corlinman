"""Tests for ``AgentCardHandler`` — Phase 4 W1 4-1D follow-up high-risk kind."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from corlinman_evolution_engine.agent_card import (
    KIND_AGENT_CARD,
    AgentCardHandler,
)
from corlinman_evolution_engine.engine import EngineConfig, EvolutionEngine

from .conftest import insert_signal


def _all_proposals(db_path: Path) -> list[dict[str, object]]:
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT id, kind, target, diff, reasoning, risk, budget_cost,
                      status, signal_ids, trace_ids, created_at, tenant_id
               FROM evolution_proposals ORDER BY id ASC"""
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _seed_drift_cluster(
    db_path: Path,
    *,
    target: str,
    count: int,
    tenant_id: str = "default",
    event_kind: str = "agent.identity_drift",
) -> list[int]:
    """Insert ``count`` ``agent.identity_drift`` signals on ``target``."""
    now_ms = int(time.time() * 1_000)
    ids: list[int] = []
    for i in range(count):
        sid = insert_signal(
            db_path,
            event_kind=event_kind,
            target=target,
            severity="warn",
            payload_json='{"reason": "voice_drift"}',
            trace_id=f"trace-{target}-{i}",
            session_id="sess-agent",
            observed_at=now_ms - 60_000 + i,
            tenant_id=tenant_id,
        )
        ids.append(sid)
    return ids


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_propose_emits_one_proposal_per_agent(
    evolution_db: Path, kb_db: Path
) -> None:
    """Four identity-drift signals on one agent → one proposal."""
    _seed_drift_cluster(evolution_db, target="casual", count=4)

    config = EngineConfig(
        db_path=evolution_db,
        kb_path=kb_db,
        lookback_days=1,
        min_cluster_size=3,
        enabled_kinds=(KIND_AGENT_CARD,),
    )
    summary = await EvolutionEngine(config).run_once()

    assert summary.proposals_written == 1
    assert summary.proposals_by_kind == {KIND_AGENT_CARD: 1}

    proposals = _all_proposals(evolution_db)
    assert len(proposals) == 1
    p = proposals[0]
    assert p["kind"] == KIND_AGENT_CARD
    assert p["target"] == "casual"
    # high-risk because applying changes agent identity
    assert p["risk"] == "high"
    assert p["status"] == "pending"
    assert p["budget_cost"] == 3
    sig_ids = json.loads(str(p["signal_ids"]))
    assert sig_ids == [1, 2, 3, 4]


async def test_propose_diff_is_valid_json_with_before_after(
    evolution_db: Path, kb_db: Path
) -> None:
    """Diff is JSON ``{before, after, rationale}`` per the kind contract."""
    _seed_drift_cluster(evolution_db, target="researcher", count=4)

    config = EngineConfig(
        db_path=evolution_db,
        kb_path=kb_db,
        lookback_days=1,
        min_cluster_size=3,
        enabled_kinds=(KIND_AGENT_CARD,),
    )
    await EvolutionEngine(config).run_once()

    diff = json.loads(str(_all_proposals(evolution_db)[0]["diff"]))
    assert set(diff.keys()) == {"before", "after", "rationale"}
    # Applier fills ``before`` from the live agent-card at apply time.
    assert diff["before"] == ""
    # ``after`` mentions the agent so the operator's editor is
    # pre-loaded with context.
    assert "researcher" in diff["after"]
    assert "researcher" in diff["rationale"]


async def test_propose_handles_persona_misalignment_event(
    evolution_db: Path, kb_db: Path
) -> None:
    """``agent.persona_misalignment`` is also a trigger."""
    _seed_drift_cluster(
        evolution_db,
        target="default",
        count=4,
        event_kind="agent.persona_misalignment",
    )

    config = EngineConfig(
        db_path=evolution_db,
        kb_path=kb_db,
        lookback_days=1,
        min_cluster_size=3,
        enabled_kinds=(KIND_AGENT_CARD,),
    )
    summary = await EvolutionEngine(config).run_once()

    assert summary.proposals_written == 1
    proposals = _all_proposals(evolution_db)
    assert proposals[0]["target"] == "default"


# ---------------------------------------------------------------------------
# Threshold + filter
# ---------------------------------------------------------------------------


async def test_propose_below_handler_threshold_yields_nothing(
    evolution_db: Path, kb_db: Path
) -> None:
    """Default agent_card threshold is 4; 3 signals shouldn't fire."""
    _seed_drift_cluster(evolution_db, target="casual", count=3)

    config = EngineConfig(
        db_path=evolution_db,
        kb_path=kb_db,
        lookback_days=1,
        min_cluster_size=3,
        enabled_kinds=(KIND_AGENT_CARD,),
    )
    summary = await EvolutionEngine(config).run_once()

    assert summary.clusters_found == 1
    assert summary.proposals_written == 0
    assert _all_proposals(evolution_db) == []


async def test_propose_ignores_unrelated_event_kinds(
    evolution_db: Path, kb_db: Path
) -> None:
    """Cluster on a different event_kind shouldn't trigger this handler."""
    now_ms = int(time.time() * 1_000)
    for i in range(4):
        insert_signal(
            evolution_db,
            event_kind="chat.intent_mismatch",  # belongs to prompt_template
            target="casual",
            severity="warn",
            payload_json="{}",
            trace_id=f"t-{i}",
            session_id="s",
            observed_at=now_ms - 60_000 + i,
        )

    config = EngineConfig(
        db_path=evolution_db,
        kb_path=kb_db,
        lookback_days=1,
        min_cluster_size=3,
        enabled_kinds=(KIND_AGENT_CARD,),
    )
    summary = await EvolutionEngine(config).run_once()

    assert summary.clusters_found == 1
    assert summary.proposals_written == 0


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------


async def test_propose_dedups_against_existing_target(
    evolution_db: Path, kb_db: Path
) -> None:
    """Re-running on the same cluster doesn't double-file a proposal."""
    _seed_drift_cluster(evolution_db, target="casual", count=4)

    config = EngineConfig(
        db_path=evolution_db,
        kb_path=kb_db,
        lookback_days=1,
        min_cluster_size=3,
        enabled_kinds=(KIND_AGENT_CARD,),
    )
    first = await EvolutionEngine(config).run_once()
    second = await EvolutionEngine(config).run_once()

    assert first.proposals_written == 1
    assert second.proposals_written == 0
    assert len(_all_proposals(evolution_db)) == 1


# ---------------------------------------------------------------------------
# Tenant routing
# ---------------------------------------------------------------------------


async def test_propose_threads_tenant_id_through(
    evolution_db: Path, kb_db: Path
) -> None:
    """Signals on a non-default tenant produce proposals on that tenant."""
    _seed_drift_cluster(
        evolution_db, target="researcher", count=4, tenant_id="acme"
    )

    config = EngineConfig(
        db_path=evolution_db,
        kb_path=kb_db,
        lookback_days=1,
        min_cluster_size=3,
        enabled_kinds=(KIND_AGENT_CARD,),
    )
    await EvolutionEngine(config).run_once()

    proposals = _all_proposals(evolution_db)
    assert len(proposals) == 1
    assert proposals[0]["tenant_id"] == "acme"


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


def test_handler_constructor_rejects_zero_threshold() -> None:
    import pytest

    with pytest.raises(ValueError, match="threshold must be >= 1"):
        AgentCardHandler(threshold=0)
