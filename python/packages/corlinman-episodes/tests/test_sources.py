"""Iter 2 tests — multi-stream source-event gathering.

Covers the sessions / signals / history / hooks / identity join,
tenant scoping, the half-open window contract, and the
"missing peer DB → no rows" graceful-degrade path.
"""

from __future__ import annotations

from pathlib import Path

from corlinman_episodes import (
    SourcePaths,
    collect_bundles,
)

from ._seed import (
    insert_hook_event,
    insert_identity_merge,
    insert_proposal_with_history,
    insert_session_message,
    insert_signal,
)


def _paths(
    sessions_db: Path,
    evolution_db: Path,
    hook_events_db: Path,
    identity_db: Path | None = None,
) -> SourcePaths:
    """Convenience wrapper so tests don't repeat the kwarg list."""
    return SourcePaths(
        sessions_db=sessions_db,
        evolution_db=evolution_db,
        hook_events_db=hook_events_db,
        identity_db=identity_db,
    )


def test_empty_window_returns_no_bundles(
    sessions_db: Path,
    evolution_db: Path,
    hook_events_db: Path,
) -> None:
    """A window with zero rows in every stream → empty list.

    The runner uses this to short-circuit to ``status=skipped_empty``
    without burning an LLM call.
    """
    bundles = collect_bundles(
        paths=_paths(sessions_db, evolution_db, hook_events_db),
        tenant_id="default",
        window_start_ms=0,
        window_end_ms=10_000,
    )
    assert bundles == []


def test_single_session_window_buckets_messages(
    sessions_db: Path,
    evolution_db: Path,
    hook_events_db: Path,
) -> None:
    """Two messages on the same key → one bundle."""
    insert_session_message(
        sessions_db,
        session_key="sess-A",
        seq=0,
        role="user",
        content="hello",
        ts_ms=2_000,
    )
    insert_session_message(
        sessions_db,
        session_key="sess-A",
        seq=1,
        role="assistant",
        content="hi",
        ts_ms=2_500,
    )

    bundles = collect_bundles(
        paths=_paths(sessions_db, evolution_db, hook_events_db),
        tenant_id="default",
        window_start_ms=0,
        window_end_ms=10_000,
    )
    assert len(bundles) == 1
    bundle = bundles[0]
    assert bundle.session_key == "sess-A"
    assert [m.seq for m in bundle.messages] == [0, 1]
    assert bundle.started_at == 2_000
    assert bundle.ended_at == 2_500
    assert not bundle.is_empty()


def test_window_is_half_open(
    sessions_db: Path,
    evolution_db: Path,
    hook_events_db: Path,
) -> None:
    """``window_end_ms`` is exclusive — message at exactly that ts
    rolls to the next pass."""
    insert_session_message(
        sessions_db,
        session_key="sess-A",
        seq=0,
        role="user",
        content="just-in",
        ts_ms=999,
    )
    insert_session_message(
        sessions_db,
        session_key="sess-A",
        seq=1,
        role="user",
        content="boundary",
        ts_ms=1_000,
    )
    bundles = collect_bundles(
        paths=_paths(sessions_db, evolution_db, hook_events_db),
        tenant_id="default",
        window_start_ms=0,
        window_end_ms=1_000,
    )
    assert len(bundles) == 1
    contents = {m.content for m in bundles[0].messages}
    assert contents == {"just-in"}


def test_tenant_isolation_drops_other_tenants(
    sessions_db: Path,
    evolution_db: Path,
    hook_events_db: Path,
) -> None:
    """Tenant A's messages never leak into tenant B's bundles.

    Pinned because cross-tenant leakage would defeat the per-tenant
    ``episodes.sqlite`` isolation guarantee in the design doc.
    """
    insert_session_message(
        sessions_db,
        session_key="sess-A",
        seq=0,
        ts_ms=100,
        tenant_id="alpha",
    )
    insert_session_message(
        sessions_db,
        session_key="sess-B",
        seq=0,
        ts_ms=100,
        tenant_id="beta",
    )

    alpha_bundles = collect_bundles(
        paths=_paths(sessions_db, evolution_db, hook_events_db),
        tenant_id="alpha",
        window_start_ms=0,
        window_end_ms=1_000,
    )
    beta_bundles = collect_bundles(
        paths=_paths(sessions_db, evolution_db, hook_events_db),
        tenant_id="beta",
        window_start_ms=0,
        window_end_ms=1_000,
    )

    assert {b.session_key for b in alpha_bundles} == {"sess-A"}
    assert {b.session_key for b in beta_bundles} == {"sess-B"}


