"""Tests for :mod:`corlinman_episodes.embed`.

Covers the design doc's iter-5 acceptance line in §"Embeddings +
retrieval":

    "splitting summary-write from embed-write keeps a remote-embedding
    outage non-blocking" + "embedding_failure_persists_episode_with_null_vector"

Plus the OQ 4 contract: dim mismatch is a hard error, not a silent
truncate.
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from pathlib import Path

import pytest
from corlinman_episodes.config import DEFAULT_TENANT_ID, EpisodesConfig
from corlinman_episodes.embed import (
    EmbeddingDimMismatchError,
    decode_embedding,
    encode_embedding,
    populate_pending_embeddings,
)
from corlinman_episodes.store import (
    Episode,
    EpisodeKind,
    EpisodesStore,
    new_episode_id,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


async def _seed_episodes(
    store: EpisodesStore,
    *,
    count: int,
    tenant_id: str = DEFAULT_TENANT_ID,
    summary_prefix: str = "summary",
    base_ended_at: int = 1_000,
) -> list[str]:
    """Insert ``count`` episodes with no embedding; return their ids in
    insertion order.

    Each row's ``ended_at`` is staggered by 1ms so the
    ``ORDER BY ended_at DESC`` in :meth:`fetch_pending_embeddings` gives
    a deterministic newest-first sequence.
    """
    ids: list[str] = []
    for i in range(count):
        ts = base_ended_at + i
        eid = new_episode_id(ts_ms=ts)
        ids.append(eid)
        await store.insert_episode(
            Episode(
                id=eid,
                tenant_id=tenant_id,
                started_at=ts - 1,
                ended_at=ts,
                kind=EpisodeKind.CONVERSATION,
                summary_text=f"{summary_prefix}-{i}",
                source_session_keys=[f"sk-{i}"],
                distilled_by="stub",
                distilled_at=ts,
            )
        )
    return ids


def _vec(dim: int, *, base: float = 0.0) -> list[float]:
    """Deterministic ``dim``-long vector for tests; values are
    base, base+1, base+2, …."""
    return [base + i for i in range(dim)]


@pytest.fixture
def episodes_db(tmp_path: Path) -> Path:
    return tmp_path / "episodes.sqlite"


@pytest.fixture
def cfg() -> EpisodesConfig:
    return EpisodesConfig()


# ---------------------------------------------------------------------------
# Encode / decode round-trip
# ---------------------------------------------------------------------------


def test_encode_decode_round_trip() -> None:
    vec = [0.0, 1.5, -2.25, 3.75]
    blob = encode_embedding(vec)
    assert len(blob) == 4 * 4
    out = decode_embedding(blob, dim=4)
    # f32 round-trip — exact for these values (powers-of-two fractions).
    assert out == vec


def test_decode_rejects_wrong_size() -> None:
    blob = encode_embedding([1.0, 2.0])
    with pytest.raises(ValueError, match="mismatches dim"):
        decode_embedding(blob, dim=3)


def test_encode_uses_little_endian_f32() -> None:
    # Sanity-check the on-disk layout. A consumer reading the bytes
    # without going through `decode_embedding` (e.g. the Rust resolver)
    # must agree on ``<f32`` framing.
    blob = encode_embedding([1.0])
    assert blob == struct.pack("<f", 1.0)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def _fetch_embedding(store: EpisodesStore, episode_id: str) -> tuple[bytes | None, int | None]:
    """Direct read of (embedding, embedding_dim) for assertions.

    The store doesn't expose a ``get_episode_by_id`` getter (the
    natural-key probe is the only public read path), so we go straight
    to SQL — keeps the test independent of any future read-shape.
    """
    cursor = await store.conn.execute(
        "SELECT embedding, embedding_dim FROM episodes WHERE id = ?",
        (episode_id,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    assert row is not None, f"episode {episode_id!r} missing"
    return (
        bytes(row[0]) if row[0] is not None else None,
        int(row[1]) if row[1] is not None else None,
    )


async def test_populate_pending_writes_embeddings_for_all_rows(
    episodes_db: Path, cfg: EpisodesConfig
) -> None:
    async with EpisodesStore(episodes_db) as store:
        ids = await _seed_episodes(store, count=3)
        calls: list[list[str]] = []

        async def provider(texts: Sequence[str]) -> list[list[float]]:
            calls.append(list(texts))
            return [_vec(8, base=float(i)) for i in range(len(texts))]

        summary = await populate_pending_embeddings(
            config=cfg,
            store=store,
            provider=provider,
            embedding_dim=8,
            batch_size=10,
        )
        assert summary.embedded == 3
        assert summary.failed == 0
        assert summary.bytes_written == 3 * 8 * 4
        assert len(calls) == 1  # all rows fit in one batch

        # Re-check the rows: every embedding must be non-NULL with the
        # configured dim.
        pending_after = await store.fetch_pending_embeddings(
            tenant_id=DEFAULT_TENANT_ID
        )
        assert pending_after == []
        for eid in ids:
            blob, dim = await _fetch_embedding(store, eid)
            assert blob is not None
            assert dim == 8
            assert len(blob) == 8 * 4


async def test_populate_pending_idempotent_skips_already_embedded(
    episodes_db: Path, cfg: EpisodesConfig
) -> None:
    async with EpisodesStore(episodes_db) as store:
        await _seed_episodes(store, count=2)
        call_count = 0

        async def provider(texts: Sequence[str]) -> list[list[float]]:
            nonlocal call_count
            call_count += 1
            return [_vec(4) for _ in texts]

        first = await populate_pending_embeddings(
            config=cfg,
            store=store,
            provider=provider,
            embedding_dim=4,
        )
        assert first.embedded == 2
        assert call_count == 1

        # Second call: nothing pending → provider must not run.
        second = await populate_pending_embeddings(
            config=cfg,
            store=store,
            provider=provider,
            embedding_dim=4,
        )
        assert second.embedded == 0
        assert call_count == 1, "provider re-invoked on a no-op sweep"


async def test_populate_pending_respects_batch_size(
    episodes_db: Path, cfg: EpisodesConfig
) -> None:
    async with EpisodesStore(episodes_db) as store:
        await _seed_episodes(store, count=5)
        batch_sizes: list[int] = []

        async def provider(texts: Sequence[str]) -> list[list[float]]:
            batch_sizes.append(len(texts))
            return [_vec(4) for _ in texts]

        summary = await populate_pending_embeddings(
            config=cfg,
            store=store,
            provider=provider,
            embedding_dim=4,
            batch_size=2,
        )
        # 5 rows, batch_size=2 → batches of [2, 2, 1].
        assert batch_sizes == [2, 2, 1]
        assert summary.embedded == 5


async def test_populate_pending_max_episodes_caps_pass(
    episodes_db: Path, cfg: EpisodesConfig
) -> None:
    async with EpisodesStore(episodes_db) as store:
        await _seed_episodes(store, count=4)

        async def provider(texts: Sequence[str]) -> list[list[float]]:
            return [_vec(4) for _ in texts]

        summary = await populate_pending_embeddings(
            config=cfg,
            store=store,
            provider=provider,
            embedding_dim=4,
            max_episodes=2,
        )
        assert summary.embedded == 2
        # Two rows still pending after a capped sweep.
        pending_left = await store.fetch_pending_embeddings(
            tenant_id=DEFAULT_TENANT_ID
        )
        assert len(pending_left) == 2


# ---------------------------------------------------------------------------
# Outage handling — design doc test
# `embedding_failure_persists_episode_with_null_vector`
# ---------------------------------------------------------------------------


async def test_provider_503_keeps_rows_null_and_continues(
    episodes_db: Path, cfg: EpisodesConfig
) -> None:
    async with EpisodesStore(episodes_db) as store:
        await _seed_episodes(store, count=3)

        async def provider(texts: Sequence[str]) -> list[list[float]]:
            raise RuntimeError("503 service unavailable")

        summary = await populate_pending_embeddings(
            config=cfg,
            store=store,
            provider=provider,
            embedding_dim=4,
            batch_size=10,  # whole batch fails together
        )
        assert summary.embedded == 0
        assert summary.failed == 3
        assert all(
            "503" in m for m in summary.failed_messages
        ), "failure messages should carry the provider error"

        # Rows must still be visible to the next sweep — embedding NULL.
        pending_left = await store.fetch_pending_embeddings(
            tenant_id=DEFAULT_TENANT_ID
        )
        assert len(pending_left) == 3


async def test_partial_batch_failure_isolates_other_batches(
    episodes_db: Path, cfg: EpisodesConfig
) -> None:
    async with EpisodesStore(episodes_db) as store:
        await _seed_episodes(store, count=4)
        call_idx = 0

        async def provider(texts: Sequence[str]) -> list[list[float]]:
            nonlocal call_idx
            call_idx += 1
            if call_idx == 2:
                raise RuntimeError("transient")
            return [_vec(4) for _ in texts]

        summary = await populate_pending_embeddings(
            config=cfg,
            store=store,
            provider=provider,
            embedding_dim=4,
            batch_size=2,
        )
        # First batch (2 rows) wrote, second batch (2 rows) failed.
        assert summary.embedded == 2
        assert summary.failed == 2

        # The two failed rows should still be pending.
        pending_left = await store.fetch_pending_embeddings(
            tenant_id=DEFAULT_TENANT_ID
        )
        assert len(pending_left) == 2


async def test_provider_arity_mismatch_is_treated_as_failure(
    episodes_db: Path, cfg: EpisodesConfig
) -> None:
    async with EpisodesStore(episodes_db) as store:
        await _seed_episodes(store, count=2)

        async def provider(texts: Sequence[str]) -> list[list[float]]:
            # Misbehave: only return one vector for two texts.
            return [_vec(4)]

        summary = await populate_pending_embeddings(
            config=cfg,
            store=store,
            provider=provider,
            embedding_dim=4,
            batch_size=10,
        )
        assert summary.embedded == 0
        assert summary.failed == 2
        # Rows stay NULL.
        assert (
            len(
                await store.fetch_pending_embeddings(
                    tenant_id=DEFAULT_TENANT_ID
                )
            )
            == 2
        )


# ---------------------------------------------------------------------------
# Dim-mismatch contract — design OQ 4
# ---------------------------------------------------------------------------


async def test_dim_mismatch_is_hard_error(
    episodes_db: Path, cfg: EpisodesConfig
) -> None:
    async with EpisodesStore(episodes_db) as store:
        await _seed_episodes(store, count=1)

        async def provider(texts: Sequence[str]) -> list[list[float]]:
            return [_vec(8) for _ in texts]  # provider returns 8-d, expected 4

        with pytest.raises(EmbeddingDimMismatchError) as excinfo:
            await populate_pending_embeddings(
                config=cfg,
                store=store,
                provider=provider,
                embedding_dim=4,
            )
        assert excinfo.value.expected == 4
        assert excinfo.value.observed == 8


async def test_dim_mismatch_preserves_earlier_rows_in_batch(
    episodes_db: Path, cfg: EpisodesConfig
) -> None:
    """If batch=1 and the second row's vector has the wrong dim, the
    first row's embedding must already be persisted.
    """
    async with EpisodesStore(episodes_db) as store:
        await _seed_episodes(store, count=2)
        call = 0

        async def provider(texts: Sequence[str]) -> list[list[float]]:
            nonlocal call
            call += 1
            # First call: correct dim. Second: wrong dim.
            dim = 4 if call == 1 else 8
            return [_vec(dim) for _ in texts]

        with pytest.raises(EmbeddingDimMismatchError):
            await populate_pending_embeddings(
                config=cfg,
                store=store,
                provider=provider,
                embedding_dim=4,
                batch_size=1,
            )

        # First row got committed before the second row's mismatch
        # raised — exactly one row remains pending.
        pending_left = await store.fetch_pending_embeddings(
            tenant_id=DEFAULT_TENANT_ID
        )
        assert len(pending_left) == 1


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


async def test_tenant_filter_excludes_other_tenants(
    episodes_db: Path, cfg: EpisodesConfig
) -> None:
    async with EpisodesStore(episodes_db) as store:
        await _seed_episodes(store, count=2, tenant_id="alpha")
        await _seed_episodes(store, count=3, tenant_id="beta")

        async def provider(texts: Sequence[str]) -> list[list[float]]:
            return [_vec(4) for _ in texts]

        alpha = await populate_pending_embeddings(
            config=cfg,
            store=store,
            provider=provider,
            embedding_dim=4,
            tenant_id="alpha",
        )
        assert alpha.embedded == 2
        assert alpha.tenant_id == "alpha"

        # Beta's three rows are still pending.
        beta_pending = await store.fetch_pending_embeddings(tenant_id="beta")
        assert len(beta_pending) == 3
        # Alpha has none.
        alpha_pending = await store.fetch_pending_embeddings(tenant_id="alpha")
        assert alpha_pending == []


# ---------------------------------------------------------------------------
# Disabled config short-circuit
# ---------------------------------------------------------------------------


async def test_disabled_config_short_circuits(
    episodes_db: Path,
) -> None:
    cfg = EpisodesConfig(enabled=False)
    async with EpisodesStore(episodes_db) as store:
        await _seed_episodes(store, count=2)

        async def provider(texts: Sequence[str]) -> list[list[float]]:
            raise AssertionError("provider must not be called when disabled")

        summary = await populate_pending_embeddings(
            config=cfg,
            store=store,
            provider=provider,
            embedding_dim=4,
        )
        assert summary.embedded == 0
        assert summary.failed == 0
        # Rows still pending — caller can re-enable + re-run.
        assert (
            len(
                await store.fetch_pending_embeddings(
                    tenant_id=DEFAULT_TENANT_ID
                )
            )
            == 2
        )


# ---------------------------------------------------------------------------
# Store-level write helpers
# ---------------------------------------------------------------------------


async def test_update_episode_embedding_validates_blob_size(
    episodes_db: Path,
) -> None:
    async with EpisodesStore(episodes_db) as store:
        ids = await _seed_episodes(store, count=1)
        with pytest.raises(ValueError, match="does not match"):
            await store.update_episode_embedding(
                episode_id=ids[0],
                embedding=b"\x00\x00\x00",  # 3 bytes — not a multiple of 4
                embedding_dim=4,
            )


async def test_update_episode_embedding_rejects_zero_dim(
    episodes_db: Path,
) -> None:
    async with EpisodesStore(episodes_db) as store:
        ids = await _seed_episodes(store, count=1)
        with pytest.raises(ValueError, match="must be positive"):
            await store.update_episode_embedding(
                episode_id=ids[0],
                embedding=b"",
                embedding_dim=0,
            )
