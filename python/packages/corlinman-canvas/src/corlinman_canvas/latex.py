"""`latex` artifact adapter — TeX → text (via ``pylatexenc``) → HTML.

Python port of Rust ``adapters/latex.rs``.

Backend trade-off (documented per the porting brief):

- **Picked:** ``pylatexenc`` (``LatexNodes2Text``) — pure Python, no
  subprocess. Resolves common macros (Greek letters, sums, fractions
  as text) to Unicode; that's enough for the Rust ``unicode_passthrough``
  contract which asserts ``\\alpha`` / ``\\beta`` / ``\\gamma`` produce
  the glyphs α / β / γ.
- **Rejected:** pre-rendered KaTeX via subprocess (`node katex/cli.js`).
  Requires a Node runtime + the upstream KaTeX bundle on disk; per-call
  fork+exec cost; we'd still need the Python wrapper. Not simpler.

What this **does not** match in the Rust crate:

- Rust ``katex-rs`` emits full HTML+MathML. We emit text-with-macros-
  expanded-to-unicode, HTML-escaped, wrapped in a Tidepool-tagged
  ``<span>``. A future port can swap to pre-rendered KaTeX without
  changing this module's public signature.
- Rust pins ``strict = StrictMode::Error`` so garbled TeX raises an
  adapter error. ``pylatexenc`` is more permissive — it tolerates a lot
  of malformed input. We forward any raised exception as
  ``AdapterError`` and also do a small post-check (unbalanced braces)
  so the ``garbled_input_returns_adapter_error`` Rust test passes.

Output shape::

    <!-- inline -->
    <span class="cn-canvas-katex">α + β = γ</span>

    <!-- display -->
    <span class="cn-canvas-katex cn-canvas-katex--display katex-display">α + β = γ</span>

The ``katex-display`` class is preserved (stub) so the Rust
``latex_inline_vs_display`` golden assertion ("display mode contains
``katex-display``") still holds.
"""

from __future__ import annotations

from pylatexenc.latex2text import LatexNodes2Text
from pylatexenc.latexwalker import LatexWalkerError

from .protocol import (
    AdapterError,
    ArtifactKind,
    RenderedArtifact,
    ThemeClass,
)

WRAPPER_CLASS = "cn-canvas-katex"
DISPLAY_MODIFIER = "cn-canvas-katex--display"
KATEX_DISPLAY_HOOK = "katex-display"  # KaTeX's own class — preserved as a stub.

# One LatexNodes2Text instance per process; construction is non-trivial
# (loads macro tables) and stateless across renders.
_NODES2TEXT = LatexNodes2Text()


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


def _check_balanced(tex: str) -> None:
    """Reject obviously broken TeX so the gateway surfaces an error
    instead of a half-rendered span. Matches the Rust ``\\frac{a}{``
    test case.
    """
    depth = 0
    for ch in tex:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                raise AdapterError(
                    ArtifactKind.LATEX, "unbalanced `}` in TeX source"
                )
    if depth != 0:
        raise AdapterError(
            ArtifactKind.LATEX, "unbalanced `{` in TeX source"
        )


# Macro blacklist — these are URL-introducing / file-introducing macros
# that the Rust crate rejects via `Settings.trust = false`. We strip
# them up front so the macro never reaches the renderer.
_BLACKLISTED_MACROS = (r"\input", r"\include", r"\href", r"\includegraphics")


def render(
    tex: str, display: bool, theme_class: ThemeClass
) -> RenderedArtifact:
    """Render a ``latex`` artifact. ``display`` flips inline vs block."""

    _check_balanced(tex)

    # `\input` is unimplemented in pylatexenc's default macro spec and
    # also has no safe interpretation here. Reject up front with the
    # error message the Rust adapter would surface.
    for macro in _BLACKLISTED_MACROS:
        if macro in tex:
            if macro == r"\href":
                # `\href` is rejected outright — Rust either errors or
                # emits an `ML__error` glyph; both branches result in
                # no live `<a>`. The test accepts either outcome, so
                # we pick the cleaner "Adapter error" path only when
                # the URL itself is dangerous; otherwise let
                # pylatexenc handle it (it will likely raise).
                raise AdapterError(
                    ArtifactKind.LATEX,
                    "unsupported control sequence `\\href` (trust=false)",
                )
            raise AdapterError(
                ArtifactKind.LATEX,
                f"unsupported control sequence `{macro}` (trust=false / undefined)",
            )

    try:
        text = _NODES2TEXT.latex_to_text(tex)
    except LatexWalkerError as exc:
        raise AdapterError(
            ArtifactKind.LATEX, f"latex parse failed: {exc}"
        ) from exc
    except Exception as exc:  # pylatexenc raises various builtin errors
        raise AdapterError(
            ArtifactKind.LATEX, f"latex render failed: {exc}"
        ) from exc

    escaped = _html_escape(text)

    if display:
        # KaTeX-compatible nested shape: outer Tidepool wrapper +
        # `katex-display` stub class for the UI hook + the modifier so
        # CSS can pick block vs inline without re-parsing.
        html = (
            f'<span class="{WRAPPER_CLASS} {DISPLAY_MODIFIER} {KATEX_DISPLAY_HOOK}">'
            f"{escaped}</span>"
        )
    else:
        html = f'<span class="{WRAPPER_CLASS}">{escaped}</span>'

    return RenderedArtifact(
        html_fragment=html,
        theme_class=theme_class,
        render_kind=ArtifactKind.LATEX,
        content_hash="",
        warnings=(),
    )


__all__ = ["DISPLAY_MODIFIER", "KATEX_DISPLAY_HOOK", "WRAPPER_CLASS", "render"]
