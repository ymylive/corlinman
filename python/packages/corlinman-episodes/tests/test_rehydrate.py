"""Iter 9 tests — cold-archive rehydration.

Pin the design-doc test matrix entry ``cold_rehydrate_on_reference``
plus the surfaces the iter 9 CLI needs:

- :func:`parse_cold_file` round-trips the iter-8 writer output.
- :func:`rehydrate_episode` restores ``summary_text`` + ``embedding``
  on the matching hot row, leaves source-id columns untouched.
- :func:`rehydrate_all` walks every cold file, tenant-filters, and
  reports a structured summary.
- A malformed cold file fails the parse step but doesn't abort the
  bulk sweep.

Stays offline — pure local-disk + SQLite.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest
from corlinman_episodes import (
    ARCHIVED_SENTINEL,
    ColdFileMalformedError,
    Episode,
    EpisodeKind,
    EpisodesConfig,
    EpisodesStore,
    RehydrateSummary,
    archive_unreferenced_episodes,
    cold_file_path,
    new_episode_id,
    parse_cold_file,
    rehydrate_all,
    rehydrate_episode,
    render_cold_file,
)

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def episodes_db(tmp_path: Path) -> Path:
    return tmp_path / "episodes.sqlite"


@pytest.fixture
def cold_root(tmp_path: Path) -> Path:
    return tmp_path


def _ms(days_ago: int, *, now: int = 1_700_000_000_000) -> int:
    return now - days_ago * 86_400_000


async def _seed_episode(
    store: EpisodesStore,
    *,
    episode_id: str | None = None,
    tenant_id: str = "default",
    kind: EpisodeKind = EpisodeKind.CONVERSATION,
    summary: str = "a summary",
    ended_at_ms: int = 0,
    embedding: bytes | None = None,
    embedding_dim: int | None = None,
    source_signal_ids: list[int] | None = None,
) -> str:
    eid = episode_id or new_episode_id()
    ep = Episode(
        id=eid,
        tenant_id=tenant_id,
        started_at=ended_at_ms - 1_000,
        ended_at=ended_at_ms,
        kind=kind,
        summary_text=summary,
        importance_score=0.5,
        embedding=embedding,
        embedding_dim=embedding_dim,
        distilled_by="stub",
        distilled_at=ended_at_ms,
        source_signal_ids=source_signal_ids or [],
    )
    await store.insert_episode(ep)
    return eid


# ---------------------------------------------------------------------------
# Pure parser
# ---------------------------------------------------------------------------


def test_parse_round_trips_writer_output() -> None:
    """``render_cold_file`` → ``parse_cold_file`` is the identity for
    every column the iter-8 writer emits."""
    blob = struct.pack("<3f", 1.0, -0.5, 0.25)
    text = render_cold_file(
        episode_id="ep-rt",
        tenant_id="default",
        kind="conversation",
        started_at=1,
        ended_at=2,
        importance_score=0.42,
        distilled_by="stub-1",
        distilled_at=2,
        last_referenced_at=99,
        summary_text="line 1\nline 2 with --- delimiter\nline 3",
        embedding=blob,
        embedding_dim=3,
    )
    cold = parse_cold_file(text)

    assert cold.episode_id == "ep-rt"
    assert cold.tenant_id == "default"
    assert cold.kind == "conversation"
    assert cold.started_at == 1
    assert cold.ended_at == 2
    assert cold.importance_score == 0.42
    assert cold.distilled_by == "stub-1"
    assert cold.distilled_at == 2
    assert cold.last_referenced_at == 99
    assert cold.summary_text == "line 1\nline 2 with --- delimiter\nline 3"
    assert cold.embedding == blob
    assert cold.embedding_dim == 3


def test_parse_rejects_missing_delimiter() -> None:
    with pytest.raises(ColdFileMalformedError):
        parse_cold_file("no front matter at all")


def test_parse_rejects_unclosed_front_matter() -> None:
    with pytest.raises(ColdFileMalformedError):
        parse_cold_file("---\nepisode_id: x\nno-closing-delim\n")


def test_parse_rejects_missing_required_keys() -> None:
    """Hand-rolled front matter with no ``kind`` should fail loudly."""
    text = "---\nepisode_id: x\ntenant_id: default\n---\n\nbody\n"
    with pytest.raises(ColdFileMalformedError) as excinfo:
        parse_cold_file(text)
    assert "kind" in excinfo.value.reason


def test_parse_rejects_embedding_dim_without_hex() -> None:
    """Both keys must appear together (or both be absent)."""
    text = (
        "---\n"
        "episode_id: x\n"
        "tenant_id: default\n"
        "kind: conversation\n"
        "started_at: 1\n"
        "ended_at: 2\n"
        "importance_score: 0.5\n"
        "distilled_by: stub\n"
        "distilled_at: 2\n"
        "embedding_dim: 4\n"  # hex is missing
        "---\n\nbody\n"
    )
    with pytest.raises(ColdFileMalformedError) as excinfo:
        parse_cold_file(text)
    assert "embedding" in excinfo.value.reason


def test_parse_rejects_dim_blob_size_mismatch() -> None:
    """``embedding_dim=4`` + 5-float bytes → catch the corruption at
    parse time so a hand-edited file doesn't silently corrupt the row."""
    blob = struct.pack("<5f", 1, 2, 3, 4, 5)  # 20 bytes; dim=4 wants 16
    text = (
        "---\n"
        "episode_id: x\n"
        "tenant_id: default\n"
        "kind: conversation\n"
        "started_at: 1\n"
        "ended_at: 2\n"
        "importance_score: 0.5\n"
        "distilled_by: stub\n"
        "distilled_at: 2\n"
        f"embedding_dim: 4\nembedding_hex: {blob.hex()}\n"
        "---\n\nbody\n"
    )
    with pytest.raises(ColdFileMalformedError):
        parse_cold_file(text)


