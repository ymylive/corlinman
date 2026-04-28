"""Unit tests for :mod:`corlinman_user_model.placeholders`."""

from __future__ import annotations

from pathlib import Path

from corlinman_user_model.placeholders import UserModelResolver
from corlinman_user_model.store import UserModelStore
from corlinman_user_model.traits import TraitKind


async def _seed_traits(db_path: Path) -> None:
    """Insert five interests + one tone for ``qq:1``.

    Confidences are deliberately spread so the top-3 cut produces a
    deterministic ordering for the comma-joined assertions below.
    """
    store = await UserModelStore.open_or_create(db_path)
    async with store as s:
        await s.upsert_trait(
            user_id="qq:1",
            trait_kind=TraitKind.INTEREST,
            trait_value="A",
            confidence=0.95,
            session_id="s1",
            now_ms=1_000,
        )
        await s.upsert_trait(
            user_id="qq:1",
            trait_kind=TraitKind.INTEREST,
            trait_value="B",
            confidence=0.85,
            session_id="s1",
            now_ms=1_000,
        )
        await s.upsert_trait(
            user_id="qq:1",
            trait_kind=TraitKind.INTEREST,
            trait_value="C",
            confidence=0.75,
            session_id="s1",
            now_ms=1_000,
        )
        await s.upsert_trait(
            user_id="qq:1",
            trait_kind=TraitKind.INTEREST,
            trait_value="D",
            confidence=0.65,
            session_id="s1",
            now_ms=1_000,
        )
        await s.upsert_trait(
            user_id="qq:1",
            trait_kind=TraitKind.INTEREST,
            trait_value="E",
            confidence=0.55,
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


async def test_resolve_returns_top_k_interests(tmp_path: Path) -> None:
    db_path = tmp_path / "user_model.sqlite"
    await _seed_traits(db_path)

    store = await UserModelStore.open_or_create(db_path)
    async with store as s:
        resolver = UserModelResolver(s, top_k=3)
        out = await resolver.resolve("user.interests", "qq:1")

    # Default min-confidence of 0.4 keeps all five but top-3 cuts to A,B,C.
    assert out == "A, B, C"


async def test_resolve_tone(tmp_path: Path) -> None:
    db_path = tmp_path / "user_model.sqlite"
    await _seed_traits(db_path)

    store = await UserModelStore.open_or_create(db_path)
    async with store as s:
        resolver = UserModelResolver(s)
        out = await resolver.resolve("user.tone", "qq:1")
    assert out == "简洁直接"


async def test_resolve_empty_user_id_returns_empty(tmp_path: Path) -> None:
    db_path = tmp_path / "user_model.sqlite"
    await _seed_traits(db_path)

    store = await UserModelStore.open_or_create(db_path)
    async with store as s:
        resolver = UserModelResolver(s)
        out = await resolver.resolve("user.interests", "")
    assert out == ""


async def test_resolve_unknown_user_returns_empty(tmp_path: Path) -> None:
    db_path = tmp_path / "user_model.sqlite"
    await _seed_traits(db_path)

    store = await UserModelStore.open_or_create(db_path)
    async with store as s:
        resolver = UserModelResolver(s)
        out = await resolver.resolve("user.interests", "qq:does-not-exist")
    assert out == ""


async def test_resolve_unknown_key_returns_empty(tmp_path: Path) -> None:
    db_path = tmp_path / "user_model.sqlite"
    await _seed_traits(db_path)

    store = await UserModelStore.open_or_create(db_path)
    async with store as s:
        resolver = UserModelResolver(s)
        out = await resolver.resolve("user.bogus", "qq:1")
    assert out == ""


async def test_resolve_topics_when_none_present(tmp_path: Path) -> None:
    db_path = tmp_path / "user_model.sqlite"
    await _seed_traits(db_path)  # only INTEREST + TONE

    store = await UserModelStore.open_or_create(db_path)
    async with store as s:
        resolver = UserModelResolver(s)
        out = await resolver.resolve("user.topics", "qq:1")
    assert out == ""


async def test_resolve_respects_top_k(tmp_path: Path) -> None:
    db_path = tmp_path / "user_model.sqlite"
    await _seed_traits(db_path)

    store = await UserModelStore.open_or_create(db_path)
    async with store as s:
        resolver = UserModelResolver(s, top_k=1)
        out = await resolver.resolve("user.interests", "qq:1")
    assert out == "A"


async def test_resolve_respects_min_confidence_floor(tmp_path: Path) -> None:
    db_path = tmp_path / "user_model.sqlite"
    await _seed_traits(db_path)

    store = await UserModelStore.open_or_create(db_path)
    async with store as s:
        # Floor 0.8 leaves only A (0.95) + B (0.85).
        resolver = UserModelResolver(s, top_k=5, min_confidence=0.8)
        out = await resolver.resolve("user.interests", "qq:1")
    assert out == "A, B"
