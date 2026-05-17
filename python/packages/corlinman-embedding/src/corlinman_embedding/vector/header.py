"""Read-only metadata probe for ``.usearch`` index files — Python port.

Mirrors :mod:`corlinman_vector::header`. Used to detect embedding-dimension
drift between the live DB and an on-disk HNSW index after the embedding
model is swapped.

The format-version field is reserved for a future conversion hook —
usearch 2.x keeps metadata in the file header but doesn't expose a version
string through the Python binding, so we record the wrapper version as a
best-effort compatibility marker.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from corlinman_embedding.vector.hnsw_store import UsearchIndex

__all__ = ["UsearchHeader", "probe_usearch_header", "probe_and_convert_if_needed"]


@dataclass(frozen=True)
class UsearchHeader:
    """Metadata scraped from a ``.usearch`` file header without HNSW adoption."""

    dim: int
    version: str
    count: int


def probe_usearch_header(path: str | os.PathLike[str]) -> UsearchHeader:
    """Load a ``.usearch`` file's header metadata.

    Internally constructs a scratch ``Index`` and calls ``load()`` — usearch
    adopts the file's own header, so we can read ``dim`` and ``size`` back
    off the adopted index. The scratch instance is dropped on return.
    """

    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"usearch file missing: {p}")
    try:
        idx = UsearchIndex.open(p)
    except Exception as exc:
        raise RuntimeError(f"usearch load({p}) failed during probe: {exc}") from exc
    if idx.dim == 0:
        raise RuntimeError(f"usearch header reports dim=0 ({p}) — corrupt file?")
    # `usearch.__version__` isn't reliably exposed in 2.x; use a stable
    # marker matching the Rust crate's CARGO_PKG_VERSION convention.
    try:
        import usearch as _us

        ver = getattr(_us, "__version__", "0.0")
    except Exception:  # pragma: no cover
        ver = "0.0"
    return UsearchHeader(dim=idx.dim, version=str(ver), count=idx.size)


def probe_and_convert_if_needed(
    index_path: str | os.PathLike[str], expected_dim: int
) -> None:
    """Inspect the on-disk index; fail loudly on dimension disagreement.

    - Missing file → no-op (fresh install).
    - Matching dim → no-op.
    - Mismatched dim → raises :class:`RuntimeError` telling the operator to
      rebuild.

    Format-version conversion is reserved for a later sprint; usearch 2.x
    doesn't expose a file format marker to branch on.
    """

    p = Path(index_path)
    if not p.exists():
        return
    header = probe_usearch_header(p)
    if header.dim != expected_dim:
        raise RuntimeError(
            f"usearch dim mismatch at {p}: file={header.dim} "
            f"expected={expected_dim}; rebuild the HNSW index"
        )
    # TODO(S4): version-conversion hook once usearch exposes a binary
    # format marker.
