"""Tests for ``ToolPolicyHandler`` — Phase 4 W1 4-1D high-risk kind."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from corlinman_evolution_engine.engine import EngineConfig, EvolutionEngine
from corlinman_evolution_engine.tool_policy import (
    KIND_TOOL_POLICY,
    ToolPolicyHandler,
)

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


def _seed_tool_signal_cluster(
    db_path: Path,
    *,
    tool: str,
    event_kind: str,
    count: int,
    tenant_id: str = "default",
) -> list[int]:
    """Insert ``count`` signals of ``event_kind`` with ``target=tool``."""
    now_ms = int(time.time() * 1_000)
    ids: list[int] = []
    for i in range(count):
        sid = insert_signal(
            db_path,
            event_kind=event_kind,
            target=tool,
            severity="warn",
            payload_json='{"reason": "operator_denied"}',
            trace_id=f"trace-{tool}-{event_kind}-{i}",
            session_id="sess-tool",
            observed_at=now_ms - 60_000 + i,
            tenant_id=tenant_id,
        )
        ids.append(sid)
    return ids


# ---------------------------------------------------------------------------
# Happy path — tighten direction
# ---------------------------------------------------------------------------


async def test_tighten_emits_proposal_on_approval_denied_cluster(
    evolution_db: Path, kb_db: Path
) -> None:
    """Three approval.denied signals on web_search → tighten proposal."""
    _seed_tool_signal_cluster(
        evolution_db,
        tool="web_search",
        event_kind="approval.denied",
        count=3,
    )

    config = EngineConfig(
        db_path=evolution_db,
        kb_path=kb_db,
        lookback_days=1,
        min_cluster_size=3,
        enabled_kinds=(KIND_TOOL_POLICY,),
    )
    summary = await EvolutionEngine(config).run_once()

    assert summary.proposals_written == 1
    assert summary.proposals_by_kind == {KIND_TOOL_POLICY: 1}

    p = _all_proposals(evolution_db)[0]
    assert p["kind"] == KIND_TOOL_POLICY
    assert p["target"] == "web_search"
    assert p["risk"] == "high"
    assert p["status"] == "pending"
    assert p["budget_cost"] == 3

    diff = json.loads(str(p["diff"]))
    assert diff["before"] == "auto"
    assert diff["after"] == "prompt"
    assert diff["rule_id"] == "tool_policy.web_search.tighten"


async def test_tighten_emits_proposal_on_unsafe_argument_cluster(
    evolution_db: Path, kb_db: Path
) -> None:
    """``tool.unsafe_argument`` is also a tighten trigger."""
    _seed_tool_signal_cluster(
        evolution_db,
        tool="code_executor",
        event_kind="tool.unsafe_argument",
        count=3,
    )

    config = EngineConfig(
        db_path=evolution_db,
        kb_path=kb_db,
        lookback_days=1,
        min_cluster_size=3,
        enabled_kinds=(KIND_TOOL_POLICY,),
    )
    summary = await EvolutionEngine(config).run_once()

    assert summary.proposals_written == 1
    diff = json.loads(str(_all_proposals(evolution_db)[0]["diff"]))
    assert diff["after"] == "prompt"


# ---------------------------------------------------------------------------
# Happy path — loosen direction
# ---------------------------------------------------------------------------


async def test_loosen_emits_proposal_when_timeout_threshold_met(
    evolution_db: Path, kb_db: Path
) -> None:
    """Five tool.timeout signals → loosen (prompt → auto) proposal."""
    _seed_tool_signal_cluster(
        evolution_db,
        tool="web_search",
        event_kind="tool.timeout",
        count=5,
    )

    config = EngineConfig(
        db_path=evolution_db,
        kb_path=kb_db,
        lookback_days=1,
        min_cluster_size=3,
        enabled_kinds=(KIND_TOOL_POLICY,),
    )
    summary = await EvolutionEngine(config).run_once()

    assert summary.proposals_written == 1
    diff = json.loads(str(_all_proposals(evolution_db)[0]["diff"]))
    assert diff["before"] == "prompt"
    assert diff["after"] == "auto"
    assert diff["rule_id"] == "tool_policy.web_search.loosen"


async def test_loosen_default_threshold_is_higher_than_tighten(
    evolution_db: Path, kb_db: Path
) -> None:
    """Four timeouts (below default loosen=5) → no proposal."""
    _seed_tool_signal_cluster(
        evolution_db,
        tool="web_search",
        event_kind="tool.timeout",
        count=4,
    )

    config = EngineConfig(
        db_path=evolution_db,
        kb_path=kb_db,
        lookback_days=1,
        min_cluster_size=3,
        enabled_kinds=(KIND_TOOL_POLICY,),
    )
    summary = await EvolutionEngine(config).run_once()

    assert summary.clusters_found == 1
    # Cluster is large enough for the engine, but the handler floors
    # loosening at 5 by default — so nothing is emitted.
    assert summary.proposals_written == 0


# ---------------------------------------------------------------------------
# Threshold not met
# ---------------------------------------------------------------------------


async def test_below_engine_min_cluster_yields_nothing(
    evolution_db: Path, kb_db: Path
) -> None:
    """N-1 signals — engine clustering drops the bucket entirely."""
    _seed_tool_signal_cluster(
        evolution_db,
        tool="web_search",
        event_kind="approval.denied",
        count=2,
    )

    config = EngineConfig(
        db_path=evolution_db,
        kb_path=kb_db,
        lookback_days=1,
        min_cluster_size=3,
        enabled_kinds=(KIND_TOOL_POLICY,),
    )
    summary = await EvolutionEngine(config).run_once()

    assert summary.signals_loaded == 2
    assert summary.clusters_found == 0
    assert summary.proposals_written == 0


async def test_below_handler_threshold_yields_nothing(
    evolution_db: Path, kb_db: Path
) -> None:
    """Custom tighten_threshold above engine min_cluster_size still gates."""
    _seed_tool_signal_cluster(
        evolution_db,
        tool="web_search",
        event_kind="approval.denied",
        count=3,
    )

    handler = ToolPolicyHandler(tighten_threshold=10)
    config = EngineConfig(
        db_path=evolution_db,
        kb_path=kb_db,
        lookback_days=1,
        min_cluster_size=3,
        enabled_kinds=(KIND_TOOL_POLICY,),
    )
    summary = await EvolutionEngine(config, handlers=[handler]).run_once()

    assert summary.clusters_found == 1
    assert summary.proposals_written == 0


async def test_ignores_unrelated_event_kinds(
    evolution_db: Path, kb_db: Path
) -> None:
    """Cluster on a different event_kind shouldn't trigger this handler."""
    _seed_tool_signal_cluster(
        evolution_db,
        tool="web_search",
        event_kind="skill.invocation.failed",  # not a tool_policy trigger
        count=3,
    )

    config = EngineConfig(
        db_path=evolution_db,
        kb_path=kb_db,
        lookback_days=1,
        min_cluster_size=3,
        enabled_kinds=(KIND_TOOL_POLICY,),
    )
    summary = await EvolutionEngine(config).run_once()

    assert summary.clusters_found == 1
    assert summary.proposals_written == 0


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------