# ---------------------------------------------------------------------------
# rehydrate_episode
# ---------------------------------------------------------------------------


async def test_rehydrate_restores_summary_and_embedding(
    episodes_db: Path, cold_root: Path
) -> None:
    """Archive a row, then rehydrate it: ``summary_text`` +
    ``embedding`` come back; source-id columns and importance stay."""
    now = 1_700_000_000_000
    cfg = EpisodesConfig(cold_archive_days=180)
    blob = struct.pack("<2f", 0.7, -0.7)

    async with EpisodesStore(episodes_db) as store:
        eid = await _seed_episode(
            store,
            episode_id="ep-rt",
            ended_at_ms=_ms(200, now=now),
            summary="the original summary",
            embedding=blob,
            embedding_dim=2,
            source_signal_ids=[101, 102],
        )
        archive_summary = await archive_unreferenced_episodes(
            config=cfg, store=store, cold_root=cold_root, now_ms=now
        )
        assert archive_summary.archived == 1

        # Hot row is now sentinel + nulls.
        cur = await store.conn.execute(
            "SELECT summary_text, embedding, embedding_dim, source_signal_ids "
            "FROM episodes WHERE id = ?",
            (eid,),
        )
        row = await cur.fetchone()
        await cur.close()
        assert row is not None
        assert row[0] == ARCHIVED_SENTINEL
        assert row[1] is None
        # Source-id columns must NOT be blanked — only the hot text +
        # vector get demoted.
        assert "101" in row[3] and "102" in row[3]

        # Rehydrate.
        ok = await rehydrate_episode(
            store=store, cold_root=cold_root, episode_id=eid
        )
        assert ok is True

        cur = await store.conn.execute(
            "SELECT summary_text, embedding, embedding_dim, source_signal_ids "
            "FROM episodes WHERE id = ?",
            (eid,),
        )
        row = await cur.fetchone()
        await cur.close()
        assert row is not None
        assert row[0] == "the original summary"
        assert bytes(row[1]) == blob
        assert row[2] == 2
        assert "101" in row[3] and "102" in row[3]


