"""Port of Rust ``tests/protocol_round_trip.rs``.

Pins the JSON wire shape so the gateway / producers can bind to a stable
contract.
"""

from __future__ import annotations

import json

import pytest

from corlinman_canvas import (
    AdapterError,
    ArtifactKind,
    CanvasPresentPayload,
    CodeBody,
    LatexBody,
    MermaidBody,
    Renderer,
    SparklineBody,
    TableBody,
    ThemeClass,
    UnknownKind,
)


@pytest.mark.parametrize(
    "payload",
    [
        CanvasPresentPayload(
            artifact_kind=ArtifactKind.CODE,
            body=CodeBody(language="rust", source="fn main(){}"),
            idempotency_key="art_code_1",
            theme_hint=ThemeClass.TP_DARK,
        ),
        CanvasPresentPayload(
            artifact_kind=ArtifactKind.MERMAID,
            body=MermaidBody(diagram="graph LR; A-->B"),
            idempotency_key="art_mermaid_1",
            theme_hint=None,
        ),
        CanvasPresentPayload(
            artifact_kind=ArtifactKind.TABLE,
            body=TableBody(markdown="| a | b |\n|---|---|\n| 1 | 2 |"),
            idempotency_key="art_table_1",
            theme_hint=ThemeClass.TP_LIGHT,
        ),
        CanvasPresentPayload(
            artifact_kind=ArtifactKind.LATEX,
            body=LatexBody(tex="E = mc^2", display=True),
            idempotency_key="art_latex_1",
            theme_hint=None,
        ),
        CanvasPresentPayload(
            artifact_kind=ArtifactKind.SPARKLINE,
            body=SparklineBody(values=(1.0, 4.0, 2.0, 9.0), unit="ms"),
            idempotency_key="art_spark_1",
            theme_hint=None,
        ),
    ],
)
def test_payload_round_trip(payload: CanvasPresentPayload) -> None:
    js = json.dumps(payload.to_json())
    restored = CanvasPresentPayload.from_json(json.loads(js))
    assert restored == payload
    # The wire form has the discriminator at the top.
    assert '"artifact_kind"' in js


def test_table_csv_round_trip() -> None:
    payload = CanvasPresentPayload(
        artifact_kind=ArtifactKind.TABLE,
        body=TableBody(csv="a,b\n1,2"),
        idempotency_key="art_table_csv",
    )
    restored = CanvasPresentPayload.from_json(payload.to_json())
    assert restored == payload


def test_unknown_kind_round_trips_as_error() -> None:
    raw = {
        "artifact_kind": "klingon",
        "body": {"language": "rust", "source": "fn main(){}"},
        "idempotency_key": "art_unknown_1",
    }
    with pytest.raises(UnknownKind):
        CanvasPresentPayload.from_json(raw)


def test_renderer_dispatch_is_exhaustive() -> None:
    # Every kind reaches a real adapter; mermaid in particular does
    # not raise UnimplementedKind here (it returns a fallback artifact).
    renderer = Renderer()
    out = renderer.render(
        CanvasPresentPayload(
            artifact_kind=ArtifactKind.MERMAID,
            body=MermaidBody(diagram="graph LR; A-->B"),
            idempotency_key="art_mermaid_stub",
        )
    )
    assert out.render_kind == ArtifactKind.MERMAID


def test_protocol_unknown_top_level_field_rejected() -> None:
    raw = {
        "artifact_kind": "code",
        "body": {"language": "rust", "source": "fn main(){}"},
        "idempotency_key": "art_typo_1",
        "themehint": "tp-dark",
    }
    with pytest.raises(AdapterError):
        CanvasPresentPayload.from_json(raw)


def test_protocol_latex_body_not_misclassified_as_table() -> None:
    raw = {
        "artifact_kind": "latex",
        "body": {"tex": "E = mc^2", "display": True},
        "idempotency_key": "art_latex_regress",
    }
    payload = CanvasPresentPayload.from_json(raw)
    assert isinstance(payload.body, LatexBody)
    assert payload.body.tex == "E = mc^2"
    assert payload.body.display is True


def test_protocol_body_kind_mismatch_rejected() -> None:
    # Says mermaid but the body is a code body — unknown fields trigger
    # the deny_unknown_fields gate.
    raw = {
        "artifact_kind": "mermaid",
        "body": {"language": "rust", "source": "fn main(){}"},
        "idempotency_key": "art_mismatch",
    }
    with pytest.raises(AdapterError):
        CanvasPresentPayload.from_json(raw)


def test_artifact_kind_wire_names_stable() -> None:
    assert ArtifactKind.CODE.as_str() == "code"
    assert ArtifactKind.MERMAID.as_str() == "mermaid"
    assert ArtifactKind.TABLE.as_str() == "table"
    assert ArtifactKind.LATEX.as_str() == "latex"
    assert ArtifactKind.SPARKLINE.as_str() == "sparkline"
