//! Iter 3 — table adapter tests.
//!
//! GFM markdown and CSV → `<table>` with `cn-canvas-table`,
//! `cn-canvas-table-row`, `cn-canvas-table-cell` class scaffolding.

use corlinman_canvas::{ArtifactBody, ArtifactKind, CanvasPresentPayload, Renderer, ThemeClass};

fn render_table(body: ArtifactBody) -> corlinman_canvas::RenderedArtifact {
    Renderer::new()
        .render(&CanvasPresentPayload {
            artifact_kind: ArtifactKind::Table,
            body,
            idempotency_key: "art_table_test".into(),
            theme_hint: Some(ThemeClass::TpLight),
        })
        .expect("render must succeed")
}

/// `table_markdown_round_trip` — 3x3 GFM table → `<table>` with
/// `<thead>` and `<tbody>`, all rows accounted for.
#[test]
fn table_markdown_round_trip() {
    let md = "\
| name | role | level |
|------|------|-------|
| ada  | dev  | 99    |
| pi   | qa   | 7     |
| zeta | ops  | 42    |
";
    let out = render_table(ArtifactBody::Table {
        markdown: Some(md.into()),
        csv: None,
    });
    let html = &out.html_fragment;
    assert!(html.starts_with("<table class=\"cn-canvas-table\">"));
    assert!(html.contains("<thead>"));
    assert!(html.contains("</thead>"));
    assert!(html.contains("<tbody>"));
    assert!(html.contains("</tbody></table>"));
    // Header cells use <th>, body cells <td> — and they all carry
    // the cell class.
    assert!(html.contains("<th class=\"cn-canvas-table-cell\">name</th>"));
    assert!(html.contains("<td class=\"cn-canvas-table-cell\">ada</td>"));
    assert!(html.contains("<td class=\"cn-canvas-table-cell\">zeta</td>"));
    // Row count: 1 header row + 3 body rows = 4.
    assert_eq!(
        html.matches("<tr class=\"cn-canvas-table-row\">").count(),
        4,
        "expected 4 rows (1 header + 3 body), got {}",
        html
    );
}

/// `table_csv_round_trip` — 3x3 CSV → `<table>` with same shape;
/// embedded comma in a quoted field survives.
#[test]
fn table_csv_round_trip() {
    let csv = "\
city,population,note
\"New York, NY\",8000000,big
Tokyo,13000000,bigger
\"London, UK\",9000000,fog
";
    let out = render_table(ArtifactBody::Table {
        markdown: None,
        csv: Some(csv.into()),
    });
    let html = &out.html_fragment;
    assert!(html.contains("<table class=\"cn-canvas-table\">"));
    assert!(html.contains("<th class=\"cn-canvas-table-cell\">city</th>"));
    // Quoted field survived as a single cell with the comma intact.
    assert!(html.contains("<td class=\"cn-canvas-table-cell\">New York, NY</td>"));
    assert!(html.contains("<td class=\"cn-canvas-table-cell\">London, UK</td>"));
    // 1 header + 3 data = 4 rows.
    assert_eq!(
        html.matches("<tr class=\"cn-canvas-table-row\">").count(),
        4
    );
}

/// `table_markdown_with_inline_code` — backtick spans inside cells
/// produce a small `<code>` tag inside the cell, content is HTML-
/// escaped.
#[test]
fn table_markdown_with_inline_code() {
    let md = "\
| name | call |
|------|------|
| min  | `min(a, b)` |
| max  | `max(a, b)` |
";
    let out = render_table(ArtifactBody::Table {
        markdown: Some(md.into()),
        csv: None,
    });
    let html = &out.html_fragment;
    assert!(html.contains("<code>min(a, b)</code>"));
    assert!(html.contains("<code>max(a, b)</code>"));
}

/// Producer sends both `markdown` and `csv` → adapter rejects.
#[test]
fn table_both_sources_rejected() {
    let result = Renderer::new().render(&CanvasPresentPayload {
        artifact_kind: ArtifactKind::Table,
        body: ArtifactBody::Table {
            markdown: Some("| a |\n|---|\n| 1 |".into()),
            csv: Some("a\n1".into()),
        },
        idempotency_key: "art_dup".into(),
        theme_hint: None,
    });
    let err = result.expect_err("both-sources must error");
    let msg = format!("{err}");
    assert!(msg.contains("exactly one"), "unexpected error: {msg}");
}

/// Producer sends neither — also rejected.
#[test]
fn table_no_source_rejected() {
    let result = Renderer::new().render(&CanvasPresentPayload {
        artifact_kind: ArtifactKind::Table,
        body: ArtifactBody::Table {
            markdown: None,
            csv: None,
        },
        idempotency_key: "art_empty".into(),
        theme_hint: None,
    });
    assert!(result.is_err(), "empty body must error");
}

/// HTML escape on cell content — both paths.
#[test]
fn table_cell_content_escaped() {
    let md = "\
| header |
|--------|
| <script>alert(1)</script> |
";
    let out = render_table(ArtifactBody::Table {
        markdown: Some(md.into()),
        csv: None,
    });
    assert!(
        !out.html_fragment.contains("<script>"),
        "raw <script> reached HTML: {}",
        out.html_fragment
    );
    assert!(out.html_fragment.contains("&lt;script&gt;"));

    let csv = "header\n<img src=x onerror=alert(1)>\n";
    let out = render_table(ArtifactBody::Table {
        markdown: None,
        csv: Some(csv.into()),
    });
    assert!(
        !out.html_fragment.contains("<img"),
        "raw <img> reached HTML: {}",
        out.html_fragment
    );
    assert!(out.html_fragment.contains("&lt;img"));
}

/// Markdown with no GFM table is an adapter error — the body
/// declared as a table but no `|`-separated content present.
#[test]
fn table_markdown_without_table_rejected() {
    let result = Renderer::new().render(&CanvasPresentPayload {
        artifact_kind: ArtifactKind::Table,
        body: ArtifactBody::Table {
            markdown: Some("just a paragraph".into()),
            csv: None,
        },
        idempotency_key: "art_no_table".into(),
        theme_hint: None,
    });
    let err = result.expect_err("non-table markdown must error");
    let msg = format!("{err}");
    assert!(
        msg.to_lowercase().contains("table"),
        "expected table-related error, got {msg}",
    );
}

/// Render kind is preserved on the output artifact.
#[test]
fn table_render_kind_is_table() {
    let out = render_table(ArtifactBody::Table {
        markdown: Some("| a |\n|---|\n| 1 |".into()),
        csv: None,
    });
    assert_eq!(out.render_kind, ArtifactKind::Table);
    assert_eq!(out.theme_class, ThemeClass::TpLight);
}
