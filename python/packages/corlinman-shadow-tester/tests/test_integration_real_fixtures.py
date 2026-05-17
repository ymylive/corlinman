"""Cross-component smoke test: real ShadowRunner + real MemoryOpSimulator
+ the 4 hand-crafted fixtures under ``tests/fixtures/eval/memory_op/``.

Port of ``rust/.../tests/integration_real_fixtures.rs``.

End-state assertion: a single high-risk memory_op proposal flows
``Pending -> ShadowDone``, with ``shadow_metrics.pass_rate = 1.0`` and
``failed_cases = []``.
"""

from __future__ import annotations

import json
from pathlib import Path

from corlinman_evolution_store import (
    EvolutionKind,
    EvolutionProposal,
    EvolutionRisk,
    EvolutionStatus,
    EvolutionStore,
    ProposalId,
    ProposalsRepo,
)

from corlinman_shadow_tester import (
    MemoryOpSimulator,
    ShadowRunner,
)


async def test_shadow_run_passes_all_real_memory_op_fixtures(
    tmp_path: Path,
    fixtures_root: Path,
    proposals_repo: ProposalsRepo,
    store: EvolutionStore,
) -> None:
    # 1. Seed one high-risk memory_op proposal — this is the row the
    #    runner should claim, shadow, and mark ``shadow_done``.
    pid = ProposalId("evol-test-shadow-real-001")
    await proposals_repo.insert(
        EvolutionProposal(
            id=pid,
            kind=EvolutionKind.MEMORY_OP,
            target="merge_chunks:1,2",
            diff="",
            reasoning="real-fixture integration test",
            risk=EvolutionRisk.HIGH,
            budget_cost=1,
            status=EvolutionStatus.PENDING,
            created_at=1_000,
        )
    )

    # 2. Wire the runner against the package's own fixture tree. kb_path
    #    points at a non-existent file: the runner's fallback creates
    #    an empty schema in the per-case tempdir before the simulator
    #    reopens it.
    kb_path = tmp_path / "kb-does-not-exist.sqlite"

    runner = ShadowRunner(
        proposals=proposals_repo,
        kb_path=kb_path,
        eval_set_dir=fixtures_root,
    )
    runner.register_simulator(MemoryOpSimulator())

    summary = await runner.run_once()

    # 3. Orchestration assertions — exactly one proposal claimed +
    #    completed, no errors.
    assert summary.proposals_claimed == 1
    assert summary.proposals_completed == 1
    assert summary.proposals_failed == 0
    assert summary.errors == 0
    assert summary.cases_run == 4

    # 4. Row-level assertions — status terminal, all v0.3 columns
    #    populated, pass_rate is the contract: 4/4 fixtures pass.
    after = await proposals_repo.get(pid)
    assert after.status == EvolutionStatus.SHADOW_DONE

    cursor = await store.conn.execute(
        "SELECT eval_run_id, baseline_metrics_json, shadow_metrics "
        "FROM evolution_proposals WHERE id = ?",
        (pid,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    assert row is not None

    eval_run_id = row[0]
    assert eval_run_id is not None
    assert eval_run_id.startswith(
        "eval-"
    ), f"eval_run_id should follow `eval-<...>` convention, got {eval_run_id!r}"

    baseline = json.loads(row[1])
    shadow = json.loads(row[2])

    # Fields present on both blobs.
    for key in [
        "eval_run_id",
        "kind",
        "total_cases",
        "pass_rate",
        "p95_latency_ms",
    ]:
        assert key in baseline, f"baseline missing {key}"
        assert key in shadow, f"shadow missing {key}"

    assert shadow["total_cases"] == 4
    assert shadow["passed_cases"] == 4, "all 4 fixtures must pass"
    assert shadow["pass_rate"] == 1.0
    assert len(shadow["failed_cases"]) == 0, (
        f"no fixture should fail; got: {shadow['failed_cases']}"
    )
    assert shadow["kind"] == "memory_op"
