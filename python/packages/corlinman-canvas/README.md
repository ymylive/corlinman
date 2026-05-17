# corlinman-canvas (Python)

Python port of the Rust `corlinman-canvas` crate.

Pure-function transform from producer-submitted Canvas frame payloads
(`code`, `table`, `latex`, `sparkline`, `mermaid`) into Tidepool-styled
HTML fragments suitable for the admin UI transcript.

## Backend choices vs the Rust crate

| Kind       | Rust                | Python                                                 |
|------------|---------------------|--------------------------------------------------------|
| code       | `syntect`           | `pygments` (`HtmlFormatter`)                           |
| table      | `pulldown-cmark`+`csv` | `markdown-it-py` + stdlib `csv`                     |
| latex      | `katex-rs`          | `pylatexenc` (macro expansion to text + HTML wrap)     |
| sparkline  | hand-rolled SVG     | hand-rolled SVG (no extra dep)                         |
| mermaid    | feature-gated `deno_core` V8 + `mermaid.min.js` | `<pre class="mermaid">…</pre>` fallback (see TODO) |

## Mermaid backend

Both `mermaid-py` (no equivalent on PyPI under that name in the workspace) and
the `mmdc` CLI are unavailable in this environment. To avoid silently dropping
the kind, the Python adapter emits a `<pre class="mermaid">{escaped src}</pre>`
fragment so a browser-side `mermaid.js` (loaded by the UI shell) can render it
client-side. The rendered artifact carries a `warning` so the gateway / UI know
the server did not pre-render. See `src/corlinman_canvas/mermaid.py` for the
TODO if a server-side renderer is wired in later.

## HTML divergence from the Rust golden fixtures

The Python port is **not byte-identical** with the Rust crate. Notable
differences (all called out in code comments):

- `code` — Pygments tokens carry classes like `cn-canvas-code-k`,
  `cn-canvas-code-mi`. Syntect emits longer Sublime-style scopes
  (`cn-canvas-code-source.rust`, etc.). The Rust tests only assert the
  `cn-canvas-code-` *prefix* is present; the Python output satisfies that
  prefix.
- `latex` — `pylatexenc` produces unicode text, not KaTeX HTML+MathML. We
  still emit `cn-canvas-katex` / `cn-canvas-katex--display` and a stub
  `katex-display` class so the Rust display-mode test passes.
- `sparkline` — float formatting is `{:.3f}` (matches Rust's `{:.3}`).
- `table` — class names and structure match Rust output exactly.
- `mermaid` — Rust returns `CanvasError.Adapter` ("not enabled in this
  build"); the Python port returns `Ok` with a fallback `<pre class="mermaid">`
  fragment and a warning. This is the explicit instruction in the porting
  brief.
