"""`code` artifact adapter — Pygments, class-based emission.

Python port of Rust ``adapters/code.rs``. Replaces ``syntect`` with
``pygments``; both emit HTML class names (no inline colours), satisfying
the Tidepool "class-only" rule.

Design notes (carried over from Rust):

- Class prefix on every token is ``cn-canvas-code-`` so Tidepool CSS
  (``cn-canvas-code-k {color: var(--tp-keyword);}`` etc.) can re-paint
  the same HTML for light/dark themes without re-rendering.
- Unknown ``language`` is **not** an error — fall back to a plain
  ``<pre>`` with a warning. Mirrors Rust's
  ``code_unsupported_language_fallback`` test.
- Producer source is HTML-escaped on *both* paths: pygments escapes
  internally on the highlight path; we apply our own escape for the
  fallback path.

HTML divergence vs Rust: Pygments token class names are short
(``cn-canvas-code-k``, ``cn-canvas-code-mi``); syntect emits longer
Sublime-style scopes (``cn-canvas-code-source.rust``). The Rust tests
only assert the *prefix* is present — that contract holds.
"""

from __future__ import annotations

from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound

from .protocol import (
    AdapterError,
    ArtifactKind,
    RenderedArtifact,
    ThemeClass,
)

CLASS_PREFIX = "cn-canvas-code-"
WRAPPER_CLASS = "cn-canvas-code"


def _html_escape(source: str) -> str:
    """Five-char OWASP HTML body escape — mirrors the Rust adapter."""
    out: list[str] = []
    for ch in source:
        if ch == "&":
            out.append("&amp;")
        elif ch == "<":
            out.append("&lt;")
        elif ch == ">":
            out.append("&gt;")
        elif ch == '"':
            out.append("&quot;")
        elif ch == "'":
            out.append("&#39;")
        else:
            out.append(ch)
    return "".join(out)


def render(
    language: str, source: str, theme_class: ThemeClass
) -> RenderedArtifact:
    """Render a ``code`` artifact.

    Inputs are pre-validated by :func:`CanvasPresentPayload.from_json`,
    so ``language`` and ``source`` are guaranteed strings.
    """

    warnings: list[str] = []

    try:
        lexer = get_lexer_by_name(language)
    except ClassNotFound:
        lexer = None

    if lexer is not None:
        formatter = HtmlFormatter(
            nowrap=True,
            classprefix=CLASS_PREFIX,
        )
        try:
            inner = highlight(source, lexer, formatter)
        except Exception as exc:  # pragma: no cover - defensive
            raise AdapterError(
                ArtifactKind.CODE,
                f"pygments highlight failed: {exc}",
            ) from exc
        # Pygments appends a trailing newline; preserve it inside the
        # <code> the way syntect's ClassedHTMLGenerator does.
        html = f'<pre class="{WRAPPER_CLASS}"><code>{inner}</code></pre>'
    else:
        warnings.append(
            f"language `{language}` not recognised; rendered as plain text"
        )
        escaped = _html_escape(source)
        html = (
            f'<pre class="{WRAPPER_CLASS} {WRAPPER_CLASS}--plain">'
            f"<code>{escaped}</code></pre>"
        )

    return RenderedArtifact(
        html_fragment=html,
        theme_class=theme_class,
        render_kind=ArtifactKind.CODE,
        content_hash="",  # populated by Renderer.render
        warnings=tuple(warnings),
    )


__all__ = ["CLASS_PREFIX", "WRAPPER_CLASS", "render"]
