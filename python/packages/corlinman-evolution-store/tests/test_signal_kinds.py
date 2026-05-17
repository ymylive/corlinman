"""Phase 4 W4 — curator-loop signal-kind constants.

Pins the cross-language wire values that the curator + user-correction
detector emit into ``evolution_signals.event_kind``. A change here is a
breaking change to anything that reads the SQLite contract.
"""

from __future__ import annotations

from corlinman_evolution_store import (
    EVENT_CURATOR_RUN_COMPLETED,
    EVENT_CURATOR_RUN_FAILED,
    EVENT_IDLE_REFLECTION,
    EVENT_SKILL_UNUSED,
    EVENT_USER_CORRECTION,
    EvolutionSignal,
    EvolutionStore,
    SignalSeverity,
    SignalsRepo,
)


def test_signal_kind_string_values_are_pinned() -> None:
    """Wire-value contract — these strings are read back out of SQLite
    by the gateway and engine. Bumping them is a schema break."""
    assert EVENT_USER_CORRECTION == "user.correction"
    assert EVENT_SKILL_UNUSED == "skill.unused"
    assert EVENT_IDLE_REFLECTION == "idle.reflection"
    assert EVENT_CURATOR_RUN_COMPLETED == "curator.run.completed"
    assert EVENT_CURATOR_RUN_FAILED == "curator.run.failed"


def test_signal_kinds_are_exported_from_package_root() -> None:
    """Sanity-check the package-level re-export so downstream callers
    can ``from corlinman_evolution_store import EVENT_USER_CORRECTION``
    without reaching into ``.types``."""
    import corlinman_evolution_store as ces

    for name in (
        "EVENT_USER_CORRECTION",
        "EVENT_SKILL_UNUSED",
        "EVENT_IDLE_REFLECTION",
        "EVENT_CURATOR_RUN_COMPLETED",
        "EVENT_CURATOR_RUN_FAILED",
    ):
        assert name in ces.__all__, f"{name} missing from package __all__"
        assert hasattr(ces, name), f"{name} not re-exported"


async def test_user_correction_signal_roundtrips_through_repo(
    store: EvolutionStore,
) -> None:
    """End-to-end: insert a signal stamped with the new ``user.correction``
    kind, list it back filtered on that exact event_kind. Proves the
    constant + repo wiring work as one."""
    repo = SignalsRepo(store.conn)
    new_id = await repo.insert(
        EvolutionSignal(
            event_kind=EVENT_USER_CORRECTION,
            target="bulleted-summaries",
            severity=SignalSeverity.WARN,
            payload_json={
                "text": "stop using bullet points",
                "session_id": "sess-1",
            },
            trace_id="t1",
            session_id="sess-1",
            observed_at=1_000,
            tenant_id="default",
        )
    )
    assert new_id > 0

    rows = await repo.list_since(0, EVENT_USER_CORRECTION, 10)
    assert len(rows) == 1
    only = rows[0]
    assert only.event_kind == "user.correction"
    assert only.target == "bulleted-summaries"
    assert only.payload_json["text"] == "stop using bullet points"
    assert only.severity is SignalSeverity.WARN


async def test_curator_run_completed_signal_roundtrips(store: EvolutionStore) -> None:
    """Same shape, different kind — guards against accidental reuse of
    a wire string across constants."""
    repo = SignalsRepo(store.conn)
    await repo.insert(
        EvolutionSignal(
            event_kind=EVENT_CURATOR_RUN_COMPLETED,
            target="research",
            severity=SignalSeverity.INFO,
            payload_json={
                "marked_stale": 2,
                "archived": 1,
                "reactivated": 0,
            },
            observed_at=2_000,
        )
    )
    await repo.insert(
        EvolutionSignal(
            event_kind=EVENT_CURATOR_RUN_FAILED,
            target="research",
            severity=SignalSeverity.ERROR,
            payload_json={"reason": "timeout"},
            observed_at=3_000,
        )
    )

    completed = await repo.list_since(0, EVENT_CURATOR_RUN_COMPLETED, 10)
    failed = await repo.list_since(0, EVENT_CURATOR_RUN_FAILED, 10)

    assert len(completed) == 1
    assert completed[0].event_kind == "curator.run.completed"
    assert completed[0].payload_json["marked_stale"] == 2

    assert len(failed) == 1
    assert failed[0].event_kind == "curator.run.failed"
    assert failed[0].payload_json["reason"] == "timeout"
