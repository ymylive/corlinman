"""Port of Rust ``tests/adapter_code.rs``.

Asserts the wire-level outputs the gateway and UI bind to. The Rust
tests only assert prefix / class presence (not full goldens) — the
Python output (Pygments) satisfies those prefix assertions even though
the token class names differ from syntect's.
"""

from __future__ import annotations

import pytest

from corlinman_canvas import (
    ArtifactKind,
    CanvasPresentPayload,
    CodeBody,
    RenderedArtifact,
    Renderer,
    ThemeClass,
)


def _render_code(language: str, source: str) -> RenderedArtifact:
    return Renderer().render(
        CanvasPresentPayload(
            artifact_kind=ArtifactKind.CODE,
            body=CodeBody(language=language, source=source),
            idempotency_key="art_test",
            theme_hint=ThemeClass.TP_DARK,
        )
    )


def test_code_round_trip_rust() -> None:
    out = _render_code("rust", "fn main() { let x = 42; }")
    assert out.render_kind == ArtifactKind.CODE
    assert out.theme_class == ThemeClass.TP_DARK
    assert out.html_fragment.startswith('<pre class="cn-canvas-code">')
    assert out.html_fragment.endswith("</code></pre>")
    assert "cn-canvas-code-" in out.html_fragment
    assert out.warnings == ()


def test_code_language_extension_alias_works() -> None:
    # Pygments recognises 'rs' as an alias for the Rust lexer.
    out = _render_code("rs", "fn main(){}")
    assert out.warnings == ()
    assert "cn-canvas-code-" in out.html_fragment


def test_code_unsupported_language_fallback() -> None:
    out = _render_code("klingon", "qaplaH'a'")
    assert out.render_kind == ArtifactKind.CODE
    assert "cn-canvas-code--plain" in out.html_fragment
    assert "cn-canvas-code-keyword" not in out.html_fragment
    # Source survives unaltered (apostrophes HTML-escaped).
    assert "qaplaH&#39;a&#39;" in out.html_fragment
    assert len(out.warnings) == 1
    assert "klingon" in out.warnings[0]


def test_code_html_escape_pygments_path() -> None:
    source = "// <script>alert('xss')</script>"
    out = _render_code("rust", source)
    assert "<script>" not in out.html_fragment
    assert "&lt;script&gt;" in out.html_fragment


def test_code_html_escape_plain_path() -> None:
    out = _render_code("klingon", "<img src=x onerror=alert(1)>")
    assert "<img" not in out.html_fragment
    assert "&lt;img" in out.html_fragment


def test_code_theme_passthrough() -> None:
    renderer = Renderer()

    dark = renderer.render(
        CanvasPresentPayload(
            artifact_kind=ArtifactKind.CODE,
            body=CodeBody(language="rust", source="fn x(){}"),
            idempotency_key="k1",
            theme_hint=ThemeClass.TP_DARK,
        )
    )
    assert dark.theme_class == ThemeClass.TP_DARK

    light = renderer.render(
        CanvasPresentPayload(
            artifact_kind=ArtifactKind.CODE,
            body=CodeBody(language="rust", source="fn x(){}"),
            idempotency_key="k2",
            theme_hint=None,
        )
    )
    assert light.theme_class == ThemeClass.TP_LIGHT

    # Class-only output: same source, same HTML across themes.
    assert dark.html_fragment == light.html_fragment


def test_code_empty_source_is_safe() -> None:
    out = _render_code("rust", "")
    assert '<pre class="cn-canvas-code"' in out.html_fragment
    assert "</code></pre>" in out.html_fragment


@pytest.mark.parametrize("language", ["python", "javascript", "go"])
def test_code_recognised_languages_emit_token_classes(language: str) -> None:
    out = _render_code(language, "x = 1\n")
    assert "cn-canvas-code-" in out.html_fragment
    assert out.warnings == ()
