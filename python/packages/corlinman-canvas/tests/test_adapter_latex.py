"""Port of Rust ``tests/adapter_latex.rs``.

The Python backend is ``pylatexenc`` (text + unicode-glyph resolution),
not ``katex-rs`` (HTML+MathML). The Rust assertions are still satisfied:

- ``latex_inline_vs_display`` — display mode contains both
  ``cn-canvas-katex--display`` and KaTeX's ``katex-display`` stub class.
- ``latex_macro_blacklist`` — ``\\href`` / ``\\input`` are rejected
  with :class:`AdapterError`; no live ``<a>`` ever reaches the DOM.
- ``latex_unicode_passthrough`` — ``\\alpha`` / ``\\beta`` / ``\\gamma``
  resolve to α / β / γ glyphs.
"""

from __future__ import annotations

import pytest

from corlinman_canvas import (
    AdapterError,
    ArtifactKind,
    CanvasPresentPayload,
    LatexBody,
    Renderer,
    ThemeClass,
)


def _render_latex(tex: str, display: bool) -> object:
    return Renderer().render(
        CanvasPresentPayload(
            artifact_kind=ArtifactKind.LATEX,
            body=LatexBody(tex=tex, display=display),
            idempotency_key="art_latex_test",
            theme_hint=ThemeClass.TP_LIGHT,
        )
    )


def test_latex_inline_vs_display() -> None:
    inline = _render_latex("a+b", False)
    assert "cn-canvas-katex" in inline.html_fragment
    assert "cn-canvas-katex--display" not in inline.html_fragment
    assert "katex-display" not in inline.html_fragment

    block = _render_latex("a+b", True)
    assert "cn-canvas-katex--display" in block.html_fragment
    assert "katex-display" in block.html_fragment


def test_latex_macro_blacklist_href() -> None:
    # \href must surface as AdapterError — no live <a>, no href="javascript:".
    with pytest.raises(AdapterError) as exc:
        Renderer().render(
            CanvasPresentPayload(
                artifact_kind=ArtifactKind.LATEX,
                body=LatexBody(tex=r"\href{javascript:alert(1)}{x}"),
                idempotency_key="art_href",
            )
        )
    assert exc.value.kind == ArtifactKind.LATEX
    # The Rust test's *real* assertions ('no <a>', 'no javascript: href',
    # 'no event handlers') are trivially satisfied because we never
    # produce HTML at all on the rejected path.


def test_latex_macro_blacklist_input() -> None:
    with pytest.raises(AdapterError) as exc:
        Renderer().render(
            CanvasPresentPayload(
                artifact_kind=ArtifactKind.LATEX,
                body=LatexBody(tex=r"\input{/etc/passwd}"),
                idempotency_key="art_input",
            )
        )
    assert exc.value.kind == ArtifactKind.LATEX
    msg = str(exc.value).lower()
    assert "input" in msg or "undefined" in msg or "control sequence" in msg


def test_latex_unicode_passthrough() -> None:
    out = _render_latex(r"\alpha + \beta = \gamma", False)
    html = out.html_fragment
    assert "α" in html
    assert "β" in html
    assert "γ" in html
    assert out.render_kind == ArtifactKind.LATEX


def test_latex_tidepool_wrapper_always_present() -> None:
    inline = _render_latex("1+1", False)
    display = _render_latex("1+1", True)
    assert inline.html_fragment.startswith('<span class="cn-canvas-katex')
    assert display.html_fragment.startswith('<span class="cn-canvas-katex')


def test_latex_garbled_input_returns_adapter_error() -> None:
    # Unmatched brace.
    with pytest.raises(AdapterError):
        Renderer().render(
            CanvasPresentPayload(
                artifact_kind=ArtifactKind.LATEX,
                body=LatexBody(tex=r"\frac{a}{"),
                idempotency_key="art_garbled",
            )
        )


def test_latex_theme_class_echoed() -> None:
    out = Renderer().render(
        CanvasPresentPayload(
            artifact_kind=ArtifactKind.LATEX,
            body=LatexBody(tex="x"),
            idempotency_key="art_theme",
            theme_hint=ThemeClass.TP_DARK,
        )
    )
    assert out.theme_class == ThemeClass.TP_DARK
