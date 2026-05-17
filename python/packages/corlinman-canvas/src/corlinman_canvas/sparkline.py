"""`sparkline` artifact adapter — hand-rolled inline SVG.

Direct port of Rust ``adapters/sparkline.rs``. No third-party deps —
pure stdlib (``math.isfinite``).

Geometry::

    W = (n - 1) * X_STEP          # n = values.len(), X_STEP = 8
    H = Y_HEIGHT = 24              # baseline = min, ceiling = max
    Y_PAD = 1.0                    # 1px inner padding top + bottom

Each value maps to ``(i * X_STEP, Y_HEIGHT - Y_PAD -
((v - min) / (max - min)) * (Y_HEIGHT - 2 * Y_PAD))``. Constant series
(``max == min``) flattens to ``Y_HEIGHT / 2`` so the line still has a
visible "rest" state.

Validation rules (match Rust):

- ``len(values) < 2`` → ``AdapterError``.
- ``len(values) > 1024`` → ``AdapterError``.
- Any non-finite (``NaN`` / ``±Infinity``) → ``AdapterError``.

Output shape (byte-equivalent to the Rust crate)::

    <svg class="cn-canvas-spark" viewBox="0 0 24 24" preserveAspectRatio="none"
         role="img" aria-label="sparkline (unit: ms): 1, 4, 2, 9">
      <title>sparkline (unit: ms): 1, 4, 2, 9</title>
      <path class="cn-canvas-spark-line" fill="none"
            d="M0.000,18.000L8.000,8.000L16.000,16.000L24.000,1.000"/>
    </svg>
"""

from __future__ import annotations

import math

from .protocol import (
    AdapterError,
    ArtifactKind,
    RenderedArtifact,
    ThemeClass,
)

WRAPPER_CLASS = "cn-canvas-spark"
LINE_CLASS = "cn-canvas-spark-line"

X_STEP = 8.0
Y_HEIGHT = 24.0
Y_PAD = 1.0
MAX_POINTS = 1024


def _push_escaped(out: list[str], src: str) -> None:
    """OWASP five-char HTML escape — used on the aria-label / title text."""
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


def _format_value(v: float) -> str:
    """Format an f64 the way Rust's ``{v}`` does for a float — integer
    values lose their trailing ``.0`` (``5.0`` -> ``5``) so the aria
    label text matches the Rust golden text byte-for-byte where it
    matters.
    """
    if v.is_integer():
        return str(int(v))
    return repr(v)


def _build_aria_label(values: list[float], unit: str | None) -> str:
    preview_n = min(len(values), 16)
    parts = ", ".join(_format_value(v) for v in values[:preview_n])
    if len(values) > preview_n:
        parts = parts + ", …"
    if unit:
        return f"sparkline (unit: {unit}): {parts}"
    return f"sparkline: {parts}"


def render(
    values: list[float] | tuple[float, ...],
    unit: str | None,
    theme_class: ThemeClass,
) -> RenderedArtifact:
    """Render a ``sparkline`` artifact. ``unit`` goes into ``<title>``
    and ``aria-label`` for screen readers.
    """

    series = list(values)

    if len(series) < 2:
        raise AdapterError(
            ArtifactKind.SPARKLINE,
            f"sparkline requires at least 2 points, got {len(series)}",
        )
    if len(series) > MAX_POINTS:
        raise AdapterError(
            ArtifactKind.SPARKLINE,
            f"sparkline exceeds {MAX_POINTS}-point cap (got {len(series)}); "
            "pre-aggregate at the producer",
        )
    if any(not math.isfinite(v) for v in series):
        raise AdapterError(
            ArtifactKind.SPARKLINE,
            "sparkline values must be finite (no NaN, no ±Infinity)",
        )

    lo = min(series)
    hi = max(series)
    plot_height = Y_HEIGHT - 2 * Y_PAD
    total_width = (len(series) - 1) * X_STEP
    span = hi - lo

    path_parts: list[str] = []
    for i, v in enumerate(series):
        x = i * X_STEP
        if span == 0.0:
            y = Y_HEIGHT / 2.0
        else:
            y = Y_HEIGHT - Y_PAD - ((v - lo) / span) * plot_height
        cmd = "M" if i == 0 else "L"
        path_parts.append(f"{cmd}{x:.3f},{y:.3f}")
    path = "".join(path_parts)

    label = _build_aria_label(series, unit)

    out: list[str] = []
    out.append(f'<svg class="{WRAPPER_CLASS}"')
    out.append(
        f' viewBox="0 0 {total_width:.3f} {Y_HEIGHT:.3f}"'
        ' preserveAspectRatio="none" role="img" aria-label="'
    )
    _push_escaped(out, label)
    out.append('">')
    out.append("<title>")
    _push_escaped(out, label)
    out.append("</title>")
    out.append(f'<path class="{LINE_CLASS}" fill="none" d="{path}"/>')
    out.append("</svg>")

    return RenderedArtifact(
        html_fragment="".join(out),
        theme_class=theme_class,
        render_kind=ArtifactKind.SPARKLINE,
        content_hash="",
        warnings=(),
    )


__all__ = [
    "LINE_CLASS",
    "MAX_POINTS",
    "WRAPPER_CLASS",
    "X_STEP",
    "Y_HEIGHT",
    "Y_PAD",
    "render",
]
