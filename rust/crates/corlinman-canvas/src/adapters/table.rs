//! `table` artifact adapter — GFM markdown or CSV → `<table>`.
//!
//! Producers send exactly one of `markdown` or `csv` in
//! [`ArtifactBody::Table`]; this adapter rejects the both-present
//! and neither-present cases with [`CanvasError::Adapter`]. The
//! protocol-level shape is enforced by serde; this is a semantic
//! check.
//!
//! ## Markdown path — `pulldown-cmark`
//!
//! GFM tables are parsed with the `Tables` extension on. We walk
//! the event stream ourselves rather than calling `push_html`
//! because:
//!
//! - `push_html` wraps cells in `<p>` tags. We want compact
//!   `<td>cell</td>`.
//! - We need to stamp `cn-canvas-table` / `cn-canvas-table-row` /
//!   `cn-canvas-table-cell` classes for Tidepool styling. The
//!   built-in writer doesn't expose hook points.
//!
//! Inline content (bold, code spans) inside a cell is rendered as
//! escaped text in this iter — full inline support is iter 4
//! (latex) territory and shares no code with the table walker.
//!
//! ## CSV path — `csv` crate
//!
//! Standard reader, headers from the first record. Quote handling
//! and embedded commas are the crate's job. We HTML-escape every
//! cell before emission.
//!
//! ## Output shape
//!
//! ```html
//! <table class="cn-canvas-table">
//!   <thead><tr class="cn-canvas-table-row">
//!     <th class="cn-canvas-table-cell">a</th> ...
//!   </tr></thead>
//!   <tbody>
//!     <tr class="cn-canvas-table-row">
//!       <td class="cn-canvas-table-cell">1</td> ...
//!     </tr>
//!   </tbody>
//! </table>
//! ```

use pulldown_cmark::{Event, Options, Parser, Tag, TagEnd};

use crate::protocol::{ArtifactKind, CanvasError, RenderedArtifact, ThemeClass};

/// Outer wrapper class. Pairs with `border-tp-glass-edge
/// bg-tp-glass-inner` rules in iter 9's globals.css.
const WRAPPER_CLASS: &str = "cn-canvas-table";
const ROW_CLASS: &str = "cn-canvas-table-row";
const CELL_CLASS: &str = "cn-canvas-table-cell";

/// Adapter entry. Exactly one of `markdown` / `csv` must be present.
pub fn render(
    markdown: Option<&str>,
    csv_src: Option<&str>,
    theme_class: ThemeClass,
) -> Result<RenderedArtifact, CanvasError> {
    let html = match (markdown, csv_src) {
        (Some(md), None) => render_markdown(md)?,
        (None, Some(csv)) => render_csv(csv)?,
        (Some(_), Some(_)) => {
            return Err(CanvasError::Adapter {
                kind: ArtifactKind::Table,
                message: "table body must specify exactly one of `markdown` or `csv`, not both"
                    .to_string(),
            });
        }
        (None, None) => {
            return Err(CanvasError::Adapter {
                kind: ArtifactKind::Table,
                message: "table body must specify either `markdown` or `csv`".to_string(),
            });
        }
    };

    Ok(RenderedArtifact {
        html_fragment: html,
        theme_class,
        content_hash: String::new(), // iter 7 (cache) populates
        render_kind: ArtifactKind::Table,
        warnings: Vec::new(),
    })
}

