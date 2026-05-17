"""Port of Rust ``tests/adapter_table.rs``.

GFM markdown and CSV → ``<table>`` with the ``cn-canvas-table*`` class
scaffolding. Output is byte-equivalent to the Rust crate; the assertions
exercise the same shape.
"""

from __future__ import annotations

import pytest

from corlinman_canvas import (
    AdapterError,
    ArtifactKind,
    CanvasPresentPayload,
    Renderer,
    TableBody,
    ThemeClass,
)


def _render_table(body: TableBody) -> object:
    return Renderer().render(
        CanvasPresentPayload(
            artifact_kind=ArtifactKind.TABLE,
            body=body,
            idempotency_key="art_table_test",
            theme_hint=ThemeClass.TP_LIGHT,
        )
    )


def test_table_markdown_round_trip() -> None:
    md = (
        "| name | role | level |\n"
        "|------|------|-------|\n"
        "| ada  | dev  | 99    |\n"
        "| pi   | qa   | 7     |\n"
        "| zeta | ops  | 42    |\n"
    )
    out = _render_table(TableBody(markdown=md))
    html = out.html_fragment
    assert html.startswith('<table class="cn-canvas-table">')
    assert "<thead>" in html
    assert "</thead>" in html
    assert "<tbody>" in html
    assert html.endswith("</tbody></table>")
    assert '<th class="cn-canvas-table-cell">name</th>' in html
    assert '<td class="cn-canvas-table-cell">ada</td>' in html
    assert '<td class="cn-canvas-table-cell">zeta</td>' in html
    # 1 header row + 3 body rows = 4.
    assert html.count('<tr class="cn-canvas-table-row">') == 4


def test_table_csv_round_trip() -> None:
    csv = (
        "city,population,note\n"
        '"New York, NY",8000000,big\n'
        "Tokyo,13000000,bigger\n"
        '"London, UK",9000000,fog\n'
    )
    out = _render_table(TableBody(csv=csv))
    html = out.html_fragment
    assert '<table class="cn-canvas-table">' in html
    assert '<th class="cn-canvas-table-cell">city</th>' in html
    # Quoted field survives as one cell with the comma intact.
    assert '<td class="cn-canvas-table-cell">New York, NY</td>' in html
    assert '<td class="cn-canvas-table-cell">London, UK</td>' in html
    assert html.count('<tr class="cn-canvas-table-row">') == 4


def test_table_markdown_with_inline_code() -> None:
    md = (
        "| name | call |\n"
        "|------|------|\n"
        "| min  | `min(a, b)` |\n"
        "| max  | `max(a, b)` |\n"
    )
    out = _render_table(TableBody(markdown=md))
    assert "<code>min(a, b)</code>" in out.html_fragment
    assert "<code>max(a, b)</code>" in out.html_fragment


def test_table_both_sources_rejected() -> None:
    with pytest.raises(AdapterError) as exc:
        Renderer().render(
            CanvasPresentPayload(
                artifact_kind=ArtifactKind.TABLE,
                body=TableBody(markdown="| a |\n|---|\n| 1 |", csv="a\n1"),
                idempotency_key="art_dup",
            )
        )
    assert "exactly one" in str(exc.value)


def test_table_no_source_rejected() -> None:
    with pytest.raises(AdapterError):
        Renderer().render(
            CanvasPresentPayload(
                artifact_kind=ArtifactKind.TABLE,
                body=TableBody(),
                idempotency_key="art_empty",
            )
        )


def test_table_cell_content_escaped_markdown() -> None:
    md = (
        "| header |\n"
        "|--------|\n"
        "| <script>alert(1)</script> |\n"
    )
    out = _render_table(TableBody(markdown=md))
    assert "<script>" not in out.html_fragment
    assert "&lt;script&gt;" in out.html_fragment


def test_table_cell_content_escaped_csv() -> None:
    csv = "header\n<img src=x onerror=alert(1)>\n"
    out = _render_table(TableBody(csv=csv))
    assert "<img" not in out.html_fragment
    assert "&lt;img" in out.html_fragment


def test_table_markdown_without_table_rejected() -> None:
    with pytest.raises(AdapterError) as exc:
        Renderer().render(
            CanvasPresentPayload(
                artifact_kind=ArtifactKind.TABLE,
                body=TableBody(markdown="just a paragraph"),
                idempotency_key="art_no_table",
            )
        )
    assert "table" in str(exc.value).lower()


def test_table_render_kind_is_table() -> None:
    out = _render_table(TableBody(markdown="| a |\n|---|\n| 1 |"))
    assert out.render_kind == ArtifactKind.TABLE
    assert out.theme_class == ThemeClass.TP_LIGHT
