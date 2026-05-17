"""End-to-end ``AutoRollbackMonitor.run_once`` tests.

Ports ``rust/crates/corlinman-auto-rollback/src/monitor.rs::tests``.

Each test seeds a real :class:`EvolutionStore` + one (or zero)
applied proposals + an optional history row, then runs one pass and
asserts the :class:`RunSummary` counters and applier interaction.
"""

from __future__ import annotations

from typing import Any

import pytest
from corlinman_auto_rollback.config import (
    AutoRollbackThresholds,
    EvolutionAutoRollbackConfig,
)
from corlinman_auto_rollback.monitor import AutoRollbackMonitor, now_ms
from corlinman_auto_rollback.revert import (
    NotAppliedRevertError,
    RevertError,
)
from corlinman_evolution_store import (
    EvolutionHistory,
    EvolutionKind,
    EvolutionProposal,
    EvolutionRisk,
    EvolutionStatus,
    EvolutionStore,
    HistoryRepo,
    ProposalId,
    ProposalsRepo,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _MockApplier:
    """Captures every (id, reason) pair the monitor passes in; returns
    whatever ``result`` says. Mirrors the Rust ``MockApplier``."""

    def __init__(self, result: RevertError | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._result = result

    async def revert(self, proposal_id: ProposalId, reason: str) -> None:
        self.calls.append((str(proposal_id), reason))
        if self._result is not None:
            raise self._result


def _sample_config(
    grace_hours: int, min_baseline: int, err_pct: float
) -> EvolutionAutoRollbackConfig:
    return EvolutionAutoRollbackConfig(
        enabled=True,
        grace_window_hours=grace_hours,
        thresholds=AutoRollbackThresholds(
            default_err_rate_delta_pct=err_pct,
            default_p95_latency_delta_pct=25.0,
            signal_window_secs=1_800,
            min_baseline_signals=min_baseline,
        ),
    )


async def _seed_applied(
    repo: ProposalsRepo,
    *,
    proposal_id: str,
    kind: EvolutionKind,
    target: str,
    applied_at_ms: int,
) -> ProposalId:
    pid = ProposalId(proposal_id)
    await repo.insert(
        EvolutionProposal(
            id=pid,
            kind=kind,
            target=target,
            diff="",
            reasoning="",
            risk=EvolutionRisk.LOW,
            budget_cost=0,
            status=EvolutionStatus.APPLIED,
            shadow_metrics=None,
            signal_ids=[],
            trace_ids=[],
            created_at=1_000,
            decided_at=2_000,
            decided_by="auto",
            applied_at=applied_at_ms,
            rollback_of=None,
            eval_run_id=None,
            baseline_metrics_json=None,
            auto_rollback_at=None,
            auto_rollback_reason=None,
            metadata=None,
        )
    )
    # ``ProposalsRepo.insert`` writes status from the EvolutionProposal
    # row, but the Rust schema mirror uses a CHECK constraint that
    # accepts ``applied``. Patch the timestamp via mark_applied so the
    # ``applied_at`` column reflects the test-specified time exactly —
    # the helper's INSERT already passed it through ``applied_at`` so
    # this is a no-op safeguard if the schema changes.
    return pid


async def _seed_history(
    history: HistoryRepo,
    *,
    proposal_id: ProposalId,
    target: str,
    baseline_json: Any,
) -> None:
    await history.insert(
        EvolutionHistory(
            id=None,
            proposal_id=proposal_id,
            kind=EvolutionKind.MEMORY_OP,
            target=target,
            before_sha="x",
            after_sha="y",
            inverse_diff=(
                '{"action":"restore_chunk","content":"x",'
                '"namespace":"general","file_id":1,"chunk_index":0}'
            ),
            metrics_baseline=baseline_json,
            applied_at=3_000,
            rolled_back_at=None,
            rollback_reason=None,
            share_with=None,
        )
    )


async def _seed_signals(
    store: EvolutionStore, *, target: str, n: int, observed_at: int
) -> None:
    for _ in range(n):
        await store.conn.execute(
            """INSERT INTO evolution_signals
                 (event_kind, target, severity, payload_json, observed_at)
               VALUES ('tool.call.failed', ?, 'error', '{}', ?)""",
            (target, observed_at),
        )
    await store.conn.commit()


def _baseline_json(target: str, count: int) -> dict[str, Any]:
    return {
        "target": target,
        "captured_at_ms": 0,
        "window_secs": 1_800,
        "counts": {
            "tool.call.failed": count,
            "search.recall.dropped": 0,
        },
    }


# ---------------------------------------------------------------------------
# Tests — one per Rust test, same names.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_once_no_proposals_in_window(
    store: EvolutionStore,
    repos: tuple[ProposalsRepo, HistoryRepo],
) -> None:
    proposals, history = repos
    mock = _MockApplier()
    monitor = AutoRollbackMonitor(
        proposals, history, store, mock, _sample_config(72, 5, 50.0)
    )
    summary = await monitor.run_once()
    assert summary.proposals_inspected == 0
    assert summary.thresholds_breached == 0
    assert summary.rollbacks_triggered == 0
    assert summary.rollbacks_succeeded == 0
    assert summary.rollbacks_failed == 0
    assert summary.errors == 0
    assert mock.calls == []


@pytest.mark.asyncio
async def test_run_once_skips_kind_without_whitelist(
    store: EvolutionStore,
    repos: tuple[ProposalsRepo, HistoryRepo],
) -> None:
    proposals, history = repos
    # tag_rebalance has no whitelist — must not be inspected.
    pid = await _seed_applied(
        proposals,
        proposal_id="evol-skip-tag",
        kind=EvolutionKind.TAG_REBALANCE,
        target="tag_tree",
        applied_at_ms=now_ms(),
    )
    # Insert a history row anyway; the monitor never reaches it.
    await history.insert(
        EvolutionHistory(
            id=None,
            proposal_id=pid,
            kind=EvolutionKind.TAG_REBALANCE,
            target="tag_tree",
            before_sha="x",
            after_sha="y",
            inverse_diff="{}",
            metrics_baseline={},
            applied_at=3_000,
            rolled_back_at=None,
            rollback_reason=None,
            share_with=None,
        )
    )
    mock = _MockApplier()
    monitor = AutoRollbackMonitor(
        proposals, history, store, mock, _sample_config(72, 5, 50.0)
    )
    summary = await monitor.run_once()
    assert summary.proposals_inspected == 0, "kind without whitelist must skip"
    assert summary.errors == 0
    assert mock.calls == []


@pytest.mark.asyncio
async def test_run_once_no_breach_keeps_proposal_applied(
    store: EvolutionStore,
    repos: tuple[ProposalsRepo, HistoryRepo],
) -> None:
    proposals, history = repos
    now = now_ms()
    target = "delete_chunk:42"
    pid = await _seed_applied(
        proposals,
        proposal_id="evol-no-breach",
        kind=EvolutionKind.MEMORY_OP,
        target=target,
        applied_at_ms=now,
    )
    # High baseline (50) but no fresh signals — delta is negative.
    await _seed_history(
        history, proposal_id=pid, target=target, baseline_json=_baseline_json(target, 50)
    )
    mock = _MockApplier()
    monitor = AutoRollbackMonitor(
        proposals, history, store, mock, _sample_config(72, 5, 50.0)
    )
    summary = await monitor.run_once()
    assert summary.proposals_inspected == 1
    assert summary.thresholds_breached == 0
    assert summary.rollbacks_triggered == 0
    assert mock.calls == [], "no breach -> no revert call"


@pytest.mark.asyncio
async def test_run_once_breach_triggers_revert(
    store: EvolutionStore,
    repos: tuple[ProposalsRepo, HistoryRepo],
) -> None:
    proposals, history = repos
    now = now_ms()
    target = "delete_chunk:7"
    pid = await _seed_applied(
        proposals,
        proposal_id="evol-breach",
        kind=EvolutionKind.MEMORY_OP,
        target=target,
        applied_at_ms=now,
    )
    # Baseline 10 -> seed 100 fresh error signals -> +900%.
    await _seed_history(
        history, proposal_id=pid, target=target, baseline_json=_baseline_json(target, 10)
    )
    await _seed_signals(store, target=target, n=100, observed_at=now - 1_000)

    mock = _MockApplier()
    monitor = AutoRollbackMonitor(
        proposals, history, store, mock, _sample_config(72, 5, 50.0)
    )
    summary = await monitor.run_once()
    assert summary.proposals_inspected == 1
    assert summary.thresholds_breached == 1
    assert summary.rollbacks_triggered == 1
    assert summary.rollbacks_succeeded == 1
    assert summary.rollbacks_failed == 0

    assert len(mock.calls) == 1
    assert mock.calls[0][0] == "evol-breach"
    assert "breaches threshold" in mock.calls[0][1], (
        f"reason should match the metrics summary; got {mock.calls[0][1]!r}"
    )


@pytest.mark.asyncio
async def test_run_once_handles_revert_already_rolled_back(
    store: EvolutionStore,
    repos: tuple[ProposalsRepo, HistoryRepo],
) -> None:
    proposals, history = repos
    now = now_ms()
    target = "delete_chunk:11"
    pid = await _seed_applied(
        proposals,
        proposal_id="evol-race",
        kind=EvolutionKind.MEMORY_OP,
        target=target,
        applied_at_ms=now,
    )
    await _seed_history(
        history, proposal_id=pid, target=target, baseline_json=_baseline_json(target, 10)
    )
    await _seed_signals(store, target=target, n=100, observed_at=now - 1_000)

    mock = _MockApplier(NotAppliedRevertError("rolled_back"))
    monitor = AutoRollbackMonitor(
        proposals, history, store, mock, _sample_config(72, 5, 50.0)
    )
    summary = await monitor.run_once()
    assert summary.thresholds_breached == 1
    assert summary.rollbacks_triggered == 1
    assert summary.rollbacks_succeeded == 0
    assert summary.rollbacks_failed == 1
    # Critically: not a panic, not a top-level errors counter bump.
    assert summary.errors == 0


@pytest.mark.asyncio
async def test_run_once_corrupted_baseline_json_does_not_revert(
    store: EvolutionStore,
    repos: tuple[ProposalsRepo, HistoryRepo],
) -> None:
    proposals, history = repos
    now = now_ms()
    target = "delete_chunk:99"
    pid = await _seed_applied(
        proposals,
        proposal_id="evol-bad-json",
        kind=EvolutionKind.MEMORY_OP,
        target=target,
        applied_at_ms=now,
    )
    # baseline is a string, not an object — parse must fail.
    await _seed_history(
        history,
        proposal_id=pid,
        target=target,
        baseline_json="totally-not-a-snapshot",
    )
    await _seed_signals(store, target=target, n=100, observed_at=now - 1_000)

    mock = _MockApplier()
    monitor = AutoRollbackMonitor(
        proposals, history, store, mock, _sample_config(72, 5, 50.0)
    )
    summary = await monitor.run_once()
    assert summary.errors == 1
    assert summary.proposals_inspected == 0, "skipped before delta"
    assert mock.calls == [], "corruption must not auto-revert"


@pytest.mark.asyncio
async def test_with_max_proposals_per_run_returns_self(
    store: EvolutionStore,
    repos: tuple[ProposalsRepo, HistoryRepo],
) -> None:
    proposals, history = repos
    mock = _MockApplier()
    monitor = AutoRollbackMonitor(
        proposals, history, store, mock, _sample_config(72, 5, 50.0)
    )
    same = monitor.with_max_proposals_per_run(7)
    assert same is monitor
    # Smoke that the run doesn't blow up after the override.
    summary = await monitor.run_once()
    assert summary.errors == 0
