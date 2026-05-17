"""``corlinman-canvas`` — Canvas Host renderer.

Python port of the Rust ``corlinman-canvas`` crate. Pure-function
transform from producer-submitted Canvas frame payloads (``code``,
``table``, ``latex``, ``sparkline``, ``mermaid``) into Tidepool-styled
HTML fragments suitable for the admin UI transcript and other Canvas
consumers.

Public surface (mirrors the Rust crate)::

    from corlinman_canvas import (
        Renderer,
        CanvasPresentPayload,
        ArtifactKind, ArtifactBody,
        CodeBody, TableBody, LatexBody, SparklineBody, MermaidBody,
        ThemeClass, RenderedArtifact,
        CanvasError, AdapterError, BodyTooLarge, UnknownKind,
        RenderCache, RENDERER_VERSION,
    )

The renderer dispatches on ``payload.artifact_kind``; the body's shape
is validated up-front by :meth:`CanvasPresentPayload.from_json`.
"""

from __future__ import annotations

from .cache import (
    RENDERER_VERSION,
    CacheKey,
    RenderCache,
    canonical_json_bytes,
    key_for as cache_key_for,
    key_to_hex,
)
from .protocol import (
    AdapterError,
    ArtifactBody,
    ArtifactKind,
    BodyTooLarge,
    CanvasError,
    CanvasPresentPayload,
    CodeBody,
    LatexBody,
    MermaidBody,
    RenderedArtifact,
    SparklineBody,
    TableBody,
    ThemeClass,
    TimeoutError_,
    UnimplementedKind,
    UnknownKind,
)
from .renderer import Renderer

__all__ = [
    "RENDERER_VERSION",
    "AdapterError",
    "ArtifactBody",
    "ArtifactKind",
    "BodyTooLarge",
    "CacheKey",
    "CanvasError",
    "CanvasPresentPayload",
    "CodeBody",
    "LatexBody",
    "MermaidBody",
    "RenderCache",
    "RenderedArtifact",
    "Renderer",
    "SparklineBody",
    "TableBody",
    "ThemeClass",
    "TimeoutError_",
    "UnimplementedKind",
    "UnknownKind",
    "cache_key_for",
    "canonical_json_bytes",
    "key_to_hex",
]
