"""Port of Rust ``tests/adapter_mermaid.rs`` — adapted for the Python
fallback path.

The Rust crate returns ``CanvasError::Adapter`` ("feature off") for
mermaid under the default build. The Python port instead emits a
``<pre class="mermaid">…</pre>`` fragment for browser-side rendering
(see the porting brief's "PRESERVE as a feature" rule and the
``mermaid.py`` TODO).

Invariants we still pin:

- Dispatch reaches the mermaid adapter (no ``UnimplementedKind`` raised).
- Oversized input is rejected before any adapter work
  (``BodyTooLarge``) — same 256 KiB ceiling as Rust.
- Output HTML escapes the diagram source — no raw ``<script>`` from
  a hostile diagram.
- Output carries a non-empty ``warnings`` tuple so the gateway / UI
  know the server did not pre-render the SVG.
"""

from __future__ import annotations

import pytest

from corlinman_canvas import (
    ArtifactKind,
    BodyTooLarge,
    CanvasPresentPayload,
    MermaidBody,
    Renderer,
    ThemeClass,
    UnimplementedKind,
)
from corlinman_canvas import mermaid as mermaid_mod


def _render_mermaid(diagram: str) -> object:
    return Renderer().render(
        CanvasPresentPayload(
            artifact_kind=ArtifactKind.MERMAID,
            body=MermaidBody(diagram=diagram),
            idempotency_key="art_mermaid_test",
            theme_hint=ThemeClass.TP_LIGHT,
        )
    )


def test_mermaid_default_emits_client_render_fallback() -> None:
    out = _render_mermaid("graph LR; A-->B")
    assert out.render_kind == ArtifactKind.MERMAID
    # Standard mermaid.js auto-init hook class.
    assert 'class="cn-canvas-mermaid mermaid"' in out.html_fragment
    # Browser-side renderer needs the raw (escaped) source visible.
    assert "graph LR; A--&gt;B" in out.html_fragment
    # Warning surfaced so the gateway can flag "client-side render".
    assert out.warnings
    assert "mermaid" in out.warnings[0].lower()


def test_mermaid_oversized_input_rejected_before_dispatch() -> None:
    # 256 KiB cap + 1 byte.
    huge = "x" * (mermaid_mod.DEFAULT_MAX_BYTES + 1)
    with pytest.raises(BodyTooLarge) as exc:
        _render_mermaid(huge)
    assert exc.value.kind == ArtifactKind.MERMAID
    assert exc.value.max_bytes == mermaid_mod.DEFAULT_MAX_BYTES


def test_mermaid_dispatch_never_raises_unimplemented() -> None:
    try:
        _render_mermaid("graph LR; A-->B")
    except UnimplementedKind as exc:  # pragma: no cover
        pytest.fail(f"UnimplementedKind reached dispatch path: {exc}")


def test_mermaid_html_escapes_diagram_source() -> None:
    # A hostile producer can't smuggle raw <script> through the fallback.
    out = _render_mermaid("<script>alert(1)</script>")
    assert "<script>" not in out.html_fragment
    assert "&lt;script&gt;" in out.html_fragment


def test_mermaid_theme_hint_round_trips_via_protocol() -> None:
    payload = CanvasPresentPayload(
        artifact_kind=ArtifactKind.MERMAID,
        body=MermaidBody(diagram="graph LR; A-->B"),
        idempotency_key="art_mermaid_theme",
        theme_hint=ThemeClass.TP_DARK,
    )
    restored = CanvasPresentPayload.from_json(payload.to_json())
    assert restored.theme_hint == ThemeClass.TP_DARK


def test_mermaid_theme_passthrough_to_artifact() -> None:
    out = Renderer().render(
        CanvasPresentPayload(
            artifact_kind=ArtifactKind.MERMAID,
            body=MermaidBody(diagram="graph LR; A-->B"),
            idempotency_key="art_mermaid_theme2",
            theme_hint=ThemeClass.TP_DARK,
        )
    )
    assert out.theme_class == ThemeClass.TP_DARK
    # The data-theme attribute carries the wire tag for client-side JS.
    assert 'data-theme="tp-dark"' in out.html_fragment
