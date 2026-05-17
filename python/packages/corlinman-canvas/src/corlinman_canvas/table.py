"""`table` artifact adapter — GFM markdown or CSV → ``<table>``.

Python port of Rust ``adapters/table.rs``. Markdown is parsed with
``markdown-it-py`` (GFM tables extension); CSV uses stdlib ``csv``. HTML
emission is hand-rolled so we can stamp the Tidepool class names and
keep cells compact (``<td>cell</td>``, no wrapper ``<p>``).

Output shape (byte-equivalent to the Rust crate)::

    <table class="cn-canvas-table">
      <thead><tr class="cn-canvas-table-row">
        <th class="cn-canvas-table-cell">a</th> ...
      </tr></thead>
      <tbody>
        <tr class="cn-canvas-table-row">
          <td class="cn-canvas-table-cell">1</td> ...
        </tr>
      </tbody>
    </table>
"""

from __future__ import annotations

import csv as _csv
import io

from markdown_it import MarkdownIt

from .protocol import (
    AdapterError,
    ArtifactKind,
    RenderedArtifact,
    ThemeClass,
)

WRAPPER_CLASS = "cn-canvas-table"
ROW_CLASS = "cn-canvas-table-row"
CELL_CLASS = "cn-canvas-table-cell"


def _push_escaped(out: list[str], src: str) -> None:
    """Append HTML-escaped src into out; five-char OWASP set."""
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


def render(
    markdown: str | None,
    csv_src: str | None,
    theme_class: ThemeClass,
) -> RenderedArtifact:
    """Render a ``table`` artifact. Exactly one of ``markdown`` /
    ``csv_src`` must be set."""

    if markdown is not None and csv_src is not None:
        raise AdapterError(
            ArtifactKind.TABLE,
            "table body must specify exactly one of `markdown` or `csv`, not both",
        )
    if markdown is None and csv_src is None:
        raise AdapterError(
            ArtifactKind.TABLE,
            "table body must specify either `markdown` or `csv`",
        )

    if markdown is not None:
        html = _render_markdown(markdown)
    else:
        assert csv_src is not None  # for type narrowing
        html = _render_csv(csv_src)

    return RenderedArtifact(
        html_fragment=html,
        theme_class=theme_class,
        render_kind=ArtifactKind.TABLE,
        content_hash="",
        warnings=(),
    )


def _render_markdown(source: str) -> str:
    """Walk the markdown-it token stream and emit a single ``<table>``.

    Non-table events (paragraphs around the table) are ignored. If no
    table is present, raise :class:`AdapterError` so the producer learns
    about it (matches Rust ``table_markdown_without_table_rejected``).
    """

    md = MarkdownIt("commonmark").enable("table")
    tokens = md.parse(source)

    out: list[str] = []
    in_thead = False
    saw_table = False

    for tok in tokens:
        ttype = tok.type
        if ttype == "table_open":
            saw_table = True
            out.append(f'<table class="{WRAPPER_CLASS}">')
        elif ttype == "table_close":
            out.append("</table>")
        elif ttype == "thead_open":
            in_thead = True
            out.append("<thead>")
        elif ttype == "thead_close":
            in_thead = False
            out.append("</thead>")
        elif ttype == "tbody_open":
            out.append("<tbody>")
        elif ttype == "tbody_close":
            out.append("</tbody>")
        elif ttype == "tr_open":
            out.append(f'<tr class="{ROW_CLASS}">')
        elif ttype == "tr_close":
            out.append("</tr>")
        elif ttype == "th_open":
            out.append(f'<th class="{CELL_CLASS}">')
        elif ttype == "th_close":
            out.append("</th>")
        elif ttype == "td_open":
            out.append(f'<td class="{CELL_CLASS}">')
        elif ttype == "td_close":
            out.append("</td>")
        elif ttype == "inline":
            _emit_inline(out, tok.children or [], in_thead)
        # All other token types (paragraph_open/close, heading, etc.)
        # are dropped — table-only output.

    if not saw_table:
        raise AdapterError(
            ArtifactKind.TABLE, "markdown source contained no GFM table"
        )
    return "".join(out)


def _emit_inline(out: list[str], children: list, _in_thead: bool) -> None:
    """Emit inline children inside a cell.

    Mirrors the Rust adapter: ``text`` nodes are HTML-escaped; ``code``
    spans are wrapped in ``<code>``; any raw HTML is escaped (never
    passed through). Soft / hard breaks and other inline nodes are
    dropped — out of scope for table cells.
    """

    for child in children:
        ctype = child.type
        if ctype == "text":
            _push_escaped(out, child.content)
        elif ctype == "code_inline":
            out.append("<code>")
            _push_escaped(out, child.content)
            out.append("</code>")
        elif ctype in ("html_inline", "html_block"):
            # Never pass raw HTML through a cell — escape it as text so
            # a <script> in the producer's source surfaces as escaped
            # text, not executable markup.
            _push_escaped(out, child.content)
        # Drop everything else (softbreak, hardbreak, emphasis, etc.).


def _render_csv(source: str) -> str:
    """CSV → ``<table>``. First record is the header.

    Quoted fields with embedded commas (``"New York, NY"``) survive as
    one cell — that's stdlib ``csv``'s job.
    """

    reader = _csv.reader(io.StringIO(source))
    try:
        rows = list(reader)
    except _csv.Error as exc:
        raise AdapterError(
            ArtifactKind.TABLE, f"CSV parse failed: {exc}"
        ) from exc

    out: list[str] = []
    out.append(f'<table class="{WRAPPER_CLASS}">')

    if rows:
        headers = rows[0]
        if headers:
            out.append(f'<thead><tr class="{ROW_CLASS}">')
            for cell in headers:
                out.append(f'<th class="{CELL_CLASS}">')
                _push_escaped(out, cell)
                out.append("</th>")
            out.append("</tr></thead>")

        out.append("<tbody>")
        for record in rows[1:]:
            out.append(f'<tr class="{ROW_CLASS}">')
            for cell in record:
                out.append(f'<td class="{CELL_CLASS}">')
                _push_escaped(out, cell)
                out.append("</td>")
            out.append("</tr>")
        out.append("</tbody>")
    else:
        out.append("<tbody></tbody>")
    out.append("</table>")
    return "".join(out)


__all__ = ["CELL_CLASS", "ROW_CLASS", "WRAPPER_CLASS", "render"]
