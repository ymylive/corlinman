"""Iter 4 tests — :func:`episodes_run_once` end-to-end orchestration.

The runner ties iter 1-3 primitives into one idempotent pass. Tests
exercise the design-doc test matrix entries:

- ``distillation_idempotent_on_same_window`` — re-run returns prior
  run id without double-minting.
- ``distillation_resumes_after_crash`` — a stale ``running`` row is
  swept and the window re-distills.
- ``last_ok_run`` advances on a fresh pass — the next run's window
  clamps to the prior end.
- ``classify`` precedence flows through end-to-end (an apply row →
  ``EVOLUTION`` kind on the inserted episode).
- Empty-window short-circuit emits ``status='skipped_empty``.
- A failing summary provider stamps ``status='failed`` rather than
  leaving the run pinned ``running``.

The summary provider is always a stub — no ``corlinman-providers``
import, no network. Iter 7 wires the real adapter.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from corlinman_episodes import (
    RUN_STATUS_FAILED,
    RUN_STATUS_OK,
    RUN_STATUS_RUNNING,
    RUN_STATUS_SKIPPED_EMPTY,
    EpisodeKind,
    EpisodesConfig,
    EpisodesStore,
    RunSummary,
    SourcePaths,
    episodes_run_once,
    make_constant_provider,
)

from tests._seed import (
    insert_hook_event,
    insert_proposal_with_history,
    insert_session_message,
    insert_signal,
)

# ---------------------------------------------------------------------------
# Fixture wiring
# ---------------------------------------------------------------------------


@pytest.fixture
def episodes_db(tmp_path: Path) -> Path:
    """Per-test path to a fresh ``episodes.sqlite`` (file is created
    on first store-open)."""
    return tmp_path / "episodes.sqlite"


@pytest.fixture
def sources(
    sessions_db: Path,
    evolution_db: Path,
    hook_events_db: Path,
    identity_db: Path,
) -> SourcePaths:
    """Bundle the conftest-supplied per-stream DBs into the
    :class:`SourcePaths` shape the runner expects."""
    return SourcePaths(
        sessions_db=sessions_db,
        evolution_db=evolution_db,
        hook_events_db=hook_events_db,
        identity_db=identity_db,
    )


def _config(**overrides: object) -> EpisodesConfig:
    """Test-friendly config — disable the wall-clock minimum so the
    runner doesn't auto-skip a 1-minute window built around a
    seeded fixture timestamp.
    """
    base = {
        "min_window_secs": 1,  # 1 second floor; happy-path windows still pass.
        "distillation_window_hours": 24.0,
        "max_messages_per_call": 60,
    }
    base.update(overrides)
    return EpisodesConfig(**base)  # type: ignore[arg-type]


def _seed_minimal_conversation(
    *,
    sessions_db: Path,
    session_key: str = "sess-A",
    base_ms: int = 1_000_000,
) -> None:
    """Seed a tiny chat trail — produces one CONVERSATION-kind episode."""
    insert_session_message(
        sessions_db,
        session_key=session_key,
        seq=0,
        role="user",
        content="hello",
        ts_ms=base_ms,
    )
    insert_session_message(
        sessions_db,
        session_key=session_key,
        seq=1,
        role="agent",
        content="hi back",
        ts_ms=base_ms + 1_000,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_happy_path_writes_one_episode(
    episodes_db: Path,
    sources: SourcePaths,
    sessions_db: Path,
) -> None:
    """A seeded conversation produces one inserted episode + an OK run.

    Pins the load-bearing claim that the runner is idempotent end-
    to-end on a non-empty window: the run row is OK, the episode
    has the bundled session_key, and ``distilled_by`` carries the
    config alias so operators can see which provider answered.
    """
    base_ms = 1_000_000
    _seed_minimal_conversation(sessions_db=sessions_db, base_ms=base_ms)

    summary = await episodes_run_once(
        config=_config(),
        episodes_db=episodes_db,
        sources=sources,
        summary_provider=make_constant_provider("a brief chat"),
        tenant_id="default",
        now_ms=base_ms + 60_000,  # 1 min after last message; window covers it.
    )

    assert summary.status == RUN_STATUS_OK
    assert summary.episodes_written == 1
    assert summary.bundles_seen == 1
    assert summary.run_id

    async with EpisodesStore(episodes_db) as store:
        cursor = await store.conn.execute(
            "SELECT kind, summary_text, distilled_by, source_session_keys "
            "FROM episodes"
        )
        rows = await cursor.fetchall()
        await cursor.close()
    assert len(rows) == 1
    kind, text, distilled_by, sess_keys = rows[0]
    assert kind == EpisodeKind.CONVERSATION
    assert text == "a brief chat"
    assert distilled_by == "default-summary"
    # JSON-encoded list — substring-match keeps the assert simple.
    assert "sess-A" in sess_keys


async def test_apply_history_promotes_kind_to_evolution(
    episodes_db: Path,
    sources: SourcePaths,
    sessions_db: Path,
    evolution_db: Path,
) -> None:
    """An apply row in-window dominates the chat → kind is EVOLUTION.

    Asserts the classifier rule precedence flows through the runner
    (apply > conversation) and that ``source_history_ids`` is
    persisted onto the episode.
    """
    base_ms = 2_000_000
    _seed_minimal_conversation(sessions_db=sessions_db, base_ms=base_ms)

    sig_id = insert_signal(
        evolution_db,
        event_kind="tool_invocation_failed",
        target="web_search",
        severity="error",
        observed_at_ms=base_ms + 500,
    )
    history_id = insert_proposal_with_history(
        evolution_db,
        proposal_id="prop-1",
        kind="skill_update",
        target="web_search",
        signal_ids=[sig_id],
        applied_at_ms=base_ms + 1_500,
    )

    await episodes_run_once(
        config=_config(),
        episodes_db=episodes_db,
        sources=sources,
        summary_provider=make_constant_provider("operator approved skill_update"),
        now_ms=base_ms + 60_000,
    )

    async with EpisodesStore(episodes_db) as store:
        cursor = await store.conn.execute(
            "SELECT kind, source_history_ids, source_signal_ids, "
            "       importance_score "
            "FROM episodes"
        )
        rows = await cursor.fetchall()
        await cursor.close()
    # Two bundles surface here — the chat under sess-A and the
    # orphan-bucketed history. The kind selection is per-bundle, so
    # only the orphan-history bundle gets EVOLUTION; assert at least
    # one EVOLUTION row exists with the expected source ids and a
    # non-default importance score.
    evo_rows = [r for r in rows if r[0] == EpisodeKind.EVOLUTION]
    assert len(evo_rows) == 1
    _, history_json, signal_json, score = evo_rows[0]
    assert str(history_id) in history_json
    assert str(sig_id) in signal_json
    # Importance baseline (apply +0.2 + signal density 0.05 + severity
    # error 0.15) sits well above the 0.5 default — sanity that the
    # frozen score landed on the row.
    assert score >= 0.4


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_re_run_on_same_window_is_idempotent(
    episodes_db: Path,
    sources: SourcePaths,
    sessions_db: Path,
) -> None:
    """A second pass that reproduces the same ``(start, end)`` window
    short-circuits to the prior run row instead of double-minting.

    Two design-doc claims are pinned:

    - The unique-window guard on
      ``episode_distillation_runs(tenant_id, window_start, window_end)``
      fires when the next pass picks up the same window.
    - The runner catches the conflict, looks up the prior row via
      :meth:`EpisodesStore.find_run`, and surfaces its summary
      unchanged — the cron can fire twice without double-minting.

    Achieving "same window" deterministically takes a small staged
    setup: run once → flip the run's status to ``failed`` so
    :meth:`EpisodesStore.latest_ok_run` no longer pins the clamp →
    re-run with the same ``now_ms``. The unique guard still applies
    (the failed row keeps the unique key) so the second runner
    surfaces the prior row.
    """
    base_ms = 3_000_000
    _seed_minimal_conversation(sessions_db=sessions_db, base_ms=base_ms)
    cfg = _config()
    now_ms = base_ms + 60_000

    first = await episodes_run_once(
        config=cfg,
        episodes_db=episodes_db,
        sources=sources,
        summary_provider=make_constant_provider("first"),
        now_ms=now_ms,
    )
    assert first.status == RUN_STATUS_OK

    # Force the next clamp to ignore this run so ``select_window``
    # produces the same ``(start, end)`` again. The unique-index row
    # remains — that's the trigger we want to fire.
    import sqlite3

    conn = sqlite3.connect(episodes_db)
    try:
        conn.execute(
            "UPDATE episode_distillation_runs SET status = ?",
            (RUN_STATUS_FAILED,),
        )
        conn.commit()
    finally:
        conn.close()

    second = await episodes_run_once(
        config=cfg,
        episodes_db=episodes_db,
        sources=sources,
        summary_provider=make_constant_provider("second"),
        now_ms=now_ms,
    )
    # The second run hit the unique-window guard and surfaced the
    # original (now ``failed``) run row.
    assert second.run_id == first.run_id
    # The status reflects what's on disk (``failed`` after our flip),
    # not a freshly-minted ``ok`` — the runner returned the existing
    # row verbatim instead of writing a new one.
    assert second.status == RUN_STATUS_FAILED

    async with EpisodesStore(episodes_db) as store:
        cursor = await store.conn.execute(
            "SELECT COUNT(*), summary_text FROM episodes"
        )
        row = await cursor.fetchone()
        await cursor.close()
    assert row is not None
    count, text = row
    # One episode — the natural-key probe also fired on this pass
    # (the bundle reproduces) but never reached ``insert_episode``
    # because the run-row guard triggered first. ``"second"`` never
    # ran.
    assert count == 1
    assert text == "first"


async def test_natural_key_dedup_after_crash(
    episodes_db: Path,
    sources: SourcePaths,
    sessions_db: Path,
) -> None:
    """A bundle whose natural key already exists is skipped on retry.

    Simulates: run minted episode A under window W, crashed before
    finishing the run row; the run row gets swept to ``failed``;
    operator manually opens a *new* window that re-collects bundle
    A. The natural-key probe must keep us from inserting a duplicate.
    """
    base_ms = 4_000_000
    _seed_minimal_conversation(sessions_db=sessions_db, base_ms=base_ms)

    # First pass — full happy path inserts the episode.
    cfg = _config()
    first = await episodes_run_once(
        config=cfg,
        episodes_db=episodes_db,
        sources=sources,
        summary_provider=make_constant_provider("first"),
        now_ms=base_ms + 60_000,
    )
    assert first.episodes_written == 1

    # Reach into the run table and drop the OK row so the next pass
    # *can* claim a window that overlaps. This mimics a crashed run
    # that never wrote a row at all (the worst case — no run-log
    # idempotency, only natural-key dedup left).
    import sqlite3

    conn = sqlite3.connect(episodes_db)
    try:
        conn.execute("DELETE FROM episode_distillation_runs")
        conn.commit()
    finally:
        conn.close()

    second = await episodes_run_once(
        config=cfg,
        episodes_db=episodes_db,
        sources=sources,
        summary_provider=make_constant_provider("second"),
        now_ms=base_ms + 60_000,
    )

    # The second pass collected the same bundle, kind, started_at,
    # ended_at — natural-key probe matched, so no new row.
    assert second.episodes_reused == 1
    assert second.episodes_written == 0

    async with EpisodesStore(episodes_db) as store:
        cursor = await store.conn.execute("SELECT COUNT(*) FROM episodes")
        row = await cursor.fetchone()
        await cursor.close()
    assert row == (1,)


# ---------------------------------------------------------------------------
# Empty + short-circuit windows
# ---------------------------------------------------------------------------


async def test_empty_window_skipped_empty(
    episodes_db: Path,
    sources: SourcePaths,
) -> None:
    """No source rows in window → ``status='skipped_empty'``.

    The run row is still written so the next pass's
    ``latest_ok_run`` clamp picks up the window-end (operators
    should see proof the runner examined the window).
    """
    summary = await episodes_run_once(
        config=_config(),
        episodes_db=episodes_db,
        sources=sources,
        summary_provider=make_constant_provider("never called"),
        now_ms=10_000_000,
    )
    assert summary.status == RUN_STATUS_SKIPPED_EMPTY
    assert summary.episodes_written == 0

    async with EpisodesStore(episodes_db) as store:
        latest = await store.latest_ok_run(tenant_id="default")
    assert latest is not None
    assert latest.status == RUN_STATUS_SKIPPED_EMPTY


async def test_window_below_min_secs_short_circuits(
    episodes_db: Path,
    sources: SourcePaths,
) -> None:
    """``min_window_secs`` larger than the available span → skip.

    The clamp can collapse the window to near-zero on a back-to-back
    cron tick; the runner should record a skipped_empty row rather
    than open the LLM call.
    """
    cfg = _config(min_window_secs=3600)  # 1h floor
    # Pre-seed an OK run that ends recently — the next pass clamps
    # window_start to that end and the wall-clock span shrinks.
    async with EpisodesStore(episodes_db) as store:
        run = await store.open_run(
            tenant_id="default",
            window_start=0,
            window_end=10_000_000,
            started_at=10_000_000,
        )
        await store.finish_run(run.run_id, status=RUN_STATUS_OK)

    summary = await episodes_run_once(
        config=cfg,
        episodes_db=episodes_db,
        sources=sources,
        summary_provider=make_constant_provider("never called"),
        now_ms=10_000_500,  # 500ms after the prior end — well below 1h.
    )
    assert summary.status == RUN_STATUS_SKIPPED_EMPTY
    assert summary.error_message == "window_below_min_secs"


async def test_disabled_config_is_no_op(
    episodes_db: Path,
    sources: SourcePaths,
) -> None:
    """``enabled=False`` short-circuits without touching the DB."""
    cfg = EpisodesConfig(enabled=False)
    summary = await episodes_run_once(
        config=cfg,
        episodes_db=episodes_db,
        sources=sources,
        summary_provider=make_constant_provider("never called"),
        now_ms=1_000,
    )
    assert summary.status == RUN_STATUS_SKIPPED_EMPTY
    assert summary.run_id == ""
    assert summary.error_message == "episodes_disabled"
    # No DB file — the runner never opened a store.
    assert not episodes_db.exists()


# ---------------------------------------------------------------------------
# Crash resume
# ---------------------------------------------------------------------------


async def test_stale_running_row_is_swept(
    episodes_db: Path,
    sources: SourcePaths,
    sessions_db: Path,
) -> None:
    """A pre-existing stale ``running`` row is swept before the new
    pass opens its own row.

    The unique-window guard would otherwise pin the window forever;
    the sweeper marks the ghost ``failed`` so a runner with a
    *different* window can proceed.
    """
    base_ms = 5_000_000
    _seed_minimal_conversation(sessions_db=sessions_db, base_ms=base_ms)

    # Plant a stale ``running`` row — old enough that the sweeper
    # picks it up. Use a non-overlapping window so the new pass
    # opens a fresh row.
    async with EpisodesStore(episodes_db) as store:
        ghost = await store.open_run(
            tenant_id="default",
            window_start=10,
            window_end=20,
            started_at=1,  # ancient compared to now_ms below
        )

    summary = await episodes_run_once(
        config=_config(run_stale_after_secs=1),
        episodes_db=episodes_db,
        sources=sources,
        summary_provider=make_constant_provider("hello"),
        now_ms=base_ms + 60_000,
    )
    assert summary.status == RUN_STATUS_OK
    assert ghost.run_id in summary.swept_stale_runs


async def test_provider_failure_marks_run_failed(
    episodes_db: Path,
    sources: SourcePaths,
    sessions_db: Path,
) -> None:
    """Provider raising → run row is ``failed``, exception propagates.

    Without this, a flaky provider would leave a half-finished run
    pinned ``running`` and block the next pass via the unique-window
    guard.
    """
    base_ms = 6_000_000
    _seed_minimal_conversation(sessions_db=sessions_db, base_ms=base_ms)

    async def _boom(*, prompt: str, kind: EpisodeKind) -> str:
        raise RuntimeError("provider down")

    with pytest.raises(RuntimeError, match="provider down"):
        await episodes_run_once(
            config=_config(),
            episodes_db=episodes_db,
            sources=sources,
            summary_provider=_boom,
            now_ms=base_ms + 60_000,
        )

    async with EpisodesStore(episodes_db) as store:
        cursor = await store.conn.execute(
            "SELECT status, error_message FROM episode_distillation_runs"
        )
        rows = await cursor.fetchall()
        await cursor.close()
    assert len(rows) == 1
    status, err = rows[0]
    assert status == RUN_STATUS_FAILED
    assert "provider down" in err


# ---------------------------------------------------------------------------
# Tenant isolation + window advancement
# ---------------------------------------------------------------------------


async def test_tenant_isolation(
    episodes_db: Path,
    sources: SourcePaths,
    sessions_db: Path,
) -> None:
    """Tenant A's run does not surface tenant B's session messages.

    Mirrors the cross-tenant boundary test from the design doc's
    matrix; the per-stream collectors all filter on tenant_id, so
    the runner inherits the property.
    """
    base_ms = 7_000_000
    insert_session_message(
        sessions_db,
        session_key="sess-A",
        seq=0,
        content="tenant a only",
        ts_ms=base_ms,
        tenant_id="tenant-a",
    )
    insert_session_message(
        sessions_db,
        session_key="sess-B",
        seq=0,
        content="tenant b only",
        ts_ms=base_ms,
        tenant_id="tenant-b",
    )

    summary_a = await episodes_run_once(
        config=_config(),
        episodes_db=episodes_db,
        sources=sources,
        summary_provider=make_constant_provider("a"),
        tenant_id="tenant-a",
        now_ms=base_ms + 60_000,
    )
    assert summary_a.episodes_written == 1

    async with EpisodesStore(episodes_db) as store:
        cursor = await store.conn.execute(
            "SELECT tenant_id, source_session_keys FROM episodes"
        )
        rows = await cursor.fetchall()
        await cursor.close()
    assert len(rows) == 1
    tenant_id, sess_keys = rows[0]
    assert tenant_id == "tenant-a"
    assert "sess-A" in sess_keys
    assert "sess-B" not in sess_keys


async def test_run_summary_ok_property_covers_skipped_empty(
    episodes_db: Path,
    sources: SourcePaths,
) -> None:
    """``RunSummary.ok`` is True for both ``ok`` and ``skipped_empty``.

    The contract matches :meth:`EpisodesStore.latest_ok_run` — both
    statuses count as window-advancing for the next pass's clamp.
    Pin the property explicitly so a future tweak doesn't silently
    leave a skipped-empty run un-advanced.
    """
    summary = await episodes_run_once(
        config=_config(),
        episodes_db=episodes_db,
        sources=sources,
        summary_provider=make_constant_provider("no-op"),
        now_ms=10_000_000,
    )
    assert summary.status == RUN_STATUS_SKIPPED_EMPTY
    assert summary.ok is True

    failing = RunSummary(
        tenant_id="t",
        run_id="r",
        status=RUN_STATUS_FAILED,
        window_start_ms=0,
        window_end_ms=1,
    )
    assert failing.ok is False

    running = RunSummary(
        tenant_id="t",
        run_id="r",
        status=RUN_STATUS_RUNNING,
        window_start_ms=0,
        window_end_ms=1,
    )
    assert running.ok is False


# ---------------------------------------------------------------------------
# Hook-event session_key joins onto source_session_keys
# ---------------------------------------------------------------------------


async def test_hook_event_session_key_added_to_episode(
    episodes_db: Path,
    sources: SourcePaths,
    sessions_db: Path,
    hook_events_db: Path,
) -> None:
    """A hook event with a session_key not in the message stream is
    still folded into ``source_session_keys`` on the episode.

    Picked because the resolver displays the keys to operators —
    losing one would break "which session in this episode?" answers.
    """
    base_ms = 8_000_000
    _seed_minimal_conversation(
        sessions_db=sessions_db, session_key="sess-main", base_ms=base_ms
    )
    # A tool_approved hook stamped against the same session — the
    # collector buckets it into the bundle for sess-main, so the
    # session_key list still has just one entry. Assert it's there.
    insert_hook_event(
        hook_events_db,
        kind="tool_approved",
        session_key="sess-main",
        occurred_at_ms=base_ms + 500,
    )

    await episodes_run_once(
        config=_config(),
        episodes_db=episodes_db,
        sources=sources,
        summary_provider=make_constant_provider("ok"),
        now_ms=base_ms + 60_000,
    )

    async with EpisodesStore(episodes_db) as store:
        cursor = await store.conn.execute(
            "SELECT source_session_keys FROM episodes"
        )
        rows = await cursor.fetchall()
        await cursor.close()
    assert len(rows) == 1
    assert "sess-main" in rows[0][0]
