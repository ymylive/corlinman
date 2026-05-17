"""Repo tests — ports of ``rust/.../src/repo.rs#tests``."""

from __future__ import annotations

import json

import pytest
from corlinman_evolution_store import (
    EvolutionGuardConfig,
    EvolutionHistory,
    EvolutionKind,
    EvolutionProposal,
    EvolutionRisk,
    EvolutionSignal,
    EvolutionStatus,
    EvolutionStore,
    HistoryRepo,
    IntentLogRepo,
    NotFoundError,
    ProposalId,
    ProposalsRepo,
    RecursionGuardCooldownError,
    RecursionGuardViolationError,
    SignalSeverity,
    SignalsRepo,
    iso_week_window,
)


# ---------------------------------------------------------------------------
# Helpers — mirror the Rust ``insert_pending`` / ``insert_applied`` fixtures.
# ---------------------------------------------------------------------------


async def _insert_pending(
    repo: ProposalsRepo, id_: str, kind: EvolutionKind, risk: EvolutionRisk
) -> ProposalId:
    pid = ProposalId(id_)
    await repo.insert(
        EvolutionProposal(
            id=pid,
            kind=kind,
            target=f"target-{id_}",
            diff="",
            reasoning="fixture",
            risk=risk,
            budget_cost=0,
            status=EvolutionStatus.PENDING,
            created_at=1_000,
        )
    )
    return pid


async def _insert_applied(repo: ProposalsRepo, id_: str) -> ProposalId:
    pid = ProposalId(id_)
    await repo.insert(
        EvolutionProposal(
            id=pid,
            kind=EvolutionKind.MEMORY_OP,
            target=f"delete_chunk:{id_}",
            diff="",
            reasoning="",
            risk=EvolutionRisk.LOW,
            budget_cost=0,
            status=EvolutionStatus.APPLIED,
            created_at=1_000,
            decided_at=2_000,
            decided_by="auto",
            applied_at=3_000,
        )
    )
    return pid


async def _insert_applied_at(
    repo: ProposalsRepo, id_: str, applied_at_ms: int
) -> ProposalId:
    pid = ProposalId(id_)
    await repo.insert(
        EvolutionProposal(
            id=pid,
            kind=EvolutionKind.MEMORY_OP,
            target=f"delete_chunk:{id_}",
            diff="",
            reasoning="",
            risk=EvolutionRisk.LOW,
            budget_cost=0,
            status=EvolutionStatus.APPLIED,
            created_at=1_000,
            decided_at=2_000,
            decided_by="auto",
            applied_at=applied_at_ms,
        )
    )
    return pid


async def _insert_with_created_at(
    repo: ProposalsRepo, id_: str, kind: EvolutionKind, created_at_ms: int
) -> ProposalId:
    pid = ProposalId(id_)
    await repo.insert(
        EvolutionProposal(
            id=pid,
            kind=kind,
            target=f"target-{id_}",
            diff="",
            reasoning="fixture",
            risk=EvolutionRisk.LOW,
            budget_cost=0,
            status=EvolutionStatus.PENDING,
            created_at=created_at_ms,
        )
    )
    return pid


