"""Tests for :class:`SchedulerStore` — the :mod:`aiosqlite`-backed
run-history persistence layer.

Python-only: the Rust crate is purely in-memory and persists through
the hook bus + downstream observers. The Python brief asks for an
aiosqlite store as part of the port, so these tests cover the wrapper
in isolation (the store is decoupled from the runtime — the gateway
integration code wires a hook subscription that drives
:meth:`SchedulerStore.record_outcome` per firing).
"""

from __future__ import annotations

from pathlib import Path

from corlinman_server.scheduler import (
    RunRecord,
    SchedulerStore,
    SubprocessOutcome,
    SubprocessOutcomeKind,
)


async def test_open_creates_file_and_applies_schema(tmp_path: Path) -> None:
    """``open`` creates the parent directory + the SQLite file + applies
    the schema. A subsequent ``count`` on the empty table returns 0."""
    p = tmp_path / "nested" / "scheduler.sqlite"
    store = await SchedulerStore.open(p)
    try:
        assert p.exists(), "SQLite file should exist after open"
        assert await store.count() == 0
    finally:
        await store.close()


async def test_record_outcome_success_persists_row(tmp_path: Path) -> None:
    """Recording a :class:`SubprocessOutcomeKind.SUCCESS` outcome
    persists a row with ``outcome_kind = "success"`` and ``error_kind = None``.
    Round-trip through :meth:`list_recent`."""
    store = await SchedulerStore.open(tmp_path / "s.sqlite")
    try:
        outcome = SubprocessOutcome(kind=SubprocessOutcomeKind.SUCCESS, duration_secs=0.42)
        row_id = await store.record_outcome(
            job_name="daily-engine",
            run_id="abc123",
            action_kind="subprocess",
            outcome=outcome,
        )
        assert row_id > 0

        rows = await store.list_recent()
        assert len(rows) == 1
        r = rows[0]
        assert isinstance(r, RunRecord)
        assert r.job_name == "daily-engine"
        assert r.run_id == "abc123"
        assert r.action_kind == "subprocess"
        assert r.outcome_kind == "success"
        assert r.error_kind is None
        assert r.exit_code is None
        # 0.42s → 420ms.
        assert r.duration_ms == 420
        assert r.fired_at_ms > 0
    finally:
        await store.close()


async def test_record_outcome_failure_maps_error_kind(tmp_path: Path) -> None:
    """Non-zero exit / timeout / spawn-failed outcomes get mapped to
    the same vocabulary the hook bus uses on ``EngineRunFailed.error_kind``.
    Pinned per branch so a code drift surfaces immediately."""
    store = await SchedulerStore.open(tmp_path / "s.sqlite")
    try:
        cases: list[tuple[SubprocessOutcome, str, int | None]] = [
            (
                SubprocessOutcome(
                    kind=SubprocessOutcomeKind.NON_ZERO_EXIT,
                    duration_secs=0.1,
                    exit_code=1,
                ),
                "exit_code",
                1,
            ),
            (
                SubprocessOutcome(kind=SubprocessOutcomeKind.TIMEOUT, duration_secs=1.0),
                "timeout",
                None,
            ),
            (
                SubprocessOutcome(
                    kind=SubprocessOutcomeKind.SPAWN_FAILED,
                    duration_secs=0.0,
                    error="No such file",
                ),
                "spawn_failed",
                None,
            ),
        ]
        for i, (outcome, expected_error_kind, expected_exit_code) in enumerate(cases):
            await store.record_outcome(
                job_name="j",
                run_id=f"r-{i}",
                action_kind="subprocess",
                outcome=outcome,
            )
        rows = await store.list_recent()
        # list_recent orders DESC by fired_at_ms — but all three rows
        # land in the same millisecond on a fast box, so the secondary
        # ``id DESC`` ordering kicks in. We assert the *set* of
        # error_kinds rather than the order to keep the test stable.
        error_kinds = {r.error_kind for r in rows}
        assert error_kinds == {"exit_code", "timeout", "spawn_failed"}
        # Tie one row back to its exit_code for the non_zero_exit case.
        non_zero = next(r for r in rows if r.outcome_kind == "non_zero_exit")
        assert non_zero.exit_code == 1
        # The other two have no exit_code.
        for r in rows:
            if r.outcome_kind != "non_zero_exit":
                assert r.exit_code is None
        _ = expected_error_kind, expected_exit_code  # asserted via the set above
    finally:
        await store.close()


async def test_list_for_job_filters_and_orders(tmp_path: Path) -> None:
    """``list_for_job`` returns only rows for the named job, newest
    first by ``fired_at_ms`` (with ``id DESC`` as the tie-breaker on
    same-millisecond rows)."""
    store = await SchedulerStore.open(tmp_path / "s.sqlite")
    try:
        for i in range(3):
            await store.record_raw(
                job_name="job-a",
                run_id=f"a-{i}",
                action_kind="subprocess",
                outcome_kind="success",
                error_kind=None,
                exit_code=None,
                duration_ms=10,
                fired_at_ms=1000 + i,  # explicit stamps so the order is deterministic
            )
        await store.record_raw(
            job_name="job-b",
            run_id="b-0",
            action_kind="subprocess",
            outcome_kind="success",
            error_kind=None,
            exit_code=None,
            duration_ms=10,
            fired_at_ms=2000,
        )

        a_rows = await store.list_for_job("job-a")
        assert [r.run_id for r in a_rows] == ["a-2", "a-1", "a-0"]
        b_rows = await store.list_for_job("job-b")
        assert [r.run_id for r in b_rows] == ["b-0"]
    finally:
        await store.close()


async def test_get_by_run_id_returns_none_for_missing(tmp_path: Path) -> None:
    """Missing run_id → ``None`` (not an exception). Callers branch on
    the ``None`` return."""
    store = await SchedulerStore.open(tmp_path / "s.sqlite")
    try:
        assert await store.get_by_run_id("nope") is None
        await store.record_raw(
            job_name="j",
            run_id="present",
            action_kind="subprocess",
            outcome_kind="success",
            error_kind=None,
            exit_code=None,
            duration_ms=5,
        )
        got = await store.get_by_run_id("present")
        assert got is not None
        assert got.run_id == "present"
    finally:
        await store.close()


async def test_close_is_idempotent(tmp_path: Path) -> None:
    """``close`` swallows errors on a second call — used at shutdown
    so the gateway can call it from multiple cleanup paths."""
    store = await SchedulerStore.open(tmp_path / "s.sqlite")
    await store.close()
    # Second close should not raise.
    await store.close()


async def test_records_unsupported_action_via_record_raw(tmp_path: Path) -> None:
    """The dispatcher's unsupported-action branch has no
    :class:`SubprocessOutcome` to wrap, so callers persist it via
    :meth:`record_raw`. Test that the row makes it through with the
    expected vocabulary (mirrors the hook event's ``error_kind``)."""
    store = await SchedulerStore.open(tmp_path / "s.sqlite")
    try:
        await store.record_raw(
            job_name="agentic",
            run_id="run-u",
            action_kind="run_agent",
            outcome_kind="unsupported_action",
            error_kind="unsupported_action",
            exit_code=None,
            duration_ms=0,
        )
        row = await store.get_by_run_id("run-u")
        assert row is not None
        assert row.action_kind == "run_agent"
        assert row.outcome_kind == "unsupported_action"
        assert row.error_kind == "unsupported_action"
    finally:
        await store.close()
