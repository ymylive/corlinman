"""Content-addressed in-memory cache — Python port of ``cache.rs``.

Mirrors the Rust crate's shape:

    cache_key = blake2b( artifact_kind || canonical_json(body)
                       || theme_tag || RENDERER_VERSION )

Notes vs the Rust crate:

- Rust uses ``blake3`` (pure Rust, no C deps). Python's stdlib ships
  ``hashlib.blake2b`` which is C-backed and ubiquitous; we configure
  it with ``digest_size=32`` so the digest is the same byte count
  (32 bytes / 64 hex chars) and the ``content_hash`` field stays
  shape-compatible across the two ports.
- ``capacity == 0`` matches the Rust kill-switch — disabled cache,
  no allocation, every ``get`` misses.
- LRU implemented via ``collections.OrderedDict`` (move-to-end on hit,
  pop-oldest on overflow). One lock guards the whole table; the
  protected section is two dict ops — dwarfed by render work.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

from .protocol import (
    ArtifactBody,
    ArtifactKind,
    CodeBody,
    LatexBody,
    MermaidBody,
    RenderedArtifact,
    SparklineBody,
    TableBody,
    ThemeClass,
)

#: Bump when rendering output bytes change for the same input (Pygments
#: style swap, mermaid backend change, etc.). Old keys instantly miss.
RENDERER_VERSION: int = 1

CacheKey = bytes  # 32-byte digest


class RenderCache:
    """Content-addressed LRU. Cheap to copy (single mutex + dict)."""

    def __init__(self, capacity: int = 0) -> None:
        self._capacity = max(0, int(capacity))
        self._lock = threading.Lock()
        # ``None`` -> disabled. Constructed once; never flips at runtime.
        self._inner: OrderedDict[CacheKey, RenderedArtifact] | None
        self._inner = OrderedDict() if self._capacity > 0 else None

    @property
    def capacity(self) -> int:
        return self._capacity

    def is_disabled(self) -> bool:
        return self._inner is None

    def __len__(self) -> int:
        if self._inner is None:
            return 0
        with self._lock:
            return len(self._inner)

    def is_empty(self) -> bool:
        return len(self) == 0

    def get(self, key: CacheKey) -> RenderedArtifact | None:
        if self._inner is None:
            return None
        with self._lock:
            artifact = self._inner.get(key)
            if artifact is not None:
                # Touch -> most-recently-used.
                self._inner.move_to_end(key)
            return artifact

    def insert(self, key: CacheKey, artifact: RenderedArtifact) -> RenderedArtifact:
        if self._inner is None:
            return artifact
        with self._lock:
            if key in self._inner:
                self._inner.move_to_end(key)
            else:
                self._inner[key] = artifact
                while len(self._inner) > self._capacity:
                    self._inner.popitem(last=False)
        return artifact


# --- Key derivation ---------------------------------------------------------


def _theme_tag(theme: ThemeClass) -> str:
    return theme.value  # already 'tp-light' / 'tp-dark'


def canonical_json_bytes(body: ArtifactBody) -> bytes:
    """Canonicalise the body to a deterministic byte sequence.

    Object keys are sorted recursively so two semantically equal bodies
    hash identically regardless of producer field ordering.
    """

    value = _body_to_value(body)
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _body_to_value(body: ArtifactBody) -> Mapping[str, Any]:
    if isinstance(body, CodeBody):
        return {"language": body.language, "source": body.source}
    if isinstance(body, MermaidBody):
        return {"diagram": body.diagram}
    if isinstance(body, LatexBody):
        return {"tex": body.tex, "display": body.display}
    if isinstance(body, SparklineBody):
        out: dict[str, Any] = {"values": list(body.values)}
        if body.unit is not None:
            out["unit"] = body.unit
        return out
    if isinstance(body, TableBody):
        out2: dict[str, Any] = {}
        if body.markdown is not None:
            out2["markdown"] = body.markdown
        if body.csv is not None:
            out2["csv"] = body.csv
        return out2
    raise TypeError(f"unknown body type: {type(body).__name__}")


def key_for(kind: ArtifactKind, body: ArtifactBody, theme: ThemeClass) -> CacheKey:
    """Compute the cache key for a ``(kind, body, theme)`` triple.

    The output is also written into :attr:`RenderedArtifact.content_hash`
    (lower-case hex) so clients can dedup network responses without
    re-hashing the HTML fragment.
    """

    hasher = hashlib.blake2b(digest_size=32)
    hasher.update(RENDERER_VERSION.to_bytes(4, "little", signed=False))
    hasher.update(kind.as_str().encode("utf-8"))
    hasher.update(b"\x00")  # delimiter
    hasher.update(_theme_tag(theme).encode("utf-8"))
    hasher.update(b"\x00")
    hasher.update(canonical_json_bytes(body))
    return hasher.digest()


def key_to_hex(key: CacheKey) -> str:
    """Lower-case hex form of a :data:`CacheKey`. 64 chars."""
    return key.hex()


__all__ = [
    "RENDERER_VERSION",
    "CacheKey",
    "RenderCache",
    "canonical_json_bytes",
    "key_for",
    "key_to_hex",
]