def test_signals_group_by_session_id_and_orphans_collapse(
    sessions_db: Path,
    evolution_db: Path,
    hook_events_db: Path,
) -> None:
    """Signals carrying ``session_id`` join into the matching bundle;
    orphan signals (``session_id=NULL``) collapse into one bundle.
    """
    insert_session_message(
        sessions_db,
        session_key="sess-A",
        seq=0,
        ts_ms=100,
    )
    insert_signal(
        evolution_db,
        event_kind="tool.timeout",
        session_id="sess-A",
        observed_at_ms=200,
    )
    insert_signal(
        evolution_db,
        event_kind="cron.dust",
        session_id=None,
        observed_at_ms=300,
    )
    insert_signal(
        evolution_db,
        event_kind="cron.cluster",
        session_id=None,
        observed_at_ms=400,
    )

    bundles = collect_bundles(
        paths=_paths(sessions_db, evolution_db, hook_events_db),
        tenant_id="default",
        window_start_ms=0,
        window_end_ms=1_000,
    )

    by_key = {b.session_key: b for b in bundles}
    assert "sess-A" in by_key
    assert None in by_key
    assert {s.event_kind for s in by_key["sess-A"].signals} == {"tool.timeout"}
    assert {s.event_kind for s in by_key[None].signals} == {
        "cron.dust",
        "cron.cluster",
    }


def test_history_join_pulls_signal_ids(
    sessions_db: Path,
    evolution_db: Path,
    hook_events_db: Path,
) -> None:
    """``HistoryRow.signal_ids`` reflects the proposal's JSON list.

    The importance scorer credits applies that fired against many
    signals — that join must survive the trip through the collector.
    """
    sig_a = insert_signal(
        evolution_db,
        event_kind="tool.timeout",
        observed_at_ms=10,
    )
    sig_b = insert_signal(
        evolution_db,
        event_kind="tool.timeout",
        observed_at_ms=20,
    )
    insert_proposal_with_history(
        evolution_db,
        proposal_id="prop-1",
        kind="skill_update",
        target="web_search",
        signal_ids=[sig_a, sig_b],
        applied_at_ms=500,
    )

    bundles = collect_bundles(
        paths=_paths(sessions_db, evolution_db, hook_events_db),
        tenant_id="default",
        window_start_ms=0,
        window_end_ms=1_000,
    )
    # History rows orphan-bucket on ``session_key=None``.
    orphan = next(b for b in bundles if b.session_key is None)
    assert len(orphan.history) == 1
    h = orphan.history[0]
    assert h.target == "web_search"
    assert sorted(h.signal_ids) == sorted([sig_a, sig_b])


def test_hook_events_filter_by_kind(
    sessions_db: Path,
    evolution_db: Path,
    hook_events_db: Path,
) -> None:
    """Only design-listed kinds surface; junk kinds are dropped.

    A noisy ``hook_events`` table shouldn't widen episode coverage —
    the kind whitelist is the load-bearing filter.
    """
    insert_hook_event(
        hook_events_db,
        kind="evolution_applied",
        session_key="sess-A",
        occurred_at_ms=50,
    )
    insert_hook_event(
        hook_events_db,
        kind="random_unrelated_log",  # not in HOOK_KINDS_OF_INTEREST
        session_key="sess-A",
        occurred_at_ms=60,
    )

    insert_session_message(
        sessions_db, session_key="sess-A", seq=0, ts_ms=10
    )

    bundles = collect_bundles(
        paths=_paths(sessions_db, evolution_db, hook_events_db),
        tenant_id="default",
        window_start_ms=0,
        window_end_ms=1_000,
    )
    sess_a = next(b for b in bundles if b.session_key == "sess-A")
    assert {h.kind for h in sess_a.hooks} == {"evolution_applied"}


def test_identity_merges_attach_to_orphan_bundle(
    sessions_db: Path,
    evolution_db: Path,
    hook_events_db: Path,
    identity_db: Path,
) -> None:
    """Identity merges have no ``session_key`` linkage → orphan bucket.

    The design says identity merges are "narratively load-bearing";
    they must produce at least one bundle even if no other stream
    fired.
    """
    insert_identity_merge(
        identity_db,
        user_a="user-1",
        user_b="user-2",
        channel="telegram",
        consumed_at_ms=500,
    )

    bundles = collect_bundles(
        paths=_paths(sessions_db, evolution_db, hook_events_db, identity_db),
        tenant_id="default",
        window_start_ms=0,
        window_end_ms=1_000,
    )
    assert len(bundles) == 1
    b = bundles[0]
    assert b.session_key is None
    assert len(b.identity_merges) == 1
    assert b.identity_merges[0].channel == "telegram"


def test_missing_identity_db_does_not_raise(
    sessions_db: Path,
    evolution_db: Path,
    hook_events_db: Path,
    tmp_path: Path,
) -> None:
    """A nonexistent ``identity.sqlite`` path → degrades to no rows.

    The collector mustn't blow up on a fresh dev checkout that hasn't
    bootstrapped the B2 verification table yet.
    """
    insert_session_message(
        sessions_db, session_key="sess-A", seq=0, ts_ms=100
    )
    bundles = collect_bundles(
        paths=_paths(
            sessions_db,
            evolution_db,
            hook_events_db,
            tmp_path / "definitely-not-here.sqlite",
        ),
        tenant_id="default",
        window_start_ms=0,
        window_end_ms=1_000,
    )
    assert len(bundles) == 1
    assert bundles[0].identity_merges == []
