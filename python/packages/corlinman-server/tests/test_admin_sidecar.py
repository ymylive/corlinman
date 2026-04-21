"""Tests for the Feature C admin HTTP sidecar.

Covers the core ``_run_benchmark`` coroutine since that's the piece that
matters — it owns py-config parsing, provider instantiation, and bridging
to :func:`corlinman_embedding.benchmark_embedding`. The HTTP wrapper is
thin stdlib glue; we don't retest ``http.server`` itself.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from corlinman_server.admin_sidecar import _run_benchmark


def _write_py_config(tmp_path: Path, providers: list[dict[str, Any]], embedding: dict[str, Any] | None) -> Path:
    """Shared fixture — drop a py-config.json the sidecar can read."""
    path = tmp_path / "py-config.json"
    path.write_text(
        json.dumps({"providers": providers, "aliases": {}, "embedding": embedding}),
        encoding="utf-8",
    )
    return path


@pytest.mark.asyncio
async def test_run_benchmark_happy_path_with_mock_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: py-config points at an openai slot, we monkeypatch the
    provider's ``embed`` to return deterministic vectors, assert the
    ``BenchmarkView`` shape comes back."""
    path = _write_py_config(
        tmp_path,
        providers=[
            {
                "name": "openai",
                "kind": "openai",
                "api_key": "sk-test",
                "base_url": None,
                "enabled": True,
                "params": {},
            }
        ],
        embedding={
            "provider": "openai",
            "model": "text-embedding-3-small",
            "dimension": 3,
            "enabled": True,
            "params": {},
        },
    )
    monkeypatch.setenv("CORLINMAN_PY_CONFIG", str(path))

    # Monkeypatch the provider's embed so we don't hit the network.
    vectors = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
    call_idx = {"i": 0}

    async def fake_embed(self: Any, texts: list[str], *, dimension: int, params: dict[str, Any] | None = None) -> list[list[float]]:
        _ = self, texts, dimension, params
        vec = vectors[call_idx["i"]]
        call_idx["i"] += 1
        return [vec]

    with patch(
        "corlinman_embedding.OpenAICompatibleEmbeddingProvider.embed",
        fake_embed,
    ):
        result = await _run_benchmark({"samples": ["hello", "world"]})

    assert result["dimension"] == 3
    assert len(result["similarity_matrix"]) == 2
    assert result["similarity_matrix"][0][0] == pytest.approx(1.0)
    assert result["similarity_matrix"][0][1] == pytest.approx(0.0)  # orthogonal
    assert result["latency_ms_p50"] >= 0.0
    assert result["warnings"] == []


@pytest.mark.asyncio
async def test_run_benchmark_rejects_empty_samples() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        await _run_benchmark({"samples": []})


@pytest.mark.asyncio
async def test_run_benchmark_rejects_non_list_samples() -> None:
    with pytest.raises(ValueError):
        await _run_benchmark({"samples": "not-a-list"})  # type: ignore[dict-item]


@pytest.mark.asyncio
async def test_run_benchmark_rejects_when_no_py_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORLINMAN_PY_CONFIG", raising=False)
    with pytest.raises(RuntimeError, match="py-config not available"):
        await _run_benchmark({"samples": ["hi"]})


@pytest.mark.asyncio
async def test_run_benchmark_rejects_when_embedding_section_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_py_config(tmp_path, providers=[], embedding=None)
    monkeypatch.setenv("CORLINMAN_PY_CONFIG", str(path))
    with pytest.raises(RuntimeError, match="no enabled"):
        await _run_benchmark({"samples": ["hi"]})


@pytest.mark.asyncio
async def test_run_benchmark_honours_dimension_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Caller can override `dimension` to dry-run a dimension change."""
    path = _write_py_config(
        tmp_path,
        providers=[
            {
                "name": "openai",
                "kind": "openai",
                "api_key": "sk-test",
                "base_url": None,
                "enabled": True,
                "params": {},
            }
        ],
        embedding={
            "provider": "openai",
            "model": "text-embedding-3-small",
            "dimension": 1536,  # configured
            "enabled": True,
            "params": {},
        },
    )
    monkeypatch.setenv("CORLINMAN_PY_CONFIG", str(path))

    observed: dict[str, int] = {}

    async def fake_embed(self: Any, texts: list[str], *, dimension: int, params: dict[str, Any] | None = None) -> list[list[float]]:
        _ = self, texts, params
        observed["dim"] = dimension
        return [[1.0] * dimension]

    with patch(
        "corlinman_embedding.OpenAICompatibleEmbeddingProvider.embed",
        fake_embed,
    ):
        await _run_benchmark({"samples": ["hi"], "dimension": 8})

    assert observed["dim"] == 8


@pytest.mark.asyncio
async def test_run_benchmark_cleans_up_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanity — a stale env var shouldn't leak into later tests."""
    monkeypatch.delenv("CORLINMAN_PY_CONFIG", raising=False)
    assert "CORLINMAN_PY_CONFIG" not in os.environ
