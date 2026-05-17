"""Usearch header probe — mirror of `header.rs` tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from corlinman_embedding.vector.header import (
    probe_and_convert_if_needed,
    probe_usearch_header,
)
from corlinman_embedding.vector.hnsw_store import UsearchIndex


def test_probe_reads_dim_and_count(tmp_path: Path) -> None:
    path = tmp_path / "probe.usearch"
    idx = UsearchIndex.create_with_capacity(8, 4)
    idx.add(1, [0.0] * 8)
    idx.add(2, [1.0] * 8)
    idx.save(path)
    h = probe_usearch_header(path)
    assert h.dim == 8
    assert h.count == 2
    assert h.version  # non-empty


def test_probe_and_convert_matching_dim_is_ok(tmp_path: Path) -> None:
    path = tmp_path / "ok.usearch"
    idx = UsearchIndex.create_with_capacity(4, 4)
    idx.save(path)
    probe_and_convert_if_needed(path, 4)  # no raise


def test_probe_and_convert_dim_mismatch_raises(tmp_path: Path) -> None:
    path = tmp_path / "mismatch.usearch"
    idx = UsearchIndex.create_with_capacity(4, 4)
    idx.save(path)
    with pytest.raises(RuntimeError, match="dim mismatch"):
        probe_and_convert_if_needed(path, 5)


def test_probe_and_convert_missing_file_is_noop(tmp_path: Path) -> None:
    probe_and_convert_if_needed(tmp_path / "nope.usearch", 8)
