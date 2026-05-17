"""Port of Rust ``tests/adapter_sparkline.rs``.

Same geometry, same validation, same aria-label text — byte-equivalent
SVG output with the same `M` / `L` path commands and `1.000` peak
coordinate at ``Y_PAD``.
"""

from __future__ import annotations

import math

import pytest

from corlinman_canvas import (
    AdapterError,
    ArtifactKind,
    CanvasPresentPayload,
    Renderer,
    SparklineBody,
    ThemeClass,
)


def _render_spark(values, unit=None) -> object:
    return Renderer().render(
        CanvasPresentPayload(
            artifact_kind=ArtifactKind.SPARKLINE,
            body=SparklineBody(values=tuple(values), unit=unit),
            idempotency_key="art_spark_test",
            theme_hint=ThemeClass.TP_LIGHT,
        )
    )


def test_sparkline_4_points() -> None:
    out = _render_spark([1.0, 4.0, 2.0, 9.0], "ms")
    html = out.html_fragment

    assert 'class="cn-canvas-spark"' in html
    assert 'class="cn-canvas-spark-line"' in html
    # 1 M (move-to) + 3 L (line-to) for a 4-point series.
    assert html.count("M") == 1
    assert html.count("L") == 3

    # Max should be at y == Y_PAD == 1.000.
    assert "1.000" in html

    # viewBox: 3 segments * 8 step = 24 units wide.
    assert 'viewBox="0 0 24' in html
    assert "unit: ms" in html
    assert out.render_kind == ArtifactKind.SPARKLINE


def test_sparkline_constant() -> None:
    out = _render_spark([5.0, 5.0, 5.0, 5.0])
    html = out.html_fragment
    # Every point at y == 12.000 (Y_HEIGHT/2).
    assert html.count("12.000") >= 4
    assert "NaN" not in html


def test_sparkline_empty_rejected_zero_points() -> None:
    with pytest.raises(AdapterError) as exc:
        Renderer().render(
            CanvasPresentPayload(
                artifact_kind=ArtifactKind.SPARKLINE,
                body=SparklineBody(values=()),
                idempotency_key="art_spark_empty",
            )
        )
    assert exc.value.kind == ArtifactKind.SPARKLINE


def test_sparkline_empty_rejected_one_point() -> None:
    with pytest.raises(AdapterError) as exc:
        Renderer().render(
            CanvasPresentPayload(
                artifact_kind=ArtifactKind.SPARKLINE,
                body=SparklineBody(values=(3.15,)),
                idempotency_key="art_spark_one",
            )
        )
    assert "at least 2" in str(exc.value)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_sparkline_non_finite_rejected(bad: float) -> None:
    with pytest.raises(AdapterError):
        Renderer().render(
            CanvasPresentPayload(
                artifact_kind=ArtifactKind.SPARKLINE,
                body=SparklineBody(values=(1.0, bad, 2.0)),
                idempotency_key="art_spark_bad",
            )
        )


def test_sparkline_oversized_rejected() -> None:
    values = tuple(float(i) for i in range(2000))
    with pytest.raises(AdapterError) as exc:
        Renderer().render(
            CanvasPresentPayload(
                artifact_kind=ArtifactKind.SPARKLINE,
                body=SparklineBody(values=values),
                idempotency_key="art_spark_big",
            )
        )
    assert "1024" in str(exc.value)


def test_sparkline_unit_html_escaped() -> None:
    out = _render_spark([1.0, 2.0], "<script>alert(1)</script>")
    html = out.html_fragment
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_sparkline_theme_class_echoed() -> None:
    out = Renderer().render(
        CanvasPresentPayload(
            artifact_kind=ArtifactKind.SPARKLINE,
            body=SparklineBody(values=(1.0, 2.0)),
            idempotency_key="art_spark_theme",
            theme_hint=ThemeClass.TP_DARK,
        )
    )
    assert out.theme_class == ThemeClass.TP_DARK
