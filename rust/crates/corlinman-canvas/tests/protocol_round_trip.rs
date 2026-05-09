//! Iter 1 protocol surface tests.
//!
//! These tests pin the wire shape now so the gateway in iter 8 and
//! the producer-side skill in iter 10 can both bind to the same JSON
//! contract without re-derivation.

use corlinman_canvas::{
    ArtifactBody, ArtifactKind, CanvasError, CanvasPresentPayload, Renderer,
    ThemeClass,
};

/// `protocol_present_payload_round_trips` — every kind survives
/// serde-json round-trip with byte-stable shape.
#[test]
fn protocol_present_payload_round_trips() {
    let cases = vec![
        CanvasPresentPayload {
            artifact_kind: ArtifactKind::Code,
            body: ArtifactBody::Code {
                language: "rust".into(),
                source: "fn main(){}".into(),
            },
            idempotency_key: "art_code_1".into(),
            theme_hint: Some(ThemeClass::TpDark),
        },
        CanvasPresentPayload {
            artifact_kind: ArtifactKind::Mermaid,
            body: ArtifactBody::Mermaid {
                diagram: "graph LR; A-->B".into(),
            },
            idempotency_key: "art_mermaid_1".into(),
            theme_hint: None,
        },
        CanvasPresentPayload {
            artifact_kind: ArtifactKind::Table,
            body: ArtifactBody::Table {
                markdown: Some("| a | b |\n|---|---|\n| 1 | 2 |".into()),
                csv: None,
            },
            idempotency_key: "art_table_1".into(),
            theme_hint: Some(ThemeClass::TpLight),
        },
        CanvasPresentPayload {
            artifact_kind: ArtifactKind::Latex,
            body: ArtifactBody::Latex {
                tex: "E = mc^2".into(),
                display: true,
            },
            idempotency_key: "art_latex_1".into(),
            theme_hint: None,
        },
        CanvasPresentPayload {
            artifact_kind: ArtifactKind::Sparkline,
            body: ArtifactBody::Sparkline {
                values: vec![1.0, 4.0, 2.0, 9.0],
                unit: Some("ms".into()),
            },
            idempotency_key: "art_spark_1".into(),
            theme_hint: None,
        },
    ];

    for original in cases {
        let json = serde_json::to_string(&original).expect("serialise");
        let restored: CanvasPresentPayload =
            serde_json::from_str(&json).expect("deserialise");
        assert_eq!(
            original, restored,
            "round-trip mismatch for {:?}",
            original.artifact_kind
        );

        // The wire form should also be human-readable JSON with the
        // discriminator at the top — assert artifact_kind is present.
        assert!(
            json.contains("\"artifact_kind\""),
            "missing discriminator in {json}",
        );
    }
}

/// Verifies CSV-bodied table also round-trips (the `markdown`/`csv`
/// option fields are non-trivial to get right with `untagged`).
#[test]
fn protocol_table_csv_round_trip() {
    let original = CanvasPresentPayload {
        artifact_kind: ArtifactKind::Table,
        body: ArtifactBody::Table {
            markdown: None,
            csv: Some("a,b\n1,2".into()),
        },
        idempotency_key: "art_table_csv".into(),
        theme_hint: None,
    };
    let json = serde_json::to_string(&original).expect("serialise");
    let restored: CanvasPresentPayload =
        serde_json::from_str(&json).expect("deserialise");
    assert_eq!(original, restored);
}

/// `unknown_kind_round_trips_as_error` — payload with a bogus
/// `artifact_kind` fails deserialisation cleanly. The renderer never
/// has to handle the unknown variant; serde rejects it at the
/// gateway boundary.
#[test]
fn unknown_kind_round_trips_as_error() {
    let raw = r#"{
        "artifact_kind": "klingon",
        "body": { "language": "rust", "source": "fn main(){}" },
        "idempotency_key": "art_unknown_1"
    }"#;
    let result: Result<CanvasPresentPayload, _> = serde_json::from_str(raw);
    assert!(
        result.is_err(),
        "expected unknown artifact_kind to fail deserialisation"
    );
}

