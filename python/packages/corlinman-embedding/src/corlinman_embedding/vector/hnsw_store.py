"""Thin async wrapper around `usearch-python` 2.x — HNSW cosine index.

Mirrors the public surface of Rust's :mod:`corlinman_vector::usearch_index`:

- :meth:`UsearchIndex.create` / :meth:`UsearchIndex.create_with_capacity`
- :meth:`UsearchIndex.open` / :meth:`UsearchIndex.open_checked`
- :meth:`UsearchIndex.save`
- :meth:`UsearchIndex.add` / :meth:`UsearchIndex.upsert`
- :meth:`UsearchIndex.search`
- :attr:`size` / :attr:`dim`

Cosine metric (USearch ``MetricKind.Cos``, ``ScalarKind.F32``). ``search``
returns ``(key, distance)`` pairs with **smaller is more similar** — same as
Rust. Callers wanting similarity transform via ``1.0 - distance``.

The usearch CPython binding releases the GIL during HNSW work, so the
methods here are sync. The :class:`UsearchIndex` wraps them and exposes
``async`` mutators (``aadd`` / ``aupsert`` / ``asave``) for cases where the
caller is in an async context and wants to off-load via
``asyncio.to_thread``; the sync variants stay available for tests.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Sequence

import numpy as np
from usearch.index import Index, MetricKind, ScalarKind

__all__ = ["UsearchIndex", "DEFAULT_CAPACITY"]


#: Default HNSW capacity used when creating a fresh index.
DEFAULT_CAPACITY: int = 50_000


def _as_vector(vec: Sequence[float]) -> np.ndarray:
    arr = np.asarray(vec, dtype=np.float32)
    if arr.ndim != 1:
        raise ValueError(f"vector must be 1-D, got shape {arr.shape}")
    return arr


class UsearchIndex:
    """HNSW index over f32 cosine-distance vectors."""

    __slots__ = ("_index", "_dim")

    def __init__(self, index: Index, dim: int) -> None:
        self._index = index
        self._dim = dim

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def create(cls, dim: int) -> "UsearchIndex":
        """Create a new empty in-memory index with ``dim`` dimensions."""

        return cls.create_with_capacity(dim, DEFAULT_CAPACITY)

    @classmethod
    def create_with_capacity(cls, dim: int, capacity: int) -> "UsearchIndex":
        """Same as :meth:`create` but with a caller-supplied initial capacity.

        Note: ``usearch-python`` 2.x's high-level :class:`usearch.index.Index`
        wrapper auto-grows on demand and does not expose a ``reserve`` hook,
        so ``capacity`` is accepted for API parity with Rust but is otherwise
        a no-op hint.
        """

        index = Index(ndim=dim, metric=MetricKind.Cos, dtype=ScalarKind.F32)
        _ = capacity  # honoured implicitly via auto-grow; kept for API parity
        return cls(index, dim)

    @classmethod
    def open(cls, path: str | os.PathLike[str]) -> "UsearchIndex":
        """Open (load) an existing ``.usearch`` file."""

        p = str(Path(path))
        try:
            index = Index.restore(p)
        except Exception as exc:  # pragma: no cover - depends on usearch error type
            raise RuntimeError(f"usearch load({p}) failed: {exc}") from exc
        dim = int(index.ndim)
        if dim == 0:
            raise RuntimeError(f"loaded index reports dim=0 (corrupt file?): {p}")
        return cls(index, dim)

    @classmethod
    def open_checked(cls, path: str | os.PathLike[str], expected_dim: int) -> "UsearchIndex":
        """:meth:`open` with a dimension assertion."""

        idx = cls.open(path)
        if idx._dim != expected_dim:
            raise RuntimeError(
                f"usearch dim mismatch: file={idx._dim} expected={expected_dim}"
            )
        return idx

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def save(self, path: str | os.PathLike[str]) -> None:
        """Save the index to disk. Parent directory must exist."""

        self._index.save(str(Path(path)))

    def add(self, key: int, vector: Sequence[float]) -> None:
        """Insert a (key, vector) pair. Fails on duplicate keys."""

        arr = _as_vector(vector)
        if arr.shape[0] != self._dim:
            raise ValueError(
                f"dim mismatch on add: got {arr.shape[0]} want {self._dim}"
            )
        # usearch-python silently grows capacity on add, but we mirror
        # Rust's explicit reserve-on-overflow behaviour for parity + to
        # surface allocation failures synchronously.
        try:
            self._index.add(int(key), arr)
        except RuntimeError as exc:
            # USearch raises RuntimeError for duplicate keys via the
            # high-level wrapper; surface it with a clearer message.
            if "Duplicate keys" in str(exc):
                raise RuntimeError(f"usearch add(key={key}) failed: duplicate key") from exc
            raise

    def upsert(self, key: int, vector: Sequence[float]) -> None:
        """Idempotent insert: remove any existing entry for ``key``, then add."""

        # usearch-python's `remove` is a no-op when the key isn't present.
        try:
            self._index.remove(int(key))
        except Exception:  # pragma: no cover - defensive
            pass
        self.add(key, vector)

    def search(self, query: Sequence[float], k: int) -> list[tuple[int, float]]:
        """Query the top-``k`` nearest keys for ``query``.

        Returns ``(key, distance)`` pairs ordered best-first (smaller distance
        ⇒ more similar). With cosine metric the distance is ``1 -
        cosine_similarity``.
        """

        # Dimension check first — matches Rust's behaviour where a wrong-dim
        # query is an error even against an empty index.
        arr = _as_vector(query)
        if arr.shape[0] != self._dim:
            raise ValueError(
                f"dim mismatch on search: got {arr.shape[0]} want {self._dim}"
            )
        if k <= 0 or len(self._index) == 0:
            return []
        matches = self._index.search(arr, k)
        # `matches.keys` and `matches.distances` are numpy arrays.
        out: list[tuple[int, float]] = []
        keys = np.atleast_1d(matches.keys)
        dists = np.atleast_1d(matches.distances)
        for key, dist in zip(keys.tolist(), dists.tolist(), strict=False):
            out.append((int(key), float(dist)))
        return out

    # ------------------------------------------------------------------
    # Async wrappers — off-load CPU work to the default executor.
    # ------------------------------------------------------------------

    async def asave(self, path: str | os.PathLike[str]) -> None:
        await asyncio.to_thread(self.save, path)

    async def aadd(self, key: int, vector: Sequence[float]) -> None:
        await asyncio.to_thread(self.add, key, vector)

    async def aupsert(self, key: int, vector: Sequence[float]) -> None:
        await asyncio.to_thread(self.upsert, key, vector)

    async def asearch(self, query: Sequence[float], k: int) -> list[tuple[int, float]]:
        return await asyncio.to_thread(self.search, query, k)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def size(self) -> int:
        """Number of vectors currently indexed."""

        return int(len(self._index))

    @property
    def dim(self) -> int:
        """Vector dimensionality."""

        return self._dim

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"UsearchIndex(dim={self._dim}, size={self.size})"
