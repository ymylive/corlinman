"""Tests for :mod:`corlinman_goals.evidence` (iter 4).

Two impls under test:

- :class:`EpisodesStoreEvidence` — D1-backed; we seed an
  ``episodes.sqlite`` from D1's own ``EpisodesStore`` so the schema
  drifts loudly if the D1 column list changes.
- :class:`StaticEvidence` — in-memory; same window-overlap rules.

The protocol :class:`EpisodeEvidence` is implicitly under test via
``isinstance`` runtime checks on both impls.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from corlinman_episodes.store import Episode, EpisodeKind, EpisodesStore
from corlinman_goals.evidence import (
    DEFAULT_EVIDENCE_LIMIT,
    EpisodeEvidence,
    EpisodesStoreEvidence,
    EvidenceEpisode,
    StaticEvidence,
)
from corlinman_goals.windows import Window

_NOW = int(
    datetime(2026, 5, 9, 14, 0, tzinfo=UTC).timestamp() * 1000
)
_HOUR_MS = 3600 * 1000
_DAY_MS = 24 * _HOUR_MS


def _ev(
    *,
    episode_id: str,
    started: int,
    ended: int,
    kind: str = "conversation",
    body: str = "talked about thing",
    importance: float = 0.5,
) -> EvidenceEpisode:
    return EvidenceEpisode(
        episode_id=episode_id,
        started_at_ms=started,
        ended_at_ms=ended,
        kind=kind,
        summary_text=body,
        importance_score=importance,
    )


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_static_evidence_satisfies_protocol() -> None:
    """``StaticEvidence`` is what the test fixtures use to drive
    reflection (iter 5); the protocol guard catches an accidental
    method-rename without waiting for an integration test failure."""
    assert isinstance(StaticEvidence([]), EpisodeEvidence)


def test_episodes_store_evidence_satisfies_protocol(tmp_path: Path) -> None:
    """``EpisodesStoreEvidence`` is the production impl. Same guard;
    different reason — the construction path goes through ``open``,
    so a missing ``fetch`` would only blow up at first call."""
    ev = EpisodesStoreEvidence(
        episodes_db_path=tmp_path / "episodes.sqlite",
        tenant_id="t",
    )
    assert isinstance(ev, EpisodeEvidence)


# ---------------------------------------------------------------------------
# StaticEvidence — pure-Python window math
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_static_filters_by_window_overlap() -> None:
    """Half-open overlap: ``started < end AND ended > start``.

    - Episode entirely before the window → excluded.
    - Episode entirely after → excluded.
    - Episode straddling the lower bound → included.
    - Episode straddling the upper bound → included.
    - Episode entirely inside → included.
    """
    window = Window(start_ms=_NOW - _DAY_MS, end_ms=_NOW)
    ev = StaticEvidence(
        [
            _ev(  # entirely before
                episode_id="before",
                started=_NOW - 3 * _DAY_MS,
                ended=_NOW - 2 * _DAY_MS,
            ),
            _ev(  # straddles lower bound
                episode_id="straddle-lo",
                started=_NOW - _DAY_MS - _HOUR_MS,
                ended=_NOW - 23 * _HOUR_MS,
            ),
            _ev(  # entirely inside
                episode_id="inside",
                started=_NOW - 12 * _HOUR_MS,
                ended=_NOW - 6 * _HOUR_MS,
            ),
            _ev(  # straddles upper bound
                episode_id="straddle-hi",
                started=_NOW - _HOUR_MS,
                ended=_NOW + _HOUR_MS,
            ),
            _ev(  # entirely after
                episode_id="after",
                started=_NOW + _DAY_MS,
                ended=_NOW + 2 * _DAY_MS,
            ),
        ]
    )
    rows = await ev.fetch(agent_id="mentor", window=window)
    ids = {r.episode_id for r in rows}
    assert ids == {"straddle-lo", "inside", "straddle-hi"}


@pytest.mark.asyncio
async def test_static_orders_by_ended_at_desc() -> None:
    """Most-recent-end first; tie-broken on id desc.

    Mirrors the SQL ORDER BY so reflection sees a deterministic
    list across both impls.
    """
    window = Window(start_ms=_NOW - _DAY_MS, end_ms=_NOW + _DAY_MS)
    ev = StaticEvidence(
        [
            _ev(episode_id="a", started=_NOW - _HOUR_MS, ended=_NOW),
            _ev(episode_id="c", started=_NOW - _HOUR_MS, ended=_NOW),
            _ev(
                episode_id="b",
                started=_NOW - 2 * _HOUR_MS,
                ended=_NOW - _HOUR_MS,
            ),
        ]
    )
    rows = await ev.fetch(agent_id="mentor", window=window)
    # Same ended_at: id-desc tiebreak puts ``c`` before ``a``; ``b`` last.
    assert [r.episode_id for r in rows] == ["c", "a", "b"]


@pytest.mark.asyncio
async def test_static_caps_at_limit() -> None:
    """``limit`` clips to the most-recent N — older episodes drop off."""
    window = Window(start_ms=_NOW - 10 * _DAY_MS, end_ms=_NOW)
    eps = [
        _ev(
            episode_id=f"e{i}",
            started=_NOW - i * _HOUR_MS,
            ended=_NOW - (i - 1) * _HOUR_MS,
        )
        for i in range(1, 6)
    ]
    ev = StaticEvidence(eps)
    rows = await ev.fetch(agent_id="mentor", window=window, limit=3)
    assert len(rows) == 3
    # Newest three: e1, e2, e3 (ended_at desc).
    assert [r.episode_id for r in rows] == ["e1", "e2", "e3"]


@pytest.mark.asyncio
async def test_static_default_limit_matches_design() -> None:
    """Cap defaults to the design's ``evidence_max_episodes = 8``.

    Pinning the constant in a test (not just in code) makes a config
    drift between the resolver, reflection, and the design doc loud.
    """
    window = Window(start_ms=_NOW - 10 * _DAY_MS, end_ms=_NOW)
    eps = [
        _ev(
            episode_id=f"e{i:02d}",
            started=_NOW - i * _HOUR_MS,
            ended=_NOW - (i - 1) * _HOUR_MS,
        )
        for i in range(1, 20)
    ]
    ev = StaticEvidence(eps)
    rows = await ev.fetch(agent_id="mentor", window=window)
    assert len(rows) == DEFAULT_EVIDENCE_LIMIT == 8


# ---------------------------------------------------------------------------
# EpisodesStoreEvidence — D1-backed impl
# ---------------------------------------------------------------------------


async def _seed_episode(
    store: EpisodesStore,
    *,
    episode_id: str,
    tenant_id: str,
    started: int,
    ended: int,
    body: str = "summary",
    kind: EpisodeKind = EpisodeKind.CONVERSATION,
    importance: float = 0.5,
) -> None:
    """Seed one ``episodes`` row through D1's own writer.

    Going through ``insert_episode`` (rather than raw SQL) means the
    test exercises the D1 schema as published — a column rename in D1
    breaks here loudly, exactly the canary we want.
    """
    await store.insert_episode(
        Episode(
            id=episode_id,
            tenant_id=tenant_id,
            started_at=started,
            ended_at=ended,
            kind=kind,
            summary_text=body,
            importance_score=importance,
            distilled_by="test",
            distilled_at=ended,
        )
    )


@pytest.mark.asyncio
async def test_episodes_store_evidence_overlap_and_limit(
    tmp_path: Path,
) -> None:
    """End-to-end overlap + limit + ordering against a real D1 DB."""
    db = tmp_path / "episodes.sqlite"
    async with EpisodesStore(db) as store:
        # Two in window, one before, one after; plus tenant isolation row.
        await _seed_episode(
            store,
            episode_id="ep-in-1",
            tenant_id="t1",
            started=_NOW - 6 * _HOUR_MS,
            ended=_NOW - 5 * _HOUR_MS,
            body="recent",
        )
        await _seed_episode(
            store,
            episode_id="ep-in-2",
            tenant_id="t1",
            started=_NOW - 12 * _HOUR_MS,
            ended=_NOW - 10 * _HOUR_MS,
            body="earlier same window",
        )
        await _seed_episode(
            store,
            episode_id="ep-before",
            tenant_id="t1",
            started=_NOW - 3 * _DAY_MS,
            ended=_NOW - 2 * _DAY_MS,
        )
        await _seed_episode(
            store,
            episode_id="ep-other-tenant",
            tenant_id="t2",
            started=_NOW - 6 * _HOUR_MS,
            ended=_NOW - 5 * _HOUR_MS,
        )

    async with EpisodesStoreEvidence(
        episodes_db_path=db, tenant_id="t1"
    ) as ev:
        window = Window(start_ms=_NOW - _DAY_MS, end_ms=_NOW)
        rows = await ev.fetch(agent_id="mentor", window=window)

    assert [r.episode_id for r in rows] == ["ep-in-1", "ep-in-2"]
    assert rows[0].summary_text == "recent"
    # Cross-tenant row never surfaces — tenant_id is the first WHERE clause.
    assert all(r.episode_id != "ep-other-tenant" for r in rows)


@pytest.mark.asyncio
async def test_episodes_store_evidence_respects_limit_kwarg(
    tmp_path: Path,
) -> None:
    """Caller-provided ``limit`` overrides the default."""
    db = tmp_path / "episodes.sqlite"
    async with EpisodesStore(db) as store:
        for i in range(5):
            await _seed_episode(
                store,
                episode_id=f"ep-{i}",
                tenant_id="t",
                started=_NOW - (i + 1) * _HOUR_MS,
                ended=_NOW - i * _HOUR_MS,
            )

    async with EpisodesStoreEvidence(
        episodes_db_path=db, tenant_id="t"
    ) as ev:
        window = Window(start_ms=_NOW - _DAY_MS, end_ms=_NOW + _HOUR_MS)
        rows = await ev.fetch(agent_id="a", window=window, limit=2)

    # Two newest by ended_at desc.
    assert len(rows) == 2
    assert rows[0].episode_id == "ep-0"
    assert rows[1].episode_id == "ep-1"


@pytest.mark.asyncio
async def test_episodes_store_evidence_use_outside_context_raises(
    tmp_path: Path,
) -> None:
    """Defensive: calling ``fetch`` after ``close()`` raises a clear error
    rather than blowing up deep inside aiosqlite."""
    ev = EpisodesStoreEvidence(
        episodes_db_path=tmp_path / "episodes.sqlite",
        tenant_id="t",
    )
    with pytest.raises(RuntimeError, match="outside async context"):
        _ = ev.conn
