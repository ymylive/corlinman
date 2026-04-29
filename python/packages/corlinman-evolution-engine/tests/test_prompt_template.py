"""Tests for ``PromptTemplateHandler`` — Phase 4 W1 4-1D high-risk kind."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from corlinman_evolution_engine.engine import EngineConfig, EvolutionEngine
from corlinman_evolution_engine.prompt_template import (
    KIND_PROMPT_TEMPLATE,
    PromptTemplateHandler,
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


def _seed_intent_mismatch_cluster(
    db_path: Path,
    *,
    target: str,
    count: int,
    tenant_id: str = "default",
    event_kind: str = "chat.intent_mismatch",
) -> list[int]:
    """Insert ``count`` ``chat.intent_mismatch`` signals on ``target``."""
    now_ms = int(time.time() * 1_000)
    ids: list[int] = []
    for i in range(count):
        sid = insert_signal(
            db_path,
            event_kind=event_kind,
            target=target,
            severity="warn",
            payload_json='{"reason": "missed_intent"}',
            trace_id=f"trace-{target}-{i}",
            session_id="sess-prompt",
            observed_at=now_ms - 60_000 + i,
            tenant_id=tenant_id,
        )
        ids.append(sid)
    return ids


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_propose_emits_one_proposal_per_segment(
    evolution_db: Path, kb_db: Path
) -> None:
    """Three intent-mismatch signals on one segment → one proposal."""
    _seed_intent_mismatch_cluster(
        evolution_db, target="agent.greeting", count=3
    )

    config = EngineConfig(
        db_path=evolution_db,
        kb_path=kb_db,
        lookback_days=1,
        min_cluster_size=3,
        enabled_kinds=(KIND_PROMPT_TEMPLATE,),
    )
    summary = await EvolutionEngine(config).run_once()

    assert summary.proposals_written == 1
    assert summary.proposals_by_kind == {KIND_PROMPT_TEMPLATE: 1}

    proposals = _all_proposals(evolution_db)
    assert len(proposals) == 1
    p = proposals[0]
    assert p["kind"] == KIND_PROMPT_TEMPLATE
    assert p["target"] == "agent.greeting"
    # high-risk because applying changes agent behaviour
    assert p["risk"] == "high"
    assert p["status"] == "pending"
    assert p["budget_cost"] == 3
    sig_ids = json.loads(str(p["signal_ids"]))
    assert sig_ids == [1, 2, 3]


async def test_propose_diff_is_valid_json_with_before_after(
    evolution_db: Path, kb_db: Path
) -> None:
    """Diff is JSON ``{before, after, rationale}`` per the kind contract."""
    _seed_intent_mismatch_cluster(
        evolution_db, target="tool.web_search.system", count=4
    )

    config = EngineConfig(
        db_path=evolution_db,
        kb_path=kb_db,
        lookback_days=1,
        min_cluster_size=3,
        enabled_kinds=(KIND_PROMPT_TEMPLATE,),
    )
    await EvolutionEngine(config).run_once()

    diff = json.loads(str(_all_proposals(evolution_db)[0]["diff"]))
    assert set(diff.keys()) == {"before", "after", "rationale"}
    # Applier fills ``before`` from the live template at apply time.
    assert diff["before"] == ""
    # ``after`` mentions the segment so the operator's editor is
    # pre-loaded with context.
    assert "tool.web_search.system" in diff["after"]
    assert "tool.web_search.system" in diff["rationale"]


async def test_propose_handles_poor_quality_event(
    evolution_db: Path, kb_db: Path
) -> None:
    """``agent.poor_response_quality`` is also a trigger."""
    _seed_intent_mismatch_cluster(
        evolution_db,
        target="agent.fallback",
        count=3,
        event_kind="agent.poor_response_quality",
    )

    config = EngineConfig(
        db_path=evolution_db,
        kb_path=kb_db,
        lookback_days=1,
        min_cluster_size=3,
        enabled_kinds=(KIND_PROMPT_TEMPLATE,),
    )
    summary = await EvolutionEngine(config).run_once()

    assert summary.proposals_written == 1
    proposals = _all_proposals(evolution_db)
    assert proposals[0]["target"] == "agent.fallback"


# ---------------------------------------------------------------------------
# Threshold not met
# ---------------------------------------------------------------------------


async def test_propose_below_engine_min_cluster_yields_nothing(
    evolution_db: Path, kb_db: Path
) -> None:
    """N-1 signals — engine clustering drops the bucket entirely."""
    _seed_intent_mismatch_cluster(
        evolution_db, target="agent.greeting", count=2
    )

    config = EngineConfig(
        db_path=evolution_db,
        kb_path=kb_db,
        lookback_days=1,
        min_cluster_size=3,
        enabled_kinds=(KIND_PROMPT_TEMPLATE,),
    )
    summary = await EvolutionEngine(config).run_once()

    assert summary.signals_loaded == 2
    assert summary.clusters_found == 0
    assert summary.proposals_written == 0
    assert _all_proposals(evolution_db) == []


async def test_propose_below_handler_threshold_yields_nothing(
    evolution_db: Path, kb_db: Path
) -> None:
    """Handler-level threshold above engine min_cluster_size still gates."""
    _seed_intent_mismatch_cluster(
        evolution_db, target="agent.greeting", count=3
    )

    handler = PromptTemplateHandler(threshold=5)
    config = EngineConfig(
        db_path=evolution_db,
        kb_path=kb_db,
        lookback_days=1,
        # Engine accepts the cluster (size 3 >= 3) but the handler floor
        # is 5, so it emits nothing.
        min_cluster_size=3,
        enabled_kinds=(KIND_PROMPT_TEMPLATE,),
    )
    summary = await EvolutionEngine(config, handlers=[handler]).run_once()

    assert summary.clusters_found == 1
    assert summary.proposals_written == 0
    assert _all_proposals(evolution_db) == []


async def test_propose_ignores_unrelated_event_kinds(
    evolution_db: Path, kb_db: Path
) -> None:
    """Cluster on a different event_kind shouldn't trigger this handler."""
    now_ms = int(time.time() * 1_000)
    for i in range(3):
        insert_signal(
            evolution_db,
            event_kind="tool.call.failed",  # not a prompt_template trigger
            target="agent.greeting",
            severity="error",
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
        enabled_kinds=(KIND_PROMPT_TEMPLATE,),
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
    _seed_intent_mismatch_cluster(
        evolution_db, target="agent.greeting", count=3
    )

    config = EngineConfig(
        db_path=evolution_db,
        kb_path=kb_db,
        lookback_days=1,
        min_cluster_size=3,
        enabled_kinds=(KIND_PROMPT_TEMPLATE,),
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


async def test_propose_risk_is_always_high(
    evolution_db: Path, kb_db: Path
) -> None:
    """Every prompt_template proposal carries risk='high'."""
    _seed_intent_mismatch_cluster(
        evolution_db, target="agent.greeting", count=3
    )
    _seed_intent_mismatch_cluster(
        evolution_db, target="tool.web_search.system", count=4
    )

    config = EngineConfig(
        db_path=evolution_db,
        kb_path=kb_db,
        lookback_days=1,
        min_cluster_size=3,
        enabled_kinds=(KIND_PROMPT_TEMPLATE,),
    )
    summary = await EvolutionEngine(config).run_once()

    assert summary.proposals_written == 2
    for p in _all_proposals(evolution_db):
        assert p["risk"] == "high", (
            f"prompt_template proposals must always be risk=high so the "
            f"ShadowTester routes them through docker sandbox; got {p['risk']!r}"
        )


# ---------------------------------------------------------------------------
# Tenant id propagation
# ---------------------------------------------------------------------------


async def test_propose_propagates_tenant_id_from_signal(
    evolution_db: Path, kb_db: Path
) -> None:
    """Signal with tenant_id='acme' → proposal carries tenant_id='acme'."""
    _seed_intent_mismatch_cluster(
        evolution_db,
        target="agent.greeting",
        count=3,
        tenant_id="acme",
    )

    config = EngineConfig(
        db_path=evolution_db,
        kb_path=kb_db,
        lookback_days=1,
        min_cluster_size=3,
        enabled_kinds=(KIND_PROMPT_TEMPLATE,),
    )
    summary = await EvolutionEngine(config).run_once()

    assert summary.proposals_written == 1
    p = _all_proposals(evolution_db)[0]
    assert p["tenant_id"] == "acme"


async def test_propose_default_tenant_falls_through(
    evolution_db: Path, kb_db: Path
) -> None:
    """Signals without an explicit tenant_id → proposal tenant='default'."""
    _seed_intent_mismatch_cluster(
        evolution_db,
        target="agent.greeting",
        count=3,
        # tenant_id defaults to "default" via the conftest helper.
    )

    config = EngineConfig(
        db_path=evolution_db,
        kb_path=kb_db,
        lookback_days=1,
        min_cluster_size=3,
        enabled_kinds=(KIND_PROMPT_TEMPLATE,),
    )
    await EvolutionEngine(config).run_once()

    p = _all_proposals(evolution_db)[0]
    assert p["tenant_id"] == "default"


async def test_propose_two_tenants_emit_independent_proposals(
    evolution_db: Path, kb_db: Path
) -> None:
    """Same target across two tenants → two independent proposals."""
    _seed_intent_mismatch_cluster(
        evolution_db,
        target="agent.greeting",
        count=3,
        tenant_id="acme",
    )
    _seed_intent_mismatch_cluster(
        evolution_db,
        target="agent.greeting",
        count=3,
        tenant_id="globex",
    )

    config = EngineConfig(
        db_path=evolution_db,
        kb_path=kb_db,
        lookback_days=1,
        min_cluster_size=3,
        enabled_kinds=(KIND_PROMPT_TEMPLATE,),
    )
    summary = await EvolutionEngine(config).run_once()

    assert summary.proposals_written == 2
    proposals = _all_proposals(evolution_db)
    tenants = sorted(str(p["tenant_id"]) for p in proposals)
    assert tenants == ["acme", "globex"]
    # Both rows share the same target — dedup is per (kind, target) and
    # the existing handler dedup is global across tenants. We rely on
    # the cluster-key tenant axis to split them in the FIRST run; on a
    # re-run of the same fixture, both targets ARE deduped because the
    # handler queries by (kind, target). That's a known follow-up for
    # the Rust applier — see the report.
    assert all(p["target"] == "agent.greeting" for p in proposals)