async def test_rehydrate_already_hot_returns_false(
    episodes_db: Path, cold_root: Path
) -> None:
    """Calling rehydrate on a row that's already hot is a no-op."""
    now = 1_700_000_000_000

    async with EpisodesStore(episodes_db) as store:
        eid = await _seed_episode(
            store,
            episode_id="ep-hot",
            ended_at_ms=_ms(10, now=now),
            summary="still hot",
        )
        # We need a cold file to exist so the path-check passes —
        # craft one even though the row is hot.
        cold_path = cold_file_path(root=cold_root, episode_id=eid)
        cold_path.parent.mkdir(parents=True, exist_ok=True)
        cold_path.write_text(
            render_cold_file(
                episode_id=eid,
                tenant_id="default",
                kind="conversation",
                started_at=1,
                ended_at=2,
                importance_score=0.5,
                distilled_by="stub",
                distilled_at=2,
                last_referenced_at=None,
                summary_text="other text",
                embedding=None,
                embedding_dim=None,
            ),
            encoding="utf-8",
        )

        ok = await rehydrate_episode(
            store=store, cold_root=cold_root, episode_id=eid
        )
        assert ok is False

        # And the hot column is untouched.
        cur = await store.conn.execute(
            "SELECT summary_text FROM episodes WHERE id = ?", (eid,)
        )
        row = await cur.fetchone()
        await cur.close()
        assert row is not None and row[0] == "still hot"


async def test_rehydrate_missing_cold_file_returns_false(
    episodes_db: Path, cold_root: Path
) -> None:
    """No cold file → rehydrate is a no-op (False)."""
    async with EpisodesStore(episodes_db) as store:
        await _seed_episode(store, episode_id="ep-x", ended_at_ms=1)
        ok = await rehydrate_episode(
            store=store, cold_root=cold_root, episode_id="never-existed"
        )
        assert ok is False


# ---------------------------------------------------------------------------
# rehydrate_all
# ---------------------------------------------------------------------------


async def test_rehydrate_all_promotes_every_cold_row(
    episodes_db: Path, cold_root: Path
) -> None:
    """End-to-end: archive 3 rows → rehydrate-all → all 3 hot."""
    now = 1_700_000_000_000
    cfg = EpisodesConfig(cold_archive_days=180)

    ids = ["ep-a", "ep-b", "ep-c"]
    async with EpisodesStore(episodes_db) as store:
        for i, eid in enumerate(ids):
            await _seed_episode(
                store,
                episode_id=eid,
                ended_at_ms=_ms(200 + i, now=now),
                summary=f"summary-{i}",
            )
        await archive_unreferenced_episodes(
            config=cfg, store=store, cold_root=cold_root, now_ms=now
        )

        summary = await rehydrate_all(store=store, cold_root=cold_root)

    assert isinstance(summary, RehydrateSummary)
    assert summary.rehydrated == 3
    assert set(summary.rehydrated_episode_ids) == set(ids)

    async with EpisodesStore(episodes_db) as store:
        cur = await store.conn.execute(
            "SELECT id, summary_text FROM episodes ORDER BY id"
        )
        rows = await cur.fetchall()
        await cur.close()
    assert len(rows) == 3
    for r in rows:
        assert r[1] != ARCHIVED_SENTINEL


async def test_rehydrate_all_is_idempotent(
    episodes_db: Path, cold_root: Path
) -> None:
    """Second pass: zero rehydrated, all skipped_already_hot."""
    now = 1_700_000_000_000
    cfg = EpisodesConfig(cold_archive_days=180)

    async with EpisodesStore(episodes_db) as store:
        await _seed_episode(
            store,
            episode_id="ep-once",
            ended_at_ms=_ms(200, now=now),
            summary="hi",
        )
        await archive_unreferenced_episodes(
            config=cfg, store=store, cold_root=cold_root, now_ms=now
        )
        first = await rehydrate_all(store=store, cold_root=cold_root)
        second = await rehydrate_all(store=store, cold_root=cold_root)

    assert first.rehydrated == 1
    assert second.rehydrated == 0
    assert second.skipped_already_hot == 1


