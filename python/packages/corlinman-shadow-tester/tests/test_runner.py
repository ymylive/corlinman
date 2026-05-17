"""Runner tests — ports of ``rust/.../src/runner.rs#tests``."""

from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
import pytest
from corlinman_evolution_store import (
    EvolutionKind,
    EvolutionProposal,
    EvolutionRisk,
    EvolutionStatus,
    EvolutionStore,
    ProposalId,
    ProposalsRepo,
)

from corlinman_shadow_tester.eval import EvalCase, ExpectedOutcome, ProposalSpec
from corlinman_shadow_tester.runner import ShadowRunner
from corlinman_shadow_tester.simulator import SimulatorOutput


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class MockSimulator:
    """Pretend simulator: returns a deterministic merge. Real
    :class:`MemoryOpSimulator` is exercised by the integration test."""

    def __init__(self, kind: EvolutionKind) -> None:
        self._kind = kind

    def kind(self) -> EvolutionKind:
        return self._kind

    async def simulate(self, case: EvalCase, kb_path: Path) -> SimulatorOutput:
        return SimulatorOutput(
            case_name=case.name,
            passed=True,
            latency_ms=5,
            baseline={"chunks_total": 0},
            shadow={"chunks_total": 0, "rows_merged": 1},
        )


async def _write_eval_set(dir_: Path, kind: EvolutionKind) -> None:
    kind_dir = dir_ / kind.as_str()
    kind_dir.mkdir(parents=True, exist_ok=True)
    body = """
description: mock case
kb_seed: []
proposal:
  target: "merge_chunks:1,2"
  reasoning: "mock"
  risk: high
expected:
  outcome: no_op
"""
    (kind_dir / "case-001.yaml").write_text(body)


def _proposal(id_: str, kind: EvolutionKind, risk: EvolutionRisk) -> EvolutionProposal:
    return EvolutionProposal(
        id=ProposalId(id_),
        kind=kind,
        target="merge_chunks:1,2",
        diff="",
        reasoning="fixture",
        risk=risk,
        budget_cost=0,
        status=EvolutionStatus.PENDING,
        created_at=1_000,
    )


# ---------------------------------------------------------------------------
# run_once
# ---------------------------------------------------------------------------


async def test_run_once_processes_pending_high_risk(
    tmp_path: Path,
    proposals_repo: ProposalsRepo,
    store: EvolutionStore,
) -> None:
    pid = ProposalId("p-1")
    await proposals_repo.insert(
        _proposal("p-1", EvolutionKind.MEMORY_OP, EvolutionRisk.HIGH)
    )

    eval_dir = tmp_path / "eval"
    await _write_eval_set(eval_dir, EvolutionKind.MEMORY_OP)

    runner = ShadowRunner(
        proposals=proposals_repo,
        kb_path=tmp_path / "kb-missing.sqlite",  # bootstrap empty kb
        eval_set_dir=eval_dir,
    )
    runner.register_simulator(MockSimulator(EvolutionKind.MEMORY_OP))

    summary = await runner.run_once()
    assert summary.proposals_claimed == 1
    assert summary.proposals_completed == 1
    assert summary.cases_run == 1

    after = await proposals_repo.get(pid)
    assert after.status == EvolutionStatus.SHADOW_DONE

    cursor = await store.conn.execute(
        "SELECT shadow_metrics, baseline_metrics_json, eval_run_id "
        "FROM evolution_proposals WHERE id = ?",
        (pid,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    assert row is not None
    shadow = json.loads(row[0])
    assert shadow["total_cases"] == 1
    assert shadow["passed_cases"] == 1
    assert row[1] is not None, "baseline metrics persisted"
    assert str(row[2]).startswith("eval-")


async def test_run_once_skips_low_risk(
    tmp_path: Path,
    proposals_repo: ProposalsRepo,
) -> None:
    pid = ProposalId("p-low")
    await proposals_repo.insert(
        _proposal("p-low", EvolutionKind.MEMORY_OP, EvolutionRisk.LOW)
    )
    eval_dir = tmp_path / "eval"
    await _write_eval_set(eval_dir, EvolutionKind.MEMORY_OP)

    runner = ShadowRunner(
        proposals=proposals_repo,
        kb_path=tmp_path / "kb.sqlite",
        eval_set_dir=eval_dir,
    )
    runner.register_simulator(MockSimulator(EvolutionKind.MEMORY_OP))

    summary = await runner.run_once()
    assert summary.proposals_claimed == 0

    after = await proposals_repo.get(pid)
    assert after.status == EvolutionStatus.PENDING


async def test_run_once_handles_missing_eval_set(
    tmp_path: Path,
    proposals_repo: ProposalsRepo,
    store: EvolutionStore,
) -> None:
    pid = ProposalId("p-noeval")
    await proposals_repo.insert(
        _proposal("p-noeval", EvolutionKind.MEMORY_OP, EvolutionRisk.HIGH)
    )

    # eval_set_dir exists but has no per-kind subdir -> MissingDirError.
    eval_dir = tmp_path / "eval"
    eval_dir.mkdir(parents=True)

    runner = ShadowRunner(
        proposals=proposals_repo,
        kb_path=tmp_path / "kb.sqlite",
        eval_set_dir=eval_dir,
    )
    runner.register_simulator(MockSimulator(EvolutionKind.MEMORY_OP))

    summary = await runner.run_once()
    assert summary.proposals_claimed == 1
    assert summary.proposals_completed == 1
    assert summary.cases_run == 0

    after = await proposals_repo.get(pid)
    assert after.status == EvolutionStatus.SHADOW_DONE

    cursor = await store.conn.execute(
        "SELECT eval_run_id FROM evolution_proposals WHERE id = ?",
        (pid,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    assert row is not None
    assert row[0] == "no-eval-set"


async def test_run_once_skips_no_simulator_registered(
    tmp_path: Path,
    proposals_repo: ProposalsRepo,
) -> None:
    pid = ProposalId("p-skill")
    await proposals_repo.insert(
        _proposal("p-skill", EvolutionKind.SKILL_UPDATE, EvolutionRisk.HIGH)
    )

    eval_dir = tmp_path / "eval"
    await _write_eval_set(eval_dir, EvolutionKind.MEMORY_OP)

    runner = ShadowRunner(
        proposals=proposals_repo,
        kb_path=tmp_path / "kb.sqlite",
        eval_set_dir=eval_dir,
    )
    # Only memory_op registered; skill_update has no handler.
    runner.register_simulator(MockSimulator(EvolutionKind.MEMORY_OP))

    summary = await runner.run_once()
    assert summary.proposals_claimed == 0

    after = await proposals_repo.get(pid)
    assert after.status == EvolutionStatus.PENDING