async def test_dedups_against_existing_target(
    evolution_db: Path, kb_db: Path
) -> None:
    """Re-running on the same cluster doesn't double-file a proposal."""
    _seed_tool_signal_cluster(
        evolution_db,
        tool="web_search",
        event_kind="approval.denied",
        count=3,
    )

    config = EngineConfig(
        db_path=evolution_db,
        kb_path=kb_db,
        lookback_days=1,
        min_cluster_size=3,
        enabled_kinds=(KIND_TOOL_POLICY,),
    )
    s1 = await EvolutionEngine(config).run_once()
    assert s1.proposals_written == 1

    s2 = await EvolutionEngine(config).run_once()
    assert s2.proposals_written == 0
    assert s2.skipped_existing == 1
    assert len(_all_proposals(evolution_db)) == 1


# ---------------------------------------------------------------------------
# Risk is always "high"
# ---------------------------------------------------------------------------


async def test_risk_is_always_high(evolution_db: Path, kb_db: Path) -> None:
    """Every tool_policy proposal carries risk='high'."""
    _seed_tool_signal_cluster(
        evolution_db,
        tool="web_search",
        event_kind="approval.denied",
        count=3,
    )
    _seed_tool_signal_cluster(
        evolution_db,
        tool="code_executor",
        event_kind="tool.timeout",
        count=5,
    )

    config = EngineConfig(
        db_path=evolution_db,
        kb_path=kb_db,
        lookback_days=1,
        min_cluster_size=3,
        enabled_kinds=(KIND_TOOL_POLICY,),
    )
    summary = await EvolutionEngine(config).run_once()

    assert summary.proposals_written == 2
    for p in _all_proposals(evolution_db):
        assert p["risk"] == "high", (
            f"tool_policy proposals must always be risk=high so the "
            f"ShadowTester routes them through docker sandbox; got {p['risk']!r}"
        )