async def test_rehydrate_all_filters_by_tenant(
    episodes_db: Path, cold_root: Path
) -> None:
    """A cold file for tenant B is not touched when calling
    rehydrate_all for tenant A."""
    now = 1_700_000_000_000
    cfg = EpisodesConfig(cold_archive_days=180)

    async with EpisodesStore(episodes_db) as store:
        await _seed_episode(
            store,
            episode_id="ep-acme",
            tenant_id="acme",
            ended_at_ms=_ms(200, now=now),
            summary="acme",
        )
        await _seed_episode(
            store,
            episode_id="ep-globex",
            tenant_id="globex",
            ended_at_ms=_ms(200, now=now),
            summary="globex",
        )
        # Run archive for both tenants.
        await archive_unreferenced_episodes(
            config=cfg,
            store=store,
            cold_root=cold_root,
            tenant_id="acme",
            now_ms=now,
        )
        await archive_unreferenced_episodes(
            config=cfg,
            store=store,
            cold_root=cold_root,
            tenant_id="globex",
            now_ms=now,
        )

        # Only rehydrate acme.
        summary = await rehydrate_all(
            store=store, cold_root=cold_root, tenant_id="acme"
        )

    assert summary.tenant_id == "acme"
    assert summary.rehydrated == 1
    assert summary.rehydrated_episode_ids == ("ep-acme",)

    # Globex stays cold.
    async with EpisodesStore(episodes_db) as store:
        cur = await store.conn.execute(
            "SELECT summary_text FROM episodes WHERE id = 'ep-globex'"
        )
        r = await cur.fetchone()
        await cur.close()
    assert r is not None and r[0] == ARCHIVED_SENTINEL


async def test_rehydrate_all_handles_malformed_file(
    episodes_db: Path, cold_root: Path
) -> None:
    """A malformed cold file doesn't abort the bulk sweep — it lands
    under :attr:`RehydrateSummary.failed`."""
    now = 1_700_000_000_000
    cfg = EpisodesConfig(cold_archive_days=180)

    async with EpisodesStore(episodes_db) as store:
        await _seed_episode(
            store,
            episode_id="ep-good",
            ended_at_ms=_ms(200, now=now),
            summary="good",
        )
        await archive_unreferenced_episodes(
            config=cfg, store=store, cold_root=cold_root, now_ms=now
        )
        # Drop a malformed file alongside the good one.
        bad_path = cold_file_path(root=cold_root, episode_id="ep-bad")
        bad_path.parent.mkdir(parents=True, exist_ok=True)
        bad_path.write_text("not a cold file at all", encoding="utf-8")

        summary = await rehydrate_all(store=store, cold_root=cold_root)

    assert summary.rehydrated == 1
    assert "ep-good" in summary.rehydrated_episode_ids
    assert summary.failed == 1
    assert "ep-bad" in summary.failed_episode_ids


async def test_rehydrate_all_empty_cold_dir(
    episodes_db: Path, cold_root: Path
) -> None:
    """Fresh tenant, no cold files → empty summary."""
    async with EpisodesStore(episodes_db) as store:
        summary = await rehydrate_all(store=store, cold_root=cold_root)
    assert summary.rehydrated == 0
    assert summary.failed == 0


# ---------------------------------------------------------------------------
# CLI subcommand smoke
# ---------------------------------------------------------------------------


def test_cli_rehydrate_all_subcommand(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``corlinman-episodes rehydrate-all`` parses, runs, exits 0."""
    import asyncio

    from corlinman_episodes import cli_main

    episodes_db = tmp_path / "episodes.sqlite"
    cold_root = tmp_path
    now = 1_700_000_000_000
    cfg = EpisodesConfig(cold_archive_days=180)

    async def setup() -> None:
        async with EpisodesStore(episodes_db) as store:
            await _seed_episode(
                store, episode_id="ep-cli", ended_at_ms=_ms(200, now=now), summary="hello"
            )
            await archive_unreferenced_episodes(
                config=cfg, store=store, cold_root=cold_root, now_ms=now
            )

    asyncio.run(setup())

    rc = cli_main(
        [
            "rehydrate-all",
            "--episodes-db",
            str(episodes_db),
            "--cold-root",
            str(cold_root),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "rehydrated:" in out
    assert "ep-cli" in out
