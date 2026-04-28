"""Unit tests for :mod:`corlinman_user_model.store`."""

from __future__ import annotations

from pathlib import Path

import pytest
from corlinman_user_model.store import UserModelStore
from corlinman_user_model.traits import TraitKind


async def test_open_or_create_idempotent(tmp_path: Path) -> None:
    """Calling ``open_or_create`` twice does not blow away existing rows."""
    db_path = tmp_path / "user_model.sqlite"
    store = await UserModelStore.open_or_create(db_path)
    async with store as s:
        await s.upsert_trait(
            user_id="qq:1",
            trait_kind=TraitKind.INTEREST,
            trait_value="技术管理",
            confidence=0.7,
            session_id="sess-1",
            now_ms=1_000,
        )

    # Second open should find the row still there.
    store2 = await UserModelStore.open_or_create(db_path)
    async with store2 as s:
        traits = await s.list_traits_for_user("qq:1", min_confidence=0.0)
    assert len(traits) == 1
    assert traits[0].trait_value == "技术管理"


async def test_upsert_inserts_new_trait(tmp_path: Path) -> None:
    db_path = tmp_path / "user_model.sqlite"
    store = await UserModelStore.open_or_create(db_path)
    async with store as s:
        await s.upsert_trait(
            user_id="qq:42",
            trait_kind=TraitKind.TONE,
            trait_value="简洁直接",
            confidence=0.6,
            session_id="sess-1",
            now_ms=1_000,
        )
        traits = await s.list_traits_for_user("qq:42", min_confidence=0.0)

    assert len(traits) == 1
    t = traits[0]
    assert t.user_id == "qq:42"
    assert t.trait_kind is TraitKind.TONE
    assert t.trait_value == "简洁直接"
    assert t.confidence == pytest.approx(0.6)
    assert t.first_seen == 1_000
    assert t.last_seen == 1_000
    assert t.session_ids == ("sess-1",)


async def test_upsert_three_times_converges_via_weighted_average(
    tmp_path: Path,
) -> None:
    """0.7 * old + 0.3 * new should converge toward the new observation."""
    db_path = tmp_path / "user_model.sqlite"
    store = await UserModelStore.open_or_create(db_path)
    async with store as s:
        # First insert: 0.5
        await s.upsert_trait(
            user_id="qq:1",
            trait_kind=TraitKind.INTEREST,
            trait_value="Rust 异步运行时",
            confidence=0.5,
            session_id="sess-1",
            now_ms=1_000,
        )
        # Second observation: 0.9 → 0.7 * 0.5 + 0.3 * 0.9 = 0.62
        await s.upsert_trait(
            user_id="qq:1",
            trait_kind=TraitKind.INTEREST,
            trait_value="Rust 异步运行时",
            confidence=0.9,
            session_id="sess-2",
            now_ms=2_000,
        )
        # Third observation: 0.9 → 0.7 * 0.62 + 0.3 * 0.9 = 0.704
        await s.upsert_trait(
            user_id="qq:1",
            trait_kind=TraitKind.INTEREST,
            trait_value="Rust 异步运行时",
            confidence=0.9,
            session_id="sess-3",
            now_ms=3_000,
        )
        traits = await s.list_traits_for_user("qq:1", min_confidence=0.0)

    assert len(traits) == 1
    t = traits[0]
    assert t.confidence == pytest.approx(0.704, abs=1e-6)
    assert t.first_seen == 1_000  # preserved
    assert t.last_seen == 3_000
    assert set(t.session_ids) == {"sess-1", "sess-2", "sess-3"}


async def test_upsert_does_not_duplicate_session_id(tmp_path: Path) -> None:
    """Re-upserting the same session must not append to ``session_ids`` twice."""
    db_path = tmp_path / "user_model.sqlite"
    store = await UserModelStore.open_or_create(db_path)
    async with store as s:
        for _ in range(3):
            await s.upsert_trait(
                user_id="qq:1",
                trait_kind=TraitKind.INTEREST,
                trait_value="技术管理",
                confidence=0.6,
                session_id="sess-only",
                now_ms=1_000,
            )
        traits = await s.list_traits_for_user("qq:1", min_confidence=0.0)

    assert traits[0].session_ids == ("sess-only",)


async def test_list_filters_by_kind(tmp_path: Path) -> None:
    db_path = tmp_path / "user_model.sqlite"
    store = await UserModelStore.open_or_create(db_path)
    async with store as s:
        await s.upsert_trait(
            user_id="qq:1",
            trait_kind=TraitKind.INTEREST,
            trait_value="技术管理",
            confidence=0.8,
            session_id="s1",
            now_ms=1_000,
        )
        await s.upsert_trait(
            user_id="qq:1",
            trait_kind=TraitKind.TONE,
            trait_value="简洁直接",
            confidence=0.7,
            session_id="s1",
            now_ms=1_000,
        )
        interests = await s.list_traits_for_user(
            "qq:1", kind=TraitKind.INTEREST, min_confidence=0.0
        )
        tones = await s.list_traits_for_user(
            "qq:1", kind=TraitKind.TONE, min_confidence=0.0
        )

    assert len(interests) == 1
    assert interests[0].trait_kind is TraitKind.INTEREST
    assert len(tones) == 1
    assert tones[0].trait_kind is TraitKind.TONE


async def test_list_filters_by_min_confidence(tmp_path: Path) -> None:
    db_path = tmp_path / "user_model.sqlite"
    store = await UserModelStore.open_or_create(db_path)
    async with store as s:
        await s.upsert_trait(
            user_id="qq:1",
            trait_kind=TraitKind.INTEREST,
            trait_value="A",
            confidence=0.9,
            session_id="s1",
            now_ms=1_000,
        )
        await s.upsert_trait(
            user_id="qq:1",
            trait_kind=TraitKind.INTEREST,
            trait_value="B",
            confidence=0.45,
            session_id="s1",
            now_ms=1_000,
        )
        await s.upsert_trait(
            user_id="qq:1",
            trait_kind=TraitKind.INTEREST,
            trait_value="C",
            confidence=0.2,
            session_id="s1",
            now_ms=1_000,
        )
        # Default floor is 0.4 → C is excluded.
        default = await s.list_traits_for_user("qq:1")
        # Tighter floor → only A.
        tight = await s.list_traits_for_user("qq:1", min_confidence=0.7)

    assert {t.trait_value for t in default} == {"A", "B"}
    assert {t.trait_value for t in tight} == {"A"}
    # Default ordering is by confidence DESC.
    assert [t.trait_value for t in default] == ["A", "B"]


async def test_prune_low_confidence(tmp_path: Path) -> None:
    db_path = tmp_path / "user_model.sqlite"
    store = await UserModelStore.open_or_create(db_path)
    async with store as s:
        await s.upsert_trait(
            user_id="qq:1",
            trait_kind=TraitKind.INTEREST,
            trait_value="high",
            confidence=0.9,
            session_id="s1",
            now_ms=1_000,
        )
        await s.upsert_trait(
            user_id="qq:1",
            trait_kind=TraitKind.INTEREST,
            trait_value="mid",
            confidence=0.45,
            session_id="s1",
            now_ms=1_000,
        )
        await s.upsert_trait(
            user_id="qq:1",
            trait_kind=TraitKind.INTEREST,
            trait_value="low",
            confidence=0.1,
            session_id="s1",
            now_ms=1_000,
        )
        deleted = await s.prune_low_confidence(0.3)
        remaining = await s.list_traits_for_user("qq:1", min_confidence=0.0)

    assert deleted == 1
    assert {t.trait_value for t in remaining} == {"high", "mid"}
