"""`mermaid` artifact adapter — client-side fallback.

The Rust crate sandboxes a vendored ``mermaid.min.js`` inside
``deno_core`` (V8) under ``--features mermaid``. None of the candidate
Python backends are available here:

- The PyPI ``mermaid-py`` package is not a workspace-blessed dep, and
  most variants either shell out to ``mmdc`` (not installed) or call
  the kroki.io HTTP endpoint (we shouldn't make network calls from a
  pure-function renderer).
- The ``mmdc`` CLI (mermaid-cli) is not on ``$PATH`` in this
  environment.

Per the porting brief: **do not silently drop**. We emit a
``<pre class="mermaid">{escaped src}</pre>`` fragment so a browser-side
``mermaid.js`` loaded by the UI shell can render it client-side. The
rendered artifact carries a ``warning`` so the gateway / UI know the
server did not pre-render the SVG.

TODO(corlinman-canvas): wire a real server-side renderer when one of
the following becomes available in the workspace:

  - the ``mermaid-cli`` (``mmdc``) binary on ``$PATH``;
  - a pure-Python mermaid renderer (none in 2026 yet);
  - the Rust crate's ``--features mermaid`` build, exposed via FFI.

The input is still capped (``DEFAULT_MAX_BYTES``) so a hostile producer
cannot ship a 1 MiB blob through the gateway just to hit the fallback.
"""

from __future__ import annotations

from .protocol import (
    ArtifactKind,
    BodyTooLarge,
    RenderedArtifact,
    ThemeClass,
)

DEFAULT_MAX_BYTES = 256 * 1024
DEFAULT_TIMEOUT_MS = 5_000

WRAPPER_CLASS = "cn-canvas-mermaid"
FALLBACK_WARNING = (
    "mermaid backend not configured server-side; emitted client-render "
    "fallback (browser-side mermaid.js renders the <pre class=\"mermaid\"> "
    "element). See corlinman_canvas.mermaid TODO."
)


def _html_escape(src: str) -> str:
    out: list[str] = []
    for ch in src:
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


def render(diagram: str, theme_class: ThemeClass) -> RenderedArtifact:
    """Render a ``mermaid`` artifact via the client-render fallback.

    Raises :class:`BodyTooLarge` if the diagram source exceeds the
    256 KiB ceiling (same as the Rust adapter's input cap, which
    applies regardless of feature state).
    """

    if len(diagram.encode("utf-8")) > DEFAULT_MAX_BYTES:
        raise BodyTooLarge(
            max_bytes=DEFAULT_MAX_BYTES, kind=ArtifactKind.MERMAID
        )

    escaped = _html_escape(diagram)
    # `<pre class="mermaid">` is the standard mermaid.js auto-init hook
    # — the browser-side library scans the document for that class and
    # replaces the element with the rendered SVG.
    html = (
        f'<pre class="{WRAPPER_CLASS} mermaid" data-theme="{theme_class.value}">'
        f"{escaped}</pre>"
    )

    return RenderedArtifact(
        html_fragment=html,
        theme_class=theme_class,
        render_kind=ArtifactKind.MERMAID,
        content_hash="",
        warnings=(FALLBACK_WARNING,),
    )


__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_TIMEOUT_MS",
    "FALLBACK_WARNING",
    "WRAPPER_CLASS",
    "render",
]
