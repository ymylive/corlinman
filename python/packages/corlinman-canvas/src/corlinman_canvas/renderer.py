"""Renderer entry point — Python port of ``lib.rs::Renderer``.

Dispatches on :attr:`CanvasPresentPayload.artifact_kind` to the matching
adapter module. Includes the iter-7 embedded :class:`RenderCache`; the
default constructor keeps the cache disabled so behaviour matches the
Rust ``Renderer::default()`` exactly.
"""

from __future__ import annotations

from . import cache as _cache
from . import code as _code
from . import latex as _latex
from . import mermaid as _mermaid
from . import sparkline as _sparkline
from . import table as _table
from .cache import RenderCache
from .protocol import (
    ArtifactBody,
    ArtifactKind,
    CanvasPresentPayload,
    CodeBody,
    LatexBody,
    MermaidBody,
    RenderedArtifact,
    SparklineBody,
    TableBody,
    ThemeClass,
)


class Renderer:
    """Stateless renderer. Cloning / re-instantiating is cheap — adapter
    state (Pygments lexers, pylatexenc tables) is module-level."""

    def __init__(self) -> None:
        self._cache = RenderCache(0)

    @classmethod
    def with_cache(cls, capacity: int) -> Renderer:
        """Construct a renderer backed by a fixed-capacity LRU. Capacity
        ``0`` disables the cache entirely (same as the default ctor).
        """
        inst = cls()
        inst._cache = RenderCache(capacity)
        return inst

    @property
    def cache(self) -> RenderCache:
        """Borrow the embedded cache (for stats / admin endpoints)."""
        return self._cache

    def render(self, payload: CanvasPresentPayload) -> RenderedArtifact:
        """Render a ``present``-frame payload to a single
        :class:`RenderedArtifact`. Pure for code/table/latex/sparkline;
        mermaid currently always returns a client-render fallback fragment.
        """

        theme = payload.theme_hint if payload.theme_hint is not None else ThemeClass.default()
        key = _cache.key_for(payload.artifact_kind, payload.body, theme)
        hit = self._cache.get(key)
        if hit is not None:
            return hit

        artifact = _dispatch(payload.artifact_kind, payload.body, theme)

        if not artifact.content_hash:
            artifact = _replace(artifact, content_hash=_cache.key_to_hex(key))

        if not self._cache.is_disabled():
            self._cache.insert(key, artifact)
        return artifact


def _replace(artifact: RenderedArtifact, **overrides: object) -> RenderedArtifact:
    """Frozen-dataclass copy helper — equivalent to dataclasses.replace
    but typed for clarity."""
    return RenderedArtifact(
        html_fragment=str(overrides.get("html_fragment", artifact.html_fragment)),
        theme_class=overrides.get("theme_class", artifact.theme_class),  # type: ignore[arg-type]
        render_kind=overrides.get("render_kind", artifact.render_kind),  # type: ignore[arg-type]
        content_hash=str(overrides.get("content_hash", artifact.content_hash)),
        warnings=overrides.get("warnings", artifact.warnings),  # type: ignore[arg-type]
    )


def _dispatch(
    kind: ArtifactKind, body: ArtifactBody, theme: ThemeClass
) -> RenderedArtifact:
    if isinstance(body, CodeBody):
        return _code.render(body.language, body.source, theme)
    if isinstance(body, TableBody):
        return _table.render(body.markdown, body.csv, theme)
    if isinstance(body, LatexBody):
        return _latex.render(body.tex, body.display, theme)
    if isinstance(body, SparklineBody):
        return _sparkline.render(list(body.values), body.unit, theme)
    if isinstance(body, MermaidBody):
        return _mermaid.render(body.diagram, theme)
    # Should be unreachable — kind/body always match after from_json.
    from .protocol import UnimplementedKind

    raise UnimplementedKind(kind)


__all__ = ["Renderer"]