def _meta_proposal(
    id_: str,
    kind: EvolutionKind,
    created_at: int,
    status: EvolutionStatus,
    applied_at: int | None,
    metadata: dict | None,
) -> EvolutionProposal:
    assert kind.is_meta(), "fixture is for meta kinds only"
    return EvolutionProposal(
        id=ProposalId(id_),
        kind=kind,
        target=f"meta-target-{id_}",
        diff="",
        reasoning="guard fixture",
        risk=EvolutionRisk.HIGH,
        budget_cost=1,
        status=status,
        created_at=created_at,
        decided_at=None if applied_at is None else applied_at - 1,
        decided_by=None if applied_at is None else "operator",
        applied_at=applied_at,
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------


async def test_signals_insert_and_list_round_trip(store: EvolutionStore) -> None:
    repo = SignalsRepo(store.conn)
    new_id = await repo.insert(
        EvolutionSignal(
            event_kind="tool.call.failed",
            target="web_search",
            severity=SignalSeverity.ERROR,
            payload_json={"reason": "timeout"},
            trace_id="t1",
            session_id="s1",
            observed_at=1_000,
            tenant_id="default",
        )
    )
    assert new_id > 0

    rows = await repo.list_since(0, "tool.call.failed", 10)
    assert len(rows) == 1
    assert rows[0].target == "web_search"
    assert rows[0].payload_json["reason"] == "timeout"


# ---------------------------------------------------------------------------
# Proposals — decision flow + shadow flow
# ---------------------------------------------------------------------------


async def test_proposals_decision_flow(store: EvolutionStore) -> None:
    repo = ProposalsRepo(store.conn)
    pid = ProposalId("evol-test-001")
    await repo.insert(
        EvolutionProposal(
            id=pid,
            kind=EvolutionKind.MEMORY_OP,
            target="merge_chunks:42,43",
            diff="",
            reasoning="two near-duplicate chunks",
            risk=EvolutionRisk.LOW,
            budget_cost=0,
            status=EvolutionStatus.PENDING,
            signal_ids=[1, 2, 3],
            trace_ids=["t1"],
            created_at=1_000,
        )
    )

    pending = await repo.list_by_status(EvolutionStatus.PENDING, 10)
    assert len(pending) == 1
    assert pending[0].id == pid

    await repo.set_decision(pid, EvolutionStatus.APPROVED, 2_000, "operator")
    after = await repo.get(pid)
    assert after.status == EvolutionStatus.APPROVED
    assert after.decided_at == 2_000
    assert after.decided_by == "operator"

    await repo.mark_applied(pid, 3_000)
    after = await repo.get(pid)
    assert after.status == EvolutionStatus.APPLIED
    assert after.applied_at == 3_000


async def test_list_pending_for_shadow_filters_kind_and_risk(
    store: EvolutionStore,
) -> None:
    repo = ProposalsRepo(store.conn)
    await _insert_pending(repo, "p-high-mem", EvolutionKind.MEMORY_OP, EvolutionRisk.HIGH)
    await _insert_pending(repo, "p-low-mem", EvolutionKind.MEMORY_OP, EvolutionRisk.LOW)
    await _insert_pending(
        repo, "p-high-skill", EvolutionKind.SKILL_UPDATE, EvolutionRisk.HIGH
    )

    hits = await repo.list_pending_for_shadow(
        EvolutionKind.MEMORY_OP, [EvolutionRisk.MEDIUM, EvolutionRisk.HIGH], 10
    )
    assert len(hits) == 1
    assert hits[0].id == "p-high-mem"


async def test_claim_for_shadow_transitions_then_fails_on_non_pending(
    store: EvolutionStore,
) -> None:
    repo = ProposalsRepo(store.conn)
    pid = await _insert_pending(
        repo, "p-claim", EvolutionKind.MEMORY_OP, EvolutionRisk.HIGH
    )
    await repo.claim_for_shadow(pid)
    after = await repo.get(pid)
    assert after.status == EvolutionStatus.SHADOW_RUNNING

    with pytest.raises(NotFoundError):
        await repo.claim_for_shadow(pid)


async def test_mark_shadow_done_persists_metrics_and_eval_id(
    store: EvolutionStore,
) -> None:
    repo = ProposalsRepo(store.conn)
    pid = await _insert_pending(repo, "p-done", EvolutionKind.MEMORY_OP, EvolutionRisk.HIGH)
    await repo.claim_for_shadow(pid)

    baseline = {"chunks_total": 2}
    shadow = {"chunks_total": 1, "rows_merged": 1}
    await repo.mark_shadow_done(pid, "eval-2026-04-27-abc123", baseline, shadow)

    cursor = await store.conn.execute(
        "SELECT status, eval_run_id, baseline_metrics_json, shadow_metrics "
        "FROM evolution_proposals WHERE id = ?",
        (pid,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    assert row is not None
    assert row[0] == "shadow_done"
    assert row[1] == "eval-2026-04-27-abc123"
    assert json.loads(row[2]) == baseline
    assert json.loads(row[3]) == shadow


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


async def test_history_insert_and_rollback(store: EvolutionStore) -> None:
    proposals = ProposalsRepo(store.conn)
    pid = ProposalId("evol-test-002")
    await proposals.insert(
        EvolutionProposal(
            id=pid,
            kind=EvolutionKind.TAG_REBALANCE,
            target="tag_tree",
            diff="",
            reasoning="",
            risk=EvolutionRisk.LOW,
            budget_cost=0,
            status=EvolutionStatus.APPLIED,
            created_at=1_000,
            decided_at=2_000,
            decided_by="auto",
            applied_at=3_000,
        )
    )

    history = HistoryRepo(store.conn)
    hid = await history.insert(
        EvolutionHistory(
            proposal_id=pid,
            kind=EvolutionKind.TAG_REBALANCE,
            target="tag_tree",
            before_sha="abc",
            after_sha="def",
            inverse_diff="noop",
            metrics_baseline={"err_rate": 0.02},
            applied_at=3_000,
        )
    )
    assert hid > 0

    await history.mark_rolled_back(pid, 4_000, "metrics regression")
    cursor = await store.conn.execute(
        "SELECT rolled_back_at, rollback_reason FROM evolution_history WHERE proposal_id = ?",
        (pid,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    assert row is not None
    assert row[0] == 4_000
    assert row[1] == "metrics regression"


async def test_history_latest_for_proposal_round_trip(store: EvolutionStore) -> None:
    proposals = ProposalsRepo(store.conn)
    pid = await _insert_applied(proposals, "evol-hist-001")
    history = HistoryRepo(store.conn)
    hid = await history.insert(
        EvolutionHistory(
            proposal_id=pid,
            kind=EvolutionKind.MEMORY_OP,
            target="delete_chunk:42",
            before_sha="aaa",
            after_sha="bbb",
            inverse_diff=(
                '{"action":"restore_chunk","content":"x","namespace":"general",'
                '"file_id":1,"chunk_index":0}'
            ),
            metrics_baseline={"target": "delete_chunk:42"},
            applied_at=3_000,
        )
    )

    got = await history.latest_for_proposal(pid)
    assert got.id == hid
    assert got.proposal_id == pid
    assert got.kind == EvolutionKind.MEMORY_OP
    assert got.target == "delete_chunk:42"
    assert got.applied_at == 3_000
    assert "restore_chunk" in got.inverse_diff

    with pytest.raises(NotFoundError):
        await history.latest_for_proposal(ProposalId("evol-hist-nope"))


async def test_share_with_round_trips_through_history(store: EvolutionStore) -> None:
    """``Some(non-empty)``, ``Some(empty)``, and ``None`` each round-trip
    byte-for-byte. The distinction matters: empty = "operator approved
    with no peers", None = "legacy unfederated apply"."""
    proposals = ProposalsRepo(store.conn)
    history = HistoryRepo(store.conn)

    cases: list[tuple[str, list[str] | None]] = [
        ("legacy", None),
        ("empty-peers", []),
        ("two-peers", ["bravo", "charlie"]),
    ]
    for suffix, share_with in cases:
        pid = await _insert_applied(proposals, f"evol-hist-share-{suffix}")
        await history.insert(
            EvolutionHistory(
                proposal_id=pid,
                kind=EvolutionKind.SKILL_UPDATE,
                target="skills/web_search.md",
                before_sha="aaa",
                after_sha="bbb",
                inverse_diff='{"op":"skill_update"}',
                metrics_baseline={},
                applied_at=3_000,
                share_with=share_with,
            )
        )
        got = await history.latest_for_proposal(pid)
        assert got.share_with == share_with, (
            f"share_with must round-trip for fixture '{suffix}'"
        )


async def test_share_with_corrupt_json_decodes_as_none(store: EvolutionStore) -> None:
    proposals = ProposalsRepo(store.conn)
    history = HistoryRepo(store.conn)
    pid = await _insert_applied(proposals, "evol-hist-share-corrupt")
    await history.insert(
        EvolutionHistory(
            proposal_id=pid,
            kind=EvolutionKind.SKILL_UPDATE,
            target="skills/web_search.md",
            before_sha="aaa",
            after_sha="bbb",
            inverse_diff="{}",
            metrics_baseline={},
            applied_at=3_000,
            share_with=["bravo"],
        )
    )

    # Plant garbage TEXT — bypasses the repo's serialisation path.
    await store.conn.execute(
        "UPDATE evolution_history SET share_with = ? WHERE proposal_id = ?",
        ("not json at all", pid),
    )
    await store.conn.commit()

    got = await history.latest_for_proposal(pid)
    assert got.share_with is None
    assert got.proposal_id == pid
    assert got.kind == EvolutionKind.SKILL_UPDATE


# ---------------------------------------------------------------------------
# Auto-rollback transitions + grace window
# ---------------------------------------------------------------------------


async def test_mark_auto_rolled_back_happy_path(store: EvolutionStore) -> None:
    repo = ProposalsRepo(store.conn)
    pid = await _insert_applied(repo, "evol-ar-001")
    await repo.mark_auto_rolled_back(pid, 5_000, "err_signal_count: 4 -> 12 (+200%)")
    after = await repo.get(pid)
    assert after.status == EvolutionStatus.ROLLED_BACK

    cursor = await store.conn.execute(
        "SELECT auto_rollback_at, auto_rollback_reason FROM evolution_proposals WHERE id = ?",
        (pid,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    assert row is not None
    assert row[0] == 5_000
    assert row[1] == "err_signal_count: 4 -> 12 (+200%)"


async def test_mark_auto_rolled_back_double_call_is_not_found(
    store: EvolutionStore,
) -> None:
    repo = ProposalsRepo(store.conn)
    pid = await _insert_applied(repo, "evol-ar-002")
    await repo.mark_auto_rolled_back(pid, 5_000, "first")
    with pytest.raises(NotFoundError):
        await repo.mark_auto_rolled_back(pid, 6_000, "second")


async def test_mark_auto_rolled_back_rejects_non_applied_status(
    store: EvolutionStore,
) -> None:
    repo = ProposalsRepo(store.conn)
    pid = await _insert_pending(
        repo, "evol-ar-003", EvolutionKind.MEMORY_OP, EvolutionRisk.LOW
    )
    with pytest.raises(NotFoundError):
        await repo.mark_auto_rolled_back(pid, 5_000, "won't take")
    after = await repo.get(pid)
    assert after.status == EvolutionStatus.PENDING


async def test_list_applied_in_grace_window_filters_by_time(
    store: EvolutionStore,
) -> None:
    repo = ProposalsRepo(store.conn)
    now = 100 * 3_600 * 1_000
    in_window = now - 3_600 * 1_000
    too_old = now - 100 * 3_600 * 1_000
    in_future = now + 5 * 60 * 1_000
    await _insert_applied_at(repo, "evol-grace-in", in_window)
    await _insert_applied_at(repo, "evol-grace-old", too_old)
    await _insert_applied_at(repo, "evol-grace-future", in_future)

    hits = await repo.list_applied_in_grace_window(now, 72, 10)
    ids = [str(p.id) for p in hits]
    assert ids == ["evol-grace-in"]


async def test_list_applied_in_grace_window_excludes_rolled_back(
    store: EvolutionStore,
) -> None:
    repo = ProposalsRepo(store.conn)
    now = 100 * 3_600 * 1_000
    applied_at = now - 3_600 * 1_000
    pid_rolled = await _insert_applied_at(repo, "evol-grace-rolled", applied_at)
    await _insert_applied_at(repo, "evol-grace-live", applied_at)
    await repo.mark_auto_rolled_back(pid_rolled, now, "test-rollback")

    hits = await repo.list_applied_in_grace_window(now, 72, 10)
    ids = [str(p.id) for p in hits]
    assert ids == ["evol-grace-live"]


# ---------------------------------------------------------------------------
# ISO week budget gate
# ---------------------------------------------------------------------------


def test_iso_week_window_round_trip() -> None:
    """``iso_week_window`` for a Wednesday must snap back to Monday
    00:00 UTC and forward to the following Monday. Pin against literal
    calendar dates."""
    # 2026-04-29T15:00:00Z — a Wednesday → unix epoch 1_777_474_800s.
    now_ms = 1_777_474_800 * 1_000
    start_ms, end_ms = iso_week_window(now_ms)
    expect_start_ms = 1_777_248_000 * 1_000
    expect_end_ms = 1_777_852_800 * 1_000
    assert start_ms == expect_start_ms
    assert end_ms == expect_end_ms
    assert end_ms - start_ms == 7 * 24 * 3_600 * 1_000


async def test_count_proposals_in_iso_week_filters_kind(store: EvolutionStore) -> None:
    repo = ProposalsRepo(store.conn)
    now_ms = 1_777_474_800 * 1_000
    start_ms, _ = iso_week_window(now_ms)
    in_window = start_ms + 3_600 * 1_000
    ancient = start_ms - 30 * 24 * 3_600 * 1_000

    await _insert_with_created_at(repo, "p-mem-1", EvolutionKind.MEMORY_OP, in_window)
    await _insert_with_created_at(
        repo, "p-mem-2", EvolutionKind.MEMORY_OP, in_window + 60_000
    )
    await _insert_with_created_at(
        repo, "p-skill", EvolutionKind.SKILL_UPDATE, in_window + 120_000
    )
    await _insert_with_created_at(repo, "p-mem-old", EvolutionKind.MEMORY_OP, ancient)

    memory_only = await repo.count_proposals_in_iso_week(now_ms, EvolutionKind.MEMORY_OP)
    assert memory_only == 2

    total = await repo.count_proposals_in_iso_week(now_ms, None)
    assert total == 3


async def test_count_proposals_in_iso_week_includes_rolled_back(
    store: EvolutionStore,
) -> None:
    repo = ProposalsRepo(store.conn)
    now_ms = 1_777_474_800 * 1_000
    start_ms, _ = iso_week_window(now_ms)
    in_window = start_ms + 3_600 * 1_000

    pid = await _insert_with_created_at(
        repo, "p-rolled", EvolutionKind.MEMORY_OP, in_window
    )
    await repo.set_decision(pid, EvolutionStatus.APPROVED, in_window + 1, "op")
    await repo.mark_applied(pid, in_window + 2)
    await repo.mark_auto_rolled_back(pid, in_window + 3, "test")

    count = await repo.count_proposals_in_iso_week(now_ms, EvolutionKind.MEMORY_OP)
    assert count == 1


# ---------------------------------------------------------------------------
# Apply intent log
# ---------------------------------------------------------------------------


async def test_intent_log_record_then_commit_clears_uncommitted(
    store: EvolutionStore,
) -> None:
    repo = IntentLogRepo(store.conn)
    intent_id = await repo.record_intent("evol-int-001", "memory_op", "delete_chunk:42", 1_000)

    before = await repo.list_uncommitted()
    assert len(before) == 1
    assert before[0].id == intent_id
    assert before[0].proposal_id == "evol-int-001"
    assert before[0].kind == "memory_op"
    assert before[0].target == "delete_chunk:42"

    await repo.mark_committed(intent_id, 2_000)
    after = await repo.list_uncommitted()
    assert after == []


async def test_intent_log_record_then_fail_clears_uncommitted(
    store: EvolutionStore,
) -> None:
    repo = IntentLogRepo(store.conn)
    intent_id = await repo.record_intent("evol-int-002", "memory_op", "merge_chunks:1,2", 1_000)
    await repo.mark_failed(intent_id, 2_000, "kb: chunk 2 missing")
    assert await repo.list_uncommitted() == []


async def test_intent_log_uncommitted_preserves_only_in_flight(
    store: EvolutionStore,
) -> None:
    repo = IntentLogRepo(store.conn)
    committed = await repo.record_intent("evol-int-c", "memory_op", "t-c", 1_000)
    failed = await repo.record_intent("evol-int-f", "memory_op", "t-f", 1_500)
    open_id = await repo.record_intent("evol-int-o", "memory_op", "t-o", 2_000)
    await repo.mark_committed(committed, 1_100)
    await repo.mark_failed(failed, 1_600, "test")

    outstanding = await repo.list_uncommitted()
    assert len(outstanding) == 1
    assert outstanding[0].id == open_id
    assert outstanding[0].proposal_id == "evol-int-o"


async def test_intent_log_double_commit_is_idempotent(store: EvolutionStore) -> None:
    repo = IntentLogRepo(store.conn)
    intent_id = await repo.record_intent("evol-int-idem", "memory_op", "t", 1_000)
    await repo.mark_committed(intent_id, 2_000)
    await repo.mark_committed(intent_id, 9_999)

    cursor = await store.conn.execute(
        "SELECT committed_at, failed_at FROM apply_intent_log WHERE id = ?",
        (intent_id,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    assert row is not None
    assert row[0] == 2_000
    assert row[1] is None


# ---------------------------------------------------------------------------
# Enum / kind classification
# ---------------------------------------------------------------------------


def test_kind_serializes_to_snake_case() -> None:
    cases = [
        (EvolutionKind.ENGINE_CONFIG, "engine_config"),
        (EvolutionKind.ENGINE_PROMPT, "engine_prompt"),
        (EvolutionKind.OBSERVER_FILTER, "observer_filter"),
        (EvolutionKind.CLUSTER_THRESHOLD, "cluster_threshold"),
    ]
    for kind, expected in cases:
        assert kind.as_str() == expected
        assert kind.value == expected
        assert EvolutionKind.from_str(expected) == kind


async def test_kind_round_trips_through_repo(store: EvolutionStore) -> None:
    repo = ProposalsRepo(store.conn)
    for kind in (
        EvolutionKind.ENGINE_CONFIG,
        EvolutionKind.ENGINE_PROMPT,
        EvolutionKind.OBSERVER_FILTER,
        EvolutionKind.CLUSTER_THRESHOLD,
    ):
        pid = ProposalId(f"evol-meta-{kind.as_str()}")
        await repo.insert(
            EvolutionProposal(
                id=pid,
                kind=kind,
                target=f"meta-target-{kind.as_str()}",
                diff=f'{{"placeholder":"{kind.as_str()}"}}',
                reasoning="iter 1 round-trip fixture",
                risk=EvolutionRisk.HIGH,
                budget_cost=1,
                status=EvolutionStatus.PENDING,
                created_at=1_000,
            )
        )
        got = await repo.get(pid)
        assert got.kind == kind
        assert got.id == pid
        assert got.kind.is_meta()


def test_is_meta_partition() -> None:
    cases = [
        (EvolutionKind.MEMORY_OP, False),
        (EvolutionKind.TAG_REBALANCE, False),
        (EvolutionKind.RETRY_TUNING, False),
        (EvolutionKind.AGENT_CARD, False),
        (EvolutionKind.SKILL_UPDATE, False),
        (EvolutionKind.PROMPT_TEMPLATE, False),
        (EvolutionKind.TOOL_POLICY, False),
        (EvolutionKind.NEW_SKILL, False),
        (EvolutionKind.ENGINE_CONFIG, True),
        (EvolutionKind.ENGINE_PROMPT, True),
        (EvolutionKind.OBSERVER_FILTER, True),
        (EvolutionKind.CLUSTER_THRESHOLD, True),
    ]
    for kind, expected in cases:
        assert kind.is_meta() == expected, f"is_meta wrong for {kind!r}"


# ---------------------------------------------------------------------------
# Metadata round-trip + corrupt decode
# ---------------------------------------------------------------------------


async def test_metadata_is_none_for_legacy_inserts(store: EvolutionStore) -> None:
    repo = ProposalsRepo(store.conn)
    pid = await _insert_pending(
        repo, "p-meta-legacy", EvolutionKind.MEMORY_OP, EvolutionRisk.LOW
    )
    got = await repo.get(pid)
    assert got.metadata is None


async def test_metadata_round_trips_arbitrary_json(store: EvolutionStore) -> None:
    repo = ProposalsRepo(store.conn)
    pid = ProposalId("p-meta-rt")
    blob = {
        "federated_from": {
            "tenant": "acme",
            "source_proposal_id": "evol-acme-2026-05-01-007",
            "hop": 1,
        },
        "trace_descent": ["t1", "t2"],
    }
    await repo.insert(
        EvolutionProposal(
            id=pid,
            kind=EvolutionKind.MEMORY_OP,
            target="t",
            diff="",
            reasoning="fixture",
            risk=EvolutionRisk.LOW,
            budget_cost=0,
            status=EvolutionStatus.PENDING,
            created_at=1_000,
            metadata=blob,
        )
    )
    got = await repo.get(pid)
    assert got.metadata == blob


async def test_metadata_corrupt_json_decodes_as_none(store: EvolutionStore) -> None:
    repo = ProposalsRepo(store.conn)
    pid = await _insert_pending(
        repo, "p-meta-corrupt", EvolutionKind.MEMORY_OP, EvolutionRisk.LOW
    )
    await store.conn.execute(
        "UPDATE evolution_proposals SET metadata = ? WHERE id = ?",
        ("not json", pid),
    )
    await store.conn.commit()

    got = await repo.get(pid)
    assert got.metadata is None
    assert got.id == pid


# ---------------------------------------------------------------------------
# Meta recursion guard — clause A (descent) + clause B (cooldown)
# ---------------------------------------------------------------------------


async def test_meta_insert_with_no_parent_succeeds(store: EvolutionStore) -> None:
    repo = ProposalsRepo(store.conn).with_guard(EvolutionGuardConfig())
    await repo.insert(
        _meta_proposal(
            "evol-meta-orphan",
            EvolutionKind.ENGINE_PROMPT,
            1_000,
            EvolutionStatus.PENDING,
            None,
            {"trace_descent": ["t1"]},
        )
    )


async def test_meta_insert_rejects_when_parent_is_also_meta(
    store: EvolutionStore,
) -> None:
    guarded = ProposalsRepo(store.conn).with_guard(EvolutionGuardConfig())
    seed = ProposalsRepo(store.conn)
    parent_id = "evol-meta-parent"
    await seed.insert(
        _meta_proposal(
            parent_id,
            EvolutionKind.ENGINE_PROMPT,
            1_000,
            EvolutionStatus.APPLIED,
            2_000,
            None,
        )
    )

    child_created = 1_000 + 7_200_000  # 2h after parent.applied_at — out of cooldown
    child = _meta_proposal(
        "evol-meta-child",
        EvolutionKind.ENGINE_PROMPT,
        child_created,
        EvolutionStatus.PENDING,
        None,
        {"parent_meta_proposal_id": parent_id},
    )
    with pytest.raises(RecursionGuardViolationError) as excinfo:
        await guarded.insert(child)
    assert excinfo.value.parent_id == parent_id
    assert excinfo.value.parent_kind == EvolutionKind.ENGINE_PROMPT


async def test_meta_insert_allows_non_meta_parent(store: EvolutionStore) -> None:
    guarded = ProposalsRepo(store.conn).with_guard(EvolutionGuardConfig())
    seed = ProposalsRepo(store.conn)
    parent_id = "evol-mem-parent"
    await seed.insert(
        EvolutionProposal(
            id=ProposalId(parent_id),
            kind=EvolutionKind.MEMORY_OP,
            target="merge_chunks:1,2",
            diff="",
            reasoning="fixture parent",
            risk=EvolutionRisk.LOW,
            budget_cost=0,
            status=EvolutionStatus.APPLIED,
            created_at=1_000,
            decided_at=1_500,
            decided_by="operator",
            applied_at=2_000,
        )
    )
    child = _meta_proposal(
        "evol-meta-from-mem",
        EvolutionKind.ENGINE_PROMPT,
        10_000_000,
        EvolutionStatus.PENDING,
        None,
        {"parent_meta_proposal_id": parent_id},
    )
    await guarded.insert(child)


async def test_meta_cooldown_rejects_within_window(store: EvolutionStore) -> None:
    seed = ProposalsRepo(store.conn)
    first_applied = 5_000_000
    await seed.insert(
        _meta_proposal(
            "evol-meta-first",
            EvolutionKind.ENGINE_CONFIG,
            first_applied - 1_000,
            EvolutionStatus.APPLIED,
            first_applied,
            None,
        )
    )

    guarded = ProposalsRepo(store.conn).with_guard(EvolutionGuardConfig())
    second_created = first_applied + 1_800_000  # 30 min later
    second = _meta_proposal(
        "evol-meta-second",
        EvolutionKind.ENGINE_CONFIG,
        second_created,
        EvolutionStatus.PENDING,
        None,
        None,
    )
    with pytest.raises(RecursionGuardCooldownError) as excinfo:
        await guarded.insert(second)
    assert excinfo.value.last_applied_at_ms == first_applied
    assert excinfo.value.window_secs == 3_600
    assert 1_799 <= excinfo.value.remaining_secs <= 1_801


async def test_meta_cooldown_allows_after_window(store: EvolutionStore) -> None:
    seed = ProposalsRepo(store.conn)
    first_applied = 10_000_000
    await seed.insert(
        _meta_proposal(
            "evol-meta-old",
            EvolutionKind.CLUSTER_THRESHOLD,
            first_applied - 1_000,
            EvolutionStatus.APPLIED,
            first_applied,
            None,
        )
    )

    # Rewind the prior row's applied_at 2h into the past.
    await store.conn.execute(
        "UPDATE evolution_proposals SET applied_at = applied_at - 7200000 WHERE id = ?",
        ("evol-meta-old",),
    )
    await store.conn.commit()

    guarded = ProposalsRepo(store.conn).with_guard(EvolutionGuardConfig())
    await guarded.insert(
        _meta_proposal(
            "evol-meta-new",
            EvolutionKind.CLUSTER_THRESHOLD,
            first_applied,
            EvolutionStatus.PENDING,
            None,
            None,
        )
    )


async def test_meta_cooldown_per_tenant_independent(store: EvolutionStore) -> None:
    seed = ProposalsRepo(store.conn)
    first_applied = 50_000_000
    await seed.insert(
        _meta_proposal(
            "evol-meta-tenantA",
            EvolutionKind.OBSERVER_FILTER,
            first_applied - 1_000,
            EvolutionStatus.APPLIED,
            first_applied,
            None,
        )
    )
    # Move tenant A's row out of the default bucket.
    await store.conn.execute(
        "UPDATE evolution_proposals SET tenant_id = ? WHERE id = ?",
        ("tenant-a", "evol-meta-tenantA"),
    )
    await store.conn.commit()

    guarded = ProposalsRepo(store.conn).with_guard(EvolutionGuardConfig())
    cross_tenant = _meta_proposal(
        "evol-meta-tenantB",
        EvolutionKind.OBSERVER_FILTER,
        first_applied + 60_000,  # 1m after — would fail same tenant
        EvolutionStatus.PENDING,
        None,
        None,
    )
    await guarded.insert(cross_tenant)


async def test_non_meta_kinds_skip_guard_entirely(store: EvolutionStore) -> None:
    guarded = ProposalsRepo(store.conn).with_guard(EvolutionGuardConfig())
    for i in range(100):
        pid = ProposalId(f"evol-mem-burst-{i:03d}")
        await guarded.insert(
            EvolutionProposal(
                id=pid,
                kind=EvolutionKind.MEMORY_OP,
                target="merge_chunks:1,2",
                diff="",
                reasoning="burst",
                risk=EvolutionRisk.LOW,
                budget_cost=0,
                status=EvolutionStatus.PENDING,
                created_at=1_000,
            )
        )
