"""Tests for the near-duplicate detector."""

from __future__ import annotations

from pathlib import Path

import pytest
from corlinman_evolution_engine.memory_op import (
    DuplicatePair,
    find_near_duplicate_pairs,
    jaccard,
    reasoning_for,
)
from corlinman_evolution_engine.store import ChunkRow, KbStore

from .conftest import insert_chunk


def _chunk(cid: int, content: str) -> ChunkRow:
    return ChunkRow(id=cid, namespace="general", content=content)


def test_jaccard_identical_sets() -> None:
    assert jaccard(frozenset({"a", "b"}), frozenset({"a", "b"})) == 1.0


def test_jaccard_disjoint_sets() -> None:
    assert jaccard(frozenset({"a"}), frozenset({"b"})) == 0.0


def test_jaccard_two_empty_sets_returns_zero() -> None:
    assert jaccard(frozenset(), frozenset()) == 0.0


def test_jaccard_partial_overlap() -> None:
    a = frozenset({"x", "y", "z"})
    b = frozenset({"x", "y", "w"})
    # intersection {x,y}=2, union {x,y,z,w}=4 → 0.5
    assert jaccard(a, b) == 0.5


def test_find_near_duplicate_pairs_yields_one_pair() -> None:
    chunks = [
        _chunk(1, "the quick brown fox jumps over the lazy dog"),
        _chunk(2, "the quick brown fox jumps over the lazy dog!"),
        _chunk(3, "completely unrelated content about machine learning models"),
    ]

    pairs = find_near_duplicate_pairs(chunks, similarity_threshold=0.95)

    assert len(pairs) == 1
    pair = pairs[0]
    assert pair.merge_target == "merge_chunks:1,2"
    assert pair.similarity >= 0.95


def test_find_near_duplicate_pairs_empty_when_below_threshold() -> None:
    chunks = [
        _chunk(1, "alpha beta gamma delta epsilon"),
        _chunk(2, "alpha foo bar baz qux quux corge"),
    ]

    pairs = find_near_duplicate_pairs(chunks, similarity_threshold=0.95)

    assert pairs == []


def test_find_near_duplicate_pairs_skips_short_chunks() -> None:
    # Both chunks tokenise to {"hi"}; without min_token_count they'd match.
    chunks = [_chunk(1, "hi"), _chunk(2, "hi")]

    pairs = find_near_duplicate_pairs(
        chunks, similarity_threshold=0.95, min_token_count=4
    )

    assert pairs == []


def test_find_near_duplicate_pairs_sorted_by_similarity_desc() -> None:
    chunks = [
        _chunk(1, "alpha beta gamma delta epsilon zeta eta theta"),
        _chunk(2, "alpha beta gamma delta epsilon zeta eta theta"),  # 1.00 vs 1
        _chunk(3, "alpha beta gamma delta epsilon zeta eta IOTA"),  # ~0.78 vs 1
    ]

    pairs = find_near_duplicate_pairs(chunks, similarity_threshold=0.5)

    # Sort: 1<>2 first (1.00), then 1<>3 / 2<>3 (lower).
    assert pairs[0].merge_target == "merge_chunks:1,2"
    assert pairs[0].similarity == 1.0
    assert all(pairs[i].similarity >= pairs[i + 1].similarity for i in range(len(pairs) - 1))


def test_merge_target_normalises_id_order() -> None:
    pair = DuplicatePair(chunk_a=10, chunk_b=3, similarity=0.99)

    assert pair.merge_target == "merge_chunks:3,10"


def test_reasoning_for_mentions_both_ids() -> None:
    pair = DuplicatePair(chunk_a=42, chunk_b=43, similarity=0.97)

    msg = reasoning_for(pair)

    assert "42" in msg
    assert "43" in msg
    assert "97" in msg  # percent rendering


def test_similarity_threshold_out_of_range_raises() -> None:
    with pytest.raises(ValueError):
        find_near_duplicate_pairs([], similarity_threshold=1.5)


# ---------------------------------------------------------------------------
# Integration with KbStore — the task spec asks specifically for "mock
# kb.sqlite with 2 near-duplicate chunks → 1 memory_op proposal generated".
# That whole path is covered in test_engine.py; here we just verify the
# detector talks to a real KbStore correctly.
# ---------------------------------------------------------------------------


async def test_kb_store_round_trip_finds_duplicate(kb_db: Path) -> None:
    insert_chunk(kb_db, content="the quick brown fox jumps over the lazy dog")
    insert_chunk(kb_db, content="the quick brown fox jumps over the lazy dog!")
    insert_chunk(kb_db, content="entirely different text about distributed systems")

    async with KbStore(kb_db) as kb:
        chunks = await kb.list_chunks()

    pairs = find_near_duplicate_pairs(chunks, similarity_threshold=0.95)

    assert len(pairs) == 1
    assert pairs[0].similarity >= 0.95
