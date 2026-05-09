"""Iter 8 tests — cold archival sweep.

Pin the design-doc test matrix entries:

- ``cold_archive_after_180d_unreferenced`` — time-warped row → hot
  columns null, cold file present, reads still work (resolver-side
  rehydration is iter 9, but the row itself must be addressable).
- Idempotent re-run — already-archived rows aren't re-archived.
- ``INCIDENT`` rows are exempted (auto-rollback audit-trail forever).
- Recently-referenced rows are not archived.
- Cold file payload round-trips ``summary_text`` + ``embedding`` bytes.

All tests stay offline — archival is a pure local-disk + SQLite
operation; no providers, no network.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest
from corlinman_episodes import (
    ARCHIVED_SENTINEL,
    COLD_DIR_NAME,
    ArchiveSummary,
    Episode,
    EpisodeKind,
    EpisodesConfig,
    EpisodesStore,
    archive_unreferenced_episodes,
    cold_file_path,
    iter_cold_files,
    new_episode_id,
    render_cold_file,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def episodes_db(tmp_path: Path) -> Path:
    return tmp_path / "episodes.sqlite"


@pytest.fixture
def cold_root(tmp_path: Path) -> Path:
    """The cold-archive root — production layout puts this next to
    ``episodes.sqlite`` under the same per-tenant directory."""
    return tmp_path


def _config(**overrides: object) -> EpisodesConfig:
    base: dict[str, object] = {"cold_archive_days": 180}
    base.update(overrides)
    return EpisodesConfig(**base)  # type: ignore[arg-type]


def _ms(days_ago: int, *, now: int = 1_700_000_000_000) -> int:
    """Helper: ``now - days_ago * 86_400_000``."""
    return now - days_ago * 86_400_000


async def _seed_episode(
    store: EpisodesStore,
    *,
    id: str | None = None,
    tenant_id: str = "default",
    kind: EpisodeKind = EpisodeKind.CONVERSATION,
    summary: str = "a summary",
    ended_at_ms: int = 0,
    last_referenced_at: int | None = None,
    importance: float = 0.5,
    embedding: bytes | None = None,
    embedding_dim: int | None = None,
) -> str:
    """Insert one episode row and return its id."""
    eid = id or new_episode_id()
    ep = Episode(
        id=eid,
        tenant_id=tenant_id,
        started_at=ended_at_ms - 1_000,
        ended_at=ended_at_ms,
        kind=kind,
        summary_text=summary,
        importance_score=importance,
        embedding=embedding,
        embedding_dim=embedding_dim,
        distilled_by="stub",
        distilled_at=ended_at_ms,
        last_referenced_at=last_referenced_at,
    )
    await store.insert_episode(ep)
    return eid


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_archives_old_unreferenced_row(
    episodes_db: Path, cold_root: Path
) -> None:
    """The canonical case: a 200-day-old conversation row → archived."""
    now = 1_700_000_000_000
    cfg = _config(cold_archive_days=180)

    async with EpisodesStore(episodes_db) as store:
        eid = await _seed_episode(
            store,
            id="ep-old",
            kind=EpisodeKind.CONVERSATION,
            summary="long-forgotten chat about haiku",
            ended_at_ms=_ms(200, now=now),
            last_referenced_at=None,
        )

        summary = await archive_unreferenced_episodes(
            config=cfg,
            store=store,
            cold_root=cold_root,
            now_ms=now,
        )

    assert isinstance(summary, ArchiveSummary)
    assert summary.archived == 1
    assert summary.archived_episode_ids == (eid,)

    # Cold file present + readable.
    cold = cold_file_path(root=cold_root, episode_id=eid)
    assert cold.exists()
    text = cold.read_text(encoding="utf-8")
    assert "long-forgotten chat about haiku" in text
    assert f"episode_id: {eid}" in text
    assert "kind: conversation" in text

    # Hot row stamped with the sentinel.
    async with EpisodesStore(episodes_db) as store:
        cur = await store.conn.execute(
            "SELECT summary_text, embedding, embedding_dim FROM episodes "
            "WHERE id = ?",
            (eid,),
        )
        row = await cur.fetchone()
        await cur.close()
    assert row is not None
    assert row[0] == ARCHIVED_SENTINEL
    assert row[1] is None
    assert row[2] is None


async def test_archive_skips_recently_referenced(
    episodes_db: Path, cold_root: Path
) -> None:
    """A row referenced 30 days ago must NOT archive even if it's
    older than the cutoff by ``ended_at`` — ``last_referenced_at``
    wins per design.
    """
    now = 1_700_000_000_000
    cfg = _config(cold_archive_days=180)

    async with EpisodesStore(episodes_db) as store:
        eid = await _seed_episode(
            store,
            id="ep-warm",
            ended_at_ms=_ms(300, now=now),
            last_referenced_at=_ms(30, now=now),
            summary="hit recently",
        )

        summary = await archive_unreferenced_episodes(
            config=cfg, store=store, cold_root=cold_root, now_ms=now
        )

    assert summary.archived == 0
    assert eid not in summary.archived_episode_ids

    cold = cold_file_path(root=cold_root, episode_id=eid)
    assert not cold.exists()


async def test_archive_skips_recent_unreferenced(
    episodes_db: Path, cold_root: Path
) -> None:
    """An episode younger than the cutoff (e.g. 30 days ago, never
    referenced) is not archived. ``COALESCE(last_referenced_at,
    ended_at)`` falls back to ``ended_at``, which is fresh enough.
    """
    now = 1_700_000_000_000
    cfg = _config(cold_archive_days=180)

    async with EpisodesStore(episodes_db) as store:
        await _seed_episode(
            store,
            id="ep-young",
            ended_at_ms=_ms(30, now=now),
            last_referenced_at=None,
            summary="recent chat",
        )
        summary = await archive_unreferenced_episodes(
            config=cfg, store=store, cold_root=cold_root, now_ms=now
        )

    assert summary.archived == 0


async def test_archive_exempts_incident_kind(
    episodes_db: Path, cold_root: Path
) -> None:
    """``INCIDENT`` (auto-rollback) episodes are never archived per
    design §"Decay / pruning"."""
    now = 1_700_000_000_000
    cfg = _config(cold_archive_days=180)

    async with EpisodesStore(episodes_db) as store:
        eid = await _seed_episode(
            store,
            id="ep-incident",
            kind=EpisodeKind.INCIDENT,
            ended_at_ms=_ms(365, now=now),  # 1 year ago, plenty stale
            last_referenced_at=None,
            summary="auto-rollback fired on web_search",
        )
        summary = await archive_unreferenced_episodes(
            config=cfg, store=store, cold_root=cold_root, now_ms=now
        )

    # Either the SELECT excluded it (skipped_recent stays 0) or the
    # defence-in-depth path counted it under skipped_exempt — either
    # way it's not archived.
    assert summary.archived == 0
    assert eid not in summary.archived_episode_ids
    assert not cold_file_path(root=cold_root, episode_id=eid).exists()


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


async def test_archive_is_idempotent_on_rerun(
    episodes_db: Path, cold_root: Path
) -> None:
    """Re-running on the same data must not re-archive: the sentinel
    text is the idempotency guard. Cold-file mtime would change if
    the row reprocessed; we check the archived count instead so the
    test stays orthogonal to the filesystem clock."""
    now = 1_700_000_000_000
    cfg = _config(cold_archive_days=180)

    async with EpisodesStore(episodes_db) as store:
        await _seed_episode(
            store,
            id="ep-once",
            ended_at_ms=_ms(200, now=now),
            summary="archive me",
        )

        first = await archive_unreferenced_episodes(
            config=cfg, store=store, cold_root=cold_root, now_ms=now
        )
        second = await archive_unreferenced_episodes(
            config=cfg, store=store, cold_root=cold_root, now_ms=now
        )

    assert first.archived == 1
    assert second.archived == 0
    assert second.archived_episode_ids == ()


# ---------------------------------------------------------------------------
# Cold-file payload
# ---------------------------------------------------------------------------


async def test_cold_file_round_trips_embedding_hex(
    episodes_db: Path, cold_root: Path
) -> None:
    """An archived row carrying an embedding writes the bytes as hex
    in the cold file's front matter so iter-9 rehydration can read
    them back without ambiguity."""
    now = 1_700_000_000_000
    cfg = _config(cold_archive_days=180)

    # Simple 4-dim vector, packed f32 little-endian (matches encode_embedding).
    vec = [1.0, 0.5, -0.25, 0.0]
    blob = struct.pack(f"<{len(vec)}f", *vec)

    async with EpisodesStore(episodes_db) as store:
        eid = await _seed_episode(
            store,
            id="ep-vec",
            ended_at_ms=_ms(200, now=now),
            embedding=blob,
            embedding_dim=4,
            summary="has a vector",
        )
        await archive_unreferenced_episodes(
            config=cfg, store=store, cold_root=cold_root, now_ms=now
        )

    text = cold_file_path(root=cold_root, episode_id=eid).read_text(encoding="utf-8")
    assert "embedding_dim: 4" in text
    assert f"embedding_hex: {blob.hex()}" in text


async def test_iter_cold_files_yields_archived(
    episodes_db: Path, cold_root: Path
) -> None:
    """``iter_cold_files`` walks the cold dir for the iter-9 ``rehydrate-all``
    bulk path. Empty dir → empty iter (defensive for fresh tenants)."""
    now = 1_700_000_000_000
    cfg = _config(cold_archive_days=180)

    # Empty before any sweep.
    assert list(iter_cold_files(cold_root=cold_root)) == []

    async with EpisodesStore(episodes_db) as store:
        for i in range(3):
            await _seed_episode(
                store,
                id=f"ep-{i}",
                ended_at_ms=_ms(200 + i, now=now),
                summary=f"summary {i}",
            )
        await archive_unreferenced_episodes(
            config=cfg, store=store, cold_root=cold_root, now_ms=now
        )

    files = list(iter_cold_files(cold_root=cold_root))
    assert len(files) == 3
    for f in files:
        assert f.suffix == ".md"
        assert f.parent.name == COLD_DIR_NAME


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


async def test_archive_is_tenant_scoped(
    episodes_db: Path, cold_root: Path
) -> None:
    """Tenant A's archival pass never archives tenant B's rows even
    when both are stale — the SELECT pins ``tenant_id``."""
    now = 1_700_000_000_000
    cfg = _config(cold_archive_days=180)

    async with EpisodesStore(episodes_db) as store:
        await _seed_episode(
            store,
            id="ep-acme",
            tenant_id="acme",
            ended_at_ms=_ms(200, now=now),
            summary="acme secret",
        )
        await _seed_episode(
            store,
            id="ep-globex",
            tenant_id="globex",
            ended_at_ms=_ms(200, now=now),
            summary="globex secret",
        )

        summary = await archive_unreferenced_episodes(
            config=cfg, store=store, cold_root=cold_root, tenant_id="acme", now_ms=now
        )

    assert summary.archived == 1
    assert summary.archived_episode_ids == ("ep-acme",)
    assert cold_file_path(root=cold_root, episode_id="ep-acme").exists()
    assert not cold_file_path(root=cold_root, episode_id="ep-globex").exists()


# ---------------------------------------------------------------------------
# Render-helper unit tests (pure)
# ---------------------------------------------------------------------------


def test_render_cold_file_omits_embedding_when_absent() -> None:
    """A row with ``embedding=None`` produces a file without the
    ``embedding_*`` keys (so iter-9 rehydration can tell hot-NULL
    apart from "had-an-embedding-before-archive").
    """
    text = render_cold_file(
        episode_id="ep-x",
        tenant_id="default",
        kind="conversation",
        started_at=1,
        ended_at=2,
        importance_score=0.42,
        distilled_by="stub",
        distilled_at=2,
        last_referenced_at=None,
        summary_text="hello",
        embedding=None,
        embedding_dim=None,
    )
    assert "embedding_dim" not in text
    assert "embedding_hex" not in text
    assert "kind: conversation" in text
    assert "hello" in text
    assert text.endswith("\n")


def test_archive_disabled_returns_empty_summary(
    episodes_db: Path, cold_root: Path
) -> None:
    """When ``config.enabled=False`` the sweep is a structured no-op,
    matching the pattern across the runner / embed paths."""

    import asyncio

    async def go() -> ArchiveSummary:
        async with EpisodesStore(episodes_db) as store:
            return await archive_unreferenced_episodes(
                config=EpisodesConfig(enabled=False),
                store=store,
                cold_root=cold_root,
            )

    summary = asyncio.run(go())
    assert summary.archived == 0
    assert summary.archived_episode_ids == ()