# ---------------------------------------------------------------------------
# Tenant id propagation
# ---------------------------------------------------------------------------


async def test_propagates_tenant_id_from_signal(
    evolution_db: Path, kb_db: Path
) -> None:
    """Signal with tenant_id='acme' → proposal carries tenant_id='acme'."""
    _seed_tool_signal_cluster(
        evolution_db,
        tool="web_search",
        event_kind="approval.denied",
        count=3,
        tenant_id="acme",
    )

    config = EngineConfig(
        db_path=evolution_db,
        kb_path=kb_db,
        lookback_days=1,
        min_cluster_size=3,
        enabled_kinds=(KIND_TOOL_POLICY,),
    )
    summary = await EvolutionEngine(config).run_once()

    assert summary.proposals_written == 1
    p = _all_proposals(evolution_db)[0]
    assert p["tenant_id"] == "acme"


async def test_default_tenant_falls_through(
    evolution_db: Path, kb_db: Path
) -> None:
    """Signals without tenant_id → proposal tenant='default'."""
    _seed_tool_signal_cluster(
        evolution_db,
        tool="web_search",
        event_kind="approval.denied",
        count=3,
    )

    config = EngineConfig(
        db_path=evolution_db,
        kb_path=kb_db,
        lookback_days=1,
        min_cluster_size=3,
        enabled_kinds=(KIND_TOOL_POLICY,),
    )
    await EvolutionEngine(config).run_once()

    p = _all_proposals(evolution_db)[0]
    assert p["tenant_id"] == "default"


async def test_two_tenants_emit_independent_proposals(
    evolution_db: Path, kb_db: Path
) -> None:
    """Same tool across two tenants → two independent proposals."""
    _seed_tool_signal_cluster(
        evolution_db,
        tool="web_search",
        event_kind="approval.denied",
        count=3,
        tenant_id="acme",
    )
    _seed_tool_signal_cluster(
        evolution_db,
        tool="web_search",
        event_kind="approval.denied",
        count=3,
        tenant_id="globex",
    )

    config = EngineConfig(
        db_path=evolution_db,
        kb_path=kb_db,
        lookback_days=1,
        min_cluster_size=3,
        enabled_kinds=(KIND_TOOL_POLICY,),
    )
    summary = await EvolutionEngine(config).run_once()

    assert summary.proposals_written == 2
    proposals = _all_proposals(evolution_db)
    tenants = sorted(str(p["tenant_id"]) for p in proposals)
    assert tenants == ["acme", "globex"]
    assert all(p["target"] == "web_search" for p in proposals)


# ---------------------------------------------------------------------------
# Multi-tool / multi-direction shape
# ---------------------------------------------------------------------------


async def test_two_tools_both_emit_proposals(
    evolution_db: Path, kb_db: Path
) -> None:
    """Tighten on one tool + loosen on another → two proposals."""
    _seed_tool_signal_cluster(
        evolution_db,
        tool="web_search",
        event_kind="approval.denied",
        count=3,
    )
    _seed_tool_signal_cluster(
        evolution_db,
        tool="code_executor",
        event_kind="tool.timeout",
        count=5,
    )

    config = EngineConfig(
        db_path=evolution_db,
        kb_path=kb_db,
        lookback_days=1,
        min_cluster_size=3,
        enabled_kinds=(KIND_TOOL_POLICY,),
    )
    summary = await EvolutionEngine(config).run_once()

    assert summary.proposals_written == 2
    proposals = _all_proposals(evolution_db)
    by_target = {str(p["target"]): p for p in proposals}
    web_diff = json.loads(str(by_target["web_search"]["diff"]))
    code_diff = json.loads(str(by_target["code_executor"]["diff"]))
    assert web_diff["after"] == "prompt"
    assert code_diff["after"] == "auto"


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


def test_handler_rejects_zero_thresholds() -> None:
    import pytest

    with pytest.raises(ValueError, match="tighten_threshold"):
        ToolPolicyHandler(tighten_threshold=0)
    with pytest.raises(ValueError, match="loosen_threshold"):
        ToolPolicyHandler(loosen_threshold=0)