/// Walk the pulldown-cmark event stream and emit a single
/// `<table>` element. Non-table events (paragraphs around the
/// table, etc.) are ignored — producers should send only a table.
/// If no table event is seen at all, return an adapter error so
/// the producer learns about it.
fn render_markdown(source: &str) -> Result<String, CanvasError> {
    let mut opts = Options::empty();
    opts.insert(Options::ENABLE_TABLES);
    let parser = Parser::new_ext(source, opts);

    let mut html = String::new();
    let mut in_thead = false;
    let mut saw_table = false;

    for event in parser {
        match event {
            Event::Start(Tag::Table(_)) => {
                saw_table = true;
                html.push_str("<table class=\"");
                html.push_str(WRAPPER_CLASS);
                html.push_str("\">");
            }
            Event::End(TagEnd::Table) => {
                html.push_str("</table>");
            }
            Event::Start(Tag::TableHead) => {
                in_thead = true;
                html.push_str("<thead><tr class=\"");
                html.push_str(ROW_CLASS);
                html.push_str("\">");
            }
            Event::End(TagEnd::TableHead) => {
                in_thead = false;
                html.push_str("</tr></thead><tbody>");
            }
            Event::Start(Tag::TableRow) => {
                html.push_str("<tr class=\"");
                html.push_str(ROW_CLASS);
                html.push_str("\">");
            }
            Event::End(TagEnd::TableRow) => {
                html.push_str("</tr>");
            }
            Event::Start(Tag::TableCell) => {
                html.push_str(if in_thead {
                    "<th class=\""
                } else {
                    "<td class=\""
                });
                html.push_str(CELL_CLASS);
                html.push_str("\">");
            }
            Event::End(TagEnd::TableCell) => {
                html.push_str(if in_thead { "</th>" } else { "</td>" });
            }
            Event::Text(t) => {
                push_escaped(&mut html, &t);
            }
            Event::Code(c) => {
                // Inline `code` spans inside a cell — render as a
                // small `<code>` so they're at least
                // distinguishable; the cell class still applies.
                html.push_str("<code>");
                push_escaped(&mut html, &c);
                html.push_str("</code>");
            }
            Event::Html(raw) | Event::InlineHtml(raw) => {
                // pulldown-cmark passes through raw HTML as `Html`
                // events. We *never* emit raw HTML inside a cell —
                // a `<script>` in a producer's table source must
                // surface as escaped text, not executable markup.
                push_escaped(&mut html, &raw);
            }
            // Drop everything else (SoftBreak, HardBreak,
            // FootnoteReference, …) — out of scope for table
            // cells. iter 4 (latex) revisits inline rendering.
            _ => {}
        }
    }

    // pulldown-cmark closes `<tbody>` implicitly via the parser
    // state machine; we matched only on Tag, so we have to close
    // ourselves.
    if let Some(idx) = html.rfind("</table>") {
        // Insert `</tbody>` before the closing `</table>` if not
        // already there. Cheap, correct.
        if !html[..idx].ends_with("</tbody>") {
            html.replace_range(idx..idx, "</tbody>");
        }
    }

    if !saw_table {
        return Err(CanvasError::Adapter {
            kind: ArtifactKind::Table,
            message: "markdown source contained no GFM table".to_string(),
        });
    }
    Ok(html)
}

/// CSV → `<table>`. First record is the header.
fn render_csv(source: &str) -> Result<String, CanvasError> {
    let mut reader = csv::ReaderBuilder::new()
        .has_headers(true)
        .flexible(true)
        .from_reader(source.as_bytes());

    let headers = reader
        .headers()
        .map_err(|e| CanvasError::Adapter {
            kind: ArtifactKind::Table,
            message: format!("CSV header parse failed: {e}"),
        })?
        .clone();

    let mut html = String::new();
    html.push_str("<table class=\"");
    html.push_str(WRAPPER_CLASS);
    html.push_str("\">");

    if !headers.is_empty() {
        html.push_str("<thead><tr class=\"");
        html.push_str(ROW_CLASS);
        html.push_str("\">");
        for cell in &headers {
            html.push_str("<th class=\"");
            html.push_str(CELL_CLASS);
            html.push_str("\">");
            push_escaped(&mut html, cell);
            html.push_str("</th>");
        }
        html.push_str("</tr></thead>");
    }

    html.push_str("<tbody>");
    for record in reader.records() {
        let record = record.map_err(|e| CanvasError::Adapter {
            kind: ArtifactKind::Table,
            message: format!("CSV row parse failed: {e}"),
        })?;
        html.push_str("<tr class=\"");
        html.push_str(ROW_CLASS);
        html.push_str("\">");
        for cell in &record {
            html.push_str("<td class=\"");
            html.push_str(CELL_CLASS);
            html.push_str("\">");
            push_escaped(&mut html, cell);
            html.push_str("</td>");
        }
        html.push_str("</tr>");
    }
    html.push_str("</tbody></table>");

    Ok(html)
}

/// HTML escape into an existing buffer to avoid the temporary
/// allocation `format!` would do. Five-char OWASP set.
fn push_escaped(out: &mut String, input: &str) {
    for ch in input.chars() {
        match ch {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&#39;"),
            other => out.push(other),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn push_escaped_handles_owasp_five() {
        let mut out = String::new();
        push_escaped(&mut out, "<a href=\"x\">'&'</a>");
        assert!(!out.contains('<'));
        assert!(!out.contains('>'));
        assert!(out.contains("&lt;"));
        assert!(out.contains("&quot;"));
        assert!(out.contains("&#39;"));
        assert!(out.contains("&amp;"));
    }
}