/// Renderer returns `Unimplemented` for kinds whose adapters
/// haven't shipped yet. Iter 2 wired `Code`; iter 3 wired `Table`;
/// iter 4 wired `Latex`. Mermaid / Sparkline remain stubbed until
/// iter 5-6.
#[test]
fn renderer_stub_returns_unimplemented_for_unwired_kinds() {
    let renderer = Renderer::new();

    // Iter 2 wired Code, iter 3 wired Table, iter 4 wired Latex.
    // Mermaid / Sparkline still stub.
    let unwired: Vec<(ArtifactKind, ArtifactBody)> = vec![
        (
            ArtifactKind::Mermaid,
            ArtifactBody::Mermaid {
                diagram: "graph LR; A-->B".into(),
            },
        ),
        (
            ArtifactKind::Sparkline,
            ArtifactBody::Sparkline {
                values: vec![1.0, 2.0],
                unit: None,
            },
        ),
    ];

    for (kind, body) in unwired {
        let payload = CanvasPresentPayload {
            artifact_kind: kind,
            body,
            idempotency_key: format!("art_stub_{}", kind.as_str()),
            theme_hint: None,
        };
        let err = renderer
            .render(&payload)
            .expect_err("stub must error for unwired kind");
        match err {
            CanvasError::Unimplemented { kind: got } => assert_eq!(got, kind),
            other => panic!("expected Unimplemented for {kind:?}, got {other:?}"),
        }
    }
}

/// `deny_unknown_fields` on the payload struct — typo-protection so
/// producers don't silently send a stale field name.
#[test]
fn protocol_unknown_top_level_field_rejected() {
    let raw = r#"{
        "artifact_kind": "code",
        "body": { "language": "rust", "source": "fn main(){}" },
        "idempotency_key": "art_typo_1",
        "themehint": "tp-dark"
    }"#;
    let result: Result<CanvasPresentPayload, _> = serde_json::from_str(raw);
    assert!(
        result.is_err(),
        "expected unknown top-level field to fail deserialisation"
    );
}

/// Regression: a Latex payload (`tex` field) must deserialise as the
/// Latex variant — not silently as `Table { markdown: None, csv: None
/// }` because Table has only optional fields. Caught by the iter 1
/// custom `Deserialize` impl on `CanvasPresentPayload`.
#[test]
fn protocol_latex_body_not_misclassified_as_table() {
    let raw = r#"{
        "artifact_kind": "latex",
        "body": { "tex": "E = mc^2", "display": true },
        "idempotency_key": "art_latex_regress"
    }"#;
    let payload: CanvasPresentPayload = serde_json::from_str(raw).expect("deserialise");
    match payload.body {
        ArtifactBody::Latex { tex, display } => {
            assert_eq!(tex, "E = mc^2");
            assert!(display);
        }
        other => panic!("expected Latex variant, got {other:?}"),
    }
}

/// Body shape mismatched against `artifact_kind` is a typed serde
/// error, not a misclassification.
#[test]
fn protocol_body_kind_mismatch_rejected() {
    // Says it's mermaid but the body is a code body.
    let raw = r#"{
        "artifact_kind": "mermaid",
        "body": { "language": "rust", "source": "fn main(){}" },
        "idempotency_key": "art_mismatch"
    }"#;
    let result: Result<CanvasPresentPayload, _> = serde_json::from_str(raw);
    assert!(
        result.is_err(),
        "expected mismatched body to fail, got {result:?}"
    );
}

/// `ArtifactKind::as_str` matches the wire form exactly.
#[test]
fn artifact_kind_wire_names_stable() {
    assert_eq!(ArtifactKind::Code.as_str(), "code");
    assert_eq!(ArtifactKind::Mermaid.as_str(), "mermaid");
    assert_eq!(ArtifactKind::Table.as_str(), "table");
    assert_eq!(ArtifactKind::Latex.as_str(), "latex");
    assert_eq!(ArtifactKind::Sparkline.as_str(), "sparkline");

    // Belt-and-braces: serde and `as_str` agree.
    let json = serde_json::to_string(&ArtifactKind::Sparkline).expect("ser");
    assert_eq!(json, "\"sparkline\"");
}
